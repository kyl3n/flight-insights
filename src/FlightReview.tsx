import { useState } from 'react';
import type { InsightReport } from './api';
import type { LibraryComparison, LibraryFlight, LibraryFlightDetail } from './flightLibraryApi';
import FlightConfiguration from './FlightConfiguration';
import { FlightEnvironmentSnapshot } from './FlightEnvironment';
import FlightHealth from './FlightHealth';
import { AdviceSources, ParameterExplanationCard, type PerformanceAdviceData } from './PerformanceAdvice';
import { ControlAnalysis, FilterModel, ModeledSpectrum, SpectralWindows } from './InsightAdvanced';
import { Badge, MetricDetails, Notes, number, readable } from './ui';

const valueText = (value: unknown) => value == null ? 'Unknown' : typeof value === 'object' ? JSON.stringify(value) : String(value);
export const metricName = (name: string) => {
  const vibration = /^vibration\.([^.]+)\.([xyz])\.(p95_mps2|maximum_mps2)$/.exec(name);
  if (vibration) return `Vibration ${vibration[2].toUpperCase()} · ${vibration[3] === 'p95_mps2' ? '95th percentile' : 'maximum'} · sensor ${vibration[1]}`;
  const control = /^control\.(roll|pitch|yaw)\.tracking_rmse_deg_s$/.exec(name);
  if (control) return `${readable(control[1])} rate tracking error`;
  return readable(name);
};
export function flightDate(flight: LibraryFlight, compact = false) {
  if (!flight.started_at || !Number.isFinite(Date.parse(flight.started_at))) return 'Recorded date unavailable';
  return new Intl.DateTimeFormat(undefined, compact ? { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' } : { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', timeZoneName: 'short' }).format(new Date(flight.started_at));
}

