import { useEffect, useRef, useState } from 'react';
import { errorMessage, request, type Run } from './api';
import { Badge, Icon, Notes, readable } from './ui';

export interface FlightEnvironmentContext {
  gcs_name?: string;
  gcs_version?: string;
  applications?: {name: string; version: string}[];
  notes?: string;
}

interface SoftwareVersion {
  name: string;
  version: string | null;
  build: string | null;
  provenance: string;
}

export interface FlightEnvironmentSnapshotData {
  schema_version: 1;
  id: string | null;
  content_sha256: string | null;
  observed_at: string | null;
  status: 'captured' | 'partial' | 'unavailable';
  scope: 'current_host' | 'capture_start' | 'armed_interval_start';
  host: {os: string | null; distribution: string | null; distribution_version: string | null; release: string | null; version: string | null; architecture: string | null; provenance: string};
  software: {application: SoftwareVersion; engine: SoftwareVersion};
  detected_applications: {name: string; category: 'gcs' | 'router' | 'video'; version: string | null; process_name: string; provenance: 'running_process_name';version_status?:string;version_provenance?:string|null;version_reason?:string;version_observed_at?:string|null}[];
  declared_context: FlightEnvironmentContext & {provenance: 'operator_declared'};
  load: {memory_percent: number | null; available_memory_bytes: number | null; load_average_1m_5m_15m: number[] | null; engine_rss_bytes: number | null; provenance: string};
  coverage: {process_scan_status: 'complete' | 'partial' | 'unavailable'; names_checked: number; version_detection: string};
  limitations: string[];
  errors: string[];
}

export function EnvironmentSetup({ready, busy, run, value, onChange}: {
  ready: boolean;
  busy: string;
  run: Run;
  value?: FlightEnvironmentContext;
  onChange: (value: FlightEnvironmentContext) => void;
}) {
  const [inspection, setInspection] = useState<FlightEnvironmentSnapshotData | null>(null);
  const [error, setError] = useState('');
  const generation = useRef(0);
  const context = value || {};
  const applications = context.applications || [];
  const disabled = !ready || Boolean(busy);
  const detectedGcs = inspection?.detected_applications.filter(application => application.category === 'gcs') || [];
  const selectedGcs = context.gcs_name?.trim().toLowerCase();
  const matchingGcs = selectedGcs ? detectedGcs.find(application => application.name.toLowerCase() === selectedGcs) : detectedGcs.length === 1 ? detectedGcs[0] : undefined;
  const askGcsVersion = !matchingGcs?.version || Boolean(context.gcs_version);

  useEffect(() => {
    generation.current++;
    if (!ready) setInspection(null);
    return () => { generation.current++; };
  }, [ready]);

  async function inspect() {
    const current = generation.current;
    setInspection(null);
    setError('');
    await run('Inspecting flight computer', async () => {
      try {
        const result = await request<FlightEnvironmentSnapshotData>('environment.inspect', {environment_context: context});
        if (generation.current === current) setInspection(result);
      } catch (cause) {
        if (generation.current === current) setError(errorMessage(cause));
      }
    });
  }

  function updateApplication(index: number, field: 'name' | 'version', next: string) {
    onChange({...context, applications: applications.map((application, at) => at === index ? {...application, [field]: next} : application)});
  }

  return <details className="conditions environment-setup">
    <summary>Flight computer &amp; software</summary>
    <p>The computer, Flight Lab and analysis-engine versions are saved with each capture and observed flight. Add the ground station and other flight software you use so later reports retain that context.</p>
    <div className="button-row"><button type="button" className="button secondary small" disabled={disabled} onClick={inspect}><Icon name="refresh"/>Inspect flight computer</button><span className="field-help">Reads this computer and checks recognized running flight software and available version metadata.</span></div>
    {error && <div role="alert"><Notes title="Computer inspection unavailable" items={[error, 'You can still record a flight. Any context that cannot be collected will be marked unavailable.']}/></div>}
    {inspection && <section className="environment-inspection" aria-label="Current flight computer inspection"><h4>Current computer</h4><DetectedEnvironment snapshot={inspection}/><p className="field-help">This check describes the computer now. Each capture and flight keeps its own observation.</p></section>}
    <div className="form-grid">
      <label>Ground station name<input value={context.gcs_name || ''} maxLength={160} placeholder={matchingGcs?.name || 'For example Mission Planner or QGroundControl'} onChange={event => onChange({...context, gcs_name: event.target.value})}/></label>
      {askGcsVersion && <label>Ground station version (if known)<input value={context.gcs_version || ''} maxLength={160} placeholder="From the ground station’s About screen" onChange={event => onChange({...context, gcs_name: context.gcs_name || matchingGcs?.name || '', gcs_version: event.target.value})}/></label>}
    </div>
    <p className="field-help">Entries below and ground-station details you enter are saved as operator-declared. A running process name does not reveal its version or prove which application controlled the flight. Leave unknown versions blank.</p>
    {applications.map((application, index) => <div className="form-grid" key={index}>
      <label>Other flight application {index + 1}<input value={application.name} maxLength={160} placeholder="Only software used for this flight" onChange={event => updateApplication(index, 'name', event.target.value)}/></label>
      <label>Application {index + 1} version (if known)<input value={application.version} maxLength={160} onChange={event => updateApplication(index, 'version', event.target.value)}/></label>
      <div className="span-two"><button type="button" className="button secondary small" onClick={() => onChange({...context, applications: applications.filter((_, at) => at !== index)})}>Remove application {index + 1}</button></div>
    </div>)}
    <div className="button-row"><button type="button" className="button secondary small" disabled={applications.length >= 8} onClick={() => onChange({...context, applications: [...applications, {name: '', version: ''}]})}>Add flight application</button></div>
    <label>Flight computer notes (optional)<textarea value={context.notes || ''} maxLength={2000} rows={3} placeholder="Relevant changes, such as a ground-station update or another computer used for this flight" onChange={event => onChange({...context, notes: event.target.value})}/></label>
    <p className="field-help">These details are optional. No baseline flight or earlier recording is required to start collecting evidence.</p>
  </details>;
}

