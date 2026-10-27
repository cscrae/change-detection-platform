import type { ChangePatchCollection, DetectionStats, ImageInfo } from './api';
import type { ImageryPhase } from './regions';

export interface TimelinePair {
  key: string;
  before: ImageInfo;
  after: ImageInfo;
  color: string;
  status: 'waiting' | 'pending' | 'running' | 'succeeded' | 'failed';
  taskId?: string;
  maskPath?: string;
  stats?: DetectionStats;
  geojson?: ChangePatchCollection;
  warning?: string;
}

const COLORS = ['#f43f5e', '#06b6d4', '#f59e0b', '#8b5cf6', '#10b981', '#3b82f6', '#ec4899', '#84cc16'];
export function pairColor(index: number): string {
  if (index < COLORS.length) return COLORS[index];
  // Continue with distinct hues instead of recycling the first eight colors.
  const hue = (index * 137.508) % 360;
  const channel = (n: number) => {
    const k = (n + hue / 30) % 12;
    return Math.round(255 * (0.5 - 0.38 * Math.max(-1, Math.min(k - 3, 9 - k, 1))))
      .toString(16).padStart(2, '0');
  };
  return `#${channel(0)}${channel(8)}${channel(4)}`;
}

export function sourceName(image: ImageInfo): string {
  try {
    const metadata = JSON.parse(image.meta_json || '{}');
    if (Array.isArray(metadata.source_files) && metadata.source_files.length) return metadata.source_files.join(' + ');
    if (typeof metadata.source_file === 'string' && metadata.source_file) return metadata.source_file;
  } catch { /* Older image records may not include metadata. */ }
  return image.file_path.split(/[\\/]/).pop() || image.id;
}

export function phaseDate(image: ImageInfo): string {
  try {
    const precision = JSON.parse(image.meta_json || '{}').date_precision;
    if (precision === 'year') return image.capture_date?.slice(0, 4) || '日期未知';
    if (precision === 'day') return image.capture_date?.slice(0, 10) || '日期未知';
  } catch { /* Older image records retain their existing month/day formatting. */ }
  return image.capture_date?.slice(0, image.source === 'local' ? 7 : 10) || '日期未知';
}

/** Display label only; phase.month remains the workspace key and request value. */
export function imageryPhaseLabel(phase: Pick<ImageryPhase, 'month' | 'scenes'>): string {
  return phase.scenes.length > 0 && phase.scenes.every(scene => scene.date_precision === 'year')
    ? `${phase.month.slice(0, 4)}年（月份未知）`
    : phase.month;
}

export function pairBounds(before: ImageInfo, after: ImageInfo): [number, number, number, number] | undefined {
  const values = [before.bounds_south, before.bounds_west, before.bounds_north, before.bounds_east,
    after.bounds_south, after.bounds_west, after.bounds_north, after.bounds_east];
  if (!values.every(value => typeof value === 'number' && Number.isFinite(value))) return;
  const bounds: [number, number, number, number] = [
    Math.max(before.bounds_south!, after.bounds_south!), Math.max(before.bounds_west!, after.bounds_west!),
    Math.min(before.bounds_north!, after.bounds_north!), Math.min(before.bounds_east!, after.bounds_east!),
  ];
  return bounds[0] < bounds[2] && bounds[1] < bounds[3] ? bounds : undefined;
}

export function buildTimeline(images: ImageInfo[]): { images: ImageInfo[]; pairs: TimelinePair[] } {
  if (images.some(image => !image.capture_date || !/^\d{4}-\d{2}-\d{2}/.test(image.capture_date) ||
      !Number.isFinite(Date.parse(image.capture_date)))) throw new Error('影像缺少有效日期，无法自动配对时相');
  const sorted = [...images].sort((a, b) => Date.parse(a.capture_date!) - Date.parse(b.capture_date!) || a.id.localeCompare(b.id));
  const phases = new Map<string, ImageInfo[]>();
  for (const image of sorted) {
    const date = phaseDate(image);
    phases.set(date, [...(phases.get(date) || []), image]);
  }
  const groups = [...phases.values()];
  if (groups.length < 2) throw new Error(images.length
    ? '该区域只有一个可用时相，至少需要两个不同日期的时相才能检测'
    : '该区域没有本地影像覆盖，请重新选择区域');
  const pairs: TimelinePair[] = [];
  for (let i = 0; i < groups.length - 1; i++) {
    const adjacent: TimelinePair[] = [];
    for (const before of groups[i]) for (const after of groups[i + 1]) {
      if (!pairBounds(before, after)) continue;
      adjacent.push({ key: `${before.id}:${after.id}`, before: { ...before, phase: 'before' },
        after: { ...after, phase: 'after' }, color: pairColor(pairs.length + adjacent.length), status: 'waiting' });
    }
    if (!adjacent.length) throw new Error(`${phaseDate(groups[i][0])} 与 ${phaseDate(groups[i + 1][0])} 影像没有空间交集，无法检测`);
    pairs.push(...adjacent);
  }
  return { images: sorted, pairs };
}
