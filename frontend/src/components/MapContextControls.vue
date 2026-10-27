<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import L from 'leaflet';
import { buildGraticule, formatCoordinates } from '../utils/graticule';
import { normalizePoiCollection, resolveBasemapConfig, type PoiPoint } from '../utils/mapContext';

const props = withDefaults(defineProps<{ map: L.Map | null; active?: boolean; interactive?: boolean }>(), {
  active: true, interactive: true,
});
const expanded = ref(false);
const controlsRef = ref<HTMLElement | null>(null);
const basemapEnabled = ref(true);
const gridEnabled = ref(true);
const poiEnabled = ref(true);
const basemapError = ref('');
const poiMessage = ref('放大地图至 11 级后查询视野内 POI');
const poiCount = ref(0);
const poiLoading = ref(false);
const center = ref<[number, number]>([116.5, 39.2]);
const pointer = ref<[number, number] | null>(null);
const coordinates = computed(() => formatCoordinates(...(pointer.value || center.value)));
let boundMap: L.Map | null = null;
let baseLayer: L.TileLayer | null = null;
let gridLayer: L.LayerGroup | null = null;
let poiLayer: L.LayerGroup | null = null;
let ownAttribution: L.Control.Attribution | null = null;
let poiRequest: AbortController | null = null;
let poiTimer: ReturnType<typeof setTimeout> | undefined;
let points: PoiPoint[] = [];
let basemapCycleFailed = false;
let poiAutoPan = false;

function live(): boolean {
  const node = boundMap?.getContainer();
  return props.active && boundMap === props.map && !!node?.isConnected && node.clientWidth > 0 && node.clientHeight > 0;
}

function stopPoiRequest(): void {
  clearTimeout(poiTimer);
  poiTimer = undefined;
  poiRequest?.abort();
  poiRequest = null;
  poiLoading.value = false;
}

function updateCoordinates(event?: L.LeafletMouseEvent): void {
  if (!live() || !boundMap) return;
  if (event) pointer.value = [event.latlng.lng, event.latlng.lat];
  const location = boundMap.getCenter();
  center.value = [location.lng, location.lat];
}

function clearPointer(): void {
  pointer.value = null;
  updateCoordinates();
}

function drawGrid(): void {
  gridLayer?.clearLayers();
  if (!live() || !boundMap || !gridLayer || !gridEnabled.value) return;
  const bounds = boundMap.getBounds();
  const size = boundMap.getSize();
  const grid = buildGraticule({ west: bounds.getWest(), south: bounds.getSouth(), east: bounds.getEast(), north: bounds.getNorth() },
    { width: size.x, height: size.y });
  for (const line of grid.lines) {
    L.polyline(line.points, { pane: 'cdp-graticule', interactive: false, color: '#9bc6d5', weight: 1, opacity: 0.5, dashArray: '3 5' }).addTo(gridLayer);
    const label = document.createElement('span');
    label.textContent = line.label;
    label.dataset.coordinateAxis = line.axis;
    L.marker(line.labelPosition, { pane: 'cdp-graticule', interactive: false, keyboard: false,
      icon: L.divIcon({ className: 'cdp-graticule-label', html: label, iconSize: [100, 18], iconAnchor: [0, 9] }),
    }).addTo(gridLayer);
  }
}

function drawPois(): void {
  poiLayer?.clearLayers();
  if (!live() || !poiLayer || !poiEnabled.value) return;
  for (const point of points) {
    const dot = document.createElement('span');
    dot.className = 'cdp-poi-dot';
    dot.dataset.poiId = point.id;
    const marker = L.marker([point.latitude, point.longitude], {
      pane: 'cdp-poi', interactive: props.interactive, keyboard: props.interactive,
      title: point.name, alt: point.name,
      icon: L.divIcon({ className: 'cdp-poi-marker', html: dot, iconSize: [16, 16], iconAnchor: [8, 8] }),
    });
    if (props.interactive) {
      const content = document.createElement('div');
      const title = document.createElement('strong');
      title.textContent = point.name;
      const category = document.createElement('p');
      category.textContent = point.category;
      const coordinate = document.createElement('p');
      coordinate.textContent = formatCoordinates(point.longitude, point.latitude);
      content.append(title, category, coordinate);
      if (point.address) {
        const address = document.createElement('p');
        address.textContent = point.address;
        content.append(address);
      }
      marker.on('click', () => {
        const popup = marker.getPopup();
        if (!popup) return;
        const panelHeight = controlsRef.value?.offsetHeight || 56;
        popup.options.autoPanPaddingTopLeft = L.point(12, panelHeight + 24);
        popup.options.maxHeight = Math.max(72, (boundMap?.getSize().y || 400) - panelHeight - 110);
      });
      marker.bindPopup(content, { pane: 'cdp-poi-popup', maxWidth: 280, autoPanPaddingBottomRight: [12, 70] });
    }
    marker.addTo(poiLayer);
  }
}

