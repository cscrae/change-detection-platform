import assert from 'node:assert/strict';
import test from 'node:test';
import { resolveBasemapConfig, normalizePoiCollection } from '../frontend/src/utils/mapContext.ts';
import { buildGraticule, formatCoordinates, formatDegree, MERCATOR_MAX_LATITUDE } from '../frontend/src/utils/graticule.ts';

const collection = (features, metadata = {}) => ({ type: 'FeatureCollection', features, ...metadata });
const point = (coordinates, properties = {}, id) => ({ type: 'Feature', geometry: { type: 'Point', coordinates }, properties, id });
const view = { width: 1000, height: 700 };

test('default XYZ context uses the official OSM endpoint and visible attribution metadata', () => {
  const config = resolveBasemapConfig();
  assert.equal(config.url, 'https://tile.openstreetmap.org/{z}/{x}/{y}.png');
  assert.equal(config.coordinateSystem, 'EPSG:3857');
  assert.equal(config.datum, 'WGS84');
  assert.match(config.attribution, /https:\/\/www\.openstreetmap\.org\/copyright/);
  assert.equal(config.tileOptions.attribution, config.attribution);
  assert.equal(config.tileOptions.referrerPolicy, 'strict-origin-when-cross-origin');
  assert.equal(config.tileOptions.noWrap, true);
});

test('custom providers must explicitly declare matching tile grid, datum and attribution', () => {
  const env = { VITE_BASEMAP_URL: '/tiles/{z}/{x}/{y}.png' };
  assert.throws(() => resolveBasemapConfig(env), /须声明/);
  env.VITE_BASEMAP_CRS = 'EPSG:3857';
  env.VITE_BASEMAP_DATUM = 'WGS84';
  assert.throws(() => resolveBasemapConfig(env), /来源署名/);
  env.VITE_BASEMAP_ATTRIBUTION = 'Provider <script>bad()</script>';
  const config = resolveBasemapConfig(env);
  assert.equal(config.url, env.VITE_BASEMAP_URL);
  assert.equal(config.attribution, 'Provider &lt;script&gt;bad()&lt;/script&gt;');
  for (const datum of ['GCJ02', 'GCJ-02', 'BD09', 'BD-09']) {
    assert.throws(() => resolveBasemapConfig({ ...env, VITE_BASEMAP_DATUM: datum }), /须先转换/);
  }
  assert.throws(() => resolveBasemapConfig({ ...env, VITE_BASEMAP_CRS: 'EPSG:4326' }), /须先转换/);
  assert.throws(() => resolveBasemapConfig({ ...env, VITE_BASEMAP_URL: 'javascript:{z}/{x}/{y}' }), /地址须/);
  assert.throws(() => resolveBasemapConfig({ ...env, VITE_BASEMAP_URL: 'https://example.test/image.png' }), /地址须/);
  assert.throws(() => resolveBasemapConfig({ ...env, VITE_BASEMAP_MAX_NATIVE_ZOOM: 'NaN' }), /整数/);
});

test('POIs retain safe plain text properties, valid numeric lon/lat and service status', () => {
  const parsed = normalizePoiCollection(collection([
    point([114.3, 38.1], { name: '<img src=x onerror=bad()>', category: '学校' }, 12),
    point([114.4, 38.2], { amenity: 'hospital' }),
    point([114.5, 38.3], null),
  ], { configured: true, warning: '来源提示' }));
  assert.equal(parsed.points.length, 3);
  assert.equal(parsed.points[0].name, '<img src=x onerror=bad()>');
  assert.equal(parsed.points[0].id, '12');
  assert.equal(parsed.points[0].longitude, 114.3);
  assert.equal(parsed.points[1].category, 'hospital');
  assert.equal(parsed.points[2].name, '未命名地点');
  assert.equal(parsed.warning, '来源提示');
  assert.equal(parsed.configured, true);
  assert.equal(parsed.truncated, false);
  assert.equal(normalizePoiCollection(collection([], { configured: false })).configured, false);
});

test('invalid POIs are skipped and visible markers are bounded even for oversized payloads', () => {
  const parsed = normalizePoiCollection(collection([
    null, {}, point(['114', 38]), point([NaN, 38]), point([Infinity, 38]),
    point([181, 38]), point([114, 91]),
    { type: 'Feature', geometry: { type: 'LineString', coordinates: [[114, 38], [115, 39]] } },
    point([114, 38]), point([115, 39]), point([116, 40]),
  ]), { limit: 2 });
  assert.equal(parsed.points.length, 2);
  assert.equal(parsed.truncated, true);
  assert.equal(normalizePoiCollection(collection([point([114, 38])], { truncated: true })).truncated, true);
  assert.throws(() => normalizePoiCollection(null), /FeatureCollection/);
  assert.throws(() => normalizePoiCollection(collection([], { datum: 'GCJ-02' })), /WGS84/);
  assert.throws(() => normalizePoiCollection(collection([], {
    crs: { type: 'name', properties: { name: 'EPSG:3857' } },
  })), /WGS84/);
});

