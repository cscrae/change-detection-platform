"""Project-local imagery, bounded window reads, WGS84 crops and Web Mercator tiles."""
from functools import lru_cache, wraps
from concurrent.futures import Future
from threading import Lock, RLock
from pathlib import Path
import calendar
import io
import json
import math
import os
import hashlib
import re
from datetime import date

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.transform import from_bounds
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds, transform_geom
from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union
from shapely.errors import GEOSException
from PIL import Image

PROJECT = Path(__file__).resolve().parents[3]
DATA = Path(os.getenv('ASSET_DIR', str(PROJECT / 'data')))
IMAGERY = Path(os.environ.get('LOCAL_IMAGERY_DIR', 'Y:/河北影像/2025年'))
BOUNDARY = Path(os.environ.get('HEBEI_BOUNDARY_FILE', str(DATA / 'boundaries/hebei-administrative-province.geojson')))
# Navigation envelope only, never represented as an administrative polygon.
HEBEI_BOUNDS = (113.45, 36.0, 119.9, 42.65)
MOSAIC_RULE_VERSION = 'directory-month-fine-first-v2'


class StaleMosaicVersion(ValueError):
    pass


def mosaic_version(rows):
    members = sorted((row['id'], row.get('version', ''), row.get('available', True), row.get('source_root', ''), row.get('source_priority', 0), row.get('date', '')) for row in rows)
    return hashlib.sha256(json.dumps([MOSAIC_RULE_VERSION, members], separators=(',', ':')).encode()).hexdigest()[:24]


def imagery_phases(rows, region_id):
    groups = {}
    for row in rows:
        month = row.get('date', '')[:7]
        if month:
            groups.setdefault(month, []).append(row)
    phases = []
    for month, members in sorted(groups.items()):
        members = sorted(members, key=lambda row: row['id'])
        usable = [row for row in members if row.get('available', True)]
        bounds = [row['bounds'] for row in usable]
        phases.append(dict(id=f'{region_id}:{month}', region_id=region_id, month=month,
                           scenes=members, available=bool(usable), version=mosaic_version(members),
                           bounds=[min(b[0] for b in bounds), min(b[1] for b in bounds),
                                   max(b[2] for b in bounds), max(b[3] for b in bounds)] if bounds else None))
    return phases


def resolve_mosaic_scene_ids(region_id, scene_ids=None, month=None, version=None):
    """Resolve a short, versioned phase query without trusting client membership.

    Callers that prepare/render a tile hold _catalog_lock across this resolution
    and their existing row/version validation, so refresh cannot split snapshots.
    Legacy explicit scene IDs retain the same downstream validation.
    """
    if month is not None and scene_ids is not None:
        raise ValueError('month 与 scene_ids 不能同时传入')
    if month is None:
        if scene_ids is None:
            raise ValueError('请选择 month 或 scene_ids')
        return scene_ids.split(',') if scene_ids else []
    if not isinstance(month, str) or not re.fullmatch(r'[0-9]{4}-(0[1-9]|1[0-2])', month):
        raise ValueError('month 必须使用 YYYY-MM 格式')
    if not version:
        raise ValueError('按 month 请求拼接瓦片时必须提供时相版本 v')
    from app.fetchers.regions import region_feature
    region_feature(region_id)
    with _catalog_lock:
        regional = [row for row in catalog() if row.get('region_id') == region_id]
        phase = next((item for item in imagery_phases(regional, region_id) if item['month'] == month), None)
        if phase is None:
            raise ValueError('该月份没有影像，请刷新影像目录')
        if phase['version'] != version:
            raise StaleMosaicVersion('时相已更新，请刷新影像目录')
        ids = [row['id'] for row in phase['scenes'] if row.get('available', True)]
        if not ids:
            raise ValueError('该月份没有可用影像')
        return ids


def single_flight(function):
    """Share concurrent work; failures are broadcast but never cached."""
    pending, lock = {}, Lock()

    @wraps(function)
    def run(*args):
        with lock:
            future = pending.get(args)
            owner = future is None
            if owner:
                future = pending[args] = Future()
        if not owner:
            return future.result()
        try:
            result = function(*args)
            future.set_result(result)
            return result
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with lock:
                pending.pop(args, None)

    run.cache_clear = function.cache_clear
    run.cache_info = function.cache_info
    return run