async function loadPois(): Promise<void> {
  stopPoiRequest();
  if (!live() || !boundMap || !poiEnabled.value) return;
  if (boundMap.getZoom() < 11) {
    points = []; drawPois(); poiCount.value = 0;
    poiMessage.value = '放大地图至 11 级后查询视野内 POI';
    return;
  }
  const current = boundMap;
  const bounds = current.getBounds();
  const bbox = [Math.max(-180, bounds.getWest()), Math.max(-85.05112878, bounds.getSouth()),
    Math.min(180, bounds.getEast()), Math.min(85.05112878, bounds.getNorth())];
  if (bbox[0] >= bbox[2] || bbox[1] >= bbox[3]) return;
  const request = new AbortController();
  const timeout = setTimeout(() => request.abort(), 15000);
  poiRequest = request;
  poiLoading.value = true;
  try {
    const query = new URLSearchParams({ bbox: bbox.map(n => n.toFixed(6)).join(','), limit: '500' });
    const response = await fetch(`/py/local/poi?${query}`, { signal: request.signal });
    if (!response.ok) throw new Error(`POI 查询失败（HTTP ${response.status}）`);
    const data = normalizePoiCollection(await response.json());
    if (request.signal.aborted || poiRequest !== request || current !== boundMap || !live()) return;
    const unchanged = JSON.stringify(points) === JSON.stringify(data.points);
    points = data.points;
    poiCount.value = points.length;
    const count = `${points.length} 个地点${data.truncated ? ' · 已达上限，请缩小范围' : ''}`;
    poiMessage.value = data.configured ? [data.warning, count].filter(Boolean).join(' · ') : '尚未接入 POI 地点数据';
    if (!unchanged || poiLayer?.getLayers().length !== points.length) drawPois();
  } catch (error) {
    if (poiRequest !== request || !live()) return;
    points = []; drawPois(); poiCount.value = 0;
    poiMessage.value = request.signal.aborted ? 'POI 查询超时，请稍后重试' : error instanceof Error ? error.message : 'POI 查询失败';
  } finally {
    clearTimeout(timeout);
    if (poiRequest === request) { poiLoading.value = false; poiRequest = null; }
  }
}

function schedulePois(): void {
  stopPoiRequest();
  if (!live() || !poiEnabled.value) return;
  poiTimer = setTimeout(() => { poiTimer = undefined; void loadPois(); }, 250);
}

function onViewChanged(): void {
  if (!live()) return;
  const popupPan = poiAutoPan;
  poiAutoPan = false;
  pointer.value = null;
  updateCoordinates(); drawGrid();
  if (!popupPan) schedulePois();
}

function onPopupAutoPan(): void {
  poiAutoPan = true;
  stopPoiRequest();
}

function tileFailure(): void {
  basemapCycleFailed = true;
  if (live()) basemapError.value = '参考底图暂不可用，请检查网络或配置单位 XYZ 服务；影像与经纬网仍可使用';
}

function tileLoading(): void { basemapCycleFailed = false; }

function tileLoaded(): void {
  if (live() && !basemapCycleFailed) basemapError.value = '';
}

function syncBasemap(): void {
  if (!boundMap) return;
  if (baseLayer) {
    if (basemapEnabled.value && !boundMap.hasLayer(baseLayer)) baseLayer.addTo(boundMap);
    else if (!basemapEnabled.value) baseLayer.remove();
  }
}

function detach(): void {
  stopPoiRequest();
  if (boundMap) {
    boundMap.off('moveend resize', onViewChanged);
    boundMap.off('mousemove', updateCoordinates);
    boundMap.off('mouseout', clearPointer);
    boundMap.off('autopanstart', onPopupAutoPan);
  }
  baseLayer?.off('tileerror', tileFailure).off('loading', tileLoading).off('load', tileLoaded);
  baseLayer?.remove(); baseLayer = null;
  gridLayer?.clearLayers().remove(); gridLayer = null;
  poiLayer?.clearLayers().remove(); poiLayer = null;
  ownAttribution?.remove(); ownAttribution = null;
  boundMap = null;
  pointer.value = null;
  points = [];
  poiAutoPan = false;
}

