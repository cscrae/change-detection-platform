// Exercise the real scheduler and preview queue without a browser, NAS or image decoding.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import test from 'node:test';
import vm from 'node:vm';

const require = createRequire(new URL('../frontend/package.json', import.meta.url));
const ts = require('typescript');
const flush = async () => { for (let i = 0; i < 4; i++) await new Promise(resolve => setImmediate(resolve)); };

function harness() {
  const events = new Map(), panes = new Map(), starts = [], requests = [], details = [];
  const counters = { active: 0, backgrounds: 0, maximum: 0, maxBackgrounds: 0 };
  const begin = kind => {
    starts.push(kind); counters.active++;
    if (kind !== 'detail') counters.backgrounds++;
    counters.maximum = Math.max(counters.maximum, counters.active);
    counters.maxBackgrounds = Math.max(counters.maxBackgrounds, counters.backgrounds);
    return () => { counters.active--; if (kind !== 'detail') counters.backgrounds--; };
  };
  const bounds = {
    getNorthWest: () => ({ lat: 38.12, lng: 114.3 }),
    getSouthEast: () => ({ lat: 38.1, lng: 114.32 }),
  };
  const map = {
    getPane: name => panes.get(name),
    createPane: name => { const pane = { style: {} }; panes.set(name, pane); return pane; },
    project: ({ lat, lng }, z) => {
      const x = (lng + 180) / 360 * 256 * 2 ** z, y = (90 - lat) / 180 * 256 * 2 ** z;
      return { x, y, divideBy: value => ({ x: x / value, y: y / value }) };
    },
    unproject: ({ x, y }, z) => ({ lat: 90 - y / (256 * 2 ** z) * 180, lng: x / (256 * 2 ** z) * 360 - 180 }),
    getZoom: () => 17, getCenter: () => ({ lat: 38.11, lng: 114.31 }), getBounds: () => bounds,
    on(names, callback, context) {
      for (const name of names.split(' ')) events.set(name, [...(events.get(name) || []), { callback, context }]);
    },
    off(names, callback, context) {
      for (const name of names.split(' ')) events.set(name, (events.get(name) || []).filter(item => item.callback !== callback || item.context !== context));
    },
    fire(name) { for (const item of events.get(name) || []) item.callback.call(item.context); },
  };
  const leaflet = {
    point: (x, y) => ({ x, y }), latLngBounds: (a, b) => [a, b],
    imageOverlay() {
      const handlers = new Map(), image = { style: {}, dataset: {}, removeAttribute() {} };
      return {
        on(name, callback) { handlers.set(name, callback); return this; },
        off() { handlers.clear(); return this; },
        addTo() { queueMicrotask(() => handlers.get('load')?.()); return this; },
        remove() { return this; }, getElement: () => image,
      };
    },
  };
  const pendingFetch = (url, { signal }) => {
    const coordinates = url.match(/\/(\d+)\/(\d+)\/(\d+)$/).slice(1).map(Number);
    const finish = begin(coordinates[0] <= 7 ? 'base' : 'ring');
    return new Promise((resolve, reject) => {
      let done = false;
      const settle = aborted => {
        if (done) return;
        done = true; signal.removeEventListener('abort', abort); finish();
        if (aborted) reject(new DOMException('Aborted', 'AbortError'));
        else resolve(new Response(new Uint8Array([1, 2, 3]), { status: 200 }));
      };
      const abort = () => settle(true);
      const request = { coordinates, complete: () => settle(false), get done() { return done; } };
      requests.push(request);
      signal.addEventListener('abort', abort, { once: true });
      if (signal.aborted) abort();
    });
  };
  const compiled = new Map();
  function load(name) {
    if (compiled.has(name)) return compiled.get(name);
    const source = fs.readFileSync(new URL(`../frontend/src/utils/${name}.ts`, import.meta.url), 'utf8');
    const output = ts.transpileModule(source, {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true },
    }).outputText;
    const exports = {};
    compiled.set(name, exports);
    vm.runInNewContext(output, {
      exports, console, AbortController, Blob, Response, URL, fetch: pendingFetch,
      setTimeout, clearTimeout, queueMicrotask,
      requestAnimationFrame: callback => setTimeout(callback, 0), cancelAnimationFrame: clearTimeout,
      require: dependency => {
        if (dependency === 'leaflet') return leaflet;
        if (dependency === './basemap') return { LOCAL_MAP_OPTIONS: { minZoom: 6 } };
        if (dependency === './tileScheduler') return load('tileScheduler');
        throw new Error(`Unexpected test dependency ${dependency}`);
      },
    }, { filename: `${name}.ts` });
    return exports;
  }
  const { TileScheduler } = load('tileScheduler');
  const { TilePreview } = load('tilePreview');
  const scheduler = new TileScheduler(map, () => {});
  const preview = new TilePreview(map, bounds, scheduler, ({ z, x, y }) => `/tiles/${z}/${x}/${y}`, () => {});
  preview.resume();
  const enqueueDetail = (priority = 0) => {
    let release;
    const wait = new Promise(resolve => { release = resolve; });
    details.push(release);
    scheduler.enqueue({}, { priority: () => priority, run: async () => { const finish = begin('detail'); await wait; finish(); } });
    return release;
  };
  return {
    map, scheduler, preview, starts, requests, counters, enqueueDetail,
    async dispose() { preview.dispose(); scheduler.dispose(); details.forEach(resolve => resolve()); await flush(); },
  };
}