class BoundaryUnavailable(RuntimeError):
    """Never silently widen the permitted area when the boundary is missing."""


@lru_cache(maxsize=1)
def province():
    try:
        data = json.loads(BOUNDARY.read_text(encoding='utf-8'))
        features = data.get('features', [data])
        region = unary_union([shape(f.get('geometry', f)) for f in features])
        if (region.is_empty or not region.is_valid or
                region.geom_type not in ('Polygon', 'MultiPolygon') or
                not box(*HEBEI_BOUNDS).covers(region)):
            raise ValueError('省界必须是河北范围内有效的 WGS84 Polygon/MultiPolygon')
        return region
    except (OSError, ValueError, KeyError, TypeError, GEOSException) as exc:
        raise BoundaryUnavailable('河北省界文件缺失或无效，请恢复 data/boundaries/hebei-administrative-province.geojson 后重启服务') from exc


@lru_cache(maxsize=1)
def province_mercator():
    return transform_geom('EPSG:4326', 'EPSG:3857', mapping(province()))


_catalog_lock = RLock()
_catalog_generation = 0


def catalog(refresh=False):
    # Serialize explicit refresh with readers so one page load builds one snapshot.
    with _catalog_lock:
        if refresh:
            _clear_catalog()
        return _configured_catalog()


@lru_cache(maxsize=2)
def _catalog_snapshot(imagery_dir):
    imagery = Path(imagery_dir)
    manifest = imagery / 'catalog.json'
    if not manifest.exists():
        from app.fetchers.imagery_index import scan_catalog
        from app.fetchers.regions import region_collection
        names = {feature['properties']['name']: feature['properties']['region_id']
                 for feature in region_collection()['features']}
        return scan_catalog(imagery, DATA / 'cache/imagery', names)
    stat = manifest.stat()
    root = imagery.resolve()
    versions = []
    for row in _manifest_rows(str(manifest), stat.st_mtime_ns, stat.st_size):
        path = (root / row['file']).resolve()
        if not path.is_relative_to(root):
            raise ValueError('影像目录中的路径越界')
        try:
            source_stat = path.stat()
            versions.append((row['id'], source_stat.st_mtime_ns, source_stat.st_size))
        except OSError:
            versions.append((row['id'], 0, 0))
    return _catalog(str(root), stat.st_mtime_ns, stat.st_size, tuple(versions))


@lru_cache(maxsize=2)
def _manifest_rows(path, modified, size):
    return json.loads(Path(path).read_text(encoding='utf-8'))['images']


@lru_cache(maxsize=2)
def _catalog(imagery_dir, modified, size, source_versions):
    imagery = Path(imagery_dir)
    rows = _manifest_rows(str(imagery / 'catalog.json'), modified, size)
    result = []
    for row in rows:
        path = (imagery / row['file']).resolve()
        if not path.is_relative_to(imagery):
            raise ValueError('影像目录中的路径越界')
        try:
            with rasterio.Env(GDAL_PAM_ENABLED='NO'), rasterio.open(path) as src:
                if src.crs is None:
                    raise ValueError(f'影像缺少坐标系: {row["file"]}')
                if src.crs.is_projected:
                    resolution_m = max(src.res) * src.crs.linear_units_factor[1]
                else:
                    with WarpedVRT(src, crs='EPSG:3857') as reference:
                        resolution_m = max(reference.res)
                result.append({**row, 'available': True, 'error': '', 'filename': Path(row['file']).name,
                               'version': f'{path.stat().st_mtime_ns}:{path.stat().st_size}',
                               'resolution_m': round(resolution_m, 6),
                               'bounds': list(transform_bounds(src.crs, 'EPSG:4326', *src.bounds)),
                               'width': src.width, 'height': src.height, 'crs': str(src.crs),
                               'resolution': list(src.res)})
        except (OSError, ValueError, rasterio.errors.RasterioError) as exc:
            result.append({**row, 'available':False, 'error':str(exc), 'filename':Path(row['file']).name})
    return sorted(result, key=lambda r: (r['date'], r['id']))


