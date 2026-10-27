import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import { test } from 'node:test';
import vm from 'node:vm';

const require = createRequire(new URL('../frontend/package.json', import.meta.url));
const ts = require('typescript');
const vue = require('vue');
const { parse, compileScript } = require('@vue/compiler-sfc');
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const area = { west: 114, south: 38, east: 115, north: 39 };
const image = (id, date = '2024-09-01') => ({ id, source: 'local', phase: '', capture_date: date,
  file_path: `${id}.tif`, bounds_south: 38, bounds_west: 114, bounds_north: 39, bounds_east: 115 });
const fourImages = () => [image('2026', '2026-05-01'), image('2024', '2024-09-01'),
  image('2023', '2023-11-01'), image('2025', '2025-11-01')];

function load(relativePath, api = {}) {
  const mounted = [], unmounted = [], timers = new Map();
  let timerId = 0;
  let source = fs.readFileSync(new URL(relativePath, import.meta.url), 'utf8');
  if (relativePath.endsWith('.vue')) source = compileScript(parse(source).descriptor, { id: 'workflow-test' }).content;
  const output = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true },
  }).outputText;
  const exports = {};
  const sandbox = {
    exports, console, Error, Blob, AbortController, crypto: globalThis.crypto,
    window: { addEventListener() {}, removeEventListener() {} },
    ResizeObserver: class { observe() {} disconnect() {} },
    require: name => {
      if (name === 'vue') return { ...vue, onActivated() {}, onDeactivated() {}, onMounted: callback => mounted.push(callback), onUnmounted: callback => unmounted.push(callback) };
      if (name === '../utils/settings') return { settingsState: vue.ref(), models: vue.ref([]), refreshSettings: async () => {} };
      // Navigation is independently exercised by real Leaflet browser tests.
      if (name === '../utils/regionNavigation') return { RegionNavigation: class { setRegion() {} suspend() {} resume() {} dispose() {} } };
      // This VM exercises MapBasemap state without rendering its presentation children.
      if (name === './MapContextControls.vue') return {};
      if (name === '../utils/api') return { ...api };
      if (name === '../utils/timeline') return load('../frontend/src/utils/timeline.ts', api).exports;
      if (name === '../utils/results') return load('../frontend/src/utils/results.ts', api).exports;
      if (name === '../utils/mapLayers') return load('../frontend/src/utils/mapLayers.ts', api).exports;
      if (name === '../utils/selectionGeometry') return load('../frontend/src/utils/selectionGeometry.ts', api).exports;
      if (name === '../utils/phaseLayerCache') return load('../frontend/src/utils/phaseLayerCache.ts', api).exports;
      if (name === './tileScheduler') return load('../frontend/src/utils/tileScheduler.ts', api).exports;
      if (name === './tilePreview') return load('../frontend/src/utils/tilePreview.ts', api).exports;
      if (name === '../utils/regions') return { getRegionData: () => new Promise(() => {}), displaySourceLayer: () => api.leaflet.layerGroup() };
      if (name === './regionView') return load('../frontend/src/utils/regionView.ts', api).exports;
      if (name === '../utils/province' || name === './province') return load('../frontend/src/utils/province.ts', api).exports;
      if (name === '../utils/basemap' || name === './basemap') return { getLocalCatalog: async () => ({ images: [], warning: null }),
        addCoverage() {}, LOCAL_MAP_OPTIONS: {} };
      if (name === 'leaflet') return api.leaflet;
      if (name.endsWith('.css')) return {};
      return require(name);
    },
    setTimeout: callback => { const id = ++timerId; timers.set(id, callback); return id; },
    clearTimeout: id => timers.delete(id),
  };
  vm.runInNewContext(output, sandbox, { filename: relativePath });
  return { exports, mounted, unmounted, timers, async poll() {
    const next = timers.entries().next().value;
    assert.ok(next, 'a poll must have been scheduled');
    const [id, callback] = next;
    timers.delete(id);
    await callback();
    await new Promise(resolve => setImmediate(resolve));
  } };
}

function detection(api = {}) {
  let nextTask = 0;
  const harness = load('../frontend/src/composables/useDetection.ts', {
    fetchLocalImagery: async () => ({ items: fourImages(), count: 4 }),
    createTask: async () => ({ id: `task-${++nextTask}`, status: 'pending' }),
    getTask: async id => ({ id, status: 'succeeded', progress: 100 }),
    getTaskResult: async () => ({ mask_path: 'mask.tif', stats_json: '{"changed_pixels":0,"change_ratio":0}' }),
    getMaskOverlay: async () => ({ png_b64: 'mask', bounds: [38, 114, 39, 115] }),
    downloadTaskResult: async () => new Blob(['{"type":"FeatureCollection","features":[]}']),
    ...api,
  });
  return { ...harness, state: harness.exports.useDetection() };
}
const parameters = () => ({ algorithm: 'cva', params: { provider: 'cpu', threshold: 0.7 } });

test('explicit selected scenes reach retrieval and monthly mosaics create one pair', async () => {
  let request;
  const harness = detection({ fetchLocalImagery: async payload => {
    request = payload;
    return { items: [image('january', '2025-01-01'), image('may', '2025-05-01')], count: 2 };
  } });
  await harness.state.submitTask(parameters, area, { region_id: 'xiongan', scene_ids: ['january-source', 'may-fine', 'may-coarse'] });
  assert.equal(request.region_id, 'xiongan');
  assert.deepEqual([...request.scene_ids], ['january-source', 'may-fine', 'may-coarse']);
  assert.equal(harness.state.images.value.length, 2);
  assert.equal(harness.state.pairs.value.length, 1);
});

