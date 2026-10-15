import { lazy, Suspense, useCallback, useEffect, useState } from 'react';
import { getCurrentWindow } from '@tauri-apps/api/window';
import { confirm } from '@tauri-apps/plugin-dialog';
import { errorMessage, isDesktop, request, type CollectionStatus, type Session, type Snapshot } from './api';
import Collection from './Collection';
import ConnectionSetup from './ConnectionSetup';
import Flights from './Flights';
import { Badge, Icon } from './ui';
import './analysis-workflows.css';
import './workspace.css';

const Network = lazy(() => import('./Network'));
const Compare = lazy(() => import('./Compare'));
const Evidence = lazy(() => import('./Evidence'));
const Insights = lazy(() => import('./Insights'));
const Experiments = lazy(() => import('./Experiments'));
const Research = lazy(() => import('./Research'));

type Tool = 'evidence' | 'insights' | 'compare' | 'experiments' | 'research';
type Page = 'flights' | 'setup' | Tool;
const tools: {id: Tool; label: string; description: string}[] = [
  {id:'evidence', label:'Import & manage sources', description:'Parameter exports, original files, and evidence integrity.'},
  {id:'insights', label:'Manual analysis', description:'Choose a specific log interval, sensor, or filter model.'},
  {id:'compare', label:'Manual comparison', description:'Compare selected parameter snapshots and measured sessions.'},
  {id:'experiments', label:'Flight experiments', description:'Plan and review repeated flights with a specific goal.'},
  {id:'research', label:'Research library', description:'Read the technical references behind recommendations.'},
];

