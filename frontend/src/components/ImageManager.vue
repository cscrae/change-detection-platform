<script setup lang="ts">
import { ref, onMounted, computed, watch } from 'vue';
import type { ImageInfo, AreaBounds, AreaPolygon } from '../utils/api';
import { phaseDate, sourceName } from '../utils/timeline';
import { getLocalCatalog, type LocalScene } from '../utils/basemap';

const props = withDefaults(defineProps<{ bounds?: AreaBounds | null; geometry?: AreaPolygon | null; images?: ImageInfo[]; showCatalog?: boolean }>(), { images: () => [], showCatalog: true });
const emit = defineEmits<{
  (e: 'area', bounds: AreaBounds | null): void;
  (e: 'locate', bounds: AreaBounds): void;
}>();

const activeTab = ref<'area' | 'list'>('area');
const localScenes = ref<LocalScene[]>([]);
const coverageWarning = ref('');
const errorMsg = ref('');
const failedPreviews = ref<Record<string, boolean>>({});
const previewVersion = ref<Record<string, number>>({});
const wInput = ref('');
const sInput = ref('');
const eInput = ref('');
const nInput = ref('');

watch(() => props.bounds, bounds => {
  wInput.value = bounds?.west.toFixed(6) ?? '';
  sInput.value = bounds?.south.toFixed(6) ?? '';
  eInput.value = bounds?.east.toFixed(6) ?? '';
  nInput.value = bounds?.north.toFixed(6) ?? '';
}, { immediate: true });

const center = computed(() => {
  const b = props.bounds;
  return b ? { lat: (b.south + b.north) / 2, lng: (b.west + b.east) / 2 } : null;
});
const spanKM = computed(() => {
  const b = props.bounds;
  if (!b) return null;
  return {
    w: Number(((b.east - b.west) * 111.32 * Math.cos((b.south + b.north) / 2 * Math.PI / 180)).toFixed(2)),
    h: Number(((b.north - b.south) * 111.32).toFixed(2)),
  };
});

function locateScene(scene: LocalScene): void {
  const [west, south, east, north] = scene.bounds;
  emit('locate', { west, south, east, north });
}
function clearBounds(): void { emit('area', null); }
function applyCoordInput(): void {
  const fields = [
    { label: '西 W（经度）', input: wInput, min: -180, max: 180 },
    { label: '南 S（纬度）', input: sInput, min: -90, max: 90 },
    { label: '东 E（经度）', input: eInput, min: -180, max: 180 },
    { label: '北 N（纬度）', input: nInput, min: -90, max: 90 },
  ];
  const values: number[] = [];
  for (const field of fields) {
    const text = String(field.input.value).trim();
    const value = Number(text);
    if (!text || !Number.isFinite(value)) {
      errorMsg.value = `${field.label}未填写或格式无效，请只输入数字和小数点`;
      return;
    }
    if (value < field.min || value > field.max) {
      errorMsg.value = `${field.label}须在 ${field.min} 至 ${field.max} 之间，请检查经纬度是否填反`;
      return;
    }
    values.push(value);
  }
  const [west, south, east, north] = values;
  if (west >= east || south >= north) {
    errorMsg.value = '西经度须小于东经度，南纬度须小于北纬度';
    return;
  }
  errorMsg.value = '';
  const bounds = { west, south, east, north };
  emit('area', bounds);
  emit('locate', bounds);
}

function imagePreviewSrc(img: ImageInfo): string { return `/api/images/${img.id}/preview?ts=${img.created_at}&retry=${previewVersion.value[img.id] || 0}`; }
watch(() => props.images, images => { if (images.length) activeTab.value = 'list'; });
onMounted(async () => {
  if (!props.showCatalog) return;
  try {
    const data = await getLocalCatalog();
    localScenes.value = data.images;
    coverageWarning.value = data.warning ?? '';
  } catch (e: unknown) { errorMsg.value = e instanceof Error ? e.message : '影像目录加载失败'; }
});
</script>

