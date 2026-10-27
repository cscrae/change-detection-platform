<script setup lang="ts">
import { ref, watch, onMounted, onActivated, onDeactivated, onUnmounted, nextTick } from 'vue';
import { useWorkspace, registerAiMapAdapter } from '../composables/useWorkspace';
import AlgorithmForm from '../components/AlgorithmForm.vue';
import ImageManager from '../components/ImageManager.vue';
import RegionImageryList from '../components/RegionImageryList.vue';
import MapBasemap from '../components/MapBasemap.vue';
import type { AreaBounds, AreaPolygon } from '../utils/api';
import { imageryPhaseLabel } from '../utils/timeline';
const {
  selectedArea,
  selectedGeometry,
  selectedAdministrative,
  administrativeBoundary,
  selectAdministrativeRegion,
  useAdministrativeBoundary,
  selectionEditing,
  setSelectedArea,
  images,
  results,
  inProgress,
  isSubmitting,
  taskProgress,
  taskStageLabel,
  taskError,
  batchMessage,
  overlayMsg,
  submitDetection,
  selectedRegion,
  catalogPhases,
  loadedPhaseIds,
  activeMonth,
  imageryPhases,
  activePhase,
  catalogLoading,
  catalogError,
  selectWorkspaceRegion,
  refreshCatalog,
  addPhase,
  removePhase,
  closePhase,
} = useWorkspace();
const map = ref<InstanceType<typeof MapBasemap> | null>(null);
let rendered = 0;
let active = true;
let pendingFit: [number, number, number, number] | undefined;
function fitPending() {
  if (active && pendingFit && map.value) {
    map.value.flyToBounds(pendingFit, 24);
    pendingFit = undefined;
  }
}
function renderResults() {
  if (!map.value) return;
  if (results.value.length < rendered || !results.value.length) {
    map.value.clearResultOverlays();
    rendered = 0;
    pendingFit = undefined;
  }
  for (const result of results.value.slice(rendered)) {
    map.value.addResultOverlays(result);
    pendingFit = result.maskBounds ?? result.resultBounds;
  }
  rendered = results.value.length;
  fitPending();
}
watch(() => results.value.length, renderResults);
onMounted(async () => {
  await nextTick();
  renderResults();
});
function registerMapAdapter(): void {
  registerAiMapAdapter({
    // AI/UI commands use WGS84 [west, south, east, north]. MapBasemap's
    // imperative Leaflet adapter consumes [south, west, north, east].
    flyToBounds: ([west, south, east, north]) => map.value?.flyToBounds([south, west, north, east]),
    selectRegion: (regionId) => map.value?.selectRegionById(regionId),
  });
}
onMounted(() => {
  registerMapAdapter();
});
onActivated(() => {
  active = true;
  registerMapAdapter();
  void nextTick(fitPending);
});
onDeactivated(() => {
  active = false;
  registerAiMapAdapter(null);
});
onUnmounted(() => {
  registerAiMapAdapter(null);
});
function areaChanged(bounds: AreaBounds | null, geometry: AreaPolygon | null = null) {
  if (inProgress.value) return;
  setSelectedArea(bounds, geometry);
}
function locate(bounds: AreaBounds) {
  map.value?.flyToBounds([bounds.south, bounds.west, bounds.north, bounds.east], 32);
}
function detectLoaded() {
  if (
    selectionEditing.value ||
    catalogLoading.value ||
    !selectedRegion.value ||
    imageryPhases.value.length < 2
  )
    return;
  void submitDetection().catch(() => {});
}
function selectCountyBoundary() {
  void useAdministrativeBoundary().catch((error: unknown) => {
    if (error instanceof DOMException && error.name === 'AbortError') return;
    taskError.value = error instanceof Error ? error.message : '县界加载失败，请重试';
  });
}
</script>
<template>
  <div class="overview-layout">
    <section class="overview-map panel" aria-label="检测区域地图">
      <div class="map-heading"><span class="eyebrow">检测工作区</span></div>
      <div class="phase-tabs" role="tablist" aria-label="影像时相">
        <span v-if="!imageryPhases.length" class="text-muted text-xs"
          >从右栏拖入整月时相，在此切换</span
        >
        <div
          v-for="phase in imageryPhases"
          :key="phase.month"
          class="phase-tab"
          :class="{ 'is-active': activeMonth === phase.month }"
        >
          <button
            role="tab"
            :aria-selected="activeMonth === phase.month"
            @click="activeMonth = phase.month"
          >
            {{ imageryPhaseLabel(phase) }} <span class="text-faint text-xs">{{ phase.scenes.length }} 张</span>
          </button>
          <button
            class="phase-close"
            :aria-label="`关闭时相 ${imageryPhaseLabel(phase)}`"
            :disabled="inProgress"
            @click="closePhase(phase.month)"
          >
            ×
          </button>
        </div>
      </div>
      <MapBasemap
        ref="map"
        workspace-mode
        :active-phase="activePhase"
        :catalog-phases="catalogPhases"
        :selection-bounds="selectedArea"
        :selection-geometry="selectedGeometry"
        :administrative-region="selectedAdministrative"
        :boundary-selection="administrativeBoundary"
        :selection-disabled="inProgress"
        @area="areaChanged"
        @editing="selectionEditing = $event"
        @region="selectWorkspaceRegion"
        @admin-region="selectAdministrativeRegion"
        @use-admin-boundary="selectCountyBoundary"
        @phase-drop="addPhase"
        @phase-stale="refreshCatalog(false)"
      />
    </section>
    <aside class="operations-panel">
      <fieldset :disabled="inProgress || selectionEditing" class="operation-fields">
        <RegionImageryList
          :name="selectedRegion?.properties.name"
          :phases="catalogPhases"
          :loaded-ids="loadedPhaseIds"
          :loading="catalogLoading"
          :error="catalogError"
          :disabled="inProgress"
          @add="addPhase"
          @remove="removePhase"
          @refresh="refreshCatalog()"
        />
        <p v-if="administrativeBoundary" class="text-xs p-2" role="status">检测范围：{{ administrativeBoundary.properties.name }}完整县界（天地图 {{ administrativeBoundary.properties.data_date }} 公开轮廓；有效覆盖以所选影像为准）</p>
        <ImageManager
          :bounds="selectedArea"
          :geometry="selectedGeometry"
          :images="images"
          :show-catalog="false"
          @area="areaChanged"
          @locate="locate"
        />
        <AlgorithmForm />
      </fieldset>
      <button
        class="btn-primary submit-detection"
        :disabled="
          selectionEditing ||
          catalogLoading ||
          inProgress ||
          isSubmitting ||
          !selectedArea ||
          imageryPhases.length < 2
        "
        @click="detectLoaded"
      >
        {{ inProgress ? '正在检测…' : '提交检测任务' }} <span aria-hidden="true">↗</span>
      </button>
      <section v-if="batchMessage || taskError" class="panel task-progress" aria-live="polite">
        <div class="section-heading">
          <b>{{ inProgress ? '检测进度' : taskError ? '检测已停止' : '检测完成' }}</b
          ><span>{{ Math.floor(taskProgress) }}%</span>
        </div>
        <div
          class="detection-progress-track"
          :class="{ 'is-running': inProgress }"
          role="progressbar"
          :aria-valuenow="Math.floor(taskProgress)"
          :aria-valuemin="0"
          :aria-valuemax="100"
          :aria-valuetext="`${taskStageLabel}，${Math.floor(taskProgress)}%`"
          aria-label="检测进度"
        >
          <div class="detection-progress-fill" :style="{ width: `${taskProgress}%` }" />
        </div>
        <p>{{ inProgress ? taskStageLabel : batchMessage }}</p>
        <!-- <p v-if="inProgress" class="text-xs text-muted">
          可切换页面；关闭或刷新浏览器会停止后续组的提交，当前已提交任务继续执行。
        </p> -->
        <p v-if="taskError" class="error-text">{{ taskError }}</p>
      </section>
      <p v-if="overlayMsg" class="warning-text">{{ overlayMsg }}</p>
      <div class="workspace-links">
        <RouterLink to="/comparison" class="btn-secondary">查看对比 →</RouterLink
        ><RouterLink to="/reports" class="btn-secondary">查看报告 →</RouterLink>
      </div>
    </aside>
  </div>
</template>
<style scoped>
.phase-tabs {
  display: flex;
  flex-shrink: 0;
  gap: 8px;
  min-height: 46px;
  align-items: center;
  overflow-x: auto;
  padding: 7px 12px;
  border-bottom: 1px solid #244257;
}
.phase-tab {
  display: flex;
  flex-shrink: 0;
  border: 1px solid #244257;
  border-radius: 6px;
}
.phase-tab.is-active {
  border-color: #22d3ee;
  background: #22d3ee15;
  color: #67e8f9;
}
.phase-tab button {
  padding: 5px 9px;
  font-size: 13px;
}
.phase-tab .phase-close {
  border-left: 1px solid #244257;
}
</style>
