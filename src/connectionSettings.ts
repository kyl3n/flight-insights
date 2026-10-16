import type { CollectionProfile, NetworkSource } from './api';

export type SetupMode = 'local_gcs' | 'network' | 'serial_gcs' | 'offline';
export interface SetupDraft {
  mode: SetupMode;
  aircraft: string;
  localAddress: string;
  senderAddress: string;
  observerPort: string;
  readbackEnabled: boolean;
  remotePort: string;
  appSystemId: string;
  appComponentId: string;
  systemId: string;
  componentId: string;
  uid: string;
  aircraftVersion: string;
  connectionVersion: string;
  synthetic: boolean;
  radioEnabled: boolean;
  radioHost: string;
  radioPort: string;
  cameraEnabled: boolean;
  cameraHost: string;
  cameraPort: string;
  cameraPath: string;
  gcs: 'mission_planner' | 'qgroundcontrol' | 'other';
  serialPort: string;
  baudRate: string;
  telemetrySourceId?: string;
  radioSourceId?: string;
  cameraSourceId?: string;
  environmentContext?: CollectionProfile['environment_context'];
}

export const SETTINGS_CHANGED = 'q22:connection-settings-changed';
const DRAFT_KEY = 'q22.connection-setup.v1';
const PROFILE_KEY = 'q22.collection-profile.v1';
const SOURCES_KEY = 'q22.network-sources.v1';
const requiredSources = ['app_mavlink_capture', 'onboard_bin', 'preflight_parameters', 'postflight_parameters'];
const defaultAppSystemId = 254;
const defaultAppComponentId = 191;

function readJson(key: string): unknown {
  try { return JSON.parse(localStorage.getItem(key) || 'null'); } catch { return null; }
}

