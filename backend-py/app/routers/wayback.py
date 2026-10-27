"""Local-only imagery endpoints; /wayback retained as a compatibility alias."""
from pathlib import Path
from functools import partial
from typing import Any, List
from fastapi import APIRouter, HTTPException, Response, Query, Request
from app.utils.tile_queue import tile_response
from app.utils.tile_worker import prepare_job
from pydantic import BaseModel, Field
from app.fetchers.local import (catalog, crop_images, render_tile, render_mosaic, province,
                                BoundaryUnavailable, imagery_phases, StaleMosaicVersion, selection_pixels,
                                resolve_mosaic_scene_ids)
from app.fetchers import local
from shapely.geometry import mapping
from app.fetchers.regions import region_collection, region_feature, display_sources

router = APIRouter(prefix='/py', tags=['local-imagery'])

class LocalFetchRequest(BaseModel):
    admin_code: str | None = None
    admin_version: str | None = None
    aoi: Any = Field(None, description='手绘无孔 Polygon；admin_code 模式使用知识库完整 Polygon/MultiPolygon')
    bounds: List[float] = Field(..., min_length=4, max_length=4)
    start_date: str = '0001-01-01'
    end_date: str = '9999-12-31'
    # Omitted (or zero) retrieves every available phase; retained for API compatibility.
    max_images: int = Field(0, ge=0, le=20)
    output_dir: str
    region_id: str | None = None
    scene_ids: List[str] | None = None

class SelectionPixelsRequest(BaseModel):
    admin_code: str | None = None
    admin_version: str | None = None
    aoi: Any = Field(None, description='手绘无孔 Polygon；admin_code 模式使用知识库完整 Polygon/MultiPolygon')
    bounds: List[float] = Field(..., min_length=4, max_length=4)
    region_id: str
    scene_ids: List[str] = Field(..., min_length=1)


@router.post('/local/selection-pixels')
def get_selection_pixels(req: SelectionPixelsRequest):
    try:
        return selection_pixels(req.bounds, req.region_id, req.scene_ids, req.aoi, req.admin_code, req.admin_version)
    except (ValueError, KeyError) as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    except (BoundaryUnavailable, OSError) as exc:
        raise HTTPException(503, detail=str(exc)) from exc


