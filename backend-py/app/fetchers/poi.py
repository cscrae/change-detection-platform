"""Read-only, bounded local POI GeoJSON queries; no external POI service is used."""
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from hashlib import sha256
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import threading
import unicodedata

from app import settings

MAX_BYTES = 64 * 1024 * 1024
MAX_FILES = 32
MAX_FEATURES = 250_000
MAX_LIMIT = 2000
WEB_MERCATOR_LIMIT = math.pi * 6378137
_lock = threading.RLock()
_cache_key = None
_cache_value = None


class POIDataError(ValueError):
    """An unusable dataset, with a public message that contains no server paths."""


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.blocked = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.blocked += 1

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self.blocked:
            self.blocked -= 1

    def handle_data(self, text):
        if not self.blocked:
            self.parts.append(text)


def plain_text(value, maximum=120):
    """Keep bounded display text, never rich HTML, URLs, or arbitrary properties."""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ''
    parser = _PlainText()
    parser.feed(str(value)[:maximum * 8])
    parser.close()
    text = ' '.join(parser.parts).replace('<', '').replace('>', '')
    text = ''.join(' ' if unicodedata.category(char).startswith('C') else char for char in text)
    return re.sub(r'\s+', ' ', text).strip()[:maximum]


def parse_bbox(value):
    try:
        values = [float(item.strip()) for item in value.split(',')]
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError('bbox 必须是西、南、东、北四个逗号分隔的经纬度') from exc
    if len(values) != 4 or not all(math.isfinite(item) for item in values):
        raise ValueError('bbox 必须包含四个有限数值')
    w, s, e, n = values
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise ValueError('bbox 经度须在 -180 至 180、纬度须在 -90 至 90，且西小于东、南小于北')
    return values


def _coordinate_system(document):
    declarations = []
    if 'coordinate_system' in document:
        declarations.append(document['coordinate_system'])
    if 'crs' in document:
        crs = document['crs']
        if not isinstance(crs, dict) or crs.get('type') != 'name':
            raise POIDataError('POI crs 必须是明确的 EPSG:4326 或 EPSG:3857 名称声明')
        properties = crs.get('properties')
        if not isinstance(properties, dict):
            raise POIDataError('POI crs 声明缺少有效的 properties.name')
        declarations.append(properties.get('name'))
    systems = []
    for value in declarations:
        name = value.strip().upper() if isinstance(value, str) else ''
        aliases = {
            'EPSG:4326': 'EPSG:4326', 'OGC:CRS84': 'EPSG:4326', 'CRS84': 'EPSG:4326',
            'URN:OGC:DEF:CRS:EPSG::4326': 'EPSG:4326',
            'URN:OGC:DEF:CRS:OGC:1.3:CRS84': 'EPSG:4326',
            'HTTP://WWW.OPENGIS.NET/DEF/CRS/EPSG/0/4326': 'EPSG:4326',
            'EPSG:3857': 'EPSG:3857', 'URN:OGC:DEF:CRS:EPSG::3857': 'EPSG:3857',
            'HTTP://WWW.OPENGIS.NET/DEF/CRS/EPSG/0/3857': 'EPSG:3857',
        }
        if name not in aliases:
            raise POIDataError('不支持 POI 坐标系；仅接受 WGS84 / EPSG:4326 或 EPSG:3857，不自动转换 GCJ-02 / BD-09')
        systems.append(aliases[name])
    if len(set(systems)) > 1:
        raise POIDataError('POI 的 crs 与 coordinate_system 声明不一致')
    # RFC 7946 GeoJSON without an explicit CRS uses WGS84 longitude, latitude.
    return systems[0] if systems else 'EPSG:4326'


def _point(geometry, crs):
    if not isinstance(geometry, dict) or geometry.get('type') != 'Point':
        return None
    coordinates = geometry.get('coordinates')
    if not isinstance(coordinates, list) or not 2 <= len(coordinates) <= 3:
        return None
    try:
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
               for v in coordinates):
            return None
    except OverflowError:
        return None
    x, y = coordinates[:2]
    if crs == 'EPSG:3857':
        if abs(x) > WEB_MERCATOR_LIMIT or abs(y) > WEB_MERCATOR_LIMIT:
            return None
        x = math.degrees(x / 6378137)
        y = math.degrees(math.atan(math.sinh(y / 6378137)))
    if not (-180 <= x <= 180 and -90 <= y <= 90):
        return None
    return float(x), float(y)


@dataclass(frozen=True)
class Dataset:
    points: tuple
    longitudes: tuple
    sources: tuple
    skipped: int


def clear_cache():
    global _cache_key, _cache_value
    with _lock:
        _cache_key, _cache_value = None, None


def _source_files():
    configured_path = os.getenv('POI_GEOJSON_PATH', '').strip()
    if configured_path:
        path = Path(configured_path).expanduser()
        if not path.is_file():
            return [], True, 'unavailable', '已配置的 POI 数据文件不可读取，请检查服务器数据挂载'
        return [path], True, '', ''
    directory = settings.ASSET_ROOT / 'poi'
    if not directory.exists():
        return [], False, 'not_configured', '尚未配置 POI 数据，请在 ASSET_DIR/poi 放置 GeoJSON，或设置 POI_GEOJSON_PATH'
    files = sorted(directory.glob('*.geojson'), key=lambda item: item.name)
    if not files:
        return [], False, 'not_configured', '尚未配置 POI GeoJSON 数据'
    return files, True, '', ''


