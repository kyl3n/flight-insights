import { useCallback, useEffect, useState } from 'react';
import { getCurrentWindow } from '@tauri-apps/api/window';
import { confirm } from '@tauri-apps/plugin-dialog';
import { errorMessage, isDesktop, request, type CollectionStatus, type Session, type Snapshot } from './api';
import Network from './Network';
import Collection from './Collection';
import Compare from './Compare';
import Evidence from './Evidence';
import Insights from './Insights';
import { Badge, Icon } from './ui';

type Page = 'overview'|'collection'|'network'|'compare'|'evidence'|'insights';
const pages: {id:Page;label:string;icon:string}[] = [{id:'overview',label:'Overview',icon:'overview'},{id:'collection',label:'Prepare & watch',icon:'evidence'},{id:'network',label:'Network checks',icon:'network'},{id:'insights',label:'Flight insights',icon:'compare'},{id:'compare',label:'Compare',icon:'compare'},{id:'evidence',label:'Evidence',icon:'evidence'}];

function App() {
  const [page,setPage] = useState<Page>('overview'); const [ready,setReady] = useState(false);
  const [busy,setBusy] = useState(''); const [error,setError] = useState('');
  const [restartAcknowledged,setRestartAcknowledged] = useState(false); const [engineGeneration,setEngineGeneration] = useState(0);
  const [snapshots,setSnapshots] = useState<Snapshot[]>([]); const [sessions,setSessions] = useState<Session[]>([]);
  const [engineStatus,setEngineStatus] = useState<Record<string,unknown> | null>(null);
  const [collectionStatus,setCollectionStatus]=useState<CollectionStatus|null>(null);
  const observer=collectionStatus?.active_capture_session||collectionStatus?.capture_sessions.find(capture=>capture.status==='observing');
  useEffect(()=>{
    if(!observer||!isDesktop())return;
    // Browser integration tests have no native window. Production Tauri provides
    // this metadata and owns the default close action after its event handlers.
    const internals=(window as unknown as {__TAURI_INTERNALS__?:{metadata?:{currentWindow?:unknown}}}).__TAURI_INTERNALS__;
    if(!internals?.metadata?.currentWindow)return;
    let disposed=false;let unlisten:(()=>void)|undefined;
    void getCurrentWindow().onCloseRequested(async event=>{
      try{const close=await confirm('The app observer is recording. Closing this app stops its engine and recorder, leaving any open armed interval incomplete. Keep the app open to continue capture.',{title:'Stop recording and close?',kind:'warning',okLabel:'Close and stop recording',cancelLabel:'Keep recording'});if(!close)event.preventDefault();}
      catch{event.preventDefault();setError('The close confirmation could not open. Stop the app observer explicitly before closing.');}
    }).then(off=>{if(disposed)off();else unlisten=off;}).catch(()=>setError('The native close warning could not be registered. Keep the app open while recording; closing it stops collection.'));
    return()=>{disposed=true;unlisten?.();};
  },[observer?.uuid]);
  const refresh = useCallback(async () => {
    async function allPages<T>(method:string,key:string):Promise<T[]> {
      const all:T[] = []; let offset = 0;
      for (;;) {
        const page = await request<Record<string,unknown> & {next_offset?:number|null}>(method,{offset,limit:100});
        const items = page[key] as T[];
        all.push(...items);
        if (page.next_offset == null) return all;
        if (page.next_offset <= offset) throw new Error('The evidence catalog page could not fit in the engine response. No partial catalog is presented as complete.');
        offset = page.next_offset;
      }
    }
    const [parameters,flights] = await Promise.all([allPages<Snapshot>('parameters.list','snapshots'),allPages<Session>('sessions.list','sessions')]);
    setSnapshots(parameters); setSessions(flights);
  },[]);
  useEffect(()=>{if(ready&&(page==='evidence'||page==='compare'))void refresh().catch(cause=>setError(errorMessage(cause)));},[page,ready,refresh]);
  const run = useCallback(async (label:string,action:()=>Promise<void>) => {setError('');setBusy(label);try {await action();} catch (err) {setError(errorMessage(err));if (typeof err === 'object' && err && 'code' in err && /^(engine_(io|stopped|unavailable|crashed|start|timeout|failed|exited)|protocol_)/.test(String(err.code))) {setReady(false);setRestartAcknowledged(false);}} finally {setBusy('');}},[]);
  const connect = useCallback(async () => {await run('Connecting to local engine',async () => {setReady(false);await request('handshake');setEngineStatus(await request('engine.status'));await refresh();setReady(true);});},[refresh,run]);
  const restart = useCallback(async () => {await run('Restarting local engine',async () => {setReady(false);await request('engine.restart');setEngineGeneration(previous => previous+1);setRestartAcknowledged(false);setEngineStatus(await request('engine.status'));await refresh();setReady(true);});},[refresh,run]);
  useEffect(() => {if (isDesktop()) void connect();},[connect]);
  return <div className="app-shell"><a className="skip-link" href="#main-content">Skip to workspace</a>
    <aside className="sidebar"><a className="brand" href="#overview" onClick={e => {e.preventDefault();setPage('overview');}} aria-label="Q22 Flight Lab overview"><span className="brand-mark"><svg viewBox="0 0 40 40" fill="none" aria-hidden="true"><path d="m9 30 11-20 11 20-11-7z" stroke="currentColor" strokeWidth="2"/><path d="m20 10 0 13M9 30l11-7 11 7" stroke="currentColor" strokeWidth="2"/><circle cx="20" cy="10" r="3" fill="currentColor"/></svg></span><span><strong>Q22<span className="brand-dot">.</span></strong><small>FLIGHT LAB</small></span></a>
      <div className="sidebar-label">WORKSPACE</div><nav aria-label="Main navigation">{pages.map(item => <button key={item.id} aria-label={item.label} className={`nav-item ${page === item.id ? 'active' : ''}`} aria-current={page === item.id ? 'page' : undefined} onClick={() => setPage(item.id)}><Icon name={item.icon}/><span>{item.label}</span>{item.id === 'evidence' && snapshots.length + sessions.length > 0 && <span className="nav-count">{snapshots.length + sessions.length}</span>}</button>)}</nav>
      <div className="sidebar-bottom"><div className="local-mode"><span className="status-dot"/><div><strong>Local by design</strong><p>Your evidence stays here.</p></div></div><div className="sidebar-version">Milestone 01<span>ENGINEERING PREVIEW</span></div></div>
    </aside>
    <div className="workspace"><header className="topbar"><div className="context"><span className="context-icon"><Icon name="evidence" size={18}/></span><span>Q22 workspace</span><span className="context-separator">/</span><span className="muted">{pages.find(item => item.id === page)?.label}</span></div><div className="topbar-actions"><span className="local-tag"><span className="status-dot"/>Offline first</span><Badge tone={ready ? 'good' : 'neutral'}>{ready ? 'Engine connected' : isDesktop() ? 'Engine disconnected' : 'Browser preview'}</Badge><button className="button primary small" onClick={() => setPage('collection')}><Icon name="evidence" size={17}/>Prepare &amp; watch</button></div></header>
      <main id="main-content" tabIndex={-1}>
        {!ready && <div className="engine-banner"><Icon name="info"/><div><strong>{isDesktop() ? 'Local engine is not connected' : 'Open the desktop app to work with real evidence'}</strong><p>{isDesktop() ? 'Connect the packaged engine to import files and inspect this computer.' : 'This browser preview shows the workspace. Network inspection, local imports, and analysis require the installed desktop app.'}</p></div>{isDesktop() && <button className="button secondary small" disabled={Boolean(busy)} onClick={connect}>Connect engine</button>}</div>}
        {!ready && isDesktop() && <details className="restart-panel"><summary>Engine recovery</summary><p>Restart does not repeat the failed operation or undo host changes. The local repair journal will be checked again before rollback is available. Keep displayed commands for manual recovery if ownership cannot be verified. Archived evidence remains on disk.</p><label className="checkbox-label"><input type="checkbox" checked={restartAcknowledged} onChange={e => setRestartAcknowledged(e.target.checked)}/>I have saved any required rollback commands and will inspect the outcome before retrying.</label><button className="button secondary" disabled={Boolean(busy) || !restartAcknowledged} onClick={restart}>Restart analysis engine</button></details>}
        {observer&&<div className="observer-strip"><div><strong>{collectionStatus?.status_unavailable?'Observer status unavailable · keep app open':'App observer recording'}</strong><span>{observer.profile.aircraft_id} · {observer.watcher_state.replaceAll('_',' ')} · evidence {collectionStatus?.readiness.state}</span></div><button className="button secondary small" onClick={()=>setPage('collection')}>View collection</button></div>}
        {error && <div className="error-banner" role="alert"><Icon name="info"/><div><strong>Operation could not complete</strong><p>{error}</p></div><button className="icon-button" aria-label="Dismiss error" onClick={() => setError('')}><Icon name="close"/></button></div>}
        {busy && <div className="working-status" role="status" aria-live="polite"><span className="spinner"/>{busy}…</div>}
        <div hidden={page !== 'overview'}><Overview ready={ready} snapshots={snapshots} sessions={sessions} navigate={setPage} engineStatus={engineStatus}/></div>
        <div hidden={page !== 'collection'}><Collection ready={ready} busy={busy} run={run} onStatus={setCollectionStatus} onEvidence={()=>setPage('evidence')}/></div>
        <div hidden={page !== 'network'}><Network ready={ready} busy={busy} run={run} engineGeneration={engineGeneration}/></div>
        <div hidden={page !== 'insights'}><Insights ready={ready} active={page==='insights'} busy={busy} run={run} onImport={()=>setPage('evidence')}/></div>
        <div hidden={page !== 'compare'}><Compare ready={ready} busy={busy} run={run} snapshots={snapshots} sessions={sessions} onImport={() => setPage('evidence')}/></div>
        <div hidden={page !== 'evidence'}><Evidence ready={ready} busy={busy} run={run} snapshots={snapshots} sessions={sessions} refresh={refresh} onCollection={()=>setPage('collection')}/></div>
        <footer className="workspace-footer"><span>Q22 Flight Lab · Local engineering workspace</span><span>Milestone build · Hardware validation pending</span></footer>
      </main>
    </div>
  </div>;
}