test('empty explicit source selection never retrieves all phases', async () => {
  let calls = 0;
  const harness = detection({ fetchLocalImagery: async () => { calls++; return { items: fourImages() }; } });
  await harness.state.submitTask(parameters, area, { region_id: 'xiongan', scene_ids: [] });
  assert.equal(calls, 0);
  assert.match(harness.state.taskError.value, /至少两个不同月份/);
});

test('polygon submission freezes coordinates and scene IDs before asynchronous retrieval', async () => {
  let captured;
  const request = deferred();
  const selection = vue.reactive({ region_id: 'xiongan', scene_ids: ['a', 'b'],
    aoi: { type: 'Polygon', coordinates: [[[114, 38], [115, 38], [114, 39], [114, 38]]] } });
  const original = JSON.stringify(selection);
  const { state } = detection({ fetchLocalImagery: payload => { captured = payload; return request.promise; } });
  const submitting = state.submitTask(parameters, area, selection);
  selection.aoi.coordinates[0][0][0] = 116;
  selection.scene_ids.push('c');
  assert.equal(JSON.stringify({ region_id: captured.region_id, scene_ids: captured.scene_ids, aoi: captured.aoi }), original);
  request.resolve({ items: fourImages().map(image => ({ ...image, meta_json: JSON.stringify({ region_id: captured.region_id, aoi: captured.aoi }) })), count: 4 });
  await submitting;
  assert.equal(state.pairs.value.length, 3);
  const overlay = await state.loadResultOverlay('task-1');
  assert.equal(JSON.stringify(overlay.aoi), JSON.stringify(captured.aoi));
});

test('polygon detection stops when an older crop server drops its AOI', async () => {
  let tasks = 0;
  const { state } = detection({ fetchLocalImagery: async () => ({ items: fourImages() }),
    createTask: async () => { tasks++; return { id: 'wrong-box' }; } });
  await state.submitTask(parameters, area, { region_id: 'xiongan', scene_ids: ['a', 'b'],
    aoi: { type: 'Polygon', coordinates: [[[114, 38], [115, 38], [114, 39], [114, 38]]] } });
  assert.equal(tasks, 0);
  assert.equal(state.inProgress.value, false);
  assert.match(state.taskError.value, /未确认多边形范围/);
});

test('trusted county geometry, holes and version survive asynchronous preparation and result overlays', async () => {
  let captured;
  const request=deferred();
  const selection=vue.reactive({region_id:'xiongan',admin_code:'130629',admin_version:'frozen-v1',scene_ids:['a','b'],
    aoi:{type:'MultiPolygon',coordinates:[[[[114,38],[115,38],[115,39],[114,39],[114,38]],[[114.2,38.2],[114.4,38.2],[114.2,38.4],[114.2,38.2]]],[[[115.1,38],[115.2,38],[115.1,38.1],[115.1,38]]]]}});
  const original=JSON.stringify(selection.aoi);
  const {state}=detection({fetchLocalImagery:payload=>{captured=payload;return request.promise;}});
  const submitting=state.submitTask(parameters,area,selection);
  selection.admin_version='changed';
  selection.aoi.coordinates[1][0][0][0]=116;
  assert.equal(captured.admin_version,'frozen-v1');
  assert.equal(JSON.stringify(captured.aoi),original);
  request.resolve({items:fourImages().map(image=>({...image,meta_json:JSON.stringify({region_id:captured.region_id,admin_code:captured.admin_code,admin_version:captured.admin_version,aoi:captured.aoi})})),count:4});
  await submitting;
  assert.equal(state.pairs.value.length,3);
  assert.equal(state.taskError.value,'');
  const overlay=await state.loadResultOverlay('task-1');
  assert.equal(JSON.stringify(overlay.aoi),original);
});

test('county preparation refuses a crop from another boundary version before creating tasks', async () => {
  let tasks=0;
  const aoi={type:'MultiPolygon',coordinates:[[[[114,38],[115,38],[114,39],[114,38]]]]};
  const {state}=detection({fetchLocalImagery:async()=>({items:fourImages().map(image=>({...image,meta_json:JSON.stringify({region_id:'xiongan',admin_code:'130629',admin_version:'old-v0',aoi})}))}),
    createTask:async()=>{tasks++;return {id:'wrong-version'};}});
  await state.submitTask(parameters,area,{region_id:'xiongan',admin_code:'130629',admin_version:'frozen-v1',scene_ids:['a','b'],aoi});
  assert.equal(tasks,0);
  assert.match(state.taskError.value,/未确认多边形范围/);
});

