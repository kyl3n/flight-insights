import { expect, test, type Page } from '@playwright/test';
import { resolve } from 'node:path';
import { connectEngine } from './engine-harness';

async function openLogs(page:Page){
  await page.getByRole('navigation').getByRole('button',{name:'Evidence',exact:true}).click();
  await page.getByRole('tab',{name:'Flight log',exact:true}).click();
  return page.locator('#flight-log-panel');
}

test('BIN import preserves boot and parameter evidence and creates comparable scoped sessions',async({page},testInfo)=>{
  const engine=await connectEngine(page);
  try{
    await page.goto('/');await expect(page.getByText('Engine connected',{exact:true})).toBeVisible();
    const panel=await openLogs(page);
    engine.files.push(resolve('fixtures/logs/synthetic-flight.bin'));
    await expect(panel.getByRole('button',{name:'Choose log file',exact:true})).toBeEnabled();
    await panel.getByRole('button',{name:'Choose log file',exact:true}).click();
    await panel.getByLabel('Log label',{exact:true}).fill('Synthetic decoded BIN');
    await panel.getByLabel('This log contains synthetic test data').check();
    await panel.getByRole('button',{name:'Import flight log',exact:true}).click();
    await expect(panel.getByRole('heading',{name:'Synthetic decoded BIN',exact:true})).toBeVisible();
    await expect(panel.getByText('Keep sources and clock segments separate',{exact:true})).toBeVisible();
    await panel.getByRole('combobox',{name:'Recorded source',exact:true}).selectOption('dataflash');
    await panel.getByRole('combobox',{name:'Clock segment',exact:true}).selectOption('1');
    await expect(panel.getByText('No usable timed interval',{exact:true})).toBeVisible();
    await expect(panel.getByRole('button',{name:'Create scoped session'})).toBeDisabled();
    await panel.getByRole('combobox',{name:'Clock segment',exact:true}).selectOption('0');
    await expect(panel.getByRole('cell',{name:'ATC_RAT_RLL_P',exact:true}).first()).toBeVisible();
    await expect(panel.getByText('Observed values only',{exact:true})).toBeVisible();
    await expect(panel.getByRole('combobox',{name:'Battery monitor instance',exact:true})).toHaveValue('0');
    await page.evaluate(()=>{if(document.activeElement instanceof HTMLElement)document.activeElement.blur();});
    await page.screenshot({path:testInfo.outputPath('flight-log-evidence.png'),fullPage:true});
    await page.setViewportSize({width:640,height:900});
    expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(640);
    await page.evaluate(()=>{if(document.activeElement instanceof HTMLElement)document.activeElement.blur();});
    await page.screenshot({path:testInfo.outputPath('flight-log-evidence-compact.png'),fullPage:true});
    await page.setViewportSize({width:1440,height:1000});
    await panel.getByLabel('Declared aircraft identity',{exact:true}).fill('synthetic-aircraft');
    await panel.getByLabel('Declared controller board',{exact:true}).fill('synthetic-board');
    await panel.getByLabel('Declared firmware identity',{exact:true}).fill('synthetic-fw-1');
    await panel.getByLabel('Flight phase',{exact:true}).fill('hover');
    for(const entry of [{label:'Synthetic before setting change',start:'1',end:'2'},{label:'Synthetic after setting change',start:'3',end:'5'}]){
      await panel.getByLabel('Session label',{exact:true}).fill(entry.label);
      await panel.getByLabel('Interval start (seconds)',{exact:true}).fill(entry.start);
      await panel.getByLabel('Interval end (seconds)',{exact:true}).fill(entry.end);
      await panel.getByRole('button',{name:'Create scoped session'}).click();
      await expect(page.getByRole('heading',{name:entry.label,exact:true})).toBeVisible();
    }
    await expect(page.getByText(/UTC unavailable/).first()).toBeVisible();
    const requests=engine.calls.filter(call=>call.method==='logs.session');
    expect(requests).toHaveLength(2);
    expect(requests.every(call=>call.params.source_key==='dataflash'&&call.params.boot_epoch===0&&call.params.battery_id==='0')).toBeTruthy();
    await page.getByRole('navigation').getByRole('button',{name:'Compare',exact:true}).click();
    await page.getByLabel(/Reference flight/).selectOption({label:'Synthetic before setting change [synthetic]'});
    await page.getByLabel(/Flight to evaluate/).selectOption({label:'Synthetic after setting change [synthetic]'});
    await page.getByRole('button',{name:'Compare',exact:true}).last().click();
    await expect(page.getByText('1 changed',{exact:true})).toBeVisible();
    await expect(page.getByRole('row').filter({hasText:'Mean power'})).toContainText('240 W');
    await expect(page.getByText('Conditions limit this comparison',{exact:true})).toBeVisible();
    expect(engine.calls.some(call=>call.method==='jobs.get')).toBeTruthy();
    await openLogs(page);
    await panel.getByLabel('Session label',{exact:true}).fill('Synthetic interval crossing setting change');
    await panel.getByLabel('Interval start (seconds)',{exact:true}).fill('1');
    await panel.getByLabel('Interval end (seconds)',{exact:true}).fill('5');
    await panel.getByRole('button',{name:'Create scoped session'}).click();
    const crossing=page.locator('.evidence-card').filter({has:page.getByRole('heading',{name:'Synthetic interval crossing setting change',exact:true})});
    await expect(crossing).toBeVisible();
    await expect(crossing.locator('summary').filter({hasText:'Mean power'})).toContainText('Unavailable');
    await crossing.getByText('Logged configuration and split boundaries',{exact:true}).click();
    await expect(crossing.getByText('Suggested interval boundaries:',{exact:true})).toBeVisible();
    await expect(crossing.getByRole('cell',{name:'3 s',exact:true})).toBeVisible();
    await crossing.locator('summary').filter({hasText:'Mean power'}).click();
    await expect(crossing.locator('.metric-details>details[open] .metric-unavailable-reason')).toBeVisible();
    await page.screenshot({path:testInfo.outputPath('log-comparison.png'),fullPage:true});
    await page.reload();await expect(page.getByText('Engine connected',{exact:true})).toBeVisible();
    await openLogs(page);await expect(panel.getByRole('button',{name:/Synthetic decoded BIN/})).toBeVisible();
    expect(engine.calls.filter(call=>call.method==='logs.import')).toHaveLength(1);
  }finally{await engine.close();}
});

