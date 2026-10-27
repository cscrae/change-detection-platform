"""Read-only Linux server preflight; writes only its requested JSON report.

Does not install packages, start/restart containers, mount storage, apply settings
or submit tasks. --runtime additionally checks already-running containers.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess


def run(args, timeout=30):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return dict(ok=result.returncode == 0, returncode=result.returncode,
                    stdout=result.stdout.strip(), stderr=result.stderr[-2000:])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return dict(ok=False, error=str(exc))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, default=Path('deploy/server.env'))
    parser.add_argument('--compose-file', type=Path, default=Path('deploy/docker-compose.server.yml'))
    parser.add_argument('--runtime', action='store_true', help='Probe already-running containers; no model inference')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    checks, warnings = [], []

    def check(name, ok, detail=None):
        checks.append(dict(name=name, ok=bool(ok), detail=detail))

    check('linux_host', platform.system() == 'Linux', platform.platform())
    check('docker_cli', shutil.which('docker') is not None)
    check('nvidia_smi', shutil.which('nvidia-smi') is not None)
    check('env_file', args.env_file.is_file())
    compose = ['docker', 'compose', '--env-file', str(args.env_file), '-f', str(args.compose_file)]
    config = run(compose + ['config', '--format', 'json'])
    check('compose_config', config['ok'], config.get('stderr') or config.get('error'))
    if config['ok']:
        doc = json.loads(config['stdout'])
        py = doc['services']['py-algo']
        volumes = {row['target']: row for row in py['volumes']}
        for target in ('/data', '/opt/assets/imagery', '/mnt/imagery', '/opt/models', '/opt/assets/boundaries', '/opt/assets/poi'):
            row = volumes.get(target, {})
            source = Path(row.get('source', '/path/not/configured'))
            check('mount:' + target, source.is_dir(), str(source))
            if target != '/data':
                check('read_only:' + target, row.get('read_only') is True)
            if target == '/data' and source.is_dir():
                check('data_writable', os.access(source, os.W_OK))
                free = shutil.disk_usage(source).free
                check('data_free_at_least_10GiB', free >= 10*1024**3, {'free_bytes': free})
            if target == '/opt/assets/poi' and source.is_dir():
                count = sum(1 for item in source.glob('*.geojson') if item.is_file())
                check('poi_directory_ready', True, {'geojson_files': count, 'empty_allowed': True})
                if count == 0:
                    warnings.append('POI directory is empty. Maps will report that POI data is not configured; this is not a startup failure.')
        model = Path(volumes['/opt/models']['source']) / 'enforcement_2.onnx'
        check('model_present', model.is_file(), str(model))
        # Hash the model, not the raster collection; proves which artifact is used.
        if model.is_file():
            with model.open('rb') as stream:
                check('model_sha256_recorded', True, hashlib.file_digest(stream, 'sha256').hexdigest())
        index = Path(volumes['/opt/assets/imagery']['source'])
        metadata = index / 'server-relocation.json'
        check('relocated_catalog', metadata.is_file(), str(metadata))
        if metadata.is_file():
            report = json.loads(metadata.read_text(encoding='utf-8'))
            check('vrt_mount_matches', report.get('container_root') == '/mnt/imagery')
        boundary = Path(volumes['/opt/assets/boundaries']['source']) / 'hebei-administrative-province.geojson'
        check('boundary_present', boundary.is_file(), str(boundary))
        ports = doc['services']['nginx'].get('ports', [])
        check('ingress_loopback_only', bool(ports) and all(p.get('host_ip') == '127.0.0.1' for p in ports))
        check('no_api_host_ports', not doc['services']['go-api'].get('ports') and not py.get('ports'))
        warnings.append('Dedicated runtime must reside on local disk; do not put SQLite on SMB/NFS. Check mount type on target host.')
        warnings.append('Do not reuse a Windows runtime database with E:/ or UNC model/source paths; initialize a separate server runtime.')
    for name, cmd in [('docker_server', ['docker', 'info', '--format', '{{.ServerVersion}}']),
                      ('gpu_devices', ['nvidia-smi', '--query-gpu=index,name,driver_version,memory.total', '--format=csv,noheader'])]:
        result = run(cmd)
        check(name, result['ok'], result.get('stdout') or result.get('error') or result.get('stderr'))
    if args.runtime:
        for name, cmd in [
            ('container_state', compose + ['ps', '--format', 'json']),
            ('container_nvidia_smi', compose + ['exec', '-T', 'py-algo', 'nvidia-smi', '-L']),
            ('ort_registered_cuda', compose + ['exec', '-T', 'py-algo', 'python', '-c',
             "import onnxruntime as ort; ort.preload_dlls(directory=''); print(ort.__version__,ort.get_available_providers()); assert 'CUDAExecutionProvider' in ort.get_available_providers()"]),
            ('catalog_sample_pixels', compose + ['exec', '-T', 'py-algo', 'python', '-c',
             "import json,pathlib,rasterio,numpy as np; p=pathlib.Path('/opt/assets/imagery'); rows=json.loads((p/'catalog.json').read_text())['images']; selected={}; [(selected.setdefault(r.get('role'),r)) for r in rows if r.get('role') in ('t1','t2')]; assert len(selected)==2; [(print(role,src.shape,float(np.mean(src.read([1,2,3],out_shape=(3,16,16))))),src.close()) for role,row in selected.items() for src in [rasterio.open(p/row['file'])]]"]),
        ]:
            result = run(cmd, timeout=120 if name == 'catalog_sample_pixels' else 30)
            check(name, result['ok'], result.get('stdout') or result.get('error') or result.get('stderr'))
        warnings.append('A registered CUDA provider and readable pixels do not prove real CUDA model inference. Validate the model in Management and run the smoke benchmark.')
    report = dict(at=datetime.now(timezone.utc).isoformat(), status='passed' if all(c['ok'] for c in checks) else 'failed',
                  scope='read-only-preflight', checks=checks, warnings=warnings, model_inference_tested=False)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        parser.error('Report path already exists; choose a new filename')
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(status=report['status'], failed_checks=[c['name'] for c in checks if not c['ok']], report=str(args.output.resolve()))))
    raise SystemExit(0 if report['status'] == 'passed' else 1)


if __name__ == '__main__':
    main()
