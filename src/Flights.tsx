import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { open } from '@tauri-apps/plugin-dialog';
import { errorMessage, request, type EngineJob, type Run } from './api';
import type { FlightLibrary, LibraryFlight, LibraryFlightDetail, LibraryJobResult } from './flightLibraryApi';
import FlightReview, { flightDate } from './FlightReview';
import { useEngineJobs, jobActive } from './useEngineJobs';
import { Badge, Empty, Icon, Notes, number } from './ui';
import './flight-workspace.css';

interface FlightsProps {
  ready: boolean;
  active: boolean;
  busy: string;
  run: Run;
  onSetup: () => void;
  collectionControls?: ReactNode;
  advancedTools?: ReactNode;
  collectionActive?: boolean;
  refreshToken?: number;
}
interface ImportSelection { paths: string[]; aircraft: string; synthetic: boolean }
interface QueuedImport { path: string; aircraft: string; synthetic: boolean }
const basename = (path: string) => path.split(/[\\/]/).pop() || path;
const statusLabel = (flight: LibraryFlight) => flight.status === 'ready' ? 'Analyzed' : flight.status === 'awaiting_log' ? 'Log needed' : flight.status === 'needs_review' ? 'Review needed' : 'Analysis pending';
const kindLabel = (flight: LibraryFlight) => flight.kind === 'observed_flight' ? 'Observed flight' : flight.kind === 'log_interval' ? 'Recorded armed interval' : 'Log segment · flight boundaries unverified';

