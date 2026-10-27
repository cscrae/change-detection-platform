import { computed, inject, provide, reactive, ref, watch, onUnmounted, type InjectionKey } from 'vue';
import { models, settingsState, refreshSettings } from '../utils/settings';
import { useDetection, type ResultOverlayPayload } from './useDetection';
import type { AreaBounds, AreaPolygon, AreaGeometry } from '../utils/api';
import { polygonFromPoints, rectangleEditPoints } from '../utils/selectionGeometry';
import { imageryPhaseLabel } from '../utils/timeline';
import {
  getRegionData,
  type RegionFeature,
  type AdministrativeFeature,
  type RegionScenes,
  type ImageryPhase,
} from '../utils/regions';

export interface AiMapAdapter {
  /** WGS84 wire order: [west, south, east, north]. */
  flyToBounds(bounds: [number, number, number, number]): void;
  selectRegion?(regionId: string): void;
}
let aiMapAdapter: AiMapAdapter | null = null;
export function registerAiMapAdapter(adapter: AiMapAdapter | null): void {
  aiMapAdapter = adapter;
}
export function getAiMapAdapter(): AiMapAdapter | null {
  return aiMapAdapter;
}

export interface DetectionRequest {
  admin_code?: string;
  admin_version?: string;
  region_id?: string;
  phase_dates?: string[];
  phase_ids?: string[];
  scene_ids?: string[];
  date_range?: { start: string; end: string; precision: 'day' | 'month' | 'year' };
  pair_policy?: 'first_last' | 'adjacent' | 'specified';
  aoi?: AreaPolygon;
  model_id?: string;
  detection_settings?: Record<string, number>;
}

