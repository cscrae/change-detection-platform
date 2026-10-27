/** Leaflet's EPSG:3857 projection stops at this latitude. */
export const MERCATOR_MAX_LATITUDE = 85.0511287798066;

export interface GeographicBounds {
  west: number;
  south: number;
  east: number;
  north: number;
}

export interface GraticuleLine {
  axis: 'longitude' | 'latitude';
  value: number;
  label: string;
  points: [number, number][];
  labelPosition: [number, number];
}

export interface GraticuleGrid {
  lines: GraticuleLine[];
  longitudeStep: number;
  latitudeStep: number;
}

export function wrapLongitude(longitude: number): number {
  const wrapped = ((longitude + 180) % 360 + 360) % 360 - 180;
  return wrapped === -180 && longitude > 0 ? 180 : wrapped;
}

function precisionForStep(step: number): number {
  return Math.max(0, Math.min(7, Math.ceil(-Math.log10(step))));
}

export function formatDegree(value: number, axis: 'longitude' | 'latitude', precision = 6): string {
  if (!Number.isFinite(value)) return '—';
  const decimals = Number.isFinite(precision) ? Math.max(0, Math.min(8, Math.floor(precision))) : 6;
  const coordinate = axis === 'longitude' ? wrapLongitude(value) : value;
  if (axis === 'latitude' && Math.abs(coordinate) > 90) return '—';
  const rounded = Number(coordinate.toFixed(decimals));
  const suffix = axis === 'longitude' ? (rounded < 0 ? 'W' : 'E') : (rounded < 0 ? 'S' : 'N');
  return `${Math.abs(rounded).toFixed(decimals)}°${suffix}`;
}

export function formatCoordinates(longitude: number, latitude: number, precision = 6): string {
  return `${formatDegree(longitude, 'longitude', precision)}  ${formatDegree(latitude, 'latitude', precision)}`;
}

function niceStep(span: number, targetLines: number): number {
  const raw = Math.max(1e-7, span / Math.max(1, targetLines));
  const power = 10 ** Math.floor(Math.log10(raw));
  const ratio = raw / power;
  return (ratio <= 1 ? 1 : ratio <= 2 ? 2 : ratio <= 5 ? 5 : 10) * power;
}

function ticks(minimum: number, maximum: number, step: number, limit: number): number[] {
  const first = Math.ceil((minimum - step * 1e-9) / step);
  const last = Math.floor((maximum + step * 1e-9) / step);
  const result: number[] = [];
  for (let offset = 0; offset < Math.min(limit, last - first + 1); offset++) {
    result.push(Number(((first + offset) * step).toFixed(9)));
  }
  return result;
}

/** Return bounded, viewport-adaptive line geometry in Leaflet [latitude, longitude] order. */
export function buildGraticule(
  bounds: GeographicBounds,
  viewport: { width: number; height: number; maxLines?: number },
): GraticuleGrid {
  const empty: GraticuleGrid = { lines: [], longitudeStep: 0, latitudeStep: 0 };
  const values = [bounds.west, bounds.south, bounds.east, bounds.north, viewport.width, viewport.height];
  if (!values.every(Number.isFinite) || viewport.width <= 0 || viewport.height <= 0
    || bounds.north <= bounds.south) return empty;
  const south = Math.max(-MERCATOR_MAX_LATITUDE, bounds.south);
  const north = Math.min(MERCATOR_MAX_LATITUDE, bounds.north);
  if (north <= south) return empty;
  // Leaflet may expose longitudes outside [-180, 180] when the viewport crosses a world edge.
  // Keep geometry in that visible world copy, wrapping only the labels.
  let east = bounds.east;
  if (east < bounds.west) east += 360 * Math.ceil((bounds.west - east) / 360);
  if (east <= bounds.west) return empty;
  const center = bounds.west / 2 + east / 2;
  const span = Math.min(360, east - bounds.west);
  const west = center - span / 2;
  east = center + span / 2;
  if (!Number.isFinite(center) || !Number.isFinite(span)) return empty;
  const maxLines = Number.isFinite(viewport.maxLines)
    ? Math.max(4, Math.min(80, Math.floor(viewport.maxLines!))) : 24;
  const axisLimit = Math.floor(maxLines / 2);
  const longitudeStep = niceStep(east - west, Math.min(axisLimit - 1, Math.max(2, viewport.width / 150)));
  const latitudeStep = niceStep(north - south, Math.min(axisLimit - 1, Math.max(2, viewport.height / 120)));
  const lines: GraticuleLine[] = [];
  for (const longitude of ticks(west, east, longitudeStep, axisLimit)) {
    lines.push({
      axis: 'longitude', value: longitude,
      label: formatDegree(longitude, 'longitude', precisionForStep(longitudeStep)),
      points: [[south, longitude], [north, longitude]],
      labelPosition: [north - (north - south) * 0.035, longitude],
    });
  }
  for (const latitude of ticks(south, north, latitudeStep, axisLimit)) {
    lines.push({
      axis: 'latitude', value: latitude,
      label: formatDegree(latitude, 'latitude', precisionForStep(latitudeStep)),
      points: [[latitude, west], [latitude, east]],
      labelPosition: [latitude, west + (east - west) * 0.025],
    });
  }
  return { lines, longitudeStep, latitudeStep };
}
