import { expect, test, type Page } from '@playwright/test';
import { randomUUID } from 'node:crypto';
import { createSocket } from 'node:dgram';
import type { CaptureSession, CollectionStatus, NetworkInspection, NetworkSource } from '../../src/api';
import { connectEngine } from './engine-harness';

const feedApproval = 'I configured this separate live feed and approved its sender and aircraft source IDs. It is not log replay.';
const probeApproval = 'This is a separate forwarded feed; the receive port is reserved for Flight Lab.';
const activeMethods = new Set([
  'session.prepare_and_watch', 'session.stop', 'radio.capture',
  'acquisition.parameters.capture', 'acquisition.logs.list', 'acquisition.logs.download',
  'orchestration.policy.save', 'orchestration.retrieval.select',
  'network.plan', 'network.apply', 'network.rollback', 'network.bench_opt_in',
]);

function expectNoActiveWork(calls: {method: string}[]) {
  expect(calls.filter(call => activeMethods.has(call.method) || /serial/i.test(call.method))).toEqual([]);
}

function expectNoChecks(calls: {method: string}[]) {
  expect(calls.filter(call => ['network.inspect', 'network.probe'].includes(call.method))).toEqual([]);
  expectNoActiveWork(calls);
}

async function openSetup(page: Page) {
  await page.getByRole('navigation').getByRole('button', {name: 'Connection setup', exact: true}).click();
  await expect(page.getByRole('heading', {name: 'Connection setup', exact: true})).toBeVisible();
}

async function continueSetup(page: Page) {
  await page.getByRole('button', {name: 'Continue', exact: true}).click();
}

async function localFeedReview(page: Page, name: string, port?: number) {
  await page.getByRole('radio', {name: 'Ground station on this computer', exact: true}).check();
  await continueSetup(page);
  await continueSetup(page);
  await page.getByLabel('Aircraft profile name', {exact: true}).filter({visible: true}).fill(name);
  if (port !== undefined) await page.getByLabel('Observer UDP port', {exact: true}).fill(String(port));
  await continueSetup(page);
  await expect(page.getByRole('button', {name: 'Save setup', exact: true})).toBeVisible();
}

// Only host inspection and bounded probe responses are simulated. Startup,
// collection previews/status, and all other requests use the actual engine.
function syntheticInspection(sources: NetworkSource[], probe = false): NetworkInspection {
  return {
    status: 'inspected', platform: 'Synthetic setup test host',
    interfaces: [{name: 'test-loopback', index: 1, up: true, ipv4: [{address: '127.0.0.1', prefix: 8, network: '127.0.0.0/8'}]}],
    routes: [], default_routes: [],
    targets: sources.map(source => ({source_id: source.id, host: source.host, state: 'route_available', selected_interface: 'test-loopback', local_address: '127.0.0.1', issues: []})),
    firewall: {state: 'unknown', reason: 'Synthetic boundary; no native firewall inspection is performed.'},
    limitations: ['Synthetic host inspection is not physical aircraft evidence.'],
    ...(probe ? {probes: sources.map(source => ({
      source_id: source.id, state: 'no_packets',
      checks: [{local_address: '127.0.0.1', port: source.port, packets_from_configured_host: 0, valid_heartbeat_datagrams: 0}],
      details: 'No packets arrived during this bounded test observation.',
      recovery_action: 'Check the ground station forwarding destination and keep its aircraft connection open.',
    }))} : {}),
  };
}

