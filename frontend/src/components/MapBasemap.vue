<script setup lang="ts">
import {
  ref,
  computed,
  watch,
  nextTick,
  onMounted,
  onUnmounted,
  onActivated,
  onDeactivated,
  shallowRef,
} from 'vue';
import { settingsState } from '../utils/settings';
import { PhaseLayerCache } from '../utils/phaseLayerCache';
import L from 'leaflet';
import MapContextControls from './MapContextControls.vue';
import 'leaflet/dist/leaflet.css';
import { LOCAL_MAP_OPTIONS, addCoverage } from '../utils/basemap';
import {
  displaySourceLayer,
  getRegionData,
  type RegionCollection,
  type RegionFeature,
  type RegionImagery,
  type ImageryPhase,
  type AdministrativeFeature,
} from '../utils/regions';
import { containsProvincePoint, provincePolygons } from '../utils/province';
import {
  getImageOverlay,
  type AreaBounds,
  type AreaPolygon,
  type AreaGeometry,
  type ChangePatch,
  type DetectionStats,
  type ImageInfo,
} from '../utils/api';
import {
  constrainToRegion,
  regionCoversPoint,
  polygonFromPoints,
  polygonBounds,
  polygonError,
  samePoint,
  rectanglePoints,
  rectangleEditPoints,
  moveParallelEdge,
  type Position,
} from '../utils/selectionGeometry';
import { colorText, newestImagesFirst, newestResultsFirst } from '../utils/mapLayers';
import { imageryPhaseLabel, phaseDate, sourceName, type TimelinePair } from '../utils/timeline';
import { RegionNavigation } from '../utils/regionNavigation';
import type { ResultOverlayPayload } from '../composables/useDetection';

const props = defineProps<{
  selectionBounds?: AreaBounds | null;
  selectionGeometry?: AreaPolygon | null;
  images?: ImageInfo[];
  selectionDisabled?: boolean;
  workspaceMode?: boolean;
  activePhase?: ImageryPhase;
  catalogPhases?: ImageryPhase[];
  administrativeRegion?: AdministrativeFeature | null;
  boundarySelection?: AdministrativeFeature | null;
}>();
const emit = defineEmits<{
  (e: 'area', bounds: AreaBounds | null, geometry?: AreaPolygon | null): void;
  (e: 'editing', editing: boolean): void;
  (e: 'region', region: RegionFeature | null): void;
  (e: 'phase-drop', id: string): void;
  (e: 'phase-stale'): void;
  (e: 'admin-region', region: AdministrativeFeature | null): void;
  (e: 'use-admin-boundary'): void;
}>();

const mapRef = ref<HTMLDivElement | null>(null);
let map: L.Map | null = null;
const contextMap = shallowRef<L.Map | null>(null);
const contextActive = ref(true);
let currentTile: L.LayerGroup | null = null;
let phaseCache: PhaseLayerCache | null = null;
const boundaryError = ref('');
const imageryError = ref('');
const layersExpanded = ref(false);
const baseMapsVisible = ref(true);
const navigationVisible = ref(false);
const regions = ref<RegionFeature[]>([]);
const activeRegion = ref<RegionFeature | null>(null);
const countyRegions = ref<AdministrativeFeature[]>([]);
const countyBoundaryError = ref('');
let countyBoundaryRequest: AbortController | null = null;
let countyLayer: L.GeoJSON | null = null;
let countyLabels: L.LayerGroup | null = null;
const countyLabelEntries = new Map<string, { marker: L.Marker; leader: L.Polyline; feature: AdministrativeFeature }>();
function drawCounties(): void {
  countyLayer?.remove(); countyLayer = null;
  countyLabels?.remove(); countyLabels = null;
  countyLabelEntries.clear();
  if (!map || !activeRegion.value) return;
  const currentMap = map;
  countyLabels = L.layerGroup().addTo(map);
  countyLayer = L.geoJSON({type:'FeatureCollection', features:countyRegions.value} as GeoJSON.GeoJsonObject, {
    pane: 'cdp-counties',
    style: f => ({color: f?.id === props.administrativeRegion?.id ? '#22d3ee' : '#8bb3c9', weight: f?.id === props.administrativeRegion?.id ? 2.5 : 1, fillOpacity: 0.02}),
    onEachFeature: (raw, layer) => {
      const feature = raw as unknown as AdministrativeFeature;
      layer.on('click', () => chooseCounty(feature));
      const label = document.createElement('span'); label.textContent = feature.properties.name;
      const marker = L.marker([feature.properties.label[1],feature.properties.label[0]], {
        pane:'cdp-region-labels', interactive:false, keyboard:false,
        icon:L.divIcon({className:'region-label county-label', html:label, iconSize:[120,22],iconAnchor:[60,11]}),
      }).addTo(countyLabels!);
      const leader = L.polyline([], {pane:'cdp-region-labels', interactive:false, color:'#94b8ce', weight:1, opacity:0.6}).addTo(countyLabels!);
      countyLabelEntries.set(feature.id, {marker,leader,feature});
    },
  }).addTo(currentMap);
  countyLayer.eachLayer(layer => {
    const feature = (layer as L.GeoJSON).feature as GeoJSON.Feature | undefined;
    const element = (layer as L.Path).getElement?.();
    element?.setAttribute('data-admin-code', String(feature?.id || ''));
    element?.setAttribute('aria-label', String(feature?.properties?.name || ''));
  });
  arrangeRegionLabels();
}
async function loadCounties(): Promise<void> {
  countyBoundaryRequest?.abort();
  countyRegions.value = []; countyBoundaryError.value = ''; drawCounties();
  const source = activeRegion.value?.id; if (!source || !map) return;
  const request = new AbortController(); countyBoundaryRequest = request;
  try {
    const data = await getRegionData<{features:AdministrativeFeature[]}>(`/api/administrative-regions?source_id=${encodeURIComponent(source)}`, request.signal);
    if (request.signal.aborted || activeRegion.value?.id !== source || !map) return;
    countyRegions.value = data.features; drawCounties();
  } catch (error) { if (!request.signal.aborted) countyBoundaryError.value = error instanceof Error ? error.message : '县级边界加载失败'; }
}
const COUNTY_FOCUS_ZOOM = 12;
function focusCounty(feature: AdministrativeFeature): void {
  if (!map || map.getZoom() >= COUNTY_FOCUS_ZOOM) return;
  const [w, s, e, n] = feature.properties.bounds;
  const bounds = L.latLngBounds([[s, w], [n, e]]);
  const zoom = Math.min(COUNTY_FOCUS_ZOOM, map.getBoundsZoom(bounds, false, L.point(64, 64)));
  // Selecting a larger county must not undo the user's zoom.
  if (zoom > map.getZoom()) animateToBounds(bounds, { padding: [32, 32], maxZoom: COUNTY_FOCUS_ZOOM });
}
function chooseCounty(feature: AdministrativeFeature | null): void {
  if (!map || props.selectionDisabled || drawMode.value) return;
  if (feature?.id !== props.administrativeRegion?.id) emit('admin-region', feature);
  if (feature) focusCounty(feature);
  else if (cityExtent) animateToBounds(cityExtent, { padding: [32, 32], maxZoom: 12 });
}
watch(() => props.administrativeRegion, feature => {
  drawCounties();
  if (feature && activeRegion.value?.id === feature.properties.source_id) focusCounty(feature);
});
watch(() => props.boundarySelection, () => { showSelection(props.selectionBounds); syncSelectionEditor(); });
const regionYears = ref<number[]>([]);
const regionYear = ref<number | null>(null);
const isOverview = ref(true);
let regionRequest: AbortController | null = null;
let navigationRequest: AbortController | null = null;
let regionMask: L.Polygon | null = null;
let provinceExtent: L.LatLngBounds | null = null;
let overviewZoom = 6;
let navigationAnimating = false;
let cityExtent: L.LatLngBounds | null = null;
let cityFitZoom = 6;
let cityExitZoom = 5;
const NAVIGATION_DURATION = 0.4;
let navigationTarget: { bounds: L.LatLngBounds; options: L.FitBoundsOptions } | null = null;
let navigationEnd: (() => void) | null = null;
let regionNavigation: RegionNavigation | null = null;
const dragOver = ref(false);
const regionPaths = new Map<string, L.Path>();
const regionLabels = new Map<
  string,
  { marker: L.Marker; leader: L.Polyline; feature: RegionFeature }
>();
const regionStyle = (id: string): L.PathOptions => ({
  color: activeRegion.value?.id === id ? '#22d3ee' : '#7197ac',
  weight: activeRegion.value?.id === id ? 2.5 : 1.2,
  fillColor: '#193b51',
  fillOpacity: isOverview.value ? 0.45 : 0,
});

function hasVisibleMap(): boolean {
  const container = map?.getContainer();
  return !!container?.isConnected && container.clientWidth > 0 && container.clientHeight > 0;
}

function setNavigationMinimum(zoom: number): void {
  if (!map) return;
  if (map.getZoom() < zoom) map.setZoom(zoom, { animate: false });
  map.setMinZoom(zoom);
}

function updateOverviewZoom(): void {
  if (!map || !provinceExtent || !hasVisibleMap()) return;
  const interrupted = navigationAnimating;
  cancelNavigation();
  // Changing a viewport or lock may raise the minimum; its zoomend is not a
  // user request to leave the city. Recompute without the previous minimum.
  map.setMinZoom(0);
  overviewZoom = map.getBoundsZoom(provinceExtent, false, L.point(48, 48));
  if (isOverview.value) {
    map.setMaxBounds(L.latLngBounds([]));
    map.fitBounds(provinceExtent, { padding: [24, 24], animate: false });
    map.setMaxBounds(provinceExtent);
    map.setMinZoom(overviewZoom);
  } else if (cityExtent) {
    cityFitZoom = Math.min(12, map.getBoundsZoom(cityExtent, false, L.point(64, 64)));
    cityExitZoom = Math.max(0, cityFitZoom - 1);
    setNavigationMinimum(
      props.selectionDisabled
        ? cityExitZoom + (map.options.zoomSnap || 0.25)
        : Math.min(overviewZoom, cityExitZoom),
    );
    if (interrupted) map.fitBounds(cityExtent, { padding: [32, 32], maxZoom: 12, animate: false });
  } else map.setMinZoom(overviewZoom);
  finishNavigation();
}