function attach(map: L.Map): void {
  boundMap = map;
  for (const [name, zIndex] of [['cdp-reference', '150'], ['cdp-graticule', '675'], ['cdp-poi', '680'], ['cdp-poi-popup', '1200']] as const) {
    const pane = map.getPane(name) || map.createPane(name);
    pane.style.zIndex = zIndex;
    pane.style.pointerEvents = name.startsWith('cdp-poi') ? '' : 'none';
  }
  if (!map.attributionControl?.getContainer()?.isConnected) ownAttribution = L.control.attribution({ position: 'bottomright', prefix: false }).addTo(map);
  try {
    const config = resolveBasemapConfig(import.meta.env);
    baseLayer = L.tileLayer(config.url, { ...config.tileOptions, pane: 'cdp-reference' })
      .on('tileerror', tileFailure).on('loading', tileLoading).on('load', tileLoaded);
    syncBasemap();
  } catch (error) {
    basemapError.value = error instanceof Error ? error.message : '参考底图配置无效';
  }
  gridLayer = L.layerGroup().addTo(map);
  poiLayer = L.layerGroup().addTo(map);
  map.on('moveend resize', onViewChanged);
  map.on('mousemove', updateCoordinates);
  map.on('mouseout', clearPointer);
  map.on('autopanstart', onPopupAutoPan);
  onViewChanged();
}

watch(() => props.map, map => { detach(); if (map) attach(map); }, { flush: 'post', immediate: true });
watch(() => props.active, active => {
  if (active) { syncBasemap(); onViewChanged(); }
  else {
    stopPoiRequest(); pointer.value = null; poiAutoPan = false;
    baseLayer?.remove(); gridLayer?.clearLayers(); poiLayer?.clearLayers();
  }
});
watch(basemapEnabled, syncBasemap);
watch(gridEnabled, drawGrid);
watch(poiEnabled, enabled => {
  stopPoiRequest();
  if (enabled) schedulePois();
  else { points = []; drawPois(); poiCount.value = 0; poiMessage.value = 'POI 已关闭'; }
});
watch(() => props.interactive, drawPois);
onBeforeUnmount(detach);
</script>

<template>
  <section ref="controlsRef" class="map-context-controls panel" data-map-context-controls aria-label="参考底图与坐标">
    <div class="context-heading">
      <button class="btn-ghost" :aria-expanded="expanded" @click="expanded = !expanded">地图与坐标 {{ expanded ? '▾' : '▸' }}</button>
      <span class="context-system">WGS84 / Web Mercator</span>
    </div>
    <output class="coordinate-readout" aria-label="地图经纬度">{{ pointer ? '鼠标' : '中心' }} {{ coordinates }}</output>
    <div v-if="expanded" class="context-options">
      <label><input v-model="basemapEnabled" type="checkbox" />参考底图</label>
      <label><input v-model="gridEnabled" type="checkbox" />经纬网</label>
      <label><input v-model="poiEnabled" type="checkbox" />POI 地点</label>
      <p v-if="basemapError" role="status" class="context-warning">{{ basemapError }}</p>
      <p v-if="poiEnabled" role="status" aria-label="POI 状态">{{ poiLoading ? '正在查询视野内 POI…' : poiMessage }}</p>
      <p v-if="poiEnabled && poiCount" class="context-system">仅加载当前视野，最多 500 个地点</p>
    </div>
  </section>
</template>

<style scoped>
.map-context-controls { position: absolute; top: 12px; left: 56px; z-index: 1100; padding: 7px 10px; max-width: calc(100% - 190px); font-size: 11px; pointer-events: auto; }
.context-heading { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 12px; }
.context-heading button { font-weight: 600; }
.coordinate-readout { display: block; color: #cffafe; font-variant-numeric: tabular-nums; white-space: nowrap; margin-top: 3px; }
.context-system { color: #9cb6c6; font-size: 10px; }
.context-options { display: flex; flex-wrap: wrap; gap: 8px 14px; margin-top: 8px; border-top: 1px solid #29404c; padding-top: 8px; max-width: 330px; }
.context-options label { display: flex; align-items: center; gap: 5px; }
.context-options p { flex-basis: 100%; overflow-wrap: anywhere; }
.context-warning { color: #fbbf24; }
@media (max-width: 640px) {
  .map-context-controls { top: 88px; left: 12px; max-width: calc(100% - 24px); padding: 6px 8px; }
  .context-heading .context-system { display: none; }
  .coordinate-readout { white-space: normal; font-size: 10px; }
}
</style>

<style>
.cdp-graticule-label { color: #d5edf3; font-size: 10px; font-variant-numeric: tabular-nums; text-shadow: 0 1px 3px #07121c, 0 0 3px #07121c; pointer-events: none; }
.cdp-poi-marker { background: transparent; border: 0; }
.cdp-poi-dot { display: block; width: 12px; height: 12px; margin: 2px; border: 2px solid #fff; border-radius: 50%; background: #f59e0b; box-shadow: 0 1px 4px #07121c; }
</style>