export default function Flights({ ready, active, busy, run, onSetup, collectionControls, advancedTools, collectionActive = false, refreshToken = 0 }: FlightsProps) {
  const [library, setLibrary] = useState<FlightLibrary | null>(null);
  const [selectedId, setSelectedId] = useState('');
  const [detail, setDetail] = useState<LibraryFlightDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState('');
  const [detailError, setDetailError] = useState('');
  const [query, setQuery] = useState('');
  const [period, setPeriod] = useState('all');
  const [importSelection, setImportSelection] = useState<ImportSelection | null>(null);
  const [importQueue, setImportQueue] = useState<QueuedImport[]>([]);
  const [queueing, setQueueing] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [autoPaused, setAutoPaused] = useState(false);
  const [blockedForCollection, setBlockedForCollection] = useState(false);
  const [analysisRevision, setAnalysisRevision] = useState(0);
  const [notice, setNotice] = useState('');
  const mounted = useRef(true);
  const listGeneration = useRef(0);
  const detailGeneration = useRef(0);
  const selected = useRef(selectedId); selected.current = selectedId;
  const refreshCount = useRef(0);
  const ownedRefreshes = useRef(new Set<string>());
  const ownedImports = useRef(new Map<string, QueuedImport>());
  const importBusy = useRef(false);
  const refreshBusy = useRef(false);
  const pageSize = useRef(50);

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; listGeneration.current++; detailGeneration.current++; }; }, []);

  const loadLibrary = useCallback(async (more = false) => {
    const generation = ++listGeneration.current;
    const limit = 50;
    const pages = more ? Math.ceil(pageSize.current / limit) + 1 : Math.ceil(pageSize.current / limit);
    let next: number | null = 0;
    let result: FlightLibrary | null = null;
    const flights: LibraryFlight[] = [];
    for (let page = 0; page < pages && next !== null; page++) {
      const response: FlightLibrary = await request<FlightLibrary>('library.list', { offset: next, limit });
      if (!mounted.current || generation !== listGeneration.current) return;
      if (!Array.isArray(response.flights)) throw new Error('The engine returned an invalid flight library.');
      flights.push(...response.flights);
      if (response.next_offset !== null && response.next_offset <= next) throw new Error('Flight history could not be paged safely. Refresh the library to try again.');
      next = response.next_offset;
      result = response;
    }
    if (!result || !mounted.current || generation !== listGeneration.current) return;
    const unique = [...new Map(flights.map(flight => [flight.id, flight])).values()];
    pageSize.current = Math.max(50, unique.length);
    setLibrary({ ...result, flights: unique, next_offset: next });
    setSelectedId(previous => unique.some(flight => flight.id === previous) ? previous : unique[0]?.id || '');
    setError('');
  }, []);

  const jobs = useEngineJobs(ready, ['library.import', 'library.refresh', 'library.analyze'], completed => {
    for (const job of completed) {
      if (job.method === 'library.import') {
        ownedImports.current.delete(job.id);
        setNotice('Flight logs imported. Available measurements and comparisons are attached to their saved intervals.');
      }
      if (ownedRefreshes.current.delete(job.id) || job.method === 'library.import') {
        const result = job.result as LibraryJobResult | undefined;
        const pending = result?.pending_logs ?? result?.summary?.pending_logs ?? 0;
        if (pending > 0 && ((result?.indexed_logs ?? 0) + (result?.analyzed_flights ?? 0) + (result?.compared_flights ?? 0) + (result?.alignment_attempts ?? 0) > 0 || job.method === 'library.import')) {
          if (refreshCount.current < 20) setAutoRefresh(true);
          else setAutoPaused(true);
        }
      }
    }
    void loadLibrary().then(() => { if (mounted.current) setAnalysisRevision(value => value + 1); }).catch(cause => { if (mounted.current) setError(errorMessage(cause)); });
  });
  const pending = jobs.jobs.filter(jobActive);
  const disabled = !ready || Boolean(busy) || queueing;

  useEffect(() => {
    if (!ready || !active) return;
    let current = true; setLoading(true); setAutoRefresh(true);
    void loadLibrary().catch(cause => { if (current) setError(errorMessage(cause)); }).finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [ready, active, refreshToken, loadLibrary]);

  useEffect(() => {
    if (!ready) { setDetail(null); setAutoRefresh(true); setBlockedForCollection(false); refreshCount.current = 0; return; }
  }, [ready]);

  useEffect(() => { if (!collectionActive) { setAutoRefresh(true); setBlockedForCollection(false); } }, [collectionActive]);

  useEffect(() => {
    for (const job of jobs.jobs) {
      if (job.state !== 'cancelled' && job.state !== 'interrupted') continue;
      const interrupted = ownedImports.current.get(job.id);
      if (!interrupted) continue;
      ownedImports.current.delete(job.id);
      setImportQueue(items => [interrupted, ...items]);
      setNotice('An interrupted log import is queued to resume. Preserved originals are reused.');
    }
  }, [jobs.jobs]);

  useEffect(() => {
    if (!ready || !active || !selectedId) return;
    const generation = ++detailGeneration.current;
    setDetailLoading(true); setDetailError('');
    if (detail?.flight.id !== selectedId) setDetail(null);
    void request<LibraryFlightDetail>('library.detail', { flight_id: selectedId }).then(response => {
      if (mounted.current && generation === detailGeneration.current && selected.current === selectedId) {
        setDetail(response);
        if (response.comparison_pending && refreshCount.current < 20) setAutoRefresh(true);
      }
    }).catch(cause => { if (mounted.current && generation === detailGeneration.current) setDetailError(errorMessage(cause)); }).finally(() => { if (mounted.current && generation === detailGeneration.current) setDetailLoading(false); });
    return () => { detailGeneration.current++; };
  }, [ready, active, selectedId, analysisRevision, refreshToken]);

  useEffect(() => {
    if (!ready || !active || !collectionActive) return;
    let current = true;
    const timer = setInterval(() => {
      void loadLibrary().catch(cause => { if (current) setError(errorMessage(cause)); });
    }, 5000);
    return () => { current = false; clearInterval(timer); };
  }, [ready, active, collectionActive, loadLibrary]);

  useEffect(() => {
    if (!ready || !active || !jobs.available || jobs.pending || queueing || importQueue.length || !autoRefresh || collectionActive || blockedForCollection || refreshBusy.current || importBusy.current) return;
    refreshBusy.current = true; setQueueing(true); setAutoRefresh(false);
    void request<{ job: EngineJob }>('library.refresh', { max_logs: 10 }).then(result => {
      if (!mounted.current) return;
      ownedRefreshes.current.add(result.job.id); refreshCount.current++; jobs.queue(result.job); setAutoPaused(false);
    }).catch(cause => { if (mounted.current) setError(`Automatic analysis could not start: ${errorMessage(cause)}`); }).finally(() => { refreshBusy.current = false; if (mounted.current) setQueueing(false); });
  }, [ready, active, jobs.available, jobs.pending, queueing, importQueue.length, autoRefresh, collectionActive, blockedForCollection, jobs.queue]);

  useEffect(() => {
    if (!ready || !jobs.available || jobs.pending || queueing || !importQueue.length || collectionActive || blockedForCollection || importBusy.current || refreshBusy.current) return;
    importBusy.current = true; setQueueing(true);
    const next = importQueue[0];
    setImportQueue(items => items.slice(1));
    void request<{ job: EngineJob }>('library.import', { path: next.path, label: basename(next.path), synthetic: next.synthetic, ...(next.aircraft ? { aircraft_id: next.aircraft } : {}) }).then(result => {
      if (!mounted.current) return;
      ownedImports.current.set(result.job.id, next);
      jobs.queue(result.job); setNotice(`Importing ${basename(next.path)}. Performance analysis follows automatically.`);
    }).catch(cause => {
      if (!mounted.current) return;
      const message = errorMessage(cause);
      if (/analysis resumes after collection stops/i.test(message)) { setImportQueue(items => [next, ...items]); setBlockedForCollection(true); }
      else setError(`Could not import ${basename(next.path)}: ${message}`);
    }).finally(() => { importBusy.current = false; if (mounted.current) setQueueing(false); });
  }, [ready, jobs.available, jobs.pending, queueing, importQueue, collectionActive, blockedForCollection, jobs.queue]);

  async function chooseLogs() {
    await run('Choosing flight logs', async () => {
      const paths = await open({ multiple: true, directory: false, title: 'Add flight logs', filters: [{ name: 'Flight logs', extensions: ['bin', 'BIN', 'log', 'LOG', 'tlog', 'TLOG'] }] });
      if (!paths || !mounted.current) return;
      setImportSelection({ paths: Array.isArray(paths) ? paths : [paths], aircraft: '', synthetic: false });
    });
  }
  function importLogs(event: FormEvent) {
    event.preventDefault();
    if (!importSelection) return;
    setImportQueue(previous => [...previous, ...importSelection.paths.map(path => ({ path, aircraft: importSelection.aircraft.trim(), synthetic: importSelection.synthetic }))]);
    setImportSelection(null); setNotice('Logs queued for local import and analysis.'); setError('');
  }
  async function updateContext(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!detail) return;
    const form = new FormData(event.currentTarget);
    const aircraft = String(form.get('aircraft_id') || '').trim();
    const battery = String(form.get('battery_id') || '').trim();
    await run('Updating this flight’s analysis', async () => {
      const result = await request<{ job: EngineJob }>('library.analyze', { flight_id: detail.flight.id, aircraft_id: aircraft, ...(battery ? { battery_id: battery } : {}) });
      jobs.queue(result.job); setNotice('This flight’s analysis is updating.');
    });
  }
  function refresh() {
    refreshCount.current = 0; setAutoRefresh(true); setAutoPaused(false);
    void loadLibrary().catch(cause => setError(errorMessage(cause)));
  }

  const summary = library?.summary;
  const filtered = (library?.flights || []).filter(flight => {
    const text = `${flight.label} ${flight.aircraft.label} ${flight.source_key || ''}`.toLowerCase();
    if (!text.includes(query.toLowerCase().trim())) return false;
    if (period === 'recent') return flight.started_at != null && summary?.period_start != null && summary?.period_end != null && Date.parse(flight.started_at) >= Date.parse(summary.period_start) && Date.parse(flight.started_at) <= Date.parse(summary.period_end);
    return true;
  });
  const dated = filtered.filter(flight => flight.started_at && Number.isFinite(Date.parse(flight.started_at))).sort((a, b) => Date.parse(b.started_at!) - Date.parse(a.started_at!) || a.id.localeCompare(b.id));
  const undated = filtered.filter(flight => !flight.started_at || !Number.isFinite(Date.parse(flight.started_at)));
  const recentFailures = jobs.jobs.filter(job => job.state === 'failed' || job.state === 'interrupted').slice(0, 3);

  return <div className="stack flights-workspace">
    <header className="page-title flight-page-title"><div><span className="eyebrow">YOUR FLIGHT HISTORY</span><h1>Flights</h1><p>Every flight, its configuration, and what to improve next.</p></div><button type="button" className="button primary" disabled={disabled || collectionActive} onClick={chooseLogs}><Icon name="upload"/>Import flight logs</button></header>
    {collectionControls}
    {importSelection && <section className="panel flight-import" aria-label="Import selected flight logs"><div className="panel-heading"><div><h2>Add {importSelection.paths.length === 1 ? 'a flight log' : `${importSelection.paths.length} flight logs`}</h2><p>Analysis and eligible comparisons are prepared automatically.</p></div><button type="button" className="icon-button" aria-label="Close log import" onClick={() => setImportSelection(null)}><Icon name="close"/></button></div><form className="padded" onSubmit={importLogs}><div className="flight-import-files">{importSelection.paths.map(path => <span key={path}><Icon name="evidence" size={16}/>{basename(path)}</span>)}</div><label>Aircraft name (optional)<input autoComplete="off" value={importSelection.aircraft} onChange={event => setImportSelection({ ...importSelection, aircraft: event.target.value })} placeholder="e.g. Survey UAV 01"/></label><p className="field-help">Use the same name only for logs from the same aircraft. This is your declaration for comparison; recorded hardware identity takes priority.</p><div className="import-footer"><label className="checkbox-label"><input type="checkbox" checked={importSelection.synthetic} onChange={event => setImportSelection({ ...importSelection, synthetic: event.target.checked })}/>These are synthetic test logs</label><button className="button primary" type="submit" disabled={disabled || collectionActive}>Import and analyze</button></div></form></section>}
    {(error || jobs.error) && <div role="alert"><Notes title="Flight history needs attention" items={[error || jobs.error]}/><button className="button secondary small" disabled={disabled} onClick={refresh}>Retry flight analysis</button></div>}
    {(pending.length > 0 || importQueue.length > 0 || notice) && <div className="flight-background" role="status"><div>{pending.length > 0 && <span className="spinner" aria-hidden="true"/>}<span>{pending.length > 0 ? `${pending[0].method === 'library.import' ? 'Importing and analyzing logs' : pending[0].method === 'library.analyze' ? 'Updating flight analysis' : 'Analyzing flight history'}${pending[0].progress?.fraction != null ? ` · ${Math.round(pending[0].progress.fraction * 100)}%` : ''}` : notice}{importQueue.length > 0 && ` · ${importQueue.length} log${importQueue.length === 1 ? '' : 's'} waiting`}</span></div>{pending.length > 0 && <button type="button" className="button secondary small" disabled={disabled || pending[0].cancel_requested} onClick={() => run('Stopping background analysis', async () => { setImportQueue([]); ownedImports.current.clear(); setAutoRefresh(false); await jobs.cancel(pending[0]); })}>{pending[0].cancel_requested ? 'Stopping…' : 'Stop analysis'}</button>}{!pending.length && !importQueue.length && notice && <button type="button" className="icon-button" aria-label="Dismiss import status" onClick={() => setNotice('')}><Icon name="close" size={16}/></button>}</div>}
    {recentFailures.length > 0 && <details className="flight-disclosure flight-job-errors"><summary>Analysis jobs needing attention · {recentFailures.length}</summary><div className="flight-disclosure-body">{recentFailures.map(job => <p key={job.id}>{job.error?.message || 'Analysis was interrupted before it finished.'}</p>)}<button type="button" className="button secondary small" disabled={disabled || jobs.pending} onClick={refresh}>Retry pending analysis</button></div></details>}
    {(autoPaused || collectionActive) && <div className="flight-background"><span>{collectionActive ? `Log imports and history analysis resume when live collection stops.${importQueue.length ? ` ${importQueue.length} selected logs remain queued.` : ''}` : 'More flight logs are ready for analysis.'}</span>{!collectionActive && <button type="button" className="button secondary small" disabled={disabled || jobs.pending} onClick={refresh}>Continue analysis</button>}</div>}
    {summary && summary.total > 0 && <div className="flight-library-summary" aria-label="Flight library summary"><div><strong>{summary.total}</strong><span>saved flight records &amp; intervals</span></div><div><strong>{summary.analyzed}</strong><span>analyzed</span></div><div><strong>{summary.recent_flights}</strong><span>in the latest 30 recorded days</span></div>{summary.unknown_dates > 0 && <div><strong>{summary.unknown_dates}</strong><span>without a recorded date</span></div>}</div>}
    {!library?.flights.length ? <section className="panel flight-empty-state">{loading ? <div className="empty" role="status"><span className="spinner"/><p>Loading your flight history…</p></div> : <Empty icon="compare" title="Your next insight starts with a flight log" action={<div className="button-row"><button type="button" className="button primary" disabled={disabled || collectionActive} onClick={chooseLogs}>Import your first log</button><button type="button" className="button secondary" onClick={onSetup}>Set up live collection</button></div>}>Add one flight or a month of logs. The app keeps each recorded interval with its measurements, configuration evidence, and comparisons as your history grows.</Empty>}<div className="flight-empty-steps"><span><b>1</b> Add your logs</span><span><b>2</b> Open a flight</span><span><b>3</b> Review changes and suggestions</span></div></section> : <div className="flight-workspace-grid">
      <section className="flight-history-panel panel" aria-label="Flight history"><div className="flight-history-heading"><h2>Flight history</h2><button type="button" className="icon-button" aria-label="Refresh flight history" disabled={disabled || jobs.pending} onClick={refresh}><Icon name="refresh" size={18}/></button></div><div className="flight-history-filters"><label className="sr-only" htmlFor="flight-search">Search flights</label><input id="flight-search" type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="Search flights or aircraft"/><label className="sr-only" htmlFor="flight-period">Flight history period</label><select id="flight-period" value={period} onChange={event => setPeriod(event.target.value)}><option value="all">All history · newest first</option><option value="recent">Latest 30 recorded days</option></select></div>
        {!filtered.length && <p className="inline-empty">No loaded flights match this view.{library.next_offset != null ? ' Load earlier flights to search more history.' : ''}</p>}
        {dated.map(flight => <FlightRow key={flight.id} flight={flight} selected={selectedId === flight.id} onSelect={() => setSelectedId(flight.id)}/>)}
        {undated.length > 0 && <><div className="flight-date-divider">Recorded date unavailable<small>Shown in import order</small></div>{undated.map(flight => <FlightRow key={flight.id} flight={flight} selected={selectedId === flight.id} onSelect={() => setSelectedId(flight.id)}/>)}</>}
        {library.next_offset != null && <div className="flight-history-more"><button type="button" className="button secondary small" disabled={disabled || loading} onClick={() => { setLoading(true); void loadLibrary(true).catch(cause => setError(errorMessage(cause))).finally(() => setLoading(false)); }}>Load earlier flights</button><small>{library.flights.length} of {library.total} loaded</small></div>}
      </section>
      <section className="flight-selected" aria-label="Selected flight insights" aria-busy={detailLoading}>
        {detailLoading && !detail && <div className="panel padded" role="status"><p>Loading this flight’s insights…</p></div>}
        {detailError && <div className="panel padded" role="alert"><Notes title="This flight could not be loaded" items={[detailError]}/><button className="button secondary small" onClick={() => setAnalysisRevision(value => value + 1)}>Retry this flight</button></div>}
        {active && detail && detail.flight.id === selectedId && <article className="flight-detail-card" key={detail.flight.id}><header className="flight-detail-heading"><div className="flight-detail-kicker"><span>{kindLabel(detail.flight)}</span>{detail.flight.synthetic && <Badge tone="warn">Synthetic</Badge>}</div><h2>{detail.flight.label}</h2><p>{flightDate(detail.flight)}{detail.flight.time_basis === 'receive_utc' && ' · receiver time'}</p><div className="flight-detail-meta"><span>{detail.flight.aircraft.label || 'Aircraft not identified'}</span><Badge tone={detail.flight.status === 'needs_review' ? 'warn' : 'blue'}>{statusLabel(detail.flight)}</Badge></div></header>
          {detail.review_reasons.length > 0 && <details className="flight-attention" open={!detail.report}><summary>Evidence needing attention · {detail.review_reasons.length}</summary><ul>{detail.review_reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul></details>}
          <FlightReview detail={detail}/>
          {detail.flight.log_id && <details className="flight-disclosure flight-context-editor"><summary>Flight context and analysis choices</summary><form className="flight-disclosure-body" onSubmit={updateContext}><p>Record context you know about this flight. This declaration does not replace recorded hardware identity or historical settings.</p><div className="form-grid"><label>Aircraft for comparison<input name="aircraft_id" defaultValue={/declared|operator/.test(detail.flight.aircraft.identity_basis) ? detail.flight.aircraft.id || '' : ''} placeholder="Aircraft name, if known"/></label>{Boolean(detail.flight.available_battery_ids?.length) && <label>Battery monitor<select name="battery_id" defaultValue={detail.flight.battery_id || detail.report?.scope.battery_id || ''} required={detail.flight.available_battery_ids!.length > 1}><option value="">{detail.flight.available_battery_ids!.length > 1 ? 'Choose the physical monitor' : 'Automatic single monitor'}</option>{detail.flight.available_battery_ids!.map(id => <option key={id} value={id}>{id}</option>)}</select></label>}</div><button type="submit" className="button secondary" disabled={disabled || jobs.pending || collectionActive}>Update flight analysis</button></form></details>}
        </article>}
      </section>
    </div>}
    {advancedTools}
    <p className="flight-local-note"><Icon name="check" size={15}/>Logs and analysis stay on this computer.</p>
  </div>;
}

function FlightRow({ flight, selected, onSelect }: { flight: LibraryFlight; selected: boolean; onSelect: () => void }) {
  const duration = flight.interval_start_s != null && flight.interval_end_s != null ? flight.interval_end_s - flight.interval_start_s : null;
  return <button type="button" className={`flight-history-row${selected ? ' selected' : ''}`} aria-pressed={selected} onClick={onSelect}><span className="flight-row-top"><span>{flightDate(flight, true)}</span>{flight.synthetic && <span className="flight-test-label">Test</span>}</span><strong>{flight.label}</strong><span>{flight.aircraft.label || 'Aircraft not identified'}{duration != null && duration >= 0 && ` · ${number(duration, 's')}`}</span><span className="flight-row-bottom"><span className={`flight-row-status ${flight.status === 'needs_review' || flight.status === 'awaiting_log' ? 'attention' : ''}`}>{statusLabel(flight)}</span>{flight.finding_count > 0 && <span>{flight.finding_count} finding{flight.finding_count === 1 ? '' : 's'}</span>}<Icon name="arrow" size={15}/></span></button>;
}
