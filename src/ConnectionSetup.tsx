import { useEffect, useRef, useState, type FormEvent } from 'react';
import { errorMessage, request, type CollectionPreview, type NetworkInspection } from './api';
import { getSetupSavedAt, loadSetupDraft, saveSetup, setupProfile, setupSources, validateSetup, type SetupDraft, type SetupMode } from './connectionSettings';
import { Badge, Icon, Notes, readable } from './ui';
import './connection-setup.css';

const steps = ['Connection', 'Devices', 'Telemetry', 'Review'];
const modes: {id: SetupMode; title: string; description: string}[] = [
  {id: 'local_gcs', title: 'Ground station on this computer', description: 'Mission Planner or QGroundControl receives your field UDP or serial link and forwards a copy to Flight Lab.'},
  {id: 'network', title: 'Forwarded feed from another device', description: 'A configured radio, router, or another computer sends a separate telemetry feed to this computer.'},
  {id: 'offline', title: 'Analyze saved files only', description: 'Import existing flight logs and parameter files. No live connection settings are needed.'},
];

export default function ConnectionSetup({ready, observerActive, onCollection, onNetwork, onEvidence}: {
  ready: boolean; observerActive: boolean; onCollection: () => void; onNetwork: () => void; onEvidence: () => void;
}) {
  const [draft, setDraft] = useState(loadSetupDraft);
  const [step, setStep] = useState(0);
  const [errors, setErrors] = useState<string[]>([]);
  const [working, setWorking] = useState('');
  const [inspection, setInspection] = useState<NetworkInspection | null>(null);
  const [check, setCheck] = useState<NetworkInspection | null>(null);
  const [saved, setSaved] = useState(false);
  const [savedAt] = useState(getSetupSavedAt);
  const [forwardingConfirmed, setForwardingConfirmed] = useState(false);
  const title = useRef<HTMLHeadingElement>(null);
  const generation = useRef(0);
  const offline = draft.mode === 'offline';
  const localForwarding = draft.mode === 'local_gcs' || draft.mode === 'serial_gcs';
  const gcsName = draft.gcs === 'mission_planner' ? 'Mission Planner' : draft.gcs === 'qgroundcontrol' ? 'QGroundControl' : 'your ground station';
  const sources = setupSources(draft);
  useEffect(() => { title.current?.focus(); }, [step]);
  useEffect(() => {
    generation.current += 1;
    if (!ready) { setInspection(null); setCheck(null); }
    return () => { generation.current += 1; };
  }, [ready]);

  function update<K extends keyof SetupDraft>(key: K, value: SetupDraft[K]) {
    generation.current += 1;
    setDraft(previous => ({...previous, [key]: value}));
    setErrors([]); setCheck(null); setSaved(false); setForwardingConfirmed(false);
  }
  function chooseMode(mode: SetupMode) {
    generation.current += 1;
    setDraft(previous => ({...previous, mode,
      ...(mode === previous.mode ? {} : mode === 'local_gcs' || mode === 'serial_gcs'
        ? {localAddress: '127.0.0.1', senderAddress: '127.0.0.1'}
        : mode === 'network' ? {localAddress: '', senderAddress: ''} : {}),
    }));
    setErrors([]); setCheck(null); setSaved(false); setForwardingConfirmed(false);
  }
  function next(event: FormEvent) {
    event.preventDefault();
    const problems = step === 1 ? validateSetup(draft, 'devices') : step === 2 ? validateSetup(draft, 'telemetry') : [];
    setErrors(problems);
    if (problems.length) return;
    setStep(offline ? 3 : Math.min(step + 1, 3));
  }
  async function perform(label: string, action: () => Promise<void>) {
    setWorking(label); setErrors([]);
    try { await action(); } catch (cause) { setErrors([errorMessage(cause)]); }
    finally { setWorking(''); }
  }
  async function inspectComputer() {
    const current = generation.current;
    await perform('Finding this computer’s addresses', async () => {
      const result = await request<NetworkInspection>('network.inspect', {sources: []});
      if (current === generation.current) setInspection(result);
    });
  }
  async function checkConnections() {
    const problems = validateSetup(draft);
    if (problems.length) { setErrors(problems); return; }
    if (observerActive || !ready || !forwardingConfirmed) return;
    const current = generation.current;
    setCheck(null);
    await perform('Checking the configured connections', async () => {
      const result = await request<NetworkInspection>('network.probe', {sources, timeout_ms: 1500});
      if (current === generation.current) setCheck(result);
    });
  }
  async function save() {
    const problems = validateSetup(draft);
    if (problems.length) { setErrors(problems); return; }
    const current = generation.current;
    await perform('Saving connection settings', async () => {
      if (!offline) await request<CollectionPreview>('session.preview', setupProfile(draft) as unknown as Record<string, unknown>);
      if (current !== generation.current) return;
      saveSetup(draft);
      setSaved(true);
    });
  }

  return <div className="stack connection-setup">
    <div className="page-title"><div><span className="eyebrow">GET CONNECTED, ONE STEP AT A TIME</span><h1>Connection setup</h1><p>Tell Flight Lab where your data comes from. We’ll explain each setting and help you check it.</p></div><Badge tone={saved ? 'blue' : 'neutral'}>{saved ? 'Settings saved' : savedAt ? 'Review saved settings' : 'First-time setup'}</Badge></div>
    <ol className="setup-steps" aria-label="Setup progress">{steps.map((label, index) => <li key={label} aria-current={step === index ? 'step' : undefined} className={step === index ? 'current' : step > index ? 'done' : ''}><span>{index + 1}</span>{label}{offline && (index === 1 || index === 2) && <small>Not needed</small>}</li>)}</ol>
    <section className="panel">
      <div className="panel-heading"><div><h2 ref={title} tabIndex={-1}>{['How will you use Flight Lab?', 'Connect your equipment', 'Tell the app where to listen', 'Review your connection settings'][step]}</h2><p>Step {step + 1} of 4{offline ? ' · saved-file workflow' : ''}</p></div></div>
      <form className="padded setup-form" onSubmit={next} noValidate>
        <fieldset disabled={Boolean(working)}>
          {step === 0 && <>
            <div className="setup-options" role="radiogroup" aria-label="Connection method">{modes.map(mode => <label className={`setup-option ${draft.mode === mode.id ? 'selected' : ''}`} key={mode.id}><input type="radio" name="setup-mode" aria-label={mode.title} value={mode.id} checked={draft.mode === mode.id} onChange={() => chooseMode(mode.id)}/><span><strong>{mode.title}</strong><span>{mode.description}</span></span></label>)}</div>
            <p className="field-help">You can change these settings later from Connection setup. Existing recordings and imported files stay in your evidence library.</p>
          </>}
          {step === 1 && <>
            {localForwarding ? <>
              <label>Ground station<select value={draft.gcs} onChange={event => update('gcs', event.target.value as SetupDraft['gcs'])}><option value="mission_planner">Mission Planner</option><option value="qgroundcontrol">QGroundControl</option><option value="other">Another ground station / router</option></select></label>
              <p>Connect to the aircraft in {gcsName} first. Keep its normal telemetry connection and native flight logging enabled.</p>
              <div className="setup-guidance"><h3>Give Flight Lab its own telemetry copy</h3><p>Configure a separate MAVLink UDP forwarding output in {gcsName}. The next step shows the exact destination. Keep the controlling ground station’s receive port for that program.</p>{draft.gcs === 'qgroundcontrol' && <p>QGroundControl forwarding is one-way. It supports this passive observer; parameter readback and onboard-log requests need a separate bidirectional connection.</p>}<p className="field-help">The forwarding controls depend on the installed ground-station version. If it cannot provide an independent output, use saved logs or a separately commissioned router.</p></div>
            </> : <div className="setup-guidance"><h3>Use a dedicated forwarded feed</h3><p>Connect this computer to the radio or forwarding device’s network. The sender must already be configured to send a separate MAVLink copy to this computer. A radio’s management IP can be different from the address that sends telemetry.</p><p>Keep your internet adapter’s gateway and DNS unchanged. If the device uses a separate subnet, its connected adapter needs an unused address on that subnet. Open Network checks for route and address guidance.</p></div>}
            <div className="setup-optional"><h3>Optional device checks</h3><p>Only add devices you want Flight Lab to check. Use their actual settings; the addresses below are examples.</p>
              <label className="checkbox-label"><input type="checkbox" checked={draft.radioEnabled} onChange={event => update('radioEnabled', event.target.checked)}/>Check a digiQ / SkyPulse radio</label>
              {draft.radioEnabled && <div className="form-grid"><label>digiQ / SkyPulse IPv4<input inputMode="decimal" value={draft.radioHost} onChange={event => update('radioHost', event.target.value)} placeholder="For example 10.0.0.2" aria-describedby="setup-radio-help"/></label><label>Radio HTTP port<input inputMode="numeric" value={draft.radioPort} onChange={event => update('radioPort', event.target.value)}/></label><p id="setup-radio-help" className="field-help span-two">Find this in the radio’s current configuration. Port 80 is the usual HTTP port; use your device’s actual value. SkyPulse checks require its supported management endpoints. This is not the telemetry UDP port.</p></div>}
              <label className="checkbox-label"><input type="checkbox" checked={draft.cameraEnabled} onChange={event => update('cameraEnabled', event.target.checked)}/>Check a camera / RTSP stream</label>
              {draft.cameraEnabled && <div className="form-grid"><label>Camera IPv4<input inputMode="decimal" value={draft.cameraHost} onChange={event => update('cameraHost', event.target.value)} placeholder="From the camera’s network settings"/></label><label>Camera RTSP port<input inputMode="numeric" value={draft.cameraPort} onChange={event => update('cameraPort', event.target.value)}/></label><label className="span-two">Camera RTSP path<input value={draft.cameraPath} onChange={event => update('cameraPath', event.target.value)} placeholder="/"/></label><p className="field-help span-two">Use the port and path from the camera’s RTSP URL, usually port 554. Checks test the RTSP response, not video playback. Leave credentials out; authentication must be checked in your camera player or ground station.</p></div>}
            </div>
          </>}
          {step === 2 && <>
            <div className="setup-guidance"><h3>{localForwarding ? 'The ground station and Flight Lab share this computer' : 'Choose the receiving computer and actual sender'}</h3><p>{localForwarding ? '127.0.0.1 means this computer. Use it for both addresses when your local ground station forwards over loopback. The digiQ radio address belongs in the optional device checks, not in these loopback fields.' : 'Computer receive IPv4 is an address assigned to this computer’s connected adapter. Telemetry sender IPv4 is the device that actually sends the forwarded packets, which may be a radio, router, or another computer.'}</p></div>
            <div className="setup-discovery"><button type="button" className="button secondary small" disabled={!ready} onClick={inspectComputer}><Icon name="refresh"/>Find computer addresses</button><span className="field-help">Reads this computer’s adapters. It does not change settings or scan devices.</span></div>
            {inspection && <div className="setup-addresses"><h3>Addresses found on this computer</h3>{inspection.interfaces.flatMap(adapter => adapter.ipv4.map(ip => <button type="button" className="setup-address" disabled={!adapter.up} key={`${adapter.index}-${ip.address}`} onClick={() => update('localAddress', ip.address)}><strong>{ip.address}</strong><span>{adapter.name} · /{ip.prefix} · {adapter.up ? 'Adapter up' : 'Adapter down'}</span><span>Use this address</span></button>))}{!inspection.interfaces.some(adapter => adapter.ipv4.length) && <p>No IPv4 addresses were reported. Check the cable, adapter and operating-system network settings.</p>}{inspection.status !== 'inspected' && <p className="field-help">Adapter inspection: {readable(inspection.status)}. The list may be incomplete.</p>}</div>}
            <div className="form-grid">
              <label className="span-two">Aircraft profile name<input value={draft.aircraft} onChange={event => update('aircraft', event.target.value)} placeholder="A name you recognize, for example Q22 test aircraft" maxLength={160}/></label>
              <label>Computer receive IPv4<input inputMode="decimal" value={draft.localAddress} onChange={event => update('localAddress', event.target.value)} placeholder={localForwarding ? '127.0.0.1' : 'Address on this computer’s device adapter'} aria-describedby="setup-receiver-help"/></label>
              <label>Telemetry sender IPv4<input inputMode="decimal" value={draft.senderAddress} onChange={event => update('senderAddress', event.target.value)} placeholder={localForwarding ? '127.0.0.1' : 'Actual forwarding sender’s address'} aria-describedby="setup-sender-help"/></label>
              <p id="setup-receiver-help" className="field-help">The forwarding device must send to this receiving address. A remote device cannot reach this computer using 127.0.0.1.</p>
              <p id="setup-sender-help" className="field-help">Packets from other sender addresses are excluded. Do not assume this is the radio’s management IP.</p>
              <label>Observer UDP port<input inputMode="numeric" value={draft.observerPort} onChange={event => update('observerPort', event.target.value)} aria-describedby="setup-port-help"/></label>
              <p id="setup-port-help" className="field-help">14551 is a suggested separate port. Match it in the forwarding destination and keep it free for Flight Lab. Leave the GCS’s normal port (often 14550) and video port alone.</p>
              <label>MAVLink system ID<input inputMode="numeric" value={draft.systemId} onChange={event => update('systemId', event.target.value)}/></label>
              <label>Autopilot component ID<input inputMode="numeric" value={draft.componentId} onChange={event => update('componentId', event.target.value)}/></label>
              <p className="field-help span-two">Check both IDs in your ground station’s MAVLink inspector or vehicle configuration. The suggested value 1 is common, but must match your aircraft; IDs identify a stream, not verified physical identity.</p>
              <label className="span-two">Controller UID (optional)<input inputMode="numeric" value={draft.uid} onChange={event => update('uid', event.target.value)} placeholder="Leave blank if unknown"/></label>
            </div>
            <details className="technical setup-advanced"><summary>Existing profile versions and test data</summary><div className="form-grid"><label>Aircraft profile version<input value={draft.aircraftVersion} onChange={event => update('aircraftVersion', event.target.value)} maxLength={160}/></label><label>Connection profile version<input value={draft.connectionVersion} onChange={event => update('connectionVersion', event.target.value)} maxLength={160}/></label></div><label className="checkbox-label"><input type="checkbox" checked={draft.synthetic} onChange={event => update('synthetic', event.target.checked)}/>This feed contains synthetic bench-test data</label></details>
          </>}
          {step === 3 && <>
            {offline ? <div className="setup-guidance"><h3>Ready to work with saved files</h3><p>No radio IP, UDP port or COM port is required. Open Evidence to import your original BIN, LOG, TLOG or parameter files.</p><p>Any existing live-connection settings are kept for later. Saving this choice does not stop an active observer.</p></div> : <>
              <div className="setup-destination"><span>Set your forwarding destination to</span><strong>{draft.localAddress}:{draft.observerPort}</strong><p>UDP · accept telemetry from {draft.senderAddress} · MAVLink system {draft.systemId}, component {draft.componentId}</p></div>
              <dl className="metadata setup-summary"><dt>Aircraft profile</dt><dd>{draft.aircraft}</dd><dt>Connection</dt><dd>{modes.find(mode => mode.id === draft.mode)?.title}</dd><dt>Radio management</dt><dd>{draft.radioEnabled ? `${draft.radioHost}:${draft.radioPort}` : 'Optional check skipped'}</dd><dt>Camera check</dt><dd>{draft.cameraEnabled ? `rtsp://${draft.cameraHost}:${draft.cameraPort}${draft.cameraPath}` : 'Optional check skipped'}</dd><dt>Controller identity</dt><dd>{draft.uid || 'UID unknown · passive observation still available'}</dd></dl>
              <ol className="setup-checklist"><li>Set the forwarding output to the destination above. Keep the GCS’s control connection and native logging enabled.</li><li>For a remote sender, verify the connected adapter’s address/subnet and a scoped inbound UDP firewall rule. Network checks provides more detailed guidance.</li><li>Check the connection below when the feed is available, or save now and test later.</li></ol>
              <label className="checkbox-label"><input type="checkbox" checked={forwardingConfirmed} onChange={event => setForwardingConfirmed(event.target.checked)}/>This is a separate forwarded feed; the receive port is reserved for Flight Lab.</label>
              <div className="setup-discovery"><button type="button" className="button secondary" disabled={!ready || observerActive || !forwardingConfirmed} onClick={checkConnections}><Icon name="network"/>Check connections</button><span className="field-help">Briefly listens for telemetry and contacts only your selected radio/camera. Starts no recording.</span></div>
              {observerActive && <Notes title="An observer is already recording" items={['Keep the existing recording running. Use Prepare & watch to inspect it; connection probing is unavailable here while it owns a receive socket. Saved settings apply to the next setup, not the active capture.']}/>}
              {check ? <ConnectionResults result={check} sources={sources}/> : <div className="setup-check-status"><Badge>Connection not tested</Badge><p>Saving valid settings does not establish that a device is connected or that telemetry is arriving.</p></div>}
              <details className="technical setup-advanced"><summary>Parameter downloads, onboard logs and video</summary><p>This setup prepares passive telemetry observation. Parameter readbacks and onboard-log requests have a separate reviewed bidirectional connection in Prepare &amp; watch; they need the actual remote address/port and a known controller UID. A one-way forwarded feed cannot carry those requests.</p><p>Camera and UDP checks do not decode video. Keep your working camera player or ground station and its video port. Flight Lab’s optional video diagnostics are available in Network checks.</p></details>
            </>}
            {saved && <div className="setup-saved" role="status"><Icon name="check"/><div><strong>Setup saved on this computer</strong><p>{offline ? 'Your saved-file workflow is ready.' : 'Prepare & watch and Network checks now use these settings. Review and start the observer there when you are ready.'}</p></div></div>}
          </>}
          {errors.length > 0 && <div role="alert" className="setup-errors"><Notes title="Check these settings" items={errors}/></div>}
          {working && <p role="status" aria-live="polite" className="working-status"><span className="spinner"/>{working}…</p>}
          <div className="setup-footer"><div>{step > 0 && <button type="button" className="button secondary" onClick={() => { setStep(offline ? 0 : step - 1); setErrors([]); setSaved(false); }}>Back</button>}</div><div className="button-row">{step < 3 ? <button type="submit" className="button primary">Continue<Icon name="arrow"/></button> : <>
            {!saved && <button type="button" className="button primary" disabled={!ready && !offline} onClick={save}>Save setup</button>}
            {saved && <button type="button" className="button primary" onClick={offline ? onEvidence : onCollection}>{offline ? 'Open Evidence' : 'Open Prepare & watch'}<Icon name="arrow"/></button>}
            {!offline && <button type="button" className="button secondary" onClick={onNetwork}>Open Network checks</button>}
          </>}</div></div>
        </fieldset>
      </form>
    </section>
  </div>;
}

