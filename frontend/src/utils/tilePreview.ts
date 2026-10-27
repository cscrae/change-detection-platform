import L from 'leaflet';
import { TileScheduler } from './tileScheduler';
import { LOCAL_MAP_OPTIONS } from './basemap';

type Coord = { x: number; y: number; z: number };
type Item = { coord: Coord; controller: AbortController; blob?: Blob; overlay?: L.ImageOverlay;
  url?: string; loaded: boolean; used: number; retries: number; timer?: ReturnType<typeof setTimeout> };
const keyOf = (c: Coord) => `${c.z}/${c.x}/${c.y}`;
const EXTRA_TILES = 64;

/** Decoded coarse tiles remain under the detail grid through pan and animated zoom. */
export class TilePreview {
  private base = new Map<string, Coord>();
  private wanted = new Map<string, Coord>();
  private items = new Map<string, Item>();
  private owned = new Set<string>();
  private failures = new Set<string>();
  private paused = true;
  private idle = false;
  private clock = 0;

  constructor(private map: L.Map, private bounds: L.LatLngBounds | undefined,
    private scheduler: TileScheduler, private url: (c: Coord) => string,
    private stale: () => void) {
    if (!map.getPane('cdp-preview')) {
      const pane = map.createPane('cdp-preview');
      pane.style.zIndex = '190';
      pane.style.pointerEvents = 'none';
    }
    // No fabricated global extent when a catalog entry has no known footprint.
    if (bounds) {
      // The fitted overview minimum varies with window size; the imagery base levels do not.
      const min = Math.floor(LOCAL_MAP_OPTIONS.minZoom ?? 6);
      for (const z of [min, min + 1]) {
        for (const c of this.cover(bounds, z)) this.base.set(keyOf(c), c);
      }
    }
  }