export default function FlightReview({ detail }: { detail: LibraryFlightDetail }) {
  const { report, flight } = detail;
  return <>
    {detail.detail_omitted_reason && <div className="flight-attention"><p>{detail.detail_omitted_reason}</p><p>Use Manual analysis in Advanced tools to inspect a narrower interval.</p></div>}
    {report ? <>
      <Performance report={report}/>
      <Findings report={report}/>
      <AutomaticComparison detail={detail}/>
      <SuggestedSteps report={report}/>
      <RecentHistory flight={flight} history={detail.recent_history}/>
      <details className="flight-disclosure"><summary>Configuration and software</summary><div className="flight-disclosure-body">
        <FlightConfiguration configuration={report.configuration}/>
        {!report.configuration && <p>No complete preflight readback is linked to this flight.</p>}
        {(!detail.observed_context || report.flight_context?.environment_snapshot) && <section className="panel padded"><FlightEnvironmentSnapshot snapshot={report.flight_context?.environment_snapshot}/>{report.flight_context?.reason && <p className="field-help">{report.flight_context.reason}</p>}</section>}
        {detail.observed_context && !report.flight_context?.environment_snapshot && <section className="panel padded"><h3>Collection context</h3><p className="field-help">Saved when this app observed the armed interval. The original recording and interval boundaries link this observation to the collected telemetry.</p><FlightEnvironmentSnapshot snapshot={detail.observed_context.environment_snapshot}/><Notes title="Collection context coverage" items={detail.observed_context.limitations}/></section>}
        <details className="technical"><summary>Parameters observed in the log · {report.parameter_evidence.observations.length}</summary><p>These values describe recorded observations. Missing parameters and later changes remain unknown.</p><div className="table-scroll"><table><thead><tr><th>Parameter</th><th>Observed value</th><th>Age at interval start</th></tr></thead><tbody>{report.parameter_evidence.observations.map((item, index) => <tr key={`${item.name}-${index}`}><td className="mono">{item.name}</td><td>{valueText(item.value)}</td><td>{number(item.age_at_start_s, 's')}</td></tr>)}</tbody></table></div>{(report.parameter_evidence.observations_truncated || report.parameter_evidence.changes_truncated) && <p>The displayed parameter evidence is incomplete; full coverage is not established.</p>}<p className="field-help">{report.parameter_evidence.change_count} parameter change observations inside this interval.</p></details>
      </div></details>
      <details className="flight-disclosure"><summary>Detailed measurements and source evidence</summary><div className="flight-disclosure-body">
        <section className="panel padded"><h3>Measurement coverage</h3><MetricDetails metrics={Object.fromEntries(Object.entries(report.metrics).map(([key, metric]) => [key, { ...metric, algorithm_version: report.algorithm_version, interval_start_s: report.scope.interval_start_s, interval_end_s: report.scope.interval_end_s, log_id: report.scope.log_id, source_key: report.scope.source_key, boot_epoch: report.scope.boot_epoch, time_domain: report.scope.time_domain, source_sha256: report.scope.source_sha256 }]))}/></section>
        <VibrationObservations report={report}/>
        <FlightHealth health={report.health}/>
        <ControlAnalysis control={report.control}/><FilterModel model={report.filter_model}/><ModeledSpectrum value={report.modeled_spectrum}/>
        <section className="panel padded"><h3>Gyro spectrum</h3><p>{report.spectrum.reason || readable(report.spectrum.status)}</p><SpectralWindows spectrum={report.spectrum}/>{Boolean(report.spectrum.peaks?.length) && <div className="table-scroll"><table><thead><tr><th>Peak frequency</th><th>Recorded PSD</th></tr></thead><tbody>{report.spectrum.peaks!.map((peak, index) => <tr key={index}><td>{number(peak.frequency_hz, 'Hz')}</td><td>{number(peak.psd)}</td></tr>)}</tbody></table></div>}</section>
        <Notes title="Analysis coverage and limitations" items={[...report.warnings, ...report.limitations]}/>
        <details className="technical"><summary>Preserved report identity and exact scope</summary><pre>{JSON.stringify({ report_id: report.id, algorithm_version: report.algorithm_version, scope: report.scope, operating_profile: report.operating_profile }, null, 2)}</pre></details>
      </div></details>
    </> : <><section className="flight-review-section"><h3>{detail.detail_omitted_reason ? 'Saved analysis needs a narrower view' : flight.status === 'awaiting_log' ? 'Add the flight log to complete this flight' : flight.status === 'needs_review' ? 'This flight needs an analysis choice' : 'Analysis is being prepared'}</h3><p>{detail.detail_omitted_reason ? 'The full saved report exceeds the display limit for this combined view.' : flight.status === 'awaiting_log' ? 'The flight event is saved. Its onboard log supplies the detailed performance measurements.' : 'Available evidence will appear here after local analysis finishes. Any source or monitor choices that need your input are shown below.'}</p></section>{detail.observed_context && <details className="flight-disclosure"><summary>Collection computer and software</summary><div className="flight-disclosure-body"><p>Saved when this app observed the armed interval.</p><FlightEnvironmentSnapshot snapshot={detail.observed_context.environment_snapshot}/><Notes title="Collection context coverage" items={detail.observed_context.limitations}/></div></details>}</>}
  </>;
}

function Performance({ report }: { report: InsightReport }) {
  const available = Object.entries(report.metrics).filter(([, metric]) => metric.value != null && Number.isFinite(metric.value));
  const missing = Object.entries(report.metrics).filter(([, metric]) => metric.value == null || !Number.isFinite(metric.value));
  const vibration = report.vibration.distributions?.filter(item => Number.isFinite(item.p95_mps2)).sort((a, b) => b.p95_mps2 - a.p95_mps2)[0];
  return <section className="flight-review-section" aria-label="Flight performance"><div className="flight-section-heading"><h3>Performance</h3><span>{number(report.scope.interval_end_s - report.scope.interval_start_s, 's')} analyzed</span></div><div className="flight-metric-grid">
    {vibration && <article><span>Highest vibration · 95th percentile</span><strong>{number(vibration.p95_mps2, 'm/s²')}</strong><small>Sensor {vibration.instance_id} · {vibration.axis.toUpperCase()} axis · {number(vibration.covered_duration_s, 's')} covered</small></article>}
    {available.slice(0, vibration ? 5 : 6).map(([key, metric]) => <article key={key}><span>{metricName(key)}</span><strong>{number(metric.value, metric.unit)}</strong><small>{metric.coverage_fraction == null ? 'Coverage not established' : `${number(metric.coverage_fraction * 100, '%')} interval coverage`}</small></article>)}
  </div>{!available.length && !vibration && <p>No supported performance measurements were available in this interval.</p>}{missing.length > 0 && <details className="technical"><summary>{missing.length} measurement{missing.length === 1 ? '' : 's'} unavailable</summary>{missing.map(([key, metric]) => <p key={key}><strong>{metricName(key)}:</strong> {metric.unavailable_reason || 'The required samples were not recorded.'}</p>)}</details>}</section>;
}

