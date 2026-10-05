import { expect, test } from '@playwright/test';
import { connectEngine } from './engine-harness';

test('source configuration survives reload without probing and fits compact screens', async ({page},testInfo) => {
  const engine = await connectEngine(page);
  try {
    await page.goto('/');
    await expect(page.getByText('Engine connected',{exact:true})).toBeVisible();
    await page.getByRole('navigation').getByRole('button',{name:'Network checks'}).click();
    await page.getByLabel('Device IPv4',{exact:true}).fill('10.0.0.2');
    await page.getByRole('button',{name:'Add source',exact:true}).click();
    await expect(page.getByText('1 configured',{exact:true})).toBeVisible();
    expect(engine.calls.some(call => ['network.inspect','network.probe','network.apply','network.rollback'].includes(call.method))).toBeFalsy();
    await page.reload();
    await expect(page.getByText('Engine connected',{exact:true})).toBeVisible();
    await page.getByRole('navigation').getByRole('button',{name:'Network checks'}).click();
    await expect(page.getByText('1 configured',{exact:true})).toBeVisible();
    await expect(page.getByRole('button',{name:'Remove digiQ / SkyPulse 10.0.0.2'})).toBeVisible();
    await expect(page.getByText('No inspection yet',{exact:true})).toBeVisible();
    expect(engine.calls.some(call => ['network.inspect','network.probe','network.apply','network.rollback'].includes(call.method))).toBeFalsy();
    for (const width of [640,360]) {
      await page.setViewportSize({width,height:900});
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
      await expect(page.getByRole('button',{name:'Add source',exact:true})).toBeVisible();
    }
    await page.screenshot({path:testInfo.outputPath('network-compact.png'),fullPage:true});
  } finally {await engine.close();}
});

test('simulated native failure restores durable repair recovery and requires explicit rollback acknowledgement', async ({page}) => {
  let crashNextInspection = false;
  let persistedRepair = false;
  const engine = await connectEngine(page,{boundaryOverride:(method) => {
    // Test-only plans/outcomes: no host settings or real repair commands execute.
    if (method === 'network.plan') return {plan_id:'test-plan',state:'ready',title:'Synthetic boundary repair',commands:[['synthetic-network-command','add','10.0.0.10']],rollback_commands:[['synthetic-network-command','remove','10.0.0.10']],requires_elevation:true,warnings:['Test-only boundary response; no host settings change.']};
    if (method === 'network.apply') {persistedRepair=true;return {plan_id:'test-plan',state:'verification_failed',may_have_applied:true,message:'Synthetic verification failure; inspect host outcome.'};}
    if (method === 'network.recovery') return {status:'ready',warnings:[],repairs:persistedRepair?[{plan_id:'test-plan',state:'applied',title:'Synthetic boundary repair',commands:[],rollback_commands:[['synthetic-network-command','remove','10.0.0.10']],requires_elevation:true,warnings:[],ownership:'owned',may_have_applied:true,recovered:true,message:'Synthetic recorded ownership was revalidated.'}]:[]};
    if (method === 'network.rollback') {persistedRepair=false;return {plan_id:'test-plan',state:'rolled_back',message:'Synthetic rollback boundary acknowledged.'};}
    if (method === 'network.inspect' && crashNextInspection) {crashNextInspection=false;return {__q22TransportError:{code:'engine_crashed',message:'Synthetic native transport failure'}};}
    if (method === 'engine.restart') return {protocol_version:1,engine_version:'test-native-boundary'};
    return undefined;
  }});
  try {
    await page.goto('/');
    await expect(page.getByText('Engine connected',{exact:true})).toBeVisible();
    await page.getByRole('navigation').getByRole('button',{name:'Network checks'}).click();
    await page.getByLabel('Device IPv4',{exact:true}).fill('10.0.0.2');
    await page.getByRole('button',{name:'Add source',exact:true}).click();
    await page.getByRole('button',{name:'Inspect computer',exact:true}).click();
    await expect(page.getByText('Firewall visibility',{exact:true})).toBeVisible();
    await page.getByLabel('Unused local IPv4 address',{exact:true}).fill('10.0.0.10');
    await page.getByRole('button',{name:'Preview exact changes'}).click();
    await expect(page.getByRole('button',{name:'Apply approved repair',exact:true})).toBeDisabled();
    await page.getByLabel('I reviewed the adapter, address, exact commands, and rollback. Apply this host change.').check();
    await page.getByRole('button',{name:'Apply approved repair',exact:true}).click();
    await expect(page.getByRole('button',{name:'Undo this repair',exact:true})).toBeVisible();
    await page.getByRole('button',{name:'Inspect computer',exact:true}).click();
    await expect(page.getByRole('button',{name:'Undo this repair',exact:true})).toBeVisible();
    await page.getByRole('button',{name:'Remove digiQ / SkyPulse 10.0.0.2'}).click();
    await expect(page.getByRole('button',{name:'Undo this repair',exact:true})).toBeVisible();
    crashNextInspection=true;
    await page.getByRole('button',{name:'Inspect computer',exact:true}).click();
    await expect(page.getByText('Local engine is not connected',{exact:true})).toBeVisible();
    await page.getByText('Engine recovery',{exact:true}).click();
    await expect(page.getByRole('button',{name:'Restart analysis engine',exact:true})).toBeDisabled();
    expect(engine.calls.some(call => call.method === 'engine.restart')).toBeFalsy();
    await page.getByLabel('I have saved any required rollback commands and will inspect the outcome before retrying.').check();
    await page.getByRole('button',{name:'Restart analysis engine',exact:true}).click();
    await expect(page.getByText('Engine connected',{exact:true})).toBeVisible();
    await expect(page.getByText('Recovered from local journal',{exact:true})).toBeVisible();
    await page.reload();
    await expect(page.getByText('Engine connected',{exact:true})).toBeVisible();
    await page.getByRole('navigation').getByRole('button',{name:'Network checks'}).click();
    await expect(page.getByText('Recovered from local journal',{exact:true})).toBeVisible();
    await expect(page.getByText("'synthetic-network-command' 'remove' '10.0.0.10'",{exact:true})).toBeVisible();
    await expect(page.getByRole('button',{name:'Undo this repair',exact:true})).toBeDisabled();
    await page.getByLabel('I reviewed the exact rollback for Synthetic boundary repair and want to undo this host change.').check();
    await page.getByRole('button',{name:'Undo this repair',exact:true}).click();
    await expect(page.getByText('Synthetic rollback boundary acknowledged.',{exact:true})).toBeVisible();
    expect(engine.calls.filter(call => call.method === 'network.apply')).toHaveLength(1);
    expect(engine.calls.filter(call => call.method === 'engine.restart')).toHaveLength(1);
    expect(engine.calls.filter(call => call.method === 'network.rollback')).toHaveLength(1);
  } finally {await engine.close();}
});
