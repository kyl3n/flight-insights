import { expect, test } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { join, resolve } from 'node:path';
import { connectEngine } from './engine-harness';
import { openWorkspace } from './navigation';

// Actual MAVLink frames in explicitly synthetic log files. These checks cover
// imported chronology and analysis, not a physical aircraft or GCS connection.
function flightLogs(folder: string) {
  const python = resolve(process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
  const oldest = join(folder, 'earlier-flight.tlog');
  const newest = join(folder, 'latest-flight.tlog');
  const base = Math.floor(Date.now() / 1000) * 1_000_000;
  execFileSync(python, ['-c', `
import struct, sys
from pathlib import Path
from pymavlink.dialects.v20 import ardupilotmega as mav
base = int(sys.argv[3])
for path, offset, current, gain in [(sys.argv[1], -3*86400, 1000, .1), (sys.argv[2], -86400, 800, .12)]:
    codec = mav.MAVLink(None, srcSystem=1, srcComponent=1)
    packets = []
    stamp = base + offset * 1_000_000
    def packet(second, message):
        packets.append(struct.pack('>Q', (stamp + int(second * 1_000_000)) & ~3) + message.pack(codec))
        codec.seq = (codec.seq + 1) % 256
    packet(0, mav.MAVLink_autopilot_version_message(131072, 0x040600ff, 0, 0, 9, [0]*8, [0]*8, [0]*8, 1, 2, 12345))
    for second in range(9):
        packet(second+.1, mav.MAVLink_system_time_message(stamp + int((second+.1)*1_000_000), int((100+second+.1)*1000)))
        packet(second+.2, mav.MAVLink_heartbeat_message(2, 3, 128 if 1 <= second < 7 else 0, 0, 4, 3))
        packet(second+.3, mav.MAVLink_sys_status_message(0, 0, 0, 0, 24000, current, 80, 0, 0, 0, 0, 0, 0))
        if second == 0:
            packet(second+.4, mav.MAVLink_param_value_message(b'ATC_RAT_RLL_P', gain, 9, 1, 0))
    Path(path).write_bytes(b''.join(packets))
`, oldest, newest, String(base)], {encoding: 'utf8'});
  return {oldest, newest};
}

test('flight import automatically analyzes saved logs and keeps deep insights on the selected flight', async ({page}, testInfo) => {
  test.setTimeout(90_000);
  const engine = await connectEngine(page);
  try {
    await page.goto('/');
    await expect(page.getByText('Engine connected', {exact: true})).toBeVisible();
    const nav = page.getByRole('navigation', {name: 'Main navigation'});
    await expect(nav.getByRole('button')).toHaveText(['Flights', 'Setup']);
    await openWorkspace(page, 'Overview');
    engine.files.push([resolve('fixtures/logs/synthetic-insights.log')]);
    await page.getByRole('button', {name: 'Import flight logs', exact: true}).click();
    await page.getByRole('checkbox', {name: 'These are synthetic test logs', exact: true}).check();
    await page.getByRole('button', {name: 'Import and analyze', exact: true}).click();
    const history = page.getByRole('region', {name: 'Flight history', exact: true});
    const flight = history.getByRole('button', {name: /synthetic-insights/});
    // Import, indexing and analysis are asynchronous engine jobs. Allow their
    // bounded completion window instead of treating five seconds as an import
    // performance requirement on a shared Windows CI runner.
    await expect(flight).toBeVisible({timeout: 30_000});
    await flight.click();
    const detail = page.getByRole('region', {name: 'Selected flight insights', exact: true});
    await expect(detail.getByRole('heading', {name: 'Performance', exact: true})).toBeVisible();
    await expect(detail.getByRole('heading', {name: 'Suggested next steps', exact: true})).toBeVisible();
    await expect(detail.getByText('Recorded date unavailable', {exact: true})).toBeVisible();
    const suggestions = detail.getByRole('region', {name: 'Flight improvement suggestions', exact: true});
    const explanation = suggestions.getByText('Why this could help and how to verify it', {exact: true}).first();
    await explanation.click();
    await expect(suggestions.getByText('Evidence needed before changing settings', {exact: true}).first()).toBeVisible();
    await expect(suggestions.getByRole('link').first()).toHaveAttribute('href', /^https:\/\/github\.com\/ArduPilot\/.+\/blob\/[a-f0-9]{40}\//);
    await explanation.click();
    await suggestions.getByText('More suggestions (3)', {exact: true}).click();
    await expect(suggestions.getByRole('heading', {name: 'Compare RPM, throttle and FFT notch tracking', exact: true})).toBeVisible();
    await suggestions.getByText('More suggestions (3)', {exact: true}).click();
    const library = await engine.rpc('library.list') as any;
    const selected = await engine.rpc('library.detail', {flight_id: library.flights[0].id}) as any;
    expect(selected.report).not.toBeNull();
    expect(selected.report.performance_advice.recommendations.length).toBeGreaterThan(0);
    expect(selected.comparison).toBeNull();
    expect(selected.flight.started_at).toBeNull();
    expect(engine.calls.filter(call => call.method === 'library.import')).toHaveLength(1);
    expect(engine.calls.filter(call => call.method === 'insights.report')).toHaveLength(0);
    const beforeReload = await engine.rpc('insights.list') as any;
    await page.evaluate(() => { if (document.activeElement instanceof HTMLElement) document.activeElement.blur(); window.scrollTo(0, 0); });
    await page.screenshot({path: testInfo.outputPath('flight-library-overview.png')});
    await page.screenshot({path: testInfo.outputPath('flight-library-desktop.png'), fullPage: true});
    for (const width of [640, 360]) {
      await page.setViewportSize({width, height: 900});
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    }
    await page.evaluate(() => { if (document.activeElement instanceof HTMLElement) document.activeElement.blur(); window.scrollTo(0, 0); });
    await page.screenshot({path: testInfo.outputPath('flight-library-mobile.png'), fullPage: true});
    await page.reload();
    await expect(page.getByText('Engine connected', {exact: true})).toBeVisible();
    await expect(history.getByRole('button', {name: /synthetic-insights/})).toBeVisible();
    const afterReload = await engine.rpc('insights.list') as any;
    expect(afterReload.reports.map((report: any) => report.id)).toEqual(beforeReload.reports.map((report: any) => report.id));
    expect(engine.calls.some(call => /^(?:session\.prepare_and_watch|acquisition\.(?:parameters|logs)\.|network\.apply)/.test(call.method))).toBe(false);
  } finally { await engine.close(); }
});

test('flight history uses recorded chronology and automatically compares earlier flights from the same aircraft', async ({page}, testInfo) => {
  test.setTimeout(90_000);
  const engine = await connectEngine(page);
  try {
    const logs = flightLogs(engine.data);
    await page.goto('/');
    await expect(page.getByText('Engine connected', {exact: true})).toBeVisible();
    // Import newest first to prove display/comparison order does not use import time.
    engine.files.push([logs.newest, logs.oldest]);
    await page.getByRole('button', {name: 'Import flight logs', exact: true}).click();
    await page.getByRole('checkbox', {name: 'These are synthetic test logs', exact: true}).check();
    await page.getByRole('button', {name: 'Import and analyze', exact: true}).click();
    const history = page.getByRole('region', {name: 'Flight history', exact: true});
    await expect(history.getByRole('button', {name: /earlier-flight/})).toBeVisible({timeout: 30_000});
    const rows = history.getByRole('button', {name: /(?:latest|earlier)-flight/});
    await expect(rows).toHaveCount(2);
    await expect(rows.nth(0)).toContainText('latest-flight');
    await expect(rows.nth(1)).toContainText('earlier-flight');
    await rows.nth(0).click();
    const detail = page.getByRole('region', {name: 'Selected flight insights', exact: true});
    await expect(detail.getByRole('heading', {name: 'What changed', exact: true})).toBeVisible();
    await expect(detail).toContainText('earlier-flight');
    const comparedPower = detail.getByRole('table', {name: 'Performance compared with the previous eligible flight', exact: true})
      .getByRole('row').filter({hasText: 'Mean power'});
    await expect(comparedPower).toContainText('240 W');
    await expect(comparedPower).toContainText('192 W');
    await expect(comparedPower).toContainText('-48 W');
    await detail.getByText('Review configuration differences', {exact: true}).click();
    await expect(detail.getByRole('cell', {name: 'ATC_RAT_RLL_P', exact: true})).toBeVisible();
    await detail.getByText('Review configuration differences', {exact: true}).click();
    await expect(detail.getByRole('heading', {name: 'Recent flight history', exact: true})).toBeVisible();
    const library = await engine.rpc('library.list') as any;
    const newest = library.flights.find((flight: any) => flight.label.includes('latest-flight'));
    const earlier = library.flights.find((flight: any) => flight.label.includes('earlier-flight'));
    const selected = await engine.rpc('library.detail', {flight_id: newest.id}) as any;
    expect(selected.previous_flight.id).toBe(earlier.id);
    expect(selected.comparison).not.toBeNull();
    const power = selected.comparison.metrics.find((metric: any) => metric.name === 'mean_power_w');
    expect(power).toMatchObject({before: 240, after: 192, delta: -48});
    expect(selected.flight.aircraft.id).toBe(selected.previous_flight.aircraft.id);
    await page.evaluate(() => { if (document.activeElement instanceof HTMLElement) document.activeElement.blur(); window.scrollTo(0, 0); });
    await page.screenshot({path: testInfo.outputPath('flight-automatic-comparison.png'), fullPage: true});
    await page.getByRole('searchbox', {name: 'Search flights', exact: true}).fill('earlier-flight');
    await expect(history.getByRole('button', {name: /earlier-flight/})).toBeVisible();
    await expect(history.getByRole('button', {name: /latest-flight/})).toHaveCount(0);
    expect(engine.calls.some(call => call.method === 'insights.compare')).toBe(false);
  } finally { await engine.close(); }
});