function Overview({ready,snapshots,sessions,navigate,engineStatus}: {ready:boolean;snapshots:Snapshot[];sessions:Session[];navigate:(page:Page)=>void;engineStatus:Record<string,unknown>|null}) {
  return <div className="stack"><div className="page-title"><div><span className="eyebrow">FLIGHT ENGINEERING, WITH EVIDENCE</span><h1>Your flight workspace</h1><p>Connect your sources. Preserve the configuration. Understand the next flight.</p></div><Badge>Observe · preserve · compare</Badge></div>
    <section className="overview-hero"><div className="hero-copy"><div className="hero-kicker"><span className="short-rule"/>START WITH WHAT YOU KNOW</div><h2>Better decisions begin<br/>with the right evidence.</h2><p>Bring network health, flight performance, and ArduPilot settings into one local workspace. Every result begins with your actual sources.</p><button className="button primary" onClick={() => navigate('collection')}>Prepare &amp; watch<Icon name="arrow"/></button></div><div className="hero-diagram" aria-label="Workflow: source evidence, associate flight and parameters, then compare" role="img"><div className="diagram-orbit orbit-one"/><div className="diagram-orbit orbit-two"/><div className="diagram-core"><svg viewBox="0 0 80 80" fill="none" aria-hidden="true"><path d="m25 25 30 30m0-30L25 55" stroke="currentColor" strokeWidth="4"/><rect x="30" y="30" width="20" height="20" rx="6" stroke="currentColor" strokeWidth="2"/><circle cx="22" cy="22" r="13" stroke="currentColor" strokeWidth="2"/><circle cx="58" cy="22" r="13" stroke="currentColor" strokeWidth="2"/><circle cx="22" cy="58" r="13" stroke="currentColor" strokeWidth="2"/><circle cx="58" cy="58" r="13" stroke="currentColor" strokeWidth="2"/></svg><span>Q22</span></div><div className="diagram-node node-one"><span><Icon name="network"/></span>Sources</div><div className="diagram-node node-two"><span><Icon name="evidence"/></span>Configuration</div><div className="diagram-node node-three"><span><Icon name="compare"/></span>Flight evidence</div><span className="diagram-caption">CONNECTED EVIDENCE, CLEARER CONTEXT</span></div></section>
    <section className="workspace-status" aria-label="Workspace evidence status"><div><span className="status-icon"><Icon name="network"/></span><div><span>Local analysis engine</span><strong>{ready ? 'Connected' : 'Not connected'}</strong></div><Badge tone={ready ? 'good' : 'neutral'}>{ready ? 'Ready' : 'Unavailable'}</Badge></div><div><span className="status-icon"><Icon name="evidence"/></span><div><span>Parameter snapshots</span><strong>{snapshots.length ? `${snapshots.length} imported` : 'Awaiting import'}</strong></div></div><div><span className="status-icon"><Icon name="compare"/></span><div><span>Flight evidence</span><strong>{sessions.length ? `${sessions.length} sessions imported` : 'Awaiting import'}</strong></div></div></section>
    <div className="section-title"><h2>Move from setup to understanding</h2><span>Two connected workflows</span></div>
    <div className="two-column"><button className="workflow-card" onClick={() => navigate('network')}><div className="workflow-top"><span className="workflow-icon"><Icon name="network" size={25}/></span><span className="step-number">01</span></div><h3>Get the right streams<br/>to this computer</h3><p>Check adapters, subnet routes, digiQ reachability, camera access, and arriving UDP data. Review scoped repair and recovery instructions.</p><span className="workflow-link">Open network checks<Icon name="arrow" size={19}/></span></button><button className="workflow-card" onClick={() => navigate('compare')}><div className="workflow-top"><span className="workflow-icon green"><Icon name="compare" size={25}/></span><span className="step-number">02</span></div><h3>Connect setting changes<br/>with flight performance</h3><p>Compare immutable parameter snapshots alongside measured flight differences. Keep configuration evidence and confounding conditions visible.</p><span className="workflow-link">Open comparison workspace<Icon name="arrow" size={19}/></span></button></div>
    <section className="principles-strip"><div><Icon name="check"/><span>Originals preserved</span></div><div><Icon name="check"/><span>No aircraft parameter writes</span></div><div><Icon name="check"/><span>Unknown stays unknown</span></div></section>
    <details className="panel milestone-details"><summary>About this milestone and available capabilities</summary><div className="padded"><p>This milestone provides host network diagnostics and repair recovery, parameter imports and diffs, and scoped performance comparisons. The Flight log workspace checks the installed engine’s native log adapters before enabling import. Prepare & watch adds bounded passive telemetry recording and provisional armed-interval bundles when supported by the installed engine. Explicit read-only parameter and onboard log acquisition use a separate bidirectional path and require fresh disarmed identity evidence. Folder scans produce provisional candidates. Flight insights preserve scoped measurements and versioned guidance; automatic flight reconciliation, exact-build tuning validation, and hardware qualification remain pending.</p><p>Camera reachability and UDP packet observations do not establish full decoded stream quality. All comparisons remain descriptive; configuration changes are never sent to the aircraft.</p>{engineStatus && <details className="technical"><summary>Local engine capabilities</summary><pre>{JSON.stringify(engineStatus,null,2)}</pre></details>}</div></details>
  </div>;
}
export default App;
