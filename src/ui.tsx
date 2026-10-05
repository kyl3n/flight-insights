import type { ReactNode } from 'react';
import type { Metric, Session } from './api';

export function Icon({name, size = 20}: {name: string; size?: number}) {
  const paths: Record<string, ReactNode> = {
    overview: <><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></>,
    network: <><rect x="8" y="3" width="8" height="6" rx="1.5"/><rect x="2" y="16" width="7" height="5" rx="1.5"/><rect x="15" y="16" width="7" height="5" rx="1.5"/><path d="M12 9v4M5.5 16v-3h13v3"/></>,
    compare: <><path d="M4 4v16h16M8 16V9M13 16V5M18 16v-4"/><path d="m7 7 6-4 6 5"/></>,
    evidence: <><path d="M14 3H5v18h14V8zM14 3v5h5M8 12h8M8 16h6"/></>,
    upload: <><path d="M12 16V3m-5 5 5-5 5 5M4 15v6h16v-6"/></>,
    arrow: <path d="M4 12h16m-6-6 6 6-6 6"/>,
    check: <path d="m5 12 4 4L19 6"/>,
    info: <><circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7v1"/></>,
    refresh: <><path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 6a8 8 0 0 1 13 4M5 14a8 8 0 0 0 13 4"/></>,
    close: <path d="m6 6 12 12M18 6 6 18"/>,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] || paths.info}</svg>;
}
export function Badge({children, tone = 'neutral'}: {children: ReactNode; tone?: 'neutral'|'good'|'warn'|'bad'|'blue'}) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}
export function Empty({icon, title, children, action}: {icon: string; title: string; children: ReactNode; action?: ReactNode}) {
  return <div className="empty"><span className="empty-icon"><Icon name={icon} size={28}/></span><h3>{title}</h3><p>{children}</p>{action}</div>;
}
export function Notes({items, title = 'Evidence limits'}: {items?: string[]; title?: string}) {
  if (!items?.length) return null;
  return <div className="note"><Icon name="info"/><div><strong>{title}</strong><ul>{items.map((item, i) => <li key={i}>{item}</li>)}</ul></div></div>;
}
export function readable(value: string):string {
  const labels: Record<string,string> = {
    energy_wh:'Energy',mean_power_w:'Mean power',peak_power_w:'Peak power',roll_tracking_rmse_deg:'Roll tracking RMSE',
    aircraft_id:'Aircraft identity',board:'Controller board',firmware:'Firmware identity',mass_kg:'All-up mass',payload_kg:'Payload',
    battery_id:'Battery identity',battery_soc_pct:'Battery state of charge',battery_temperature_c:'Battery temperature',wind_mps:'Wind speed',
    hardware_revision:'Hardware revision',props:'Propeller configuration',parameter_evidence:'Parameter evidence',parameter_changes:'Parameter changes during interval',
    power_coverage:'Power data coverage',tracking_coverage:'Tracking data coverage',negative_current:'Current sign / calibration',selected_duration_s:'Selected interval duration',source_sha256:'Original source identity',
  };
  if (value.startsWith('before.')) return `Baseline ${readable(value.slice(7)).toLowerCase()}`;
  if (value.startsWith('after.')) return `Candidate ${readable(value.slice(6)).toLowerCase()}`;
  return labels[value] || value.replaceAll('_', ' ').replace(/^\w/, c => c.toUpperCase());
}
export function number(value: number | null | undefined, unit?: string): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return 'Unavailable';
  return `${new Intl.NumberFormat(undefined, {maximumFractionDigits: 4}).format(value)}${unit ? ` ${unit}` : ''}`;
}
export function stateTone(state: string): 'neutral'|'good'|'warn'|'bad'|'blue' {
  if (/^(ready|reachable|received|connected|observed|applied|rolled_back|available|ok)$/.test(state)) return 'good';
  if (/fail|error|blocked|unreachable/.test(state)) return 'bad';
  if (/unknown|partial|unsupported|unverified|confounded|timeout|uncertain|ownership|interrupted|absent/.test(state)) return 'warn';
  return 'neutral';
}
export function MetricDetails({metrics}: {metrics:Record<string,Metric>}) {
  return <div className="metric-details">{Object.entries(metrics).map(([key,metric]) => <details key={key}><summary><span>{readable(key)}</span><strong>{number(metric.value,metric.unit)}</strong></summary><dl className="metadata"><dt>Selected interval</dt><dd>{number(metric.interval_start_s,'s')} – {number(metric.interval_end_s,'s')}</dd><dt>Observed duration</dt><dd>{number(metric.covered_duration_s,'s')} ({number(metric.coverage_fraction == null ? null : metric.coverage_fraction*100,'%')})</dd><dt>Algorithm</dt><dd>{metric.algorithm_version || 'Unavailable'}</dd><dt>Source fields</dt><dd className="mono break-word">{metric.source_fields?.join(', ') || 'Unavailable'}</dd><dt>Valid sample pairs</dt><dd>{number(metric.valid_pair_count)}</dd><dt>Excluded intervals</dt><dd>{number(metric.exclusion_count)}</dd>{metric.log_id&&<><dt>Log source / clock segment</dt><dd>{metric.source_key} · segment {metric.boot_epoch}</dd><dt>Clock domain</dt><dd>{metric.time_domain||'Unavailable'}</dd><dt>Source SHA-256</dt><dd className="mono break-word">{metric.source_sha256||'Unavailable'}</dd></>}</dl>{metric.unavailable_reason&&<p className="metric-unavailable-reason"><strong>Unavailable:</strong> {metric.unavailable_reason}</p>}{metric.interpretation && <p>{metric.interpretation}</p>}{metric.uncertainty && <p>{metric.uncertainty}</p>}{Boolean(metric.exclusions?.length) && <ul className="metric-exclusions">{metric.exclusions!.map((exclusion,index) => <li key={index}>{number(exclusion.start_s,'s')} – {number(exclusion.end_s,'s')}: {exclusion.reason}</li>)}</ul>}{metric.exclusions_truncated && <p>Only the first 100 excluded intervals are listed; the exclusion count includes all intervals.</p>}</details>)}</div>;
}
export function ShellCommands({commands,empty}: {commands:string[][];empty:string}) {
  if (!commands.length) return <p className="field-help">{empty}</p>;
  return <div className="shell-commands">{commands.map((args,index) => {
    const powershell = /(?:^|[\\/])(?:powershell|pwsh)(?:\.exe)?$/i.test(args[0] || '');
    const commandIndex = args.findIndex(arg => arg.toLowerCase() === '-command');
    const embeddedScript = powershell && commandIndex >= 0 && args.length === commandIndex+2;
    // POSIX shell single-quote escaping preserves spaces, quotes and metacharacters.
    // For PowerShell, show the exact script instead of re-interpreting -Command args.
    const text = embeddedScript ? args[commandIndex+1] : args.map(arg => `'${arg.replaceAll("'", "'\"'\"'")}'`).join(' ');
    return <div key={index}><span className="field-help">{embeddedScript ? 'Run in PowerShell as administrator' : 'Run in a POSIX shell with the required privileges'}</span><pre>{text}</pre><details className="technical"><summary>Exact process arguments</summary><pre>{JSON.stringify(args,null,2)}</pre></details></div>;
  })}</div>;
}
export function SessionParameterEvidence({session}: {session:Session}) {
  if(session.parameter_evidence!=='logged_observations'&&!session.parameter_change_observations?.length)return null;
  return <details className="technical session-parameter-evidence"><summary>Logged configuration and split boundaries</summary><Notes title="Partial logged configuration" items={['This snapshot contains observed parameter values from the selected log source and clock segment. It is not a verified full readback; unobserved parameters and changes remain unknown.']}/>{Boolean(session.parameter_split_points_s?.length)&&<p className="field-help"><strong>Suggested interval boundaries:</strong> {session.parameter_split_points_s!.map(time=>number(time,'s')).join(', ')}. Create separately scoped sessions at these observations before attributing a performance difference to a configuration.</p>}{Boolean(session.parameter_change_observations?.length)&&<div className="table-scroll"><table><caption className="sr-only">Parameter observations inside the selected flight interval</caption><thead><tr><th>Time</th><th>Parameter</th><th>Before</th><th>Observed value</th><th>Observation</th></tr></thead><tbody>{session.parameter_change_observations!.map((item,index)=><tr key={`${item.name}-${index}`}><td>{number(item.time_s,'s')}</td><td className="mono">{item.name}</td><td>{item.before==null?'Not observed':String(item.before)}</td><td>{item.after==null?'Unavailable':String(item.after)}</td><td>{readable(item.kind)}</td></tr>)}</tbody></table></div>}{session.parameter_changes_truncated&&<p className="field-help">Only the first 1,000 change observations are displayed; {number(session.parameter_change_count)} were detected. The listed split boundaries are incomplete.</p>}</details>;
}