test('result parsing accepts optional histograms and rejects inconsistent sample counts', () => {
  const { parseDetectionStats, meanChangeScore } = load('../frontend/src/utils/results.ts').exports;
  const histogram = { bin_edges: Array.from({ length: 21 }, (_, i) => i / 20),
    changed_counts: [1, ...Array(19).fill(0)], all_valid_counts: [2, ...Array(19).fill(0)],
    changed_sample_count: 1, all_valid_sample_count: 2 };
  const stats = parseDetectionStats({ changed_pixels: 1, mean_confidence: 0, confidence_histograms: histogram });
  assert.equal(stats.confidence_histograms?.changed_sample_count, 1);
  const wrapped = parseDetectionStats({ stats: { changed_pixels: 1, confidence_histograms: histogram } });
  assert.equal(wrapped.confidence_histograms?.all_valid_sample_count, 2, 'wrapped stats retain histogram data');
  const legacyHistogram = { bin_edges: histogram.bin_edges, changed_counts: histogram.changed_counts, all_valid_counts: histogram.all_valid_counts };
  assert.equal(parseDetectionStats({ confidence_histograms: legacyHistogram }).confidence_histograms?.changed_sample_count, 1);
  assert.equal(meanChangeScore(stats), '0.000', 'real zero confidence is retained');
  assert.equal(parseDetectionStats({ confidence_histograms: { ...histogram, changed_sample_count: 9 } }).confidence_histograms, undefined);
  assert.equal(parseDetectionStats({ mean_confidence: .8 }).mean_confidence, .8, 'old results remain usable');
  assert.equal(meanChangeScore({ changed_pixels: 0, mean_confidence: 0 }), '—');
  assert.equal(meanChangeScore({}), '—');
});

test('annual imagery labels preserve declared temporal precision', () => {
  const { phaseDate } = load('../frontend/src/utils/timeline.ts').exports;
  assert.equal(phaseDate({ ...image('annual'), meta_json: JSON.stringify({ date_precision: 'year' }) }), '2024');
  assert.equal(phaseDate({ ...image('daily'), meta_json: JSON.stringify({ date_precision: 'day' }) }), '2024-09-01');
  assert.equal(phaseDate(image('legacy')), '2024-09');
});

test('workspace phase labels hide unknown months only for exclusively annual scenes', () => {
  const { imageryPhaseLabel } = load('../frontend/src/utils/timeline.ts').exports;
  const phase = { month: '2023-01', scenes: [{ date_precision: 'year' }, { date_precision: 'year' }] };
  assert.equal(imageryPhaseLabel(phase), '2023年（月份未知）');
  assert.equal(phase.month, '2023-01', 'display formatting preserves the phase request key');
  assert.equal(imageryPhaseLabel({ ...phase, scenes: [{ date_precision: 'month' }] }), '2023-01');
  assert.equal(imageryPhaseLabel({ ...phase, scenes: [{ date_precision: 'day' }] }), '2023-01');
  assert.equal(imageryPhaseLabel({ ...phase, scenes: [{ date_precision: 'year' }, { date_precision: 'month' }] }), '2023-01');
  assert.equal(imageryPhaseLabel({ ...phase, scenes: [] }), '2023-01');
});

test('hotspots rank by measured area with stable ties and do not mutate input', () => {
  const { rankedPatches, patchBounds } = load('../frontend/src/utils/results.ts').exports;
  const feature = (id, area) => ({ id, properties: { area_m2: area }, geometry: { type: 'Polygon', coordinates: [[[114, 38], [115, 38], [115, 39], [114, 38]]] } });
  const features = [feature('patch-10', 200), feature('patch-2', 200), feature('patch-1', 100)];
  const ranked = rankedPatches({ geojson: { features } });
  assert.equal(ranked.map(f => f.id).join(','), 'patch-2,patch-10,patch-1');
  assert.equal(features[0].id, 'patch-10');
  assert.equal(patchBounds(features[0]).join(','), '38,114,39,115');
});

test('inline patch package reaches the map and completed pair without another download', async () => {
  const geojson = { type: 'FeatureCollection', schema_version: 2, features: [
    { type: 'Feature', id: 'patch-1', bbox: [114, 38, 114.01, 38.01],
      geometry: { type: 'Polygon', coordinates: [[[114, 38], [114.01, 38], [114.01, 38.01], [114, 38]]] },
      properties: { patch_id: 'patch-1', area_pixels: 4, area_m2: 455.21, area_ha: 0.045521,
        perimeter_m: 100, centroid: [114.005, 38.005], confidence_mean: 0.9 } },
    { type: 'Feature', id: 'patch-2', geometry: { type: 'MultiPolygon', coordinates: [] },
      properties: { area_pixels: 6, area_m2: 800, confidence_mean: null } },
  ] };
  let downloads = 0, delivered;
  const { state, poll } = detection({
    getTaskResult: async () => ({ geojson, stats_json: '{"feature_count":2,"changed_area_pixels":10,"changed_area_m2":1255.21,"mean_confidence_all":0.1}' }),
    downloadTaskResult: async () => { downloads++; throw new Error('inline results should not be downloaded'); },
  });
  state.onResult(data => { delivered = data; });
  await state.submitTask(parameters, area);
  await poll();
  assert.equal(downloads, 0);
  assert.equal(delivered.geojson, geojson);
  assert.equal(state.pairs.value[0].geojson.features.length, 2);
  assert.equal(state.pairs.value[0].geojson.features[0].properties.area_m2, 455.21);
  assert.equal(state.pairs.value[0].geojson.features[1].properties.confidence_mean, null);
  assert.equal(delivered.stats.changed_area_m2, 1255.21);
  assert.equal(delivered.stats.changed_area_pixels, 10);
  assert.equal(delivered.stats.mean_confidence_all, 0.1);
});

