import L from 'leaflet';
import type { ImageryPhase } from './regions';
import { TileScheduler } from './tileScheduler';
import { TilePreview } from './tilePreview';

export const PHASE_CACHE_DEFAULTS = { inactiveLayers: 3, inactiveBytes: 128 * 1024 * 1024, idleMs: 5 * 60_000 };
type CacheLimits = typeof PHASE_CACHE_DEFAULTS;
type TileRecord = { el: HTMLImageElement; coords: L.Coords };
// Leaflet 1.9 extension points not represented in its public TypeScript declarations.
// Keep private API usage here, covered by browser lifecycle tests.
interface GridInternals {
  _tiles: Record<string, TileRecord>;
  _resetView(event?: L.LeafletEvent): void;
  _update(center?: L.LatLng): void;
  _pruneTiles(): void;
  _removeTile(key: string): void;
  _tileZoom?: number;
  _getTiledPixelBounds(center: L.LatLng): L.Bounds;
}
const grid = L.TileLayer.prototype as unknown as GridInternals;
const internals = (layer: L.TileLayer) => layer as unknown as GridInternals;
type TileResource = { request: AbortController; coords: L.Coords; loaded: boolean; url?: string; retries: number; retryTimer?: ReturnType<typeof setTimeout> };

class RetainedTileLayer extends L.TileLayer {
  paused = false;
  failed = false;
  stale = false;
  preview?: TilePreview;
  private resources = new Map<HTMLImageElement, TileResource>();

  constructor(url: string, options: L.TileLayerOptions, private scheduler: TileScheduler) {
    super(url, options);
    this.on('tileunload tileabort', event => this.release((event as L.TileEvent).tile as HTMLImageElement));
  }

  getEvents(): Record<string, L.LeafletEventHandlerFn> {
    const events = L.TileLayer.prototype.getEvents!.call(this);
    return Object.fromEntries(Object.entries(events).map(([name, handler]) =>
      [name, (event: L.LeafletEvent) => { if (!this.paused) handler.call(this, event); }]));
  }

  _update(center?: L.LatLng) {
    if (!this.paused && this.scheduler.ready) {
      this.removePending(resource => !Number.isFinite(this.priority(resource.coords)));
      grid._update.call(this, center);
      this.updatePreview();
    }
  }
  _resetView(event?: L.LeafletEvent) { if (!this.paused) grid._resetView.call(this, event); }
  _pruneTiles() { if (!this.paused) grid._pruneTiles.call(this); }

  // Leaflet's native implementation tests img.complete. An img awaiting fetch has
  // no src and is already "complete", so it cannot track this loader's requests.
  _abortLoading() {
    this.removePending(resource => resource.coords.z !== internals(this)._tileZoom);
  }

  private removePending(predicate: (resource: TileResource) => boolean) {
    for (const [key, record] of Object.entries(internals(this)._tiles || {})) {
      const resource = this.resources.get(record.el);
      if (resource && !resource.loaded && predicate(resource)) internals(this)._removeTile(key);
    }
  }

  private priority(coords: L.Coords) {
    if (this.paused || !this._map || coords.z !== Math.round(this._map.getZoom())) return Infinity;
    const bounds = internals(this)._getTiledPixelBounds(this._map.getCenter());
    const size = this.getTileSize();
    const left = coords.x * size.x, top = coords.y * size.y;
    if (left >= bounds.max!.x || left + size.x <= bounds.min!.x ||
        top >= bounds.max!.y || top + size.y <= bounds.min!.y) return Infinity;
    const center = bounds.getCenter();
    return (left + size.x / 2 - center.x) ** 2 + (top + size.y / 2 - center.y) ** 2;
  }

  private updatePreview() {
    const visible = [...this.resources.values()].filter(r => Number.isFinite(this.priority(r.coords)));
    this.preview?.update(visible.length > 0 && visible.every(r => r.loaded));
  }

  refreshViewport() { if (!this.paused) this._resetView(); }