test('backend coordinate declarations, diagnostic messages and optional popup fields are normalized', () => {
  const normalized = normalizePoiCollection(collection([
    point([114.3, 38.1], { name: '学校', address: '河北省\n某街道', source_id: 'local-poi-123' }),
    point([114.4, 38.2], { address: null, source_id: '' }),
  ], { coordinate_system: 'EPSG:4326', configured: true, status: 'ready', message: '已加载本地 POI' }));
  assert.equal(normalized.warning, '已加载本地 POI');
  assert.equal(normalized.points[0].address, '河北省某街道');
  assert.equal(normalized.points[0].sourceId, 'local-poi-123');
  assert.ok(!Object.hasOwn(normalized.points[1], 'address'));
  assert.ok(!Object.hasOwn(normalized.points[1], 'sourceId'));
  assert.equal(normalizePoiCollection(collection([], {
    coordinate_system: 'CRS84', configured: false, status: 'not_configured', message: '请配置 POI 目录',
  })).warning, '请配置 POI 目录');
  assert.equal(normalizePoiCollection(collection([], { warning: '优先警告', message: '后备提示' })).warning, '优先警告');
  for (const coordinate_system of ['GCJ02', 'BD09', 'EPSG:3857', 'EPSG:4490', '', null, 4326]) {
    assert.throws(() => normalizePoiCollection(collection([], { coordinate_system })), /WGS84/);
  }
});

test('graticule adapts to geographical extent and viewport size without excessive lines', () => {
  const province = buildGraticule({ west: 113.45, south: 36, east: 119.9, north: 42.65 }, view);
  const patch = buildGraticule({ west: 113.904080, south: 38.70005, east: 113.90996, north: 38.70466 }, view);
  assert.ok(province.lines.length > 0 && province.lines.length <= 24);
  assert.ok(patch.lines.length > 0 && patch.lines.length <= 24);
  assert.ok(patch.longitudeStep < province.longitudeStep);
  assert.ok(patch.latitudeStep < province.latitudeStep);
  assert.ok(patch.lines.some(line => line.label.includes('.')));
  const small = buildGraticule({ west: 113, south: 36, east: 120, north: 43 }, { width: 250, height: 200 });
  assert.ok(small.lines.length < province.lines.length);
  const bounded = buildGraticule({ west: -180, south: -90, east: 180, north: 90 }, { width: 1e8, height: 1e8, maxLines: 10 });
  assert.ok(bounded.lines.length <= 10);
});

test('antimeridian crossing retains short visible geometry and normalized longitude labels', () => {
  const crossing = buildGraticule({ west: 175, south: -10, east: -175, north: 10 }, view);
  assert.ok(crossing.lines.length > 0);
  const parallels = crossing.lines.filter(line => line.axis === 'latitude');
  assert.ok(parallels.every(line => line.points[1][1] - line.points[0][1] === 10));
  assert.ok(crossing.lines.some(line => line.axis === 'longitude' && line.value > 180 && line.label.endsWith('W')));
  const unwrapped = buildGraticule({ west: 535, south: -10, east: 545, north: 10 }, view);
  assert.ok(unwrapped.lines.length > 0);
  assert.ok(unwrapped.lines.every(line => line.points.every(position => position[1] >= 535 && position[1] <= 545)));
});

test('polar, invalid and multi-world bounds are clipped or rejected without infinite loops', () => {
  const world = buildGraticule({ west: -1000, south: -90, east: 1000, north: 90 }, view);
  assert.ok(world.lines.length <= 24);
  assert.ok(world.lines.every(line => line.points.every(([latitude]) => Math.abs(latitude) <= MERCATOR_MAX_LATITUDE)));
  for (const bounds of [
    { west: NaN, south: 0, east: 10, north: 10 },
    { west: 0, south: 89, east: 10, north: 90 },
    { west: 0, south: 5, east: 10, north: 1 },
    { west: 10, south: 0, east: 10, north: 10 },
  ]) assert.equal(buildGraticule(bounds, view).lines.length, 0);
  assert.equal(buildGraticule({ west: 0, south: 0, east: 10, north: 10 }, { width: 0, height: 0 }).lines.length, 0);
});

test('coordinate text wraps longitudes, handles hemispheres and avoids negative zero', () => {
  assert.equal(formatCoordinates(113.904080, 38.700050), '113.904080°E  38.700050°N');
  assert.equal(formatCoordinates(-73.5, -20.25, 2), '73.50°W  20.25°S');
  assert.equal(formatDegree(185, 'longitude', 0), '175°W');
  assert.equal(formatDegree(-0.0000001, 'latitude', 6), '0.000000°N');
  assert.equal(formatDegree(NaN, 'latitude'), '—');
  assert.equal(formatDegree(95, 'latitude'), '—');
});