test('one submission retrieves all phases without a cap and enforces default model/CUDA', async () => {
  let retrieval, submitted;
  const { state } = detection({ fetchLocalImagery: async payload => {
    retrieval = payload; return { items: fourImages(), count: 4 };
  }, createTask: async payload => { submitted = payload; return { id: 'task', status: 'pending' }; } });
  await state.submitTask(parameters, area);
  assert.deepEqual(Array.from(retrieval.bounds), [114, 38, 115, 39]);
  assert.deepEqual(Object.keys(retrieval), ['progress_id', 'bounds', 'model_id', 'config_revision', 'params']);
  assert.equal(submitted.algorithm, 'hebei_onnx');
  assert.equal(submitted.params.provider, 'cuda');
  assert.equal(submitted.params.threshold, 0.7);
  assert.equal(submitted.before_id, '2023');
  assert.equal(submitted.after_id, '2024');
});

test('four unordered phases create exactly three adjacent tasks sequentially and retain colors', async () => {
  const submitted = [], delivered = [], colors = [];
  const { state, poll } = detection({ createTask: async payload => {
    submitted.push([payload.before_id, payload.after_id]);
    return { id: `task-${submitted.length}`, status: 'pending' };
  }, getMaskOverlay: async (_path, _alpha, color) => {
    colors.push(color); return { png_b64: color, bounds: [38, 114, 39, 115] };
  } });
  state.onResult(data => delivered.push(data));
  await state.submitTask(parameters, area);
  assert.deepEqual(submitted, [['2023', '2024']]);
  await poll();
  assert.deepEqual(submitted, [['2023', '2024'], ['2024', '2025']]);
  assert.equal(delivered.length, 1);
  assert.equal(state.isSuccess.value, false);
  assert.equal(state.inProgress.value, true);
  await poll();
  assert.equal(submitted.length, 3);
  await poll();
  assert.deepEqual(submitted, [['2023', '2024'], ['2024', '2025'], ['2025', '2026']]);
  assert.equal(new Set(colors).size, 3);
  assert.equal(delivered.length, 3);
  assert.equal(state.taskProgress.value, 100);
  assert.equal(state.isSuccess.value, true);
  assert.equal(state.inProgress.value, false);
  assert.equal(state.beforeImage.value.phase, 'before');
  assert.equal(state.afterImage.value.phase, 'after');
  assert.ok(state.pairs.value.every(pair => pair.status === 'succeeded'));
});

test('pending and running polls do not submit the next pair or mark the batch complete', async () => {
  let calls = 0, polls = 0;
  const { state, poll } = detection({ createTask: async () => { calls++; return { id: 'task', status: 'pending' }; },
    getTask: async () => ({ id: 'task', status: ++polls === 1 ? 'pending' : 'running', progress: 50 }) });
  await state.submitTask(parameters, area);
  await poll(); await poll();
  assert.equal(calls, 1);
  assert.equal(state.isSuccess.value, false);
  assert.ok(Math.abs(state.taskProgress.value - (20 + 80 / 6)) < 1e-9);
});

test('next task starts while previous preview is still rendering', async () => {
  const rendering = deferred();
  let calls = 0;
  const { state, poll } = detection({ createTask: async () => ({ id: `task-${++calls}`, status: 'pending' }) });
  state.onResult(() => rendering.promise);
  await state.submitTask(parameters, area);
  const polling = poll();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls, 2);
  assert.equal(state.inProgress.value, true);
  rendering.resolve(); await polling;
  assert.equal(calls, 2);
});

test('failed pair stops the remaining sequence and preserves completed results', async () => {
  let calls = 0;
  const { state, poll } = detection({ createTask: async () => ({ id: `task-${++calls}`, status: 'pending' }),
    getTask: async id => ({ id, status: id === 'task-1' ? 'succeeded' : 'failed', error_msg: 'GPU failed' }) });
  await state.submitTask(parameters, area);
  await poll(); await poll();
  assert.equal(calls, 2);
  assert.deepEqual(Array.from(state.pairs.value, pair => pair.status), ['succeeded', 'failed', 'waiting']);
  assert.equal(state.taskError.value, 'GPU failed');
  assert.equal(state.isSuccess.value, false);
  assert.equal(state.inProgress.value, false);
});

test('changing the area ignores an in-flight retrieval', async () => {
  const request = deferred();
  let tasks = 0;
  const { state } = detection({ fetchLocalImagery: () => request.promise, createTask: () => { tasks++; } });
  const submitting = state.submitTask(parameters, area);
  state.resetDetection();
  request.resolve({ items: fourImages(), count: 4 });
  await submitting;
  assert.equal(tasks, 0);
  assert.equal(state.images.value.length, 0);
  assert.equal(state.taskStatus.value, '');
});

test('changing the area ignores an in-flight task submission', async () => {
  const request = deferred();
  const { state } = detection({ createTask: () => request.promise });
  const submitting = state.submitTask(parameters, area);
  await new Promise(resolve => setImmediate(resolve));
  state.resetDetection();
  request.resolve({ id: 'obsolete', status: 'pending' });
  await submitting;
  assert.equal(state.currentTaskId.value, '');
  assert.equal(state.beforeImage.value, null);
  assert.equal(state.taskStatus.value, '');
});

test('an obsolete polling response cannot restore the old batch or load results', async () => {
  const response = deferred();
  let resultCalls = 0;
  const { state, poll } = detection({ getTask: () => response.promise, getTaskResult: () => { resultCalls++; } });
  await state.submitTask(parameters, area);
  const polling = poll();
  state.resetDetection();
  response.resolve({ id: 'old', status: 'succeeded' });
  await polling;
  assert.equal(state.taskStatus.value, '');
  assert.equal(resultCalls, 0);
});