def _clear_catalog():
    global _catalog_generation, _source_snapshot
    from app.fetchers.imagery_index import clear_cache
    with _catalog_lock:
        _catalog_snapshot.cache_clear()
        _catalog.cache_clear()
        _manifest_rows.cache_clear()
        _tile_metadata.cache_clear()
        _cached_tile.cache_clear()
        province.cache_clear()
        province_mercator.cache_clear()
        _catalog_generation += 1
        _source_snapshot = None
        clear_cache()


catalog.cache_clear = _clear_catalog


def selection(bounds, aoi=None, region_id=None, admin_code=None, admin_version=None):
    if admin_code:
        from app.fetchers.administrative import boundary_geometry
        geom = boundary_geometry(admin_code, admin_version, region_id)
        if aoi is not None and not geom.equals_exact(shape(aoi), 0):
            raise ValueError('检测轮廓与入库县级边界不一致')
        return geom
    if aoi is not None:
        if not region_id:
            raise ValueError('多边形选区必须指定当前行政区 region_id')
        if not isinstance(aoi, dict) or aoi.get('type') != 'Polygon':
            raise ValueError('aoi 必须为 GeoJSON Polygon')
        coordinates = aoi.get('coordinates')
        if not isinstance(coordinates, list) or len(coordinates) != 1:
            raise ValueError('多边形仅支持一个无孔闭合环')
        ring = coordinates[0]
        if not isinstance(ring, list) or len(ring) < 4:
            raise ValueError('多边形至少需要三个不同顶点并闭合')
        for point in ring:
            if (not isinstance(point, list) or len(point) != 2 or
                    not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in point) or
                    not (-180 <= point[0] <= 180 and -90 <= point[1] <= 90)):
                raise ValueError('多边形坐标必须为有效的 [经度, 纬度]')
        if ring[0] != ring[-1]:
            raise ValueError('多边形环必须闭合')
        if len(set(map(tuple, ring[:-1]))) != len(ring) - 1:
            raise ValueError('多边形顶点不能重复')
        geom = shape(aoi)
        if not geom.is_valid or geom.is_empty or geom.area <= 0:
            raise ValueError('多边形不能自交、折返或面积为零')
        from app.fetchers.regions import region_geometry
        region = region_geometry(region_id)
        if not region.buffer(1e-9).covers(geom):
            raise ValueError('多边形须完整位于当前行政区内，不能包含区外区域或孔洞')
        # Only numerical boundary tolerance is accepted; never silently repair geometry.
        geom = geom.intersection(region).intersection(province())
        if geom.is_empty or geom.area <= 0:
            raise ValueError('多边形选区没有行政区内有效面积')
        return geom
    if len(bounds) != 4 or not all(math.isfinite(v) for v in bounds):
        raise ValueError('范围必须为四个有限数值 [west, south, east, north]')
    w, s, e, n = bounds
    if not (w < e and s < n and -180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise ValueError('选区经纬度无效')
    geom = box(w, s, e, n).intersection(province())
    if geom.is_empty or geom.area == 0:
        raise ValueError('选区位于河北范围之外')
    return geom


def crop_images(bounds, start, end, limit, output_dir, region_id=None, scene_ids=None, aoi=None, admin_code=None, admin_version=None):
    from app.utils.progress import Progress
    progress = Progress(output_dir)
    progress("preparing", 0)
    if scene_ids is not None:
        return crop_mosaics(bounds, start, end, limit, output_dir, region_id, scene_ids, aoi, admin_code, admin_version)
    if admin_code:
        raise ValueError('县界检测必须指定当前来源及已选影像')
    start, end = date.fromisoformat(start), date.fromisoformat(end)
    if start > end:
        raise ValueError('起始日期不能晚于结束日期')
    aoi = selection(bounds, aoi, region_id)
    candidates = []
    for row in catalog():
        if not row.get('available', True) or (region_id and row.get('region_id') != region_id):
            continue
        acquired = date.fromisoformat(row['date'])
        precision = row.get('date_precision', 'month')
        period_start = acquired.replace(month=1, day=1) if precision == 'year' else acquired
        period_end = (acquired.replace(month=12, day=31) if precision == 'year' else
                      acquired if precision == 'day' else acquired.replace(day=calendar.monthrange(acquired.year, acquired.month)[1]))
        if period_start > end or period_end < start:
            continue
        overlap = aoi.intersection(box(*row['bounds']))
        if overlap.area > 0:
            candidates.append((row, overlap))
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    result = []
    written = []
    selected = candidates[:limit] if limit else candidates
    try:
        for index, (row, overlap) in enumerate(selected):
            progress("reading_images", 95 * index / max(1, len(selected)))
            with rasterio.Env(GDAL_PAM_ENABLED='NO'), rasterio.open(source_path(row)) as src:
                # Calculate native-scale geographic grid, then read only this AOI.
                with WarpedVRT(src, crs='EPSG:4326') as reference:
                    rx, ry = reference.res
                w, s, e, n = overlap.bounds
                width, height = max(1, math.ceil((e-w)/rx)), max(1, math.ceil((n-s)/ry))
                transform = from_bounds(w, s, e, n, width, height)
                with WarpedVRT(src, crs='EPSG:4326', transform=transform, width=width, height=height,
                               resampling=Resampling.bilinear, add_alpha=True) as vrt:
                    rgb = vrt.read([1, 2, 3])
                    valid = vrt.read(vrt.count) > 0
                progress("cropping", 95 * (index + .6) / max(1, len(selected)))
                valid &= geometry_mask([mapping(overlap)], (height, width), transform, invert=True)
                if not valid.any():
                    progress("cropping", 95 * (index + 1) / max(1, len(selected)), force=True)
                    continue
                rgb[:, ~valid] = 0
                path = output_dir / (row['id'] + '.tif')
                written.append(path)
                with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True):
                    with rasterio.open(path, 'w', driver='GTiff', width=width, height=height,
                                       count=3, dtype=rgb.dtype, crs='EPSG:4326', transform=transform,
                                       tiled=True, compress='deflate', BIGTIFF='IF_SAFER') as dst:
                        dst.write(rgb)
                        dst.write_mask(valid.astype('uint8') * 255)
                        dst.update_tags(source=row['file'], capture_month=row['date'][:7],
                                        date_precision=row.get('date_precision', 'month'))
                progress("saving_images", 95 * (index + 1) / max(1, len(selected)), force=True)
                result.append(dict(path=str(path.resolve()), date=row['date'], bounds=[w,s,e,n],
                                   width=width, height=height, bands=3, dtype=str(rgb.dtype),
                                   crs='EPSG:4326', source='local', source_file=row['file'],
                                   date_precision=row.get('date_precision', 'month')))
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    return {'images': result, 'count': len(result), 'dates_requested': [r['date'] for r,_ in selected]}