function createWorkspace() {
  const detection = useDetection();
  const selectedModel = ref('');
  const detectionSettings = reactive<Record<string, number>>({});
  let algorithmRevision: number | undefined;
  watch(
    [() => settingsState.value?.revision, detection.inProgress],
    () => {
      if (
        detection.inProgress.value ||
        !settingsState.value ||
        algorithmRevision === settingsState.value.revision
      )
        return;
      algorithmRevision = settingsState.value.revision;
      Object.assign(detectionSettings, settingsState.value.settings.detection);
      selectedModel.value = settingsState.value.settings.default_model;
    },
    { immediate: true },
  );
  function getDetectionSelection() {
    if (
      !settingsState.value?.effective ||
      !models.value.some(
        (model) => model.id === selectedModel.value && model.status === 'ready' && model.enabled,
      )
    )
      return;
    return {
      algorithm: 'hebei_onnx',
      model_id: selectedModel.value,
      config_revision: settingsState.value.revision,
      params: { ...detectionSettings },
    };
  }
  const selectedArea = ref<AreaBounds | null>(null);
  const selectedAdministrative = ref<AdministrativeFeature | null>(null);
  const administrativeBoundary = ref<AdministrativeFeature | null>(null);
  let boundaryRequest: AbortController | null = null;
  let boundaryGeneration = 0;
  const selectedGeometry = ref<AreaPolygon | null>(null);
  const selectionEditing = ref(false);
  function setSelectedArea(bounds: AreaBounds | null, geometry: AreaPolygon | null = null) {
    if (detection.inProgress.value) return;
    boundaryGeneration++;
    boundaryRequest?.abort();
    detection.resetDetection();
    administrativeBoundary.value = null;
    selectedArea.value = bounds;
    selectedGeometry.value = bounds && geometry ? JSON.parse(JSON.stringify(geometry)) : null;
  }
  const selectedRegion = ref<RegionFeature | null>(null);
  // A null editor geometry denotes a rectangle, not an absent selection.
  // Preserve its administrative intersection when passing it to detection/AI.
  const detectionAoi = computed<AreaGeometry | null>(() => {
    if (administrativeBoundary.value) return administrativeBoundary.value.geometry;
    if (!selectedArea.value) return null;
    if (selectedGeometry.value) return selectedGeometry.value;
    if (!selectedRegion.value) return null;
    const points = rectangleEditPoints(selectedArea.value, selectedRegion.value.geometry);
    return points ? polygonFromPoints(points) : null;
  });
  const selectionError = computed(() => {
    if (!selectedArea.value) return '请先在地图框选检测区域';
    if (!detectionAoi.value)
      return '框选与行政区相交后包含多个分离区域或孔洞，请缩小框选或绘制单一无孔多边形';
    return '';
  });
  const catalogPhases = ref<ImageryPhase[]>([]);
  async function selectAdministrativeRegion(feature: AdministrativeFeature | null) {
    if (detection.inProgress.value) return;
    if (feature && feature.properties.source_id !== selectedRegion.value?.id) return;
    if (feature?.id !== selectedAdministrative.value?.id) {
      boundaryGeneration++;
      boundaryRequest?.abort();
    }
    selectedAdministrative.value = feature;
  }

  async function useAdministrativeBoundary(code = selectedAdministrative.value?.id, version?: string, isCurrent = () => true) {
    if (!code || detection.inProgress.value) return;
    const generation = ++boundaryGeneration;
    const sourceID = selectedRegion.value?.id;
    const boundaryVersion = version ?? (selectedAdministrative.value?.id === code ? selectedAdministrative.value.properties.boundary_version : undefined);
    boundaryRequest?.abort();
    const request = new AbortController();
    boundaryRequest = request;
    try {
      const feature = await getRegionData<AdministrativeFeature>(`/api/administrative-regions/${encodeURIComponent(code)}${boundaryVersion ? `?version=${encodeURIComponent(boundaryVersion)}` : ''}`, request.signal);
      if (!isCurrent() || generation !== boundaryGeneration || request.signal.aborted || detection.inProgress.value || sourceID !== selectedRegion.value?.id) throw new Error('工作区已变化，请重新选择县界');
      if (feature.properties.level !== 'county' || feature.properties.source_id !== sourceID) throw new Error('县区与当前影像来源不匹配');
      const [west,south,east,north] = feature.properties.bounds;
      setSelectedArea({west,south,east,north});
      administrativeBoundary.value = feature;
    } finally {
      if (boundaryRequest === request) boundaryRequest = null;
    }
  }
  const loadedPhaseIds = ref<string[]>([]);
  const imageryPhases = computed(() =>
    catalogPhases.value.filter(
      (phase) => phase.available && loadedPhaseIds.value.includes(phase.id),
    ),
  );
  const loadedScenes = computed(() =>
    imageryPhases.value.flatMap((phase) => phase.scenes.filter((scene) => scene.available)),
  );
  const activeMonth = ref('');
  const catalogLoading = ref(false);
  const catalogError = ref('');
  let catalogRequest: AbortController | null = null;
  const indexLoading = ref(false);
  const indexError = ref('');
  let indexRequest: AbortController | null = null;
  let indexReady: Promise<void> = Promise.resolve();
  function initializeCatalog(): Promise<void> {
    if (indexLoading.value) return indexReady;
    indexLoading.value = true;
    indexError.value = '';
    const request = new AbortController();
    indexRequest = request;
    indexReady = getRegionData<unknown>('/py/local/catalog?refresh=true', request.signal)
      .then(() => {})
      .catch((error) => {
        if (!request.signal.aborted)
          indexError.value = error instanceof Error ? error.message : '影像目录初始化失败';
      })
      .finally(() => {
        indexLoading.value = false;
      });
    return indexReady;
  }
  async function retryIndex() {
    await initializeCatalog();
    if (!indexError.value) await refreshCatalog(false);
  }
  // App provides one workspace per document; SPA navigation reuses this promise.
  void initializeCatalog();
  onUnmounted(() => {
    boundaryRequest?.abort();
    catalogRequest?.abort();
    indexRequest?.abort();
  });
  const activePhase = computed(() =>
    imageryPhases.value.find((phase) => phase.month === activeMonth.value),
  );
  async function selectWorkspaceRegion(region: RegionFeature | null, isCurrent = () => true, signal?: AbortSignal) {
    if (!isCurrent() || signal?.aborted) return;
    if (detection.inProgress.value || selectedRegion.value?.id === region?.id) return;
    catalogRequest?.abort();
    selectedRegion.value = region;
    void selectAdministrativeRegion(null);
    setSelectedArea(null);
    loadedPhaseIds.value = [];
    activeMonth.value = '';
    catalogPhases.value = [];
    catalogError.value = '';
    catalogLoading.value = false;
    detection.resetDetection();
    if (!region) return;
    await refreshCatalog(false, isCurrent, signal);
  }
  async function refreshCatalog(force = true, isCurrent = () => true, signal?: AbortSignal) {
    if (!isCurrent() || signal?.aborted) return;
    const region = selectedRegion.value;
    if (!region || detection.inProgress.value) return;
    catalogRequest?.abort();
    const request = new AbortController();
    const abort = () => request.abort();
    signal?.addEventListener('abort', abort, { once: true });
    catalogRequest = request;
    catalogLoading.value = true;
    catalogError.value = '';
    try {
      await indexReady;
      if (request.signal.aborted || !isCurrent()) return;
      if (indexError.value && !force) throw new Error(indexError.value);
      const data = await getRegionData<RegionScenes>(
        `/py/local/regions/${encodeURIComponent(region.id)}/scenes${force ? '?refresh=true' : ''}`,
        request.signal,
      );
      if (!request.signal.aborted && isCurrent() && selectedRegion.value?.id === region.id) {
        indexError.value = '';
        const previous = imageryPhases.value
          .map((phase) => `${phase.id}:${phase.version}`)
          .join('|');
        catalogPhases.value = data.phases;
        loadedPhaseIds.value = imageryPhases.value.map((phase) => phase.id);
        if (
          imageryPhases.value.map((phase) => `${phase.id}:${phase.version}`).join('|') !== previous
        )
          detection.resetDetection();
        if (!imageryPhases.value.some((phase) => phase.month === activeMonth.value))
          activeMonth.value = imageryPhases.value[0]?.month || '';
      }
    } catch (error) {
      if (!request.signal.aborted && isCurrent())
        catalogError.value = error instanceof Error ? error.message : '影像列表加载失败';
    } finally {
      signal?.removeEventListener('abort', abort);
      if (catalogRequest === request) catalogLoading.value = false;
    }
  }
  function addPhase(phaseId: string) {
    if (detection.inProgress.value) return;
    const phase = catalogPhases.value.find((item) => item.id === phaseId);
    if (!phase?.available || phase.region_id !== selectedRegion.value?.id) return;
    if (!loadedPhaseIds.value.includes(phaseId)) {
      loadedPhaseIds.value = [...loadedPhaseIds.value, phaseId];
      detection.resetDetection();
    }
    activeMonth.value = phase.month;
  }
  function removePhase(phaseId: string) {
    if (detection.inProgress.value) return;
    loadedPhaseIds.value = loadedPhaseIds.value.filter((id) => id !== phaseId);
    detection.resetDetection();
    if (!imageryPhases.value.some((phase) => phase.month === activeMonth.value))
      activeMonth.value = imageryPhases.value[0]?.month || '';
  }
  function closePhase(month: string) {
    if (detection.inProgress.value) return;
    const phase = imageryPhases.value.find((item) => item.month === month);
    if (phase) removePhase(phase.id);
  }
  // Both the page button and the typed AI command submit through this boundary.
  async function submitDetection(request: DetectionRequest = {}, isCurrent = () => true) {
    const fail = (message: string): never => {
      detection.taskError.value = message;
      throw new Error(message);
    };
    if (detection.inProgress.value || selectionEditing.value || catalogLoading.value)
      fail('工作区正在编辑、加载或检测，请稍后再试');
    const region = selectedRegion.value;
    if (!region || (request.region_id && request.region_id !== region.id))
      return fail('请先选择本次检测的影像来源');
    if (request.admin_code) await useAdministrativeBoundary(request.admin_code, request.admin_version, isCurrent);
    if (!isCurrent() || selectedRegion.value?.id !== region.id) return fail('工作区已变化，请重新选择检测区域');
    const aoi = detectionAoi.value;
    if (!aoi) return fail(selectionError.value);
    if (request.aoi && JSON.stringify(request.aoi) !== JSON.stringify(aoi))
      fail('本次指定范围与当前框选不同，请先确认检测区域');
    const available = catalogPhases.value.filter(
      (phase) => phase.available && phase.region_id === region.id,
    );
    let phases = imageryPhases.value;
    if (
      request.scene_ids?.some(
        (id) =>
          !available.some((phase) =>
            phase.scenes.some((scene) => scene.id === id && scene.available),
          ),
      )
    )
      fail('指定影像不在当前区域目录中');
    if (request.phase_dates?.length) {
      phases = request.phase_dates.map((date) => {
        const matches = available.filter((phase) =>
          date.length === 7
            ? phase.month === date
            : phase.scenes.every((scene) => scene.date_precision === 'day' && scene.date === date),
        );
        if (matches.length !== 1)
          fail(`时相 ${date} 不存在或日期精度不匹配；当前目录可用时相：${available.map(imageryPhaseLabel).join('、') || '无'}`);
        return matches[0];
      });
    } else if (request.phase_ids?.length) {
      phases = request.phase_ids.map(
        (id) => available.find((phase) => phase.id === id) || fail('指定时相不在当前区域目录中'),
      );
    } else if (request.scene_ids?.length) {
      phases = available.filter((phase) =>
        phase.scenes.some((scene) => request.scene_ids!.includes(scene.id)),
      );
    } else if (request.date_range) {
      const { start, end, precision } = request.date_range;
      if (precision === 'day') fail('当前按月时相检测不支持按日范围，请明确月份');
      phases = available.filter(
        (phase) =>
          phase.month.slice(0, start.length) >= start && phase.month.slice(0, end.length) <= end,
      );
    }
    phases = [...new Map(phases.map((phase) => [phase.id, phase])).values()].sort((a, b) =>
      a.month.localeCompare(b.month),
    );
    if (
      (request.pair_policy === 'first_last' || (request.date_range && !request.pair_policy)) &&
      phases.length > 2
    )
      phases = [phases[0], phases[phases.length - 1]];
    if (phases.length < 2) fail('至少需要两个不同月份的可用时相');
    if (
      request.scene_ids?.some(
        (id) => !phases.some((phase) => phase.scenes.some((scene) => scene.id === id)),
      )
    )
      fail('指定影像不属于本次选择的时相');
    if (
      request.date_range &&
      phases.some(
        (phase) =>
          phase.month.slice(0, request.date_range!.start.length) < request.date_range!.start ||
          phase.month.slice(0, request.date_range!.end.length) > request.date_range!.end,
      )
    )
      fail('指定时相与日期范围不一致');
    if (
      request.phase_dates &&
      request.phase_ids &&
      (phases.length !== request.phase_ids.length ||
        phases.some((phase) => !request.phase_ids!.includes(phase.id)))
    )
      fail('指定月份和时相不一致');
    if (
      request.date_range &&
      request.phase_dates?.some(
        (date) => date < request.date_range!.start || date > request.date_range!.end,
      )
    )
      fail('指定月份与日期范围不一致');
    const ids = phases.map((phase) => phase.id);
    const scenes = phases.flatMap((phase) =>
      phase.scenes.filter(
        (scene) => scene.available && (!request.scene_ids || request.scene_ids.includes(scene.id)),
      ),
    );
    if (
      phases.some(
        (phase) => !scenes.some((scene) => phase.scenes.some((item) => item.id === scene.id)),
      )
    )
      fail('每个指定时相都必须包含可用影像');
    if (
      request.model_id &&
      !models.value.some(
        (model) => model.id === request.model_id && model.status === 'ready' && model.enabled,
      )
    )
      fail('指定模型尚未就绪');
    const overrides = request.detection_settings || {};
    for (const [key, value] of Object.entries(overrides)) {
      if (
        !Number.isFinite(value) ||
        !['threshold', 'resolution', 'kernel_size', 'min_area_pixels'].includes(key) ||
        (key === 'threshold' && (value < 0 || value > 1)) ||
        (key === 'resolution' && value <= 0) ||
        (key === 'kernel_size' && (!Number.isInteger(value) || value < 1 || value > 99)) ||
        (key === 'min_area_pixels' && (!Number.isInteger(value) || value < 0))
      )
        fail('检测参数无效');
    }
    if (!settingsState.value?.effective || (!request.model_id && !getDetectionSelection()))
      fail('所选模型或配置尚未就绪');
    if (request.model_id) selectedModel.value = request.model_id;
    Object.assign(detectionSettings, overrides);
    if (JSON.stringify(ids) !== JSON.stringify(loadedPhaseIds.value)) {
      detection.resetDetection();
      loadedPhaseIds.value = ids;
      if (!phases.some((phase) => phase.month === activeMonth.value))
        activeMonth.value = phases[0].month;
    }
    await detection.submitTask(
      getDetectionSelection,
      selectedArea.value,
      {
        region_id: region.id,
        admin_code: administrativeBoundary.value?.id,
        admin_version: administrativeBoundary.value?.properties.boundary_version,
        scene_ids: scenes.map((scene) => scene.id),
        aoi,
      },
      isCurrent,
    );
    if (!isCurrent()) throw new DOMException('Session changed', 'AbortError');
    if (!detection.currentTaskId.value || detection.taskStatus.value === 'failed')
      fail(detection.taskError.value || '检测未成功提交');
    return { task_id: detection.currentTaskId.value, pair_count: detection.pairs.value.length };
  }
  const selectedPairKey = ref('');
  const results = ref<ResultOverlayPayload[]>([]);
  const selectedPair = computed(() =>
    detection.pairs.value.find((pair) => pair.key === selectedPairKey.value),
  );
  detection.onResult((result) => {
    results.value.push(result);
  });
  watch(
    () => detection.pairs.value,
    () => {
      results.value = [];
      selectedPairKey.value = '';
    },
    { flush: 'sync' },
  );
  watch(
    () => detection.pairs.value.map((pair) => `${pair.key}:${pair.status}`).join('|'),
    () => {
      if (!selectedPairKey.value) {
        selectedPairKey.value =
          detection.pairs.value.find((pair) => pair.status === 'succeeded')?.key || '';
      }
    },
  );
  let observedSources = '';
  let pendingRevision = false;
  watch(
    () => JSON.stringify(settingsState.value?.settings.sources),
    (sources) => {
      if (!sources) return;
      if (observedSources && observedSources !== sources) pendingRevision = true;
      observedSources = sources;
      if (pendingRevision && !detection.inProgress.value) void updateConfiguration();
    },
  );
  watch(detection.inProgress, (running) => {
    if (!running && pendingRevision) void updateConfiguration();
  });
  async function updateConfiguration() {
    pendingRevision = false;
    setSelectedArea(null);
    loadedPhaseIds.value = [];
    activeMonth.value = '';
    await retryIndex();
  }
  const refreshOnFocus = () => {
    void refreshSettings().catch(() => {});
  };
  const warnOnClose = (event: BeforeUnloadEvent) => {
    if (detection.inProgress.value) {
      event.preventDefault();
      event.returnValue = '';
    }
  };
  window.addEventListener('focus', refreshOnFocus);
  window.addEventListener('beforeunload', warnOnClose);
  onUnmounted(() => {
    window.removeEventListener('focus', refreshOnFocus);
    window.removeEventListener('beforeunload', warnOnClose);
  });
  return {
    ...detection,
    selectedModel,
    detectionSettings,
    getDetectionSelection,
    submitDetection,
    selectedArea,
    selectedGeometry,
    selectedAdministrative,
    administrativeBoundary,
    selectAdministrativeRegion,
    useAdministrativeBoundary,
    detectionAoi,
    selectionError,
    selectionEditing,
    setSelectedArea,
    selectedPairKey,
    selectedPair,
    results,
    selectedRegion,
    catalogPhases,
    loadedPhaseIds,
    loadedScenes,
    activeMonth,
    imageryPhases,
    activePhase,
    indexLoading,
    indexError,
    retryIndex,
    catalogLoading,
    catalogError,
    selectWorkspaceRegion,
    refreshCatalog,
    addPhase,
    removePhase,
    closePhase,
  };
}

type Workspace = ReturnType<typeof createWorkspace>;
const workspaceKey: InjectionKey<Workspace> = Symbol('detection-workspace');
export function provideWorkspace() {
  const workspace = createWorkspace();
  provide(workspaceKey, workspace);
  return workspace;
}
export function useWorkspace() {
  const workspace = inject(workspaceKey);
  if (!workspace) throw new Error('Detection workspace is unavailable');
  return workspace;
}