test('empty, single-phase, invalid-date, and failed retrievals never submit detection', async () => {
  for (const reply of [{ items: [], count: 0 }, { items: [image('only')], count: 1 },
    { items: [image('bad', 'invalid')], count: 1 }, new Error('retrieval failed')]) {
    let tasks = 0;
    const { state } = detection({ fetchLocalImagery: async () => { if (reply instanceof Error) throw reply; return reply; },
      createTask: () => { tasks++; } });
    await state.submitTask(parameters, area);
    assert.equal(tasks, 0);
    assert.equal(state.taskStatus.value, 'failed');
    assert.equal(state.inProgress.value, false);
    assert.ok(state.taskError.value);
  }
});

test('duplicate submissions are blocked throughout retrieval and sequence', async () => {
  let fetches = 0;
  const request = deferred();
  const { state } = detection({ fetchLocalImagery: () => { fetches++; return request.promise; } });
  const submitting = state.submitTask(parameters, area);
  await state.submitTask(parameters, area);
  request.resolve({ items: fourImages(), count: 4 });
  await submitting;
  await state.submitTask(parameters, area);
  assert.equal(fetches, 1);
});

test('a replaced AI session cannot create a task after image preparation completes', async () => {
  let release, current = true, created = 0;
  const harness = detection({
    fetchLocalImagery: () => new Promise(resolve => { release = resolve; }),
    createTask: async () => { created++; return { id: 'unexpected', status: 'pending' }; },
  });
  const pending = harness.state.submitTask(parameters, area, undefined, () => current);
  current = false;
  release({ items: fourImages(), count: 4 });
  await pending;
  assert.equal(created, 0);
  assert.equal(harness.state.inProgress.value, false);
  assert.equal(harness.state.currentTaskId.value, '');
});

test('no selected area or unavailable model produces no retrieval', async () => {
  let fetches = 0;
  const { state } = detection({ fetchLocalImagery: () => { fetches++; } });
  await state.submitTask(parameters, null);
  assert.match(state.taskError.value, /区域/);
  await state.submitTask(() => undefined, area);
  assert.match(state.taskError.value, /模型/);
  assert.equal(fetches, 0);
});

test('zero-change statistics remain available without a mask overlay', async () => {
  const { state } = detection({ getTaskResult: async () => ({ stats_json: '{"changed_pixels":0,"change_ratio":0,"mean_confidence":0}' }) });
  const result = await state.loadResultOverlay('task');
  assert.equal(result.stats.changed_pixels, 0);
  assert.equal(result.stats.change_ratio, 0);
  assert.equal(result.geojson.type, 'FeatureCollection');
});

test('obsolete result loading stops before requesting additional overlays', async () => {
  const request = deferred();
  let downloadCalls = 0;
  const { state } = detection({ getTaskResult: () => request.promise, downloadTaskResult: () => { downloadCalls++; } });
  const loading = state.loadResultOverlay('old');
  state.resetDetection();
  request.resolve({ stats_json: '{"changed_pixels":100}' });
  assert.equal(await loading, undefined);
  assert.equal(downloadCalls, 0);
});

test('same-date images are grouped and only overlapping adjacent phases are compared', () => {
  const { buildTimeline, sourceName, pairColor } = load('../frontend/src/utils/timeline.ts').exports;
  const a = image('a', '2023-11-01'), b = image('b', '2023-11-01');
  const c = image('c', '2024-09-01');
  const timeline = buildTimeline([c, b, a]);
  assert.deepEqual(Array.from(timeline.pairs, pair => [pair.before.id, pair.after.id]), [['a', 'c'], ['b', 'c']]);
  assert.equal(sourceName({ ...a, meta_json: '{"source_file":"鹿泉2311.tif"}' }), '鹿泉2311.tif');
  assert.equal(sourceName({ ...a, meta_json: '{"source_file":""}' }), 'a.tif');
  assert.equal(new Set(Array.from({ length: 30 }, (_, index) => pairColor(index))).size, 30);
  assert.throws(() => buildTimeline([a, { ...c, bounds_west: 116, bounds_east: 117 }]), /空间交集/);
});

function mockLeaflet() {
  const overlays = [], vectors = [];
  const panes = new Map();
  const point = { x: 10, y: 10 };
  const group = () => ({ layers: new Set(), addLayer(layer) { this.layers.add(layer); return this; },
    removeLayer(layer) { this.layers.delete(layer); return this; }, clearLayers() { this.layers.clear(); },
    addTo(target) { this.parent = target; target.addLayer(this); return this; },
    remove() { this.parent?.removeLayer(this); this.parent = null; } });
  const handler = { enable() {}, disable() {} };
  const container = { style: {}, addEventListener() {}, removeEventListener() {} };
  const map = { stop() {}, ...group(), dragging: handler, doubleClickZoom: handler, scrollWheelZoom: handler,
    boxZoom: handler, touchZoom: handler, getContainer: () => container,
    createPane: (name, parent) => { const pane = { style: {}, parent }; panes.set(name, pane); return pane; },
    getPane: name => panes.get(name),
    hasLayer(layer) { return this.layers.has(layer); }, on() {}, remove() {},
    getSize: () => ({ x: 800, y: 600 }),
    latLngToContainerPoint: () => point };
  return { overlays, vectors, point, panes, mapInstance: map, layerGroup: group, map: () => map, TileLayer: class {},
    latLngBounds: (southwest, northeast) => ({ getNorthEast: () => northeast, getSouthWest: () => southwest }),
    rectangle: bounds => ({ ...group(), getBounds: () => bounds }),
    imageOverlay: (url, _bounds, options) => { const layer = { ...group(), url, options }; overlays.push(layer); return layer; },
    geoJSON: (_geojson, options) => { const layer = { ...group(), options }; vectors.push(layer); return layer; },
  };
}