function cancelNavigation(): void {
  if (navigationEnd) map?.off('moveend', navigationEnd);
  navigationEnd = null;
  const wasFlying = navigationTarget !== null;
  navigationTarget = null;
  navigationAnimating = true;
  regionNavigation?.suspend(true);
  // stop() snaps a fractional fly zoom with a second asynchronous animation.
  // Reset synchronously so that stale zoom cannot overwrite the next target.
  if (map && wasFlying) map.setView(map.getCenter(), map.getZoom(), { animate: false });
  else map?.stop();
}

function finishNavigation(): void {
  if (navigationEnd) map?.off('moveend', navigationEnd);
  navigationEnd = null;
  // Correct while exit detection is still suppressed (including an interrupted fly).
  regionNavigation?.suspend(false);
  regionNavigation?.correct();
  navigationAnimating = false;
  if (navigationTarget && isOverview.value && provinceExtent) map?.setMaxBounds(provinceExtent);
  navigationTarget = null;
  arrangeRegionLabels();
  showRegionLabels();
}

function hideRegionLabels(): void {
  const pane = map?.getPane('cdp-region-labels');
  if (!pane) return;
  pane.style.transition = 'none';
  pane.style.opacity = '0';
}

function showRegionLabels(): void {
  const pane = map?.getPane('cdp-region-labels');
  if (!pane || navigationAnimating) return;
  // Commit the hidden state even when a short or interrupted flight finishes
  // in the same frame; fade the entire pane, including county leader lines.
  void pane.offsetWidth;
  pane.style.transition = 'opacity 0.2s ease';
  pane.style.opacity = '1';
}

function animateToBounds(bounds: L.LatLngBounds, options: L.FitBoundsOptions = {}): void {
  if (!map) return;
  if (navigationTarget?.bounds.equals(bounds) &&
      JSON.stringify(navigationTarget.options) === JSON.stringify(options)) return;
  cancelNavigation();
  hideRegionLabels();
  navigationTarget = { bounds, options };
  const started = Date.now();
  // A flight can start inside a previous zoomend (automatic province exit).
  // That previous moveend must not complete the newly started flight.
  navigationEnd = () => {
    if (Date.now() - started < NAVIGATION_DURATION * 1000) return;
    finishNavigation();
  };
  map.on('moveend', navigationEnd);
  try {
    if (!L.Browser.any3d) {
      map.fitBounds(bounds, { ...options, animate: false });
      finishNavigation();
      return;
    }
    map.flyToBounds(bounds, { ...options, duration: NAVIGATION_DURATION });
  } catch {
    finishNavigation();
  }
}

function arrangeRegionLabels(): void {
  if (!map) return;
  const container = map.getContainer(), extent = container.getBoundingClientRect();
  const controlsRoot = container.closest('[data-map-workspace]') || container;
  const controls = [toolbarRef.value, layersRef.value, ...controlsRoot.querySelectorAll<HTMLElement>('.leaflet-control, [data-map-context-controls]')]
    .filter((node): node is HTMLElement => !!node && node.isConnected)
    .map(node => node.getBoundingClientRect());
  const occupied: { x: number; y: number; width: number }[] = [];
  for (const entry of regionLabels.values()) entry.marker.setOpacity(isOverview.value ? 1 : 0);
  const entries = isOverview.value ? regionLabels.values() : countyLabelEntries.values();
  if (!isOverview.value) for (const entry of regionLabels.values()) entry.leader.setLatLngs([]);
  for (const { marker, leader, feature } of entries) {
    marker.setOpacity(1);
    const [lng, lat] = feature.properties.label;
    const original = L.latLng(lat, lng);
    const point = map.latLngToContainerPoint(original);
    if (point.x < 0 || point.y < 0 || point.x > extent.width || point.y > extent.height) {
      marker.setOpacity(0); leader.setLatLngs([]); continue;
    }
    const width = feature.properties.name.length * 12 + 8;
    let target = point;
    const offsets = [
      [0, 0],
      [0, -24],
      [0, 24],
      [32, -24],
      [-32, -24],
      [32, 24],
      [-32, 24],
      [64, 0],
      [-64, 0],
      [0, -48],
      [0, 48],
      [64, -48],
      [-64, 48],
    ];
    for (let radius = 72; radius <= 240; radius += 24) {
      for (let delta = -radius; delta <= radius; delta += 24) {
        offsets.push([delta,-radius],[delta,radius],[-radius,delta],[radius,delta]);
      }
    }
    for (const [dx, dy] of offsets) {
      const candidate = L.point(point.x + dx, point.y + dy);
      if (candidate.x < width/2+8 || candidate.x > extent.width-width/2-8 || candidate.y < 14 || candidate.y > extent.height-14) continue;
      if (controls.some(rect => candidate.x + width/2 + 4 > rect.left-extent.left && candidate.x-width/2-4 < rect.right-extent.left && candidate.y+14 > rect.top-extent.top && candidate.y-14 < rect.bottom-extent.top)) continue;
      if (
        occupied.every(
          (box) =>
            Math.abs(box.x - candidate.x) > (box.width + width) / 2 + 4 ||
            Math.abs(box.y - candidate.y) > 22,
        )
      ) {
        target = candidate;
        break;
      }
    }
    occupied.push({ x: target.x, y: target.y, width });
    const location = map.containerPointToLatLng(target);
    marker.setLatLng(location);
    leader.setLatLngs(target.equals(point) ? [] : [original, location]);
  }
}

function clearRegionNavigation(): void {
  countyBoundaryRequest?.abort();
  countyLayer?.remove(); countyLayer = null;
  countyLabels?.remove(); countyLabels = null;
  countyRegions.value = [];
  countyLabelEntries.clear();
  cancelNavigation();
  regionNavigation?.setRegion(null);
  cityExtent = null;
  map?.setMaxBounds(L.latLngBounds([]));
  phaseCache?.clear();
  regionRequest?.abort();
  regionRequest = null;
  activeRegion.value = null;
  regionYears.value = [];
  regionYear.value = null;
  imageryError.value = '';
  currentTile?.remove();
  currentTile = null;
  regionMask?.remove();
  regionMask = null;
  navigationVisible.value = false;
  finishNavigation();
}

function returnToProvince(fit = true, notify = true): void {
  clearRegionNavigation();
  isOverview.value = true;
  for (const entry of imageEntries.value) {
    entry.visible = false;
    toggleImage(entry);
  }
  for (const entry of resultEntries.value) {
    entry.visible = false;
    toggleResult(entry);
  }
  resultRect?.remove();
  resultAnchor.value = null;
  selectedPatch.value = null;
  setDrawMode(false);
  showSelection(null);
  regionPaths.forEach((layer, id) => layer.setStyle(regionStyle(id)));
  if (fit && map && provinceExtent && hasVisibleMap()) {
    map.setMinZoom(0);
    overviewZoom = map.getBoundsZoom(provinceExtent, false, L.point(48, 48));
    map.setMinZoom(overviewZoom);
    map.setMaxBounds(L.latLngBounds([]));
    animateToBounds(provinceExtent, { padding: [24, 24] });
  }
  if (notify) emit('area', null);
  if (notify && props.workspaceMode) emit('region', null);
  arrangeRegionLabels();
}

function loadActivePhase(): void {
  navigationVisible.value = false;
  imageryError.value = '';
  const phase = props.activePhase;
  if (!props.workspaceMode || !map) return;
  const selected = phase?.region_id === activeRegion.value?.id ? phase : undefined;
  if (!baseMapsVisible.value || !hasVisibleMap()) phaseCache?.suspend();
  phaseCache?.activate(selected);
  navigationVisible.value = !!selected;
  toggleNavigation();
}
watch(() => props.activePhase, loadActivePhase);
watch(
  () => props.catalogPhases,
  (phases) => phaseCache?.reconcile(phases || []),
  { flush: 'sync' },
);
function acceptDrag(event: DragEvent): void {
  if (
    !props.workspaceMode ||
    props.selectionDisabled ||
    !event.dataTransfer?.types.includes('application/x-cdp-phase')
  )
    return;
  event.preventDefault();
  event.dataTransfer.dropEffect = 'copy';
  dragOver.value = true;
}
function dropScene(event: DragEvent): void {
  dragOver.value = false;
  if (!props.workspaceMode || props.selectionDisabled) return;
  const id = event.dataTransfer?.getData('application/x-cdp-phase');
  if (!id) return;
  event.preventDefault();
  setDrawMode(false);
  emit('phase-drop', id);
}

async function loadRegionImagery(year?: number): Promise<void> {
  const selected = activeRegion.value;
  if (!selected || !map) return;
  regionRequest?.abort();
  const request = new AbortController();
  regionRequest = request;
  currentTile?.remove();
  currentTile = null;
  navigationVisible.value = false;
  imageryError.value = '';
  try {
    const url = `/py/local/regions/${encodeURIComponent(selected.id)}/imagery${year == null ? '' : `?year=${year}`}`;
    const data = await getRegionData<RegionImagery>(url, request.signal);
    if (request.signal.aborted || !map || activeRegion.value?.id !== selected.id) return;
    regionYears.value = data.years;
    regionYear.value = data.selected_year;
    if (!data.sources.length) return;
    currentTile = L.layerGroup(
      data.sources.map((source) => {
        const layer = displaySourceLayer(source);
        layer.on('tileerror', () => {
          if (regionRequest === request)
            imageryError.value = '部分影像瓦片加载失败，请检查数据服务';
        });
        return layer;
      }),
    );
    navigationVisible.value = true;
    toggleNavigation();
  } catch (error) {
    if (!request.signal.aborted)
      imageryError.value = error instanceof Error ? error.message : '区域影像加载失败';
  }
}

