"""Copy only current test_up indices/VRT XML to a new Linux-compatible catalog.

No raster is opened/copied, and the input catalog and source TIFFs are untouched.
The output must be a new directory. Standard library only; runs on Windows/Linux.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import tempfile
import xml.etree.ElementTree as ET
from test_up_source import resolve_source_root


def contained_file(root: Path, value: str) -> Path:
    relative = PurePosixPath(value.replace('\\', '/'))
    if relative.is_absolute() or '..' in relative.parts or ':' in value:
        raise ValueError(f'Unsafe catalog file: {value}')
    result = (root / Path(*relative.parts)).resolve()
    if not result.is_relative_to(root) or result.suffix.lower() != '.vrt':
        raise ValueError(f'Expected VRT beneath input catalog: {value}')
    return result


def translate(value: str, windows_root: PureWindowsPath, container_root: PurePosixPath) -> str:
    # Use Windows path semantics even when running on Linux.
    old = PureWindowsPath(value)
    if not old.is_absolute():
        raise ValueError(f'Expected absolute source reference: {value}')
    if '..' in old.parts:
        raise ValueError(f'Parent traversal in source reference: {value}')
    try:
        relative = old.relative_to(windows_root)
    except ValueError as exc:
        raise ValueError(f'Source reference is outside --windows-root: {value}') from exc
    return str(container_root.joinpath(*relative.parts))


def rewrite_metadata(value, old_root, new_root):
    if isinstance(value, dict):
        return {key: rewrite_metadata(item, old_root, new_root) for key, item in value.items()}
    if isinstance(value, list):
        return [rewrite_metadata(item, old_root, new_root) for item in value]
    if isinstance(value, str):
        try:
            return translate(value, old_root, new_root)
        except ValueError:
            return value
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--windows-root', help='Original Windows root; defaults to TEST_UP_SOURCE_ROOT or project .env.local.json')
    parser.add_argument('--container-root', default='/mnt/imagery')
    args = parser.parse_args()
    try:
        args.windows_root = resolve_source_root(args.windows_root)
    except ValueError as exc:
        parser.error(str(exc))
    source, output = args.input.resolve(), args.output.resolve()
    old_root, new_root = PureWindowsPath(args.windows_root), PurePosixPath(args.container_root)
    if not old_root.is_absolute() or not new_root.is_absolute() or '..' in new_root.parts:
        parser.error('Source and container roots must be absolute and contain no parent traversal')
    if output.exists() or output.is_relative_to(source) or source.is_relative_to(output):
        parser.error('Output must be a new directory outside the input tree')
    catalog = json.loads((source / 'catalog.json').read_text(encoding='utf-8-sig'))
    if not isinstance(catalog.get('images'), list) or not catalog['images']:
        parser.error('Input catalog must contain a nonempty images list')
    vrt_paths = sorted({row['file'] for row in catalog['images']})
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=output.name + '.staging-', dir=output.parent))
    references = 0
    try:
        for relative in vrt_paths:
            path = contained_file(source, relative)
            xml = path.read_bytes()
            if b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():
                raise ValueError(f'DTD/entity is unsupported: {relative}')
            root = ET.fromstring(xml)
            if root.tag != 'VRTDataset':
                raise ValueError(f'Not a VRT: {relative}')
            nodes = root.findall('.//SourceFilename')
            if not nodes:
                raise ValueError(f'No source references: {relative}')
            for node in nodes:
                if node.get('relativeToVRT', '0') != '0' or not node.text:
                    raise ValueError(f'Only absolute source references are supported: {relative}')
                node.text = translate(node.text, old_root, new_root)
                node.set('relativeToVRT', '0')
                references += 1
            target = temporary / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(ET.tostring(root, encoding='utf-8', xml_declaration=True))
        for name in ('catalog.json', 'pairs.json', 'prepare-test-up.json'):
            path = source / name
            if not path.is_file():
                continue
            content = rewrite_metadata(json.loads(path.read_text(encoding='utf-8-sig')), old_root, new_root)
            if name == 'prepare-test-up.json':
                content['local_source_path'] = '/opt/assets/imagery'
                content['app_settings_applied'] = False
            (temporary / name).write_text(json.dumps(content, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        report = dict(generator=Path(__file__).name, prepared_at=datetime.now(timezone.utc).isoformat(),
                      source_catalog=str(source), source_catalog_sha256=hashlib.sha256((source / 'catalog.json').read_bytes()).hexdigest(),
                      source_root=str(old_root), container_root=str(new_root), catalog_rows=len(catalog['images']),
                      unique_vrts=len(vrt_paths), rewritten_references=references, originals_copied=False,
                      source_modified=False, pixel_read_tested=False, app_settings_applied=False)
        (temporary / 'server-relocation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.rename(output)
    except BaseException:
        # Only the staging directory created above is eligible for cleanup.
        if temporary.exists() and temporary.parent == output.parent and temporary.name.startswith(output.name + '.staging-'):
            shutil.rmtree(temporary)
        raise
    print(json.dumps(dict(report, output=str(output)), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