async function mapHarness(api = {}, props = vue.reactive({ selectionBounds: null, images: [] })) {
  const leaflet = mockLeaflet();
  const harness = load('../frontend/src/components/MapBasemap.vue', { leaflet,
    getImageOverlay: async () => ({ image_b64: 'image', bounds: [38, 114, 39, 115] }), ...api });
  const map = harness.exports.default.setup(props, { expose() {}, emit() {} });
  map.mapRef.value = {};
  for (const callback of harness.mounted) await callback();
  // These layer tests exercise a detailed detection view, after leaving the province overview.
  map.isOverview.value = false;
  return { ...harness, map, leaflet, props };
}

test('map retains every color summary and orders whole period layers chronologically', async () => {
  const { map, leaflet } = await mapHarness();
  const { pairs } = load('../frontend/src/utils/timeline.ts').exports.buildTimeline(fourImages());
  // Simulate results arriving out of order; zero changes remain a real summary.
  for (const pair of [pairs[1], pairs[0], pairs[2]]) map.addResultOverlays({ pair,
    maskDataUrl: pair.key, maskBounds: [38, 114, 39, 115],
    stats: { feature_count: pairs.indexOf(pair), change_ratio: 0.0017, mean_confidence: 0.9321 },
    geojson: { type: 'FeatureCollection', features: [] } });
  assert.equal(map.resultLayers.size, 3);
  assert.deepEqual(Array.from(map.resultEntries.value, entry => entry.key), Array.from(pairs, pair => pair.key).reverse());
  assert.deepEqual(Array.from(map.statsRows.value, row => row.count), ['2', '1', '0']);
  assert.ok(map.statsRows.value.every(row => row.ratio === '0.17%' && row.confidence === '0.9321'));
  for (const entry of map.resultEntries.value) {
    const raster = leaflet.overlays.find(layer => layer.url === entry.key);
    const vector = leaflet.vectors.find(layer => layer.options.style.color === entry.color);
    assert.equal(raster.options.pane, vector.options.pane);
  }
  const [newest, middle] = map.resultEntries.value;
  const z = entry => Number(leaflet.panes.get(entry.pane).style.zIndex);
  assert.ok(z(newest) > z(middle));
  newest.visible = false; map.toggleResult(newest);
  assert.equal(leaflet.mapInstance.hasLayer(map.resultLayers.get(newest.key)), false);
  assert.equal(map.statsRows.value.length, 3, 'hiding a layer must retain its statistics');
  assert.equal(map.statsRows.value[0].visible, false);
  newest.visible = true; map.toggleResult(newest);
  assert.equal(leaflet.mapInstance.hasLayer(map.resultLayers.get(newest.key)), true);
  map.moveLayer('result', newest.key, 1);
  assert.ok(z(middle) > z(newest));
  map.resetLayerOrder();
  assert.ok(z(newest) > z(middle));
  map.clearResultOverlays();
  assert.equal(map.statsRows.value.length, 0);
  assert.equal(map.resultLayers.size, 0);
  assert.equal(map.resultAnchor.value, null);
});

test('base map toggle preserves the selected imagery and keeps results independent', async () => {
  const { map, leaflet } = await mapHarness();
  await map.loadImages(fourImages());
  assert.deepEqual(Array.from(map.imageEntries.value, entry => entry.key), ['2026', '2025', '2024', '2023']);
  const { pairs } = load('../frontend/src/utils/timeline.ts').exports.buildTimeline(fourImages());
  for (const pair of pairs) map.addResultOverlays({ pair, maskDataUrl: pair.key, maskBounds: [38, 114, 39, 115] });
  const oldest = map.imageEntries.value.find(entry => entry.key === '2023');
  map.activateImage(oldest);
  assert.equal(map.activeImageKey.value, '2023');
  assert.equal(map.navigationVisible.value, false);
  assert.equal(map.imageEntries.value.filter(entry => entry.visible).length, 1);
  assert.ok(map.resultEntries.value.every(entry => leaflet.mapInstance.hasLayer(map.resultLayers.get(entry.key))));
  const z = entry => Number(leaflet.panes.get(entry.pane).style.zIndex);
  assert.ok(map.resultEntries.value.every(entry => leaflet.panes.get(entry.pane).parent === leaflet.panes.get('cdp-results')));
  assert.ok(map.imageEntries.value.every(entry => leaflet.panes.get(entry.pane).parent === leaflet.panes.get('cdp-images')));
  assert.ok(Number(leaflet.panes.get('cdp-results').style.zIndex) > Number(leaflet.panes.get('cdp-images').style.zIndex));
  map.moveLayer('image', oldest.key, 1);
  assert.ok(z(map.imageEntries.value[0]) > z(oldest));
  map.toggleBaseMaps();
  assert.equal(map.baseMapsVisible.value, false);
  assert.equal(map.activeImageKey.value, '2023');
  assert.ok([...map.imageLayers.values()].every(group => !leaflet.mapInstance.hasLayer(group)));
  assert.equal(leaflet.mapInstance.layers.size, map.resultLayers.size + 1, 'only results and the result extent remain');
  assert.equal(map.visibleLayerCount.value, map.resultEntries.value.length);
  const newer = map.imageEntries.value.find(entry => entry.key === '2024');
  map.activateImage(newer);
  assert.equal(map.baseMapsVisible.value, false, 'choosing imagery must preserve the hidden state');
  assert.equal(leaflet.mapInstance.hasLayer(map.imageLayers.get(newer.key)), false);
  map.toggleBaseMaps();
  assert.equal(map.baseMapsVisible.value, true);
  assert.equal(leaflet.mapInstance.hasLayer(map.imageLayers.get(newer.key)), true);
  assert.equal(leaflet.mapInstance.hasLayer(map.imageLayers.get(oldest.key)), false);
  map.activateImage(oldest);
  assert.equal(leaflet.mapInstance.hasLayer(map.imageLayers.get(oldest.key)), true);
  map.resetLayerOrder();
  assert.equal(map.imageEntries.value[0].key, '2026');
});