function App() {
  const [page,setPage] = useState<Page>('flights');
  const [visited,setVisited] = useState<Set<Tool>>(new Set());
  const [networkVisited,setNetworkVisited] = useState(false);
  const [ready,setReady] = useState(false);
  const [busy,setBusy] = useState('');
  const [error,setError] = useState('');
  const [restartAcknowledged,setRestartAcknowledged] = useState(false);
  const [engineGeneration,setEngineGeneration] = useState(0);
  const [snapshots,setSnapshots] = useState<Snapshot[]>([]);
  const [sessions,setSessions] = useState<Session[]>([]);
  const [collectionStatus,setCollectionStatus] = useState<CollectionStatus|null>(null);
  const [libraryRevision,setLibraryRevision] = useState(0);
  const observer = collectionStatus?.active_capture_session || collectionStatus?.capture_sessions.find(capture => capture.status === 'observing');

  const navigate = useCallback((next: Page) => {
    setPage(next);
    if (next !== 'flights' && next !== 'setup') setVisited(previous => new Set([...previous,next]));
    if (next === 'flights') setLibraryRevision(previous => previous + 1);
  },[]);

  useEffect(() => {
    if (!observer || !isDesktop()) return;
    const internals = (window as unknown as {__TAURI_INTERNALS__?:{metadata?:{currentWindow?:unknown}}}).__TAURI_INTERNALS__;
    if (!internals?.metadata?.currentWindow) return;
    let disposed = false;
    let unlisten: (() => void) | undefined;
    void getCurrentWindow().onCloseRequested(async event => {
      try {
        const close = await confirm('Flight collection is running. Closing the app stops recording and leaves any current flight incomplete.', {title:'Stop collecting and close?',kind:'warning',okLabel:'Close and stop recording',cancelLabel:'Keep recording'});
        if (!close) event.preventDefault();
      } catch {
        event.preventDefault();
        setError('The close confirmation could not open. Stop collection before closing.');
      }
    }).then(off => {if (disposed) off(); else unlisten = off;}).catch(() => setError('The close warning could not be registered. Keep the app open while collecting flights.'));
    return () => {disposed = true; unlisten?.();};
  },[observer?.uuid]);

  const refresh = useCallback(async () => {
    async function allPages<T>(method: string, key: string): Promise<T[]> {
      const all: T[] = [];
      let offset = 0;
      for (;;) {
        const result = await request<Record<string,unknown> & {next_offset?:number|null}>(method,{offset,limit:100});
        all.push(...result[key] as T[]);
        if (result.next_offset == null) return all;
        if (result.next_offset <= offset) throw new Error('The source list could not be loaded completely.');
        offset = result.next_offset;
      }
    }
    const [parameters,flights] = await Promise.all([allPages<Snapshot>('parameters.list','snapshots'),allPages<Session>('sessions.list','sessions')]);
    setSnapshots(parameters);
    setSessions(flights);
    setLibraryRevision(previous => previous + 1);
  },[]);
  useEffect(() => {if (ready && (page === 'evidence' || page === 'compare')) void refresh().catch(cause => setError(errorMessage(cause)));},[page,ready,refresh]);

  const run = useCallback(async (label: string, action: () => Promise<void>) => {
    setError(''); setBusy(label);
    try {await action();}
    catch (err) {
      setError(errorMessage(err));
      if (typeof err === 'object' && err && 'code' in err && /^(engine_(io|stopped|unavailable|crashed|start|timeout|failed|exited)|protocol_)/.test(String(err.code))) {
        setReady(false); setRestartAcknowledged(false);
      }
    } finally {setBusy('');}
  },[]);
  const connect = useCallback(async () => {
    await run('Connecting to local engine',async () => {setReady(false); await request('handshake'); await refresh(); setReady(true);});
  },[refresh,run]);
  const restart = useCallback(async () => {
    await run('Restarting local engine',async () => {
      setReady(false); await request('engine.restart'); setEngineGeneration(previous => previous + 1);
      setRestartAcknowledged(false); await refresh(); setReady(true);
    });
  },[refresh,run]);
  useEffect(() => {if (isDesktop()) void connect();},[connect]);
  const openNetwork = () => {
    setNetworkVisited(true); navigate('setup');
    requestAnimationFrame(() => {
      const details = document.getElementById('connection-troubleshooting') as HTMLDetailsElement | null;
      if (details) {details.open = true; details.scrollIntoView({block:'start'});}
    });
  };
  const isTool = page !== 'flights' && page !== 'setup';
  const title = page === 'setup' ? 'Setup' : tools.find(tool => tool.id === page)?.label || 'Flights';
  const advancedTools = <details className="panel workspace-tools"><summary>Advanced tools</summary><div className="workspace-tools-grid">{tools.map(tool => <button key={tool.id} aria-label={tool.label} onClick={() => navigate(tool.id)}><strong>{tool.label}</strong><span>{tool.description}</span><Icon name="arrow" size={18}/></button>)}</div></details>;

  return <div className="app-shell flight-app"><a className="skip-link" href="#main-content">Skip to workspace</a>
    <aside className="sidebar">
      <a className="brand" href="#flights" onClick={event => {event.preventDefault(); navigate('flights');}} aria-label="Q22 Flight Lab flights"><span className="brand-mark"><svg viewBox="0 0 40 40" fill="none" aria-hidden="true"><path d="m9 30 11-20 11 20-11-7z" stroke="currentColor" strokeWidth="2"/><path d="m20 10 0 13M9 30l11-7 11 7" stroke="currentColor" strokeWidth="2"/><circle cx="20" cy="10" r="3" fill="currentColor"/></svg></span><span><strong>Q22<span className="brand-dot">.</span></strong><small>FLIGHT LAB</small></span></a>
      <div className="sidebar-label">YOUR WORKSPACE</div>
      <nav aria-label="Main navigation">{([{id:'flights',label:'Flights',icon:'evidence'},{id:'setup',label:'Setup',icon:'network'}] as const).map(item => <button key={item.id} className={`nav-item ${(item.id === 'flights' ? page !== 'setup' : page === 'setup') ? 'active' : ''}`} aria-current={(item.id === 'flights' ? page !== 'setup' : page === 'setup') ? 'page' : undefined} onClick={() => navigate(item.id)}><Icon name={item.icon}/><span>{item.label}</span></button>)}</nav>
      <div className="sidebar-purpose">Every flight.<br/>A clearer picture.</div>
      <div className="sidebar-bottom"><div className="local-mode"><span className="status-dot"/><div><strong>Stored on this computer</strong><p>Your flights and original logs.</p></div></div><div className="sidebar-version">Q22 Flight Lab<span>FLIGHT PERFORMANCE ANALYSIS</span></div></div>
    </aside>
    <div className="workspace"><header className="topbar"><div className="context"><span className="context-icon"><Icon name="evidence" size={18}/></span><span>Flight Lab</span><span className="context-separator">/</span><span className="muted">{title}</span></div><div className="topbar-actions"><span className="local-tag"><span className="status-dot"/>Local workspace</span><Badge tone={ready ? 'good' : 'neutral'}>{ready ? 'Engine connected' : isDesktop() ? 'Engine disconnected' : 'Browser preview'}</Badge></div></header>
      <main id="main-content" tabIndex={-1}>
        {!ready && <div className="engine-banner"><Icon name="info"/><div><strong>{isDesktop() ? 'Connecting to your flight data' : 'Open the desktop app to analyze your flights'}</strong><p>{isDesktop() ? 'Your local engine loads and analyzes the logs on this computer.' : 'Flight import and live collection are available in the installed app.'}</p></div>{isDesktop() && <button className="button secondary small" disabled={Boolean(busy)} onClick={connect}>Connect engine</button>}</div>}
        {!ready && isDesktop() && <details className="restart-panel"><summary>Engine recovery</summary><p>Restart does not repeat the failed operation. Saved flight data remains on disk. Save any network rollback commands before restarting.</p><label className="checkbox-label"><input type="checkbox" checked={restartAcknowledged} onChange={event => setRestartAcknowledged(event.target.checked)}/>I have saved any required rollback commands and will inspect the outcome before retrying.</label><button className="button secondary" disabled={Boolean(busy) || !restartAcknowledged} onClick={restart}>Restart analysis engine</button></details>}
        {observer && page !== 'flights' && <div className="observer-strip"><div><strong>{collectionStatus?.status_unavailable ? 'Collection status unavailable · keep app open' : 'Collecting flight data'}</strong><span>{observer.profile.aircraft_id} · {observer.watcher_state.replaceAll('_',' ')}</span></div><button className="button secondary small" onClick={() => navigate('flights')}>View collection</button></div>}
        {error && <div className="error-banner" role="alert"><Icon name="info"/><div><strong>Operation could not complete</strong><p>{error}</p></div><button className="icon-button" aria-label="Dismiss error" onClick={() => setError('')}><Icon name="close"/></button></div>}
        {busy && <div className="working-status" role="status" aria-live="polite"><span className="spinner"/>{busy}…</div>}
        <div hidden={page !== 'flights'}><Flights ready={ready} active={page === 'flights'} busy={busy} run={run} onSetup={() => navigate('setup')} refreshToken={libraryRevision} collectionActive={Boolean(observer)} collectionControls={<Collection compact ready={ready} busy={busy} run={run} onStatus={setCollectionStatus} onEvidence={() => navigate('evidence')} onSetup={() => navigate('setup')}/>} advancedTools={advancedTools}/></div>
        {page === 'setup' && <div className="stack"><ConnectionSetup ready={ready} observerActive={Boolean(observer)} onCollection={() => navigate('flights')} onNetwork={openNetwork} onEvidence={() => navigate('flights')}/><details id="connection-troubleshooting" className="panel workspace-tools" onToggle={event => {if (event.currentTarget.open) setNetworkVisited(true);}}><summary>Connection troubleshooting</summary>{networkVisited && <Suspense fallback={<p className="padded">Loading connection tools…</p>}><Network ready={ready} busy={busy} run={run} engineGeneration={engineGeneration} onSetup={() => navigate('setup')}/></Suspense>}</details></div>}
        {isTool && <div className="workspace-back"><button className="button secondary small" onClick={() => navigate('flights')}>← Back to flights</button><span>Advanced tools · {title}</span></div>}
        <Suspense fallback={isTool ? <div className="working-status" role="status">Loading tools…</div> : null}>
          {visited.has('evidence') && <div hidden={page !== 'evidence'}><Evidence ready={ready} busy={busy} run={run} snapshots={snapshots} sessions={sessions} refresh={refresh} onCollection={() => navigate('flights')}/></div>}
          {visited.has('insights') && <div hidden={page !== 'insights'}><Insights ready={ready} active={page === 'insights'} busy={busy} run={run} onImport={() => navigate('evidence')}/></div>}
          {visited.has('compare') && <div hidden={page !== 'compare'}><Compare ready={ready} busy={busy} run={run} snapshots={snapshots} sessions={sessions} onImport={() => navigate('evidence')}/></div>}
          {visited.has('experiments') && <div hidden={page !== 'experiments'}><Experiments ready={ready} busy={busy} run={run} onInsights={() => navigate('insights')}/></div>}
          {visited.has('research') && <div hidden={page !== 'research'}><Research ready={ready} busy={busy} run={run} observerActive={Boolean(observer)}/></div>}
        </Suspense>
        <footer className="workspace-footer"><span>Q22 Flight Lab · Flight performance analysis</span><span>Original logs preserved locally</span></footer>
      </main>
    </div>
  </div>;
}
export default App;