function selectRegion(feature?: RegionFeature, fitCity = true, resetArea = true): void {
  if (
    !feature ||
    !map ||
    drawMode.value ||
    (resetArea && props.selectionDisabled) ||
    activeRegion.value?.id === feature.id
  )
    return;
  if (resetArea) {
    returnToProvince(false, false);
    emit('area', null);
  } else clearRegionNavigation();
  activeRegion.value = feature;
  isOverview.value = false;
  regionPaths.forEach((layer, id) => layer.setStyle(regionStyle(id)));
  const rings: L.LatLngExpression[][] = [
    [
      [-85, -180],
      [-85, 180],
      [85, 180],
      [85, -180],
    ],
  ];
  for (const polygon of provincePolygons(feature.geometry))
    for (const ring of polygon) rings.push(ring.map(([lng, lat]) => [lat, lng] as L.LatLngTuple));
  regionMask = L.polygon(rings, {
    pane: 'province-mask',
    stroke: false,
    fillColor: '#152330',
    fillOpacity: 1,
    fillRule: 'evenodd',
    interactive: false,
  }).addTo(map);
  const extent = L.geoJSON(feature as GeoJSON.GeoJsonObject).getBounds();
  cityExtent = extent;
  regionNavigation?.setRegion(feature.geometry);
  // Compute the same entry scale even when an image/result supplies the target.
  cancelNavigation();
  map.setMinZoom(0);
  cityFitZoom = Math.min(12, map.getBoundsZoom(extent, false, L.point(64, 64)));
  cityExitZoom = Math.max(0, cityFitZoom - 1);
  setNavigationMinimum(
    props.selectionDisabled
      ? cityExitZoom + (map.options.zoomSnap || 0.25)
      : Math.min(overviewZoom, cityExitZoom),
  );
  if (fitCity) {
    animateToBounds(extent, { padding: [32, 32], maxZoom: 12 });
  } else finishNavigation();
  if (props.workspaceMode) emit('region', feature);
  else void loadRegionImagery();
  void loadCounties();
  arrangeRegionLabels();
}

function selectRegionById(regionId: string): void {
  const feature = regions.value.find(
    (item) => item.id === regionId || item.properties.region_id === regionId,
  );
  if (feature) selectRegion(feature);
}

function installRegions(data: RegionCollection): void {
  if (!map) return;
  const m = map;
  regions.value = data.features;
  addCoverage(m, data);
  const [w, s, e, n] = data.bounds;
  provinceExtent = L.latLngBounds([s, w], [n, e]);
  m.createPane('cdp-regions').style.zIndex = '400';
  m.createPane('cdp-counties').style.zIndex = '410';
  const labels = m.createPane('cdp-region-labels');
  labels.style.zIndex = '660';
  labels.style.pointerEvents = 'none';
  L.geoJSON(data as GeoJSON.GeoJsonObject, {
    pane: 'cdp-regions',
    style: (f) => regionStyle(String(f?.id)),
    onEachFeature: (f, layer) => {
      const feature = f as unknown as RegionFeature;
      regionPaths.set(feature.id, layer as L.Path);
      layer.on('click', () => {
        if (isOverview.value) selectRegion(feature);
      });
      const [lng, lat] = feature.properties.label;
      const label = document.createElement('span');
      label.textContent = feature.properties.name;
      const marker = L.marker([lat, lng], {
        interactive: false,
        pane: 'cdp-region-labels',
        icon: L.divIcon({
          className: 'region-label',
          html: label,
          iconSize: [96, 22],
          iconAnchor: [48, 11],
        }),
      }).addTo(m);
      const leader = L.polyline([], {
        pane: 'cdp-region-labels',
        interactive: false,
        color: '#94b8ce',
        weight: 1,
        opacity: 0.6,
      }).addTo(m);
      regionLabels.set(feature.id, { marker, leader, feature });
    },
  }).addTo(m);
  for (const feature of data.features) {
    const element = regionPaths.get(feature.id)?.getElement();
    element?.setAttribute('data-region-id', feature.id);
    element?.setAttribute('aria-label', feature.properties.name);
  }
  updateOverviewZoom();
  m.on('resize', updateOverviewZoom);
  m.on('movestart zoomstart', hideRegionLabels);
  m.on('moveend zoomend resize', arrangeRegionLabels);
  m.on('moveend', showRegionLabels);
  arrangeRegionLabels();
  m.on('zoomend', () => {
    if (
      hasVisibleMap() &&
      !drawMode.value &&
      !props.selectionBounds &&
      !props.selectionDisabled &&
      !navigationAnimating &&
      activeRegion.value &&
      m.getZoom() <= cityExitZoom
    )
      returnToProvince();
  });
  // A pointer/keyboard gesture can interrupt flyTo without its final moveend.
  m.getContainer().addEventListener('pointerdown', interruptNavigation, true);
  m.getContainer().addEventListener('wheel', interruptNavigation, { passive: true, capture: true });
  m.getContainer().addEventListener('keydown', interruptNavigation, true);
}

function interruptNavigation(): void {
  if (!navigationAnimating || !map) return;
  const target = navigationTarget;
  cancelNavigation();
  // Complete this flight's target (province, city, county or result) before
  // applying the gesture, rather than resetting every interruption to the city.
  if (target) map.fitBounds(target.bounds, { ...target.options, animate: false });
  navigationTarget = target;
  finishNavigation();
}

type Bounds = [number, number, number, number];
interface ImageLayerEntry {
  key: string;
  image: ImageInfo;
  pane: string;
  visible: boolean;
  loading: boolean;
  error: string;
}
interface ResultLayerEntry {
  aoi?: AreaGeometry;
  key: string;
  pair?: TimelinePair;
  color: string;
  pane: string;
  visible: boolean;
  bounds?: Bounds;
  stats?: DetectionStats;
  featureCount?: number;
}
const imageEntries = ref<ImageLayerEntry[]>([]);
const resultEntries = ref<ResultLayerEntry[]>([]);
const selectedPatch = ref<{ key: string; feature: ChangePatch; pair?: TimelinePair } | null>(null);
const imageLayers = new Map<string, L.LayerGroup>();
const resultLayers = new Map<string, L.LayerGroup>();
// Reuse panes between batches rather than accumulating detached panes.
const imagePanes: string[] = [];
const resultPanes: string[] = [];
let imageGeneration = 0;
const VECTOR_STYLE: L.PathOptions = {
  color: '#38bdf8',
  weight: 1,
  opacity: 0.8,
  fillColor: '#0ea5e9',
  fillOpacity: 0.15,
};

const drawMode = ref(false);
const polygonMode = ref<'draw' | 'edit' | null>(null);
const selectionMessage = ref('');
let polygonPoints: Position[] = [];
let draftLayers: L.LayerGroup | null = null;
let draftEvents: AbortController | null = null;
let draftLine: L.Polyline | null = null;
let draftFill: L.Polygon | null = null;
let selectedVertex: number | null = null;
let draggingVertex = false;
let vertexDragPoints: Position[] | null = null;
let committedGeometryKey: string | null = null;
let edgeDrag: {
  index: number;
  points: Position[];
  projected: Position[];
  cursor: L.Point;
  zoom: number;
  pointerId: number;
} | null = null;
let selectionPolygon: L.Polygon | null = null;
let displayedGeometry: AreaGeometry | null = null;
let selectionRect: L.Rectangle | null = null;
let selectionLabel: L.Marker | null = null;
let selectionLabelContent: HTMLDivElement | null = null;
let pixelRequest: AbortController | null = null;
let pixelTimer: ReturnType<typeof setTimeout> | undefined;
let displayedSelection: AreaBounds | null = null;
let resultRect: L.Polygon | null = null;
let drawStart: L.LatLng | null = null;
let drawPointerId: number | null = null;
let resizeObserver: ResizeObserver | null = null;
const statsPanelRef = ref<HTMLElement | null>(null);
const toolbarRef = ref<HTMLElement | null>(null);
const layersRef = ref<HTMLElement | null>(null);
watch(layersExpanded, () => { void nextTick(arrangeRegionLabels); });
const resultAnchor = ref<{ x: number; y: number } | null>(null);
const activeImageKey = computed(
  () => imageEntries.value.find((entry) => entry.visible && !entry.loading && !entry.error)?.key,
);
const visibleLayerCount = computed(
  () =>
    resultEntries.value.filter((entry) => entry.visible).length +
    (baseMapsVisible.value
      ? imageEntries.value.filter((entry) => entry.visible).length + Number(navigationVisible.value)
      : 0),
);
function formatStat(value: number | undefined, kind: 'count' | 'ratio' | 'confidence'): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  if (kind === 'ratio') return `${(value * 100).toFixed(2)}%`;
  return kind === 'confidence' ? value.toFixed(4) : value.toLocaleString();
}
const statsRows = computed(() =>
  resultEntries.value.map((entry) => ({
    ...entry,
    label: entry.pair
      ? `${phaseDate(entry.pair.before)} → ${phaseDate(entry.pair.after)}`
      : '检测结果',
    count: formatStat(entry.stats?.feature_count ?? entry.featureCount, 'count'),
    ratio: formatStat(entry.stats?.change_ratio, 'ratio'),
    confidence: formatStat(entry.stats?.mean_confidence, 'confidence'),
  })),
);

function updateResultAnchor(): void {
  if (!map || !resultRect) {
    resultAnchor.value = null;
    return;
  }
  const point = map.latLngToContainerPoint(resultRect.getBounds().getNorthEast());
  const size = map.getSize();
  const width = statsPanelRef.value?.offsetWidth || 200;
  const height = statsPanelRef.value?.offsetHeight || 200;
  const x = Math.max(12, Math.min(point.x + 8, size.x - width - 12));
  let y = Math.max(12, point.y - height - 8);
  const toolbar = toolbarRef.value;
  if (
    toolbar &&
    x + width > toolbar.offsetLeft &&
    x < toolbar.offsetLeft + toolbar.offsetWidth &&
    y + height > toolbar.offsetTop &&
    y < toolbar.offsetTop + toolbar.offsetHeight
  ) {
    y = toolbar.offsetTop + toolbar.offsetHeight + 8;
  }
  resultAnchor.value = { x, y: Math.max(12, Math.min(y, size.y - height - 12)) };
}
watch(statsRows, async () => {
  await nextTick();
  updateResultAnchor();
});