test('a later completed period does not undo the custom order of existing result layers', async () => {
  const { map } = await mapHarness();
  const { pairs } = load('../frontend/src/utils/timeline.ts').exports.buildTimeline(fourImages());
  for (const pair of pairs.slice(0, 2)) map.addResultOverlays({ pair });
  map.moveLayer('result', pairs[0].key, -1);
  map.addResultOverlays({ pair: pairs[2] });
  assert.deepEqual(Array.from(map.resultEntries.value, entry => entry.key), [pairs[2].key, pairs[0].key, pairs[1].key]);
});

test('visibility changes during image loading survive completion and obsolete batches cannot restore imagery', async () => {
  const request = deferred();
  const { map, leaflet, unmounted } = await mapHarness({ getImageOverlay: () => request.promise });
  const loading = map.loadImages([image('old')]);
  map.toggleBaseMaps();
  request.resolve({ image_b64: 'image', bounds: [38, 114, 39, 115] });
  await loading;
  assert.equal(leaflet.mapInstance.hasLayer(map.imageLayers.get('old')), false);
  map.toggleBaseMaps();
  assert.equal(leaflet.mapInstance.hasLayer(map.imageLayers.get('old')), true);
  const obsolete = deferred();
  const newer = await mapHarness({ getImageOverlay: () => obsolete.promise });
  const pending = newer.map.loadImages([image('obsolete')]);
  newer.props.images = [];
  await vue.nextTick();
  obsolete.resolve({ image_b64: 'image', bounds: [38, 114, 39, 115] });
  await pending;
  assert.equal(newer.map.imageLayers.size, 0);
  assert.equal(newer.map.imageEntries.value.length, 0);
  for (const callback of unmounted) callback();
});

test('a failed base image stays identifiable while other images and result layers remain usable', async () => {
  const { map } = await mapHarness({ getImageOverlay: async path => {
    if (path === 'bad.tif') throw new Error('render failed');
    return { image_b64: 'image', bounds: [38, 114, 39, 115] };
  } });
  await map.loadImages([image('bad'), image('good')]);
  assert.equal(map.imageEntries.value.find(entry => entry.key === 'bad').error, 'render failed');
  assert.equal(map.imageLayers.size, 1);
  assert.equal(map.activeImageKey.value, 'good');
});

test('reloading a result replaces its summary while preserving visibility and user ordering', async () => {
  const { map, leaflet } = await mapHarness();
  const { pairs } = load('../frontend/src/utils/timeline.ts').exports.buildTimeline(fourImages());
  for (const pair of pairs) map.addResultOverlays({ pair, resultBounds: [38, 114, 39, 115] });
  const entry = map.resultEntries.value[0];
  entry.visible = false; map.toggleResult(entry);
  map.moveLayer('result', entry.key, 1);
  const oldGroup = map.resultLayers.get(entry.key);
  map.addResultOverlays({ pair: entry.pair, resultBounds: [37, 113, 38, 114],
    stats: { change_ratio: 0, mean_confidence: 0 },
    geojson: { type: 'FeatureCollection', features: [{}, {}] } });
  assert.equal(map.resultEntries.value[1].key, entry.key);
  assert.equal(map.statsRows.value[1].count, '2');
  assert.equal(map.statsRows.value[1].ratio, '0.00%');
  assert.equal(map.statsRows.value[1].confidence, '0.0000');
  assert.equal(map.statsRows.value[1].visible, false);
  assert.equal(leaflet.mapInstance.hasLayer(oldGroup), false);
  assert.equal(map.resultLayers.size, 3);
  assert.deepEqual(Array.from(map.resultRect.getBounds().getSouthWest()), [37, 113]);
  assert.deepEqual(Array.from(map.resultRect.getBounds().getNorthEast()), [39, 115]);
});