test('guided live settings persist into network and observer workspaces without starting work', async ({page}, testInfo) => {
  const engine = await connectEngine(page);
  try {
    await page.goto('/');
    await expect(page.getByText('Engine connected', {exact: true})).toBeVisible();
    await openSetup(page);
    await expect(page.getByText('First-time setup', {exact: true})).toBeVisible();
    await page.screenshot({path: testInfo.outputPath('connection-setup-start.png'), fullPage: true});
    await page.getByRole('radio', {name: 'Forwarded feed from another device', exact: true}).check();
    await continueSetup(page);
    await page.getByLabel('Check a digiQ / SkyPulse radio', {exact: true}).check();
    await page.getByLabel('digiQ / SkyPulse IPv4', {exact: true}).fill('10.0.0.27');
    await page.getByLabel('Radio HTTP port', {exact: true}).fill('8080');
    await page.getByLabel('Check a camera / RTSP stream', {exact: true}).check();
    await page.getByLabel('Camera IPv4', {exact: true}).fill('10.0.0.41');
    await page.getByLabel('Camera RTSP port', {exact: true}).fill('8554');
    await page.getByLabel('Camera RTSP path', {exact: true}).fill('/live/main');
    await continueSetup(page);
    await page.getByLabel('Aircraft profile name', {exact: true}).filter({visible: true}).fill('Bench aircraft setup');
    await page.getByLabel('Computer receive IPv4', {exact: true}).fill('10.0.0.21');
    await page.getByLabel('Telemetry sender IPv4', {exact: true}).fill('10.0.0.31');
    await page.getByLabel('Observer UDP port', {exact: true}).fill('14651');
    await page.getByLabel('MAVLink system ID', {exact: true}).fill('42');
    await page.getByLabel('Autopilot component ID', {exact: true}).fill('7');
    await continueSetup(page);
    await page.screenshot({path: testInfo.outputPath('connection-setup-review.png'), fullPage: true});
    expectNoChecks(engine.calls);
    await page.getByRole('button', {name: 'Save setup', exact: true}).click();
    await expect(page.getByRole('button', {name: 'Open Prepare & watch', exact: true})).toBeVisible();
    expectNoChecks(engine.calls);

    await page.getByRole('button', {name: 'Open Prepare & watch', exact: true}).click();
    await expect(page.getByLabel('Aircraft profile name', {exact: true})).toHaveValue('Bench aircraft setup');
    await expect(page.getByLabel('Observer local IPv4', {exact: true})).toHaveValue('10.0.0.21');
    await expect(page.getByLabel('Expected sender IPv4', {exact: true})).toHaveValue('10.0.0.31');
    await expect(page.getByLabel('Dedicated observer UDP port', {exact: true})).toHaveValue('14651');
    await expect(page.getByLabel('Approved MAVLink system ID', {exact: true})).toHaveValue('42');
    await expect(page.getByLabel('Approved autopilot component ID', {exact: true})).toHaveValue('7');
    await expect(page.getByLabel(feedApproval, {exact: true})).not.toBeChecked();
    await expect(page.getByRole('button', {name: 'Preview observer setup', exact: true})).toBeDisabled();

    await page.getByRole('navigation').getByRole('button', {name: 'Network checks', exact: true}).click();
    await expect(page.getByRole('button', {name: 'Remove digiQ / SkyPulse 10.0.0.27', exact: true})).toBeVisible();
    await expect(page.getByRole('button', {name: 'Remove UDP telemetry or video 10.0.0.31', exact: true})).toBeVisible();
    await expect(page.getByRole('button', {name: 'Remove Camera / RTSP 10.0.0.41', exact: true})).toBeVisible();
    await expect(page.getByText('No inspection yet', {exact: true})).toBeVisible();
    expectNoChecks(engine.calls);

    await page.reload();
    await expect(page.getByText('Engine connected', {exact: true})).toBeVisible();
    await openSetup(page);
    await expect(page.getByRole('radio', {name: 'Forwarded feed from another device', exact: true})).toBeChecked();
    await continueSetup(page);
    await expect(page.getByLabel('digiQ / SkyPulse IPv4', {exact: true})).toHaveValue('10.0.0.27');
    await expect(page.getByLabel('Radio HTTP port', {exact: true})).toHaveValue('8080');
    await expect(page.getByLabel('Camera IPv4', {exact: true})).toHaveValue('10.0.0.41');
    await expect(page.getByLabel('Camera RTSP port', {exact: true})).toHaveValue('8554');
    await expect(page.getByLabel('Camera RTSP path', {exact: true})).toHaveValue('/live/main');
    await continueSetup(page);
    await expect(page.getByLabel('Computer receive IPv4', {exact: true})).toHaveValue('10.0.0.21');
    await expect(page.getByLabel('Observer UDP port', {exact: true})).toHaveValue('14651');
    for (const width of [640, 360]) {
      await page.setViewportSize({width, height: 900});
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    }
    await page.screenshot({path: testInfo.outputPath('connection-setup-compact.png'), fullPage: true});
    expectNoChecks(engine.calls);
  } finally { await engine.close(); }
});