@single_flight
@lru_cache(maxsize=256)
def _tile_metadata(path, version):
    """Cache only immutable metadata, never share GDAL dataset handles across threads."""
    with rasterio.Env(GDAL_PAM_ENABLED='NO'), rasterio.open(path) as src:
        factors = tuple(src.overviews(1))
        if not factors:
            return factors, None
        with WarpedVRT(src, crs='EPSG:3857') as reference:
            return factors, max(reference.res)


def _tile_overview(path, target_resolution, version=None):
    # A fixed-size WarpedVRT can otherwise warp billions of native pixels at low zoom.
    # Open the finest sufficient overview explicitly; detection crops keep native IO.
    if version is None:
        stat = path.stat()
        version = f'{stat.st_mtime_ns}:{stat.st_size}'
    factors, resolution = _tile_metadata(str(path), version)
    if not factors:
        return -1
    scale = target_resolution / resolution
    return max((index for index, factor in enumerate(factors) if factor <= scale), default=-1)


def _tile_rows(scene, z, x, y, region_id=None):
    if not (0 <= z <= 20 and 0 <= x < 2**z and 0 <= y < 2**z):
        raise ValueError('瓦片坐标无效')
    region = province()
    if region_id is not None:
        from app.fetchers.regions import region_geometry
        region = region.intersection(region_geometry(region_id))
    rows = [row for row in catalog() if row.get('available', True)]
    if scene == 'latest':
        latest = {}
        for row in rows:
            latest[row['region']] = row
        rows = list(latest.values())
    else:
        rows = [r for r in rows if r['id'] == scene]
        if not rows:
            raise KeyError(scene)
    if region_id is not None:
        rows = [row for row in rows if row.get('region_id') == region_id]
        if not rows:
            raise KeyError(scene)
    return rows


