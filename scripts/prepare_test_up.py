"""Prepare a local VRT/catalog bridge to read-only SMB test_up imagery.

Run with backend-py/.venv-local/Scripts/python.exe. Defaults to one Lingshou
pair. Source TIFFs and labels remain untouched; this never applies app settings.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.io import MemoryFile
from shapely.geometry import box, shape
from shapely.ops import unary_union
from test_up_source import resolve_source_root

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = PROJECT / 'data/validation/hebei-testsets-2026-10-26/manifest-test-up.jsonl'
GROUP_MONTHS = {
    '4月衡水': None, '4月邢台': None,  # Source VRTs mix March supplements and April.
    '5月衡水': '2025-05', '5月邢台': '2025-05',
    '6月张家口1_2': '2025-06', '6月衡水1_2': '2025-06',
    '6月邢台1_2': '2025-06', '8月灵寿': '2025-08',
}
PROVENANCE = '原目录 VRT 引用的源文件名；未独立核实传感器拍摄日期'


def arguments():
    parser = argparse.ArgumentParser(description='生成 test_up 本地索引和 VRT，原图留在共享目录。')
    parser.add_argument('--source-root', help='共享根目录；默认读取 TEST_UP_SOURCE_ROOT 环境变量或项目 .env.local.json')
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument('--group', nargs='+', choices=GROUP_MONTHS, help='默认只准备 8月灵寿')
    choice.add_argument('--all-groups', action='store_true')
    parser.add_argument('--limit', type=int, default=1, help='每组配对上限，0 表示该组全部；默认 1')
    parser.add_argument('--output', type=Path, default=PROJECT / 'data/imagery/hebei-test-up')
    parser.add_argument('--manifest', type=Path, help='可选完整配对 JSONL；默认优先使用已有调查清单')
    parser.add_argument('--replace', action='store_true', help='替换本脚本之前生成的目录清单')
    args = parser.parse_args()
    try:
        args.source_root = resolve_source_root(args.source_root)
    except ValueError as exc:
        parser.error(str(exc))
    if args.limit < 0:
        parser.error('--limit 必须大于等于 0')
    args.groups = list(GROUP_MONTHS) if args.all_groups else (args.group or ['8月灵寿'])
    if not Path(args.source_root).is_absolute():
        parser.error('--source-root 必须是绝对路径或 UNC 共享路径')
    args.output = args.output.resolve()
    permitted = (PROJECT / 'data/imagery').resolve()
    if args.output == permitted or not args.output.is_relative_to(permitted):
        parser.error('--output 必须是项目 data/imagery 下的专用子目录')
    if str(args.output).startswith('\\\\'):
        parser.error('输出必须是本地目录，不能写到中转站')
    marker = args.output / 'prepare-test-up.json'
    if args.output.exists() and any(args.output.iterdir()):
        if not marker.exists() or json.loads(marker.read_text(encoding='utf-8')).get('generator') != 'prepare_test_up.py':
            parser.error('输出目录已有其他数据；请选择新的专用子目录')
        if not args.replace:
            parser.error('该目录已经准备过；重新生成请明确添加 --replace')
    return args


def select_pairs(args):
    manifest = args.manifest or (DEFAULT_MANIFEST if DEFAULT_MANIFEST.exists() else None)
    if manifest:
        records = [json.loads(line) for line in manifest.read_text(encoding='utf-8').splitlines() if line.strip()]
    else:
        records = []
        root = Path(args.source_root)
        for group in args.groups:
            names = {}
            for role in ['t1_patches', 't2_patches', 'label_patches']:
                names[role] = {p.name for p in (root / group / role).iterdir() if p.suffix.lower() in {'.tif', '.tiff'}}
            if not (names['t1_patches'] == names['t2_patches'] == names['label_patches']):
                raise ValueError(f'{group} 前期、后期、标签的文件名集合不一致')
            records.extend({'group': group, 'filename': name} for name in sorted(names['t1_patches']))
    result = []
    for group in args.groups:
        selected = sorted((r for r in records if r['group'] == group), key=lambda r: r['filename'])
        if not selected:
            raise ValueError(f'清单没有找到 {group}')
        if len({r['filename'] for r in selected}) != len(selected):
            raise ValueError(f'{group} 存在重复文件名')
        for record in selected[:args.limit or None]:
            name = record['filename']
            if Path(name).name != name or Path(name).suffix.lower() not in {'.tif', '.tiff'}:
                raise ValueError(f'不合法的文件名: {name}')
            # Reconstruct beneath the selected source root; never trust arbitrary paths from JSONL.
            result.append({'group': group, 'filename': name, **{
                role: Path(args.source_root) / group / folder / name
                for role, folder in [('t1', 't1_patches'), ('t2', 't2_patches'), ('label', 'label_patches')]
            }})
    return result


def header(path):
    with rasterio.open(path) as src:
        if src.crs is None or src.count != 3 or any(dt != 'uint8' for dt in src.dtypes):
            raise ValueError(f'需要有坐标系的三波段 uint8 影像: {path}')
        if src.transform.a <= 0 or src.transform.e >= 0 or abs(src.transform.b) + abs(src.transform.d) > 1e-10:
            raise ValueError(f'仅支持北向上的规则网格: {path}')
        stat = path.stat()
        return dict(path=str(path), width=src.width, height=src.height,
                    crs=src.crs.to_wkt(), transform=list(src.transform.to_gdal()),
                    bounds=list(src.bounds), nodata=src.nodata,
                    block_shapes=[list(s) for s in src.block_shapes],
                    source_version=f'{stat.st_mtime_ns}:{stat.st_size}')


def vrt_xml(reference, source):
    root = ET.Element('VRTDataset', rasterXSize=str(reference['width']), rasterYSize=str(reference['height']))
    ET.SubElement(root, 'SRS').text = reference['crs']
    ET.SubElement(root, 'GeoTransform').text = ', '.join(map(str, reference['transform']))
    for band, color in enumerate(['Red', 'Green', 'Blue'], 1):
        node = ET.SubElement(root, 'VRTRasterBand', dataType='Byte', band=str(band))
        ET.SubElement(node, 'ColorInterp').text = color
        if source['nodata'] is not None:
            ET.SubElement(node, 'NoDataValue').text = str(source['nodata'])
        item = ET.SubElement(node, 'ComplexSource', resampling='bilinear')
        ET.SubElement(item, 'SourceFilename', relativeToVRT='0').text = source['path']
        ET.SubElement(item, 'SourceBand').text = str(band)
        block_h, block_w = source['block_shapes'][band - 1]
        ET.SubElement(item, 'SourceProperties', RasterXSize=str(source['width']), RasterYSize=str(source['height']),
                      DataType='Byte', BlockXSize=str(block_w), BlockYSize=str(block_h))
        ET.SubElement(item, 'SrcRect', xOff='0', yOff='0', xSize=str(source['width']), ySize=str(source['height']))
        ET.SubElement(item, 'DstRect', xOff='0', yOff='0', xSize=str(reference['width']), ySize=str(reference['height']))
        if source['nodata'] is not None:
            ET.SubElement(item, 'NODATA').text = str(source['nodata'])
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def probe(xml):
    # Opening VRT metadata alone does not prove SMB pixels are readable.
    with MemoryFile(xml, ext='.vrt') as mem, mem.open() as src:
        rgb = src.read([1, 2, 3], out_shape=(3, 16, 16), resampling=Resampling.bilinear)
        valid = src.dataset_mask(out_shape=(16, 16)) > 0
        return dict(output_shape=list(rgb.shape), valid_pixels=int(valid.sum()),
                    minimum=int(rgb.min()), maximum=int(rgb.max()))


def date_fields(group, role):
    month = '2024-11' if role == 't1' else GROUP_MONTHS[group]
    if month:
        return dict(date=month + '-01', date_precision='month', date_source=PROVENANCE,
                    warning='日期中的01是月份占位；源月份由 VRT 引用名称提供，尚未核实传感器元数据。')
    return dict(date='2025-01-01', date_precision='year', date_source=PROVENANCE,
                warning='该组后期 VRT 同时引用2025年3月补景和4月影像，切片与源景未逐张映射；仅登记2025年，01-01为占位。')


def atomic_json(path, document):
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    tmp.replace(path)


def main():
    args = arguments()
    pairs = select_pairs(args)
    boundaries = json.loads((PROJECT / 'data/boundaries/hebei-source-groups.geojson').read_text(encoding='utf-8'))
    regions = [(f['properties'], shape(f['geometry'])) for f in boundaries['features']]
    province = unary_union([geometry for _, geometry in regions])
    images, pair_index = [], []
    generation = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    vrt_dir = args.output / 'vrt' / generation
    vrt_dir.mkdir(parents=True, exist_ok=True)
    atomic_json(args.output / 'prepare-test-up.json', dict(generator='prepare_test_up.py', status='preparing',
                source_root=args.source_root, app_settings_applied=False))
    with rasterio.Env(GDAL_PAM_ENABLED='NO', GDAL_DISABLE_READDIR_ON_OPEN='EMPTY_DIR'):
        for number, pair in enumerate(pairs, 1):
            before, after = header(pair['t1']), header(pair['t2'])
            if before['crs'] != after['crs'] or not np.allclose(before['bounds'], after['bounds'], rtol=0, atol=1e-6):
                raise ValueError(f'两期坐标系/覆盖范围不同，需单独配准，已停止: {pair["filename"]}')
            from rasterio.warp import transform_bounds
            wgs_bounds = list(transform_bounds(before['crs'], 'EPSG:4326', *before['bounds']))
            footprint = box(*wgs_bounds)
            if not province.covers(footprint):
                raise ValueError(f'切片矩形范围未完全位于河北，需要人工确认: {pair["filename"]}')
            props, _ = max(regions, key=lambda item: item[1].intersection(footprint).area)
            region_id, region_name = props['region_id'], props['name']
            memberships = [dict(region_id=p['region_id'], region=p['name'],
                                bbox_fraction_approx=g.intersection(footprint).area / footprint.area)
                           for p, g in regions if g.intersection(footprint).area / footprint.area > 1e-9]
            key = hashlib.sha256((pair['group'] + '/' + pair['filename']).encode()).hexdigest()[:20]
            previews = {}
            scene_ids = []
            for role, metadata in [('t1', before), ('t2', after)]:
                xml = vrt_xml(before, metadata)
                previews[role] = probe(xml)
                if not previews[role]['valid_pixels']:
                    raise ValueError(f'{pair["filename"]} 的 {role} 抽样没有有效像元，请单独检查')
                target = vrt_dir / f'{key}-{role}.vrt'
                # Source versions are embedded so rewriting after source changes also changes the VRT.
                document = ET.fromstring(xml)
                tags = ET.SubElement(document, 'Metadata')
                ET.SubElement(tags, 'MDI', key='source_version').text = metadata['source_version']
                target.write_bytes(ET.tostring(document, encoding='utf-8', xml_declaration=True))
                # A cross-city tile is visible in each intersecting source region; the platform
                # clips it to the selected region. All views reference the same local VRT.
                for member in memberships:
                    scene_id = f'test-up-{key}-{role}-{member["region_id"]}'
                    scene_ids.append(scene_id)
                    images.append(dict(id=scene_id, file=target.relative_to(args.output).as_posix(),
                                       region=member['region'], region_id=member['region_id'],
                                       **date_fields(pair['group'], role),
                                       original_group=pair['group'], pair_id=key, role=role,
                                       source_file=metadata['path'], source_version=metadata['source_version']))
            pair_index.append(dict(pair_id=key, group=pair['group'], filename=pair['filename'],
                                   region=region_name, region_id=region_id, bounds=wgs_bounds,
                                   region_memberships=memberships,
                                   scene_ids=scene_ids, label_source=str(pair['label']),
                                   label_policy='保留共享路径供后续评估；未作为展示或检测输入登记，未验证标签像元类别',
                                   original_sources={'t1': before, 't2': after}, pixel_probes=previews,
                                   aligned_grid=dict(width=before['width'], height=before['height'],
                                                     crs=before['crs'], transform=before['transform'])))
            print(f'[{number}/{len(pairs)}] {pair["group"]} / {pair["filename"]} -> {region_name}', flush=True)
    report = dict(generator='prepare_test_up.py', status='prepared', prepared_at=datetime.now(timezone.utc).isoformat(),
                  source_root=args.source_root, source_modified=False, originals_copied=False,
                  groups=args.groups, limit_per_group=args.limit, pairs=len(pair_index), scenes=len(images),
                  regions=dict(Counter(item['region'] for item in pair_index)),
                  administrative_policy='按影像范围判断；跨市切片在每个相交地市分别登记，同一VRT被复用，平台按所选地市裁切',
                  local_source_path=str(args.output), date_policy=PROVENANCE,
                  source_year_setting='必须留空；此目录同时包含2024和2025年',
                  grid_policy='VRT同覆盖范围重采样到前期网格；后期上采样不增加真实空间细节',
                  pairs_index='pairs.json',
                  refresh_policy='原始共享文件更新后重新运行 --replace，并在平台重新校验、应用配置及刷新目录',
                  app_settings_applied=False)
    atomic_json(args.output / 'pairs.json', {'pairs': pair_index})
    atomic_json(args.output / 'prepare-test-up.json', report)
    # Publish the catalogue last. Unlisted VRTs from a previous run are harmless.
    atomic_json(args.output / 'catalog.json', {'images': images})
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    print('准备完成。请在“管理与配置 → 影像来源”添加 local_source_path；年份留空，先校验再应用。')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, rasterio.errors.RasterioError) as error:
        print(f'准备失败：{error}', file=sys.stderr, flush=True)
        raise SystemExit(1)