function Findings({ report }: { report: InsightReport }) {
  const findings = report.findings.map(item => ({ id: item.id, title: item.title, detail: `${number(item.evidence.time_s, 's')} into this interval`, evidence: item.evidence, details: item.details }));
  const health = report.health?.findings || [];
  return <section className="flight-review-section" aria-label="Flight findings"><div className="flight-section-heading"><h3>Findings</h3><span>{report.finding_count + health.length} recorded observations</span></div>
    {!findings.length && !health.length && <p>No supported issue was identified in the available records. Check measurement coverage before drawing conclusions.</p>}
    {health.slice(0, 3).map(item => <article className="flight-finding" key={item.id}><span className="flight-finding-marker" aria-hidden="true"/><div><h4>{item.title}</h4><p>{item.explanation}</p><details className="technical"><summary>Checks and supporting observations</summary><Notes title="Recommended checks" items={item.recommended_checks}/><p>{item.observation_count} qualifying observations. Related settings: {item.parameter_families.join(', ') || 'No parameter family established'}.</p><pre>{JSON.stringify(item.evidence, null, 2)}</pre></details></div></article>)}
    {findings.slice(0, 4).map(item => <article className="flight-finding" key={item.id}><span className="flight-finding-marker" aria-hidden="true"/><div><h4>{item.title}</h4><p>{item.detail}</p><details className="technical"><summary>Supporting measurement</summary><pre>{JSON.stringify({ evidence: item.evidence, observed: item.details }, null, 2)}</pre></details></div></article>)}
    {(findings.length > 4 || health.length > 3) && <details className="technical"><summary>More findings · {Math.max(0, findings.length - 4) + Math.max(0, health.length - 3)}</summary>{health.slice(3).map(item => <article className="flight-extra-finding" key={item.id}><h4>{item.title}</h4><p>{item.explanation}</p><Notes title="Recommended checks" items={item.recommended_checks}/></article>)}{findings.slice(4).map(item => <article className="flight-extra-finding" key={item.id}><h4>{item.title}</h4><p>{item.detail}</p><pre>{JSON.stringify({ evidence: item.evidence, observed: item.details }, null, 2)}</pre></article>)}</details>}
    {report.findings_truncated && <p className="field-help">The report contains {report.findings.length} of {report.finding_count} detected findings. A narrower analysis interval is needed to inspect more events.</p>}
  </section>;
}

function SuggestedSteps({ report }: { report: InsightReport }) {
  const advice = report.performance_advice;
  const recommendations = advice?.recommendations || [];
  return <section className="flight-review-section" aria-label="Flight improvement suggestions"><div className="flight-section-heading"><h3>Suggested next steps</h3><span>For this flight’s evidence</span></div>
    {recommendations.length ? <>{recommendations.slice(0, 3).map((item, index) => <Suggestion key={item.id} item={item} index={index}/>)}{recommendations.length > 3 && <details className="technical"><summary>More suggestions ({recommendations.length - 3})</summary>{recommendations.slice(3).map((item, index) => <Suggestion key={item.id} item={item} index={index + 3}/>)}</details>}</> : <p>No parameter change is supported by the current evidence. Record the missing measurements before choosing a tuning change.</p>}
    {advice && <details className="technical"><summary>Recommendation sources and coverage</summary><p>Reviewed guidance: {advice.retrieved_at}. {advice.parameter_context?.complete_preflight_readback ? 'A complete typed preflight readback is available.' : 'Parameter context is limited to recorded observations.'}</p><Notes title="Recommendation limits" items={[...(advice.parameter_context?.warnings || []), ...advice.limitations]}/></details>}
  </section>;
}

