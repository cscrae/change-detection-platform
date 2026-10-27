"""Selected-phase mosaics and read-only external-directory discovery."""
import io
import math
import json
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from functools import lru_cache
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image
import rasterio
from rasterio.transform import from_bounds
from shapely.geometry import box

from app.fetchers import local
from app.fetchers.local import catalog as read_catalog
from app.fetchers.imagery_index import capture_month, clear_cache, raster_metadata, scan_catalog


class ImageryWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / '2025年'
        self.root.mkdir()
        self.bounds = [114.30, 38.10, 114.31, 38.11]
        self.rows = []
        for name, size, value, month in [('coarse', 40, 100, '01'), ('fine', 80, 200, '01'), ('next', 40, 150, '03')]:
            relative = f'{month}月/石家庄市/{name}2025{month}.tif'
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            pixels = np.full((3, size, size), value, dtype='uint8')
            if name == 'fine': pixels[:, :, :size//2] = 0
            with rasterio.open(path, 'w', driver='GTiff', width=size, height=size, count=3,
                               dtype='uint8', crs='EPSG:4326', nodata=0,
                               transform=from_bounds(*self.bounds, size, size)) as dst:
                dst.write(pixels)
            self.rows.append(dict(id=name, file=relative, region_id='130100', region='石家庄市',
                                  date=f'2025-{month}-01', **raster_metadata(path)))
        self.patches = [patch.object(local, 'IMAGERY', self.root),
                        patch.object(local, 'catalog', return_value=self.rows),
                        patch.object(local, 'province', return_value=box(*self.bounds)),
                        patch('app.fetchers.regions.region_geometry', return_value=box(*self.bounds))]
        for item in self.patches: item.start()
        local.render_tile.cache_clear(); local.province_mercator.cache_clear(); clear_cache()

    def tearDown(self):
        for item in reversed(self.patches): item.stop()
        local.render_tile.cache_clear(); local.province_mercator.cache_clear(); clear_cache()
        self.temporary.cleanup()

    def crop(self, ids):
        return local.crop_images(self.bounds, '2025-01-01', '2025-12-31', 0,
                                 Path(self.temporary.name) / 'out', '130100', ids)

    def test_selected_sources_group_by_month_and_fill_fine_nodata(self):
        result = self.crop(['coarse', 'fine', 'fine', 'next'])
        self.assertEqual(result['count'], 2)
        self.assertEqual([row['date'] for row in result['images']], ['2025-01-01', '2025-03-01'])
        self.assertEqual([row['date_precision'] for row in result['images']], ['month', 'month'])
        with rasterio.open(result['images'][0]['path']) as src:
            rgb = src.read()
            self.assertEqual(rgb[0, src.height//2, src.width//4], 100)
            self.assertEqual(rgb[0, src.height//2, 3*src.width//4], 200)
            self.assertTrue((src.dataset_mask() == 255).all())
            self.assertEqual(src.tags()['date_precision'], 'month')
        self.assertEqual(len(result['images'][0]['source_files']), 2)
        self.assertEqual(self.crop(['next'])['count'], 1)

    def test_selected_annual_source_covers_full_year_and_preserves_precision(self):
        self.rows[0]['date_precision'] = 'year'
        for start, end in [('2025-08-01', '2025-08-31'), ('2025-12-31', '2025-12-31')]:
            with self.subTest(start=start, end=end):
                result = local.crop_images(self.bounds, start, end, 0,
                                          Path(self.temporary.name) / 'out', '130100', ['coarse', 'next'])
                self.assertEqual(result['count'], 1)
                image = result['images'][0]
                self.assertEqual(image['date'], '2025-01-01')
                self.assertEqual(image['scene_ids'], ['coarse'])
                self.assertEqual(image['date_precision'], 'year')
                self.assertEqual(result['dates_requested'], ['2025-01-01'])
                with rasterio.open(image['path']) as src:
                    self.assertEqual(src.tags()['date_precision'], 'year')
                    self.assertEqual(src.tags()['capture_month'], '2025-01')
                    self.assertTrue((src.read() == 100).all())
        for outside in ['2024-12-31', '2026-01-01']:
            with self.subTest(outside=outside):
                result = local.crop_images(self.bounds, outside, outside, 0,
                                          Path(self.temporary.name) / 'out', '130100', ['coarse'])
                self.assertEqual(result['count'], 0)
                self.assertEqual(result['dates_requested'], [])

    def test_mixed_annual_and_monthly_sources_keep_unknown_month(self):
        self.rows[0]['date_precision'] = 'year'
        image = self.crop(['coarse', 'fine'])['images'][0]
        self.assertEqual(image['date'], '2025-01-01')
        self.assertEqual(image['date_precision'], 'year')
        self.assertEqual(set(image['scene_ids']), {'coarse', 'fine'})
        with rasterio.open(image['path']) as src:
            self.assertEqual(src.tags()['date_precision'], 'year')
            values = src.read(1)
            self.assertTrue((values == 100).any())
            self.assertTrue((values == 200).any())

    def test_tile_has_same_priority_and_transparent_outside_region(self):
        z = 14; lng, lat = 114.305, 38.105
        x = int((lng + 180) / 360 * 2**z)
        y = int((1-math.asinh(math.tan(math.radians(lat)))/math.pi)/2*2**z)
        png = local.render_mosaic('130100', ['fine', 'coarse'], z, x, y)
        rgba = np.asarray(Image.open(io.BytesIO(png)))
        valid = rgba[:, :, 3] > 0
        values = rgba[:, :, 0][valid]
        self.assertTrue((values == 100).any())
        self.assertTrue((values == 200).any())
        self.assertTrue((~valid).any())
        self.assertEqual(png, local.render_mosaic('130100', ['coarse', 'fine'], z, x, y))

    def test_empty_unknown_cross_region_and_cross_month_rejected(self):
        for ids in ([], ['missing']):
            with self.assertRaises(ValueError): self.crop(ids)
        with self.assertRaises(ValueError): local.selected_scenes('130600', ['fine'])
        with self.assertRaises(ValueError): local.render_mosaic('130100', ['fine', 'next'], 8, 1, 1)
        with self.assertRaises(ValueError): local.render_mosaic('130100', ['fine'], 30, 1, 1)

    def test_equal_resolution_priority_does_not_depend_on_drag_order(self):
        self.rows[-1]['date'] = '2025-01-01'
        result = self.crop(['next', 'coarse'])
        self.assertEqual(result['count'], 1)
        with rasterio.open(result['images'][0]['path']) as src:
            self.assertTrue((src.read() == 100).all())

    def test_explicit_null_selection_is_not_legacy_all_images(self):
        from fastapi import HTTPException
        from app.routers.wayback import LocalFetchRequest, fetch_local
        request = LocalFetchRequest(bounds=self.bounds, scene_ids=None, output_dir=str(self.root / 'out'))
        with self.assertRaises(HTTPException) as error: fetch_local(request)
        self.assertEqual(error.exception.status_code, 400)

    def test_failed_phase_cleanup(self):
        self.rows[-1]['bounds'] = [115, 39, 116, 40]
        with self.assertRaisesRegex(ValueError, '2025-03'): self.crop(['fine', 'next'])
        self.assertEqual(list((Path(self.temporary.name) / 'out').glob('*.tif')), [])

    def test_selection_pixels_match_crop_without_reading_pixels(self):
        from app.routers.wayback import SelectionPixelsRequest, get_selection_pixels
        for ids in (['fine'], ['fine', 'coarse'], ['next']):
            with patch.object(local, 'read_mosaic', side_effect=AssertionError('preview must not read pixels')):
                preview = get_selection_pixels(SelectionPixelsRequest(
                    bounds=self.bounds, region_id='130100', scene_ids=ids))
            image = self.crop(ids)['images'][0]
            self.assertEqual(preview, dict(width=image['width'], height=image['height'],
                                           pixels=image['width'] * image['height']))

    def test_selection_pixels_above_previous_limit(self):
        with patch.object(local, '_crop_resolution', return_value=(0.000001, 0.000001)):
            preview = local.selection_pixels(self.bounds, '130100', ['fine'])
        self.assertGreater(preview['pixels'], 16_000_000)

    def test_folder_dates_override_filename_and_include_subfolders(self):
        examples = [('11月/石家庄市/影像202510补.tif', '2025-11-01'),
                    ('9月/石家庄市/石家庄市8月补.tif', '2025-09-01'),
                    ('03月/石家庄市/补充/影像202511.tif', '2025-03-01'),
                    ('3月/石家庄市/邢台s2米补.tif', '2025-03-01'),
                    ('3月/石家庄市/影像2025031.tif', '2025-03-01')]
        for relative, expected in examples:
            self.assertEqual(capture_month(self.root / relative, self.root)[0], expected)
        with self.assertRaisesRegex(ValueError, '目录'):
            capture_month(self.root / '石家庄市/影像202503.tif', self.root)

    def test_black_pixels_without_nodata_are_valid(self):
        with rasterio.open(self.root / self.rows[1]['file'], 'r+') as src:
            src.nodata = None
        result = self.crop(['fine', 'coarse'])
        with rasterio.open(result['images'][0]['path']) as src:
            self.assertEqual(src.read(1)[src.height//2, src.width//4], 0)
            self.assertTrue((src.dataset_mask() == 255).all())

    def test_phase_members_versions_bounds_and_unavailable(self):
        broken = dict(self.rows[0], id='broken', available=False)
        empty = dict(broken, id='empty', date='2025-05-01')
        phases = local.imagery_phases(self.rows + [broken, empty], '130100')
        self.assertEqual(len(phases), 3)
        self.assertEqual(phases[0]['id'], '130100:2025-01')
        self.assertEqual(len(phases[0]['scenes']), 3)
        self.assertEqual(phases[0]['bounds'], self.bounds)
        self.assertFalse(phases[-1]['available'])
        self.assertIsNone(phases[-1]['bounds'])
        self.assertEqual(local.mosaic_version(self.rows), local.mosaic_version(list(reversed(self.rows))))
        self.assertNotEqual(local.mosaic_version(self.rows), local.mosaic_version(self.rows[:-1]))
        changed = [dict(row, version='updated') for row in self.rows]
        self.assertNotEqual(local.mosaic_version(self.rows), local.mosaic_version(changed))

    def test_legacy_manifest_date_preserved_and_file_version_changes(self):
        manifest = self.root / 'catalog.json'
        record = {**self.rows[0], 'date': '2024-09-01'}
        manifest.write_text(json.dumps({'images': [record]}), encoding='utf-8')
        before = read_catalog()[0]
        self.assertEqual(before['date'], '2024-09-01')
        self.assertGreater(before['resolution_m'], 1, 'geographic degrees cannot be compared to metres')
        path = self.root / record['file']
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        self.assertEqual(before['version'], read_catalog()[0]['version'], 'snapshot stays stable until explicit refresh')
        after = read_catalog(refresh=True)[0]
        self.assertNotEqual(before['version'], after['version'])
        self.assertNotEqual(local.mosaic_version([before]), local.mosaic_version([after]))

    def test_versioned_tiles_cache_headers_and_reject_stale_members(self):
        from fastapi import HTTPException
        from app.routers.wayback import mosaic_tile
        version = local.mosaic_version(self.rows[:2])
        response = mosaic_tile('130100', 0, 0, 0, 'fine,coarse', version)
        self.assertEqual(response.headers['cache-control'], 'private, max-age=86400')
        self.assertEqual(mosaic_tile('130100', 0, 0, 0, 'fine,coarse').headers['cache-control'], 'no-cache')
        for ids, revision in [('fine,coarse', 'old'), ('fine', version)]:
            with self.assertRaises(HTTPException) as context:
                mosaic_tile('130100', 0, 0, 0, ids, revision)
            self.assertEqual(context.exception.status_code, 409)
        self.rows[0]['version'] = 'changed'
        with self.assertRaises(local.StaleMosaicVersion):
            local.render_mosaic('130100', ['fine', 'coarse'], 0, 0, 0, version)

    def test_single_flight_shares_work_and_retries_failure(self):
        started, release = Event(), Event()
        calls = []

        @local.single_flight
        @lru_cache(maxsize=512)
        def render(key):
            calls.append(key)
            started.set()
            self.assertTrue(release.wait(5))
            if key == 'error':
                raise ValueError('retryable')
            return b'png'

        with ThreadPoolExecutor(max_workers=8) as pool:
            first = pool.submit(render, 'tile')
            self.assertTrue(started.wait(5))
            others = [pool.submit(render, 'tile') for _ in range(7)]
            release.set()
            self.assertEqual([first.result()] + [item.result() for item in others], [b'png'] * 8)
        self.assertEqual(calls, ['tile'])
        for _ in range(2):
            with self.assertRaises(ValueError): render('error')
        self.assertEqual(calls.count('error'), 2)

    def test_fully_covered_tile_skips_coarser_sources(self):
        from rasterio.transform import from_bounds
        # Coarse is the only readable source; a later missing source must never be opened.
        missing = dict(self.rows[0], id='missing', file='missing.tif', resolution_m=99999)
        rgb, valid = local.read_mosaic([self.rows[0], missing], 'EPSG:4326',
                                     from_bounds(*self.bounds, 40, 40), 40, 40)
        self.assertTrue(valid.all())
        self.assertTrue((rgb == 100).all())

    def test_old_index_rule_is_rebuilt_and_nested_members_join_month(self):
        path = self.root / '01月/石家庄市/补充/fine202511.tif'
        path.parent.mkdir()
        path.write_bytes((self.root / self.rows[1]['file']).read_bytes())
        cache = Path(self.temporary.name) / 'index'
        rows = scan_catalog(self.root, cache, {'石家庄市': '130100'})
        self.assertEqual(next(row for row in rows if row['filename'] == path.name)['date'], '2025-01-01')
        index = next(cache.glob('*.json'))
        doc = json.loads(index.read_text(encoding='utf-8'))
        doc.pop('index_version')
        for row in doc['images']:
            row['date'] = '1999-12-01'
        index.write_text(json.dumps(doc), encoding='utf-8')
        clear_cache()
        refreshed = scan_catalog(self.root, cache, {'石家庄市': '130100'})
        self.assertTrue(all(row['date'].startswith('2025') for row in refreshed))

    def test_index_keeps_broken_files_updates_and_never_writes_source(self):
        broken = self.root / '01月/石家庄市/broken202501.tif'
        broken.write_bytes(b'not a raster')
        before = {str(path.relative_to(self.root)): path.stat().st_mtime_ns for path in self.root.rglob('*') if path.is_file()}
        cache = Path(self.temporary.name) / 'cache'
        rows = scan_catalog(self.root, cache, {'石家庄市': '130100'})
        self.assertEqual(len(rows), 4)
        self.assertFalse(next(row for row in rows if row['filename'] == broken.name)['available'])
        self.assertEqual(before, {str(path.relative_to(self.root)): path.stat().st_mtime_ns for path in self.root.rglob('*') if path.is_file()})
        self.assertTrue(list(cache.glob('*.json')))
        ids = [row['id'] for row in rows]
        clear_cache()
        self.assertEqual(ids, [row['id'] for row in scan_catalog(self.root, cache, {'石家庄市': '130100'})])
        with self.assertRaises(OSError): scan_catalog(self.root / 'missing', cache, {})


if __name__ == '__main__': unittest.main()