@router.post('/local/fetch')
@router.post('/wayback/fetch', deprecated=True)
def fetch_local(req: LocalFetchRequest):
    if 'scene_ids' in req.model_fields_set and req.scene_ids is None:
        raise HTTPException(400, detail='请选择行政区及至少一张影像')
    if not Path(req.output_dir).is_absolute():
        raise HTTPException(400, detail='output_dir 必须是绝对路径')
    try:
        result = crop_images(req.bounds, req.start_date, req.end_date, req.max_images, req.output_dir,
                             req.region_id, req.scene_ids, req.aoi, req.admin_code, req.admin_version)
        # Confirm the validated AOI so older workers cannot silently crop a box.
        if req.aoi is not None:
            result['aoi'] = req.aoi
        if req.admin_code:
            from app.fetchers.administrative import boundary_feature
            feature = boundary_feature(req.admin_code, req.admin_version, req.region_id)
            result['aoi'] = feature['geometry']
            result['admin_code'] = req.admin_code
            result['admin_version'] = feature['properties']['boundary_version']
        return result
    except BoundaryUnavailable as exc:
        raise HTTPException(503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(400, detail='行政区不存在') from exc
    except OSError as exc:
        raise HTTPException(503, detail=f'影像目录不可用: {exc}') from exc

@router.get('/local/catalog')
def list_catalog(refresh: bool = False):
    try:
        boundary = province()
        return {'images': catalog(refresh=refresh), 'bounds': list(boundary.bounds),
                'boundary': mapping(boundary), 'boundary_mode': 'administrative', 'warning': None}
    except BoundaryUnavailable as exc:
        raise HTTPException(503, detail=str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(503, detail=f'影像目录不可用: {exc}') from exc


@router.get('/local/regions/{region_id}/scenes')
def list_region_scenes(region_id: str, refresh: bool = False, admin_code: str | None = None):
    try:
        feature = region_feature(region_id)
        rows = sorted((row for row in catalog(refresh=refresh) if row.get('region_id') == region_id),
                      key=lambda row: (row.get('date', '9999'), row['file']))
        phases = imagery_phases(rows, region_id)
        if admin_code:
            from app.fetchers.administrative import boundary_geometry
            from shapely.geometry import box
            try:
                geometry = boundary_geometry(admin_code, source_id=region_id)
            except ValueError as exc:
                raise HTTPException(400, detail=str(exc)) from exc
            rows = [row for row in rows if row.get('bounds') and geometry.intersection(box(*row['bounds'])).area > 0]
            # The workspace loads whole source/month mosaics. Keep their original
            # scene membership and version; county filtering only narrows candidates.
            matches = {row['id'] for row in rows if row.get('available', True)}
            phases = [{**phase, 'matching_scene_count': sum(row['id'] in matches for row in phase['scenes'])}
                      for phase in phases if any(row['id'] in matches for row in phase['scenes'])]
        return {'region_id': region_id, 'name': feature['properties']['name'], 'scenes': rows,
                'phases': phases}
    except KeyError as exc:
        raise HTTPException(404, detail='行政区不存在') from exc
    except (BoundaryUnavailable, OSError, ValueError) as exc:
        raise HTTPException(503, detail=f'影像目录不可用: {exc}') from exc


@router.get('/local/regions/{region_id}/mosaic/{z}/{x}/{y}.png')
async def mosaic_tile_http(request: Request, region_id: str, z: int, x: int, y: int,
                           scene_ids: str | None = Query(None), v: str | None = None,
                           month: str | None = None):
    prepare = partial(_prepare_mosaic_job, region_id, z, x, y, scene_ids, v, month)
    return await _render_http(request, prepare, 'private, max-age=86400' if v else 'no-cache')


def _prepare_mosaic_job(region_id, z, x, y, scene_ids=None, version=None, month=None):
    # Keep phase resolution and the original prepare_job's strict row/version
    # validation in one catalogue snapshot; refresh uses this same RLock.
    with local._catalog_lock:
        ids = resolve_mosaic_scene_ids(region_id, scene_ids, month, version)
        return prepare_job('mosaic', region_id, None, ids, z, x, y, version)


def mosaic_tile(region_id: str, z: int, x: int, y: int, scene_ids: str | None = None,
                v: str | None = None, month: str | None = None):
    try:
        with local._catalog_lock:
            ids = resolve_mosaic_scene_ids(region_id, scene_ids, month, v)
            return Response(render_mosaic(region_id, ids, z, x, y, v), media_type='image/png',
                            headers={'Cache-Control': 'private, max-age=86400' if v else 'no-cache'})
    except StaleMosaicVersion as exc:
        raise HTTPException(409, detail=str(exc), headers={'Cache-Control': 'no-store'}) from exc
    except KeyError as exc:
        raise HTTPException(404, detail='行政区不存在') from exc
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    except (BoundaryUnavailable, OSError) as exc:
        raise HTTPException(503, detail=f'影像服务不可用: {exc}') from exc

@router.get('/local/regions')
def list_regions():
    try:
        boundary = province()
        return {**region_collection(), 'boundary': mapping(boundary), 'bounds': list(boundary.bounds)}
    except BoundaryUnavailable as exc:
        raise HTTPException(503, detail=str(exc)) from exc


@router.get('/local/regions/{region_id}/imagery')
def list_region_imagery(region_id: str, year: int | None = Query(None, ge=1900, le=9999)):
    try:
        return display_sources(region_id, year)
    except BoundaryUnavailable as exc:
        raise HTTPException(503, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, detail='行政区或影像清单字段不存在') from exc
    except (OSError, ValueError, TypeError) as exc:
        raise HTTPException(503, detail=f'影像目录不可用: {exc}') from exc


@router.get('/local/regions/{region_id}/tiles/{scene}/{z}/{x}/{y}.png')
async def region_tile_http(request: Request, region_id: str, scene: str, z: int, x: int, y: int):
    return await _render_http(request, partial(prepare_job, 'tile', region_id, scene, None, z, x, y),
                              'public, max-age=3600')


def region_tile(region_id: str, scene: str, z: int, x: int, y: int):
    try:
        return Response(render_tile(scene, z, x, y, region_id), media_type='image/png',
                        headers={'Cache-Control': 'public, max-age=3600'})
    except BoundaryUnavailable as exc:
        raise HTTPException(503, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, detail='该区域影像不存在') from exc
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc


@router.get('/wayback/dates', deprecated=True)
def list_dates():
    dates = sorted({r['date'] for r in catalog() if r.get('available', True) and r.get('date')})
    return {'dates': dates, 'count': len(dates), 'date_precision': 'month'}

@router.get('/local/tiles/{scene}/{z}/{x}/{y}.png')
async def tile_http(request: Request, scene: str, z: int, x: int, y: int):
    return await _render_http(request, partial(prepare_job, 'tile', None, scene, None, z, x, y),
                              'public, max-age=3600')


def tile(scene: str, z: int, x: int, y: int):
    try:
        return Response(render_tile(scene,z,x,y), media_type='image/png', headers={'Cache-Control':'public, max-age=3600'})
    except BoundaryUnavailable as exc:
        raise HTTPException(503, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, detail='本地影像不存在') from exc
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc


async def _render_http(request, prepare, cache_control):
    try:
        return await tile_response(request, prepare, cache_control)
    except StaleMosaicVersion as exc:
        raise HTTPException(409, detail=str(exc), headers={'Cache-Control': 'no-store'}) from exc
    except KeyError as exc:
        raise HTTPException(404, detail='行政区或影像不存在') from exc
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    except (BoundaryUnavailable, OSError) as exc:
        raise HTTPException(503, detail=f'影像服务不可用: {exc}', headers={'Cache-Control': 'no-store'}) from exc