function Suggestion({ item, index }: { item: PerformanceAdviceData['recommendations'][number]; index: number }) {
  return <article className="flight-suggestion" key={item.id}><div className="flight-suggestion-number" aria-hidden="true">{index + 1}</div><div><h4>{item.title}</h4><p>{item.candidate_action}</p><details className="technical"><summary>Why this could help and how to verify it</summary><p><strong>Observed:</strong> {item.symptom}</p><p>{item.mechanism}</p>{item.observed_parameters.length > 0 && <p className="mono break-word">{item.observed_parameters.map(parameter => `${parameter.name} = ${valueText(parameter.value)}`).join(' · ')}</p>}<Notes title="Evidence needed before changing settings" items={item.evidence_requirements}/><Notes title="Other explanations" items={item.alternative_hypotheses}/><Notes title="Tradeoffs" items={item.tradeoffs}/><h4>Check the result in a subsequent flight</h4><ol>{item.verification_experiment.map((step, stepIndex) => <li key={stepIndex}>{step}</li>)}</ol><p className="field-help">{item.firmware_scope.scope}</p><AdviceSources references={item.references}/></details></div></article>;
}

function AutomaticComparison({ detail }: { detail: LibraryFlightDetail }) {
  const result = detail.comparison;
  return <section className="flight-review-section" aria-label="Automatic flight comparison"><div className="flight-section-heading"><h3>What changed</h3>{result && <span>Compared automatically</span>}</div>
    {!result ? <p>{detail.comparison_reason || 'A previous flight with compatible identity and timing is needed for an automatic comparison.'}</p> : <>
      <p className="flight-comparison-reference">Previous: <strong>{detail.previous_flight?.label || 'Earlier flight'}</strong>{detail.previous_flight && <> · {flightDate(detail.previous_flight, true)}</>}</p>
      {detail.comparison_reason && <p className="field-help">{detail.comparison_reason}</p>}
      <div className="table-scroll flight-difference-table"><table><caption className="sr-only">Performance compared with the previous eligible flight</caption><thead><tr><th>Measurement</th><th>Previous</th><th>This flight</th><th>Change</th></tr></thead><tbody>{result.metrics.filter(item => item.before != null || item.after != null).slice(0, 8).map(metric => <tr key={metric.name}><td>{metricName(metric.name)}</td><td>{number(metric.before, metric.before_unit || undefined)}</td><td>{number(metric.after, metric.after_unit || undefined)}</td><td>{metric.delta == null ? 'Not comparable' : `${metric.delta > 0 ? '+' : ''}${number(metric.delta, metric.unit)}`}{metric.percent_change != null && <small>{metric.percent_change > 0 ? '+' : ''}{number(metric.percent_change, '%')}</small>}</td></tr>)}</tbody></table></div>
      <ParameterChanges comparison={result}/>
      {result.parameter_explanations.length > 0 && <details className="technical"><summary>Why the changed parameters may affect performance</summary><div className="flight-parameter-explanations">{result.parameter_explanations.map(item => <ParameterExplanationCard key={item.parameter} item={item}/>)}</div>{result.parameter_explanations_truncated && <p>Explanations cover a bounded subset of the changed parameters.</p>}</details>}
      {result.environment_diff.changes.length > 0 && <details className="technical"><summary>Computer and software changes · {result.environment_diff.changes.length}</summary><div className="table-scroll"><table><thead><tr><th>Context</th><th>Previous</th><th>This flight</th></tr></thead><tbody>{result.environment_diff.changes.map((change, index) => <tr key={index}><td>{readable(change.field)}<small>{readable(change.provenance)}</small></td><td>{valueText(change.before)}</td><td>{valueText(change.after)}</td></tr>)}</tbody></table></div></details>}
      <p className="field-help">These are observed differences. Flight conditions, hardware, and measurement coverage can also affect the result.</p>
      <details className="technical"><summary>Comparison coverage and assumptions</summary><Badge tone="warn">{readable(result.status)}</Badge><Notes title="What affects this comparison" items={[...result.confounders.map(item => item.detail), ...result.limitations, ...result.environment_diff.limitations]}/><div className="table-scroll"><table><thead><tr><th>Metric</th><th>Earlier coverage</th><th>Later coverage</th><th>Evidence</th></tr></thead><tbody>{result.metrics.map(metric => <tr key={metric.name}><td>{metricName(metric.name)}</td><td>{number(metric.before_coverage.coverage_fraction == null ? null : metric.before_coverage.coverage_fraction * 100, '%')}</td><td>{number(metric.after_coverage.coverage_fraction == null ? null : metric.after_coverage.coverage_fraction * 100, '%')}</td><td>{readable(metric.status)}<small>{metric.limitations.join(' ')}</small></td></tr>)}</tbody></table></div></details>
    </>}
  </section>;
}

