import { expect, test } from '@playwright/test';
import { resolve, join } from 'node:path';
import { mkdir, readFile } from 'node:fs/promises';
import { createSocket } from 'node:dgram';
import { createServer } from 'node:http';
import { connectEngine } from './engine-harness';

async function imported(engine:Awaited<ReturnType<typeof connectEngine>>,file:string){
  const response=await engine.rpc('logs.import',{path:resolve('fixtures/logs',file),label:`Synthetic UI ${file}`,synthetic:true}) as any;
  let job=response.job;
  await expect.poll(async()=>{job=(await engine.rpc('jobs.get',{job_id:job.id}) as any).job;return job.state;}).toBe('succeeded');
  return job.result.log;
}

test('real insight report preserves vibration evidence, guidance, loaded parameters, and explicit local export',async({page},testInfo)=>{
  // Real imports, report/export work, full-page screenshots, and persistence
  // checks can exceed 30s together on Intel macOS CI. Individual assertions and
  // the final navigation retain their shorter budgets to expose actual stalls.
  test.setTimeout(90_000);
  const engine=await connectEngine(page);
  try{
    const log=await imported(engine,'synthetic-insights.log');
    const {snapshot}=await engine.rpc('parameters.import',{path:resolve('fixtures/parameters/synthetic-after.parm'),label:'Synthetic loaded settings',aircraft_id:'synthetic-test',board:'synthetic-board',firmware:'Copter-4.7.1',source_kind:'current_readback',observed_at:'2026-10-13T00:00:00Z',synthetic:true}) as any;
    await page.goto('/');await expect(page.getByText('Engine connected',{exact:true})).toBeVisible();
    await page.getByRole('navigation').getByRole('button',{name:'Flight insights',exact:true}).click();
    await page.getByRole('combobox',{name:'Insight log',exact:true}).selectOption(log.id);
    await page.getByRole('combobox',{name:'Insight source',exact:true}).selectOption('dataflash');
    await page.getByRole('combobox',{name:'Insight clock segment',exact:true}).selectOption('0');
    await page.getByLabel('Insight interval start (seconds)',{exact:true}).fill('1');
    await page.getByLabel('Insight interval end (seconds)',{exact:true}).fill('4');
    await page.getByText('Operating context · mass, propulsion, and battery',{exact:true}).click();
    await page.getByLabel('All-up aircraft mass (kg)',{exact:true}).fill('10');
    await page.getByLabel('Included payload mass (kg)',{exact:true}).fill('4');
    await page.getByLabel('Rotor count',{exact:true}).fill('4');
    await page.getByLabel('Propeller diameter (m)',{exact:true}).fill('0.5');
    await page.getByRole('combobox',{name:'Rotor geometry',exact:true}).selectOption('non_overlapping');
    await page.getByLabel('Declared firmware for guidance scope',{exact:true}).fill('Copter-4.7.1');
    await page.getByLabel('Declared flight phase',{exact:true}).fill('synthetic hover test');
    await page.getByRole('combobox',{name:'Loaded parameter snapshot to compare',exact:true}).selectOption(snapshot.id);
    await page.getByRole('button',{name:'Generate insight report',exact:true}).click();
    const report=page.getByRole('region',{name:'Preserved flight insight report'});
    await expect(report.getByRole('heading',{name:'Observed flight evidence',exact:true})).toBeVisible();
    await expect(report.getByRole('img',{name:/Observed vibration/})).toBeVisible();
    await expect(report.getByText('5 observed increments',{exact:true})).toBeVisible();
    await expect(report.getByText('Synthetic test evidence',{exact:true})).toBeVisible();
    await expect(report.getByText('Exact build unverified',{exact:true})).toBeVisible();
    await expect(report.locator('.insight-metrics article').filter({hasText:'Mean power'})).toContainText('386.6667 W');
    await report.getByText('Accessible vibration observation table',{exact:true}).click();
    await expect(report.getByRole('cell',{name:'70',exact:true})).toBeVisible();
    await report.getByText(/Loaded snapshot versus logged observations/).click();
    await expect(report.getByText(/A later loaded snapshot does not establish/)).toBeVisible();
    expect(await report.getByRole('link',{name:/ArduPilot\/ardupilot_wiki/}).first().getAttribute('href')).toMatch(/github\.com\/ArduPilot\/ardupilot_wiki\/blob\/[0-9a-f]{40}\//);
    const request=engine.calls.find(call=>call.method==='insights.report');
    expect(request?.params).toMatchObject({log_id:log.id,source_key:'dataflash',boot_epoch:0,interval_start_s:1,interval_end_s:4,parameter_snapshot_id:snapshot.id,operating_profile:{aircraft_mass_kg:10,payload_mass_kg:4}});
    await report.getByRole('heading',{name:'Observed flight evidence',exact:true}).scrollIntoViewIfNeeded();
    await page.evaluate(()=>{if(document.activeElement instanceof HTMLElement)document.activeElement.blur();});
    await page.screenshot({path:testInfo.outputPath('flight-insights-observed.png')});
    await page.getByRole('button',{name:'Preview exact review packet',exact:true}).click();
    await expect(page.getByTestId('review-packet')).toContainText('q22.insight-review.v1');
    expect(engine.calls.some(call=>call.method==='insights.export_review')).toBeFalsy();
    const folder=join(engine.data,'explicit-reviewed-export');await mkdir(folder);engine.files.push(folder);
    await page.getByRole('button',{name:'Choose folder and export reviewed packet',exact:true}).click();
    await expect(page.getByText('Review packet saved locally',{exact:true})).toBeVisible();
    const path=await page.locator('.insights-workspace .success-note p').first().textContent();
    const packet=JSON.parse(await readFile(path!,'utf8'));
    expect(packet.schema).toBe('q22.insight-review.v1');
    expect(packet.scope).toMatchObject({interval_start_s:1,interval_end_s:4});
    expect(JSON.stringify(packet)).not.toContain(engine.data);
    expect(JSON.stringify(packet)).not.toContain(log.source.original_name);
    await page.getByText('Exact JSON packet',{exact:false}).click();
    await page.evaluate(()=>{if(document.activeElement instanceof HTMLElement)document.activeElement.blur();window.scrollTo(0,0);});
    await page.screenshot({path:testInfo.outputPath('flight-insights.png'),fullPage:true});
    await page.setViewportSize({width:640,height:900});
    expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(640);
    await page.screenshot({path:testInfo.outputPath('flight-insights-compact.png'),fullPage:true});
    await page.reload({timeout:10_000});await expect(page.getByText('Engine connected',{exact:true})).toBeVisible();
    await page.getByRole('navigation').getByRole('button',{name:'Flight insights',exact:true}).click();
    await expect(page.getByRole('combobox',{name:'Preserved insight report',exact:true}).locator('option')).toHaveCount(2);
    expect(engine.calls.filter(call=>call.method==='insights.report')).toHaveLength(1);
    expect(engine.calls.some(call=>/acquisition\.|network\.apply|associations\.evaluate/.test(call.method))).toBeFalsy();
  }finally{await engine.close();}
});

test('telemetry without VIBE or raw gyro remains unavailable and does not invent tuning',async({page})=>{
  const engine=await connectEngine(page);
  try{
    const log=await imported(engine,'synthetic-flight.tlog');
    await page.goto('/');await expect(page.getByText('Engine connected',{exact:true})).toBeVisible();
    await page.getByRole('navigation').getByRole('button',{name:'Flight insights',exact:true}).click();
    await page.getByRole('combobox',{name:'Insight log',exact:true}).selectOption(log.id);
    await page.getByRole('combobox',{name:'Insight source',exact:true}).selectOption('1:1');
    await page.getByRole('combobox',{name:'Insight clock segment',exact:true}).selectOption('0');
    await page.getByRole('button',{name:'Generate insight report',exact:true}).click();
    const report=page.getByRole('region',{name:'Preserved flight insight report'});
    await expect(report.getByText('Vibration measurements unavailable',{exact:true})).toBeVisible();
    await expect(report.locator('.insight-metrics article').filter({hasText:'Roll tracking RMSE'})).toContainText('Unavailable');
    await expect(report.getByRole('paragraph').filter({hasText:'VIBE summaries cannot supply frequency'})).toBeVisible();
    await expect(report.getByRole('img',{name:/Observed vibration/})).toHaveCount(0);
    const response=await engine.rpc('insights.list') as any;
    const {report:stored}=await engine.rpc('insights.detail',{report_id:response.reports[0].id}) as any;
    expect(stored.parameter_evidence.attribution_allowed).toBe(false);
    expect(stored.recommendations.every((item:any)=>item.proposed_values===null)).toBe(true);
  }finally{await engine.close();}
});

test('acquisition previews are read-only and log transfers require a fresh explicit acknowledgement',async({page})=>{
  const engine=await connectEngine(page);const socket=createSocket('udp4');
  try{
    await new Promise<void>(done=>socket.bind(0,'127.0.0.1',done));const port=(socket.address() as {port:number}).port;await new Promise<void>(done=>socket.close(()=>done()));
    const profile={aircraft_id:'synthetic-ui-acquisition-only',aircraft_profile_version:'test-v1',connection_profile_version:'test-v1',connection:{local_address:'127.0.0.1',listen_port:port,expected_sender:'127.0.0.1',system_id:1,component_id:1,expected_uid:'12345',feed_origin:'live_forwarded',synthetic:true}};
    const preview=await engine.rpc('session.preview',profile) as any;
    const operation={operation_id:crypto.randomUUID(),target_profile_version:'test-v1',applicability_fingerprint:preview.applicability_fingerprint};
    const {capture_session}=await engine.rpc('session.prepare_and_watch',{...preview.profile,...operation}) as any;
    await engine.rpc('session.stop',{capture_session_id:capture_session.uuid,...operation,operation_id:crypto.randomUUID()});
    await page.goto('/');await expect(page.getByText('Engine connected',{exact:true})).toBeVisible();
    await page.getByRole('navigation').getByRole('button',{name:'Prepare & watch',exact:true}).click();
    await page.getByText('Read-only aircraft acquisition · explicit bidirectional path',{exact:true}).click();
    await page.getByLabel('Dedicated acquisition UDP port',{exact:true}).fill(String(port));
    await page.getByLabel('Approved bidirectional remote IPv4',{exact:true}).fill('127.0.0.1');
    await page.getByLabel('Approved remote UDP port',{exact:true}).fill('14590');
    await page.getByLabel('This acquisition uses synthetic test traffic',{exact:true}).check();
    await page.getByRole('button',{name:'Preview acquisition path',exact:true}).click();
    await expect(page.getByRole('heading',{name:'Reviewed read-only path',exact:true})).toBeVisible();
    await expect(page.getByRole('button',{name:'List actual onboard logs',exact:true})).toBeDisabled();
    expect(engine.calls.some(call=>/^acquisition\.(parameters|logs)\./.test(call.method))).toBeFalsy();
    await page.getByLabel('I reviewed the possible onboard logging suspension and will verify logging has resumed before flying.',{exact:true}).check();
    await expect(page.getByRole('button',{name:'List actual onboard logs',exact:true})).toBeEnabled();
    await page.getByLabel('Expected controller UID (decimal)',{exact:true}).fill('67890');
    await expect(page.getByRole('heading',{name:'Reviewed read-only path',exact:true})).toHaveCount(0);
    await page.getByRole('button',{name:'Preview acquisition path',exact:true}).click();
    await expect(page.getByRole('button',{name:'List actual onboard logs',exact:true})).toBeDisabled();
    await page.getByRole('button',{name:'Read full parameters',exact:true}).click();
    await expect(page.locator('.acquisition-workspace').getByRole('button',{name:'Cancel operation',exact:true})).toBeVisible();
    await page.locator('.acquisition-workspace').getByRole('button',{name:'Cancel operation',exact:true}).click();
    await expect(page.getByRole('heading',{name:'Acquisition evidence & recovery',exact:true})).toBeVisible();
    await expect(page.getByRole('combobox',{name:'Preserved acquisition',exact:true}).locator('option')).toHaveCount(2);
    expect(engine.calls.filter(call=>call.method==='acquisition.parameters.capture')).toHaveLength(1);
    expect(engine.calls.some(call=>call.method==='acquisition.logs.list')).toBeFalsy();
    const records=await engine.rpc('acquisition.list') as any;
    expect(records.acquisitions[0].state).not.toBe('complete');
  }finally{try{socket.close();}catch{}await engine.close();}
});

test('explicit raw radio capture preserves actual loopback responses without interpreting undocumented fields',async({page})=>{
  const paths:string[]=[];const server=createServer((request,response)=>{paths.push(request.url||'');response.setHeader('Content-Type','application/json');response.end(JSON.stringify({synthetic_test_only:true,undocumented_field:request.url==='/events.json'?'sample event':42}));});
  await new Promise<void>(done=>server.listen(0,'127.0.0.1',done));const port=(server.address() as {port:number}).port;
  const engine=await connectEngine(page);
  try{
    await page.goto('/');await expect(page.getByText('Engine connected',{exact:true})).toBeVisible();
    await page.getByRole('navigation').getByRole('button',{name:'Network checks',exact:true}).click();
    await page.getByLabel('Device IPv4',{exact:true}).fill('127.0.0.1');
    await page.getByLabel('Device port',{exact:true}).fill(String(port));
    await page.getByRole('button',{name:'Add source',exact:true}).click();
    await page.getByText('Record raw SkyPulse history',{exact:true}).click();
    await page.getByRole('combobox',{name:'Radio history source',exact:true}).selectOption({label:`127.0.0.1:${port}`});
    await page.getByLabel('Radio capture duration (seconds)',{exact:true}).fill('1');
    await page.getByLabel('Radio poll interval (milliseconds)',{exact:true}).fill('1000');
    expect(paths).toHaveLength(0);
    await page.getByRole('button',{name:'Start raw radio capture',exact:true}).click();
    await expect(page.getByRole('heading',{name:'Raw endpoint evidence',exact:true})).toBeVisible();
    await expect(page.getByText('Unvalidated · raw observations only',{exact:true})).toBeVisible();
    expect(new Set(paths)).toEqual(new Set(['/scatter_data.json','/events.json']));
    const result=await engine.rpc('radio.list') as any;
    const {capture}=await engine.rpc('radio.detail',{capture_id:result.captures[0].capture_id}) as any;
    expect(capture.normalization).toBe('raw_only_schema_unvalidated');
    expect(capture.polls.every((item:any)=>item.json_valid===true&&item.schema_validated===false)).toBe(true);
    await page.reload();await expect(page.getByText('Engine connected',{exact:true})).toBeVisible();
    expect(engine.calls.filter(call=>call.method==='radio.capture')).toHaveLength(1);
  }finally{await engine.close();await new Promise<void>((done,reject)=>server.close(error=>error?reject(error):done()));}
});

test('a delayed review packet cannot authorize export of a newer background report',async({page})=>{
  let releasePreview!:()=>void;let releaseCompletion!:()=>void;let previewArrived!:()=>void;
  const previewGate=new Promise<void>(resolve=>{releasePreview=resolve;});
  const completionGate=new Promise<void>(resolve=>{releaseCompletion=resolve;});
  const previewRequested=new Promise<void>(resolve=>{previewArrived=resolve;});
  let oldReportId='';let newJobId='';
  let engine:Awaited<ReturnType<typeof connectEngine>>;
  engine=await connectEngine(page,{boundaryOverride:async(method,params)=>{
    // Delay transport delivery only. Every job, report and packet comes from
    // the actual engine; no fabricated result or report-selection state.
    if(method==='insights.report'){
      const response=await engine.rpc(method,params) as any;newJobId=response.job.id;return response;
    }
    if(method==='jobs.get'&&params.job_id===newJobId){await completionGate;return engine.rpc(method,params);}
    if(method==='insights.review_packet'&&params.report_id===oldReportId){
      const response=await engine.rpc(method,params);previewArrived();await previewGate;return response;
    }
  }});
  try{
    const log=await imported(engine,'synthetic-insights.log');
    const first=await engine.rpc('insights.report',{log_id:log.id,source_key:'dataflash',boot_epoch:0,interval_start_s:1,interval_end_s:2,max_gap_s:1,battery_id:'0'}) as any;
    let firstJob:any;
    await expect.poll(async()=>{firstJob=(await engine.rpc('jobs.get',{job_id:first.job.id}) as any).job;return firstJob.state;}).toBe('succeeded');
    oldReportId=firstJob.result.report.id;
    await page.goto('/');await expect(page.getByText('Engine connected',{exact:true})).toBeVisible();
    await page.getByRole('navigation').getByRole('button',{name:'Flight insights',exact:true}).click();
    const saved=page.getByRole('combobox',{name:'Preserved insight report',exact:true});
    await saved.selectOption(oldReportId);
    const report=page.getByRole('region',{name:'Preserved flight insight report'});
    await expect(report.locator('.insight-metrics article').filter({hasText:'Mean power'})).toContainText('340 W');
    await page.getByRole('combobox',{name:'Insight log',exact:true}).selectOption(log.id);
    await page.getByRole('combobox',{name:'Insight source',exact:true}).selectOption('dataflash');
    await page.getByRole('combobox',{name:'Insight clock segment',exact:true}).selectOption('0');
    await page.getByLabel('Insight interval start (seconds)',{exact:true}).fill('2');
    await page.getByLabel('Insight interval end (seconds)',{exact:true}).fill('4');
    await page.getByRole('button',{name:'Generate insight report',exact:true}).click();
    await expect.poll(()=>newJobId).not.toBe('');
    let newJob:any;
    await expect.poll(async()=>{newJob=(await engine.rpc('jobs.get',{job_id:newJobId}) as any).job;return newJob.state;}).toBe('succeeded');
    const newReportId=newJob.result.report.id;
    await page.getByRole('button',{name:'Preview exact review packet',exact:true}).click();
    await previewRequested;
    releaseCompletion();
    await expect(saved).toHaveValue(newReportId);
    await expect(report.locator('.insight-metrics article').filter({hasText:'Mean power'})).toContainText('410 W');
    releasePreview();
    await expect(page.getByRole('button',{name:'Preview exact review packet',exact:true})).toBeEnabled();
    await expect(page.getByTestId('review-packet')).toHaveCount(0);
    await expect(page.getByRole('button',{name:'Choose folder and export reviewed packet',exact:true})).toHaveCount(0);
    expect(engine.calls.some(call=>call.method==='insights.export_review')).toBe(false);
    await page.getByRole('button',{name:'Preview exact review packet',exact:true}).click();
    await expect(page.getByTestId('review-packet')).toBeVisible();
    const reviewed=JSON.parse((await page.getByTestId('review-packet').textContent())!);
    expect(reviewed.scope).toMatchObject({interval_start_s:2,interval_end_s:4});
    const folder=join(engine.data,'review-race-export');await mkdir(folder);engine.files.push(folder);
    await page.getByRole('button',{name:'Choose folder and export reviewed packet',exact:true}).click();
    await expect(page.getByText('Review packet saved locally',{exact:true})).toBeVisible();
    const path=await page.locator('.insights-workspace .success-note p').first().textContent();
    expect(JSON.parse(await readFile(path!,'utf8'))).toEqual(reviewed);
    // Boundary overrides are logged once when delegated and once when returned.
    const exportCalls=engine.calls.filter(call=>call.method==='insights.export_review');
    expect(exportCalls).toHaveLength(1);expect(exportCalls[0].params.report_id).toBe(newReportId);
    expect(engine.calls.filter(call=>call.method==='insights.review_packet').every(call=>call.params.report_id===oldReportId||call.params.report_id===newReportId)).toBe(true);
  }finally{releaseCompletion();releasePreview();await engine.close();}
});
