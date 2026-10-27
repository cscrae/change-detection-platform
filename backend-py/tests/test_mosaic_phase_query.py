"""Short phase URLs keep large mosaics below proxy request-line limits."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from fastapi import HTTPException

from app.fetchers import local
from app.routers import wayback
from app.utils.tile_queue import TileRenderer


def rows_for_phase(count=514):
    rows = [dict(id=f'scene-{index:04d}-0123456789abcdef0123456789abcdef',
                 file=f'2025-08/{index}.tif', region_id='130100', date='2025-08-01',
                 bounds=[113.9, 38.7, 113.91, 38.71], version=f'file-{index}') for index in range(count)]
    return [*rows, dict(rows[0], id='unavailable', available=False),
            dict(rows[0], id='other-month', date='2025-09-01'),
            dict(rows[0], id='other-region', region_id='130600')]


class PhaseQueryFixture:
    def fixture(self):
        self.rows = rows_for_phase()
        self.catalog = patch.object(local, 'catalog', return_value=self.rows)
        self.catalog.start()
        self.addCleanup(self.catalog.stop)
        def region_feature(region_id):
            if region_id != '130100':
                raise KeyError(region_id)
            return {'properties': {'name': '临时测试区域'}}
        region = patch('app.fetchers.regions.region_feature', side_effect=region_feature)
        region.start()
        self.addCleanup(region.stop)
        local.render_mosaic.cache_clear()
        self.addCleanup(local.render_mosaic.cache_clear)

    def version(self, month='2025-08'):
        return local.mosaic_version([row for row in self.rows if row['region_id'] == '130100' and row['date'][:7] == month])


class MosaicPhaseQueryTests(PhaseQueryFixture, unittest.TestCase):
    def setUp(self):
        self.fixture()

    def test_short_query_selects_all_514_available_members_only(self):
        ids = local.resolve_mosaic_scene_ids('130100', month='2025-08', version=self.version())
        self.assertEqual(len(ids), 514)
        self.assertEqual(set(ids), {row['id'] for row in self.rows[:514]})
        self.assertNotIn('unavailable', ids)
        self.assertNotIn('other-month', ids)
        self.assertNotIn('other-region', ids)
        job = wayback._prepare_mosaic_job('130100', 0, 0, 0, version=self.version(), month='2025-08')
        self.assertEqual({row['id'] for row in job.rows}, set(ids))

    def test_sync_entry_uses_short_query_and_preserves_old_explicit_form(self):
        version = self.version()
        with patch.object(local, '_render_tile_rows', return_value=b'png') as rendered:
            short = wayback.mosaic_tile('130100', 0, 0, 0, v=version, month='2025-08')
            self.assertEqual(short.body, b'png')
            self.assertEqual(short.headers['cache-control'], 'private, max-age=86400')
            self.assertEqual(len(rendered.call_args.args[0]), 514)
            explicit = ','.join(row['id'] for row in self.rows[:514])
            old = wayback.mosaic_tile('130100', 0, 0, 0, explicit, version)
            self.assertEqual(old.body, short.body)
            self.assertEqual(rendered.call_count, 1, 'both forms should share the unchanged tile cache')
            legacy = wayback.mosaic_tile('130100', 0, 0, 0, self.rows[0]['id'])
            self.assertEqual(legacy.headers['cache-control'], 'no-cache')

    def test_same_month_version_changes_and_wrong_members_are_still_rejected(self):
        old = self.version()
        self.rows[0]['version'] = 'updated-file'
        with self.assertRaises(local.StaleMosaicVersion):
            wayback._prepare_mosaic_job('130100', 0, 0, 0, version=old, month='2025-08')
        with self.assertRaises(HTTPException) as error:
            wayback.mosaic_tile('130100', 0, 0, 0, v=old, month='2025-08')
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(error.exception.headers['Cache-Control'], 'no-store')
        with self.assertRaises(local.StaleMosaicVersion):
            # A versioned explicit subset must not bypass complete-phase membership.
            local._mosaic_rows('130100', [self.rows[0]['id']], 0, 0, 0, self.version())
        with self.assertRaises(ValueError):
            # Legacy unversioned cross-month requests keep the old safety check.
            local._mosaic_rows('130100', [self.rows[0]['id'], 'other-month'], 0, 0, 0)

    def test_wrong_month_missing_version_mixed_and_empty_scene_queries(self):
        for args in [dict(month='2025-8', version=self.version()),
                     dict(month='2025-13', version=self.version()),
                     dict(month='', version=self.version()),
                     dict(month='2025-07', version=self.version()),
                     dict(month='2025-08'),
                     dict(month='2025-08', version=''),
                     dict(month='2025-08', scene_ids='', version=self.version()),
                     dict(month='2025-08', scene_ids=self.rows[0]['id'], version=self.version()),
                     dict()]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                local.resolve_mosaic_scene_ids('130100', **args)
        self.assertEqual(local.resolve_mosaic_scene_ids('130100', scene_ids=''), [])
        with self.assertRaises(ValueError):
            wayback._prepare_mosaic_job('130100', 0, 0, 0, scene_ids='')
        with self.assertRaises(KeyError):
            local.resolve_mosaic_scene_ids('unknown', month='2025-08', version=self.version())

    def test_all_unavailable_month_and_invalid_tile_coordinates_stay_errors(self):
        self.rows.append(dict(self.rows[0], id='empty-month', date='2025-10-01', available=False))
        with self.assertRaisesRegex(ValueError, '没有可用'):
            local.resolve_mosaic_scene_ids('130100', month='2025-10', version=self.version('2025-10'))
        with self.assertRaisesRegex(ValueError, '瓦片坐标无效'):
            wayback._prepare_mosaic_job('130100', 21, 0, 0, version=self.version(), month='2025-08')


class MosaicPhaseHTTPTests(PhaseQueryFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture()
        from app.main import app
        self.app = app
        self.jobs = []
        def worker(job):
            self.jobs.append(job)
            return b'png', 1
        self.renderer = TileRenderer(concurrency=1, max_pending=2, executor=ThreadPoolExecutor(1), worker=worker)
        self.addAsyncCleanup(self.renderer.close)
        state = patch.object(app.state, 'tile_renderer', self.renderer, create=True)
        state.start()
        self.addCleanup(state.stop)

    async def request(self, parameters):
        query = urlencode(parameters).encode()
        sent, messages = False, []
        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {'type': 'http.request', 'body': b'', 'more_body': False}
            await asyncio.Event().wait()
        async def send(message):
            messages.append(message)
        path = '/py/local/regions/130100/mosaic/0/0/0.png'
        scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                 'method': 'GET', 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
                 'query_string': query, 'headers': [], 'server': ('127.0.0.1', 8090),
                 'client': ('127.0.0.1', 1234)}
        await self.app(scope, receive, send)
        start = next(item for item in messages if item['type'] == 'http.response.start')
        body = b''.join(item.get('body', b'') for item in messages if item['type'] == 'http.response.body')
        return start['status'], dict(start['headers']), body

    async def test_short_http_url_passes_all_members_through_original_worker_preparation(self):
        parameters = {'month': '2025-08', 'v': self.version()}
        self.assertLess(len(urlencode(parameters)), 100)
        status, headers, body = await self.request(parameters)
        self.assertEqual(status, 200)
        self.assertEqual(body, b'png')
        self.assertEqual(headers[b'cache-control'], b'private, max-age=86400')
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual({row['id'] for row in self.jobs[0].rows}, {row['id'] for row in self.rows[:514]})
        ids = ','.join(row['id'] for row in self.rows[:514])
        self.assertGreater(len(ids), 16000, 'fixture reproduces a proxy request-line overflow with old URLs')
        self.assertEqual((await self.request({'scene_ids': ids, 'v': self.version()}))[0], 200)
        self.assertEqual(len(self.jobs), 1, 'legacy and short requests share rendering/cache')
        self.rows[0]['version'] = 'changed-after-cache-hit'
        status, headers, body = await self.request(parameters)
        self.assertEqual(status, 409, 'current phase must be revalidated even if the tile is already cached')
        self.assertEqual(headers[b'cache-control'], b'no-store')
        self.assertEqual(len(self.jobs), 1)

    async def test_http_rejects_bad_or_ambiguous_phase_queries_before_rendering(self):
        for parameters in [{}, {'scene_ids': ''}, {'month': '2025-08'},
                           {'month': '2025-09', 'v': self.version()},
                           {'month': '2025-07', 'v': self.version()},
                           {'month': '2025-08', 'v': self.version(), 'scene_ids': ''},
                           {'month': '2025-13', 'v': self.version()}]:
            with self.subTest(parameters=parameters):
                status, _, body = await self.request(parameters)
                expected = 409 if parameters.get('month') == '2025-09' else 400
                self.assertEqual(status, expected, json.loads(body))
        self.assertEqual(self.jobs, [])


if __name__ == '__main__':
    unittest.main()