function ConnectionResults({result, sources}: {result: NetworkInspection; sources: ReturnType<typeof setupSources>}) {
  return <section className="setup-results" aria-label="Connection check results"><h3>What the checks found</h3>
    {result.status === 'partial' && <p>Some host checks were unavailable. The results below do not establish that the whole connection is healthy.</p>}
    {sources.map(source => {
      const probe = result.probes?.find(item => item.source_id === source.id);
      const target = result.targets.find(item => item.source_id === source.id);
      const heartbeat = probe?.state === 'telemetry_validated';
      const label = source.kind === 'udp' ? 'Telemetry feed' : source.kind === 'skypulse' ? 'digiQ / SkyPulse' : 'Camera / RTSP';
      return <article className="setup-result" key={source.id}><div className="line-between"><strong>{label}</strong><Badge tone={heartbeat ? 'blue' : 'neutral'}>{heartbeat ? 'MAVLink heartbeat received' : probe ? readable(probe.state) : 'Not checked'}</Badge></div>
        <p>{probe?.details || 'No probe result was returned. Run the check again when the feed is available.'}</p>
        {heartbeat && <p>Valid heartbeat framing was observed. Confirm the selected aircraft IDs in Prepare &amp; watch; this brief check does not verify aircraft identity or readiness to fly.</p>}
        {probe?.recovery_action && <p><strong>Next step:</strong> {probe.recovery_action}</p>}
        {source.kind === 'udp' && !heartbeat && <p>Check that the sender forwards to <strong>{source.local_address}:{source.port}</strong>, that packets come from <strong>{source.host}</strong>, and that this port is free and allowed through the receiving computer’s firewall.</p>}
        {target && target.issues.length > 0 && <Notes title="Address and route checks" items={target.issues}/>}
      </article>;
    })}
    <p className="field-help">Checks are a point-in-time observation and are cleared when settings change. Save settings separately.</p>
  </section>;
}