function updateSelectionPixels(): void {
  clearTimeout(pixelTimer);
  pixelRequest?.abort();
  pixelRequest = null;
  const bounds = displayedSelection;
  const content = selectionLabelContent;
  const phase = props.activePhase;
  if (!bounds || !content) return;
  if (!phase) {
    content.textContent = '像元尺寸：请先加载时相';
    return;
  }
  content.textContent = `${imageryPhaseLabel(phase)} · 正在计算像元尺寸…`;
  const request = new AbortController();
  pixelRequest = request;
  pixelTimer = setTimeout(async () => {
    try {
      const response = await fetch('/py/local/selection-pixels', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: request.signal,
        body: JSON.stringify({
          bounds: [bounds.west, bounds.south, bounds.east, bounds.north],
          aoi: displayedGeometry ?? undefined,
          admin_code: props.boundarySelection?.id,
          admin_version: props.boundarySelection?.properties.boundary_version,
          region_id: phase.region_id,
          scene_ids: phase.scenes.filter((scene) => scene.available).map((scene) => scene.id),
        }),
      });
      const result = await response.json();
      if (request.signal.aborted) return;
      if (!response.ok)
        throw new Error(typeof result.detail === 'string' ? result.detail : '像元尺寸计算失败');
      content.textContent = `${imageryPhaseLabel(phase)} · 矩形裁切网格\n${result.width.toLocaleString()} × ${result.height.toLocaleString()} 像元\n网格共 ${result.pixels.toLocaleString()} 像元`;
    } catch (error) {
      if (!request.signal.aborted)
        content.textContent = error instanceof Error ? error.message : '像元尺寸计算失败';
    }
  }, 180);
}
watch(() => props.activePhase, updateSelectionPixels);

function showSelection(
  bounds: AreaBounds | null | undefined,
  geometry: AreaPolygon | null | undefined = props.selectionGeometry,
): void {
  if (!map) return;
  displayedSelection = bounds ?? null;
  displayedGeometry = bounds ? (props.boundarySelection?.geometry ?? geometry ?? null) : null;
  selectionPolygon?.remove();
  selectionPolygon = null;
  if (displayedGeometry) {
    selectionRect?.remove();
    selectionRect = null;
    selectionPolygon = L.polygon(
      provincePolygons(displayedGeometry).map(polygon => polygon.map(ring => ring.map(([lng,lat]) => [lat,lng] as L.LatLngTuple))),
      { pane: 'cdp-area', color: '#22d3ee', weight: 2, fillOpacity: 0.08, interactive: false },
    ).addTo(map);
  }
  if (!bounds) {
    selectionRect?.remove();
    selectionRect = null;
    selectionLabel?.remove();
    selectionLabel = null;
    selectionLabelContent = null;
    clearTimeout(pixelTimer);
    pixelRequest?.abort();
    pixelRequest = null;
    return;
  }
  const extent: L.LatLngBoundsLiteral = [
    [bounds.south, bounds.west],
    [bounds.north, bounds.east],
  ];
  if (!displayedGeometry) {
    if (selectionRect) selectionRect.setBounds(extent);
    else
      selectionRect = L.rectangle(extent, {
        pane: 'cdp-area',
        color: '#22d3ee',
        weight: 2,
        fillOpacity: 0.08,
        interactive: false,
      }).addTo(map);
  }
  if (!selectionLabel) {
    selectionLabelContent = document.createElement('div');
    selectionLabelContent.className = 'selection-pixel-content';
    selectionLabelContent.setAttribute('role', 'status');
    selectionLabelContent.setAttribute('aria-label', '框选区域像元尺寸');
    selectionLabel = L.marker([bounds.north, bounds.east], {
      pane: 'cdp-area',
      interactive: false,
      keyboard: false,
      zIndexOffset: 1000,
      icon: L.divIcon({
        className: 'selection-pixel-label',
        html: selectionLabelContent,
        iconSize: [0, 0],
        iconAnchor: [0, 0],
      }),
    }).addTo(map);
  } else selectionLabel.setLatLng([bounds.north, bounds.east]);
  updateSelectionPixels();
}
watch(
  () => [props.selectionBounds, props.selectionGeometry],
  () => {
    if (drawMode.value) setDrawMode(false);
    else cancelSelectionDrag();
    if (committedGeometryKey !== JSON.stringify(props.selectionGeometry?.coordinates))
      selectedVertex = null;
    committedGeometryKey = null;
    showSelection(props.selectionBounds);
    syncSelectionEditor();
  },
);
watch(
  () => props.selectionDisabled,
  (disabled) => {
    if (disabled) setDrawMode(false);
    else syncSelectionEditor();
    if (activeRegion.value) updateOverviewZoom();
  },
);
watch(activeRegion, syncSelectionEditor);

function clearDraftLayers(): void {
  draftEvents?.abort();
  draftEvents = null;
  draftLayers?.remove();
  draftLayers = null;
  draftLine = null;
  draftFill = null;
}

function setDrawMode(enabled: boolean): void {
  if (enabled && props.selectionDisabled) return;
  cancelSelectionDrag();
  polygonMode.value = null;
  clearDraftLayers();
  polygonPoints = [];
  selectedVertex = null;
  selectionMessage.value = '';
  emit('editing', enabled);
  if (enabled) {
    isOverview.value = false;
    regionPaths.forEach((layer, id) => layer.setStyle(regionStyle(id)));
  }
  drawMode.value = enabled;
  if (!map) return;
  for (const handler of [
    map.dragging,
    map.scrollWheelZoom,
    map.boxZoom,
    map.touchZoom,
  ]) {
    if (enabled) handler.disable();
    else handler.enable();
  }
  map.getContainer().style.cursor = enabled ? 'crosshair' : '';
  map.getContainer().style.touchAction = enabled ? 'none' : '';
  if (!enabled) {
    if (drawPointerId != null && map.getContainer().hasPointerCapture(drawPointerId))
      map.getContainer().releasePointerCapture(drawPointerId);
    drawPointerId = null;
    drawStart = null;
    showSelection(props.selectionBounds);
    syncSelectionEditor();
  }
}

function onAreaPointerDown(event: PointerEvent): void {
  if (!map || !drawMode.value || polygonMode.value || event.button !== 0 || drawPointerId != null)
    return;
  if (isMapControl(event.target)) return;
  event.preventDefault();
  drawStart = map.mouseEventToLatLng(event);
  drawPointerId = event.pointerId;
  map.getContainer().setPointerCapture(event.pointerId);
}
function pointerBounds(event: PointerEvent): AreaBounds | null {
  if (!map || !drawStart) return null;
  const point = map.mouseEventToLatLng(event);
  const bounds = {
    west: Math.min(drawStart.lng, point.lng),
    south: Math.min(drawStart.lat, point.lat),
    east: Math.max(drawStart.lng, point.lng),
    north: Math.max(drawStart.lat, point.lat),
  };
  return bounds.east - bounds.west > 1e-7 && bounds.north - bounds.south > 1e-7 ? bounds : null;
}
function onAreaPointerMove(event: PointerEvent): void {
  if (map && edgeDrag && event.pointerId === edgeDrag.pointerId) {
    const cursor = map.project(map.mouseEventToLatLng(event), edgeDrag.zoom);
    const offset: Position = [cursor.x - edgeDrag.cursor.x, cursor.y - edgeDrag.cursor.y];
    const projected = moveParallelEdge(edgeDrag.projected, edgeDrag.index, offset);
    if (!projected) {
      selectionMessage.value = '相邻边平行或边已折返，无法移动到该位置';
      return;
    }
    const next: Position[] = projected.map(([x, y], i) => {
      if (i !== edgeDrag!.index && i !== (edgeDrag!.index + 1) % projected.length)
        return edgeDrag!.points[i];
      const p = map!.unproject([x, y], edgeDrag!.zoom);
      return [p.lng, p.lat];
    });
    if (acceptPolygonEdit(next)) renderPolygonDraft();
    return;
  }
  if (map && polygonMode.value === 'draw' && polygonPoints.length && !isMapControl(event.target)) {
    const point = map.mouseEventToLatLng(event);
    const candidate = constrainToRegion(
      activeRegion.value!.geometry,
      polygonPoints[polygonPoints.length - 1],
      [point.lng, point.lat],
    );
    draftLine?.setLatLngs(
      [...polygonPoints, candidate].map(([lng, lat]) => [lat, lng] as L.LatLngTuple),
    );
    return;
  }
  if (event.pointerId !== drawPointerId) return;
  const bounds = pointerBounds(event);
  if (bounds) showSelection(bounds, null);
}
function onAreaPointerUp(event: PointerEvent): void {
  if (edgeDrag && event.pointerId === edgeDrag.pointerId) {
    const previous = edgeDrag.points;
    endEdgeDrag();
    commitPolygonEdit(previous);
    return;
  }
  if (event.pointerId !== drawPointerId) return;
  const bounds = pointerBounds(event);
  setDrawMode(false);
  if (bounds) {
    showSelection(bounds, null);
    emit('area', bounds, null);
  }
}
function onAreaPointerCancel(): void {
  if (edgeDrag || draggingVertex) {
    cancelSelectionDrag();
    renderPolygonDraft();
  } else if (!polygonMode.value) setDrawMode(false);
}
function onAreaKeydown(event: KeyboardEvent): void {
  if (
    !hasVisibleMap() ||
    (!drawMode.value && polygonMode.value !== 'edit') ||
    props.selectionDisabled ||
    (event.target as HTMLElement)?.closest('input, textarea, select, [contenteditable="true"]')
  )
    return;
  if (event.key === 'Escape') {
    event.preventDefault();
    if (drawMode.value) setDrawMode(false);
    else {
      cancelSelectionDrag();
      selectedVertex = null;
      selectionMessage.value = '';
      renderPolygonDraft();
    }
  } else if (polygonMode.value === 'draw' && event.key === 'Backspace') {
    event.preventDefault();
    polygonPoints.pop();
    selectionMessage.value = '';
    renderPolygonDraft();
  } else if (polygonMode.value === 'draw' && event.key === 'Enter') {
    event.preventDefault();
    finishPolygon();
  } else if (
    polygonMode.value === 'edit' &&
    !edgeDrag &&
    !draggingVertex &&
    event.key === 'Delete' &&
    selectedVertex != null
  ) {
    event.preventDefault();
    const next = polygonPoints.filter((_, i) => i !== selectedVertex);
    if (acceptPolygonEdit(next)) {
      selectedVertex = null;
      commitPolygonEdit();
    }
  }
}
function clearSelection(): void {
  setDrawMode(false);
  emit('area', null);
}

function isMapControl(target: EventTarget | null): boolean {
  return (
    target instanceof Element &&
    !!target.closest(
      '.leaflet-control, .leaflet-marker-icon, button, input, select, [role="toolbar"], .selection-pixel-content',
    )
  );
}

function startPolygon(): void {
  if (!map || !activeRegion.value || props.selectionDisabled) return;
  setDrawMode(true);
  polygonMode.value = 'draw';
  showSelection(null);
  renderPolygonDraft();
}