  private cover(bounds: L.LatLngBounds, z: number, ring = 0): Coord[] {
    const nw = this.map.project(bounds.getNorthWest(), z).divideBy(256);
    const se = this.map.project(bounds.getSouthEast(), z).divideBy(256);
    const n = 2 ** z, result: Coord[] = [];
    let x0 = Math.max(0, Math.floor(nw.x) - ring), y0 = Math.max(0, Math.floor(nw.y) - ring);
    let x1 = Math.min(n - 1, Math.ceil(se.x) - 1 + ring), y1 = Math.min(n - 1, Math.ceil(se.y) - 1 + ring);
    if (this.bounds) {
      const a = this.map.project(this.bounds.getNorthWest(), z).divideBy(256);
      const b = this.map.project(this.bounds.getSouthEast(), z).divideBy(256);
      x0 = Math.max(x0, Math.floor(a.x)); y0 = Math.max(y0, Math.floor(a.y));
      x1 = Math.min(x1, Math.ceil(b.x) - 1); y1 = Math.min(y1, Math.ceil(b.y) - 1);
    }
    for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) result.push({ x, y, z });
    return result;
  }

  update(detailReady: boolean) {
    if (this.paused || !this.bounds) return;
    this.idle = detailReady && !this.scheduler.moving;
    const z = Math.max(0, Math.round(this.map.getZoom()) - 1);
    const center = this.map.project(this.map.getCenter(), z).divideBy(256);
    const ring = this.cover(this.map.getBounds(), z, 1)
      .sort((a, b) => (a.x + .5 - center.x) ** 2 + (a.y + .5 - center.y) ** 2
        - (b.x + .5 - center.x) ** 2 - (b.y + .5 - center.y) ** 2).slice(0, EXTRA_TILES);
    this.wanted = new Map(ring.map(c => [keyOf(c), c]));
    for (const [key, item] of this.items) {
      if (!item.loaded && !this.base.has(key) && (!this.idle || !this.wanted.has(key))) this.remove(key);
      else if (this.wanted.has(key) || this.base.has(key)) item.used = ++this.clock;
    }
    const targets = this.idle ? new Map([...this.base, ...this.wanted]) : this.base;
    for (const [key, coord] of targets) {
      if (this.items.has(key) || this.owned.has(key) || this.failures.has(key)) continue;
      const item: Item = { coord, controller: new AbortController(), loaded: false, used: ++this.clock, retries: 0 };
      this.items.set(key, item);
      const base = this.base.has(key);
      const job = {
        // Whole-footprint previews may span hundreds of remote TIFFs. Keep every
        // preview in the scheduler's single background slot, behind visible detail.
        background: true,
        priority: () => this.paused ? Infinity : base ? 1e9 + coord.z : this.idle ? 1e12 : Infinity,
        run: async () => {
          try {
            const response = await fetch(this.url(coord), { signal: item.controller.signal });
            if ((response.status === 503 || response.status === 429) && item.retries < 2) {
              await response.body?.cancel();
              const delay = Math.min(30000, Math.max(500 * 2 ** item.retries++, Number(response.headers.get('Retry-After')) * 1000 || 0));
              if (!item.controller.signal.aborted) item.timer = setTimeout(() => {
                item.timer = undefined;
                if (this.items.get(key) === item) this.scheduler.enqueue(item, job);
              }, delay);
              return;
            }
            if (response.status === 409) this.stale();
            if (!response.ok) throw new Error('Preview unavailable');
            const blob = await response.blob();
            if (this.items.get(key) !== item) return;
            this.install(item, blob);
          } catch {
            if (!item.controller.signal.aborted) { this.failures.add(key); this.remove(key); }
          }
        },
      };
      this.scheduler.enqueue(item, job);
    }
    this.trim();
    this.scheduler.kick();
  }

  /** Detail takes ownership immediately, never waits on a queued background job. */
  claim(coord: Coord): Blob | undefined {
    const key = keyOf(coord), item = this.items.get(key);
    this.owned.add(key);
    if (item?.blob) { item.used = ++this.clock; return item.blob; }
    if (item) this.remove(key);
    return undefined;
  }
  release(coord: Coord) { this.owned.delete(keyOf(coord)); }
  remember(coord: Coord, blob: Blob) {
    const key = keyOf(coord);
    if (this.paused || this.items.has(key) || (!this.base.has(key) && !this.wanted.has(key))) return;
    const item: Item = { coord, controller: new AbortController(), loaded: false, used: ++this.clock, retries: 0 };
    this.items.set(key, item);
    this.install(item, blob);
  }
  private install(item: Item, blob: Blob) {
    const { x, y, z } = item.coord;
    item.blob = blob;
    item.url = URL.createObjectURL(blob);
    const bounds = L.latLngBounds(this.map.unproject(L.point(x * 256, (y + 1) * 256), z),
      this.map.unproject(L.point((x + 1) * 256, y * 256), z));
    const overlay = item.overlay = L.imageOverlay(item.url, bounds,
      { pane: 'cdp-preview', interactive: false, zIndex: z, className: 'cdp-tile-preview' });
    overlay.on('load', () => { item.loaded = true; this.trim(); });
    overlay.on('error', () => { this.failures.add(keyOf(item.coord)); this.remove(keyOf(item.coord)); });
    overlay.addTo(this.map);
    const img = overlay.getElement()!;
    img.style.imageRendering = 'auto';
    img.style.visibility = this.paused ? 'hidden' : '';
    img.dataset.tile = keyOf(item.coord);
  }
  private trim() {
    const extra = [...this.items].filter(([key]) => !this.base.has(key)).sort((a, b) => a[1].used - b[1].used);
    for (const [key] of extra.slice(0, Math.max(0, extra.length - EXTRA_TILES))) this.remove(key);
  }
  private remove(key: string) {
    const item = this.items.get(key);
    if (!item) return;
    this.items.delete(key);
    this.scheduler.cancel(item);
    clearTimeout(item.timer);
    item.controller.abort();
    const img = item.overlay?.getElement();
    item.overlay?.remove(); item.overlay?.off();
    if (img) { img.onload = img.onerror = null; img.removeAttribute('src'); }
    if (item.url) URL.revokeObjectURL(item.url);
  }
  suspend() {
    this.paused = true;
    for (const [key, item] of this.items) {
      if (!item.loaded) this.remove(key);
      else if (item.overlay) item.overlay.getElement()!.style.visibility = 'hidden';
    }
  }
  resume() {
    this.paused = false;
    this.failures.clear();
    for (const item of this.items.values()) if (item.overlay) item.overlay.getElement()!.style.visibility = '';
  }
  dispose() { this.suspend(); for (const key of this.items.keys()) this.remove(key); this.owned.clear(); }
  get tileCount() { return this.items.size; }
  get bytes() { return [...this.items.values()].reduce((sum, item) => sum + 256 * 256 * 4 + (item.blob?.size || 0), 0); }
}
