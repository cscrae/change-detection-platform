<script setup lang="ts">
import { computed } from 'vue';
import type { ImageryPhase } from '../utils/regions';
import { imageryPhaseLabel } from '../utils/timeline';
const props = defineProps<{ name?: string; phases: ImageryPhase[]; loadedIds: string[]; loading: boolean; error: string; disabled: boolean }>();
const emit = defineEmits<{ (event: 'add' | 'remove', id: string): void; (event: 'refresh'): void }>();
const availableCount = (phase: ImageryPhase) => phase.scenes.filter(scene => scene.available).length;
const years = computed(() => {
  const groups = new Map<string, ImageryPhase[]>();
  for (const phase of props.phases) {
    const year = phase.month.slice(0, 4);
    if (!groups.has(year)) groups.set(year, []);
    groups.get(year)!.push(phase);
  }
  return [...groups.entries()].sort(([a], [b]) => b.localeCompare(a)).map(([year, phases]) => ({
    year, phases: [...phases].sort((a, b) => b.month.localeCompare(a.month)),
  }));
});
function dragPhase(event: DragEvent, phase: ImageryPhase) {
  if (props.disabled || !phase.available || !event.dataTransfer) { event.preventDefault(); return; }
  event.dataTransfer.setData('application/x-cdp-phase', phase.id);
  event.dataTransfer.effectAllowed = 'copy';
}
</script>
<template>
  <section class="panel p-3 space-y-3 text-sm" aria-label="影像来源列表">
    <header class="flex items-center justify-between gap-2">
      <h3 class="text-cyan-400 font-semibold">{{ name ? `${name}影像` : '影像来源' }}</h3>
      <button class="btn-ghost text-xs" :disabled="disabled || loading || !name" @click="emit('refresh')">刷新目录</button>
    </header>
    <p v-if="loading" role="status" class="text-cyan-300 text-xs">正在读取影像目录…</p>
    <p v-else-if="error" role="alert" class="text-rose-400 text-xs">{{ error }}</p>
    <p v-else-if="!name" class="text-muted text-xs">点击地图选择影像来源。</p>
    <p v-else-if="!phases.length" class="text-muted text-xs">当前影像来源在所选范围没有影像时相。</p>
    <div class="region-imagery-list">
      <details v-for="group in years" :key="group.year" class="imagery-year" open>
        <summary>{{ group.year }} 年 <span class="text-muted">{{ group.phases.length }} </span></summary>
        <ul class="imagery-months">
      <li v-for="phase in group.phases" :key="phase.id" :data-phase-id="phase.id"
        class="imagery-scene" :class="{ 'is-loaded': loadedIds.includes(phase.id), 'is-unavailable': !phase.available }"
        :draggable="!disabled && phase.available" @dragstart="dragPhase($event, phase)">
        <strong class="imagery-month text-cyan-300">{{ imageryPhaseLabel(phase) }}</strong>
        <span class="imagery-count text-muted">{{ availableCount(phase) }} 张可用影像</span>
        <button class="phase-action" :disabled="disabled || !phase.available"
          :aria-label="`${loadedIds.includes(phase.id) ? '移除' : '加入'} ${imageryPhaseLabel(phase)} 时相`"
          :title="!phase.available ? '该时相暂无可用影像' : loadedIds.includes(phase.id) ? '移除时相' : '加入工作区'"
          @click="emit(loadedIds.includes(phase.id) ? 'remove' : 'add', phase.id)">
          <span aria-hidden="true">{{ loadedIds.includes(phase.id) ? '−' : '+' }}</span>
        </button>
      </li>
        </ul>
      </details>
    </div>
  </section>
</template>
<style scoped>
.region-imagery-list { display: flex; flex-direction: column; gap: 8px; max-height: 420px; overflow-y: auto; }
.imagery-year summary { padding: 6px 2px; color: #cbd5e1; font-weight: 600; cursor: pointer; }
.imagery-year summary span { margin-left: 8px; font-size: 12px; font-weight: 400; }
.imagery-months { display: flex; flex-direction: column; gap: 8px; margin: 6px 0 0; padding: 0 0 0 10px; list-style: none; }
.imagery-scene { display: flex; align-items: center; gap: 10px; padding: 8px 10px; border: 1px solid #244257; border-radius: 8px; background: #08182780; cursor: grab; }
.imagery-month { flex-shrink: 0; font-size: 14px; }
.imagery-count { flex: 1; text-align: right; font-size: 13px; }
.phase-action { display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0; width: 40px; height: 36px; border: 1px solid #244257; border-radius: 8px; background: #122d43; color: #f8fafc; font-size: 32px; line-height: 1; cursor: pointer; }
.phase-action:hover:not(:disabled) { border-color: #22d3ee; background: #193b53; }
.phase-action:focus-visible { outline: 2px solid #22d3ee; outline-offset: 2px; }
.phase-action:disabled { cursor: not-allowed; opacity: .45; }
.imagery-scene.is-loaded { border-color: #22d3ee80; }
.imagery-scene.is-unavailable { cursor: default; opacity: .7; }
</style>
