"""Offline regression checks for server transfer/planning safety; no NAS or GPU."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import server_benchmark as benchmark
import server_relocate_test_up as relocate
import server_preflight as preflight


class PreflightTests(unittest.TestCase):
    def test_empty_read_only_poi_directory_is_allowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            targets = ('/data', '/opt/assets/imagery', '/mnt/imagery', '/opt/models', '/opt/assets/boundaries', '/opt/assets/poi')
            mounts = []
            for index, target in enumerate(targets):
                source = root / str(index)
                source.mkdir()
                mounts.append(dict(target=target, source=str(source), read_only=target != '/data'))
            (root / '1/server-relocation.json').write_text(json.dumps({'container_root': '/mnt/imagery'}))
            (root / '3/enforcement_2.onnx').write_bytes(b'test-model')
            (root / '4/hebei-administrative-province.geojson').write_text('{}')
            env = root / 'server.env'
            env.write_text('')
            config = dict(services={'py-algo': {'volumes': mounts}, 'go-api': {},
                                    'nginx': {'ports': [{'host_ip': '127.0.0.1'}]}})

            def fake_run(command, **options):
                return dict(ok=True, stdout=json.dumps(config) if 'config' in command else 'available', stderr='')

            with patch('sys.argv', ['tool', '--env-file', str(env), '--output', str(root / 'report.json')]), patch.object(preflight.platform, 'system', return_value='Linux'), patch.object(preflight.shutil, 'which', return_value='/tool'), patch.object(preflight.shutil, 'disk_usage', return_value=SimpleNamespace(free=100*1024**3)), patch.object(preflight, 'run', side_effect=fake_run), redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as result:
                preflight.main()
            self.assertEqual(result.exception.code, 0)
            report = json.loads((root / 'report.json').read_text())
            self.assertEqual(report['status'], 'passed')
            self.assertTrue(any(check['name'] == 'poi_directory_ready' and check['detail']['geojson_files'] == 0 for check in report['checks']))
            self.assertTrue(any('POI directory is empty' in message for message in report['warnings']))


class RelocationTests(unittest.TestCase):
    def test_unc_to_linux_and_traversal_rejected(self):
        old = PureWindowsPath(r'\\nas.example.invalid\imagery\hebei\test_up')
        self.assertEqual(relocate.translate(str(old / '8月灵寿/t1_patches/a.tif'), old, PurePosixPath('/mnt/imagery')),
                         '/mnt/imagery/8月灵寿/t1_patches/a.tif')
        for unsafe in (r'\\other\share\a.tif', str(old / '../elsewhere/a.tif'), 'relative.tif'):
            with self.assertRaises(ValueError):
                relocate.translate(unsafe, old, PurePosixPath('/mnt/imagery'))

    def test_current_vrts_only_and_original_bytes_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output = root / 'source', root / 'export'
            (source / 'vrt').mkdir(parents=True)
            vrt = b'<VRTDataset><VRTRasterBand><SimpleSource><SourceFilename relativeToVRT="0">\\\\nas\\share\\test_up\\group\\a.tif</SourceFilename></SimpleSource></VRTRasterBand></VRTDataset>'
            (source / 'vrt/current.vrt').write_bytes(vrt)
            (source / 'vrt/obsolete.vrt').write_text('must not be copied')
            (source / 'catalog.json').write_text(json.dumps({'images': [{'id': 'a', 'file': 'vrt/current.vrt', 'source_file': r'\\nas\share\test_up\group\a.tif'}]}))
            before = {p: p.read_bytes() for p in source.rglob('*') if p.is_file()}
            with patch('sys.argv', ['tool', '--input', str(source), '--output', str(output), '--windows-root', r'\\nas\share\test_up']), redirect_stdout(io.StringIO()):
                relocate.main()
            self.assertEqual(before, {p: p.read_bytes() for p in source.rglob('*') if p.is_file()})
            self.assertFalse((output / 'vrt/obsolete.vrt').exists())
            self.assertEqual(ET.parse(output / 'vrt/current.vrt').find('.//SourceFilename').text, '/mnt/imagery/group/a.tif')
            self.assertEqual(json.loads((output / 'catalog.json').read_text())['images'][0]['source_file'], '/mnt/imagery/group/a.tif')

    def test_catalog_escape_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for name in ('../bad.vrt', '/absolute.vrt', r'E:\bad.vrt', 'current.tif'):
                with self.assertRaises(ValueError):
                    relocate.contained_file(root, name)


class BenchmarkTests(unittest.TestCase):
    def plan(self, root, bounds=None):
        output = root / 'plan.json'
        args = SimpleNamespace(bounds=bounds or [113.9, 38.7, 114.05, 38.85], before='2024-11-01',
                               after='2025-08-01', resolution=.8, max_pixels=4_000_000,
                               max_tiles=1024, region_id='130100', output=output)
        with redirect_stdout(io.StringIO()):
            benchmark.make_plan(args)
        return output, json.loads(output.read_text())

    def run_args(self, root, plan):
        return SimpleNamespace(plan=plan, stage='smoke', output=root / 'run', execute=False,
                               prior_report=None, catalog=root / 'catalog.json', base_url='http://localhost:8088',
                               compose_file=Path('deploy/docker-compose.server.yml'), env_file=Path('deploy/server.env'),
                               storage_dir=root, min_free_gib=10, interval=1, max_gpu_memory_percent=90,
                               max_container_memory_percent=85, crop_timeout=30, task_timeout=30)

    def test_tiles_cover_bounds_and_stay_under_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, plan = self.plan(Path(temporary))
            self.assertGreater(plan['tile_count'], 1)
            self.assertTrue(all(0 < tile['estimated_pixels'] <= plan['max_pixels'] for tile in plan['tiles']))
            tiles = plan['tiles']
            self.assertAlmostEqual(min(t['bounds'][0] for t in tiles), plan['bounds'][0])
            self.assertAlmostEqual(max(t['bounds'][2] for t in tiles), plan['bounds'][2])
            covered = sum((t['bounds'][2]-t['bounds'][0])*(t['bounds'][3]-t['bounds'][1]) for t in tiles)
            self.assertAlmostEqual(covered, (114.05-113.9)*(38.85-38.7))

    def test_invalid_bounds_are_rejected(self):
        for bounds in ([114, 39, 113, 40], [113, -86, 114, -85], [113, 39, float('nan'), 40]):
            with tempfile.TemporaryDirectory() as temporary, self.assertRaises(ValueError):
                self.plan(Path(temporary), bounds)

    def test_dry_run_performs_no_api_or_metric_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan, _ = self.plan(root)
            with patch.object(benchmark, 'api', side_effect=AssertionError('network forbidden')), patch.object(benchmark, 'command', side_effect=AssertionError('commands forbidden')), redirect_stdout(io.StringIO()):
                benchmark.execute(self.run_args(root, plan))
            report = json.loads((root / 'run/summary.json').read_text())
            self.assertEqual(report['submitted_tasks'], 0)
            self.assertEqual(report['status'], 'dry-run')

    def simulated_run(self, root, oversized=False):
        plan, _ = self.plan(root)
        args = self.run_args(root, plan)
        args.execute = True
        raw_rows = [{'id': 'scene-' + day, 'date': day, 'region_id': '130100', 'file': 'vrt/' + day + '.vrt'} for day in ('2024-11-01', '2025-08-01')]
        # Additional same-source rows outside the tile must not inflate every POST.
        raw_rows += [{**row, 'id': row['id'] + '-away', 'file': row['file'] + '-away'} for row in raw_rows[:2]]
        args.catalog.write_text(json.dumps({'images': raw_rows}))
        called = []

        class FakeMetrics:
            def __init__(self, *_):
                self.breached = []
                self.stop = SimpleNamespace(set=lambda: None)
                self.thread = SimpleNamespace(start=lambda: (args.output / 'metrics.jsonl').write_text('{}\n'),
                                              join=lambda **_: None, is_alive=lambda: False)

        def response(_, path, payload=None, **options):
            called.append(path)
            if path == '/api/settings':
                return dict(effective=True, revision=1, settings=dict(default_model='builtin-hebei', runtime=dict(workers=1, batch_size=1), sources=[dict(id='test-up', enabled=True, path='/opt/assets/imagery')]))
            if path == '/py/local/regions/130100/scenes':
                rows = [{**row, 'id': 'test-up:' + row['id'], 'available': True, 'source_id': 'test-up',
                         'source_root': '/opt/assets/imagery', 'bounds': [114.5, 39, 114.6, 39.1] if row['id'].endswith('-away') else [113.9, 38.7, 114.05, 38.85]} for row in raw_rows]
                rows.append(dict(id='other:unrelated', available=True, region_id='130100', date='2024-11-01'))
                return {'scenes': rows}
            if path == '/api/images/local':
                self.assertEqual(payload['scene_ids'], ['test-up:scene-2024-11-01', 'test-up:scene-2025-08-01'])
                return dict(batch_id='batch', items=[dict(id=day, capture_date=day+'T00:00:00Z', width=10000 if oversized else 512, height=512) for day in ('2024-11-01', '2025-08-01')])
            if path == '/api/tasks':
                return dict(id='task', status='running')
            if path.endswith('/result'):
                return dict(task_id='task', stats_json='{}')
            return dict(id='task', status='succeeded')

        with patch.object(benchmark, 'Metrics', FakeMetrics), patch.object(benchmark, 'api', side_effect=response), patch.object(benchmark.shutil, 'which', return_value='/tool'), patch.object(benchmark.time, 'sleep'), redirect_stdout(io.StringIO()):
            if oversized:
                with self.assertRaises(SystemExit):
                    benchmark.execute(args)
            else:
                benchmark.execute(args)
        return json.loads((args.output / 'summary.json').read_text()), called

    def test_simulated_api_workflow_preserves_batch_and_collects_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            report, paths = self.simulated_run(Path(temporary))
            self.assertEqual(report['status'], 'passed')
            self.assertEqual(report['submitted_tasks'], 1)
            self.assertIn('/api/tasks/task/result', paths)

    def test_active_scene_identity_and_unavailable_filter(self):
        plan = dict(region_id='130100', before='2024-11-01', after='2025-08-01')
        catalog = [dict(id=day, date=day, file=day+'.vrt', region_id='130100') for day in (plan['before'], plan['after'])]
        settings = {'settings': {'sources': [dict(id='test-up', enabled=True, path='/opt/assets/imagery')]}}
        active = [{**row, 'id': 'test-up:'+row['id'], 'source_id': 'test-up', 'source_root': '/opt/assets/imagery', 'available': True,
                   'bounds': [113.9, 38.7, 114.05, 38.85]} for row in catalog]
        source, rows = benchmark.matching_active_scenes(catalog, active, settings, plan)
        self.assertEqual(source, 'test-up')
        self.assertEqual([r['id'] for r in rows], ['test-up:2024-11-01', 'test-up:2025-08-01'])
        with self.assertRaisesRegex(ValueError, 'file/date/region'):
            benchmark.matching_active_scenes(catalog, [{**active[0], 'file': 'wrong.vrt'}, active[1]], settings, plan)
        with self.assertRaisesRegex(ValueError, 'both selected dates'):
            benchmark.matching_active_scenes(catalog, [active[0], {**active[1], 'available': False}], settings, plan)
        with self.assertRaisesRegex(ValueError, 'source root|configured source'):
            benchmark.matching_active_scenes(catalog, [{**active[0], 'source_root': '/unrelated'}, active[1]], settings, plan)

    def test_multiple_matching_sources_require_explicit_source_id(self):
        plan = dict(region_id='130100', before='2024-11-01', after='2025-08-01')
        catalog = [dict(id=day, date=day, file=day+'.vrt', region_id='130100') for day in (plan['before'], plan['after'])]
        settings = {'settings': {'sources': [dict(id=source, enabled=True, path='/catalog/'+source) for source in ('one', 'two')]}}
        active = [{**row, 'id': source+':'+row['id'], 'source_id': source, 'source_root': '/catalog/'+source, 'available': True,
                   'bounds': [113.9, 38.7, 114.05, 38.85]} for source in ('one', 'two') for row in catalog]
        with self.assertRaisesRegex(ValueError, 'source-id'):
            benchmark.matching_active_scenes(catalog, active, settings, plan)
        chosen, rows = benchmark.matching_active_scenes(catalog, active, settings, plan, 'two')
        self.assertEqual(chosen, 'two')
        self.assertTrue(all(row['id'].startswith('two:') for row in rows))

    def test_http_error_reports_status_without_body_or_headers(self):
        import urllib.error
        failure = urllib.error.HTTPError('http://localhost/api/settings', 403, 'forbidden',
                                         {'Authorization': 'Bearer secret'}, io.BytesIO(b'private-token-in-response'))
        with patch.object(benchmark.urllib.request, 'urlopen', side_effect=failure), self.assertRaises(RuntimeError) as result:
            benchmark.api('http://localhost', '/api/settings')
        self.assertEqual(str(result.exception), '/api/settings: HTTP 403')

    def test_oversized_actual_crop_never_submits_detection(self):
        with tempfile.TemporaryDirectory() as temporary:
            report, paths = self.simulated_run(Path(temporary), oversized=True)
            self.assertEqual(report['status'], 'failed')
            self.assertNotIn('/api/tasks', paths)
            self.assertEqual(report['submitted_tasks'], 0)

    def test_pilot_requires_passed_smoke_for_same_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan, _ = self.plan(root)
            args = self.run_args(root, plan)
            args.execute, args.stage = True, 'pilot'
            with patch.object(benchmark, 'api', side_effect=AssertionError('API forbidden')), redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                benchmark.execute(args)
            report = json.loads((args.output / 'summary.json').read_text())
            self.assertEqual(report['submitted_tasks'], 0)
            self.assertIn('requires a passed smoke', report['failures'][0])


if __name__ == '__main__':
    unittest.main()
