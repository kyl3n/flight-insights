import { expect, test } from '@playwright/test';
import { connectEngine } from './engine-harness';
import type { NetworkInspection } from '../../src/api';

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
  let syntheticInspectionCount = 0;
  // This test exercises recovery UI state, including a deliberately uncertain
  // repair. Native inspection remains covered by the separate host workflow.
  const inspection: NetworkInspection = {
    status:'ready', platform:'Synthetic test host',
    interfaces:[{name:'synthetic-adapter',index:1,up:true,ipv4:[{address:'10.0.0.10',prefix:24,network:'10.0.0.0/24'}]}],
    routes:[], default_routes:[], targets:[],
    firewall:{state:'unknown',reason:'Synthetic inspection fixture; no native firewall query is performed by this recovery test.'},
    limitations:['This response validates the recovery UI, not a physical network.'],
  };
  const engine = await connectEngine(page,{boundaryOverride:(method) => {
    // Test-only plans/outcomes: no host settings or real repair commands execute.
    if (method === 'network.capabilities') return {current_platform:'Synthetic test host',platforms:{'Synthetic test host':{inspection:'fixture',stream_probes:'fixture',repair:{state:'supported_automatic',automatic_apply:true,reason:'Test-only qualified boundary; no host settings change.'},qualification:'synthetic'}},layers:[],repair_safety:{allowed:true,state:'synthetic_grounded',reasons:[]}};
    if (method === 'network.plan') return {plan_id:'test-plan',state:'ready',title:'Synthetic boundary repair',commands:[['synthetic-network-command','add','10.0.0.10']],rollback_commands:[['synthetic-network-command','remove','10.0.0.10']],requires_elevation:true,warnings:['Test-only boundary response; no host settings change.']};
    if (method === 'network.apply') {persistedRepair=true;return {plan_id:'test-plan',state:'verification_failed',may_have_applied:true,message:'Synthetic verification failure; inspect host outcome.'};}
    if (method === 'network.recovery') return {status:'ready',warnings:[],repairs:persistedRepair?[{plan_id:'test-plan',state:'applied',title:'Synthetic boundary repair',commands:[],rollback_commands:[['synthetic-network-command','remove','10.0.0.10']],requires_elevation:true,warnings:[],ownership:'owned',may_have_applied:true,recovered:true,message:'Synthetic recorded ownership was revalidated.'}]:[]};
    if (method === 'network.rollback') {persistedRepair=false;return {plan_id:'test-plan',state:'rolled_back',message:'Synthetic rollback boundary acknowledged.'};}
    if (method === 'network.inspect' && crashNextInspection) {crashNextInspection=false;return {__q22TransportError:{code:'engine_crashed',message:'Synthetic native transport failure'}};}
    if (method === 'network.inspect') {syntheticInspectionCount++;return inspection;}
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
    expect(syntheticInspectionCount).toBe(2);
  } finally {await engine.close();}
});