def render_tile(scene, z, x, y, region_id=None):
    rows = _tile_rows(scene, z, x, y, region_id)
    return _cached_tile(tuple((row['id'], row.get('version', '')) for row in rows), region_id, z, x, y)


def selected_scenes(region_id, scene_ids, single_month=False):
    if not region_id or not scene_ids:
        raise ValueError('请选择行政区及至少一张影像')
    from app.fetchers.regions import region_feature
    region_feature(region_id)
    rows = {row['id']: row for row in catalog()}
    selected = []
    for scene_id in dict.fromkeys(scene_ids):
        row = rows.get(scene_id)
        if row is None or row.get('region_id') != region_id:
            raise ValueError(f'影像不属于当前行政区: {scene_id}')
        if not row.get('available', True):
            raise ValueError(f'影像不可用: {row.get("file")}: {row.get("error")}')
        selected.append(row)
    if single_month and len({row['date'][:7] for row in selected}) != 1:
        raise ValueError('拼接底图只能包含同月影像')
    return selected


def mosaic_order(rows):
    return sorted(rows, key=lambda row: (row.get('resolution_m', max(row.get('resolution', [float('inf')]))), row.get('source_priority', 0), row['file']))


def read_mosaic(rows, crs, transform, width, height, tile_resolution=None, datasets=None, progress=None):
    rgb = np.zeros((3, height, width), dtype='uint8')
    valid = np.zeros((height, width), dtype=bool)
    with rasterio.Env(GDAL_PAM_ENABLED='NO'):
        for source_index, row in enumerate(mosaic_order(rows)):
            path = datasets.source_path(row) if datasets is not None else source_path(row)
            options = {'overview_level': _tile_overview(path, tile_resolution, row.get('version'))} if tile_resolution else {}
            source = datasets.open(path, row.get('version'), **options) if datasets is not None else rasterio.open(path, **options)
            with source as src:
                with WarpedVRT(src, crs=crs, transform=transform, width=width, height=height,
                               resampling=Resampling.bilinear, add_alpha=True) as vrt:
                    pixels = vrt.read([1, 2, 3])
                    fill = (vrt.read(vrt.count) > 0) & ~valid
                    rgb[:, fill] = pixels[:, fill]
                    valid |= fill
            if progress:
                progress(source_index + 1, len(rows))
            if valid.all():
                break
    return rgb, valid


def _mosaic_rows(region_id, scene_ids, z, x, y, version=None):
    if not (0 <= z <= 20 and 0 <= x < 2**z and 0 <= y < 2**z):
        raise ValueError('瓦片坐标无效')
    if version:
        regional = [row for row in catalog() if row.get('region_id') == region_id]
        phases = imagery_phases(regional, region_id)
        if not any(phase['version'] == version and
                   {row['id'] for row in phase['scenes'] if row.get('available', True)} == set(scene_ids)
                   for phase in phases):
            raise StaleMosaicVersion('时相已更新，请刷新影像目录')
    return selected_scenes(region_id, scene_ids, single_month=True)


def render_mosaic(region_id, scene_ids, z, x, y, version=None):
    rows = _mosaic_rows(region_id, scene_ids, z, x, y, version)
    return _cached_tile(tuple(sorted((row['id'], row.get('version', '')) for row in rows)), region_id, z, x, y)


@single_flight
@lru_cache(maxsize=512)
def _cached_tile(versions, region_id, z, x, y):
    wanted = {scene_id for scene_id, _ in versions}
    rows = [row for row in catalog() if row['id'] in wanted]
    if sorted((row['id'], row.get('version', '')) for row in rows) != sorted(versions):
        raise StaleMosaicVersion('时相已更新，请刷新影像目录')
    return _render_tile_rows(rows, region_id, z, x, y)


