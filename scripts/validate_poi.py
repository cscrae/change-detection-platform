"""Validate and summarize local POI data without writing files or using a network."""
import argparse
import json
import os
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'backend-py'))

from app.fetchers.poi import parse_bbox, query_poi  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path', type=Path, help='GeoJSON file; overrides POI_GEOJSON_PATH for this process only')
    parser.add_argument('--bbox', default='-180,-90,180,90', help='WGS84 west,south,east,north')
    parser.add_argument('--limit', type=int, default=500, help='Maximum returned points, 1..2000')
    parser.add_argument('--show-features', action='store_true', help='Also print sanitized POI features to stdout')
    args = parser.parse_args()
    if args.path is not None:
        os.environ['POI_GEOJSON_PATH'] = str(args.path.expanduser().resolve())
    try:
        result = query_poi(parse_bbox(args.bbox), args.limit)
    except ValueError as exc:
        parser.error(str(exc))
    result['returned_count'] = len(result['features'])
    if not args.show_features:
        del result['features']
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'ready' else 1


if __name__ == '__main__':
    raise SystemExit(main())
