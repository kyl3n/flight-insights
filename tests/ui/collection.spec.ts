import { expect, test } from '@playwright/test';
import { createSocket } from 'node:dgram';
import { execFileSync } from 'node:child_process';
import { resolve } from 'node:path';
import { connectEngine } from './engine-harness';

// Synthetic loopback packets exercise the actual live-forwarded receive path.
// This isolated test declares that path live on purpose; these are not aircraft
// evidence or a validation of physical identity, GCS coexistence, or failsafes.
function heartbeatPackets():{disarmed:string[];armed:string[]} {
  const python=resolve(process.platform==='win32'?'.venv/Scripts/python.exe':'.venv/bin/python');
  return JSON.parse(execFileSync(python,['-c',[
    'import json',
    'from pymavlink.dialects.v20 import ardupilotmega as m',
    'sender=m.MAVLink(None,srcSystem=1,srcComponent=1)',
    'result={}',
    'for label,base_mode in [("disarmed",0),("armed",128)]:',
    ' frames=[]',
    ' for seq in range(256):',
    '  sender.seq=seq',
    '  frames.append(m.MAVLink_heartbeat_message(2,3,base_mode,0,3,3).pack(sender).hex())',
    ' result[label]=frames',
    'print(json.dumps(result))',
  ].join('\n')],{encoding:'utf8'}));
}

test('test transport rejects a status poll racing engine teardown',async({page})=>{
  const engine=await connectEngine(page);
  await engine.rpc('handshake');
  const shutdown=engine.close();
  try {
    await expect(engine.rpc('session.status')).rejects.toThrow('transport is closed');
  } finally {
    await shutdown;
  }
  await expect(engine.rpc('session.status')).rejects.toThrow('transport is closed');
});

