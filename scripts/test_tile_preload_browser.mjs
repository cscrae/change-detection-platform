import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.FRONTEND_URL || 'http://localhost:5173';
const out = path.resolve(process.env.BROWSER_OUTPUT_DIR || 'data/validation/tile-preload');
await fs.mkdir(out, {recursive:true});
const browser = await chromium.launch({channel:'chrome',headless:true});
const page = await browser.newPage({viewport:{width:900,height:650}});
const errors=[]; page.on('pageerror',e=>errors.push(e.message));
const idle = () => page.waitForFunction(() => {
  const h=window.h,l=h.cache.active.layer,p=l.preview;
  return !l.isLoading() && [...l.resources.values()].filter(r=>Number.isFinite(l.priority(r.coords))).every(r=>r.loaded)
    && [...p.items.values()].every(i=>i.loaded) && h.active===0;
});
try {
  await page.route('**/__preload-test',r=>r.fulfill({contentType:'text/html',body:
    '<link rel="stylesheet" href="/node_modules/leaflet/dist/leaflet.css"><style>body{margin:0}.leaflet-container{background:#000}</style><div id="map" style="width:768px;height:512px"></div>'}));
  await page.goto(`${base}/__preload-test`);
  const src=await(await page.request.get(`${base}/src/utils/phaseLayerCache.ts`)).text();
  const leaflet=src.match(/from\s+["']([^"']*leaflet[^"']*)["']/)?.[1]; assert.ok(leaflet);
  await page.evaluate(async leaflet=>{
    const {default:L}=await import(leaflet);
    const {PhaseLayerCache}=await import('/src/utils/phaseLayerCache.ts');
    const map=L.map('map',{minZoom:6,maxZoom:20,zoomSnap:.25,zoomAnimation:true,fadeAnimation:false}).setView([38.95,116],14);
    const phase={id:'xiongan:2025-01',region_id:'xiongan',version:'test-v1',available:true,
      bounds:[115.6297752206484,38.69041476027517,116.33632226742829,39.17961685913406],scenes:[{id:'test',available:true}]};
    const h=window.h={L,map,phase,starts:[],active:0,maximum:0,backgrounds:0,maxBackgrounds:0,aborted:[],delay:350,created:0,revoked:0};
    h.baseEvents=Object.values(map._events).reduce((s,h)=>s+h.length,0);
    const create=URL.createObjectURL.bind(URL),revoke=URL.revokeObjectURL.bind(URL);
    URL.createObjectURL=b=>{h.created++;return create(b)};URL.revokeObjectURL=u=>{h.revoked++;revoke(u)};
    const blobs={};
    for(const [kind,color] of Object.entries({base:'#447744',ring:'#4477bb',detail:'#ddbb55'})) {
      const c=document.createElement('canvas');c.width=c.height=256;const ctx=c.getContext('2d');ctx.fillStyle=color;ctx.fillRect(0,0,256,256);
      blobs[kind]=await new Promise(resolve=>c.toBlob(resolve));
    }
    const fetch=window.fetch.bind(window);
    window.fetch=(url,options={})=>{
      if(typeof url!=='string'||!url.includes('/mosaic/'))return fetch(url,options);
      const [_,z,x,y]=url.match(/mosaic\/(\d+)\/(\d+)\/(\d+)/).map(Number);
      const kind=z<=7?'base':z===Math.round(map.getZoom())?'detail':'ring';
      const layer=h.cache.active.layer;
      const detailReady=[...layer.resources.values()].filter(r=>Number.isFinite(layer.priority(r.coords))).every(r=>r.loaded);
      h.starts.push({key:`${z}/${x}/${y}`,z,kind,at:performance.now(),detailReady,moving:h.cache.scheduler.moving});
      h.active++;h.maximum=Math.max(h.maximum,h.active);
      if(kind!=='detail'){h.backgrounds++;h.maxBackgrounds=Math.max(h.maxBackgrounds,h.backgrounds);}
      return new Promise((resolve,reject)=>{
        let done=false;
        const finish=abort=>{if(done)return;done=true;clearTimeout(timer);options.signal?.removeEventListener('abort',cancel);h.active--;
          if(kind!=='detail')h.backgrounds--;
          if(abort){h.aborted.push({kind,key:`${z}/${x}/${y}`});reject(new DOMException('Aborted','AbortError'));}
          else resolve(new Response(blobs[kind],{status:200}));};
        const cancel=()=>finish(true),timer=setTimeout(()=>finish(false),kind==='base'?35:kind==='ring'?90:h.delay);
        options.signal?.addEventListener('abort',cancel,{once:true});if(options.signal?.aborted)cancel();
      });
    };
    h.cache=new PhaseLayerCache(map,()=>{});h.activated=performance.now();h.cache.activate(phase);
  },leaflet);
  await page.waitForFunction(()=>[...window.h.cache.active.layer.preview.items.values()].filter(i=>i.coord.z<=7&&i.loaded).length===3);
  const initial=await page.evaluate(()=>({first:window.h.starts.slice(0,3),latency:window.h.starts[0].at-window.h.activated,
    detailPending:window.h.cache.active.layer.isLoading(),base:[...document.querySelectorAll('.cdp-tile-preview')].map(i=>i.dataset.tile)}));
  assert.ok(initial.first.every(r=>r.kind==='detail'),'visible detail precedes whole-footprint coarse requests');assert.ok(initial.latency<100,'no 200 ms settle wait');
  assert.equal(initial.base.length,3);
  await page.screenshot({path:path.join(out,'initial-coarse.png')});
  await page.waitForFunction(()=>window.h.starts.some(r=>r.kind==='ring'));
  await page.evaluate(()=>{const h=window.h;h.map.fire('movestart');h.map.panBy([620,0],{animate:false});h.map.fire('movestart');});
  await page.waitForTimeout(70);
  assert.ok(await page.evaluate(()=>window.h.aborted.some(r=>r.kind==='ring')),'movement preempts background');
  await page.evaluate(()=>window.h.map.fire('moveend'));await idle();
  const ring=await page.evaluate(()=>{const h=window.h,p=h.cache.active.layer.preview;return {
    requests:h.starts.filter(r=>r.kind==='ring'),pixelBounds:h.map.getPixelBounds(),footprint:[h.map.project([h.phase.bounds[3],h.phase.bounds[0]],13),h.map.project([h.phase.bounds[1],h.phase.bounds[2]],13)],keys:[...p.wanted.keys()],loaded:[...p.items].filter(([,i])=>i.loaded).map(([k])=>k),
    maximum:h.maximum,maxBackgrounds:h.maxBackgrounds};});
  const expected=[];
  const x0=Math.max(Math.floor(ring.pixelBounds.min.x/512)-1,Math.floor(ring.footprint[0].x/256));
  const x1=Math.min(Math.ceil(ring.pixelBounds.max.x/512),Math.ceil(ring.footprint[1].x/256)-1);
  const y0=Math.max(Math.floor(ring.pixelBounds.min.y/512)-1,Math.floor(ring.footprint[0].y/256));
  const y1=Math.min(Math.ceil(ring.pixelBounds.max.y/512),Math.ceil(ring.footprint[1].y/256)-1);
  for(let y=y0;y<=y1;y++)for(let x=x0;x<=x1;x++)expected.push(`13/${x}/${y}`);
  assert.deepEqual([...ring.keys].sort(),expected.sort(),'parent viewport plus exactly one surrounding ring, clipped to footprint');
  assert.ok(ring.requests.length>0);assert.ok(ring.requests.every(r=>r.detailReady&&!r.moving),'background only after decoded viewport and movement ends');
  assert.ok(ring.keys.every(k=>ring.loaded.includes(k)),'all parents in current surrounding ring cached');
  assert.ok(ring.maximum<=4);assert.equal(ring.maxBackgrounds,1);
  await page.evaluate(()=>{const h=window.h;h.delay=1800;h.panStarts=h.starts.length;h.map.panBy([420,0],{animate:false});});
  await page.waitForTimeout(80);
  assert.ok(await page.evaluate(()=>{const h=window.h;return h.cache.active.layer.isLoading()&&[...document.querySelectorAll('.cdp-tile-preview')].some(i=>i.naturalWidth===256&&getComputedStyle(i).visibility==='visible')}));
  await page.screenshot({path:path.join(out,'pan-coarse.png')});
  await idle();
  // Zooming out reuses prefetched parent PNGs without refetching their coordinates.
  const reuse=await page.evaluate(()=>{const h=window.h;h.oldKeys=[...h.cache.active.layer.preview.items].filter(([,i])=>i.loaded&&i.coord.z===13).map(([k])=>k);h.zoomStart=h.starts.length;h.delay=60;h.map.setZoom(13,{animate:false});return h.oldKeys.length});
  assert.ok(reuse>0);await idle();
  assert.ok(await page.evaluate(()=>!window.h.starts.slice(window.h.zoomStart).some(r=>r.z===13&&window.h.oldKeys.includes(r.key))),'detail reuses cached parent bytes');
  await page.evaluate(async()=>{const h=window.h;for(const z of [15,12,16,13.5,14.25,14]){h.map.setZoom(z,{animate:false});await new Promise(r=>setTimeout(r,25));}});await idle();
  await page.evaluate(()=>window.h.map.setZoom(15,{animate:true}));
  await page.waitForTimeout(350);await idle();
  assert.ok(await page.evaluate(()=>[...document.querySelectorAll('.cdp-tile-preview')].some(i=>i.naturalWidth===256&&i.style.transform.includes('translate'))),'coarse overlays track animated zoom');
  await page.evaluate(()=>window.h.map.setZoom(14,{animate:false}));await idle();
  for(let i=0;i<4;i++){await page.evaluate(()=>window.h.map.panBy([500,0],{animate:false}));await page.waitForTimeout(50);await idle();}
  const bounded=await page.evaluate(()=>{const p=window.h.cache.active.layer.preview;return {size:p.items.size,base:p.base.size};});
  assert.ok(bounded.size<=bounded.base+64,'pan history respects per-phase preview budget');
  // Warm phase switches retain decoded coarse overlays as well as detailed tiles.
  await page.evaluate(()=>{const h=window.h;h.other={...h.phase,id:'xiongan:2025-03',version:'test-v2'};h.cache.activate(h.other)});await idle();
  const warm=await page.evaluate(async()=>{const h=window.h,start=h.starts.length,created=h.created;
    for(let i=0;i<10;i++){h.cache.activate(i%2?h.other:h.phase);await new Promise(r=>requestAnimationFrame(r));}
    return {requests:h.starts.length-start,decodes:h.created-created};});
  assert.equal(warm.requests,0);assert.equal(warm.decodes,0);
  await page.evaluate(()=>{const h=window.h;h.cache.reconcile([h.phase]);});
  assert.equal(await page.evaluate(()=>window.h.cache.stats().layers),1,'obsolete phase previews evicted with their layer');
  const disposal=await page.evaluate(()=>{const h=window.h;h.cache.dispose();return {liveUrls:h.created-h.revoked,extraEvents:Object.values(h.map._events).reduce((s,x)=>s+x.length,0)-h.baseEvents,previews:document.querySelectorAll('.cdp-tile-preview').length,maximum:h.maximum}});
  assert.equal(disposal.liveUrls,0);assert.equal(disposal.extraEvents,0);assert.equal(disposal.previews,0);assert.ok(disposal.maximum<=4);assert.deepEqual(errors,[]);
  const report={passed:true,initial,ringRequests:ring.requests.length,maximum:disposal.maximum,maxBackgrounds:ring.maxBackgrounds,coarseReuse:reuse,bounded,warm,disposal,errors};
  await fs.writeFile(path.join(out,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));
} finally {await browser.close()}
