"""Synthetic temporary POIs verify CRS safety, view filtering, and live updates."""
import asyncio
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from app.fetchers import poi


def feature(x=114, y=38, properties=None, **extra):
    return {'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [x, y]},
            'properties': properties or {'name': '临时测试点'}, **extra}


class POITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / 'poi'
        self.directory.mkdir()
        self.environment = patch.dict(os.environ, {'POI_GEOJSON_PATH': ''})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.asset = patch.object(poi.settings, 'ASSET_ROOT', self.root)
        self.asset.start()
        self.addCleanup(self.asset.stop)
        poi.clear_cache()
        self.addCleanup(poi.clear_cache)

    def write(self, features=None, path=None, **metadata):
        path = path or self.directory / 'fixture.geojson'
        path.write_text(json.dumps({'type': 'FeatureCollection',
                                    'features': [feature()] if features is None else features,
                                    **metadata}, ensure_ascii=False), encoding='utf-8')
        return path

    def query(self, bounds=None, limit=500):
        return poi.query_poi(bounds or [113, 37, 115, 39], limit)

    def test_absent_data_and_explicit_missing_path_do_not_fabricate_features(self):
        result = self.query()
        self.assertEqual(result['features'], [])
        self.assertFalse(result['configured'])
        self.assertEqual(result['status'], 'not_configured')
        with patch.dict(os.environ, {'POI_GEOJSON_PATH': str(self.root / 'missing.geojson')}):
            result = self.query()
        self.assertTrue(result['configured'])
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['features'], [])
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_bbox_edges_limit_and_safe_property_allowlist(self):
        self.write([feature(113, 37, {'name': '<b>学校</b><script>bad()</script>', 'category': '教育',
                                    'address': '甲路\x00乙号', 'secret': 'do-not-expose', 'phone': 'private'}),
                    feature(115, 39), feature(114, 38), feature(114, 40)])
        result = self.query(limit=2)
        self.assertEqual(result['matched_count'], 3)
        self.assertEqual(len(result['features']), 2)
        self.assertTrue(result['truncated'])
        properties = result['features'][0]['properties']
        self.assertEqual(properties['name'], '学校')
        self.assertEqual(properties['address'], '甲路 乙号')
        self.assertEqual(set(properties), {'name', 'category', 'address', 'source_id'})
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertEqual(result['coordinate_system'], 'EPSG:4326')
        self.assertEqual(result['source_coordinate_system'], 'EPSG:4326')
        self.assertEqual(self.query([100, 20, 101, 21])['matched_count'], 0)

    def test_web_mercator_conversion_requires_declared_crs_and_valid_range(self):
        x = math.radians(114) * 6378137
        y = math.asinh(math.tan(math.radians(38))) * 6378137
        self.write([feature(x, y)], coordinate_system='EPSG:3857')
        result = self.query()
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['source_coordinate_system'], 'EPSG:3857')
        coordinates = result['features'][0]['geometry']['coordinates']
        self.assertAlmostEqual(coordinates[0], 114)
        self.assertAlmostEqual(coordinates[1], 38)
        self.write([feature(x, y)])
        result = self.query()
        self.assertEqual(result['features'], [])
        self.assertEqual(result['skipped_count'], 1, 'undeclared metre coordinates must not be guessed')
        self.write([feature(poi.WEB_MERCATOR_LIMIT + 1, y)], coordinate_system='EPSG:3857')
        self.assertEqual(self.query()['skipped_count'], 1)

    def test_unsupported_conflicting_and_malformed_crs_fail_closed(self):
        cases = [
            {'coordinate_system': 'GCJ-02'}, {'coordinate_system': 'BD09'},
            {'coordinate_system': 'EPSG:4526'}, {'crs': None},
            {'crs': {'type': 'name', 'properties': ['EPSG:4326']}},
            {'coordinate_system': 'EPSG:4326', 'crs': {'type': 'name', 'properties': {'name': 'EPSG:3857'}}},
        ]
        for metadata in cases:
            with self.subTest(metadata=metadata):
                self.write(**metadata)
                result = self.query()
                self.assertEqual(result['status'], 'invalid_data')
                self.assertTrue(result['configured'])
                self.assertEqual(result['features'], [])
        self.write(crs={'type': 'name', 'properties': {'name': 'urn:ogc:def:crs:EPSG::4326'}})
        self.assertEqual(self.query()['matched_count'], 1)
        self.write([feature(coordinate_system='GCJ-02')])
        self.assertEqual(self.query()['status'], 'invalid_data')

    def test_invalid_points_are_counted_without_breaking_other_valid_points(self):
        invalid = [feature(True, 38), feature(float('nan'), 38), feature(181, 38),
                   feature(114, 91), feature('114', 38), feature(10 ** 400, 38),
                   {'type': 'Feature', 'geometry': {'type': 'LineString', 'coordinates': [[114, 38], [115, 39]]}},
                   {'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [114]}}, None]
        self.write([*invalid, feature()])
        result = self.query()
        self.assertEqual(result['matched_count'], 1)
        self.assertEqual(result['skipped_count'], len(invalid))

    def test_cache_reuses_parsing_refreshes_mtime_and_does_not_leak_mutations(self):
        path = self.write()
        with patch.object(poi.json, 'loads', wraps=json.loads) as parsed:
            first = self.query()
            first['features'][0]['properties']['name'] = 'client mutation'
            second = self.query()
            self.assertEqual(parsed.call_count, 1)
            self.assertEqual(second['features'][0]['properties']['name'], '临时测试点')
            before = path.stat().st_mtime_ns
            self.write([feature(properties={'name': 'changed'})])
            os.utime(path, ns=(before + 2_000_000_000, before + 2_000_000_000))
            self.assertEqual(self.query()['features'][0]['properties']['name'], 'changed')
            self.assertEqual(parsed.call_count, 2)

    def test_invalid_file_and_mtime_recovery_are_explicit(self):
        path = self.directory / 'fixture.geojson'
        path.write_bytes(b'not JSON')
        result = self.query()
        self.assertEqual(result['status'], 'invalid_data')
        self.assertNotIn(str(path), result['message'])
        self.write()
        self.assertEqual(self.query()['status'], 'ready')

    def test_explicit_file_overrides_default_directory_and_mixed_sources_are_reported(self):
        self.write()
        explicit = self.write([feature(114.5, 38.5)], self.root / 'override.json')
        with patch.dict(os.environ, {'POI_GEOJSON_PATH': str(explicit)}):
            self.assertEqual(self.query()['matched_count'], 1)
            self.assertEqual(self.query()['features'][0]['geometry']['coordinates'], [114.5, 38.5])
        x = math.radians(114) * 6378137
        y = math.asinh(math.tan(math.radians(38))) * 6378137
        self.write([feature(x, y)], self.directory / 'mercator.geojson', coordinate_system='EPSG:3857')
        result = self.query()
        self.assertEqual(result['matched_count'], 2)
        self.assertEqual(result['source_coordinate_system'], 'mixed')
        self.assertEqual(len(result['sources']), 2)

    def test_dataset_limits_return_explicit_status(self):
        self.write()
        with patch.object(poi, 'MAX_BYTES', 10):
            self.assertEqual(self.query()['status'], 'invalid_data')
        with patch.object(poi, 'MAX_FEATURES', 0):
            self.assertEqual(self.query()['status'], 'invalid_data')
        with patch.object(poi, 'MAX_FILES', 0):
            self.assertEqual(self.query()['status'], 'invalid_data')


class POIIngressTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, parameters):
        from app.main import app
        messages = []
        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        async def send(message):
            messages.append(message)
        scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                 'method': 'GET', 'scheme': 'http', 'path': '/py/local/poi',
                 'raw_path': b'/py/local/poi', 'query_string': urlencode(parameters).encode(),
                 'headers': [], 'server': ('127.0.0.1', 8090), 'client': ('127.0.0.1', 1234)}
        await app(scope, receive, send)
        status = next(message['status'] for message in messages if message['type'] == 'http.response.start')
        body = b''.join(message.get('body', b'') for message in messages if message['type'] == 'http.response.body')
        return status, json.loads(body)

    async def test_public_route_validates_user_query_without_private_control_token(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(poi.settings, 'ASSET_ROOT', Path(temporary)), \
                patch.dict(os.environ, {'POI_GEOJSON_PATH': ''}):
            status, body = await self.request({'bbox': '113,37,115,39', 'limit': '500'})
            self.assertEqual(status, 200)
            self.assertEqual(body['status'], 'not_configured')
            for bbox in ('1,2,3', 'x,2,3,4', '113,37,113,39', '115,37,113,39',
                         '113,39,115,37', '-181,37,115,39', '113,-91,115,39',
                         'nan,37,115,39', 'inf,37,115,39', '1,2,3,4,5'):
                with self.subTest(bbox=bbox):
                    self.assertEqual((await self.request({'bbox': bbox}))[0], 400)
            for limit in ('0', '-1', '2001', '1.5', 'NaN'):
                with self.subTest(limit=limit):
                    self.assertEqual((await self.request({'bbox': '113,37,115,39', 'limit': limit}))[0], 422)
            self.assertEqual((await self.request({}))[0], 422)


if __name__ == '__main__':
    unittest.main()