def _load(files):
    global _cache_key, _cache_value
    if len(files) > MAX_FILES:
        raise POIDataError('POI 文件数超过 32 个，请合并数据或指定一个 POI_GEOJSON_PATH 文件')
    def signature_for_files():
        items = []
        for path in files:
            stat = path.stat()
            items.append((str(path.resolve()), stat.st_mtime_ns, stat.st_size))
        return tuple(items)

    signature = signature_for_files()
    if sum(item[2] for item in signature) > MAX_BYTES:
        raise POIDataError('POI 数据总大小超过 64 MiB，请裁剪或拆分服务数据')
    with _lock:
        if signature == _cache_key:
            if isinstance(_cache_value, POIDataError):
                raise _cache_value
            return _cache_value
        points, sources, skipped, feature_count = [], [], 0, 0
        try:
            for path in files:
                with path.open('rb') as stream:
                    raw = stream.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise POIDataError('POI 数据大小超过 64 MiB')
                try:
                    document = json.loads(raw.decode('utf-8-sig'))
                except (UnicodeError, ValueError, RecursionError) as exc:
                    raise POIDataError('POI 文件不是有效的 UTF-8 GeoJSON') from exc
                if not isinstance(document, dict) or document.get('type') != 'FeatureCollection' or not isinstance(document.get('features'), list):
                    raise POIDataError('POI 数据必须是 GeoJSON FeatureCollection')
                crs = _coordinate_system(document)
                source_id = 'local-poi-' + sha256(path.name.encode('utf-8')).hexdigest()[:12]
                sources.append({'id': source_id, 'name': plain_text(document.get('name')) or '本地 POI',
                                'coordinate_system': crs})
                feature_count += len(document['features'])
                if feature_count > MAX_FEATURES:
                    raise POIDataError('POI 数据超过 250000 个要素，请裁剪或拆分服务数据')
                for feature in document['features']:
                    if not isinstance(feature, dict) or feature.get('type') != 'Feature':
                        skipped += 1
                        continue
                    geometry = feature.get('geometry')
                    # A GeoJSON collection has one CRS; never guess per-point offsets.
                    if 'crs' in feature or 'coordinate_system' in feature or (isinstance(geometry, dict) and ('crs' in geometry or 'coordinate_system' in geometry)):
                        raise POIDataError('POI 坐标系须在 FeatureCollection 顶层统一声明')
                    point = _point(geometry, crs)
                    if point is None:
                        skipped += 1
                        continue
                    properties = feature.get('properties')
                    properties = properties if isinstance(properties, dict) else {}
                    safe = {'source_id': source_id}
                    for key, maximum in (('name', 120), ('category', 80), ('address', 240)):
                        text = plain_text(properties.get(key), maximum)
                        if text:
                            safe[key] = text
                    points.append((point[0], point[1], safe))
            after = signature_for_files()
            if after != signature:
                raise POIDataError('POI 文件正在更新，请稍后刷新')
            points.sort(key=lambda item: (item[0], item[1]))
            dataset = Dataset(tuple(points), tuple(item[0] for item in points), tuple(sources), skipped)
        except POIDataError as exc:
            _cache_key, _cache_value = signature, exc
            raise
        _cache_key, _cache_value = signature, dataset
        return dataset


def query_poi(bounds, limit=500):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ValueError('limit 必须是 1 至 2000 的整数')
    # Validate direct callers too; URL parsing is not the only possible entry point.
    bounds = parse_bbox(','.join(str(value) for value in bounds))
    result = {'type': 'FeatureCollection', 'features': [], 'configured': bool(os.getenv('POI_GEOJSON_PATH', '').strip()),
              'coordinate_system': 'EPSG:4326', 'source_coordinate_system': None,
              'status': 'not_configured', 'matched_count': 0, 'truncated': False,
              'skipped_count': 0, 'sources': [], 'message': ''}
    try:
        files, configured, status, message = _source_files()
        result['configured'] = configured
        if not files:
            result.update(status=status, message=message)
            return result
        dataset = _load(files)
    except POIDataError as exc:
        result.update(status='invalid_data', message=str(exc))
        return result
    except OSError:
        result.update(status='unavailable', message='无法读取 POI 数据，请检查服务器数据挂载与访问权限')
        return result
    w, s, e, n = bounds
    left, right = bisect_left(dataset.longitudes, w), bisect_right(dataset.longitudes, e)
    for lng, lat, properties in dataset.points[left:right]:
        if not s <= lat <= n:
            continue
        result['matched_count'] += 1
        if len(result['features']) < limit:
            result['features'].append({'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [lng, lat]},
                                       'properties': dict(properties)})
    systems = sorted({source['coordinate_system'] for source in dataset.sources})
    result.update(configured=True, status='ready', sources=[dict(source) for source in dataset.sources],
                  source_coordinate_system=systems[0] if len(systems) == 1 else 'mixed',
                  skipped_count=dataset.skipped, truncated=result['matched_count'] > limit,
                  message=f'已加载本地 POI；忽略 {dataset.skipped} 个无效或非点要素' if dataset.skipped else '已加载本地 POI')
    return result
