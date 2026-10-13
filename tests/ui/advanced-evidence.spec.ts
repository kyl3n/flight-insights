import { expect, test } from '@playwright/test';
import { resolve } from 'node:path';
import { createSocket } from 'node:dgram';
import { connectEngine } from './engine-harness';
async function importLog(engine:Awaited<ReturnType<typeof connectEngine>>,file:string){const result=await engine.rpc('logs.import',{path:resolve('fixtures/logs',file),label:`Synthetic ${file}`,synthetic:true}) as any;let job:any;await expect.poll(async()=>{job=(await engine.rpc('jobs.get',{job_id:result.job.id}) as any).job;return job.state;}).toBe('succeeded');return job.result.log;}

test('control, batch spectrum, and static filter model retain measured versus hypothetical boundaries',async({page},testInfo)=>{
  const engine=await connectEngine(page);
  try{
    const log=await importLog(engine,'synthetic-control-batch.log');
    await page.goto('/');await expect(page.getByText('Engine connected',{exact:true})).toBeVisible();
    await page.getByRole('navigation').getByRole('button',{name:'Flight insights',exact:true}).click();
    await page.getByRole('combobox',{name:'Insight log',exact:true}).selectOption(log.id);
    await page.getByRole('combobox',{name:'Insight source',exact:true}).selectOption('dataflash');
    await page.getByRole('combobox',{name:'Insight clock segment',exact:true}).selectOption('0');
    await page.getByLabel('Insight interval start (seconds)',{exact:true}).fill('0');await page.getByLabel('Insight interval end (seconds)',{exact:true}).fill('4');
    await page.getByText('Operating context · mass, propulsion, and battery',{exact:true}).click();
    await page.getByLabel('Declared firmware for guidance scope',{exact:true}).fill('Copter-4.7.1');await page.getByLabel('Declared flight phase',{exact:true}).fill('hover');
    await page.getByText('Rate tracking, PID activity, and optional step review',{exact:true}).click();
    await page.getByLabel('Review an explicitly identified command step',{exact:true}).check();await page.getByLabel('Command step time (seconds)',{exact:true}).fill('2');
    await page.getByText('Hypothetical static filter model',{exact:true}).click();await page.getByLabel('Include an operator-defined static filter model',{exact:true}).check();
    await page.getByLabel('Model sample rate (Hz)',{exact:true}).fill('400');await page.getByRole('combobox',{name:'Stage 1 filter kind',exact:true}).selectOption('ardupilot_static_notch');
    await page.getByLabel('Stage 1 center (Hz)',{exact:true}).fill('50');await page.getByLabel('Stage 1 bandwidth (Hz)',{exact:true}).fill('20');await page.getByLabel('Stage 1 attenuation (dB)',{exact:true}).fill('30');
    await page.getByText('Optional gyro spectrum · qualified samples only',{exact:true}).click();await page.getByLabel('Provide sampling evidence for a conditional spectrum',{exact:true}).check();
    await page.getByRole('combobox',{name:'Spectral acquisition source',exact:true}).selectOption('batch_imu');await page.getByRole('combobox',{name:'Recorded filter stage',exact:true}).selectOption('pre');
    await page.getByLabel('Declared sensor sample rate (Hz)',{exact:true}).fill('400');await page.getByLabel('Declared anti-alias cutoff (Hz)',{exact:true}).fill('100');await page.getByRole('combobox',{name:'Window samples',exact:true}).selectOption('256');await page.getByLabel('Anti-alias evidence description',{exact:true}).fill('Synthetic fixture acquisition evidence only; not a real aircraft configuration.');
    await page.getByRole('button',{name:'Generate insight report',exact:true}).click();
    const control=page.locator('.control-analysis');await expect(control.getByRole('heading',{name:'Rate control evidence',exact:true})).toBeVisible();
    await expect(control.getByRole('img',{name:'Observed desired and achieved angular rate',exact:true})).toBeVisible();await expect(control.getByRole('heading',{name:'Selected step response · Observed response',exact:true})).toBeVisible();
    await expect(control).toContainText('0.44 s');await expect(control).toContainText('0.6 s');
    const model=page.locator('.filter-model');await expect(model).toContainText('Operator-defined model · not measured aircraft response');await expect(model.getByRole('img',{name:/Modeled static filter/})).toBeVisible();
    await model.getByRole('combobox',{name:'Modeled response quantity',exact:true}).selectOption('group_delay_s');await expect(model.getByRole('img',{name:/Group delay/})).toBeVisible();
    await expect(page.getByRole('heading',{name:'Qualified spectral windows',exact:true})).toBeVisible();await expect(page.getByRole('img',{name:'Spectral density by qualified time window',exact:true})).toBeVisible();
    const reports=await engine.rpc('insights.list') as any;const {report}=await engine.rpc('insights.detail',{report_id:reports.reports[0].id}) as any;
    expect(report.spectrum.source).toBe('batch_imu');expect(report.spectrum.peaks[0].frequency_hz).toBeCloseTo(50);expect(report.filter_model.response.find((row:any)=>row.frequency_hz===50).attenuation_db).toBeCloseTo(30);
    expect(report.filter_model.qualification.parameter_application_allowed).toBe(false);expect(report.control.step.status).toBe('observed_response');
    await control.scrollIntoViewIfNeeded();await page.screenshot({path:testInfo.outputPath('rate-and-filter-evidence.png')});
    await page.setViewportSize({width:640,height:900});expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(640);
  }finally{await engine.close();}
});