/** A completed selection always exposes both vertex and edge handles. */
function syncSelectionEditor(): void {
  if (!map || drawMode.value || edgeDrag || draggingVertex) return;
  clearDraftLayers();
  polygonMode.value = null;
  polygonPoints = [];
  if (props.boundarySelection) return;
  if (props.selectionDisabled || !props.selectionBounds || !activeRegion.value) return;
  const initial: Position[] | null = props.selectionGeometry
    ? props.selectionGeometry.coordinates[0].slice(0, -1).map((p) => [p[0], p[1]])
    : rectangleEditPoints(props.selectionBounds, activeRegion.value.geometry);
  if (!initial) {
    selectionMessage.value =
      '矩形交集为空、包含孔洞或多个离散区域，无法直接编辑，请重新选择单一区域';
    return;
  }
  polygonPoints = initial;
  polygonMode.value = 'edit';
  if (
    !props.selectionGeometry &&
    polygonError(rectanglePoints(props.selectionBounds), activeRegion.value.geometry)
  ) {
    selectionMessage.value = '编辑按行政区边界裁切后的矩形，松开后自动保存为多边形';
  }
  renderPolygonDraft();
}

function cancelSelectionDrag(): void {
  if (edgeDrag) {
    polygonPoints = edgeDrag.points;
    endEdgeDrag();
  }
  if (draggingVertex && vertexDragPoints) polygonPoints = vertexDragPoints;
  draggingVertex = false;
  vertexDragPoints = null;
  emit('editing', drawMode.value);
}

function commitPolygonEdit(previous?: Position[]): void {
  if (polygonMode.value !== 'edit' || props.selectionDisabled) return;
  if (
    !previous ||
    previous.length !== polygonPoints.length ||
    previous.some((p, i) => !samePoint(p, polygonPoints[i]))
  ) {
    const geometry = polygonFromPoints(polygonPoints),
      bounds = polygonBounds(geometry);
    committedGeometryKey = JSON.stringify(geometry.coordinates);
    showSelection(bounds, geometry);
    emit('area', bounds, geometry);
  }
  emit('editing', false);
  renderPolygonDraft();
}

function endEdgeDrag(): void {
  if (!edgeDrag || !map) return;
  const id = edgeDrag.pointerId;
  edgeDrag = null;
  if (map.getContainer().hasPointerCapture(id)) map.getContainer().releasePointerCapture(id);
  if (polygonMode.value !== 'draw') map.dragging.enable();
}

function beginEdgeDrag(event: PointerEvent, index: number): void {
  if (
    !map ||
    polygonMode.value !== 'edit' ||
    props.selectionDisabled ||
    event.button !== 0 ||
    edgeDrag ||
    draggingVertex
  )
    return;
  event.preventDefault();
  event.stopPropagation();
  const zoom = map.getZoom();
  edgeDrag = {
    index,
    points: polygonPoints.map((p) => [...p]),
    zoom,
    pointerId: event.pointerId,
    projected: polygonPoints.map(([lng, lat]) => {
      const p = map!.project([lat, lng], zoom);
      return [p.x, p.y];
    }),
    cursor: map.project(map.mouseEventToLatLng(event), zoom),
  };
  map.dragging.disable();
  map.getContainer().setPointerCapture(event.pointerId);
  selectedVertex = null;
  emit('editing', true);
}

function acceptPolygonEdit(next: Position[]): boolean {
  const error = polygonError(next, activeRegion.value!.geometry);
  selectionMessage.value = error;
  if (error) return false;
  polygonPoints = next;
  return true;
}

function renderPolygonDraft(): void {
  if (!map || !polygonMode.value) return;
  clearDraftLayers();
  const events = (draftEvents = new AbortController());
  draftLayers = L.layerGroup().addTo(map);
  const closed = polygonMode.value !== 'draw';
  const verticesEditable = polygonMode.value === 'edit';
  if (closed) {
    selectionRect?.remove();
    selectionRect = null;
    selectionPolygon?.remove();
    selectionPolygon = null;
  }
  const linePoints =
    closed && polygonPoints.length ? [...polygonPoints, polygonPoints[0]] : polygonPoints;
  draftLine = L.polyline(
    linePoints.map(([lng, lat]) => [lat, lng] as L.LatLngTuple),
    { pane: 'cdp-area', color: '#22d3ee', weight: 2, interactive: false },
  ).addTo(draftLayers);
  if (closed)
    draftFill = L.polygon(
      polygonPoints.map(([lng, lat]) => [lat, lng] as L.LatLngTuple),
      {
        pane: 'cdp-area',
        stroke: false,
        fillColor: '#22d3ee',
        fillOpacity: 0.08,
        interactive: false,
      },
    ).addTo(draftLayers);
  polygonPoints.forEach((point, index) => {
    const marker = L.marker([point[1], point[0]], {
      pane: 'cdp-edit',
      draggable: verticesEditable,
      title: `顶点 ${index + 1}`,
      keyboard: true,
      icon: L.divIcon({
        className: `polygon-vertex${selectedVertex === index ? ' selected' : ''}`,
        iconSize: [14, 14],
        iconAnchor: [7, 7],
      }),
    }).addTo(draftLayers!);
    marker.on('click', () => {
      if (!closed) {
        if (index === 0) finishPolygon();
      } else if (verticesEditable) {
        selectedVertex = index;
        renderPolygonDraft();
      }
    });
    // The first click of a double click creates a marker under the cursor.
    // Handle the following dblclick on that marker as well as on the map.
    marker.on('dblclick', () => {
      if (!closed) finishPolygon();
    });
    marker.on('dragstart', () => {
      draggingVertex = true;
      selectedVertex = index;
      vertexDragPoints = polygonPoints.map((p) => [...p]);
      emit('editing', true);
    });
    marker.on('drag', () => {
      const cursor = marker.getLatLng();
      const previous = polygonPoints[(index + polygonPoints.length - 1) % polygonPoints.length];
      const candidate = constrainToRegion(activeRegion.value!.geometry, previous, [
        cursor.lng,
        cursor.lat,
      ]);
      const next = polygonPoints.map((p, i) => (i === index ? candidate : p));
      acceptPolygonEdit(next);
      const accepted = polygonPoints[index];
      marker.setLatLng([accepted[1], accepted[0]]);
      draftLine?.setLatLngs(
        [...polygonPoints, polygonPoints[0]].map(([lng, lat]) => [lat, lng] as L.LatLngTuple),
      );
      draftFill?.setLatLngs(polygonPoints.map(([lng, lat]) => [lat, lng] as L.LatLngTuple));
    });
    marker.on('dragend', () => {
      if (!draggingVertex) return;
      const previous = vertexDragPoints!;
      draggingVertex = false;
      vertexDragPoints = null;
      commitPolygonEdit(previous);
    });
    if (closed) {
      const following = polygonPoints[(index + 1) % polygonPoints.length];
      const hit = L.polyline(
        [
          [point[1], point[0]],
          [following[1], following[0]],
        ],
        {
          pane: 'cdp-edit-edges',
          weight: 18,
          opacity: 0,
          className: 'polygon-edge-hit',
          bubblingMouseEvents: false,
        },
      ).addTo(draftLayers!);
      const element = hit.getElement();
      if (element)
        element.addEventListener(
          'pointerdown',
          (event) => beginEdgeDrag(event as PointerEvent, index),
          { signal: events.signal },
        );
    }
    if (verticesEditable) {
      const following = polygonPoints[(index + 1) % polygonPoints.length];
      const midpoint: Position = [(point[0] + following[0]) / 2, (point[1] + following[1]) / 2];
      L.marker([midpoint[1], midpoint[0]], {
        pane: 'cdp-edit',
        title: `添加顶点 ${index + 1}`,
        keyboard: true,
        icon: L.divIcon({ className: 'polygon-midpoint', iconSize: [10, 10], iconAnchor: [5, 5] }),
      })
        .addTo(draftLayers!)
        .on('click', () => {
          const next = [
            ...polygonPoints.slice(0, index + 1),
            midpoint,
            ...polygonPoints.slice(index + 1),
          ];
          if (acceptPolygonEdit(next)) {
            selectedVertex = index + 1;
            commitPolygonEdit();
          }
        });
    }
  });
}

function onPolygonClick(event: MouseEvent): void {
  if (
    !map ||
    polygonMode.value !== 'draw' ||
    event.button !== 0 ||
    event.detail > 1 ||
    isMapControl(event.target)
  )
    return;
  const point = map.mouseEventToLatLng(event);
  const cursor: Position = [point.lng, point.lat];
  const region = activeRegion.value!.geometry;
  if (!polygonPoints.length && !regionCoversPoint(region, cursor)) {
    selectionMessage.value = '第一个顶点须位于当前行政区内';
    return;
  }
  if (
    polygonPoints.length >= 3 &&
    map
      .latLngToContainerPoint([polygonPoints[0][1], polygonPoints[0][0]])
      .distanceTo(map.mouseEventToContainerPoint(event)) <= 10
  ) {
    finishPolygon();
    return;
  }
  const candidate = polygonPoints.length
    ? constrainToRegion(region, polygonPoints[polygonPoints.length - 1], cursor)
    : cursor;
  if (polygonPoints.some((p) => samePoint(p, candidate))) {
    selectionMessage.value = '顶点不能重复，请选择其他位置';
    return;
  }
  polygonPoints.push(candidate);
  selectionMessage.value = '';
  renderPolygonDraft();
}

function onPolygonDoubleClick(event: MouseEvent): void {
  if (polygonMode.value === 'draw' && !isMapControl(event.target)) {
    event.preventDefault();
    finishPolygon();
  }
}

function finishPolygon(): void {
  if (polygonMode.value !== 'draw' || props.selectionDisabled || !activeRegion.value) return;
  const error = polygonError(polygonPoints, activeRegion.value.geometry);
  if (error) {
    selectionMessage.value = error;
    return;
  }
  const geometry = polygonFromPoints(polygonPoints),
    bounds = polygonBounds(geometry);
  setDrawMode(false);
  showSelection(bounds, geometry);
  emit('area', bounds, geometry);
}

function toLatLngBounds(b: [number, number, number, number]): L.LatLngBounds {
  // b = [south, west, north, east]
  return L.latLngBounds([b[0], b[1]], [b[2], b[3]]);
}

function paneFor(kind: 'image' | 'result', index: number): string {
  const panes = kind === 'image' ? imagePanes : resultPanes;
  if (!panes[index]) {
    panes[index] = `cdp-${kind}-${index}`;
    map!.createPane(panes[index], map!.getPane(kind === 'image' ? 'cdp-images' : 'cdp-results'));
  }
  return panes[index];
}