test('initial visible detail is scheduled ahead of whole-footprint coarse previews', async () => {
  const h = harness();
  try {
    h.preview.update(false);
    const release = Array.from({ length: 6 }, () => h.enqueueDetail());
    h.scheduler.defer(); await flush();
    assert.deepEqual(h.starts, ['detail', 'detail', 'detail', 'detail']);
    release.slice(0, 4).forEach(resolve => resolve()); await flush();
    assert.deepEqual(h.starts.slice(4), ['detail', 'detail', 'base']);
    assert.equal(h.counters.backgrounds, 1);
    assert.ok(h.counters.maximum <= 4);
  } finally { await h.dispose(); }
});

test('an in-flight coarse preview reserves only one slot when a new viewport needs detail', async () => {
  const h = harness();
  try {
    h.preview.update(false); h.scheduler.defer(); await flush();
    assert.deepEqual(h.starts, ['base']);
    const release = Array.from({ length: 4 }, () => h.enqueueDetail()); await flush();
    assert.deepEqual(h.starts, ['base', 'detail', 'detail', 'detail']);
    release[0](); await flush();
    assert.equal(h.starts.at(-1), 'detail');
    assert.equal(h.counters.active, 4);
    assert.equal(h.counters.maxBackgrounds, 1);
  } finally { await h.dispose(); }
});

test('moving maps defer new preview work while visible detail can still run', async () => {
  const h = harness();
  try {
    h.map.fire('movestart'); h.preview.update(false);
    h.enqueueDetail(); h.enqueueDetail(); h.scheduler.defer(); await flush();
    assert.deepEqual(h.starts, ['detail', 'detail']);
    h.map.fire('moveend'); await new Promise(resolve => setTimeout(resolve, 5)); await flush();
    assert.equal(h.starts.at(-1), 'base');
    assert.equal(h.counters.maxBackgrounds, 1);
  } finally { await h.dispose(); }
});

test('base previews precede surrounding parents and cached parents remain reusable', async () => {
  const h = harness();
  try {
    h.preview.update(true); h.scheduler.defer(); await flush();
    assert.equal(h.requests.length, 1);
    assert.equal(h.requests[0].coordinates[0], 6);
    h.requests[0].complete(); await flush();
    assert.equal(h.requests[1].coordinates[0], 7);
    h.requests[1].complete(); await flush();
    const parent = h.requests[2];
    assert.equal(parent.coordinates[0], 16, 'surrounding previews stay one zoom level below current detail');
    parent.complete(); await flush();
    const [z, x, y] = parent.coordinates;
    const requestCount = h.requests.length;
    const cached = h.preview.claim({ z, x, y });
    assert.ok(cached instanceof Blob);
    assert.equal(cached.size, 3);
    assert.equal(h.requests.length, requestCount, 'claim uses retained bytes without a fresh request');
    assert.equal(h.counters.maxBackgrounds, 1);
    h.preview.suspend(); await flush();
    assert.equal(h.counters.active, 0, 'suspending aborts pending previews');
    assert.ok(h.preview.tileCount >= 3, 'decoded base and parent cache survives suspension');
  } finally { await h.dispose(); }
});
