import { expect, test } from '@playwright/test';
import { readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import type { EngineJob, FlightLog, InsightReport } from '../../src/api';
import { connectEngine } from './engine-harness';
import { openWorkspace } from './navigation';

async function completedJob(engine: Awaited<ReturnType<typeof connectEngine>>, method: string, params: Record<string, unknown>) {
  const response = await engine.rpc(method, params) as {job: EngineJob};
  let job = response.job;
  await expect.poll(async () => {
    job = (await engine.rpc('jobs.get', {job_id: job.id}) as {job: EngineJob}).job;
    return job.state;
  }).toBe('succeeded');
  return job.result!;
}

test('reviewed advice and saved-report comparisons explain observed changes without claiming a tuning result', async ({page}, testInfo) => {
  test.setTimeout(90_000);
  let holdComparison = false;
  let delayedComparison = false;
  let releaseComparison: (() => void) | undefined;
  let comparisonGate = Promise.resolve();
  const engine = await connectEngine(page, {boundaryOverride: async (method, params) => {
    if (method !== 'jobs.get' || !holdComparison) return undefined;
    const value = await engine.rpc(method, params) as {job: EngineJob};
    if (value.job.method === 'insights.compare' && value.job.state === 'succeeded') {
      delayedComparison = true;
      await comparisonGate;
    }
    return value;
  }});
  try {
    const original = await readFile(resolve('fixtures/logs/synthetic-insights.log'), 'utf8');
    // These deliberate synthetic variants test reading different observations.
    // The changed current and parameters do not model a causal aircraft effect.
    const earlierLog = original.replace('PARM, 0, INS_GYRO_FILTER, 20, 20', 'PARM, 0, INS_GYRO_FILTER, 20, 20\nPARM, 0, INS_HNTCH_MODE, 1, 1');
    const laterLog = earlierLog
      .replace('PARM, 0, INS_GYRO_FILTER, 20, 20', 'PARM, 0, INS_GYRO_FILTER, 30, 20')
      .replace('PARM, 0, INS_HNTCH_MODE, 1, 1', 'PARM, 0, INS_HNTCH_MODE, 3, 1')
      .replace('PARM, 0, ATC_RAT_RLL_P, 0.1, 0.1', 'PARM, 0, ATC_RAT_RLL_P, 0.2, 0.1')
      .split('\n').map(line => {
        if (!line.startsWith('BAT, ')) return line;
        const fields = line.split(', ');
        fields[4] = String(Number(fields[4]) * 1.2);
        return fields.join(', ');
      }).join('\n');
    const paths = [join(engine.data, 'synthetic-advice-before.log'), join(engine.data, 'synthetic-advice-after.log')];
    await Promise.all(paths.map((path, index) => writeFile(path, index ? laterLog : earlierLog)));
    const reports: InsightReport[] = [];
    for (const [index, path] of paths.entries()) {
      const imported = await completedJob(engine, 'logs.import', {path, label: `Synthetic advice scenario ${index + 1}`, synthetic: true});
      const log = imported.log as FlightLog;
      const result = await completedJob(engine, 'insights.report', {
        log_id: log.id, source_key: 'dataflash', boot_epoch: 0,
        interval_start_s: 1, interval_end_s: 4, max_gap_s: 1.1, battery_id: '0',
        operating_profile: {aircraft_mass_kg: index ? 11 : 10, payload_mass_kg: index ? 5 : 4,
          rotor_count: 4, propeller_diameter_m: 0.5, rotor_layout: 'non_overlapping',
          declared_firmware: 'Copter-4.7.1', flight_phase: 'synthetic hover'},
      });
      reports.push(result.report as InsightReport);
    }
    expect(reports[0].parameter_evidence.state).toBe('observed_only');
    expect(reports[1].parameter_evidence.state).toBe('observed_only');

    await page.goto('/');
    await expect(page.getByText('Engine connected', {exact: true})).toBeVisible();
    await openWorkspace(page, 'Flight insights');
    await page.getByRole('combobox', {name: 'Preserved insight report', exact: true}).selectOption(reports[0].id);
    const report = page.getByRole('region', {name: 'Preserved flight insight report', exact: true});
    await expect(report.getByRole('heading', {name: 'Performance improvement review', exact: true})).toBeVisible();
    const notch = report.getByRole('article').filter({has: page.getByRole('heading', {name: 'Compare RPM, throttle and FFT notch tracking', exact: true})});
    await expect(notch).toContainText('INS_HNTCH_MODE = 1');
    await expect(notch).toContainText('Bidirectional DShot is one way to obtain RPM, not a filter itself.');
    await notch.getByText('Why, prerequisites, and a validation test', {exact: true}).click();
    await expect(notch).toContainText('Compatible ESC model and installed ESC firmware');
    await expect(notch).toContainText('Correct motor pole count and electrical-to-mechanical RPM conversion');
    await expect(notch).toContainText('DMA/output support');
    await expect(notch).toContainText('update rate, delay, dropouts');
    await expect(notch).toContainText('More per-motor/harmonic notches add processor cost and phase delay');
    await expect(notch.getByRole('heading', {name: 'Validation experiment', exact: true})).toBeVisible();
    const sourceLinks = await notch.locator('.advice-sources a').all();
    expect(sourceLinks.length).toBeGreaterThan(0);
    for (const link of sourceLinks) expect(await link.getAttribute('href')).toMatch(/^https:\/\/github\.com\/ArduPilot\/(?:ardupilot|ardupilot_wiki)\/blob\/[0-9a-f]{40}\//);
    const mechanical = report.getByRole('article').filter({has: page.getByRole('heading', {name: 'Investigate vibration and clipping at the source', exact: true})});
    await expect(mechanical).toContainText('Clipped acceleration loses information that a software notch cannot recover.');
    await expect(mechanical).toContainText('Inspect propellers, motors, frame joints');

    const comparison = page.getByRole('region', {name: 'Compare preserved insight reports', exact: true});
    await comparison.getByRole('combobox', {name: 'Earlier insight report', exact: true}).selectOption(reports[0].id);
    await comparison.getByRole('combobox', {name: 'Later insight report', exact: true}).selectOption(reports[0].id);
    await expect(comparison.getByRole('button', {name: 'Compare insight reports', exact: true})).toBeDisabled();
    expect(engine.calls.filter(call => call.method === 'insights.compare')).toHaveLength(0);
    await comparison.getByRole('combobox', {name: 'Later insight report', exact: true}).selectOption(reports[1].id);
    await comparison.getByRole('button', {name: 'Compare insight reports', exact: true}).click();
    const differences = comparison.getByRole('region', {name: 'Insight report comparison result', exact: true});
    await expect(differences.getByRole('heading', {name: 'Observed differences', exact: true})).toBeVisible();
    await expect(differences.getByText('Confounded · association only', {exact: true})).toBeVisible();
    await expect(differences).toContainText('Declared payload_mass_kg differs between the reports.');
    await expect(differences).toContainText('Multiple observed parameters differ; the effect of an individual setting cannot be isolated.');
    await expect(differences).toContainText('the current analysis computer is never substituted');
    const power = differences.getByRole('table', {name: 'Performance differences between preserved reports'}).getByRole('row').filter({hasText: 'Mean power'});
    await expect(power).toContainText('386.6667 W');
    await expect(power).toContainText('464 W');
    await expect(power).toContainText('77.3333 W');
    await expect(power).toContainText('20 %');
    const mechanism = differences.getByRole('article').filter({has: page.getByRole('heading', {name: /^INS_HNTCH_MODE: 1(?:\.0)? → 3(?:\.0)?$/})});
    await expect(mechanism).toContainText('Selects the source tracking the notch center frequency.');
    await expect(mechanism).toContainText('Conditional explanation');
    await expect(mechanism).toContainText('not the direction of performance');
    await mechanism.getByText('Evidence and verification for INS_HNTCH_MODE', {exact: true}).click();
    await expect(mechanism).toContainText('High-rate known-stage gyro data with sample-rate/anti-alias context');
    await expect(mechanism.getByRole('link').first()).toHaveAttribute('href', /github\.com\/ArduPilot\/.*\/blob\/[0-9a-f]{40}\//);
    await expect(differences.getByRole('article').filter({has: page.getByRole('heading', {name: /^INS_GYRO_FILTER: 20(?:\.0)? → 30(?:\.0)?$/})})).toContainText('phase delay');
    await expect(differences.getByRole('article').filter({has: page.getByRole('heading', {name: 'ATC_RAT_RLL_P: 0.1 → 0.2', exact: true})})).toContainText('Proportional gain weights current rate error.');
    expect(engine.calls.filter(call => call.method === 'insights.compare').map(call => call.params)).toEqual([{before_report_id: reports[0].id, after_report_id: reports[1].id}]);
    expect(engine.calls.some(call => ['environment.inspect', 'session.prepare_and_watch', 'network.probe', 'network.apply', 'acquisition.parameters.capture', 'acquisition.logs.list', 'acquisition.logs.download'].includes(call.method))).toBe(false);
    await comparison.screenshot({path: testInfo.outputPath('performance-comparison.png')});
    // Changing report selection invalidates the old comparison immediately.
    await comparison.getByRole('combobox', {name: 'Earlier insight report', exact: true}).selectOption(reports[1].id);
    await expect(differences).toHaveCount(0);
    await expect(comparison.getByRole('button', {name: 'Compare insight reports', exact: true})).toBeDisabled();
    for (const side of ['Earlier', 'Later']) {
      await test.step(`changing the ${side.toLowerCase()} report rejects a delayed background result`, async () => {
        await comparison.getByRole('combobox', {name: 'Earlier insight report', exact: true}).selectOption(reports[0].id);
        await comparison.getByRole('combobox', {name: 'Later insight report', exact: true}).selectOption(reports[1].id);
        delayedComparison = false;
        comparisonGate = new Promise<void>(resolveGate => { releaseComparison = resolveGate; });
        holdComparison = true;
        await comparison.getByRole('button', {name: 'Compare insight reports', exact: true}).click();
        await expect.poll(() => delayedComparison).toBe(true);
        await comparison.getByRole('combobox', {name: `${side} insight report`, exact: true}).selectOption(reports[side === 'Earlier' ? 1 : 0].id);
        await expect(differences).toHaveCount(0);
        holdComparison = false;
        releaseComparison!();
        await expect(comparison.getByText('Background operations · 0 active', {exact: true})).toBeVisible();
        await expect(differences).toHaveCount(0);
        await expect(comparison.getByRole('button', {name: 'Compare insight reports', exact: true})).toBeDisabled();
      });
    }
    expect(engine.calls.filter(call => call.method === 'insights.compare')).toHaveLength(3);
    const preserved = await engine.rpc('insights.detail', {report_id: reports[0].id}) as {report: InsightReport};
    expect(preserved.report).toEqual(reports[0]);
  } finally { releaseComparison?.(); await engine.close(); }
});
