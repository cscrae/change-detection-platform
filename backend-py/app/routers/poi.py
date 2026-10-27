"""Public read-only POI lookup. Coordinates returned to maps always use WGS84."""
from fastapi import APIRouter, HTTPException, Query

from app.fetchers.poi import parse_bbox, query_poi

router = APIRouter(prefix='/py/local', tags=['local-poi'])


@router.get('/poi')
def local_poi(bbox: str = Query(..., max_length=180), limit: int = Query(500, ge=1, le=2000)):
    try:
        return query_poi(parse_bbox(bbox), limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
