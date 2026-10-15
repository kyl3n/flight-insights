import { useEffect, useRef, useState, type FormEvent } from 'react';
import { errorMessage, request, type CollectionPreview, type NetworkInspection } from './api';
import { getSetupSavedAt, loadSetupDraft, saveSetup, setupProfile, setupSources, validateSetup, type SetupDraft, type SetupMode } from './connectionSettings';
import { Badge, Icon, Notes, readable } from './ui';
import './connection-setup.css';

const modes: {id: SetupMode; title: string; description: string}[] = [
  {id: 'network', title: 'Live aircraft connection', description: 'Connect through your UDP radio or MAVLink router, alongside any ground station.'},
  {id: 'local_gcs', title: 'Local telemetry feed', description: 'Receive telemetry supplied by a ground station or router on this computer.'},
  {id: 'offline', title: 'Saved flight logs', description: 'Import logs now. Add a live connection whenever you need it.'},
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
  const title = useRef<HTMLHeadingElement>(null);
  const generation = useRef(0);
  const offline = draft.mode === 'offline';
  const local = draft.mode === 'local_gcs' || draft.mode === 'serial_gcs';
  const sources = setupSources(draft);

  useEffect(() => { title.current?.focus(); }, [step]);
  useEffect(() => {
    generation.current++;
    if (!ready) { setInspection(null); setCheck(null); }
    return () => { generation.current++; };
  }, [ready]);

  function update<K extends keyof SetupDraft>(key: K, value: SetupDraft[K]) {
    generation.current++;
    setDraft(previous => ({...previous, [key]: value}));
    setErrors([]); setCheck(null); setSaved(false);
  }
  function chooseMode(mode: SetupMode) {
    generation.current++;
    setDraft(previous => ({...previous, mode, ...(mode === previous.mode ? {} : mode === 'local_gcs'
      ? {localAddress: '127.0.0.1', senderAddress: '127.0.0.1', readbackEnabled: false}
      : mode === 'network' ? {localAddress: '', senderAddress: '', readbackEnabled: true} : {})}));
    setErrors([]); setCheck(null); setSaved(false);
  }
  function next(event: FormEvent) {
    event.preventDefault();
    const problems = validateSetup(draft);
    setErrors(problems);
    if (!problems.length) setStep(1);
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
    if (observerActive || !ready) return;
    const current = generation.current;
    setCheck(null);
    await perform('Checking the connection', async () => {
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
      saveSetup(draft); setSaved(true);
    });
  }

  return <div className="stack connection-setup">
    <div className="page-title"><div><span className="eyebrow">YOUR AIRCRAFT, YOUR GROUND STATION</span><h1>Setup</h1><p>Set up collection once. Your flights keep their metrics, settings and software context together.</p></div><Badge tone={saved ? 'blue' : 'neutral'}>{saved ? 'Settings saved' : savedAt ? 'Saved setup' : 'First-time setup'}</Badge></div>
    <ol className="setup-steps setup-two-steps" aria-label="Setup progress">{['Configure', 'Review'].map((label, index) => <li key={label} aria-current={step === index ? 'step' : undefined} className={step === index ? 'current' : step > index ? 'done' : ''}><span>{index + 1}</span>{label}</li>)}</ol>
    <section className="panel">
      <div className="panel-heading"><h2 ref={title} tabIndex={-1}>{step === 0 ? 'Where will your flight data come from?' : 'Ready to collect flight data'}</h2></div>
      <form className="padded setup-form" onSubmit={next} noValidate><fieldset disabled={Boolean(working)}>
        {step === 0 ? <>
          <div className="setup-options" role="radiogroup" aria-label="Connection method">{modes.map(mode => <label className={`setup-option ${draft.mode === mode.id ? 'selected' : ''}`} key={mode.id}><input type="radio" name="setup-mode" aria-label={mode.title} checked={draft.mode === mode.id} onChange={() => chooseMode(mode.id)}/><span><strong>{mode.title}</strong><span>{mode.description}</span></span></label>)}</div>
          {!offline && <>
            <div className="form-grid">
              <label className="span-two">Aircraft profile name<input value={draft.aircraft} onChange={event => update('aircraft', event.target.value)} placeholder="For example Q22 field aircraft" maxLength={160}/></label>
              <label>Computer receive IPv4<input inputMode="decimal" value={draft.localAddress} onChange={event => update('localAddress', event.target.value)} placeholder={local ? '127.0.0.1' : 'This computer’s address on the aircraft network'}/></label>
              <label>Telemetry sender IPv4<input inputMode="decimal" value={draft.senderAddress} onChange={event => update('senderAddress', event.target.value)} placeholder={local ? '127.0.0.1' : 'Your radio or MAVLink router'}/></label>
              <div><label>Observer UDP port<input inputMode="numeric" value={draft.observerPort} onChange={event => update('observerPort', event.target.value)} aria-describedby="setup-app-port-help"/></label><p id="setup-app-port-help" className="field-help">One app port for recording and supported parameter requests. Keep it free for Flight Lab.</p></div>
              <div className="setup-discovery"><button type="button" className="button secondary small" disabled={!ready} onClick={inspectComputer}><Icon name="refresh"/>Find computer addresses</button></div>
            </div>
            {inspection && <div className="setup-addresses"><h3>Addresses found on this computer</h3>{inspection.interfaces.flatMap(adapter => adapter.ipv4.map(ip => <button type="button" className="setup-address" disabled={!adapter.up} key={`${adapter.index}-${ip.address}`} onClick={() => update('localAddress', ip.address)}><strong>{ip.address}</strong><span>{adapter.name} · /{ip.prefix} · {adapter.up ? 'Adapter up' : 'Adapter down'}</span><span>Use this address</span></button>))}{!inspection.interfaces.some(adapter => adapter.ipv4.length) && <p>No IPv4 addresses were reported. Check the computer’s network connection.</p>}{inspection.status !== 'inspected' && <p className="field-help">Adapter inspection: {readable(inspection.status)}. The list may be incomplete.</p>}</div>}
            <p className="field-help">{local ? '127.0.0.1 means this computer. Your GCS or local router supplies this app’s endpoint while keeping its own connection.' : 'Use the actual sender’s address, which can differ from a radio’s management IP. Configure the radio or router to deliver telemetry to this computer and app port.'}</p>
            <div className="setup-automation">
              <label className="checkbox-label"><input type="checkbox" checked={draft.readbackEnabled} onChange={event => update('readbackEnabled', event.target.checked)}/>Capture parameters automatically before and after flights</label>
              <p className="field-help">{draft.readbackEnabled ? 'Uses this same app connection for read-only requests while disarmed. Requires a bidirectional radio or router endpoint; QGroundControl’s one-way forwarding cannot return requests.' : 'Record the incoming stream and import onboard logs. Full parameter exports can be added later; telemetry alone may contain only a subset.'}</p>
              {draft.readbackEnabled && <div className="form-grid"><label>Aircraft link UDP port<input inputMode="numeric" value={draft.remotePort} onChange={event => update('remotePort', event.target.value)} placeholder="Return-capable port on your radio / router"/></label><label>Controller UID<input inputMode="numeric" value={draft.uid} onChange={event => update('uid', event.target.value)} placeholder="Aircraft’s nonzero decimal UID" aria-describedby="setup-uid-help"/></label><p id="setup-uid-help" className="field-help span-two">In your GCS’s MAVLink inspector, select the aircraft and find <code>AUTOPILOT_VERSION.uid</code>. Enter that UID as a decimal number; it is different from the MAVLink system ID. Readbacks wait for the same UID, boot-clock evidence and fresh disarmed telemetry.</p><div className="span-two"><p className="field-help">If the UID or return port is unavailable, you can record telemetry now and enable automatic readbacks later. Imported logs and parameter exports remain usable.</p><button type="button" className="button secondary small" onClick={()=>update('readbackEnabled',false)}>Record telemetry first</button></div></div>}
            </div>
            <details className="technical setup-advanced"><summary>Aircraft IDs and advanced settings</summary><div className="form-grid"><label>MAVLink system ID<input inputMode="numeric" value={draft.systemId} onChange={event => update('systemId', event.target.value)}/></label><label>Autopilot component ID<input inputMode="numeric" value={draft.componentId} onChange={event => update('componentId', event.target.value)}/></label>{!draft.readbackEnabled && <label className="span-two">Controller UID (optional)<input inputMode="numeric" value={draft.uid} onChange={event => update('uid', event.target.value)} placeholder="Leave blank if unknown"/></label>}{draft.readbackEnabled&&<><label>App MAVLink system ID<input inputMode="numeric" value={draft.appSystemId} onChange={event=>update('appSystemId',event.target.value)} placeholder="254" aria-describedby="setup-app-identity-help" title="Flight Lab’s identity for read-only requests; choose a system ID different from the aircraft and other clients."/></label><label>App MAVLink component ID<input inputMode="numeric" value={draft.appComponentId} onChange={event=>update('appComponentId',event.target.value)} placeholder="191" aria-describedby="setup-app-identity-help"/></label><p id="setup-app-identity-help" className="field-help span-two">These identify Flight Lab’s requests, not the aircraft. Defaults are system 254 / component 191. Change them only if another app on your MAVLink link uses the same identity; the app system ID must differ from the aircraft’s.</p></>}<label>Aircraft profile version<input value={draft.aircraftVersion} onChange={event => update('aircraftVersion', event.target.value)} maxLength={160}/></label><label>Connection profile version<input value={draft.connectionVersion} onChange={event => update('connectionVersion', event.target.value)} maxLength={160}/></label></div><label className="checkbox-label"><input type="checkbox" checked={draft.synthetic} onChange={event => update('synthetic', event.target.checked)}/>This feed contains synthetic bench-test data</label></details>
            <p className="field-help">Selected aircraft: system {draft.systemId}, component {draft.componentId}. Change these above if your aircraft uses different MAVLink IDs.</p>
            <details className="technical setup-advanced"><summary>Optional device checks</summary><p className="field-help">These checks are separate from the aircraft telemetry connection.</p><label className="checkbox-label"><input type="checkbox" checked={draft.radioEnabled} onChange={event => update('radioEnabled', event.target.checked)}/>Check a digiQ / SkyPulse radio</label>{draft.radioEnabled && <div className="form-grid"><label>digiQ / SkyPulse IPv4<input inputMode="decimal" value={draft.radioHost} onChange={event => update('radioHost', event.target.value)} placeholder="Radio management address"/></label><label>Radio HTTP port<input inputMode="numeric" value={draft.radioPort} onChange={event => update('radioPort', event.target.value)}/></label></div>}<label className="checkbox-label"><input type="checkbox" checked={draft.cameraEnabled} onChange={event => update('cameraEnabled', event.target.checked)}/>Check a camera / RTSP stream</label>{draft.cameraEnabled && <div className="form-grid"><label>Camera IPv4<input inputMode="decimal" value={draft.cameraHost} onChange={event => update('cameraHost', event.target.value)}/></label><label>Camera RTSP port<input inputMode="numeric" value={draft.cameraPort} onChange={event => update('cameraPort', event.target.value)}/></label><label className="span-two">Camera RTSP path<input value={draft.cameraPath} onChange={event => update('cameraPath', event.target.value)}/></label></div>}</details>
          </>}
          {offline && <p className="setup-offline-note">No connection settings required. Import your original BIN, LOG or TLOG files from Flights to build your history.</p>}
        </> : <>
          {offline ? <div className="setup-guidance"><h3>Ready for saved flight logs</h3><p>Flight Lab analyzes imported logs and brings available comparisons and recommendations into each flight. Live collection stays off until you choose a live workflow in Setup.</p>{observerActive&&<p>A collection is already running. Saving this workflow keeps that recording intact; stop it from Flights when you are finished.</p>}</div> : <>
            <div className="setup-destination"><span>Deliver aircraft telemetry to this app endpoint</span><strong>{draft.localAddress}:{draft.observerPort}</strong><p>Accept from {draft.senderAddress} · system {draft.systemId}, component {draft.componentId}</p></div>
            <dl className="metadata setup-summary"><dt>Aircraft</dt><dd>{draft.aircraft}</dd><dt>Parameter capture</dt><dd>{draft.readbackEnabled ? `Automatic · same app port, requests to ${draft.senderAddress}:${draft.remotePort}` : 'Incoming telemetry only · full exports can be imported'}</dd><dt>Computer &amp; software</dt><dd>Observed automatically for each collection and recorded arm cycle. Unavailable versions stay unknown.</dd></dl>
            <p className="field-help">Start collecting from Flights before arming. Keep Flight Lab open and your ground station’s connection and onboard logging enabled.</p>
            <div className="setup-discovery"><button type="button" className="button secondary" disabled={!ready || observerActive} onClick={checkConnections}><Icon name="network"/>Check connections</button><span className="field-help">Optional: briefly listen for telemetry and check selected devices.</span></div>
            {observerActive && <p className="field-help">Collection is running. Saved changes apply to the next collection; connection probing is unavailable until it stops.</p>}
            {check ? <ConnectionResults result={check} sources={sources}/> : <p className="field-help">Connection not tested. Saving settings does not confirm telemetry or a return-capable parameter path.</p>}
            <details className="technical setup-advanced"><summary>How automatic collection works</summary><p>The app records telemetry and keeps observed flight intervals together. With parameter capture enabled, it requests a full readback while disarmed and refreshes stale preflight snapshots within a bounded request budget. Physical aircraft and link qualification are still required.</p><p>Parameter requests do not write settings. Onboard log transfer is a separate optional action after collection because log requests may interrupt onboard logging. Import SD logs for the richest performance analysis.</p><p>Mission Planner, QGroundControl and other GCS applications use the same app workflow. A router or radio must provide a working return path for readbacks; a one-way feed still supports recording.</p></details>
          </>}
          {saved && <div className="setup-saved" role="status"><Icon name="check"/><div><strong>Setup saved on this computer</strong><p>{offline ? 'Import logs from Flights to begin.' : 'Start collecting from Flights when your aircraft link is connected.'}</p></div></div>}
        </>}
        {errors.length > 0 && <div role="alert" className="setup-errors"><Notes title="Check these settings" items={errors}/></div>}
        {working && <p role="status" aria-live="polite" className="working-status"><span className="spinner"/>{working}…</p>}
        <div className="setup-footer"><div>{step > 0 && <button type="button" className="button secondary" onClick={() => { setStep(0); setErrors([]); setSaved(false); }}>Back</button>}</div><div className="button-row">{step === 0 ? <button type="submit" className="button primary">Continue<Icon name="arrow"/></button> : <>{!saved && <button type="button" className="button primary" disabled={!ready && !offline} onClick={save}>Save setup</button>}{saved && <button type="button" className="button primary" onClick={offline ? onEvidence : onCollection}>Open flights<Icon name="arrow"/></button>}{!offline && <button type="button" className="button secondary" onClick={onNetwork}>Connection troubleshooting</button>}</>}</div></div>
      </fieldset></form>
    </section>
  </div>;
}