function DetectedEnvironment({snapshot}: {snapshot: FlightEnvironmentSnapshotData}) {
  const host = snapshot.host.distribution
    ? [snapshot.host.distribution, snapshot.host.distribution_version].filter(Boolean).join(' ')
    : [snapshot.host.os, snapshot.host.release].filter(Boolean).join(' ');
  const application = snapshot.software.application;
  const engine = snapshot.software.engine;
  return <>
    <dl className="metadata">
      <dt>Operating system</dt><dd>{host || 'Unavailable'}{snapshot.host.architecture ? ` · ${snapshot.host.architecture}` : ''}</dd>
      {snapshot.host.distribution && <><dt>System release</dt><dd>{snapshot.host.release || 'Unavailable'}</dd></>}
      <dt>Operating-system build</dt><dd>{snapshot.host.version || 'Unavailable'}</dd>
      <dt>{application.name || 'Flight Lab'} version</dt><dd>{application.version || 'Version unavailable'}{application.build ? ` · build ${application.build}` : ''}</dd>
      <dt>{engine.name || 'Analysis engine'} version</dt><dd>{engine.version || 'Version unavailable'}{engine.build ? ` · build ${engine.build}` : ''}</dd>
    </dl>
    <p className="field-help">Computer details are reported by the operating system. Flight Lab and the analysis engine report their own versions; missing versions remain unknown.</p>
    <h4>Detected running flight software</h4>
    {snapshot.detected_applications.length ? <div className="table-scroll"><table><caption className="sr-only">Detected running flight software and version coverage</caption><thead><tr><th>Application</th><th>Version</th><th>Evidence</th></tr></thead><tbody>{snapshot.detected_applications.map((application, index) => <tr key={`${application.process_name}-${index}`}><td>{application.name}</td><td>{application.version || (application.version_status==='pending'?'Version lookup pending at observation':application.version_status==='ambiguous'?'Multiple running versions':'Version not detected')}</td><td>{application.version_provenance?readable(application.version_provenance):'Running process name only'}{application.version_reason&&<small>{readable(application.version_reason)}</small>}</td></tr>)}</tbody></table></div> : <p>{snapshot.coverage.process_scan_status === 'complete' ? 'No recognized flight applications were observed running.' : 'Running flight applications could not be fully checked.'}</p>}
    {snapshot.coverage.process_scan_status !== 'complete' && snapshot.detected_applications.length > 0 && <p className="field-help">The running-application check was incomplete; other flight applications may have been missed.</p>}
    {snapshot.status === 'partial' && <p className="field-help">Some computer or software details were unavailable. Missing information is retained as unknown.</p>}
  </>;
}

export function FlightEnvironmentSnapshot({snapshot}: {snapshot?: FlightEnvironmentSnapshotData | null}) {
  if (!snapshot || !snapshot.id || snapshot.errors.includes('not_recorded')) return <section className="flight-environment-snapshot" aria-label="Recorded flight computer and software"><h3>Flight computer &amp; software</h3><p>Computer and software context was not recorded for this flight. Current settings are not used to fill in its history.</p></section>;
  const declared = snapshot.declared_context;
  const applications = declared.applications || [];
  return <section className="flight-environment-snapshot" aria-label="Recorded flight computer and software">
    <div className="line-between"><h3>Flight computer &amp; software</h3><Badge>{snapshot.status === 'captured' ? 'Recorded context' : snapshot.status === 'partial' ? 'Partial context' : 'Context unavailable'}</Badge></div>
    <p>This saved observation belongs to {snapshot.scope === 'armed_interval_start' ? 'the start of this armed interval' : 'the start of this capture'}. Later settings and software updates do not replace it.</p>
    {snapshot.observed_at && <p className="field-help">Observed at <time dateTime={snapshot.observed_at}>{snapshot.observed_at}</time></p>}
    <DetectedEnvironment snapshot={snapshot}/>
    <h4>Operator-declared flight software</h4>
    {declared.gcs_name || declared.gcs_version || applications.length ? <div className="table-scroll"><table><caption className="sr-only">Operator-declared flight software and versions</caption><thead><tr><th>Application</th><th>Version</th><th>Evidence</th></tr></thead><tbody>{(declared.gcs_name || declared.gcs_version) && <tr><td>{declared.gcs_name || 'Ground station name not entered'}</td><td>{declared.gcs_version || 'Version not entered'}</td><td>Entered by operator</td></tr>}{applications.map((application, index) => <tr key={index}><td>{application.name || 'Application name not entered'}</td><td>{application.version || 'Version not entered'}</td><td>Entered by operator</td></tr>)}</tbody></table></div> : <p>No ground-station version or additional software was entered for this flight.</p>}
    {declared.notes && <p><strong>Operator notes:</strong> {declared.notes}</p>}
    <p className="field-help">Detected executable/package versions and operator-entered versions are separate observations. Neither alone establishes which application controlled the aircraft. A lookup that was pending at capture does not later change this saved record.</p>
    <details className="technical"><summary>Computer observation coverage</summary><Notes title="Collection limits" items={snapshot.limitations}/>{snapshot.errors.length > 0 && <Notes title="Unavailable details" items={snapshot.errors}/>}</details>
  </section>;
}