test('source alignment uses actual identity and clocks while integrity verification remains explicit',async({page})=>{
  const engine=await connectEngine(page);
  try{
    const reference=await importLog(engine,'synthetic-alignment-reference.tlog');const candidate=await importLog(engine,'synthetic-alignment-candidate.tlog');
    await page.goto('/');await expect(page.getByText('Engine connected',{exact:true})).toBeVisible();
    await page.getByRole('navigation').getByRole('button',{name:'Prepare & watch',exact:true}).click();await page.getByText('Align original sources and clock evidence',{exact:true}).click();
    const selection={log_id:reference.id,source_key:reference.streams[0].source_key,boot_epoch:reference.streams[0].boot_epoch};
    await page.getByRole('combobox',{name:'Alignment reference source',exact:true}).selectOption(JSON.stringify(selection));
    await page.locator('.candidate-choices').getByRole('checkbox',{name:new RegExp('synthetic-alignment-candidate')}).check();
    expect(engine.calls.some(call=>call.method==='alignment.analyze')).toBe(false);
    await page.getByRole('button',{name:'Analyze source alignment',exact:true}).click();await expect(page.getByRole('heading',{name:'Source alignment evidence',exact:true})).toBeVisible();
    await expect(page.locator('.alignment-report').getByText('Verified',{exact:true})).toBeVisible();
    await expect(page.getByRole('button',{name:'Recheck and attach verified matches',exact:true})).toBeDisabled();
    expect(engine.calls.some(call=>call.method==='alignment.attach')).toBe(false);
    const reports=await engine.rpc('alignment.list') as any;const {report}=await engine.rpc('alignment.get',{report_id:reports.reports[0].id}) as any;
    expect(report.candidates[0].selection.log_id).toBe(candidate.id);expect(report.candidates[0].state).toBe('verified');expect(report.parameter_timeline.attribution_allowed).toBe(false);
    await page.getByRole('navigation').getByRole('button',{name:'Evidence',exact:true}).click();await page.getByText('Catalog version & original integrity',{exact:true}).click();
    expect(engine.calls.some(call=>call.method==='catalog.verify')).toBe(false);
    await page.getByRole('button',{name:'Verify archived originals',exact:true}).click();await expect(page.getByRole('heading',{name:'Original integrity result',exact:true})).toBeVisible();await expect(page.locator('.catalog-integrity').getByText('Passed',{exact:true})).toBeVisible();
    expect(engine.calls.filter(call=>call.method==='catalog.verify')).toHaveLength(1);
  }finally{await engine.close();}
});

test('saved automatic acquisition policy needs per-Prepare approval and never resumes by renderer reload',async({page})=>{
  const engine=await connectEngine(page);const socket=createSocket('udp4');
  try{
    await new Promise<void>(done=>socket.bind(0,'127.0.0.1',done));const observerPort=(socket.address() as {port:number}).port;await new Promise<void>(done=>socket.close(()=>done()));
    await page.goto('/');await expect(page.getByText('Engine connected',{exact:true})).toBeVisible();await page.getByRole('navigation').getByRole('button',{name:'Prepare & watch',exact:true}).click();
    await page.getByLabel('Aircraft profile name',{exact:true}).fill('Synthetic policy UI test');await page.getByLabel('Dedicated observer UDP port',{exact:true}).fill(String(observerPort));await page.getByLabel('Expected sender IPv4',{exact:true}).fill('127.0.0.1');await page.getByLabel('Expected controller UID (decimal, if known)',{exact:true}).fill('12345');
    await page.getByText('Profile versions and test provenance',{exact:true}).click();await page.getByLabel('This feed contains synthetic bench-test data (live intervals will not be promoted)',{exact:true}).check();await page.getByLabel('I configured this separate live feed and approved its sender and aircraft source IDs. It is not log replay.',{exact:true}).check();
    await page.getByRole('button',{name:'Preview observer setup',exact:true}).click();await page.getByText('Create a bounded acquisition policy',{exact:true}).click();
    await page.getByLabel('Policy remote IPv4',{exact:true}).fill('127.0.0.1');await page.getByLabel('Policy remote UDP port',{exact:true}).fill('14590');
    await page.getByRole('button',{name:'Review acquisition policy',exact:true}).click();await page.getByRole('button',{name:'Save reviewed policy',exact:true}).click();
    expect(engine.calls.some(call=>call.method==='session.prepare_and_watch')).toBe(false);expect(engine.calls.some(call=>/^acquisition\.(parameters|logs)\./.test(call.method))).toBe(false);
    const prepare=page.locator('.observer-preview').getByRole('button',{name:'Prepare & watch',exact:true});await expect(prepare).toBeDisabled();
    await page.getByLabel('Approve the displayed automatic readbacks for this capture only',{exact:true}).check();await expect(prepare).toBeDisabled();await page.getByLabel('I reviewed this experimental bidirectional adapter and its separate endpoint',{exact:true}).check();await expect(prepare).toBeEnabled();
    await prepare.click();await expect(page.getByRole('heading',{name:'App observer is running',exact:true})).toBeVisible();
    await expect(page.locator('.orchestration-workspace').getByText('Runtime approved',{exact:true})).toBeVisible();
    const call=engine.calls.find(item=>item.method==='session.prepare_and_watch');expect(call?.params.acquisition_policy).toMatchObject({confirm_automatic_readback:true,confirm_experimental_adapter:true});
    await page.getByRole('button',{name:'Stop app observer',exact:true}).click();await expect(page.getByRole('heading',{name:'App observer is running',exact:true})).toHaveCount(0);
    await page.reload({timeout:10_000});await expect(page.getByText('Engine connected',{exact:true})).toBeVisible();expect(engine.calls.filter(item=>item.method==='session.prepare_and_watch')).toHaveLength(1);
    expect(engine.calls.filter(item=>item.method==='orchestration.policy.save')).toHaveLength(1);
  }finally{try{socket.close();}catch{}await engine.close();}
});