function ConnectionResults({result, sources}: {result: NetworkInspection; sources: ReturnType<typeof setupSources>}) {
  return <section className="setup-results" aria-label="Connection check results"><h3>Connection checks</h3>{result.status === 'partial' && <p>Some computer checks were unavailable.</p>}{sources.map(source => {
    const probe = result.probes?.find(item => item.source_id === source.id);
    const target = result.targets.find(item => item.source_id === source.id);
    const heartbeat = probe?.state === 'telemetry_validated';
    return <article className="setup-result" key={source.id}><div className="line-between"><strong>{source.kind === 'udp' ? 'Aircraft telemetry' : source.kind === 'skypulse' ? 'digiQ / SkyPulse' : 'Camera / RTSP'}</strong><Badge tone={heartbeat ? 'blue' : 'neutral'}>{heartbeat ? 'MAVLink heartbeat received' : probe ? readable(probe.state) : 'Not checked'}</Badge></div><p>{probe?.details || 'No probe result was returned.'}</p>{probe?.recovery_action && <p><strong>Next step:</strong> {probe.recovery_action}</p>}{heartbeat && <p className="field-help">This confirms received framing, not aircraft identity or a bidirectional request path.</p>}{source.kind === 'udp' && !heartbeat && <p className="field-help">Check delivery to {source.local_address}:{source.port} from {source.host}, and the receiving computer’s firewall.</p>}{target && target.issues.length > 0 && <Notes title="Address and route checks" items={target.issues}/>}</article>;
  })}</section>;
}