test('experimental helper requires session opt-in and a fresh exact native plan without claiming qualification', async ({page}) => {
  // Typed helper boundary only: no enrollment, elevated process, host write, or
  // real repair runs here. Ordinary data/status calls still use the real engine.
  let optedIn=false;let expired=true;
  const capability=()=>({current_platform:'Synthetic bench',platforms:{'Synthetic bench':{inspection:'fixture',stream_probes:'fixture',repair:{state:optedIn?'experimental_enabled':'experimental_available',automatic_apply:optedIn,reason:'Synthetic enrolled helper; no physical qualification.',bench_available:true,bench_opt_in:optedIn,qualified:false},qualification:'not_qualified'}},layers:[],repair_safety:{allowed:true,state:'synthetic_disarmed',reasons:[]}});
  const inspection:NetworkInspection={status:'ready',platform:'Synthetic bench',interfaces:[{name:'test-adapter',index:1,up:true,ipv4:[{address:'10.0.0.10',prefix:24,network:'10.0.0.0/24'}]}],routes:[],default_routes:[],targets:[],firewall:{state:'unknown',reason:'Test-only fixture'},limitations:['No host inspection or write occurs in this simulated helper test.']};
  const engine=await connectEngine(page,{boundaryOverride:(method,params)=>{
    if(method==='network.capabilities')return capability();
    if(method==='network.bench_opt_in'){optedIn=params.enabled===true;return capability();}
    if(method==='network.inspect')return inspection;
    if(method==='network.plan')return {plan_id:'app-test-plan',state:'ready',title:'Synthetic enrolled helper candidate',commands:[],rollback_commands:[],requires_elevation:true,automatic_apply:optedIn,warnings:['Test-only native helper contract.'],native_review:{plan_id:'native-test-plan',review_fingerprint:'review-immutable-123',before_fingerprint:'before-observed-456',candidate:{kind:'host_route',destination:'10.0.0.2/32',interface:'test-adapter'},expires_utc_ms:Date.now()+(expired?-1000:60_000),rollback_after_ms:30_000,authority:'admin_enrolled_helper'}};
    if(method==='network.apply')return {plan_id:'app-test-plan',state:'applied',may_have_applied:true,message:'Synthetic native receipt only; no host changed.',native_state:'applied_temporary',rollback_deadline_utc_ms:Date.UTC(2030,0,1)};
    return undefined;
  }});
  try{
    await page.goto('/');await expect(page.getByText('Engine connected',{exact:true})).toBeVisible();
    await page.getByRole('navigation').getByRole('button',{name:'Network checks',exact:true}).click();
    const enable=page.getByRole('button',{name:'Enable experimental bench repairs',exact:true});
    await expect(enable).toBeDisabled();
    await expect(page.getByText('Qualification: Not established',{exact:true})).toBeVisible();
    expect(engine.calls.some(call=>['network.bench_opt_in','network.inspect','network.apply'].includes(call.method))).toBe(false);
    await page.getByLabel('I accept this experimental administrator-enrolled helper for this bench session and will review each temporary host change.').check();
    await enable.click();await expect(page.getByText('Experimental session enabled',{exact:true})).toBeVisible();
    await page.getByLabel('Device IPv4',{exact:true}).fill('10.0.0.2');await page.getByRole('button',{name:'Add source',exact:true}).click();
    await page.getByRole('button',{name:'Inspect computer',exact:true}).click();
    await page.getByRole('combobox',{name:'Repair type',exact:true}).selectOption('host_route');
    await page.getByRole('button',{name:'Preview exact changes',exact:true}).click();
    await expect(page.getByRole('heading',{name:'Administrator-enrolled helper plan',exact:true})).toBeVisible();
    await expect(page.getByText('native-test-plan',{exact:true})).toBeVisible();
    await expect(page.getByText('before-observed-456',{exact:true})).toBeVisible();
    await expect(page.getByText('review-immutable-123',{exact:true})).toBeVisible();
    await page.setViewportSize({width:640,height:900});
    expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(640);
    const consent=page.getByLabel('I reviewed the exact helper candidate, fingerprints, expiry, and rollback interval. Apply this temporary host change.');
    await consent.check();await expect(page.getByRole('button',{name:'Apply approved repair',exact:true})).toBeDisabled();
    expired=false;await page.getByRole('button',{name:'Preview exact changes',exact:true}).click();
    await expect(consent).not.toBeChecked();await expect(page.getByRole('button',{name:'Apply approved repair',exact:true})).toBeDisabled();
    await consent.check();await page.getByRole('button',{name:'Apply approved repair',exact:true}).click();
    await expect(page.getByText('Synthetic native receipt only; no host changed.',{exact:true}).first()).toBeVisible();
    await expect(page.getByText('2030-01-01T00:00:00.000Z',{exact:true}).first()).toBeVisible();
    expect(engine.calls.filter(call=>call.method==='network.apply').map(call=>call.params)).toEqual([{plan_id:'app-test-plan',confirm:true}]);
    await expect(page.getByText('Qualification: Not established',{exact:true})).toBeVisible();
    await page.getByRole('button',{name:'Disable experimental repair session',exact:true}).click();
    await expect(enable).toBeDisabled();
    await page.reload();await expect(page.getByText('Engine connected',{exact:true})).toBeVisible();
    await page.getByRole('navigation').getByRole('button',{name:'Network checks',exact:true}).click();
    await expect(enable).toBeDisabled();
    expect(engine.calls.filter(call=>call.method==='network.bench_opt_in').map(call=>call.params)).toEqual([{enabled:true},{enabled:false}]);
    expect(engine.calls.filter(call=>call.method==='network.apply')).toHaveLength(1);
  }finally{await engine.close();}
});