test('invalid addresses, ports, and aircraft source IDs keep setup on the correction step', async ({page}) => {
  const engine = await connectEngine(page);
  try {
    await page.goto('/');
    await expect(page.getByText('Engine connected', {exact: true})).toBeVisible();
    await openSetup(page);
    await page.getByRole('radio', {name: 'Forwarded feed from another device', exact: true}).check();
    await continueSetup(page);
    await page.getByLabel('Check a digiQ / SkyPulse radio', {exact: true}).check();

    async function rejected(label: string, invalid: string, corrected: string) {
      const field = page.getByLabel(label, {exact: true});
      await field.fill(invalid);
      const next = page.getByRole('button', {name: 'Continue', exact: true});
      if (await next.isEnabled()) await next.click();
      await expect(field).toBeVisible();
      await expect(page.getByRole('button', {name: 'Save setup', exact: true})).toHaveCount(0);
      await field.fill(corrected);
    }

    await rejected('digiQ / SkyPulse IPv4', '10.0.0.999', '10.0.0.2');
    await rejected('Radio HTTP port', '0', '80');
    await rejected('Radio HTTP port', '65536', '80');
    await continueSetup(page);
    await page.getByLabel('Aircraft profile name', {exact: true}).filter({visible: true}).fill('Validated settings');
    await page.getByLabel('Computer receive IPv4', {exact: true}).fill('10.0.0.5');
    await page.getByLabel('Telemetry sender IPv4', {exact: true}).fill('10.0.0.2');
    await rejected('Computer receive IPv4', '0.0.0.0', '10.0.0.5');
    await rejected('Computer receive IPv4', '10.0.0.999', '10.0.0.5');
    await rejected('Telemetry sender IPv4', '0.0.0.0', '10.0.0.2');
    await rejected('Telemetry sender IPv4', '10.0.0.999', '10.0.0.2');
    await rejected('Observer UDP port', '0', '14651');
    await rejected('Observer UDP port', '14550', '14651');
    await rejected('Observer UDP port', '65536', '14651');
    await rejected('MAVLink system ID', '0', '1');
    await rejected('MAVLink system ID', '256', '1');
    await rejected('Autopilot component ID', '0', '1');
    await rejected('Autopilot component ID', '256', '1');
    await continueSetup(page);
    await expect(page.getByRole('button', {name: 'Save setup', exact: true})).toBeEnabled();
    expectNoChecks(engine.calls);
  } finally { await engine.close(); }
});

test('checking a silent forwarded feed reports missing telemetry and saving does not claim a connection', async ({page}) => {
  const engine = await connectEngine(page, {boundaryOverride: (method, params) => {
    if (method === 'network.inspect' || method === 'network.probe')
      return syntheticInspection((params.sources || []) as NetworkSource[], method === 'network.probe');
    return undefined;
  }});
  try {
    await page.goto('/');
    await expect(page.getByText('Engine connected', {exact: true})).toBeVisible();
    await openSetup(page);
    await localFeedReview(page, 'Silent forwarded feed');
    expectNoChecks(engine.calls);
    await expect(page.getByText('Connection not tested', {exact: true})).toBeVisible();
    await expect(page.getByRole('button', {name: 'Check connections', exact: true})).toBeDisabled();
    await page.getByLabel(probeApproval, {exact: true}).check();
    await page.getByRole('button', {name: 'Check connections', exact: true}).click();
    await expect(page.getByText(/No packets arrived during this bounded test observation/)).toBeVisible();
    expect(engine.calls.filter(call => call.method === 'network.probe')).toHaveLength(1);
    const probe = engine.calls.find(call => call.method === 'network.probe');
    expect(probe?.params.sources).toEqual(expect.arrayContaining([
      expect.objectContaining({kind: 'udp', host: '127.0.0.1', local_address: '127.0.0.1', expected_format: 'mavlink'}),
    ]));
    await page.getByRole('button', {name: 'Save setup', exact: true}).click();
    await expect(page.getByRole('button', {name: 'Open Prepare & watch', exact: true})).toBeVisible();
    await expect(page.getByText('No packets', {exact: true})).toBeVisible();
    await expect(page.locator('.connection-setup').getByText('Connected', {exact: true})).toHaveCount(0);
    await expect(page.locator('.connection-setup').getByText('Ready to fly', {exact: true})).toHaveCount(0);
    expectNoActiveWork(engine.calls);
    await page.reload();
    await expect(page.getByText('Engine connected', {exact: true})).toBeVisible();
    expect(engine.calls.filter(call => call.method === 'network.probe')).toHaveLength(1);
    expectNoActiveWork(engine.calls);
  } finally { await engine.close(); }
});