  createTile(coords: L.Coords, done: L.DoneCallback): HTMLElement {
    const tile = document.createElement('img');
    tile.alt = '';
    const resource: TileResource = { request: new AbortController(), coords, loaded: false, retries: 0 };
    this.resources.set(tile, resource);
    const cached = this.preview?.claim(coords);
    const fail = (error: Error) => {
      if (!this.resources.has(tile)) return;
      this.failed = true;
      tile.dataset.failed = 'true';
      done(error, tile);
    };
    tile.onload = () => {
      if (this.resources.has(tile)) { resource.loaded = true; done(undefined, tile); this.updatePreview(); }
    };
    tile.onerror = () => fail(new Error('影像瓦片解码失败'));
    const job = {
      priority: () => this.priority(coords),
      run: async () => {
        try {
          if (cached) {
            if (this.resources.has(tile)) { resource.url = URL.createObjectURL(cached); tile.src = resource.url; }
            return;
          }
          const response = await fetch(this.getTileUrl(coords), { signal: resource.request.signal });
          if ((response.status === 503 || response.status === 429) && resource.retries < 4) {
            // Release the scheduler slot while backing off; obsolete tiles cancel the timer.
            await response.body?.cancel();
            const retryAfter = Number(response.headers.get('Retry-After'));
            const delay = Math.min(30_000, Math.max(250 * 2 ** resource.retries,
              Number.isFinite(retryAfter) ? retryAfter * 1000 : 0)) + Math.random() * 150;
            resource.retries++;
            if (this.resources.has(tile) && !resource.request.signal.aborted) {
              resource.retryTimer = setTimeout(() => {
                resource.retryTimer = undefined;
                if (this.resources.has(tile) && !resource.request.signal.aborted) this.scheduler.enqueue(resource, job);
              }, delay);
            }
            return;
          }
          if (response.status === 409) this.stale = true;
          if (!response.ok) throw new Error(response.status === 409 ? '时相已更新，正在刷新目录…' : '影像瓦片加载失败，可切换时相重试');
          const blob = await response.blob();
          if (!this.resources.has(tile)) return;
          this.preview?.remember(coords, blob);
          resource.url = URL.createObjectURL(blob);
          tile.src = resource.url;
        } catch (error) {
          if (!resource.request.signal.aborted) fail(error instanceof Error ? error : new Error(String(error)));
        }
      },
    };
    this.scheduler.enqueue(resource, job);
    return tile;
  }

  private release(tile: HTMLImageElement) {
    const resource = this.resources.get(tile);
    if (!resource) return;
    this.resources.delete(tile);
    this.preview?.release(resource.coords);
    this.scheduler.cancel(resource);
    clearTimeout(resource.retryTimer);
    resource.request.abort();
    tile.onload = tile.onerror = null;
    tile.removeAttribute('src');
    if (resource.url) URL.revokeObjectURL(resource.url);
  }

  suspend() {
    this.paused = true;
    this.preview?.suspend();
    this.removePending(() => true);
    const container = this.getContainer();
    if (container) container.style.visibility = 'hidden';
  }

  resume() {
    this.paused = false;
    this.preview?.resume();
    this.failed = false;
    for (const [key, record] of Object.entries(internals(this)._tiles || {})) {
      if (record.el.dataset.failed) internals(this)._removeTile(key);
    }
    this._resetView();
    const container = this.getContainer();
    if (container) container.style.visibility = '';
  }

  dispose() {
    this.suspend();
    this.remove();
    this.preview?.dispose();
    for (const tile of this.resources.keys()) this.release(tile);
    this.off();
  }

  get tileCount() { return this.resources.size + (this.preview?.tileCount || 0); }
  get bytes() { const size = this.getTileSize(); return this.resources.size * size.x * size.y * 4 + (this.preview?.bytes || 0); }
}

type Entry = { key: string; phaseId: string; version: string; layer: RetainedTileLayer; used: number; staleReported: boolean };
export type PhaseLayerState = { loading: boolean; error: string; stale: boolean };

export class PhaseLayerCache {
  private entries = new Map<string, Entry>();
  private active?: Entry;
  private suspended = false;
  private timer: ReturnType<typeof setInterval>;
  private disposed = false;
  private scheduler: TileScheduler;

  constructor(private map: L.Map, private notify: (state: PhaseLayerState) => void,
    private limits: CacheLimits = PHASE_CACHE_DEFAULTS, private now = () => Date.now(), concurrency = 4) {
    this.scheduler = new TileScheduler(map, () => {
      if (!this.disposed && !this.suspended) this.active?.layer.refreshViewport();
    }, { concurrency });
    this.timer = setInterval(() => this.trim(), Math.min(30_000, limits.idleMs));
  }