function ParameterChanges({ comparison }: { comparison: LibraryComparison }) {
  const difference = comparison.parameter_diff;
  const full = difference.basis === 'complete_typed_preflight_readbacks';
  return <div className="flight-parameter-changes"><strong>{difference.counts.changed || 0} parameter changes {full ? 'in full preflight readbacks' : 'in shared log observations'}</strong><p>{full ? `${difference.counts.compared || 0} shared parameters compared, including recorded types.` : 'Values missing from either log remain unknown.'}</p>
    {(difference.changed.length > 0 || difference.before_only.length > 0 || difference.after_only.length > 0) && <details className="technical"><summary>Review configuration differences</summary><div className="table-scroll"><table><thead><tr><th>Parameter</th><th>Previous</th><th>This flight</th></tr></thead><tbody>{difference.changed.map(item => <tr key={item.name}><td className="mono">{item.name}{item.type_changed && <small>Recorded type also changed</small>}</td><td>{valueText(item.before)}</td><td>{valueText(item.after)}</td></tr>)}{difference.before_only.map(item => <tr key={`before-${item.name}`}><td className="mono">{item.name}</td><td>{valueText(item.value)}</td><td>{full ? 'Absent from readback' : 'Not observed'}</td></tr>)}{difference.after_only.map(item => <tr key={`after-${item.name}`}><td className="mono">{item.name}</td><td>{full ? 'Absent from readback' : 'Not observed'}</td><td>{valueText(item.value)}</td></tr>)}</tbody></table></div>{difference.truncated && <p>The displayed differences are limited. Counts may include additional changes.</p>}<Notes title="Configuration comparison limits" items={[...difference.limitations, ...(comparison.configuration_comparison?.reasons || [])]}/></details>}
  </div>;
}

function RecentHistory({ flight, history }: { flight: LibraryFlight; history: LibraryFlight[] }) {
  const [chosen, setChosen] = useState('');
  const rows = [...history.filter(item => item.id !== flight.id), flight].filter(item => item.started_at && Number.isFinite(Date.parse(item.started_at))).sort((a, b) => Date.parse(a.started_at!) - Date.parse(b.started_at!));
  if (rows.length < 2) return null;
  const keys = [...new Set(rows.flatMap(item => Object.keys(item.metrics)))].filter(key => rows.filter(item => item.metrics[key]?.value != null).length >= 2);
  const selected = keys.includes(chosen) ? chosen : keys[0];
  const units = [...new Set(rows.map(item => item.metrics[selected]?.unit).filter(Boolean))];
  const values = units.length === 1 ? rows.filter(item => item.metrics[selected]?.value != null && Number.isFinite(item.metrics[selected].value)) : [];
  if (!selected || values.length < 2) return null;
  const minimum = Math.min(...values.map(item => item.metrics[selected].value!));
  const maximum = Math.max(...values.map(item => item.metrics[selected].value!));
  const earliest = Date.parse(rows[0].started_at!);
  const latest = Date.parse(rows.at(-1)!.started_at!);
  const x = (item: LibraryFlight) => 56 + (Date.parse(item.started_at!) - earliest) / Math.max(latest - earliest, 1) * 584;
  const y = (item: LibraryFlight) => 128 - (item.metrics[selected].value! - minimum) / Math.max(maximum - minimum, 0.000001) * 88;
  return <section className="flight-review-section" aria-label="Recent flight performance history"><div className="flight-section-heading"><h3>Recent flight history</h3><span>Preceding 30 days</span></div><p>Recorded summaries for this aircraft’s comparison group.</p><label className="flight-trend-selector">History measurement<select value={selected} onChange={event => setChosen(event.target.value)}>{keys.map(key => <option key={key} value={key}>{metricName(key)}</option>)}</select></label><figure className="flight-trend"><svg viewBox="0 0 690 180" role="img" aria-label={`${metricName(selected)} across ${values.length} recorded intervals`}><line x1="56" x2="640" y1="140" y2="140" stroke="#d5deeb"/><line x1="56" x2="640" y1="40" y2="40" stroke="#edf0f5"/><text x="48" y="44" textAnchor="end">{number(maximum)}</text><text x="48" y="132" textAnchor="end">{number(minimum)}</text>{values.map(item => <circle key={item.id} cx={x(item)} cy={y(item)} r={item.id === flight.id ? 6 : 4} fill={item.id === flight.id ? '#245ede' : '#8da8cf'}><title>{item.label} · {flightDate(item)} · {number(item.metrics[selected].value, item.metrics[selected].unit)}</title></circle>)}<text x="56" y="165">{new Date(earliest).toLocaleDateString()}</text><text x="640" y="165" textAnchor="end">{new Date(latest).toLocaleDateString()}</text></svg><figcaption>{units[0]} · Each point is one interval. Different conditions and coverage can affect the values.</figcaption></figure><details className="technical"><summary>History values and coverage</summary><div className="table-scroll"><table><thead><tr><th>Flight</th><th>Recorded date</th><th>{metricName(selected)}</th><th>Coverage</th></tr></thead><tbody>{rows.map(item => <tr key={item.id}><td>{item.label}</td><td>{flightDate(item, true)}</td><td>{number(item.metrics[selected]?.value, item.metrics[selected]?.unit)}</td><td>{number(item.metrics[selected]?.coverage_fraction == null ? null : item.metrics[selected].coverage_fraction! * 100, '%')}</td></tr>)}</tbody></table></div></details></section>;
}