function syncLayerOrder(): void {
  for (const entries of [imageEntries.value, resultEntries.value]) {
    entries.forEach((entry, index) => {
      const pane = map?.getPane(entry.pane);
      if (pane) pane.style.zIndex = String(entries.length - index);
    });
  }
}

function moveLayer(kind: 'image' | 'result', key: string, direction: -1 | 1): void {
  const entries = kind === 'image' ? imageEntries.value : resultEntries.value;
  const index = entries.findIndex((entry) => entry.key === key);
  const target = index + direction;
  if (index < 0 || target < 0 || target >= entries.length) return;
  [entries[index], entries[target]] = [entries[target], entries[index]];
  syncLayerOrder();
}

function resetLayerOrder(): void {
  imageEntries.value.sort((a, b) => newestImagesFirst(a.image, b.image));
  resultEntries.value.sort((a, b) => newestResultsFirst(a.pair, b.pair));
  syncLayerOrder();
}

function syncVisibility(
  entry: { key: string; visible: boolean },
  layers: Map<string, L.LayerGroup>,
): void {
  const group = layers.get(entry.key);
  if (!map || !group) return;
  if (entry.visible) group.addTo(map);
  else group.remove();
}

function toggleImage(entry: ImageLayerEntry): void {
  syncVisibility({ key: entry.key, visible: baseMapsVisible.value && entry.visible }, imageLayers);
}
function toggleResult(entry: ResultLayerEntry): void {
  syncVisibility(entry, resultLayers);
}
function toggleNavigation(): void {
  if (props.workspaceMode) {
    if (baseMapsVisible.value && navigationVisible.value && hasVisibleMap()) phaseCache?.resume();
    else phaseCache?.suspend();
    return;
  }
  if (!map || !currentTile) return;
  if (baseMapsVisible.value && navigationVisible.value) currentTile.addTo(map);
  else currentTile.remove();
}

function toggleBaseMaps(): void {
  baseMapsVisible.value = !baseMapsVisible.value;
  toggleNavigation();
  for (const entry of imageEntries.value) toggleImage(entry);
}

function activateImage(entry: ImageLayerEntry): void {
  isOverview.value = false;
  navigationVisible.value = false;
  toggleNavigation();
  for (const item of imageEntries.value) {
    item.visible = false;
    toggleImage(item);
  }
  entry.visible = true;
  toggleImage(entry);
  const index = imageEntries.value.findIndex((item) => item.key === entry.key);
  imageEntries.value.unshift(...imageEntries.value.splice(index, 1));
  syncLayerOrder();
}

function updateResultBounds(): void {
  if (!map) return;
  const bounds = resultEntries.value.flatMap((entry) => (entry.bounds ? [entry.bounds] : []));
  resultRect?.remove();
  resultRect = null;
  resultAnchor.value = null;
  if (!bounds.length) return;
  const extent: Bounds = [
    Math.min(...bounds.map((b) => b[0])),
    Math.min(...bounds.map((b) => b[1])),
    Math.max(...bounds.map((b) => b[2])),
    Math.max(...bounds.map((b) => b[3])),
  ];
  const style = { pane: 'cdp-area', color: '#f59e0b', weight: 2, fill: false, interactive: false };
  if (resultEntries.value.some((entry) => entry.aoi)) {
    const outlines = resultEntries.value.flatMap((entry) => {
      if (entry.aoi) return provincePolygons(entry.aoi).map(polygon => polygon.map(ring => ring.map(([lng,lat]) => [lat,lng] as L.LatLngTuple)));
      const points =
        (entry.bounds
          ? rectanglePoints({
              south: entry.bounds[0],
              west: entry.bounds[1],
              north: entry.bounds[2],
              east: entry.bounds[3],
            })
          : []);
      return points.length ? [[points.map(([lng, lat]) => [lat, lng] as L.LatLngTuple)]] : [];
    });
    resultRect = L.polygon(outlines, style).addTo(map);
  } else resultRect = L.rectangle(toLatLngBounds(extent), style).addTo(map);
  updateResultAnchor();
}

function patchDetails(feature: ChangePatch, pair?: TimelinePair): string[][] {
  const properties = feature.properties || {};
  const number = (value: number | undefined, digits = 2): string =>
    typeof value === 'number' && Number.isFinite(value)
      ? value.toLocaleString(undefined, { maximumFractionDigits: digits })
      : '—';
  const score = properties.confidence_mean;
  return [
    ['时相', pair ? `${phaseDate(pair.before)} → ${phaseDate(pair.after)}` : '—'],
    [
      '面积',
      typeof properties.area_m2 === 'number' && Number.isFinite(properties.area_m2)
        ? `${number(properties.area_m2)} m²`
        : '—',
    ],
    ['像元数', number(properties.area_pixels, 0)],
    [
      '平均变化置信度',
      typeof score === 'number' && Number.isFinite(score) ? score.toFixed(4) : '—',
    ],
    ['几何', feature.geometry.type === 'MultiPolygon' ? '多部件多边形' : '多边形'],
  ];
}
const selectedPatchRows = computed(() =>
  selectedPatch.value ? patchDetails(selectedPatch.value.feature, selectedPatch.value.pair) : [],
);

/** Keep each period's mask and polygon boundary together in one ordered pane. */
function addResultOverlays(opts: ResultOverlayPayload): void {
  if (!map) return;
  const key = opts.pair?.key || 'result';
  const previous = resultEntries.value.find((entry) => entry.key === key);
  resultLayers.get(key)?.remove();
  const pane = previous?.pane ?? paneFor('result', resultEntries.value.length);
  const group = L.layerGroup();
  resultLayers.set(key, group);
  const color = opts.pair?.color || VECTOR_STYLE.color!;
  if (opts.maskDataUrl && opts.maskBounds) {
    L.imageOverlay(opts.maskDataUrl, toLatLngBounds(opts.maskBounds), {
      opacity: 1,
      pane,
    }).addTo(group);
  }
  if (opts.geojson) {
    L.geoJSON(opts.geojson as GeoJSON.GeoJsonObject, {
      style: { ...VECTOR_STYLE, color, fillColor: color },
      pane,
      onEachFeature: (feature, layer) => {
        layer.on('click', () => {
          selectedPatch.value = { key, feature: feature as ChangePatch, pair: opts.pair };
        });
        layer.on('remove', () => {
          if (selectedPatch.value?.key === key) selectedPatch.value = null;
        });
      },
    }).addTo(group);
  }
  const geojson = opts.geojson as { type?: string; features?: unknown[] } | undefined;
  const entry: ResultLayerEntry = {
    aoi: opts.aoi,
    key,
    pair: opts.pair,
    pane,
    color,
    visible: previous?.visible ?? true,
    bounds: opts.maskBounds ?? opts.resultBounds,
    stats: opts.stats ?? opts.pair?.stats,
    featureCount:
      geojson?.type === 'FeatureCollection' && Array.isArray(geojson.features)
        ? geojson.features.length
        : undefined,
  };
  if (previous) resultEntries.value.splice(resultEntries.value.indexOf(previous), 1, entry);
  else {
    // Insert the new period without undoing manual ordering of existing layers.
    const index = resultEntries.value.findIndex(
      (item) => newestResultsFirst(entry.pair, item.pair) < 0,
    );
    resultEntries.value.splice(index < 0 ? resultEntries.value.length : index, 0, entry);
  }
  syncVisibility(entry, resultLayers);
  syncLayerOrder();
  updateResultBounds();
}

/** 移除上一轮叠加的掩膜与矢量边界（提交新任务前调用）。 */
function clearResultOverlays(): void {
  for (const group of resultLayers.values()) group.remove();
  resultLayers.clear();
  resultEntries.value = [];
  resultRect?.remove();
  resultRect = null;
  resultAnchor.value = null;
}

/**
 * 将视野平滑转移到给定地理范围。bounds 顺序 [south, west, north, east]，
 * 与 addResultOverlays 的 maskBounds 保持一致，供父组件在结果就绪/选中影像后调用，
 * 保证叠加结果落在视野内可见。
 */
function flyToBounds(b: [number, number, number, number], padding: number = 0): void {
  if (!map) return;
  isOverview.value = false;
  const region = regions.value.find((feature) =>
    containsProvincePoint(feature.geometry, (b[0] + b[2]) / 2, (b[1] + b[3]) / 2),
  );
  if (region?.id !== activeRegion.value?.id) {
    if (region) selectRegion(region, false, false);
    else clearRegionNavigation();
  }
  cancelNavigation();
  try {
    map.invalidateSize({ pan: false });
    // Programmatic result positioning must obey city bounds without triggering
    // the user-only zoom-out exit. Keep the result panel's existing padding.
    animateToBounds(
      toLatLngBounds(b),
      resultRect
        ? {
            paddingTopLeft: [padding, Math.min(180, map.getSize().y / 2)],
            paddingBottomRight: [Math.min(240, map.getSize().x / 2), padding],
          }
        : { padding: [padding, padding] },
    );
  } catch {
    /* 非法/退化 bounds 时忽略，保持当前视野 */
    finishNavigation();
  }
}

async function loadImages(images: ImageInfo[] = []): Promise<void> {
  if (!map) return;
  const token = ++imageGeneration;
  for (const group of imageLayers.values()) group.remove();
  imageLayers.clear();
  imageEntries.value = [...images].sort(newestImagesFirst).map((image, index) => ({
    key: image.id,
    image,
    pane: paneFor('image', index),
    visible: !isOverview.value,
    loading: true,
    error: '',
  }));
  syncLayerOrder();
  await Promise.all(
    imageEntries.value.map(async (entry) => {
      try {
        if (!entry.image.file_path) throw new Error('影像路径缺失');
        const overlay = await getImageOverlay(entry.image.file_path);
        if (token !== imageGeneration || !map) return;
        const group = L.layerGroup();
        L.imageOverlay(
          'data:image/png;base64,' + overlay.image_b64,
          toLatLngBounds(overlay.bounds),
          { opacity: 1, pane: entry.pane },
        ).addTo(group);
        imageLayers.set(entry.key, group);
        entry.loading = false;
        toggleImage(entry);
      } catch (error) {
        if (token !== imageGeneration) return;
        entry.loading = false;
        entry.error = error instanceof Error ? error.message : '影像加载失败';
      }
    }),
  );
}
watch(
  () => props.images,
  (images) => {
    void loadImages(images);
  },
);

