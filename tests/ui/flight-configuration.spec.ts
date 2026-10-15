import { expect, test } from '@playwright/test';
import { execFile } from 'node:child_process';
import { resolve } from 'node:path';
import { promisify } from 'node:util';
import type { EngineJob, InsightReport } from '../../src/api';
import { connectEngine } from './engine-harness';

test('flight-linked typed readbacks remain bounded in the report and partial acquisitions can be preserved without upgrading them', async ({page}, testInfo) => {
  test.setTimeout(90_000);
  const engine = await connectEngine(page);
  try {
    await engine.rpc('handshake'); // Finish the engine's initial catalog migration before fixture writes.
    // Seed archived synthetic MAVLink evidence through the existing backend
    // oracles: real decoding, reducer snapshots, identity/time alignment and
    // manifests. Browser reads, report generation and promotion use real RPCs.
    const python = process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python';
    const {stdout} = await promisify(execFile)(resolve(python), ['-c', `
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'engine' / 'tests'))
from q22_engine.store import Store
from q22_engine.logs import LogService
from q22_engine.collection import CollectionService
from q22_engine.alignment import AlignmentService
from test_flight_configuration import report_pair, readback
from test_collection import envelope
root = Path(sys.argv[1])
store = Store(root)
logs = LogService(store)
collection = CollectionService(store)
services = (store, logs, collection, AlignmentService(store, logs, collection))
values = [('A_UINT64', '18446744073709551615', 7)] + [(f'TEST_{index:04d}', str(index), 6) for index in range(69)]
reports, acquisitions = report_pair(services, root, values, values[:-1] + [('TEST_0068', '999', 6)])
capture = collection.status()['capture_session']
collection.stop_capture({'capture_session_id': capture['uuid'], **envelope(collection, capture['profile'])})
# Different controller UID excludes this partial readback from those flights.
partial = readback(services, root, partial=True, uid='999', start=70.4, end=71.4)
print(json.dumps({'scope': reports[0]['scope'], 'flight_id': reports[0]['flight_context']['flight_record_id'], 'complete_id': acquisitions[0]['id'], 'partial_id': partial['id']}))
`, engine.data], {maxBuffer: 1024 * 1024});
    const fixture = JSON.parse(stdout);
    let {job} = await engine.rpc('insights.report', {log_id: fixture.scope.log_id, source_key: fixture.scope.source_key,
      boot_epoch: fixture.scope.boot_epoch, interval_start_s: fixture.scope.interval_start_s,
      interval_end_s: fixture.scope.interval_end_s, max_gap_s: 1.1, flight_record_id: fixture.flight_id}) as {job: EngineJob};
    await expect.poll(async () => {
      job = (await engine.rpc('jobs.get', {job_id: job.id}) as {job: EngineJob}).job;
      return job.state;
    }).toBe('succeeded');
    const saved = job.result!.report as InsightReport;
    expect(saved.configuration).toMatchObject({status: 'flight_bound_readbacks', effective_configuration: 'unknown',
      preflight: [{acquisition_id: fixture.complete_id, parameter_count: 70, usable_for_full_comparison: true, preview_truncated: true}]});
    const snapshotId = saved.configuration!.preflight[0].snapshot_id;
    await page.goto('/');
    await expect(page.getByText('Engine connected', {exact: true})).toBeVisible();
    await page.getByRole('navigation').getByRole('button', {name: 'Flight insights', exact: true}).click();
    await page.getByRole('combobox', {name: 'Preserved insight report', exact: true}).selectOption(saved.id);
    const report = page.getByRole('region', {name: 'Preserved flight insight report', exact: true});
    const configuration = report.locator('section.panel').filter({has: page.getByRole('heading', {name: 'Flight-linked full parameter readbacks', exact: true})});
    await expect(configuration.getByText('Preflight readback preserved', {exact: true})).toBeVisible();
    const before = configuration.locator('section').filter({has: page.getByRole('heading', {name: 'Before flight', exact: true})});
    await expect(before).toContainText('70 recorded parameters');
    await expect(before).toContainText('Full set available for comparison');
    await before.getByText('Parameter preview and preserved snapshot', {exact: true}).click();
    await expect(before).toContainText(snapshotId);
    await expect(before.getByRole('cell', {name: '18446744073709551615', exact: true})).toBeVisible();
    await expect(before.getByRole('row')).toHaveCount(65);
    await expect(before).toContainText('The preview is shortened; full comparisons read the complete preserved set.');
    await configuration.screenshot({path: testInfo.outputPath('flight-linked-readbacks.png')});

    await page.getByRole('navigation').getByRole('button', {name: 'Prepare & watch', exact: true}).click();
    const acquisition = page.locator('.acquisition-workspace');
    await acquisition.getByRole('combobox', {name: 'Preserved acquisition', exact: true}).selectOption(fixture.partial_id);
    await expect(acquisition.locator('.acquisition-result')).toContainText('Partial acquisition');
    await expect(acquisition.locator('.acquisition-result')).toContainText('2 / 3');
    expect(engine.calls.some(call => call.method === 'parameters.promote_readback')).toBe(false);
    await acquisition.getByRole('button', {name: 'Add readback to parameter library', exact: true}).click();
    await expect(acquisition.getByRole('status')).toContainText('Preserved 2 parameter observations in snapshot');
    await expect(acquisition.getByText('Background operations · 0 active', {exact: true})).toBeVisible();
    const catalog = await engine.rpc('parameters.list') as any;
    const partial = catalog.snapshots.find((item: any) => item.acquisition_id === fixture.partial_id);
    expect(partial).toBeDefined();
    expect(partial.completeness).toBe('partial');
    expect(partial.usable_for_full_comparison).toBe(false);
    expect(partial.effective_configuration).toBe('unknown');
    await acquisition.getByRole('button', {name: 'Add readback to parameter library', exact: true}).click();
    await expect(acquisition.locator('.background-jobs .log-job')).toHaveCount(2);
    await expect(acquisition.getByText('Background operations · 0 active', {exact: true})).toBeVisible();
    const jobs = await engine.rpc('jobs.list') as {jobs: EngineJob[]};
    expect(jobs.jobs.filter(item => item.method === 'parameters.promote_readback').map(item => item.state)).toEqual(['succeeded', 'succeeded']);
    await expect.poll(async () => ((await engine.rpc('parameters.list') as any).snapshots.filter((item: any) => item.acquisition_id === fixture.partial_id)).length).toBe(1);
    expect(engine.calls.filter(call => call.method === 'parameters.promote_readback').map(call => call.params)).toEqual([
      {acquisition_id: fixture.partial_id}, {acquisition_id: fixture.partial_id},
    ]);
    await page.getByRole('navigation').getByRole('button', {name: 'Compare', exact: true}).click();
    await page.getByRole('tab', {name: 'Parameter snapshots', exact: true}).click();
    await page.getByRole('combobox', {name: /Reference snapshot/}).selectOption(snapshotId);
    await page.getByRole('combobox', {name: /Changed \/ loaded snapshot/}).selectOption(partial.id);
    await page.getByRole('button', {name: 'Compare', exact: true}).last().click();
    await expect(page.getByRole('heading', {name: 'ArduPilot parameter changes', exact: true})).toBeVisible();
    await expect(page.getByText('2 added', {exact: true})).toBeVisible();
    await expect(page.getByText('70 removed', {exact: true})).toBeVisible();
    await expect(page.getByText('Compatibility needs review', {exact: true})).toBeVisible();
    expect(engine.calls.filter(call => call.method === 'parameters.diff').at(-1)?.params).toEqual({before_id: snapshotId, after_id: partial.id});
    expect(engine.calls.some(call => /^(?:acquisition\.(?:parameters|logs)\.|session\.prepare_and_watch|parameters\.import)/.test(call.method))).toBe(false);
    const retained = await engine.rpc('insights.detail', {report_id: saved.id}) as {report: InsightReport};
    expect(retained.report).toEqual(saved);
  } finally { await engine.close(); }
});