test('unavailable computer inspection remains an actionable error rather than a connection success', async ({page}) => {
  const engine = await connectEngine(page, {boundaryOverride: method => {
    if (method === 'network.inspect') return {__q22TransportError: {code: 'inspection_unavailable', message: 'Synthetic adapter inspection unavailable; check the operating-system network settings.'}};
    return undefined;
  }});
  try {
    await page.goto('/');
    await expect(page.getByText('Engine connected', {exact: true})).toBeVisible();
    await openSetup(page);
    await page.getByRole('radio', {name: 'Ground station on this computer', exact: true}).check();
    await continueSetup(page);
    await continueSetup(page);
    expectNoChecks(engine.calls);
    await page.getByRole('button', {name: 'Find computer addresses', exact: true}).click();
    await expect(page.getByRole('alert')).toContainText('Synthetic adapter inspection unavailable');
    await expect(page.getByRole('heading', {name: 'Addresses found on this computer', exact: true})).toHaveCount(0);
    await page.getByLabel('Aircraft profile name', {exact: true}).filter({visible: true}).fill('Manual local settings');
    await continueSetup(page);
    await expect(page.getByText('Connection not tested', {exact: true})).toBeVisible();
    await page.getByRole('button', {name: 'Save setup', exact: true}).click();
    await expect(page.getByText('Setup saved on this computer', {exact: true})).toBeVisible();
    await expect(page.getByText('Connection not tested', {exact: true})).toBeVisible();
    expect(engine.calls.filter(call => call.method === 'network.inspect')).toHaveLength(1);
    expect(engine.calls.filter(call => call.method === 'network.probe')).toHaveLength(0);
    expectNoActiveWork(engine.calls);
  } finally { await engine.close(); }
});

test('saved-file setup skips live requirements and opens evidence without any connection check', async ({page}) => {
  const engine = await connectEngine(page);
  try {
    await page.goto('/');
    await expect(page.getByText('Engine connected', {exact: true})).toBeVisible();
    await openSetup(page);
    await page.getByRole('radio', {name: 'Analyze saved files only', exact: true}).check();
    await continueSetup(page);
    await expect(page.getByRole('heading', {name: 'Ready to work with saved files', exact: true})).toBeVisible();
    await expect(page.getByText('No radio IP, UDP port or COM port is required.', {exact: false})).toBeVisible();
    await expect(page.getByLabel('Computer receive IPv4', {exact: true})).toHaveCount(0);
    await expect(page.getByRole('button', {name: 'Check connections', exact: true})).toHaveCount(0);
    await page.getByRole('button', {name: 'Save setup', exact: true}).click();
    await expect(page.getByText('Setup saved on this computer', {exact: true})).toBeVisible();
    await page.getByRole('button', {name: 'Open Evidence', exact: true}).click();
    await expect(page.getByRole('navigation').getByRole('button', {name: 'Evidence', exact: true})).toHaveAttribute('aria-current', 'page');
    expectNoChecks(engine.calls);
    expect(engine.calls.filter(call => call.method === 'session.preview')).toHaveLength(0);
    await page.reload();
    await expect(page.getByText('Engine connected', {exact: true})).toBeVisible();
    await openSetup(page);
    await expect(page.getByRole('radio', {name: 'Analyze saved files only', exact: true})).toBeChecked();
    expectNoChecks(engine.calls);
  } finally { await engine.close(); }
});