test('TLOG import keeps MAVLink sources separate and reports unavailable tracking honestly',async({page},testInfo)=>{
  const engine=await connectEngine(page);
  try{
    await page.goto('/');await expect(page.getByText('Engine connected',{exact:true})).toBeVisible();
    const panel=await openLogs(page);
    engine.files.push(resolve('fixtures/logs/synthetic-flight.tlog'));
    await expect(panel.getByRole('button',{name:'Choose log file',exact:true})).toBeEnabled();
    await panel.getByRole('button',{name:'Choose log file',exact:true}).click();
    await panel.getByLabel('Log label',{exact:true}).fill('Synthetic recorded MAVLink');
    await panel.getByLabel('This log contains synthetic test data').check();
    await panel.getByRole('button',{name:'Import flight log',exact:true}).click();
    await expect(panel.getByRole('heading',{name:'Synthetic recorded MAVLink',exact:true})).toBeVisible();
    await expect(panel.getByRole('combobox',{name:'Recorded source',exact:true}).locator('option')).toHaveText(['Choose source explicitly','1:1','2:1']);
    await panel.getByRole('combobox',{name:'Recorded source',exact:true}).selectOption('1:1');
    await panel.getByRole('combobox',{name:'Clock segment',exact:true}).selectOption('0');
    await panel.getByLabel('Session label',{exact:true}).fill('Synthetic TLOG scope');
    await panel.getByLabel('Declared aircraft identity',{exact:true}).fill('synthetic-aircraft');
    await panel.getByLabel('Declared controller board',{exact:true}).fill('synthetic-board');
    await panel.getByLabel('Declared firmware identity',{exact:true}).fill('synthetic-fw-1');
    await panel.getByLabel('Flight phase',{exact:true}).fill('operator-selected');
    await panel.getByLabel('Interval start (seconds)',{exact:true}).fill('0.75');
    await panel.getByLabel('Interval end (seconds)',{exact:true}).fill('2.25');
    await panel.getByRole('button',{name:'Create scoped session'}).click();
    const card=page.locator('.evidence-card').filter({has:page.getByRole('heading',{name:'Synthetic TLOG scope',exact:true})});
    await expect(card).toBeVisible();
    await expect(card.locator('summary').filter({hasText:'Roll tracking RMSE'})).toContainText('Unavailable');
    await card.getByText('Logged configuration and split boundaries',{exact:true}).click();
    await expect(card.getByText('Partial logged configuration',{exact:true})).toBeVisible();
    expect(engine.calls.filter(call=>call.method==='logs.session')[0].params).toMatchObject({source_key:'1:1',boot_epoch:0,battery_id:'battery_status:0'});
    await page.screenshot({path:testInfo.outputPath('tlog-evidence.png'),fullPage:true});
  }finally{await engine.close();}
});
