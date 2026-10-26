import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { resolve, extname, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
const playwright = await import(process.env.MAPEXP_PLAYWRIGHT_MODULE || 'playwright-core');
const engine = process.env.MAPEXP_BROWSER || 'chromium';
const project = resolve(fileURLToPath(new URL('..', import.meta.url)));
const targets = [['current', project]];
const servers = [];
async function serve(root) {
  const types = {'.js':'text/javascript', '.mjs':'text/javascript', '.html':'text/html', '.css':'text/css', '.ttf':'font/ttf'};
  const server = createServer(async (req, res) => {
    const path = resolve(root, '.' + new URL(req.url, 'http://localhost').pathname);
    if (!path.startsWith(root + sep)) { res.writeHead(403).end(); return; }
    try {
      const data = await readFile(path);
      res.writeHead(200, {'Content-Type':types[extname(path)] || 'application/octet-stream'}).end(data);
    } catch { res.writeHead(404).end(); }
  });
  await new Promise(done => server.listen(0, '127.0.0.1', done));
  servers.push(server);
  return server.address().port;
}
import assert from 'node:assert/strict';
const browser = await playwright[engine].launch(engine === 'chromium'
 ? {executablePath:process.env.CHROMIUM_PATH || '/usr/bin/chromium',headless:true,args:['--no-sandbox']}
 : {headless:true});
const now = new Date().toISOString();
const fixture = Array.from({length:1000},(_,i)=>({id:`event-${i}`,kind:i%2?'fire':'traffic',severity:i%2?'warning':'info',title:`Событие ${i} <script>`,description:'Описание происшествия для проверки карты и ленты.',location_label:'Пермь',location_precision:i%5?'district':'unknown',latitude:i%5?58+(i%20)*.003:null,longitude:i%5?56.2+(i%25)*.003:null,radius_m:1200,last_confirmed_at:now,updated_at:now,updates:2,source_count:1,source_names:['Тестовый источник']}));
const results=[];
try {
for (const [name,root] of targets) {
 const port = await serve(root);
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 let items=structuredClone(fixture);
 let revision=1, notModified=0, detailText='Проверка первоисточника';
 await page.addInitScript(()=>{
  window.EventSource=class {addEventListener(){} };
  window.__layers={markers:0,circles:0};
 });
 await page.route('**/api/**',route=>{
  const path=new URL(route.request().url()).pathname;
  if(path==='/api/events') {
   const etag='"feed-'+revision+'"';
   if(route.request().headers()['if-none-match']===etag) {
    notModified++;
    return route.fulfill({status:304,headers:{ETag:etag}});
   }
   return route.fulfill({json:items,headers:{ETag:etag}});
  }
  let data=path==='/api/config'?{city_name:'Пермь',timezone:'Asia/Yekaterinburg',latitude:58.01,longitude:56.25,zoom:11}:path==='/api/events'?items:path==='/api/status'?{connected:true}:({...items.find(e=>path.endsWith(e.id)),feed:[{source_name:'Источник',posted_at:now,text:detailText,link:'https://t.me/example/1'}]});
  return route.fulfill({json:data});
 });
 await page.route('https://*.tile.openstreetmap.org/**',route=>route.fulfill({status:200,contentType:'image/png',body:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aRZkAAAAASUVORK5CYII=','base64')}));
 await page.route('**/static/app.js*',async route=>{
  const response=await route.fetch();
  const instrumentation=`\nconst makeMarker=L.marker, makeCircle=L.circle; L.marker=(...args)=>{window.__layers.markers++;return makeMarker(...args)}; L.circle=(...args)=>{window.__layers.circles++;return makeCircle(...args)};\n`;
  await route.fulfill({response,body:(await response.text()).replace('bootstrap();',instrumentation+'window.__test={refresh,applyUpdates,renderFeed}; bootstrap();')});
 });
 await page.goto(`http://127.0.0.1:${port}/static/index.html`);
 await page.waitForFunction(()=>document.querySelectorAll('.card').length===1000);
 assert.equal(await page.locator('.event-marker').count(),800);
 assert.equal(await page.locator('.card').first().locator('h2').textContent(),fixture[0].title);
 await page.evaluate(()=>{window.__first=document.querySelector('.card');window.__marker=document.querySelector('.event-marker');window.__mutations=0;window.__observer=new MutationObserver(records=>window.__mutations+=records.filter(r=>r.type==='childList').length);window.__observer.observe(document.querySelector('#feed'),{childList:true,subtree:true});});
 await page.evaluate(()=>window.__test.refresh());
 assert.equal(notModified,1);
 const renderTimes=[];
 const timings=[];
 for(let i=0;i<8;i++) {renderTimes.push(await page.evaluate(()=>{const start=performance.now();window.__test.renderFeed();return performance.now()-start;}));}
 for(let i=0;i<8;i++) timings.push(await page.evaluate(async()=>{
  const start=performance.now();
  const input=document.querySelector('#search');input.value='Событие';input.dispatchEvent(new Event('input',{bubbles:true}));
  await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
  return performance.now()-start;
 }));
 const identity=await page.evaluate(()=>({card:window.__first===document.querySelector('.card'),marker:window.__marker===document.querySelector('.event-marker'),...window.__layers,mutations:window.__mutations}));
 if(name==='current'){assert.equal(identity.card,true);assert.equal(identity.marker,true);assert.equal(identity.markers,800);assert.equal(identity.mutations,0);}
 await page.locator('#search').fill('nothing-matches');
 await page.waitForFunction(()=>!document.querySelector('.card'));
 await page.locator('#state-action').click();
 await page.waitForFunction(()=>document.querySelectorAll('.card').length===1000);
 await page.locator('[data-kind=fire]').click();
 await page.waitForFunction(()=>document.querySelectorAll('.card').length===500);
 await page.locator('[data-kind=all]').click();
 await page.waitForFunction(()=>document.querySelectorAll('.card').length===1000);
 await page.locator('.card').first().click();
 await page.waitForSelector('#back');
 await page.waitForSelector('.entry');
 detailText='Обновлённая история без изменения карточки';
 await page.evaluate(()=>window.__test.refresh());
 await page.waitForFunction(()=>document.querySelector('.source-text')?.textContent==='Обновлённая история без изменения карточки');
 await page.locator('#back').click();
 await page.waitForSelector('#feed');
 // Apply a new snapshot through the existing update button.
 revision++;
 items=structuredClone(fixture.slice(1));items[0].title='Изменённый заголовок';items[0].severity='danger';items[0].radius_m=500;items[0].latitude=58.1;
 await page.evaluate(()=>window.__test.refresh());
 await page.locator('#new-events').click();
 await page.waitForFunction(()=>document.querySelectorAll('.card').length===999);
 assert.equal(await page.locator('.card').first().locator('h2').textContent(),'Изменённый заголовок');
 assert.equal(await page.locator('[data-event="event-0"]').count(),0);
 assert.equal(await page.locator('.event-marker.danger').count(),1);
 await page.locator('.card').first().click();
 await page.waitForSelector('.entry');
 if(name==='current') assert.equal(await page.locator('.event-marker.selected').count(),1);
 await page.locator('#back').click();
 assert.equal(await page.locator('.event-marker.selected').count(),0);
 await page.setViewportSize({width:390,height:844});
 await page.locator('#expand-feed').click();
 await page.waitForTimeout(300);
 assert.equal(await page.locator('#sidebar').evaluate(el=>el.classList.contains('full')),true);
 await page.locator('#sheet-toggle').click();
 await page.waitForTimeout(300);
 assert.equal(await page.locator('#sidebar').evaluate(el=>el.classList.contains('peek')),true);
 await page.locator('#map-retry').evaluate(el=>el.click());
 await page.waitForTimeout(100);
 assert.equal(await page.locator('.event-marker').count(),800);
 assert.deepEqual(errors,[]);
 timings.sort((a,b)=>a-b);
 results.push({name,engine,fixture:1000,repeatedSearchMedianMs:timings[4],renderMedianMs:renderTimes.sort((a,b)=>a-b)[4],identity});
 await page.close();
}
console.log(JSON.stringify(results,null,2));
} finally {
 await browser.close();
 for (const server of servers) { server.closeAllConnections(); server.close(); }
}