test('a legacy serial preference moves to ordinary local forwarding without COM configuration', async ({page}) => {
  const engine = await connectEngine(page);
  await page.addInitScript(() => {
    if (!localStorage.getItem('q22.connection-setup.v1')) localStorage.setItem('q22.connection-setup.v1', JSON.stringify({
      draft: {mode: 'serial_gcs', gcs: 'qgroundcontrol', serialPort: 'COM4', baudRate: '115200'},
      savedAt: '2026-10-15T12:00:00Z',
    }));
  });
  try {
    await page.goto('/');
    await expect(page.getByText('Engine connected', {exact: true})).toBeVisible();
    await openSetup(page);
    await expect(page.getByRole('radio', {name: 'USB / COM through a ground station', exact: true})).toHaveCount(0);
    await expect(page.getByRole('radio', {name: 'Ground station on this computer', exact: true})).toBeChecked();
    await continueSetup(page);
    await expect(page.getByRole('combobox', {name: 'Ground station', exact: true})).toHaveValue('qgroundcontrol');
    await expect(page.getByText('QGroundControl forwarding is one-way.', {exact: false})).toBeVisible();
    await expect(page.getByLabel('COM / serial port note (optional)', {exact: true})).toHaveCount(0);
    await expect(page.getByLabel('Baud rate note (optional)', {exact: true})).toHaveCount(0);
    await continueSetup(page);
    await page.getByLabel('Aircraft profile name', {exact: true}).filter({visible: true}).fill('Local forwarded feed');
    await expect(page.getByLabel('Computer receive IPv4', {exact: true})).toHaveValue('127.0.0.1');
    await expect(page.getByLabel('Telemetry sender IPv4', {exact: true})).toHaveValue('127.0.0.1');
    await continueSetup(page);
    await page.getByRole('button', {name: 'Save setup', exact: true}).click();
    await expect(page.getByText('Setup saved on this computer', {exact: true})).toBeVisible();
    const saved = await page.evaluate(() => JSON.parse(localStorage.getItem('q22.connection-setup.v1') || 'null'));
    expect(saved.draft).toMatchObject({mode: 'local_gcs', gcs: 'qgroundcontrol', serialPort: 'COM4', baudRate: '115200'});
    expectNoChecks(engine.calls);
    await page.reload();
    await expect(page.getByText('Engine connected', {exact: true})).toBeVisible();
    await openSetup(page);
    await expect(page.getByRole('radio', {name: 'Ground station on this computer', exact: true})).toBeChecked();
    await continueSetup(page);
    await expect(page.getByRole('combobox', {name: 'Ground station', exact: true})).toHaveValue('qgroundcontrol');
    await expect(page.getByLabel('COM / serial port note (optional)', {exact: true})).toHaveCount(0);
    await expect(page.getByLabel('Baud rate note (optional)', {exact: true})).toHaveCount(0);
    expectNoChecks(engine.calls);
  } finally { await engine.close(); }
});

test('changing saved setup invalidates an already reviewed observer without starting it', async ({page}) => {
  const engine = await connectEngine(page);
  try {
    await page.goto('/');
    await expect(page.getByText('Engine connected', {exact: true})).toBeVisible();
    await openSetup(page);
    await localFeedReview(page, 'Original observer settings');
    await page.getByRole('button', {name: 'Save setup', exact: true}).click();
    await page.getByRole('button', {name: 'Open Prepare & watch', exact: true}).click();
    await page.getByLabel(feedApproval, {exact: true}).check();
    await page.getByRole('button', {name: 'Preview observer setup', exact: true}).click();
    await expect(page.getByRole('heading', {name: 'Reviewed observer setup', exact: true})).toBeVisible();

    await openSetup(page);
    await continueSetup(page);
    await continueSetup(page);
    await page.getByLabel('Observer UDP port', {exact: true}).fill('14652');
    await continueSetup(page);
    await page.getByRole('button', {name: 'Save setup', exact: true}).click();
    await page.getByRole('button', {name: 'Open Prepare & watch', exact: true}).click();
    await expect(page.getByLabel('Dedicated observer UDP port', {exact: true})).toHaveValue('14652');
    await expect(page.getByLabel(feedApproval, {exact: true})).not.toBeChecked();
    await expect(page.getByRole('heading', {name: 'Reviewed observer setup', exact: true})).toHaveCount(0);
    await expect(page.getByRole('button', {name: 'Preview observer setup', exact: true})).toBeDisabled();
    expectNoChecks(engine.calls);
  } finally { await engine.close(); }
});