  private report(entry: Entry) {
    if (this.disposed || this.suspended || this.active !== entry) return;
    const stale = entry.layer.stale && !entry.staleReported;
    if (stale) entry.staleReported = true;
    this.notify({ loading: entry.layer.isLoading(),
      error: entry.layer.failed ? '影像瓦片加载失败，可切换时相重试或刷新目录' : '', stale });
  }

  activate(phase?: ImageryPhase) {
    if (this.disposed) return;
    this.scheduler.hold();
    if (this.active) { this.active.used = this.now(); this.active.layer.suspend(); this.active = undefined; }
    if (!phase?.available) { this.notify({ loading: false, error: '', stale: false }); this.trim(); return; }
    const key = `${phase.region_id}/${phase.id}/${phase.version}`;
    for (const entry of this.entries.values()) {
      if (entry.phaseId === phase.id && entry.key !== key) this.evict(entry);
    }
    let entry = this.entries.get(key);
    if (!entry) {
      const url = `/py/local/regions/${encodeURIComponent(phase.region_id)}/mosaic/{z}/{x}/{y}.png?month=${encodeURIComponent(phase.month)}&v=${encodeURIComponent(phase.version)}`;
      const layer = new RetainedTileLayer(url, { minZoom: 0, maxZoom: 20, noWrap: true, keepBuffer: 1,
        updateWhenIdle: false, updateInterval: 50, updateWhenZooming: true, attribution: '本地遥感影像',
        bounds: phase.bounds ? [[phase.bounds[1], phase.bounds[0]], [phase.bounds[3], phase.bounds[2]]] : undefined }, this.scheduler);
      entry = { key, phaseId: phase.id, version: phase.version, layer, used: this.now(), staleReported: false };
      this.entries.set(key, entry);
      const created = entry;
      layer.preview = new TilePreview(this.map, phase.bounds ? L.latLngBounds([phase.bounds[1], phase.bounds[0]], [phase.bounds[3], phase.bounds[2]]) : undefined,
        this.scheduler, c => L.Util.template(url, c), () => { layer.stale = true; this.report(created); });
      layer.on('loading load tileerror', () => { this.report(created); this.trim(); });
      this.active = entry;
      layer.paused = this.suspended;
      layer.addTo(this.map);
    } else this.active = entry;
    entry.used = this.now();
    if (this.suspended) entry.layer.suspend(); else entry.layer.resume();
    if (!this.suspended) this.scheduler.defer();
    this.report(entry);
    this.trim();
  }

  reconcile(phases: ImageryPhase[]) {
    const versions = new Map(phases.map(phase => [phase.id, phase.version]));
    for (const entry of this.entries.values()) {
      if (versions.get(entry.phaseId) !== entry.version) this.evict(entry);
    }
  }

  suspend() { this.suspended = true; this.scheduler.hold(); this.active?.layer.suspend(); }
  resume() {
    if (!this.suspended || this.disposed) return;
    this.suspended = false;
    if (this.active) { this.active.layer.resume(); this.report(this.active); }
    this.scheduler.defer();
  }

  private evict(entry: Entry) {
    this.entries.delete(entry.key);
    if (this.active === entry) this.active = undefined;
    entry.layer.dispose();
  }

  trim() {
    const idle = [...this.entries.values()].filter(entry => entry !== this.active).sort((a, b) => a.used - b.used);
    let count = idle.length, bytes = idle.reduce((sum, entry) => sum + entry.layer.bytes, 0);
    for (const entry of idle) {
      if (count > this.limits.inactiveLayers || bytes > this.limits.inactiveBytes || this.now() - entry.used >= this.limits.idleMs) {
        bytes -= entry.layer.bytes; count--; this.evict(entry);
      }
    }
  }

  clear() { for (const entry of this.entries.values()) this.evict(entry); }
  dispose() { if (this.disposed) return; this.disposed = true; clearInterval(this.timer); this.scheduler.dispose(); this.clear(); }
  stats() {
    return { layers: this.entries.size, activeKey: this.active?.key, suspended: this.suspended,
      tiles: [...this.entries.values()].reduce((sum, entry) => sum + entry.layer.tileCount, 0),
      inactiveBytes: [...this.entries.values()].filter(entry => entry !== this.active).reduce((sum, entry) => sum + entry.layer.bytes, 0) };
  }
}