function object(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function storedSources(): NetworkSource[] {
  const value = readJson(SOURCES_KEY);
  return Array.isArray(value) ? value.filter((item): item is NetworkSource => {
    const source = object(item);
    return Boolean(source && typeof source.id === 'string' && typeof source.host === 'string' && ['skypulse', 'rtsp', 'udp'].includes(String(source.kind)));
  }) : [];
}

function unusedSourceId(sources: NetworkSource[], base: string): string {
  let id = base;
  for (let suffix = 2; sources.some(source => source.id === id); suffix++) id = `${base}-${suffix}`;
  return id;
}

export function loadSetupDraft(): SetupDraft {
  const draft: SetupDraft = {
    mode: 'network', aircraft: '', localAddress: '', senderAddress: '',
    observerPort: '14551', systemId: '1', componentId: '1', uid: '', aircraftVersion: '1', connectionVersion: '1',
    readbackEnabled: true, remotePort: '', appSystemId: String(defaultAppSystemId), appComponentId: String(defaultAppComponentId),
    synthetic: false, radioEnabled: false, radioHost: '', radioPort: '80',
    cameraEnabled: false, cameraHost: '', cameraPort: '554', cameraPath: '/',
    gcs: 'other', serialPort: '', baudRate: '',
  };
  const saved = object(object(readJson(DRAFT_KEY))?.draft);
  if (saved) {
    for (const key of Object.keys(draft) as (keyof SetupDraft)[]) {
      if (typeof saved[key] === typeof draft[key]) Object.assign(draft, { [key]: saved[key] });
    }
    if (!['local_gcs', 'network', 'serial_gcs', 'offline'].includes(draft.mode)) draft.mode = 'local_gcs';
    if (draft.mode === 'serial_gcs') draft.mode = 'local_gcs';
    if (draft.mode === 'local_gcs') {
      if (typeof saved.localAddress !== 'string') draft.localAddress = '127.0.0.1';
      if (typeof saved.senderAddress !== 'string') draft.senderAddress = '127.0.0.1';
    }
    // Previous releases configured a receive-only observer. Never silently
    // enable requests when migrating an existing connection.
    if (typeof saved.readbackEnabled !== 'boolean') draft.readbackEnabled = false;
    if (!['mission_planner', 'qgroundcontrol', 'other'].includes(draft.gcs)) draft.gcs = 'mission_planner';
  }

  // The live tools can save newer settings than the last guided setup. Their
  // current configuration is authoritative; the draft retains guidance notes.
  const profile = object(readJson(PROFILE_KEY));
  if (object(profile?.environment_context)) draft.environmentContext = profile!.environment_context as CollectionProfile['environment_context'];
  const connection = object(profile?.connection);
  if (profile && connection) {
    const fields: [keyof SetupDraft, unknown][] = [
      ['aircraft', profile.aircraft_id], ['aircraftVersion', profile.aircraft_profile_version],
      ['connectionVersion', profile.connection_profile_version], ['localAddress', connection.local_address],
      ['senderAddress', connection.expected_sender], ['observerPort', connection.listen_port],
      ['systemId', connection.system_id], ['componentId', connection.component_id], ['uid', connection.expected_uid ?? ''],
    ];
    for (const [key, value] of fields) if (typeof value === 'string' || typeof value === 'number') Object.assign(draft, { [key]: String(value) });
    if (typeof connection.synthetic === 'boolean') draft.synthetic = connection.synthetic;
    const readback = object(connection.readback);
    draft.readbackEnabled = Boolean(readback);
    if (typeof readback?.remote_port === 'number') draft.remotePort = String(readback.remote_port);
    draft.appSystemId = typeof readback?.client_system_id === 'number' ? String(readback.client_system_id) : String(defaultAppSystemId);
    draft.appComponentId = typeof readback?.client_component_id === 'number' ? String(readback.client_component_id) : String(defaultAppComponentId);
    if (!saved) draft.mode = draft.localAddress.startsWith('127.') && draft.senderAddress.startsWith('127.') ? 'local_gcs' : 'network';
  }

  const sources = storedSources();
  function sourceFor(key: 'telemetrySourceId' | 'radioSourceId' | 'cameraSourceId', kind: NetworkSource['kind']) {
    const selected = typeof saved?.[key] === 'string' ? saved[key] : undefined;
    if (selected) return sources.find(source => source.id === selected && source.kind === kind);
    if (kind !== 'udp') return sources.find(source => source.kind === kind);
    return sources.find(source => source.kind === 'udp' && source.expected_format === 'mavlink' && (!connection || (
      source.host === draft.senderAddress && source.local_address === draft.localAddress && source.port === Number(draft.observerPort)
    )));
  }
  const telemetry = sourceFor('telemetrySourceId', 'udp');
  const radio = sourceFor('radioSourceId', 'skypulse');
  const camera = sourceFor('cameraSourceId', 'rtsp');
  draft.telemetrySourceId = telemetry?.id || (typeof saved?.telemetrySourceId === 'string' ? saved.telemetrySourceId : unusedSourceId(sources, 'guided-observer'));
  draft.radioSourceId = radio?.id || (typeof saved?.radioSourceId === 'string' ? saved.radioSourceId : unusedSourceId(sources, 'guided-radio'));
  draft.cameraSourceId = camera?.id || (typeof saved?.cameraSourceId === 'string' ? saved.cameraSourceId : unusedSourceId(sources, 'guided-camera'));
  if (telemetry && !connection) {
    draft.senderAddress = telemetry.host;
    if (telemetry.local_address) draft.localAddress = telemetry.local_address;
    if (telemetry.port !== undefined) draft.observerPort = String(telemetry.port);
    if (!saved) draft.mode = draft.localAddress.startsWith('127.') && draft.senderAddress.startsWith('127.') ? 'local_gcs' : 'network';
  }
  draft.radioEnabled = Boolean(radio);
  if (radio) { draft.radioHost = radio.host; draft.radioPort = String(radio.port ?? 80); }
  draft.cameraEnabled = Boolean(camera);
  if (camera) { draft.cameraHost = camera.host; draft.cameraPort = String(camera.port ?? 554); draft.cameraPath = camera.path ?? '/'; }
  return draft;
}

function validAddress(value: string): boolean {
  const address = value.trim();
  if (!/^(?:0|[1-9]\d{0,2})(?:\.(?:0|[1-9]\d{0,2})){3}$/.test(address)) return false;
  const octets = address.split('.').map(Number);
  return octets.every(octet => octet <= 255) && !(octets[0] >= 224 && octets[0] <= 239) && address !== '0.0.0.0' && address !== '255.255.255.255';
}

function integerIn(value: string, minimum: number, maximum: number): boolean {
  return /^\d+$/.test(value.trim()) && Number(value) >= minimum && Number(value) <= maximum;
}

export function validateSetup(draft: SetupDraft, scope: 'devices' | 'telemetry' | 'all' = 'all'): string[] {
  if (draft.mode === 'offline') return [];
  const errors: string[] = [];
  if (scope !== 'devices') {
    for (const [label, value] of [['Aircraft profile name', draft.aircraft], ['Aircraft profile version', draft.aircraftVersion], ['Connection profile version', draft.connectionVersion]]) {
      if (!value.trim() || [...value.trim()].length > 160) errors.push(`${label} must contain 1–160 characters.`);
    }
    if (!validAddress(draft.localAddress)) errors.push('Computer receive address must be a single numeric IPv4 address, such as 127.0.0.1 or your connected adapter address.');
    if (!validAddress(draft.senderAddress)) errors.push('Expected sender address must be a single numeric IPv4 address. Use 127.0.0.1 when the forwarding GCS runs on this computer.');
    if (draft.mode === 'network' && (
      (validAddress(draft.localAddress) && draft.localAddress.trim().startsWith('127.')) ||
      (validAddress(draft.senderAddress) && draft.senderAddress.trim().startsWith('127.'))
    )) errors.push('A feed from another device needs this computer’s network adapter address and the remote sender’s address. 127.x.x.x only reaches the same computer; choose Ground station on this computer for local forwarding.');
    if (!integerIn(draft.observerPort, 1024, 65535)) errors.push('App UDP port must be a whole number from 1024 to 65535.');
    if (!integerIn(draft.systemId, 1, 255)) errors.push('MAVLink system ID must be a whole number from 1 to 255.');
    if (!integerIn(draft.componentId, 1, 255)) errors.push('Autopilot component ID must be a whole number from 1 to 255.');
    const uid = draft.uid.trim();
    if (uid && (!/^[0-9]{1,24}$/.test(uid) || BigInt(uid) < 1n || BigInt(uid) > 18446744073709551615n)) errors.push('Controller UID must be a nonzero decimal uint64 (at most 18446744073709551615), or leave it blank if unknown.');
    if (draft.readbackEnabled && !integerIn(draft.remotePort, 1, 65535)) errors.push('Aircraft link UDP port must be the return-capable port configured on your radio or MAVLink router (1–65535).');
    if (draft.readbackEnabled) {
      const appSystemId = draft.appSystemId.trim() || String(defaultAppSystemId);
      const appComponentId = draft.appComponentId.trim() || String(defaultAppComponentId);
      if (!integerIn(appSystemId, 1, 255)) errors.push('App MAVLink system ID must be a whole number from 1 to 255, or leave it blank for 254.');
      else if (Number(appSystemId) === Number(draft.systemId)) errors.push('App MAVLink system ID must differ from the aircraft’s MAVLink system ID. Change it under Aircraft IDs and advanced settings.');
      if (!integerIn(appComponentId, 1, 255)) errors.push('App MAVLink component ID must be a whole number from 1 to 255, or leave it blank for 191.');
    }
  }
  if (scope !== 'telemetry') {
    if (draft.radioEnabled) {
      if (!validAddress(draft.radioHost)) errors.push('digiQ / SkyPulse address must be a single numeric IPv4 address.');
      if (!integerIn(draft.radioPort, 1, 65535)) errors.push('digiQ / SkyPulse HTTP port must be a whole number from 1 to 65535.');
    }
    if (draft.cameraEnabled) {
      if (!validAddress(draft.cameraHost)) errors.push('Camera address must be a single numeric IPv4 address.');
      if (!integerIn(draft.cameraPort, 1, 65535)) errors.push('Camera RTSP port must be a whole number from 1 to 65535.');
      const path = draft.cameraPath.trim() || '/';
      if (!path.startsWith('/') || path.length > 1024 || /[^\x21-\x7e]/.test(path)) errors.push('Camera RTSP path must start with /, contain no spaces, and be at most 1024 ASCII characters.');
    }
  }
  return errors;
}

export function setupProfile(draft: SetupDraft): CollectionProfile {
  if (draft.mode === 'offline') throw new Error('Offline analysis does not need a live observer profile.');
  const errors = validateSetup(draft, 'telemetry');
  if (errors.length) throw new Error(errors.join(' '));
  return {
    aircraft_id: draft.aircraft.trim(), aircraft_profile_version: draft.aircraftVersion.trim(), connection_profile_version: draft.connectionVersion.trim(),
    connection: {
      local_address: draft.localAddress.trim(), expected_sender: draft.senderAddress.trim(), listen_port: Number(draft.observerPort),
      system_id: Number(draft.systemId), component_id: Number(draft.componentId), feed_origin: 'live_forwarded', synthetic: draft.synthetic,
      ...(draft.uid.trim() ? { expected_uid: BigInt(draft.uid.trim()).toString() } : {}),
      ...(draft.readbackEnabled ? {readback: {
        remote_port: Number(draft.remotePort),
        ...(Number(draft.appSystemId.trim() || defaultAppSystemId) !== defaultAppSystemId ? {client_system_id: Number(draft.appSystemId)} : {}),
        ...(Number(draft.appComponentId.trim() || defaultAppComponentId) !== defaultAppComponentId ? {client_component_id: Number(draft.appComponentId)} : {}),
      }} : {}),
    },
    required_sources: [...requiredSources],
    ...(draft.environmentContext ? {environment_context: draft.environmentContext} : {}),
  };
}

export function setupSources(draft: SetupDraft): NetworkSource[] {
  if (draft.mode === 'offline') return [];
  const sources: NetworkSource[] = [{
    id: draft.telemetrySourceId || 'guided-observer', kind: 'udp', host: draft.senderAddress.trim(),
    port: Number(draft.observerPort), local_address: draft.localAddress.trim(), expected_format: 'mavlink',
  }];
  if (draft.radioEnabled) sources.push({ id: draft.radioSourceId || 'guided-radio', kind: 'skypulse', host: draft.radioHost.trim(), port: Number(draft.radioPort) });
  if (draft.cameraEnabled) sources.push({ id: draft.cameraSourceId || 'guided-camera', kind: 'rtsp', host: draft.cameraHost.trim(), port: Number(draft.cameraPort), path: draft.cameraPath.trim() || '/' });
  return sources;
}

export function saveSetup(draft: SetupDraft): void {
  const errors = validateSetup(draft);
  if (errors.length) throw new Error(errors.join(' '));
  const sources = storedSources();
  // Guided setup does not edit software declarations. Another workspace may
  // have saved newer declarations while this wizard was open, so preserve the
  // current profile at commit time, including an intentional removal.
  const latestProfile = object(readJson(PROFILE_KEY));
  const latestEnvironment = object(latestProfile?.environment_context)
    ? latestProfile!.environment_context as CollectionProfile['environment_context'] : undefined;
  const savedDraft: SetupDraft = {
    ...draft,
    environmentContext: latestEnvironment,
    telemetrySourceId: draft.telemetrySourceId || unusedSourceId(sources, 'guided-observer'),
    radioSourceId: draft.radioSourceId || unusedSourceId(sources, 'guided-radio'),
    cameraSourceId: draft.cameraSourceId || unusedSourceId(sources, 'guided-camera'),
  };
  const changes: [string, string][] = [];
  if (draft.mode !== 'offline') {
    const owned = new Set([savedDraft.telemetrySourceId, savedDraft.radioSourceId, savedDraft.cameraSourceId]);
    const configured = setupSources(savedDraft).map(source => {
      const previous = sources.find(item => item.id === source.id);
      const defaultPort = source.kind === 'skypulse' ? 80 : source.kind === 'rtsp' ? 554 : 14551;
      const sameEndpoint = previous?.kind === source.kind && previous.host === source.host &&
        previous.local_address === source.local_address && (previous.port ?? defaultPort) === source.port;
      return sameEndpoint && previous?.interface ? { ...source, interface: previous.interface } : source;
    });
    const merged = [...sources.filter(source => !owned.has(source.id)), ...configured];
    if (merged.length > 8) throw new Error('Network checks supports up to 8 configured sources. Remove an unused source in Network checks or skip an optional device before saving. Your existing settings have not changed.');
    changes.push([PROFILE_KEY, JSON.stringify(setupProfile(savedDraft))]);
    changes.push([SOURCES_KEY, JSON.stringify(merged)]);
  }
  changes.push([DRAFT_KEY, JSON.stringify({ draft: savedDraft, savedAt: new Date().toISOString() })]);

  // localStorage has no transaction. Keep exact original values and roll back
  // writes if quota, privacy settings, or another storage failure interrupts us.
  const previous = changes.map(([key]) => [key, localStorage.getItem(key)] as const);
  let written = 0;
  try {
    for (const [key, value] of changes) { localStorage.setItem(key, value); written++; }
  } catch {
    let restored = true;
    for (let index = written - 1; index >= 0; index--) {
      const [key, value] = previous[index];
      try { if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, value); } catch { restored = false; }
    }
    // A failed rollback may leave new values on disk. Invalidate old previews
    // and reload mounted forms before they can act on an obsolete profile.
    if (!restored) window.dispatchEvent(new Event(SETTINGS_CHANGED));
    throw new Error(restored ? 'This computer could not save connection settings. Your previous settings were retained. Check local storage availability and try again.' : 'Connection settings could not be saved completely or fully restored. Review Network checks and Observer setup before using them.');
  }
  window.dispatchEvent(new Event(SETTINGS_CHANGED));
}

export function getSetupSavedAt(): string | null {
  const value = object(readJson(DRAFT_KEY))?.savedAt;
  return typeof value === 'string' && Number.isFinite(Date.parse(value)) ? value : null;
}
