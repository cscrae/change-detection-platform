import type { TileLayerOptions } from 'leaflet';

export interface BasemapConfig {
  url: string;
  attribution: string;
  coordinateSystem: 'EPSG:3857';
  datum: 'WGS84';
  tileOptions: TileLayerOptions;
}

const OSM_URL = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
const OSM_ATTRIBUTION = '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap contributors</a>';

function text(value: unknown, maxLength = 200): string {
  return typeof value === 'string'
    ? value.replace(/[\u0000-\u001f\u007f]/g, '').trim().slice(0, maxLength)
    : '';
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[character] || character);
}

/** XYZ tiles use the map's EPSG:3857 grid; image bounds and POIs use WGS84 lon/lat. */
export function resolveBasemapConfig(env: Readonly<Record<string, unknown>> = {}): BasemapConfig {
  const url = text(env.VITE_BASEMAP_URL, 2048) || OSM_URL;
  const custom = url !== OSM_URL;
  const crs = text(env.VITE_BASEMAP_CRS).toUpperCase();
  const datum = text(env.VITE_BASEMAP_DATUM).toUpperCase().replace(/[-_\s]/g, '');
  if ((crs && crs !== 'EPSG:3857') || (datum && datum !== 'WGS84')) {
    throw new Error('底图需使用 WGS84 基准的 EPSG:3857 XYZ 瓦片；GCJ02、BD09 或其他坐标系须先转换。');
  }
  if (custom && (!crs || !datum)) {
    throw new Error('自定义底图须声明 VITE_BASEMAP_CRS=EPSG:3857 和 VITE_BASEMAP_DATUM=WGS84。');
  }
  if (!/^(https?:\/\/|\/(?!\/))/.test(url) || !['{z}', '{x}', '{y}'].every((part) => url.includes(part))) {
    throw new Error('底图地址须为 HTTP(S) 或站内 XYZ 模板，并包含 {z}、{x}、{y}。');
  }
  const customAttribution = text(env.VITE_BASEMAP_ATTRIBUTION, 1000);
  if (custom && !customAttribution) throw new Error('自定义底图须提供 VITE_BASEMAP_ATTRIBUTION 来源署名。');
  const attribution = customAttribution ? escapeHtml(customAttribution) : OSM_ATTRIBUTION;
  const rawNativeZoom = text(env.VITE_BASEMAP_MAX_NATIVE_ZOOM);
  const maxNativeZoom = rawNativeZoom ? Number(rawNativeZoom) : 19;
  if (!Number.isInteger(maxNativeZoom) || maxNativeZoom < 0 || maxNativeZoom > 22) {
    throw new Error('VITE_BASEMAP_MAX_NATIVE_ZOOM 须为 0 至 22 的整数。');
  }
  return {
    url, attribution, coordinateSystem: 'EPSG:3857', datum: 'WGS84',
    tileOptions: {
      attribution, maxNativeZoom, maxZoom: 22, noWrap: true,
      // Requests come from the user's viewport. Do not prefetch or bypass browser caching.
      updateWhenIdle: true, keepBuffer: 1, referrerPolicy: 'strict-origin-when-cross-origin',
    },
  };
}

export interface PoiPoint {
  id: string;
  name: string;
  category: string;
  longitude: number;
  latitude: number;
  address?: string;
  sourceId?: string;
}

export interface PoiCollection {
  points: PoiPoint[];
  configured: boolean;
  truncated: boolean;
  warning: string;
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}

/** GeoJSON (RFC 7946) coordinates are WGS84 [longitude, latitude]. Never render these strings as HTML. */
export function normalizePoiCollection(payload: unknown, options: { limit?: number } = {}): PoiCollection {
  const collection = record(payload);
  if (!collection || collection.type !== 'FeatureCollection' || !Array.isArray(collection.features)) {
    throw new Error('POI 服务未返回有效的 GeoJSON FeatureCollection。');
  }
  const legacyCrs = record(collection.crs);
  const legacyCrsProperties = record(legacyCrs?.properties);
  const crsName = text(legacyCrsProperties?.name).toUpperCase();
  const declaredSystem = text(collection.coordinate_system).toUpperCase();
  const geographicSystems = ['EPSG:4326', 'CRS84', 'URN:OGC:DEF:CRS:OGC:1.3:CRS84', 'OGC:CRS84'];
  const declaredDatum = text(collection.datum).toUpperCase().replace(/[-_\s]/g, '');
  if ((crsName && !geographicSystems.includes(crsName))
    || (declaredSystem && !geographicSystems.includes(declaredSystem))
    || (collection.coordinate_system !== undefined && !declaredSystem)
    || (declaredDatum && declaredDatum !== 'WGS84')) {
    throw new Error('POI 必须先转换为 WGS84 经纬度 GeoJSON。');
  }
  const limit = Number.isFinite(options.limit) ? Math.max(1, Math.min(2000, Math.floor(options.limit!))) : 500;
  const points: PoiPoint[] = [];
  let truncated = collection.truncated === true;
  const scanLimit = Math.min(collection.features.length, 10000);
  for (let index = 0; index < scanLimit; index++) {
    const feature = record(collection.features[index]);
    const geometry = record(feature?.geometry);
    if (feature?.type !== 'Feature' || geometry?.type !== 'Point' || !Array.isArray(geometry.coordinates)) continue;
    const [longitude, latitude] = geometry.coordinates;
    if (typeof longitude !== 'number' || typeof latitude !== 'number'
      || !Number.isFinite(longitude) || !Number.isFinite(latitude)
      || Math.abs(longitude) > 180 || Math.abs(latitude) > 90) continue;
    if (points.length >= limit) { truncated = true; break; }
    const properties = record(feature.properties);
    const featureId = typeof feature.id === 'number' && Number.isFinite(feature.id)
      ? String(feature.id) : text(feature.id);
    const address = text(properties?.address, 300);
    const sourceId = text(properties?.source_id, 200);
    points.push({
      id: featureId || `poi-${index}`,
      name: text(properties?.name, 120) || '未命名地点',
      category: text(properties?.category, 80) || text(properties?.amenity, 80)
        || text(properties?.tourism, 80) || text(properties?.shop, 80) || '其他',
      longitude, latitude,
      ...(address ? { address } : {}),
      ...(sourceId ? { sourceId } : {}),
    });
  }
  if (collection.features.length > scanLimit) truncated = true;
  return {
    points, configured: collection.configured !== false, truncated,
    warning: text(collection.warning, 300) || text(collection.message, 300),
  };
}