function getView() {
  return map
    ? { center: { lat: map.getCenter().lat, lng: map.getCenter().lng }, zoom: map.getZoom() }
    : null;
}
function getPhaseCacheStats() {
  return phaseCache?.stats();
}
defineExpose({
  addResultOverlays,
  clearResultOverlays,
  flyToBounds,
  selectRegionById,
  getView,
  getPhaseCacheStats,
});

let appliedBrowserConfig = '';
function rebuildPhaseCache() {
  if (!map || !props.workspaceMode || props.selectionDisabled) return;
  const browserConfig = JSON.stringify(settingsState.value?.settings.browser);
  if (phaseCache && browserConfig === appliedBrowserConfig) return;
  appliedBrowserConfig = browserConfig;
  phaseCache?.dispose();
  phaseCache = new PhaseLayerCache(
    map,
    (state) => {
      imageryError.value = state.error;
      if (state.stale && !props.selectionDisabled) emit('phase-stale');
    },
    settingsState.value
      ? {
          inactiveLayers: settingsState.value.settings.browser.inactive_layers,
          inactiveBytes: settingsState.value.settings.browser.cache_mb * 1024 * 1024,
          idleMs: settingsState.value.settings.browser.idle_seconds * 1000,
        }
      : undefined,
    undefined,
    settingsState.value?.settings.browser.concurrency || 4,
  );
  loadActivePhase();
}
watch(
  [() => JSON.stringify(settingsState.value?.settings.browser), () => props.selectionDisabled],
  rebuildPhaseCache,
);

onMounted(() => {
  if (!mapRef.value) return;
  const m = L.map(mapRef.value, {
    ...LOCAL_MAP_OPTIONS,
    doubleClickZoom: false,
  });
  map = m;
  contextMap.value = m;
  regionNavigation = new RegionNavigation(m);
  rebuildPhaseCache();
  // Parent stacking contexts prevent even long timelines crossing layer groups.
  m.createPane('cdp-images').style.zIndex = '300';
  m.createPane('cdp-results').style.zIndex = '500';
  const request = new AbortController();
  navigationRequest = request;
  getRegionData<RegionCollection>('/py/local/regions', request.signal)
    .then((data) => {
      if (map === m) installRegions(data);
    })
    .catch((error) => {
      if (map === m && !request.signal.aborted) boundaryError.value = String(error);
    });
  const areaPane = m.createPane('cdp-area');
  areaPane.style.zIndex = '700';
  areaPane.style.pointerEvents = 'none';
  // Edge hit targets stay below all vertex/midpoint markers, at every latitude.
  m.createPane('cdp-edit-edges').style.zIndex = '710';
  m.createPane('cdp-edit').style.zIndex = '720';
  showSelection(props.selectionBounds);
  syncSelectionEditor();
  m.on('move zoom resize', updateResultAnchor);
  m.on('dblclick', (event: L.LeafletMouseEvent) => {
    if (isOverview.value || polygonMode.value || props.selectionDisabled) return;
    // The mask is non-interactive: test geography, not the underlying city path.
    const point: Position = [event.latlng.lng, event.latlng.lat];
    if (countyRegions.value.some(feature => regionCoversPoint(feature.geometry, point))) return;
    returnToProvince();
  });
  const container = m.getContainer();
  container.addEventListener('pointerdown', onAreaPointerDown);
  container.addEventListener('pointermove', onAreaPointerMove);
  container.addEventListener('pointerup', onAreaPointerUp);
  container.addEventListener('pointercancel', onAreaPointerCancel);
  container.addEventListener('click', onPolygonClick);
  container.addEventListener('dblclick', onPolygonDoubleClick);
  window.addEventListener('keydown', onAreaKeydown);
  // KeepAlive detaches the map on route changes. Its zero size must not change
  // zoom limits and trigger a province navigation that resets the workspace.
  resizeObserver = new ResizeObserver(() => {
    if (hasVisibleMap()) m.invalidateSize();
  });
  resizeObserver.observe(container);
  void loadImages(props.images);
  if (props.workspaceMode) loadActivePhase();
});

onActivated(() => {
  contextActive.value = true;
  window.addEventListener('keydown', onAreaKeydown);
  void nextTick(() => {
    if (!hasVisibleMap()) return;
    resizeObserver?.observe(map!.getContainer());
    map?.invalidateSize();
    toggleNavigation();
    updateSelectionPixels();
  });
});
onDeactivated(() => {
  contextActive.value = false;
  interruptNavigation();
  window.removeEventListener('keydown', onAreaKeydown);
  resizeObserver?.disconnect();
  setDrawMode(false);
  phaseCache?.suspend();
  clearTimeout(pixelTimer);
  pixelRequest?.abort();
});

onUnmounted(() => {
  contextMap.value = null;
  navigationRequest?.abort();
  countyBoundaryRequest?.abort();
  clearTimeout(pixelTimer);
  pixelRequest?.abort();
  cancelNavigation();
  regionNavigation?.dispose();
  regionNavigation = null;
  phaseCache?.dispose();
  phaseCache = null;
  regionRequest?.abort();
  resizeObserver?.disconnect();
  imageGeneration++;
  window.removeEventListener('keydown', onAreaKeydown);
  clearDraftLayers();
  if (map) {
    const container = map.getContainer();
    container.removeEventListener('pointerdown', interruptNavigation, true);
    container.removeEventListener('wheel', interruptNavigation, true);
    container.removeEventListener('keydown', interruptNavigation, true);
    container.removeEventListener('pointerdown', onAreaPointerDown);
    container.removeEventListener('pointermove', onAreaPointerMove);
    container.removeEventListener('pointerup', onAreaPointerUp);
    container.removeEventListener('pointercancel', onAreaPointerCancel);
    container.removeEventListener('click', onPolygonClick);
    container.removeEventListener('dblclick', onPolygonDoubleClick);
    map.remove();
    map = null;
  }
});
</script>