<template>
  <section class="panel p-3 space-y-3 text-sm">
    <header class="flex items-center justify-between">
      <h3 class="text-cyan-400 font-semibold">📡 影像数据</h3>
    </header>

    <!-- Tabs -->
    <div class="flex divider-b">
      <button
        v-for="tab in (['area', 'list'] as const)"
        :key="tab"
        class="flex-1 py-1.5 text-xs transition"
        :class="activeTab === tab ? 'text-cyan-400 border-b-2 border-cyan-400 font-semibold' : 'text-muted hover:text-slate-200'"
        @click="activeTab = tab"
      >
        {{ tab === 'area' ? '检测区域' : `本次影像 (${images.length})` }}
      </button>
    </div>

    <p v-if="errorMsg" class="text-rose-400 text-xs">{{ errorMsg }}</p>

    <!-- ═════─ local imagery ──── -->
    <div v-show="activeTab === 'area'" class="space-y-3">
      <p v-if="coverageWarning" class="text-xs text-amber-300">{{ coverageWarning }}</p>
      <div class="flex flex-wrap gap-1">
        <button v-for="scene in localScenes" :key="scene.id" class="btn-ghost text-xs" @click="locateScene(scene)">{{ scene.region }} {{ scene.date.slice(0, scene.date_precision === 'year' ? 4 : scene.date_precision === 'day' ? 10 : 7) }}</button>
      </div>

      <!-- 选区信息 -->
      <div v-if="bounds" class="text-xs space-y-1 bg-slate-900/60 rounded p-2">
        <div class="flex items-center justify-between text-cyan-300">
          <span>{{ geometry ? `多边形区域 · ${geometry.coordinates[0].length - 1} 个顶点` : '已选区域' }}</span>
          <button class="text-muted hover:text-red-400" @click="clearBounds">✕ 清除</button>
        </div>
        <div class="text-slate-300">
          {{ geometry ? '包围盒：' : '范围：' }}
          <span class="text-cyan-400">W {{ wInput }} · S {{ sInput }}</span> →
          <span class="text-cyan-400">E {{ eInput }} · N {{ nInput }}</span>
        </div>
        <div v-if="center && spanKM" class="text-muted">
          中心 ({{ center.lat.toFixed(5) }}, {{ center.lng.toFixed(5) }}) ·
          {{ spanKM.w }}km × {{ spanKM.h }}km
        </div>
      </div>
      <p v-else class="text-xs text-faint">
        尚未选区，请在中央地图绘制区域或输入经纬度。
      </p>

      <!-- 精确输入 -->
      <div class="text-xs space-y-1">
        <p class="text-muted">{{ geometry ? '应用经纬度将替换为矩形选区' : '手动输入区域（经纬度，可精确微调）' }}</p>
        <div class="grid grid-cols-2 gap-2">
          <label class="flex flex-col">
            <span class="text-muted">西 W（经度）</span>
            <input v-model="wInput" type="number" step="any" min="-180" max="180" class="input" />
          </label>
          <label class="flex flex-col">
            <span class="text-muted">南 S（纬度）</span>
            <input v-model="sInput" type="number" step="any" min="-90" max="90" class="input" />
          </label>
          <label class="flex flex-col">
            <span class="text-muted">东 E（经度）</span>
            <input v-model="eInput" type="number" step="any" min="-180" max="180" class="input" />
          </label>
          <label class="flex flex-col">
            <span class="text-muted">北 N（纬度）</span>
            <input v-model="nInput" type="number" step="any" min="-90" max="90" class="input" />
          </label>
        </div>
        <button
          class="btn-secondary w-full py-1.5 font-semibold"
          @click="applyCoordInput"
        >
          应用该区域框
        </button>
      </div>

    </div>

    <!-- ═══── list tab ─── -->
    <div v-if="activeTab === 'list'" class="space-y-2">
      <div class="max-h-[320px] overflow-y-auto border border-slate-700 rounded-lg">
        <table class="w-full text-xs">
          <thead class="table-head sticky top-0">
            <tr>
              <th class="px-2 py-1 text-left">缩略图</th>
              <th class="px-2 py-1 text-left">来源影像</th>
              <th class="px-2 py-1 text-left">日期</th>
              <th class="px-2 py-1 text-left">尺寸</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="img in images" :key="img.id" class="divider-t row-hover">
              <td class="px-2 py-1">
                <div class="w-10 h-10 rounded border border-slate-600 bg-slate-900 flex items-center justify-center overflow-hidden">
                  <button v-if="failedPreviews[img.id]" class="text-xs" @click="failedPreviews[img.id] = false; previewVersion[img.id] = (previewVersion[img.id] || 0) + 1">重试</button>
                  <img v-else :src="imagePreviewSrc(img)" :alt="img.id" class="w-10 h-10 object-cover" loading="lazy" @error="failedPreviews[img.id] = true" />
                </div>
              </td>
              <td class="px-2 py-1 text-slate-300 break-all min-w-[100px]">{{ sourceName(img) }}</td>
              <td class="px-2 py-1 text-muted whitespace-nowrap">{{ phaseDate(img) }}</td>
              <td class="px-2 py-1 text-muted">{{ img.width ? `${img.width}×${img.height}` : '-' }}</td>
            </tr>
            <tr v-if="images.length === 0">
              <td colspan="4" class="px-2 py-4 text-center text-faint">选区后提交检测任务，自动获取影像</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>
</template>
