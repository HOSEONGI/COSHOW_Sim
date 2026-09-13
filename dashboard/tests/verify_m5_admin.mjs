import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const {chromium}=require(path.join(process.env.DASHBOARD_QA_NODE_MODULES,'playwright'));
const base=process.env.DASHBOARD_QA_URL||'http://127.0.0.1:8089';
const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE});
let admin;const timers=new Set();
try{
  const context=await browser.newContext({viewport:{width:1600,height:1050}}),page=await context.newPage();
  let hello,state;const errors=[],external=[],sent=[];
  await context.route('**/*',async route=>{if(new URL(route.request().url()).origin!==base){external.push(route.request().url());await route.abort();}else await route.continue();});
  if(process.env.M5_SOURCE_SNAPSHOT==='1'){
    await context.route('**/static/js/admin.js',route=>route.fulfill({path:'dashboard/run/milestone_snapshots/M5_admin.js',contentType:'text/javascript'}));
    await context.route('**/static/css/admin.css',route=>route.fulfill({path:'dashboard/run/milestone_snapshots/M5_admin.css',contentType:'text/css'}));
  }
  page.on('pageerror',error=>errors.push(error.message));
  page.on('websocket',socket=>{socket.on('framereceived',({payload})=>{if(typeof payload==='string'){const value=JSON.parse(payload);if(value.type==='hello')hello=value;if(value.type==='state')state=value;}});socket.on('framesent',({payload})=>sent.push(JSON.parse(payload)));});
  await page.goto(base+'/admin.html');
  await page.waitForFunction(()=>document.querySelectorAll('#robot-rows tr').length===6);
  await page.evaluate(()=>document.fonts.ready);
  admin=new WebSocket(base.replace('http','ws')+'/ws?role=admin');await new Promise(resolve=>admin.onopen=resolve);
  if(state.run.state!=='IDLE'){admin.send(JSON.stringify({cmd:'reset'}));await page.waitForFunction(()=>document.querySelector('#run-state').textContent==='IDLE');}
  await page.locator('[data-cmd="preflight"]').click();
  await page.waitForFunction(()=>document.querySelector('#run-state').textContent==='READY');
  assert.ok(state.checklist.filter(row=>row.blocking).every(row=>row.ok));
  await page.screenshot({path:'dashboard/REPORTS/img/M5_admin_ready.png',fullPage:true});
  await page.locator('[data-cmd="start"]').click();
  await page.waitForTimeout(150);assert.equal(state.run.state,'READY','a click shorter than 600ms must not start');
  const button=await page.locator('[data-cmd="start"]').boundingBox();
  await page.mouse.move(button.x+button.width/2,button.y+button.height/2);await page.mouse.down();
  await page.waitForTimeout(650);await page.mouse.up();
  await page.waitForFunction(()=>document.querySelector('#run-state').textContent==='RUNNING');
  await page.screenshot({path:'dashboard/REPORTS/img/M5_admin_running.png',fullPage:true});
  assert.equal(sent.filter(command=>command.cmd==='start').length,1);
  await page.locator('[data-cmd="estop"]').click();
  await page.waitForFunction(()=>document.querySelector('#run-state').textContent==='LANDING');
  assert.equal(await page.locator('[data-cmd]:enabled').count(),0);
  await page.screenshot({path:'dashboard/REPORTS/img/M5_admin_landing.png',fullPage:true});
  console.log('ACTUAL MOCK ready blocking OK; short press no start; 600ms hold starts exactly once; immediate estop; all LANDING controls locked',JSON.stringify(sent));
  // Explicit protocol fixture keeps state fresh while suppressing an ack.
  const fixture=structuredClone(state);fixture.run.state='RUNNING';fixture.events=[];
  let transmitting=true;let routed;
  await page.routeWebSocket('**/ws?role=admin',socket=>{
    routed=socket;socket.send(JSON.stringify(hello));socket.send(JSON.stringify(fixture));
    const timer=setInterval(()=>{if(transmitting)socket.send(JSON.stringify(fixture));},100);timers.add(timer);
    socket.onMessage(()=>{});socket.onClose(async(code,reason)=>{clearInterval(timer);timers.delete(timer);await socket.close({code,reason});});
  });
  await page.reload();await page.waitForFunction(()=>document.querySelector('#run-state').textContent==='RUNNING');
  await page.locator('[data-cmd="estop"]').click();
  assert.equal(await page.locator('[data-cmd]:enabled').count(),0);
  await page.waitForTimeout(1050);
  assert.deepEqual(await page.locator('[data-cmd]:enabled').evaluateAll(es=>es.map(e=>e.dataset.cmd)),['estop']);
  transmitting=false;await routed.close({code:1001,reason:'test disconnect'});
  await page.waitForFunction(()=>!document.querySelector('#connection-banner').hidden);
  assert.equal(await page.locator('[data-cmd]:enabled').count(),0);
  assert.match(await page.locator('#connection-banner').textContent(),/Ctrl\+C.*마지막 수신/);
  await page.screenshot({path:'dashboard/REPORTS/img/M5_admin_disconnected.png',fullPage:true});
  await page.locator('#events').scrollIntoViewIfNeeded();
  const emergency=await page.locator('[data-cmd="estop"]').boundingBox();assert.ok(emergency.y>=0&&emergency.y+emergency.height<=1050,'emergency control stays visible while reading events');
  assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
  console.log('PASS admin UI: 6 role rows; no-ack after1s onlyestop; closebanner+alllocked; sticky emergency visible at event scroll; externalrequests0; errors0');
}finally{for(const timer of timers)clearInterval(timer);admin?.close();await browser.close();}