<template>
  <div
    data-map-workspace
    class="w-full h-full relative overflow-hidden flex flex-col"
    :class="{ 'region-hover-enabled': !drawMode, 'region-overview': isOverview }"
  >
    <section class="shrink-0 p-2 text-xs divider-b" aria-label="影像来源导航">
      <div class="flex flex-wrap items-center gap-2">
        <button
          class="btn-secondary px-2 py-1"
          :disabled="selectionDisabled"
          @click="returnToProvince()"
        >
          河北全省
        </button>
        <select
          aria-label="选择影像来源"
          class="region-select"
          :disabled="selectionDisabled"
          :value="activeRegion?.id || ''"
          @change="
            selectRegion(
              regions.find((feature) => feature.id === ($event.target as HTMLSelectElement).value),
            )
          "
        >
          <option value="" disabled>选择影像来源</option>
          <option v-for="feature in regions" :key="feature.id" :value="feature.id">
            {{ feature.properties.name }}
          </option>
        </select>
        <select v-if="activeRegion && workspaceMode" aria-label="选择县级行政区" class="region-select"
          :disabled="selectionDisabled || !countyRegions.length" :value="administrativeRegion?.id || ''"
          @change="chooseCounty(countyRegions.find(f => f.id === ($event.target as HTMLSelectElement).value) || null)">
          <option value="">全部县区</option>
          <option v-for="county in countyRegions" :key="county.id" :value="county.id">{{ county.properties.name }}</option>
        </select>
        <button v-if="administrativeRegion" class="btn-secondary px-2 py-1" :disabled="selectionDisabled" @click="emit('use-admin-boundary')">以{{ administrativeRegion.properties.name }}边界作为检测范围</button>
        <span v-if="countyBoundaryError" role="alert" class="text-rose-400">{{ countyBoundaryError }}</span>
        <select
          v-if="!workspaceMode && regionYears.length"
          aria-label="影像年份"
          class="region-select"
          :value="regionYear"
          @change="loadRegionImagery(Number(($event.target as HTMLSelectElement).value))"
        >
          <option v-for="year in regionYears" :key="year" :value="year">{{ year }} 年</option>
        </select>
      </div>
    </section>
    <div
      class="relative flex-1 min-h-0"
      @dragover="acceptDrag"
      @drop="dropScene"
      @dragleave="dragOver = false"
    >
      <div ref="mapRef" class="absolute inset-0" />
      <MapContextControls :map="contextMap" :active="contextActive" :interactive="!drawMode" />
      <div
        v-if="dragOver"
        class="absolute inset-0 z-[1300] border-2 border-cyan-400 bg-cyan-400/10 flex items-center justify-center pointer-events-none"
      >
        <span class="panel p-3 text-cyan-300">松开以加入该时相</span>
      </div>
      <section
        v-if="selectedPatch"
        data-patch-details
        aria-label="变化图斑详情"
        class="absolute right-3 bottom-12 z-[1200] panel p-3 w-[360px] max-w-[calc(100%-24px)] max-h-[60%] overflow-y-auto text-xs"
      >
        <div class="flex items-center justify-between mb-2">
          <h3 class="panel-title">图斑详情</h3>
          <button
            class="btn-ghost px-2 py-1"
            aria-label="关闭图斑详情"
            @click="selectedPatch = null"
          >
            ×
          </button>
        </div>
        <dl class="grid grid-cols-[auto_1fr] gap-x-2 gap-y-1">
          <template v-for="[label, value] in selectedPatchRows" :key="label">
            <dt
              class="font-semibold"
              :title="
                label === '平均变化评分'
                  ? '当前图斑内有效变化像元的平均模型变化评分；不是整图斑正确率。'
                  : undefined
              "
            >
              {{ label }}
            </dt>
            <dd class="min-w-0 break-words">{{ value }}</dd>
          </template>
        </dl>
      </section>
      <p
        v-if="boundaryError || imageryError"
        class="absolute left-14 bottom-20 z-[1100] panel p-2 text-xs text-red-500 max-w-[calc(100%-80px)]"
      >
        {{ boundaryError || imageryError }}
      </p>
      <div
        v-if="!isOverview || drawMode"
        ref="toolbarRef"
        class="absolute right-3 top-3 z-[1100] panel p-2 flex flex-row flex-wrap md:flex-col gap-2 max-w-[calc(100%-64px)]"
        role="toolbar"
        aria-label="地图工具"
      >
        <button
          class="px-3 py-2 text-xs rounded"
          :class="drawMode && !polygonMode ? 'btn-primary' : 'btn-secondary'"
          :disabled="selectionDisabled"
          :aria-pressed="drawMode && !polygonMode"
          @click="setDrawMode(!drawMode || !!polygonMode)"
        >
          框选区域
        </button>
        <button
          class="px-3 py-2 text-xs rounded"
          :class="polygonMode === 'draw' ? 'btn-primary' : 'btn-secondary'"
          :disabled="selectionDisabled || !activeRegion"
          :aria-pressed="polygonMode === 'draw'"
          @click="startPolygon"
        >
          多边形区域
        </button>
        <template v-if="polygonMode === 'draw'">
          <button
            class="btn-primary px-3 py-2 text-xs"
            :disabled="selectionDisabled"
            @click="finishPolygon"
          >
            完成绘制
          </button>
          <button class="btn-secondary px-3 py-2 text-xs" @click="setDrawMode(false)">
            取消绘制
          </button>
        </template>
        <button v-if="drawMode" class="btn-secondary px-3 py-2 text-xs" @click="setDrawMode(false)">
          平移 / 缩放
        </button>
        <button
          class="btn-ghost text-xs"
          :disabled="selectionDisabled || !selectionBounds"
          @click="clearSelection"
        >
          清除选区
        </button>
      </div>
      <p
        v-if="drawMode || polygonMode === 'edit'"
        class="absolute bottom-6 left-1/2 -translate-x-1/2 z-[1100] panel px-3 py-2 text-xs w-max max-w-[calc(100%-24px)] text-center pointer-events-none"
      >
        {{
          polygonMode === 'draw'
            ? '单击加点 · 点击起点 / 双击 / Enter 闭合 · Backspace 撤销 · Esc 取消'
            : polygonMode === 'edit'
              ? '拖动顶点或边 · 点击边中点加点 · Delete 删除选中顶点 · 松开自动保存 · Esc 撤销当前拖动 · 空白处平移 / 滚轮缩放'
              : '拖动框选区域 · Esc 取消'
        }}
      </p>
      <p
        v-if="selectionMessage"
        class="absolute left-14 bottom-32 z-[1100] panel px-3 py-2 text-xs text-amber-300 pointer-events-none max-w-[calc(100%-80px)]"
        role="status"
      >
        {{ selectionMessage }}
      </p>
      <section
        v-if="!isOverview"
        ref="layersRef"
        class="absolute left-3 bottom-6 z-[1100] panel text-xs max-w-[calc(100%-24px)] flex flex-col max-h-[35%] md:max-h-[55%]"
        :class="layersExpanded ? 'w-[340px]' : ''"
        aria-label="图层控制"
      >
        <button
          class="w-full flex justify-between items-center gap-4 px-3 py-2 font-semibold"
          :aria-expanded="layersExpanded"
          @click="layersExpanded = !layersExpanded"
        >
          <span
            >图层 <span class="font-normal text-muted">{{ visibleLayerCount }} 个显示</span></span
          >
          <span aria-hidden="true">{{ layersExpanded ? '▾' : '▸' }}</span>
        </button>
        <div v-if="layersExpanded" class="px-3 pb-3 min-h-0 overflow-y-auto divider-t">
          <div class="flex justify-between gap-2 py-2">
            <button
              class="btn-secondary px-2 py-1"
              :aria-pressed="baseMapsVisible"
              @click="toggleBaseMaps"
            >
              {{ baseMapsVisible ? '隐藏底图' : '显示底图' }}
            </button>
            <button class="btn-ghost px-2 py-1" @click="resetLayerOrder">恢复时间顺序</button>
          </div>
          <p class="text-faint mb-2">从上到下叠放 · 检测结果始终位于底图上方</p>
          <h3 class="font-semibold mb-1">检测结果</h3>
          <p v-if="!resultEntries.length" class="text-faint py-2">检测完成后显示结果图层</p>
          <div
            v-for="(entry, index) in resultEntries"
            :key="entry.key"
            class="flex gap-2 py-2 divider-t items-start"
            :data-layer-key="entry.key"
          >
            <input
              v-model="entry.visible"
              type="checkbox"
              class="mt-1 shrink-0"
              :aria-label="`显示检测结果 ${entry.pair ? phaseDate(entry.pair.before) + ' → ' + phaseDate(entry.pair.after) : entry.key}`"
              @change="toggleResult(entry)"
            />
            <span class="w-3 h-3 mt-1 rounded shrink-0" :style="{ backgroundColor: entry.color }" />
            <div class="min-w-0 flex-1 break-words">
              <p class="font-semibold">
                {{
                  entry.pair
                    ? phaseDate(entry.pair.before) + ' → ' + phaseDate(entry.pair.after)
                    : '检测结果'
                }}
                <span
                  v-if="entry.pair"
                  class="ml-1 cursor-help font-normal text-faint"
                  tabindex="0"
                  :title="sourceName(entry.pair.before) + ' → ' + sourceName(entry.pair.after)"
                  :aria-label="sourceName(entry.pair.before) + ' → ' + sourceName(entry.pair.after)"
                  >ⓘ</span
                >
              </p>
              <p v-if="entry.pair?.warning" class="text-amber-300">{{ entry.pair.warning }}</p>
            </div>
            <div class="flex gap-1 shrink-0">
              <button
                class="btn-secondary px-1.5 py-1"
                aria-label="上移检测图层"
                :disabled="index === 0"
                @click="moveLayer('result', entry.key, -1)"
              >
                ↑
              </button>
              <button
                class="btn-secondary px-1.5 py-1"
                aria-label="下移检测图层"
                :disabled="index === resultEntries.length - 1"
                @click="moveLayer('result', entry.key, 1)"
              >
                ↓
              </button>
            </div>
          </div>
          <h3 class="font-semibold mt-3 mb-1">底图影像</h3>
          <p v-if="!imageEntries.length" class="text-faint py-2">
            {{ workspaceMode ? '使用工作区顶部标签切换时相' : '提交任务后显示该区域全部时相' }}
          </p>
          <div
            v-for="(entry, index) in imageEntries"
            :key="entry.key"
            class="flex gap-2 py-2 divider-t items-start"
            :data-layer-key="entry.key"
          >
            <div class="min-w-0 flex-1 break-words">
              <p class="font-semibold">
                {{ phaseDate(entry.image) }}
                <span v-if="activeImageKey === entry.key" class="ml-1 text-cyan-400 font-normal"
                  >当前底图</span
                >
              </p>
              <p class="text-muted">{{ sourceName(entry.image) }}</p>
              <p v-if="entry.loading" class="text-faint">加载中…</p>
              <p v-else-if="entry.error" class="text-red-500">{{ entry.error }}</p>
              <button v-else class="btn-ghost mt-1" @click="activateImage(entry)">
                设为当前底图
              </button>
            </div>
            <div class="flex gap-1 shrink-0">
              <button
                class="btn-secondary px-1.5 py-1"
                aria-label="上移底图"
                :disabled="index === 0"
                @click="moveLayer('image', entry.key, -1)"
              >
                ↑
              </button>
              <button
                class="btn-secondary px-1.5 py-1"
                aria-label="下移底图"
                :disabled="index === imageEntries.length - 1"
                @click="moveLayer('image', entry.key, 1)"
              >
                ↓
              </button>
            </div>
          </div>
          <div
            v-if="activeRegion && (currentTile || (workspaceMode && activePhase))"
            class="py-2 divider-t"
          >
            <span
              >{{ workspaceMode ? '当前时相影像' : '区域年度影像' }}
              <span class="text-faint"
                >{{ activeRegion.properties.name }} ·
                {{ workspaceMode ? (activePhase ? imageryPhaseLabel(activePhase) : '') : regionYear }}</span
              ></span
            >
          </div>
        </div>
      </section>
      <div
        v-if="resultAnchor && statsRows.length"
        ref="statsPanelRef"
        class="absolute z-[1050] panel p-1 text-xs max-w-[calc(100%-24px)] max-h-[40%]"
        :style="{ left: `${resultAnchor.x}px`, top: `${resultAnchor.y}px` }"
        role="status"
        aria-label="变化检测统计"
      >
        <div class="space-y-1">
          <div
            v-for="row in statsRows"
            :key="row.key"
            class="rounded px-2 py-1.5"
            :style="{ backgroundColor: row.color, color: colorText(row.color) }"
          >
            <p class="text-[10px] mb-0.5">
              {{ row.label }} <span v-if="!row.visible">· 已隐藏</span>
            </p>
            <p class="flex flex-wrap gap-x-3 gap-y-1 font-medium">
              <span>{{ row.count }} 变化图斑</span>
              <span>占比 {{ row.ratio }}</span>
              <span>置信度 {{ row.confidence }}</span>
            </p>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style>
.polygon-vertex {
  background: #22d3ee;
  border: 2px solid #fff;
  border-radius: 50%;
  cursor: pointer;
}
.polygon-vertex.selected {
  background: #f59e0b;
  box-shadow: 0 0 0 3px #f59e0b55;
}
.polygon-midpoint {
  background: #152330;
  border: 2px solid #22d3ee;
  border-radius: 50%;
  cursor: copy;
}
.polygon-edge-hit {
  cursor: move;
}
.selection-pixel-content {
  transform: translate(calc(-100% - 8px), 8px);
  width: max-content;
  max-width: 240px;
  padding: 7px 10px;
  background: #152330;
  color: #e0f2fe;
  border-radius: 0;
  white-space: pre-line;
  font-size: 12px;
  line-height: 1.5;
  font-variant-numeric: tabular-nums;
  pointer-events: none;
}
/* Native hover cannot leave stale highlights after fast movement or layer reordering. */
.region-hover-enabled [data-region-id]:hover {
  stroke: #67e8f9;
  stroke-width: 3;
  fill: #0891b2;
  fill-opacity: 0;
}
.region-hover-enabled.region-overview [data-region-id]:hover {
  fill-opacity: 0.28;
}
.region-label {
  color: #e0f2fe;
  font-size: 12px;
  font-weight: 600;
  text-align: center;
  white-space: nowrap;
  text-shadow:
    0 1px 3px #07121c,
    0 0 4px #07121c;
  pointer-events: none;
}
.region-select {
  background: var(--bg-panel, #152330);
  color: var(--text-main, #e0f2fe);
  padding: 4px;
  border-radius: 4px;
  max-width: 130px;
}
</style>