test('long timelines stay below coverage panes and reuse allocated panes after resets', async () => {
  const { map, leaflet } = await mapHarness();
  await map.loadImages(Array.from({ length: 110 }, (_, index) => image(String(index))));
  const imageRanks = map.imageEntries.value.map(entry => Number(leaflet.panes.get(entry.pane).style.zIndex));
  assert.equal(new Set(imageRanks).size, 110);
  assert.ok(imageRanks.every(Number.isInteger), 'CSS z-index requires integers');
  assert.equal(leaflet.panes.get('cdp-results').style.zIndex, '500');
  await map.loadImages(fourImages());
  map.addResultOverlays({ resultBounds: [38, 114, 39, 115] });
  const paneCount = leaflet.panes.size;
  map.clearResultOverlays();
  await map.loadImages([]);
  await map.loadImages(fourImages());
  map.addResultOverlays({ resultBounds: [38, 114, 39, 115] });
  assert.equal(leaflet.panes.size, paneCount);
});

test('export selects completed pairs and remains available after a later pair fails', async () => {
  const harness = load('../frontend/src/components/ExportPanel.vue');
  const { pairs } = load('../frontend/src/utils/timeline.ts').exports.buildTimeline(fourImages());
  const props = vue.reactive({ pairs, taskId: 'running' });
  const panel = harness.exports.default.setup(props, { expose() {}, emit() {} });
  assert.equal(panel.exportTaskId.value, '');
  props.pairs[0].taskId = 'first'; props.pairs[0].status = 'succeeded';
  await vue.nextTick();
  assert.equal(panel.exportTaskId.value, 'first');
  props.pairs[1].taskId = 'failed'; props.pairs[1].status = 'failed';
  await vue.nextTick();
  assert.equal(panel.completedPairs.value.length, 1);
  assert.equal(panel.exportTaskId.value, 'first');
});

test('the statistics card stays inside the map and below an overlapping toolbar', async () => {
  const leaflet = mockLeaflet();
  leaflet.point.x = 740; leaflet.point.y = 150;
  const harness = load('../frontend/src/components/MapBasemap.vue', { leaflet });
  const map = harness.exports.default.setup({ selectionBounds: null }, { expose() {}, emit() {} });
  map.mapRef.value = {};
  for (const callback of harness.mounted) await callback();
  map.statsPanelRef.value = { offsetWidth: 200, offsetHeight: 180 };
  map.toolbarRef.value = { offsetLeft: 700, offsetTop: 12, offsetWidth: 80, offsetHeight: 100 };
  map.addResultOverlays({ resultBounds: [38, 114, 39, 115] });
  assert.ok(map.resultAnchor.value.x + 200 <= 800 - 12);
  assert.ok(map.resultAnchor.value.y >= 12 + 100 + 8);
});


test('preparation progress is observable before fetch completes and handoff stays monotonic', async () => {
  const fetch = deferred();
  let preparationCalls = 0;
  const {state, poll} = detection({fetchLocalImagery: () => fetch.promise,
    getLocalProgress: async () => ({status:'running',stage:'mosaicking',progress: ++preparationCalls === 1 ? 50 : 30})});
  const submitting = state.submitTask(parameters, area);
  await poll(); assert.equal(state.taskProgress.value,10); assert.equal(state.taskStage.value,'mosaicking');
  await poll(); assert.equal(state.taskProgress.value,10);
  fetch.resolve({items:fourImages(),count:4,batch_id:'batch'}); await submitting;
  assert.equal(state.taskProgress.value,20); assert.equal(state.taskStage.value,'queued');
});

test('late preparation reply and stale task errors cannot overwrite a replacement generation', async () => {
  const fetch = deferred(), progress = deferred();
  const {state,poll} = detection({fetchLocalImagery:()=>fetch.promise,getLocalProgress:()=>progress.promise});
  const submitting=state.submitTask(parameters,area), polling=poll();
  fetch.resolve({items:fourImages(),count:4,batch_id:'batch'}); await submitting;
  progress.resolve({status:'running',stage:'mosaicking',progress:99}); await polling;
  assert.equal(state.taskStage.value,'queued'); assert.equal(state.taskProgress.value,20);
  state.resetDetection(); assert.equal(state.taskProgress.value,0);
});

test('progress freezes on failure and never reaches 100 while task is running', async () => {
  let polls=0;
  const {state,poll}=detection({fetchLocalImagery:async()=>({items:fourImages().slice(0,2),count:2}),
    getTask:async()=>({id:'task-1',status:++polls<3?'running':'failed',stage:polls<3?'saving':'inference',progress:polls===1?99:polls===2?100:40,error_msg:'save failed'})});
  await state.submitTask(parameters,area);
  await poll(); const last=state.taskProgress.value;
  await poll(); assert.equal(state.taskProgress.value,last); assert.ok(last<100);
  await poll(); assert.equal(state.taskProgress.value,last); assert.equal(state.inProgress.value,false);
});

test('preparation progress network errors retry without aborting image preparation', async () => {
  const fetch=deferred();let calls=0;
  const {state,poll}=detection({fetchLocalImagery:()=>fetch.promise,getLocalProgress:async()=>{if(++calls===1)throw new Error('offline');return {status:'running',stage:'cropping',progress:60};}});
  const submitting=state.submitTask(parameters,area);
  await poll();assert.equal(state.inProgress.value,true);assert.match(state.taskError.value,/重试/);
  await poll();assert.equal(state.taskError.value,'');assert.equal(state.taskProgress.value,12);
  fetch.reject(new Error('crop failed'));await submitting;
  assert.equal(state.taskProgress.value,12);assert.equal(state.inProgress.value,false);
});