def _render_tile_rows(rows, region_id, z, x, y, datasets=None):
    region = province()
    if region_id is not None:
        from app.fetchers.regions import region_geometry
        region = region.intersection(region_geometry(region_id))
    extent = 20037508.342789244
    step = 2 * extent / 2**z
    bounds = (-extent+x*step, extent-(y+1)*step, -extent+(x+1)*step, extent-y*step)
    wgs = box(*transform_bounds('EPSG:3857', 'EPSG:4326', *bounds))
    transform = from_bounds(*bounds, 256, 256)
    rows = [row for row in rows if wgs.intersects(region) and wgs.intersects(box(*row['bounds']))]
    rgb, valid = read_mosaic(rows, 'EPSG:3857', transform, 256, 256, step / 256, datasets=datasets)
    rgba = np.zeros((256, 256, 4), dtype='uint8')
    rgba[:, :, :3] = rgb.transpose(1, 2, 0)
    rgba[:, :, 3] = valid.astype('uint8') * 255
    clip = province_mercator() if region_id is None else transform_geom('EPSG:4326', 'EPSG:3857', mapping(region))
    valid = geometry_mask([clip], (256,256), transform, invert=True)
    rgba[~valid] = 0
    stream = io.BytesIO()
    Image.fromarray(rgba).save(stream, format='PNG')
    return stream.getvalue()


render_tile.cache_clear = _cached_tile.cache_clear
render_mosaic.cache_clear = _cached_tile.cache_clear


@lru_cache(maxsize=512)
def _crop_resolution(path, version):
    # Metadata only; cache by source version so dragging does not reopen rasters.
    with rasterio.Env(GDAL_PAM_ENABLED='NO'), rasterio.open(path) as src:
        with WarpedVRT(src, crs='EPSG:4326') as reference:
            return reference.res


def _mosaic_grid(aoi, sources):
    contributing = [row for row in sources if aoi.intersection(box(*row['bounds'])).area > 0]
    if not contributing:
        raise ValueError('已选影像没有覆盖检测区域')
    overlap = aoi.intersection(unary_union([box(*row['bounds']) for row in contributing]))
    resolutions = [_crop_resolution(str(source_path(row)), row.get('version')) for row in contributing]
    rx, ry = min(res[0] for res in resolutions), min(res[1] for res in resolutions)
    w, s, e, n = overlap.bounds
    width, height = max(1, math.ceil((e-w)/rx)), max(1, math.ceil((n-s)/ry))
    return contributing, overlap, width, height


def selection_pixels(bounds, region_id, scene_ids, aoi=None, admin_code=None, admin_version=None):
    from app.fetchers.regions import region_geometry
    sources = selected_scenes(region_id, scene_ids, single_month=True)
    aoi = selection(bounds, aoi, region_id, admin_code, admin_version)
    if not admin_code:
        aoi = aoi.intersection(region_geometry(region_id))
    if aoi.is_empty or aoi.area <= 0:
        raise ValueError('选区不在当前行政区内')
    _, _, width, height = _mosaic_grid(aoi, sources)
    return dict(width=width, height=height, pixels=width * height)