function VibrationObservations({ report }: { report: InsightReport }) {
  const [chosen, setChosen] = useState('');
  const scopes = [...new Set(report.vibration.series.map(item => `${item.instance_id}/${item.axis}`))];
  const selected = scopes.includes(chosen) ? chosen : scopes[0];
  const points = report.vibration.series.filter(item => `${item.instance_id}/${item.axis}` === selected);
  const minimum = report.scope.interval_start_s;
  const duration = report.scope.interval_end_s - minimum;
  const max = Math.max(report.vibration.critical_threshold_mps2, ...points.map(item => item.value_mps2), 1) * 1.1;
  return <section className="panel padded"><h3>Vibration and clipping</h3>{points.length ? <><label className="flight-trend-selector">Vibration sensor and axis<select value={selected} onChange={event => setChosen(event.target.value)}>{scopes.map(scope => <option key={scope} value={scope}>Sensor {scope.split('/')[0]} · {scope.split('/')[1].toUpperCase()} axis</option>)}</select></label><figure className="flight-trend"><svg viewBox="0 0 690 180" role="img" aria-label={`Recorded vibration, sensor and axis ${selected}`}><line x1="48" x2="650" y1="140" y2="140" stroke="#d5deeb"/>{[report.vibration.warning_threshold_mps2, report.vibration.critical_threshold_mps2].map((threshold, index) => <g key={index}><line x1="48" x2="650" y1={140 - threshold / max * 110} y2={140 - threshold / max * 110} stroke={index ? '#b6725b' : '#b5a06a'} strokeDasharray="5 5"/><text x="40" y={144 - threshold / max * 110} textAnchor="end">{number(threshold)}</text></g>)}{points.map((point, index) => <circle key={index} cx={48 + (point.time_s - minimum) / Math.max(duration, .001) * 602} cy={140 - point.value_mps2 / max * 110} r="2.5" fill="#477bbc"><title>{number(point.time_s, 's')} · {number(point.value_mps2, 'm/s²')}</title></circle>)}<text x="48" y="165">{number(minimum, 's')}</text><text x="650" y="165" textAnchor="end">{number(report.scope.interval_end_s, 's')}</text></svg><figcaption>m/s² · {report.vibration.series_reduction} Threshold basis: {readable(report.vibration.threshold_basis)}.</figcaption></figure><details className="technical"><summary>Vibration observations as a table</summary><div className="table-scroll"><table><thead><tr><th>Time</th><th>Vibration</th><th>Original record</th></tr></thead><tbody>{points.map((point, index) => <tr key={index}><td>{number(point.time_s, 's')}</td><td>{number(point.value_mps2, 'm/s²')}</td><td>{point.record_index}</td></tr>)}</tbody></table></div></details></> : <p>No qualifying vibration samples were recorded in this interval.</p>}<p>{report.vibration.clipping.interpretation}</p><details className="technical"><summary>Clipping counter changes and exclusions</summary><pre>{JSON.stringify(report.vibration.clipping, null, 2)}</pre></details></section>;
}
