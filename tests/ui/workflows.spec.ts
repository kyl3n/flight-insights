import { expect, test } from '@playwright/test';
import { resolve } from 'node:path';
import { connectEngine } from './engine-harness';
import { openWorkspace } from './navigation';

test('browser shows unavailable capabilities without manufacturing data', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByText('Open the desktop app to analyze your flights')).toBeVisible();
  await openWorkspace(page, 'Network checks');
  await expect(page.getByRole('button', { name: 'Inspect computer' })).toBeDisabled();
  await openWorkspace(page, 'Compare');
  await expect(page.getByText('Build a comparison from real evidence')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Compare', exact: true }).last()).toBeDisabled();
});

test('desktop UI imports original snapshots and displays an actual engine diff', async ({ page }) => {
  const engine = await connectEngine(page);
  try {
    await page.goto('/');
    await expect(page.getByText('Engine connected', { exact: true })).toBeVisible();
    await openWorkspace(page, 'Evidence');
    const imports = [
      { file: 'synthetic-before.param', label: 'Synthetic baseline' },
      { file: 'synthetic-after.parm', label: 'Synthetic candidate' },
    ];
    for (const item of imports) {
      engine.files.push(resolve('fixtures/parameters', item.file));
      await page.getByRole('button', { name: 'Choose file', exact: true }).click();
      await page.getByLabel('Evidence label', { exact: true }).fill(item.label);
      await page.getByLabel('Aircraft identity', { exact: true }).fill('synthetic-aircraft');
      await page.getByLabel('Controller board', { exact: true }).fill('synthetic-board');
      await page.getByLabel('Exact firmware identity', { exact: true }).fill('synthetic-fw-1');
      await page.getByLabel('Snapshot observed at (local time)', { exact: true }).fill('2026-10-05T08:00');
      await page.getByLabel('This file contains synthetic test data').check();
      await page.getByRole('button', { name: 'Import snapshot', exact: true }).click();
      await expect(page.getByRole('heading', { name: item.label, exact: true })).toBeVisible();
    }
    await openWorkspace(page, 'Compare');
    await page.getByRole('tab', { name: 'Parameter snapshots', exact: true }).click();
    await page.getByLabel(/Reference snapshot/).selectOption({ label: 'Synthetic baseline [synthetic]' });
    await page.getByLabel(/Changed \/ loaded snapshot/).selectOption({ label: 'Synthetic candidate [synthetic]' });
    await page.getByRole('button', { name: 'Compare', exact: true }).last().click();
    await expect(page.getByRole('cell', { name: /ATC_RAT_RLL_P/ })).toBeVisible();
    await expect(page.getByText('1 changed', { exact: true })).toBeVisible();
    await expect(page.getByText('1 added', { exact: true })).toBeVisible();
    await expect(page.getByText('1 removed', { exact: true })).toBeVisible();
    expect(engine.calls.filter(call => call.method === 'parameters.import')).toHaveLength(2);
    expect(engine.calls.some(call => call.method === 'parameters.diff')).toBeTruthy();
    // Reload retains evidence from SQLite, not React state.
    await page.reload();
    await expect(page.getByText('Engine connected', {exact: true})).toBeVisible();
    await openWorkspace(page, 'Evidence');
    await expect(page.getByText('2 imported', { exact: true }).first()).toBeVisible();
  } finally { await engine.close(); }
});

test('flight comparison uses snapshot-linked real engine metrics and displays confounders', async ({ page }, testInfo) => {
  const engine = await connectEngine(page);
  try {
    const sessions: any[] = [];
    for (const [index, file] of ['synthetic-before.param', 'synthetic-after.parm'].entries()) {
      const { snapshot } = await engine.rpc('parameters.import', {
        path: resolve('fixtures/parameters', file), label: `Synthetic tune ${index}`,
        aircraft_id: 'synthetic-aircraft', board: 'synthetic-board', firmware: 'synthetic-fw-1',
        source_kind: 'current_readback', observed_at: '2026-10-05T08:00:00Z', synthetic: true,
      }) as any;
      const { session } = await engine.rpc('sessions.import', {
        path: resolve('fixtures/performance', index ? 'synthetic-lower-power.csv' : 'synthetic-constant.csv'),
        label: `Synthetic flight ${index}`, snapshot_id: snapshot.id,
        aircraft_id: snapshot.aircraft_id, board: snapshot.board, firmware: snapshot.firmware,
        phase: 'hover', interval_start_s: 0, interval_end_s: 4, max_gap_s: 1.1,
        parameter_evidence: 'unverified', parameter_changes: 'unknown', conditions: {}, synthetic: true,
      }) as any;
      sessions.push(session);
    }
    await page.goto('/');
    await expect(page.getByText('Engine connected', { exact: true })).toBeVisible();
    await openWorkspace(page, 'Compare');
    await page.getByLabel(/Reference flight/).selectOption(sessions[0].id);
    await page.getByLabel(/Flight to evaluate/).selectOption(sessions[1].id);
    await page.getByRole('button', { name: 'Compare', exact: true }).last().click();
    await expect(page.getByText('Conditions limit this comparison')).toBeVisible();
    const power = page.getByRole('row').filter({ hasText: /Mean power/ });
    await expect(power).toContainText('100 W');
    await expect(power).toContainText('80 W');
    await expect(page.getByText('Contains synthetic evidence')).toBeVisible();
    await expect(page.getByText('A current/exported snapshot alone does not establish settings in this flight interval.').first()).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath('comparison.png'), fullPage: true });
  } finally { await engine.close(); }
});

test('network inspection runs against this host and does not apply a repair', async ({ page }) => {
  const engine = await connectEngine(page);
  try {
    await page.goto('/');
    await expect(page.getByText('Engine connected', { exact: true })).toBeVisible();
    await openWorkspace(page, 'Network checks');
    await page.getByRole('button', { name: 'Inspect computer' }).click();
    // Windows performs two sequential read-only PowerShell queries, each with
    // a six-second budget. Allow their supported duration plus UI overhead.
    await expect(page.getByText('Firewall visibility', { exact: true })).toBeVisible({ timeout: 20_000 });
    await expect(page.getByText('Unknown', { exact: true }).first()).toBeVisible();
    expect(engine.calls.some(call => call.method === 'network.inspect')).toBeTruthy();
    expect(engine.calls.some(call => call.method === 'network.apply')).toBeFalsy();
  } finally { await engine.close(); }
});
