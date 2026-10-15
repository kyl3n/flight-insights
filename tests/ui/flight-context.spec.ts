import { expect, test, type Page } from '@playwright/test';
import { createSocket } from 'node:dgram';
import { execFileSync } from 'node:child_process';
import { resolve } from 'node:path';
import type { CollectionStatus, FlightBundleDetail } from '../../src/api';
import type { FlightEnvironmentSnapshotData } from '../../src/FlightEnvironment';
import { connectEngine } from './engine-harness';

const feedApproval = 'I configured this separate live feed and approved its sender and aircraft source IDs. It is not log replay.';

async function openCollection(page: Page) {
  await page.getByRole('navigation').getByRole('button', {name: 'Prepare & watch', exact: true}).click();
}

const inspection: FlightEnvironmentSnapshotData = {
  schema_version: 1, id: null, content_sha256: null, observed_at: '2026-10-15T12:00:00Z', status: 'partial', scope: 'current_host',
  host: {os: 'Synthetic OS', distribution: null, distribution_version: null, release: 'Test release', version: 'Test build', architecture: 'test64', provenance: 'platform_api'},
  software: {
    application: {name: 'Q22 Flight Lab', version: null, build: null, provenance: 'unavailable'},
    engine: {name: 'Q22 analysis engine', version: 'test-engine', build: null, provenance: 'engine_module_version'},
  },
  detected_applications: [{name: 'QGroundControl', category: 'gcs', version: null, process_name: 'QGroundControl', provenance: 'running_process_name'}],
  declared_context: {provenance: 'operator_declared'},
  load: {memory_percent: null, available_memory_bytes: null, load_average_1m_5m_15m: null, engine_rss_bytes: null, provenance: 'unavailable'},
  coverage: {process_scan_status: 'complete', names_checked: 1, version_detection: 'not_available_from_process_names'},
  limitations: ['Synthetic fixture: a process name does not establish its version.'], errors: [],
};

test('flight-computer inspection is explicit and an entered GCS version remains separate from detected software', async ({page}) => {
  const engine = await connectEngine(page, {boundaryOverride: (method, params) => {
    if (method !== 'environment.inspect') return undefined;
    return {...inspection, declared_context: {...params.environment_context as object, provenance: 'operator_declared'}};
  }});
  try {
    await page.goto('/');
    await expect(page.getByText('Engine connected', {exact: true})).toBeVisible();
    await openCollection(page);
    const setup = page.locator('.environment-setup');
    await setup.locator('summary').click();
    expect(engine.calls.filter(call => call.method === 'environment.inspect')).toHaveLength(0);
    await setup.getByRole('button', {name: 'Inspect flight computer', exact: true}).click();
    const current = setup.getByRole('region', {name: 'Current flight computer inspection', exact: true});
    await expect(current).toContainText('Synthetic OS Test release');
    await expect(current.getByRole('row').filter({hasText: 'QGroundControl'})).toContainText('Version not detected');
    await expect(setup.getByLabel('Ground station version (if known)', {exact: true})).toBeVisible();
    await setup.getByLabel('Ground station name', {exact: true}).fill('QGroundControl');
    await setup.getByLabel('Ground station version (if known)', {exact: true}).fill('4.4.3');
    await setup.getByRole('button', {name: 'Add flight application', exact: true}).click();
    await setup.getByLabel('Other flight application 1', {exact: true}).fill('Flight video player');
    await setup.getByLabel('Application 1 version (if known)', {exact: true}).fill('2.0');
    await setup.getByRole('button', {name: 'Inspect flight computer', exact: true}).click();
    await expect(current.getByRole('row').filter({hasText: 'QGroundControl'})).toContainText('Version not detected');
    await expect(current.getByText('4.4.3', {exact: true})).toHaveCount(0);
    const calls = engine.calls.filter(call => call.method === 'environment.inspect');
    expect(calls).toHaveLength(2);
    expect(calls[1].params.environment_context).toMatchObject({gcs_name: 'QGroundControl', gcs_version: '4.4.3', applications: [{name: 'Flight video player', version: '2.0'}]});
    expect(engine.calls.some(call => ['session.prepare_and_watch', 'network.probe', 'network.apply'].includes(call.method))).toBe(false);
  } finally { await engine.close(); }
});

