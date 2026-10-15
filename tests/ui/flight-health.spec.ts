import { expect, test } from '@playwright/test';
import { readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import type { EngineJob, FlightLog, InsightReport } from '../../src/api';
import { connectEngine } from './engine-harness';
import { openWorkspace } from './navigation';

test('health review displays schema-qualified observations, exact source records, and unavailable unqualified measurements', async ({page}, testInfo) => {
  const engine = await connectEngine(page);
  async function completed(method: string, params: Record<string, unknown>) {
    let {job} = await engine.rpc(method, params) as {job: EngineJob};
    await expect.poll(async () => {
      job = (await engine.rpc('jobs.get', {job_id: job.id}) as {job: EngineJob}).job;
      return job.state;
    }).toBe('succeeded');
    return job.result!;
  }
  try {
    // Same explicitly synthetic wire schema as the backend decoder oracle.
    // PM.MaxT deliberately lacks a physical unit and must remain unavailable.
    const base = await readFile(resolve('fixtures/logs/synthetic-flight.log'), 'utf8');
    const header = base.split('\n').filter(line => /^(FMT|FMTU|UNIT|MULT),/.test(line));
    const records = [
      'FMT, 150, 16, GPS, QBBBc, TimeUS,I,Status,NSats,HDop',
      'FMT, 151, 18, GPA, QBCCC, TimeUS,I,HAcc,VAcc,SAcc',
      'FMT, 152, 16, XKF4, QBcH, TimeUS,C,SV,FS',
      'FMT, 153, 22, ESC, QBffc, TimeUS,Instance,RPM,Curr,Temp',
      'FMT, 154, 23, PM, QHHHHI, TimeUS,NLon,NL,Load,LR,MaxT',
      'FMT, 155, 21, POWR, QffH, TimeUS,Vcc,VServo,Flags',
      'FMT, 156, 27, MCU, Qffff, TimeUS,MTemp,MVolt,MVmin,MVmax',
      'UNIT, 0, 35, instance', 'UNIT, 0, 83, satellites', 'UNIT, 0, 113, rpm',
      'UNIT, 0, 37, %', 'UNIT, 0, 122, Hz', 'UNIT, 0, 79, degC',
      'UNIT, 0, 109, m', 'UNIT, 0, 110, m/s', 'UNIT, 0, 45, ',
      'MULT, 0, 65, 0.1', 'MULT, 0, 66, 0.01', 'MULT, 0, 45, 0',
      'FMTU, 0, 150, s#-S-, F--0B', 'FMTU, 0, 151, s#mmn, F-BBB',
      'FMTU, 0, 152, s#--, F---', 'FMTU, 0, 153, s#qAO, F-00B',
      'FMTU, 0, 154, s--%z-, F--A0-', 'FMTU, 0, 155, svv-, F00-',
      'FMTU, 0, 156, sOvvv, F0000',
      'GPS, 1000000, 0, 2, 8, 1.25', 'GPA, 1000000, 0, 0.5, 0.7, 0.2',
      'XKF4, 1000000, 0, 1.2, 2', 'ESC, 1000000, 0, 1000, 4, 25',
      'ESC, 1000000, 1, 1200, 6, 30', 'PM, 1000000, 2, 100, 250, 400, 5000',
      'POWR, 1000000, 5.2, 0, 3', 'MCU, 1000000, 30, 3.3, 3.2, 3.4',
      'GPS, 2000000, 0, 3, 14, 0.75', 'POWR, 2000000, 4.8, 0, 3',
    ];
    const path = join(engine.data, 'synthetic-health.log');
    await writeFile(path, [...header, ...records].join('\n') + '\n');
    const log = (await completed('logs.import', {path, label: 'Synthetic health observations', synthetic: true})).log as FlightLog;
    const report = (await completed('insights.report', {log_id: log.id, source_key: 'dataflash', boot_epoch: 0, interval_start_s: 0, interval_end_s: 2})).report as InsightReport;
    await page.goto('/');
    await expect(page.getByText('Engine connected', {exact: true})).toBeVisible();
    await openWorkspace(page, 'Flight insights');
    await page.getByRole('combobox', {name: 'Preserved insight report', exact: true}).selectOption(report.id);
    const health = page.locator('.flight-health');
    await expect(health.getByRole('heading', {name: 'Navigation, ESC, and system observations', exact: true})).toBeVisible();
    const gps = health.getByRole('table', {name: 'GNSS observations, GPS instance 0', exact: true});
    const hdop = gps.getByRole('row').filter({hasText: 'Horizontal dilution of precision'});
    await expect(hdop).toContainText('0.75–1.25');
    await expect(hdop).toContainText('2 valid samples');
    await hdop.getByText('Range source records', {exact: true}).click();
    await expect(hdop).toContainText('GPS · record');
    await expect(health.getByRole('table', {name: 'Scheduler observations, PM instance board', exact: true}).getByRole('row').filter({hasText: 'Reported processor load'})).toContainText('25 %');
    const unknownDuration = health.getByRole('table', {name: 'Scheduler observations, PM instance board', exact: true}).getByRole('row').filter({hasText: 'Maximum loop'});
    await expect(unknownDuration).toContainText('Unavailable');
    await expect(unknownDuration).toContainText('0 valid samples');
    await expect(health.getByRole('table', {name: 'Board power and temperature, POWR instance board', exact: true}).getByRole('row').filter({hasText: 'Board rail voltage'})).toContainText('4.8–5.2');
    await expect(health).toContainText('does not prove a brownout');
    await expect(health).toContainText('expected motor set');
    for (const link of await health.locator('.recommendation a').all()) {
      await expect(link).toHaveAttribute('href', /^https:\/\/github\.com\/ArduPilot\/ardupilot\/blob\/[a-f0-9]{40}\//);
    }
    await health.screenshot({path: testInfo.outputPath('flight-health-observations.png')});
    expect(engine.calls.some(call => /^(?:session\.prepare_and_watch|acquisition\.(?:parameters|logs)\.|network\.apply)/.test(call.method))).toBe(false);
  } finally { await engine.close(); }
});
