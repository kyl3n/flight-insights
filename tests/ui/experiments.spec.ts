import { expect, test } from '@playwright/test';
import { resolve } from 'node:path';
import type { EngineJob, FlightLog, InsightReport } from '../../src/api';
import { connectEngine } from './engine-harness';
import { openWorkspace } from './navigation';

async function completedJob(engine: Awaited<ReturnType<typeof connectEngine>>, method: string, params: Record<string, unknown>) {
  let {job} = await engine.rpc(method, params) as {job: EngineJob};
  await expect.poll(async () => {
    job = (await engine.rpc('jobs.get', {job_id: job.id}) as {job: EngineJob}).job;
    return job.state;
  }).toBe('succeeded');
  return job.result!;
}

test('saved experiment revisions evaluate real reports while unverified intervals remain insufficient repeat evidence', async ({page}, testInfo) => {
  test.setTimeout(90_000);
  const engine = await connectEngine(page);
  try {
    const imported = await completedJob(engine, 'logs.import', {path: resolve('fixtures/logs/synthetic-insights.log'), label: 'Synthetic experiment source', synthetic: true});
    const log = imported.log as FlightLog;
    const reports: InsightReport[] = [];
    for (const end of [3, 4]) {
      const result = await completedJob(engine, 'insights.report', {log_id: log.id, source_key: 'dataflash', boot_epoch: 0,
        interval_start_s: 1, interval_end_s: end, max_gap_s: 1.1, battery_id: '0', operating_profile: {flight_phase: 'hover', declared_firmware: 'Copter-4.7.1'}});
      reports.push(result.report as InsightReport);
    }
    await page.goto('/');
    await expect(page.getByText('Engine connected', {exact: true})).toBeVisible();
    await openWorkspace(page, 'Flight experiments');
    const experiments = page.locator('.experiment-workspace');
    await experiments.getByRole('textbox', {name: 'Experiment name', exact: true}).fill('Matched synthetic hover review');
    await experiments.getByRole('textbox', {name: 'Hypothesis and controlled conditions', exact: true}).fill('Compare power at the same declared hover phase; verify distinct flights and conditions before interpreting a change.');
    await experiments.getByRole('button', {name: 'Add planned parameter change', exact: true}).click();
    await experiments.getByRole('textbox', {name: 'Parameter 1', exact: true}).fill('INS_GYRO_FILTER');
    await experiments.getByRole('textbox', {name: 'Reference value 1', exact: true}).fill('20');
    await experiments.getByRole('textbox', {name: 'Candidate value 1', exact: true}).fill('20');
    await experiments.getByRole('combobox', {name: 'Goal metric 1', exact: true}).selectOption('mean_power_w');
    await expect(experiments.getByRole('textbox', {name: 'Goal unit 1', exact: true})).toHaveValue('W');
    await experiments.getByRole('spinbutton', {name: 'Minimum improvement 1', exact: true}).fill('10');
    await experiments.getByRole('button', {name: 'Add regression check', exact: true}).click();
    await experiments.getByRole('combobox', {name: 'Regression metric 1', exact: true}).selectOption('spectrum.retained_band_power');
    await expect(experiments.getByRole('textbox', {name: 'Regression unit 1', exact: true})).toHaveValue('(rad/s)²');
    await experiments.getByRole('combobox', {name: 'Regression metric 1', exact: true}).selectOption('roll_tracking_rmse_deg');
    await experiments.getByRole('spinbutton', {name: 'Maximum worsening 1', exact: true}).fill('0.5');
    const references = experiments.locator('section').filter({has: page.getByRole('heading', {name: /^Reference flights/})}).last();
    const candidates = experiments.locator('section').filter({has: page.getByRole('heading', {name: /^Candidate flights/})}).last();
    await references.getByRole('checkbox', {name: new RegExp(reports[0].id.slice(0, 8))}).check();
    await expect(candidates.getByRole('checkbox', {name: new RegExp(reports[0].id.slice(0, 8))})).toBeDisabled();
    await candidates.getByRole('checkbox', {name: new RegExp(reports[1].id.slice(0, 8))}).check();
    await experiments.getByRole('button', {name: 'Save experiment plan', exact: true}).click();
    await expect(experiments.getByRole('status')).toHaveText('Saved Matched synthetic hover review · revision 1.');
    const {experiments: plans} = await engine.rpc('experiments.list') as {experiments: {id: string}[]};
    const original = await engine.rpc('experiments.detail', {experiment_id: plans[0].id}) as any;
    expect(original.experiment).toMatchObject({revision: 1, minimum_coverage: 0.8, minimum_flights_per_group: 2,
      reference_report_ids: [reports[0].id], candidate_report_ids: [reports[1].id],
      goals: [{metric: 'mean_power_w', unit: 'W', direction: 'lower', minimum_change: 10}],
      regression_limits: [{metric: 'roll_tracking_rmse_deg', unit: 'deg', direction: 'lower', maximum_regression: 0.5}]});
    expect(engine.calls.some(call => call.method === 'experiments.evaluate')).toBe(false);

    await experiments.getByRole('button', {name: 'Evaluate saved experiment', exact: true}).click();
    const evaluation = experiments.getByRole('region', {name: 'Saved experiment evaluation', exact: true});
    await expect(evaluation.getByRole('heading', {name: 'Repeated-flight results', exact: true})).toBeVisible();
    await expect(evaluation.getByText('Insufficient evidence', {exact: true}).first()).toBeVisible();
    await expect(evaluation.getByText('0 distinct verified flights', {exact: true})).toHaveCount(2);
    await expect(evaluation).toContainText('statistical independence unestablished');
    await expect(evaluation.getByRole('table', {name: 'Repeated flight metric summaries'})).toContainText('Mean electrical power');
    const evaluated = await engine.rpc('experiments.detail', {experiment_id: plans[0].id}) as any;
    expect(evaluated.evaluations).toHaveLength(1);
    expect(evaluated.evaluations[0].status).toBe('insufficient_evidence');
    expect(evaluated.evaluations[0].decision_blockers).toContain('flight_identity_unknown');
    expect(evaluated.evaluations[0].causal_attribution).toBe(false);
    await evaluation.screenshot({path: testInfo.outputPath('experiment-evidence-limits.png')});

    await experiments.getByRole('button', {name: 'Create revision from this plan', exact: true}).click();
    await experiments.getByRole('textbox', {name: 'Experiment name', exact: true}).fill('Matched synthetic hover revision');
    await experiments.getByRole('spinbutton', {name: 'Minimum improvement 1', exact: true}).fill('15');
    await experiments.getByRole('button', {name: 'Save experiment revision', exact: true}).click();
    await expect(experiments.getByRole('status')).toHaveText('Saved Matched synthetic hover revision · revision 2.');
    await expect(evaluation).toHaveCount(0);
    const preserved = await engine.rpc('experiments.detail', {experiment_id: plans[0].id}) as any;
    expect(preserved.experiment).toEqual(original.experiment);
    expect(preserved.evaluations).toEqual(evaluated.evaluations);
    await experiments.getByRole('combobox', {name: 'Saved experiment', exact: true}).selectOption(plans[0].id);
    await expect(evaluation.getByRole('heading', {name: 'Repeated-flight results', exact: true})).toBeVisible();
    expect(engine.calls.filter(call => call.method === 'experiments.evaluate')).toHaveLength(1);
    expect(engine.calls.some(call => /^(?:session\.prepare_and_watch|acquisition\.(?:parameters|logs)\.|network\.apply)/.test(call.method))).toBe(false);
  } finally { await engine.close(); }
});

