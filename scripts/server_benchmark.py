"""Plan bounded AOIs offline; optionally run staged CUDA checks on a chosen server.

Default run is a dry run. Execution creates derived crops/results through the
existing platform API, sequentially, and never changes platform settings.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def now():
    return datetime.now(timezone.utc).isoformat()


def command(args, timeout=15):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return dict(ok=result.returncode == 0, returncode=result.returncode, stdout=result.stdout.strip(), stderr=result.stderr[-2000:])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return dict(ok=False, error=str(exc))


def api(base, path, payload=None, timeout=30):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(base.rstrip('/') + path, data=data, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # Do not persist response bodies/headers: upstream diagnostics can contain
        # credentials. Operators can inspect the service's controlled logs.
        raise RuntimeError(f'{path}: HTTP {exc.code}') from None


def matching_active_scenes(catalog, active, settings, plan, source_id=None):
    """Validate raw-catalog identity, then retain the active source-qualified IDs."""
    expected = {row['id']: row for row in catalog
                if row.get('region_id') == plan['region_id'] and row.get('date') in (plan['before'], plan['after'])}
    enabled = {row['id']: row for row in settings['settings'].get('sources', []) if row.get('enabled')}
    if source_id and source_id not in enabled:
        raise ValueError('Requested source-id is not enabled in current settings')
    groups = {}
    for row in active:
        if not row.get('available', True) or row.get('region_id') != plan['region_id'] or row.get('date') not in (plan['before'], plan['after']):
            continue
        scoped_id = row.get('id', '')
        prefix, separator, raw_id = scoped_id.partition(':')
        if not separator or not prefix or not raw_id:
            raise ValueError('Active scene lacks a source-qualified ID; refresh platform sources')
        if source_id and prefix != source_id:
            continue
        if raw_id not in expected:
            continue  # An unrelated active source is never submitted.
        if prefix not in enabled or row.get('source_id') != prefix:
            raise ValueError('Active scene source identity does not match enabled settings')
        configured_root = str(PurePosixPath(enabled[prefix]['path']))
        if str(PurePosixPath(row.get('source_root', ''))) != configured_root:
            raise ValueError('Active scene root differs from its configured source')
        reference = expected[raw_id]
        if any(row.get(field) != reference.get(field) for field in ('date', 'region_id')) or row.get('file', '').replace('\\', '/') != reference.get('file', '').replace('\\', '/'):
            raise ValueError('Active scene file/date/region differs from the supplied catalog')
        bounds = row.get('bounds', [])
        if not isinstance(bounds, list) or len(bounds) != 4 or not all(isinstance(v, (float, int)) and math.isfinite(v) for v in bounds):
            raise ValueError('Active scene has no valid WGS84 bounds')
        if not (-180 <= bounds[0] < bounds[2] <= 180 and -90 <= bounds[1] < bounds[3] <= 90):
            raise ValueError('Active scene bounds are outside WGS84')
        groups.setdefault(prefix, []).append(row)
    if len(groups) != 1:
        raise ValueError('No unique active source matches this catalog; select --source-id explicitly when multiple sources match')
    chosen, rows = next(iter(groups.items()))
    if any(not any(row['date'] == day for row in rows) for day in (plan['before'], plan['after'])):
        raise ValueError('Matching active source does not have both selected dates available')
    if len({row['id'] for row in rows}) != len(rows):
        raise ValueError('Active source contains duplicate scene IDs')
    return chosen, rows


def tile_scene_ids(rows, bounds, before, after):
    selected = [row for row in rows if min(row['bounds'][2], bounds[2]) > max(row['bounds'][0], bounds[0])
                and min(row['bounds'][3], bounds[3]) > max(row['bounds'][1], bounds[1])]
    if any(not any(row['date'] == day for row in selected) for day in (before, after)):
        raise ValueError('This tile does not intersect available scenes for both selected dates')
    return [row['id'] for row in selected]


def make_plan(args):
    west, south, east, north = args.bounds
    if not all(math.isfinite(x) for x in args.bounds) or not (-180 <= west < east <= 180 and -85 <= south < north <= 85):
        raise ValueError('Require finite WGS84 W,S,E,N bounds, no dateline crossing, latitude within ±85°')
    before, after = date.fromisoformat(args.before), date.fromisoformat(args.after)
    if before >= after:
        raise ValueError('Before date must precede after date; use actual catalog date values')
    if not math.isfinite(args.resolution) or args.resolution < .01 or args.max_pixels < 4096:
        raise ValueError('Resolution must be >= 0.01 m and pixel budget >= 4096')
    # Conservative spherical estimate + 2% margin; not a measured output grid.
    # Actual crop dimensions are checked again before detection submission.
    closest_lat = 0 if south <= 0 <= north else min(abs(south), abs(north))
    metres_per_lat = 111700 * 1.02
    metres_per_lon = metres_per_lat * math.cos(math.radians(closest_lat))
    width, height = (east - west) * metres_per_lon, (north - south) * metres_per_lat
    edge = math.floor(math.sqrt(args.max_pixels)) - 1
    columns = max(1, math.ceil(width / (edge * args.resolution)))
    rows = max(1, math.ceil(height / (edge * args.resolution)))
    if columns * rows > args.max_tiles:
        raise ValueError(f'Plan needs {columns * rows} tiles; limit is {args.max_tiles}. Choose a smaller AOI first.')
    tiles = []
    for row in range(rows):
        for column in range(columns):
            bounds = [west + (east-west)*column/columns, south + (north-south)*row/rows,
                      west + (east-west)*(column+1)/columns, south + (north-south)*(row+1)/rows]
            pixels = math.ceil((bounds[2]-bounds[0])*metres_per_lon/args.resolution) * math.ceil((bounds[3]-bounds[1])*metres_per_lat/args.resolution)
            if pixels > args.max_pixels:
                raise ValueError('Estimated tile exceeds pixel budget')
            tiles.append(dict(id=f'r{row:04d}-c{column:04d}', bounds=bounds, estimated_pixels=pixels))
    plan = dict(schema=1, generated_at=now(), region_id=args.region_id, before=str(before), after=str(after),
                bounds=args.bounds, resolution_m=args.resolution, max_pixels=args.max_pixels,
                tiles=tiles, tile_count=len(tiles), concurrency=1,
                grid_estimate='WGS84 degree-to-metre approximation with 2% margin; verify actual crop grid before inference',
                overlap_pixels=0, merged_output_supported=False,
                limitations=['Per-task full-array postprocessing is not memory bounded.',
                             'No cross-tile seam correction, stitching or polygon deduplication.',
                             'Native source resolution may produce larger crop arrays; actual dimensions are gated.'])
    if args.output.exists():
        raise ValueError('Plan output already exists; choose a new filename')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.output, plan)
    print(json.dumps(dict(plan=str(args.output.resolve()), tiles=len(tiles), largest_estimated_pixels=max(t['estimated_pixels'] for t in tiles), submitted_tasks=0)))


class Metrics:
    def __init__(self, args, compose, output):
        self.args, self.compose, self.output = args, compose, output
        self.stop = threading.Event()
        self.breached = []
        self.thread = threading.Thread(target=self.safe_sample, daemon=True)

    def safe_sample(self):
        try:
            self.sample()
        except BaseException as exc:
            self.breached.append('Metrics sampler stopped: ' + str(exc))

    def sample(self):
        rss_code = "import glob,json;rows=[]\nfor p in glob.glob('/proc/[0-9]*/status'):\n try:\n  fields=dict(x.split(':',1) for x in open(p).read().splitlines() if ':' in x); rows.append({'pid':int(fields['Pid']),'rss_kib':int(fields.get('VmRSS','0 kB').split()[0])})\n except (OSError,ValueError,KeyError): pass\nprint(json.dumps({'processes':rows,'sum_rss_kib':sum(x['rss_kib'] for x in rows)}))"
        with (self.output / 'metrics.jsonl').open('a', encoding='utf-8') as log:
            while not self.stop.is_set():
                gpu = command(['nvidia-smi', '--query-gpu=index,uuid,memory.total,memory.used,utilization.gpu', '--format=csv,noheader,nounits'])
                containers = command(self.compose + ['ps', '-q'])
                ids = containers.get('stdout', '').split()
                stats = command(['docker', 'stats', '--no-stream', '--format', '{{json .}}', *ids]) if ids else dict(ok=False, error='No project containers')
                rss = command(self.compose + ['exec', '-T', 'py-algo', 'python', '-c', rss_code])
                free = shutil.disk_usage(self.args.storage_dir).free
                alerts = []
                if not gpu['ok'] or not stats['ok'] or not rss['ok']:
                    alerts.append('Metrics unavailable; do not treat this run as a validated benchmark')
                for line in gpu.get('stdout', '').splitlines():
                    try:
                        fields = [x.strip() for x in line.split(',')]
                        if float(fields[3]) / float(fields[2]) * 100 >= self.args.max_gpu_memory_percent:
                            alerts.append('GPU memory threshold reached (host metric may include other jobs)')
                    except (ValueError, IndexError, ZeroDivisionError):
                        alerts.append('Unparseable GPU metric')
                for line in stats.get('stdout', '').splitlines():
                    try:
                        if float(json.loads(line)['MemPerc'].rstrip('%')) >= self.args.max_container_memory_percent:
                            alerts.append('Container memory threshold reached')
                    except (ValueError, KeyError, TypeError):
                        alerts.append('Unparseable container memory metric')
                if free < self.args.min_free_gib * 1024**3:
                    alerts.append('Local storage free-space threshold reached')
                self.breached.extend(item for item in alerts if item not in self.breached)
                try:
                    rss_values = json.loads(rss.get('stdout', '{}'))
                except ValueError:
                    rss_values = None
                log.write(json.dumps(dict(at=now(), gpu=gpu, docker_stats=stats, python_process_rss=rss_values,
                                          rss_probe=rss, storage_free_bytes=free, alerts=alerts)) + '\n')
                log.flush()
                self.stop.wait(self.args.interval)


def execute(args):
    raw = args.plan.read_bytes()
    plan = json.loads(raw)
    digest = hashlib.sha256(raw).hexdigest()
    if plan.get('schema') != 1 or not plan.get('tiles') or plan.get('concurrency') != 1:
        raise ValueError('Unsupported or empty plan')
    tiles = plan['tiles'][:1 if args.stage == 'smoke' else 16 if args.stage == 'pilot' else None]
    for tile in tiles:
        bounds = tile.get('bounds', [])
        if len(bounds) != 4 or not all(isinstance(v, (float, int)) and math.isfinite(v) for v in bounds):
            raise ValueError('Invalid plan bounds')
        if not 0 < tile.get('estimated_pixels', 0) <= plan['max_pixels']:
            raise ValueError('Invalid plan pixel budget')
    if args.output.exists():
        raise ValueError('Run output must be a new directory')
    args.output.mkdir(parents=True)
    report = dict(stage=args.stage, plan_sha256=digest, requested_tiles=len(tiles), started_at=now(),
                  status='dry-run', submitted_tasks=0, completed=[], failures=[], concurrency=1)
    if not args.execute:
        write_json(args.output / 'summary.json', report)
        print(json.dumps(report))
        return
    try:
        if args.stage != 'smoke':
            expected = 'smoke' if args.stage == 'pilot' else 'pilot'
            previous = json.loads(args.prior_report.read_text(encoding='utf-8')) if args.prior_report else {}
            if previous.get('status') != 'passed' or previous.get('stage') != expected or previous.get('plan_sha256') != digest:
                raise ValueError(f'{args.stage} requires a passed {expected} summary for this exact plan')
        if not shutil.which('docker') or not shutil.which('nvidia-smi'):
            raise ValueError('Execution requires Docker and nvidia-smi on the selected GPU server')
        if not args.storage_dir.is_dir() or args.min_free_gib <= 0 or args.interval < 1:
            raise ValueError('Existing local storage-dir, positive free-space budget and interval required')
        catalog = json.loads(args.catalog.read_text(encoding='utf-8-sig'))['images']
        settings = api(args.base_url, '/api/settings')
        if not settings.get('effective') or settings['settings']['runtime']['workers'] != 1:
            raise ValueError('Apply synchronized settings with runtime.workers=1 before this baseline')
        if settings['settings']['runtime']['batch_size'] != 1:
            raise ValueError('Baseline requires batch_size=1; tune only after it passes')
        active = api(args.base_url, '/py/local/regions/' + urllib.parse.quote(plan['region_id'], safe='') + '/scenes')
        source_id, active_rows = matching_active_scenes(catalog, active['scenes'], settings, plan, getattr(args, 'source_id', None))
        report['source_id'] = source_id
        report['matched_active_scene_count'] = len(active_rows)
        compose = ['docker', 'compose', '--env-file', str(args.env_file), '-f', str(args.compose_file)]
        metrics = Metrics(args, compose, args.output)
        metrics.thread.start()
        try:
            # Let the first metrics sample complete before admitting a task.
            deadline = time.monotonic() + 60
            while not (args.output / 'metrics.jsonl').exists() or (args.output / 'metrics.jsonl').stat().st_size == 0:
                if metrics.breached:
                    raise RuntimeError('; '.join(metrics.breached))
                if time.monotonic() > deadline:
                    raise RuntimeError('Initial metrics sample timed out')
                time.sleep(.2)
            for tile in tiles:
                if metrics.breached:
                    raise RuntimeError('; '.join(metrics.breached))
                started = time.monotonic()
                report['current_tile'] = tile['id']
                report['progress_id'] = str(uuid.uuid4())
                scenes = tile_scene_ids(active_rows, tile['bounds'], plan['before'], plan['after'])
                report['current_tile_scene_count'] = len(scenes)
                write_json(args.output / 'summary.json', report)
                crop = api(args.base_url, '/api/images/local', dict(bounds=tile['bounds'], region_id=plan['region_id'], scene_ids=scenes,
                           start_date=plan['before'], end_date=plan['after'], max_images=0,
                           progress_id=report['progress_id'], config_revision=settings['revision'],
                           model_id=settings['settings']['default_model'], params={'resolution': plan['resolution_m']}), timeout=args.crop_timeout)
                images = crop.get('items', [])
                pair = [[im for im in images if im.get('capture_date', '').startswith(day)] for day in (plan['before'], plan['after'])]
                if any(len(items) != 1 for items in pair):
                    raise RuntimeError('Expected one cropped image for each selected date; sparse AOIs may have no data')
                if any(im['width'] * im['height'] > plan['max_pixels'] for im in images):
                    raise RuntimeError('Actual crop exceeds pixel budget; reduce planned tile size before detection')
                if metrics.breached:
                    raise RuntimeError('; '.join(metrics.breached))
                task = api(args.base_url, '/api/tasks', dict(batch_id=crop['batch_id'], before_id=pair[0][0]['id'], after_id=pair[1][0]['id'], algorithm='hebei_onnx'), timeout=60)
                report['submitted_tasks'] += 1
                report['current_task_id'] = task['id']
                write_json(args.output / 'summary.json', report)
                deadline = time.monotonic() + args.task_timeout
                while task['status'] in ('pending', 'running'):
                    if time.monotonic() > deadline:
                        raise RuntimeError('Monitor timeout; task may still be running; do not resubmit automatically')
                    time.sleep(args.interval)
                    task = api(args.base_url, '/api/tasks/' + urllib.parse.quote(task['id'], safe=''))
                if task['status'] not in ('succeeded', 'completed'):
                    raise RuntimeError(f'Task failed: {task.get("error_msg", task)}')
                result = api(args.base_url, '/api/tasks/' + task['id'] + '/result')
                write_json(args.output / (tile['id'] + '-result.json'), result)
                report['completed'].append(dict(tile_id=tile['id'], task_id=task['id'], elapsed_seconds=round(time.monotonic()-started, 3),
                                                crop_pixels=pair[0][0]['width']*pair[0][0]['height'], task=task))
                write_json(args.output / 'summary.json', report)
            if metrics.breached:
                raise RuntimeError('; '.join(metrics.breached))
            report['status'] = 'passed'
        finally:
            metrics.stop.set()
            metrics.thread.join(timeout=55)
            report['metric_alerts'] = metrics.breached
            if metrics.thread.is_alive():
                raise RuntimeError('Metrics sampler did not stop cleanly; run is incomplete')
    except BaseException as exc:
        report['status'] = 'failed'
        report['failures'].append(str(exc))
        report['inflight_policy'] = 'No task/container is killed. Check current_task_id/progress_id before retrying.'
    report['finished_at'] = now()
    report['completed_count'] = len(report['completed'])
    write_json(args.output / 'summary.json', report)
    print(json.dumps({key: report[key] for key in ('status', 'stage', 'submitted_tasks', 'completed_count', 'failures')}))
    if report['status'] != 'passed':
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    plan = sub.add_parser('plan', help='Offline plan only; no network or raster reads')
    plan.add_argument('--bounds', nargs=4, type=float, required=True, metavar=('W', 'S', 'E', 'N'))
    plan.add_argument('--region-id', required=True)
    plan.add_argument('--before', required=True)
    plan.add_argument('--after', required=True)
    plan.add_argument('--resolution', type=float, default=.8)
    plan.add_argument('--max-pixels', type=int, default=4_000_000)
    plan.add_argument('--max-tiles', type=int, default=1024)
    plan.add_argument('--output', type=Path, required=True)
    run = sub.add_parser('run', help='Dry run unless --execute is explicitly supplied')
    run.add_argument('--plan', type=Path, required=True)
    run.add_argument('--stage', choices=('smoke', 'pilot', 'scale'), default='smoke')
    run.add_argument('--execute', action='store_true')
    run.add_argument('--prior-report', type=Path)
    run.add_argument('--catalog', type=Path)
    run.add_argument('--source-id', help='Enabled source ID; required if multiple active sources match the same raw catalog')
    run.add_argument('--base-url', default='http://localhost:8088')
    run.add_argument('--compose-file', type=Path, default=Path('deploy/docker-compose.server.yml'))
    run.add_argument('--env-file', type=Path, default=Path('deploy/server.env'))
    run.add_argument('--storage-dir', type=Path, default=Path('/srv/change-detection/runtime'))
    run.add_argument('--min-free-gib', type=float, default=10)
    run.add_argument('--max-gpu-memory-percent', type=float, default=90)
    run.add_argument('--max-container-memory-percent', type=float, default=85)
    run.add_argument('--interval', type=float, default=5)
    run.add_argument('--crop-timeout', type=int, default=1800)
    run.add_argument('--task-timeout', type=int, default=7200)
    run.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'plan':
        make_plan(args)
    else:
        if args.execute and not args.catalog:
            parser.error('--execute requires --catalog matching the active source')
        if not 0 < args.max_gpu_memory_percent <= 100 or not 0 < args.max_container_memory_percent <= 100:
            parser.error('Memory thresholds must be between 0 and 100')
        execute(args)


if __name__ == '__main__':
    main()