def crop_mosaics(bounds, start, end, limit, output_dir, region_id, scene_ids, aoi=None, admin_code=None, admin_version=None):
    from app.utils.progress import Progress
    progress = Progress(output_dir)
    progress("preparing", 0)
    start, end = date.fromisoformat(start), date.fromisoformat(end)
    if start > end:
        raise ValueError('起始日期不能晚于结束日期')
    rows = selected_scenes(region_id, scene_ids)
    from app.fetchers.regions import region_geometry
    aoi = selection(bounds, aoi, region_id, admin_code, admin_version)
    if not admin_code:
        aoi = aoi.intersection(region_geometry(region_id))
    if aoi.is_empty or aoi.area <= 0:
        raise ValueError('选区不在当前行政区内')
    phases = {}
    for row in rows:
        acquired = date.fromisoformat(row['date'][:7] + '-01')
        annual = row.get('date_precision') == 'year'
        period_start = acquired.replace(month=1, day=1) if annual else acquired
        period_end = (acquired.replace(month=12, day=31) if annual else
                      acquired.replace(day=calendar.monthrange(acquired.year, acquired.month)[1]))
        if period_start <= end and period_end >= start:
            phases.setdefault(row['date'][:7], []).append(row)
    phases = sorted(phases.items())
    if limit:
        phases = phases[:limit]
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    written, images = [], []
    try:
        for index, (month, sources) in enumerate(phases):
            progress("mosaicking", 95 * index / max(1, len(phases)))
            try:
                contributing, overlap, width, height = _mosaic_grid(aoi, sources)
            except ValueError as exc:
                raise ValueError(f'{month}: {exc}') from exc
            date_precision = 'year' if any(row.get('date_precision') == 'year' for row in contributing) else 'month'
            w, s, e, n = overlap.bounds
            transform = from_bounds(w, s, e, n, width, height)
            rgb, valid = read_mosaic(contributing, 'EPSG:4326', transform, width, height,
                progress=lambda done, total: progress("mosaicking", 95 * (index + .7 * done / total) / len(phases)))
            progress("saving_images", 95 * (index + .8) / len(phases))
            valid &= geometry_mask([mapping(overlap)], (height, width), transform, invert=True)
            if not valid.any():
                raise ValueError(f'{month} 的已选影像在检测区域没有有效像元')
            rgb[:, ~valid] = 0
            key = hashlib.sha256(','.join(sorted(row['id'] for row in sources)).encode()).hexdigest()[:16]
            path = output_dir / f'mosaic-{month}-{key}.tif'
            written.append(path)
            files = [row['file'] for row in mosaic_order(sources)]
            with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True):
                with rasterio.open(path, 'w', driver='GTiff', width=width, height=height, count=3,
                                   dtype='uint8', crs='EPSG:4326', transform=transform,
                                   tiled=True, compress='deflate', BIGTIFF='IF_SAFER') as dst:
                    dst.write(rgb)
                    dst.write_mask(valid.astype('uint8') * 255)
                    dst.update_tags(source_files=json.dumps(files, ensure_ascii=False), capture_month=month,
                                    date_precision=date_precision)
            progress("saving_images", 95 * (index + 1) / len(phases), force=True)
            images.append(dict(path=str(path.resolve()), date=month + '-01', bounds=[w, s, e, n],
                               width=width, height=height, bands=3, dtype='uint8', crs='EPSG:4326',
                               source='local', source_file=files[0], source_files=files,
                               scene_ids=[row['id'] for row in sources], date_precision=date_precision))
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    return {'images': images, 'count': len(images), 'dates_requested': [month + '-01' for month, _ in phases]}


_source_snapshot = None

def source_path(row):
    root = Path(row.get('source_root', str(IMAGERY))).resolve()
    path = (root / row['file']).resolve()
    if not path.is_relative_to(root):
        raise ValueError('影像路径越界')
    return path

def _configured_catalog():
    global _source_snapshot
    from app import settings
    if settings.active_settings is None:
        return _catalog_snapshot(str(IMAGERY))
    if _source_snapshot is None:
        _source_snapshot = build_source_catalog(settings.active_settings.sources)
    return _source_snapshot

def build_source_catalog(sources):
    from app.fetchers.imagery_index import scan_catalog
    from app.fetchers.regions import region_collection
    from app.settings import DATA_ROOT
    names = {f['properties']['name']: f['properties']['region_id'] for f in region_collection()['features']}
    rows = []
    for priority, source in enumerate(sources):
        if not source.enabled:
            continue
        root = Path(source.path).expanduser().resolve()
        if not root.is_dir():
            raise OSError(f'影像目录不可访问: {root}')
        found = (_catalog_snapshot(str(root)) if (root / 'catalog.json').exists()
                 else scan_catalog(root, DATA_ROOT / 'cache/imagery', names, source.year))
        for row in found:
            if source.year and row.get('date'):
                row = {**row, 'date':str(source.year) + row['date'][4:]}
            rows.append({**row, 'id': source.id + ':' + row['id'], 'source_id': source.id,
                         'source_root': str(root), 'source_priority': priority})
    return sorted(rows, key=lambda r: (r.get('date','9999'), r['id']))