test('passive observer records distinct armed intervals and preserves incomplete bundles after reload',async({page},testInfo)=>{
  const engine=await connectEngine(page);
  const sender=createSocket('udp4');
  const probe=createSocket('udp4');
  const packets=heartbeatPackets();
  let timer:ReturnType<typeof setInterval>|undefined;
  let armed=false;let sequence=0;let paused=false;
  try{
    await new Promise<void>(done=>probe.bind(0,'127.0.0.1',done));
    const port=(probe.address() as {port:number}).port;
    await new Promise<void>(done=>probe.close(()=>done()));
    await new Promise<void>(done=>sender.bind(0,'127.0.0.1',done));
    await page.goto('/');await expect(page.getByText('Engine connected',{exact:true})).toBeVisible();
    await page.getByRole('navigation').getByRole('button',{name:'Prepare & watch',exact:true}).click();
    await page.getByLabel('Aircraft profile name',{exact:true}).fill('Synthetic loopback transport · test only');
    await page.getByLabel('Observer local IPv4',{exact:true}).fill('127.0.0.1');
    await page.getByLabel('Dedicated observer UDP port',{exact:true}).fill(String(port));
    await page.getByLabel('Expected sender IPv4',{exact:true}).fill('127.0.0.1');
    await page.getByLabel('I configured this separate live feed and approved its sender and aircraft source IDs. It is not log replay.').check();
    await expect(page.getByRole('button',{name:'Preview observer setup',exact:true})).toBeEnabled();
    await page.getByRole('button',{name:'Preview observer setup',exact:true}).click();
    await expect(page.getByRole('heading',{name:'Reviewed observer setup',exact:true})).toBeVisible();
    expect(engine.calls.some(call=>call.method==='session.prepare_and_watch')).toBeFalsy();
    await page.locator('.observer-preview').getByRole('button',{name:'Prepare & watch',exact:true}).click();
    await expect(page.getByRole('heading',{name:'App observer is running',exact:true})).toBeVisible();
    timer=setInterval(()=>{if(!paused)sender.send(Buffer.from(packets[armed?'armed':'disarmed'][sequence++%256],'hex'),port,'127.0.0.1');},150);
    await expect.poll(async()=>{const state=await engine.rpc('session.status') as any;return state.recorder?.sources?.[0]?.armed_state;}).toBe('disarmed');
    armed=true;
    await expect(page.getByRole('button',{name:'Inspect interval 1 bundle',exact:true})).toBeVisible();
    await page.getByRole('navigation').getByRole('button',{name:'Overview',exact:true}).click();
    await expect(page.locator('.observer-strip')).toContainText('App observer recording');
    paused=true;
    await expect.poll(async()=>{const state=await engine.rpc('session.status') as any;return state.recorder?.sources?.[0]?.armed_state;},{timeout:7000}).toBe('unknown');
    const uncertain=await engine.rpc('session.status') as any;
    expect(uncertain.flight_records).toHaveLength(1);
    expect(uncertain.flight_records[0].disarm_evidence).toBeNull();
    paused=false;armed=false;
    await expect.poll(async()=>{const state=await engine.rpc('session.status') as any;return Boolean(state.flight_records[0]?.disarm_evidence);}).toBe(true);
    armed=true;
    await expect.poll(async()=>{const state=await engine.rpc('session.status') as any;return state.flight_records.length;}).toBe(2);
    armed=false;
    await expect.poll(async()=>{const state=await engine.rpc('session.status') as any;return state.flight_records.every((record:any)=>Boolean(record.disarm_evidence));}).toBe(true);
    await page.getByRole('navigation').getByRole('button',{name:'Prepare & watch',exact:true}).click();
    await page.getByRole('button',{name:'Stop app observer',exact:true}).click();
    clearInterval(timer);timer=undefined;
    await expect(page.getByRole('heading',{name:'App observer is running',exact:true})).toHaveCount(0);
    await page.getByRole('button',{name:'Inspect interval 1 bundle',exact:true}).click();
    const bundle=page.locator('.bundle-detail');
    await expect(bundle.getByRole('heading',{name:'Bundle for interval 1',exact:true})).toBeVisible();
    await expect(bundle.getByText('Capture lifecycle',{exact:true})).toBeVisible();
    await expect(bundle.getByText('Artifact association',{exact:true})).toBeVisible();
    await expect(bundle.getByText('Analysis coverage',{exact:true})).toBeVisible();
    await expect(bundle.getByText('Parameter attribution unavailable',{exact:true})).toBeVisible();
    await expect(bundle.getByText('No verified SD filename is known for this interval.',{exact:true})).toBeVisible();
    await expect(bundle.getByText('Provisional',{exact:true}).first()).toBeVisible();
    const before=await engine.rpc('session.status') as any;
    expect(before.flight_records).toHaveLength(2);
    expect(new Set(before.flight_records.map((record:any)=>record.armed_interval_id)).size).toBe(2);
    expect(before.flight_records.every((record:any)=>record.missing_sources.includes('onboard_bin')&&record.missing_sources.includes('preflight_parameters'))).toBe(true);
    await page.evaluate(()=>{if(document.activeElement instanceof HTMLElement)document.activeElement.blur();window.scrollTo(0,0);});
    await page.screenshot({path:testInfo.outputPath('collection-bundle.png'),fullPage:true});
    await page.setViewportSize({width:640,height:900});
    expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(640);
    await page.evaluate(()=>{if(document.activeElement instanceof HTMLElement)document.activeElement.blur();window.scrollTo(0,0);});
    await page.screenshot({path:testInfo.outputPath('collection-bundle-compact.png'),fullPage:true});
    await page.reload();await expect(page.getByText('Engine connected',{exact:true})).toBeVisible();
    await page.getByRole('navigation').getByRole('button',{name:'Prepare & watch',exact:true}).click();
    await expect(page.getByRole('button',{name:'Inspect interval 1 bundle',exact:true})).toBeVisible();
    await expect(page.getByRole('button',{name:'Inspect interval 2 bundle',exact:true})).toBeVisible();
    expect(engine.calls.filter(call=>call.method==='session.prepare_and_watch')).toHaveLength(1);
    expect(engine.calls.some(call=>call.method==='network.apply'||call.method==='network.rollback')).toBe(false);
  }catch(error){
    await testInfo.attach('observer-status-at-failure',{body:JSON.stringify(await engine.rpc('session.status'),null,2),contentType:'application/json'});
    throw error;
  }finally{
    if(timer)clearInterval(timer);
    try{sender.close();}catch{}
    try{probe.close();}catch{}
    await engine.close();
  }
});