test('a delayed observer start keeps its reviewed capture and preserves newer guided settings', async ({page}) => {
  // The actual engine opens only a dedicated loopback receiver. Delaying its
  // native response reproduces a slow start while the operator saves setup B.
  // No synthetic packets, aircraft requests, or host repairs are needed.
  let releaseStart!: () => void;
  const heldStart = new Promise<void>(resolve => { releaseStart = resolve; });
  let engine!: Awaited<ReturnType<typeof connectEngine>>;
  let activeCapture: CaptureSession | undefined;
  const socket = createSocket('udp4');
  await new Promise<void>(resolve => socket.bind(0, '127.0.0.1', resolve));
  const port = (socket.address() as {port: number}).port;
  await new Promise<void>(resolve => socket.close(() => resolve()));
  engine = await connectEngine(page, {boundaryOverride: async (method, params) => {
    if (method !== 'session.prepare_and_watch') return undefined;
    const response = await engine.rpc(method, params);
    await heldStart;
    return response;
  }});
  try {
    await page.goto('/');
    await expect(page.getByText('Engine connected', {exact: true})).toBeVisible();
    await openSetup(page);
    await localFeedReview(page, 'Synthetic loopback capture A', port);
    await page.getByRole('button', {name: 'Save setup', exact: true}).click();
    await page.getByRole('button', {name: 'Open Prepare & watch', exact: true}).click();
    await page.getByLabel(feedApproval, {exact: true}).check();
    await page.getByRole('button', {name: 'Preview observer setup', exact: true}).click();
    await page.locator('.observer-preview').getByRole('button', {name: 'Prepare & watch', exact: true}).click();
    await expect.poll(async () => {
      const status = await engine.rpc('session.status') as CollectionStatus;
      activeCapture = status.active_capture_session || status.capture_sessions.find(capture => capture.status === 'observing');
      return activeCapture?.profile.aircraft_id;
    }).toBe('Synthetic loopback capture A');

    await openSetup(page);
    await localFeedReview(page, 'Next capture settings B', port);
    await page.getByRole('button', {name: 'Save setup', exact: true}).click();
    await expect(page.getByText('Setup saved on this computer', {exact: true})).toBeVisible();
    releaseStart();
    await page.getByRole('button', {name: 'Open Prepare & watch', exact: true}).click();
    await expect(page.getByText(/Your newer connection settings were retained for the next capture/)).toBeVisible();
    await expect(page.getByRole('heading', {name: 'App observer is running', exact: true})).toBeVisible();
    await expect(page.getByLabel('Aircraft profile name', {exact: true})).toHaveValue('Next capture settings B');
    const saved = await page.evaluate(() => JSON.parse(localStorage.getItem('q22.collection-profile.v1') || 'null'));
    expect(saved.aircraft_id).toBe('Next capture settings B');
    const after = await engine.rpc('session.status') as CollectionStatus;
    expect(after.active_capture_session?.uuid).toBe(activeCapture?.uuid);
    expect(after.active_capture_session?.profile.aircraft_id).toBe('Synthetic loopback capture A');
    expect(engine.calls.some(call => ['network.probe', 'network.apply', 'acquisition.parameters.capture', 'acquisition.logs.list', 'acquisition.logs.download'].includes(call.method))).toBe(false);
  } finally {
    releaseStart();
    try {
      if (activeCapture) await engine.rpc('session.stop', {
        capture_session_id: activeCapture.uuid,
        operation_id: randomUUID(),
        target_profile_version: activeCapture.profile.aircraft_profile_version,
        applicability_fingerprint: activeCapture.applicability_fingerprint,
      });
    } finally { await engine.close(); }
  }
});