test('a completed background evaluation cannot replace a newly opened experiment draft', async ({page}) => {
  let evaluatedJob = '';
  let delayed = false;
  let release: (() => void) | undefined;
  const gate = new Promise<void>(resolveGate => { release = resolveGate; });
  const engine = await connectEngine(page, {boundaryOverride: async (method, params) => {
    if (method === 'experiments.evaluate') {
      const value = await engine.rpc(method, params) as {job: EngineJob};
      evaluatedJob = value.job.id;
      return value;
    }
    if (method === 'jobs.get' && params.job_id === evaluatedJob) {
      const value = await engine.rpc(method, params) as {job: EngineJob};
      if (value.job.state === 'succeeded') { delayed = true; await gate; }
      return value;
    }
    return undefined;
  }});
  try {
    const {experiment} = await engine.rpc('experiments.create', {name: 'Plan awaiting real flights', hypothesis: 'A saved plan may precede flight collection.',
      flight_phase: 'hover', minimum_coverage: 0.8, minimum_flights_per_group: 2, reference_report_ids: [], candidate_report_ids: [],
      parameter_hypothesis: [], goals: [{metric: 'mean_power_w', unit: 'W', direction: 'lower', minimum_change: 10}], regression_limits: [], pairs: []}) as any;
    await page.goto('/');
    await expect(page.getByText('Engine connected', {exact: true})).toBeVisible();
    await openWorkspace(page, 'Flight experiments');
    const experiments = page.locator('.experiment-workspace');
    await experiments.getByRole('combobox', {name: 'Saved experiment', exact: true}).selectOption(experiment.id);
    await experiments.getByRole('button', {name: 'Evaluate saved experiment', exact: true}).click();
    await expect.poll(() => delayed).toBe(true);
    await experiments.getByRole('button', {name: 'New experiment', exact: true}).click();
    await experiments.getByRole('textbox', {name: 'Experiment name', exact: true}).fill('New draft must stay selected');
    release!();
    await expect(experiments.getByText('Background operations · 0 active', {exact: true})).toBeVisible();
    await expect(experiments.getByRole('heading', {name: 'Plan an improvement test', exact: true})).toBeVisible();
    await expect(experiments.getByRole('textbox', {name: 'Experiment name', exact: true})).toHaveValue('New draft must stay selected');
    await expect(experiments.getByRole('region', {name: 'Saved experiment evaluation', exact: true})).toHaveCount(0);
    const preserved = await engine.rpc('experiments.detail', {experiment_id: experiment.id}) as any;
    expect(preserved.evaluations).toHaveLength(1);
    expect(preserved.evaluations[0].status).toBe('insufficient_evidence');
    await experiments.getByRole('combobox', {name: 'Saved experiment', exact: true}).selectOption(experiment.id);
    await expect(experiments.getByRole('region', {name: 'Saved experiment evaluation', exact: true})).toBeVisible();
  } finally { release?.(); await engine.close(); }
});