function heartbeatPackets(): {disarmed: string[]; armed: string[]} {
  const python = resolve(process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
  return JSON.parse(execFileSync(python, ['-c', [
    'import json',
    'from pymavlink.dialects.v20 import ardupilotmega as m',
    'sender=m.MAVLink(None,srcSystem=1,srcComponent=1)',
    'result={}',
    'for label,base_mode in [("disarmed",0),("armed",128)]:',
    ' frames=[]',
    ' for sequence in range(256):',
    '  sender.seq=sequence',
    '  frames.append(m.MAVLink_heartbeat_message(2,3,base_mode,0,3,3).pack(sender).hex())',
    ' result[label]=frames',
    'print(json.dumps(result))',
  ].join('\n')], {encoding: 'utf8'}));
}

test('the first flight needs no baseline and each later flight retains its own computer and declared software context', async ({page}, testInfo) => {
  test.setTimeout(90_000);
  type DelayedRefresh = {flightId: string; revision: number; requests: number; replies: number; gate: Promise<void>; release: () => void};
  let delayedRefresh: DelayedRefresh | null = null;
  let engine!: Awaited<ReturnType<typeof connectEngine>>;
  engine = await connectEngine(page, {boundaryOverride: async (method, params) => {
    const delayed = delayedRefresh;
    if (!delayed) return undefined;
    if (method === 'session.status') {
      const status = await engine.rpc(method, params) as CollectionStatus;
      // Simulate a newly announced revision while retaining real stored bundles.
      return {...status, flight_records: status.flight_records.map(record => record.id === delayed.flightId ? {...record, latest_manifest_revision: delayed.revision} : record)};
    }
    if (method === 'session.detail' && params.flight_record_id === delayed.flightId && params.association_offset === undefined && params.manifest_revision === undefined) {
      const bundle = await engine.rpc(method, params);
      delayed.requests++;
      await delayed.gate;
      delayed.replies++;
      return bundle;
    }
    return undefined;
  }});
  const sender = createSocket('udp4');
  const reservation = createSocket('udp4');
  const packets = heartbeatPackets();
  let timer: ReturnType<typeof setInterval> | undefined;
  let armed = false;
  let sequence = 0;
  try {
    await new Promise<void>(done => reservation.bind(0, '127.0.0.1', done));
    const port = (reservation.address() as {port: number}).port;
    await new Promise<void>(done => reservation.close(() => done()));
    await new Promise<void>(done => sender.bind(0, '127.0.0.1', done));
    await page.goto('/');
    await expect(page.getByText('Engine connected', {exact: true})).toBeVisible();
    await openCollection(page);
    await page.getByLabel('Aircraft profile name', {exact: true}).fill('Synthetic context test aircraft');
    await page.getByLabel('Observer local IPv4', {exact: true}).fill('127.0.0.1');
    await page.getByLabel('Expected sender IPv4', {exact: true}).fill('127.0.0.1');
    await page.getByLabel('Dedicated observer UDP port', {exact: true}).fill(String(port));
    const setup = page.locator('.environment-setup');
    await setup.locator('summary').click();
    await setup.getByLabel('Ground station name', {exact: true}).fill('Mission Planner');
    await setup.getByLabel('Ground station version (if known)', {exact: true}).fill('1.3.82');
    await setup.getByRole('textbox', {name: 'Flight computer notes (optional)', exact: true}).fill('First flight context');
    // No parameter snapshot, baseline, previous flight, or host inspection has
    // been added. Synthetic loopback heartbeats exercise only recorder flow.
    expect(engine.calls.some(call => ['parameters.import', 'sessions.import', 'logs.import', 'environment.inspect'].includes(call.method))).toBe(false);

    async function startAndRecord() {
      await page.getByLabel(feedApproval, {exact: true}).check();
      await page.getByRole('button', {name: 'Preview observer setup', exact: true}).click();
      await page.locator('.observer-preview').getByRole('button', {name: 'Prepare & watch', exact: true}).click();
      await expect(page.getByRole('heading', {name: 'App observer is running', exact: true})).toBeVisible();
      if (!timer) timer = setInterval(() => sender.send(Buffer.from(packets[armed ? 'armed' : 'disarmed'][sequence++ % 256], 'hex'), port, '127.0.0.1'), 100);
      await expect.poll(async () => (await engine.rpc('session.status') as CollectionStatus).recorder?.sources?.[0]?.armed_state).toBe('disarmed');
      armed = true;
      await expect.poll(async () => (await engine.rpc('session.status') as CollectionStatus).flight_records.length).toBe(1);
      const active = await engine.rpc('session.status') as CollectionStatus;
      armed = false;
      await expect.poll(async () => Boolean((await engine.rpc('session.status') as CollectionStatus).flight_records[0]?.disarm_evidence)).toBe(true);
      await page.getByRole('button', {name: 'Stop app observer', exact: true}).click();
      await expect(page.getByRole('heading', {name: 'App observer is running', exact: true})).toHaveCount(0);
      return {capture: active.active_capture_session!, record: active.flight_records[0]};
    }

    const first = await startAndRecord();
    await page.getByRole('button', {name: `Inspect interval ${first.record.display_number} bundle`, exact: true}).click();
    const historical = page.locator('.bundle-detail').getByRole('region', {name: 'Recorded flight computer and software', exact: true});
    await expect(historical.getByRole('table', {name: 'Operator-declared flight software and versions'}).getByRole('row').filter({hasText: 'Mission Planner'})).toContainText('1.3.82');
    await expect(historical).toContainText('First flight context');
    await expect(historical).toContainText('Operating system');
    const original = await engine.rpc('session.detail', {flight_record_id: first.record.id}) as {environment_snapshot: FlightEnvironmentSnapshotData};
    expect(original.environment_snapshot.scope).toBe('armed_interval_start');
    expect(original.environment_snapshot.declared_context.gcs_version).toBe('1.3.82');
    expect(original.environment_snapshot.id).toBeTruthy();

    const observerSetup = page.locator('.observer-setup');
    if (!(await observerSetup.evaluate(element => (element as HTMLDetailsElement).open))) await observerSetup.locator(':scope > summary').click();
    if (!(await setup.evaluate(element => (element as HTMLDetailsElement).open))) await setup.locator('summary').click();
    await setup.getByLabel('Ground station version (if known)', {exact: true}).fill('1.3.83');
    await setup.getByRole('textbox', {name: 'Flight computer notes (optional)', exact: true}).fill('Second flight after ground-station update');
    const second = await startAndRecord();
    const changed = await engine.rpc('session.detail', {flight_record_id: second.record.id}) as {environment_snapshot: FlightEnvironmentSnapshotData};
    expect(changed.environment_snapshot.declared_context.gcs_version).toBe('1.3.83');
    expect(changed.environment_snapshot.id).not.toBe(original.environment_snapshot.id);

    await page.reload();
    await expect(page.getByText('Engine connected', {exact: true})).toBeVisible();
    await openCollection(page);
    await page.getByRole('combobox', {name: 'Capture session', exact: true}).selectOption(first.capture.uuid);
    await page.getByRole('button', {name: `Inspect interval ${first.record.display_number} bundle`, exact: true}).click();
    await expect(historical.getByRole('table', {name: 'Operator-declared flight software and versions'}).getByRole('row').filter({hasText: 'Mission Planner'})).toContainText('1.3.82');
    await expect(historical).not.toContainText('1.3.83');
    await expect(historical).toContainText('First flight context');
    const retained = await engine.rpc('session.detail', {flight_record_id: first.record.id}) as FlightBundleDetail;
    expect(retained.environment_snapshot).toEqual(original.environment_snapshot);
    await page.screenshot({path: testInfo.outputPath('recorded-flight-context.png'), fullPage: true});

    async function holdAutomaticRefresh() {
      let release!: () => void;
      const gate = new Promise<void>(resolve => { release = resolve; });
      delayedRefresh = {flightId: first.record.id, revision: retained.manifest.revision + 1, requests: 0, replies: 0, gate, release};
      await page.getByRole('button', {name: 'Refresh collection', exact: true}).click();
      await expect.poll(() => delayedRefresh?.requests || 0).toBeGreaterThan(0);
      return delayedRefresh;
    }

    async function releaseAutomaticRefresh(delayed: DelayedRefresh) {
      delayedRefresh = null;
      delayed.release();
      await expect.poll(() => delayed.replies).toBe(delayed.requests);
      // Let the completed native replies and their React updates reach the UI
      // before checking that an older selection was not restored.
      await page.evaluate(async () => {
        await new Promise<void>(resolve => requestAnimationFrame(() => resolve()));
        await new Promise<void>(resolve => requestAnimationFrame(() => resolve()));
      });
    }

    await test.step('a delayed automatic refresh cannot replace a preserved manifest revision', async () => {
      const earlierRevision = Math.min(...retained.revisions.map(revision => revision.revision));
      expect(earlierRevision).toBeLessThan(retained.manifest.revision);
      const delayed = await holdAutomaticRefresh();
      const bundle = page.locator('.bundle-detail');
      await bundle.getByRole('combobox', {name: 'Evidence manifest', exact: true}).selectOption(String(earlierRevision));
      await expect(bundle.getByText('Viewing a preserved evidence manifest.', {exact: false})).toBeVisible();
      await releaseAutomaticRefresh(delayed);
      await expect(bundle.getByRole('combobox', {name: 'Evidence manifest', exact: true})).toHaveValue(String(earlierRevision));
      await expect(bundle.locator('.panel-heading p')).toContainText(`Manifest revision ${earlierRevision} ·`);
      await expect(historical).toContainText('First flight context');
      await bundle.getByRole('combobox', {name: 'Evidence manifest', exact: true}).selectOption('');
      await expect(bundle.locator('.panel-heading p')).toContainText(`Manifest revision ${retained.manifest.revision} ·`);
      await page.getByRole('button', {name: 'Refresh collection', exact: true}).click();
    });

    await test.step('a delayed automatic refresh cannot replace a newly selected flight', async () => {
      const delayed = await holdAutomaticRefresh();
      await page.getByRole('combobox', {name: 'Capture session', exact: true}).selectOption(second.capture.uuid);
      await page.getByRole('button', {name: `Inspect interval ${second.record.display_number} bundle`, exact: true}).click();
      await expect(historical).toContainText('Second flight after ground-station update');
      await releaseAutomaticRefresh(delayed);
      await expect(page.locator('.bundle-detail').getByRole('heading', {name: `Bundle for interval ${second.record.display_number}`, exact: true})).toBeVisible();
      await expect(historical.getByRole('table', {name: 'Operator-declared flight software and versions'}).getByRole('row').filter({hasText: 'Mission Planner'})).toContainText('1.3.83');
      await expect(historical).not.toContainText('First flight context');
    });
  } finally {
    delayedRefresh?.release();
    if (timer) clearInterval(timer);
    try { sender.close(); } catch {}
    try { reservation.close(); } catch {}
    await engine.close();
  }
});