test('successful adapter discovery offers a receiving address without labeling the inspection incomplete', async ({page}) => {
  const engine = await connectEngine(page, {boundaryOverride: method => {
    if (method !== 'network.inspect') return undefined;
    return {
      ...syntheticInspection([]),
      interfaces: [{name: 'test-device-adapter', index: 2, up: true, ipv4: [{address: '10.0.0.5', prefix: 24, network: '10.0.0.0/24'}]}],
    };
  }});
  try {
    await page.goto('/');
    await expect(page.getByText('Engine connected', {exact: true})).toBeVisible();
    await openSetup(page);
    await page.getByRole('radio', {name: 'Forwarded feed from another device', exact: true}).check();
    await continueSetup(page);
    await continueSetup(page);
    await expect(page.getByLabel('Computer receive IPv4', {exact: true})).toHaveValue('');
    expectNoChecks(engine.calls);
    await page.getByRole('button', {name: 'Find computer addresses', exact: true}).click();
    await expect(page.getByRole('heading', {name: 'Addresses found on this computer', exact: true})).toBeVisible();
    await expect(page.getByText(/The list may be incomplete/)).toHaveCount(0);
    await page.getByRole('button', {name: /^10\.0\.0\.5 /}).click();
    await expect(page.getByLabel('Computer receive IPv4', {exact: true})).toHaveValue('10.0.0.5');
    expect(engine.calls.filter(call => call.method === 'network.inspect')).toHaveLength(1);
    expect(engine.calls.filter(call => call.method === 'network.probe')).toHaveLength(0);
    expectNoActiveWork(engine.calls);
  } finally { await engine.close(); }
});

for (const declarationChange of ['updated', 'removed'] as const) {
  test(`guided setup preserves software declarations ${declarationChange} after the wizard opens`, async ({page}) => {
    const engine = await connectEngine(page);
    await page.addInitScript(() => {
      localStorage.setItem('q22.collection-profile.v1', JSON.stringify({
        aircraft_id: 'Previous aircraft profile', aircraft_profile_version: '1', connection_profile_version: '1',
        connection: {local_address: '127.0.0.1', expected_sender: '127.0.0.1', listen_port: 14551,
          system_id: 1, component_id: 1, feed_origin: 'live_forwarded', synthetic: false},
        required_sources: ['app_mavlink_capture', 'onboard_bin', 'preflight_parameters', 'postflight_parameters'],
        environment_context: {gcs_name: 'Mission Planner', gcs_version: 'old-version', applications: [], notes: 'Old declarations'},
      }));
    });
    try {
      await page.goto('/');
      await expect(page.getByText('Engine connected', {exact: true})).toBeVisible();
      await openSetup(page);
      await localFeedReview(page, 'Next connection profile');
      // Reproduce another workspace completing its save after the wizard has
      // loaded its draft. This is the same storage/event boundary used by a
      // delayed successful observer start, without opening a receiver here.
      await page.evaluate(change => {
        const profile = JSON.parse(localStorage.getItem('q22.collection-profile.v1')!);
        if (change === 'removed') delete profile.environment_context;
        else profile.environment_context = {gcs_name: 'QGroundControl', gcs_version: 'new-version', applications: [], notes: 'Newer declarations'};
        localStorage.setItem('q22.collection-profile.v1', JSON.stringify(profile));
        window.dispatchEvent(new Event('q22:connection-settings-changed'));
      }, declarationChange);
      await page.getByRole('button', {name: 'Save setup', exact: true}).click();
      await expect(page.getByText('Setup saved on this computer', {exact: true})).toBeVisible();
      const persisted = await page.evaluate(() => ({
        profile: JSON.parse(localStorage.getItem('q22.collection-profile.v1')!),
        draft: JSON.parse(localStorage.getItem('q22.connection-setup.v1')!).draft,
      }));
      expect(persisted.profile.aircraft_id).toBe('Next connection profile');
      if (declarationChange === 'removed') {
        expect(persisted.profile).not.toHaveProperty('environment_context');
        expect(persisted.draft).not.toHaveProperty('environmentContext');
      } else {
        expect(persisted.profile.environment_context).toEqual({gcs_name: 'QGroundControl', gcs_version: 'new-version', applications: [], notes: 'Newer declarations'});
        expect(persisted.draft.environmentContext).toEqual(persisted.profile.environment_context);
      }
      await page.getByRole('button', {name: 'Open Prepare & watch', exact: true}).click();
      await page.locator('.environment-setup > summary').click();
      await expect(page.getByLabel('Ground station name', {exact: true})).toHaveValue(declarationChange === 'removed' ? '' : 'QGroundControl');
      await expect(page.getByLabel('Ground station version (if known)', {exact: true})).toHaveValue(declarationChange === 'removed' ? '' : 'new-version');
      await expect(page.getByLabel(feedApproval, {exact: true})).not.toBeChecked();
      expectNoChecks(engine.calls);
    } finally { await engine.close(); }
  });
}
