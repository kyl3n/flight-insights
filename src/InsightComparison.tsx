import { useEffect, useRef, useState, type FormEvent } from 'react';
import { request, type EngineJob, type InsightReport, type Run } from './api';
import BackgroundJobs from './BackgroundJobs';
import { useEngineJobs } from './useEngineJobs';
import { ParameterExplanationCard, type ParameterExplanation } from './PerformanceAdvice';
import { Badge, Notes, number, readable } from './ui';

interface ReportComparison {
  before_report_id:string;after_report_id:string;status:string;
  metrics:{name:string;unit:string;before_unit:string|null;after_unit:string|null;before:number|null;after:number|null;delta:number|null;percent_change:number|null;status:string;before_coverage:{coverage_fraction:number|null};after_coverage:{coverage_fraction:number|null};limitations:string[]}[];
  parameter_diff:ParameterDifference;
  observed_parameter_diff?:ParameterDifference;
  configuration_comparison?:{status:string;eligible_for_parameter_review:boolean;all_parameters_compared?:boolean;reasons:string[];limitations:string[]};
  parameter_explanations:ParameterExplanation[];
  parameter_explanations_truncated:boolean;
  environment_diff:{status:string;changes:{field:string;before:unknown;after:unknown;provenance:string}[];limitations:string[]};
  confounders:{code:string;detail:string}[];limitations:string[];
}
interface ParameterDifference {basis:string;before_state:string;after_state:string;observations_eligible_for_review:boolean;changed:{name:string;before:unknown;after:unknown;before_type?:number;after_type?:number;before_token?:string;after_token?:string;type_changed?:boolean}[];before_only:{name:string;value:unknown}[];after_only:{name:string;value:unknown}[];counts:Record<string,number>;truncated:boolean;limitations:string[]}
const display=(value:unknown)=>value==null?'Unknown':typeof value==='object'?JSON.stringify(value):String(value);
function metricLabel(name:string) {
  const vibration=/^vibration\.([^.]+)\.([xyz])\.(p95_mps2|maximum_mps2)$/.exec(name);
  if(vibration)return `Vibration ${vibration[2].toUpperCase()} · sensor ${vibration[1]} · ${vibration[3]==='p95_mps2'?'95th percentile':'maximum'}`;
  const control=/^control\.(roll|pitch|yaw)\.tracking_rmse_deg_s$/.exec(name);
  if(control)return `${readable(control[1])} rate tracking RMSE`;
  if(name==='spectrum.retained_band_power')return 'Gyro signal power in recorded frequency band';
  return readable(name);
}
export default function InsightComparison({reports,ready,busy,run}:{reports:InsightReport[];ready:boolean;busy:string;run:Run}) {
  const [before,setBefore]=useState('');const [after,setAfter]=useState('');const [result,setResult]=useState<ReportComparison|null>(null);
  const generation=useRef(0);
  const selected=useRef({before,after});selected.current={before,after};
  const queuedComparisons=useRef(new Map<string,{before:string;after:string;generation:number}>());
  const jobs=useEngineJobs(ready,['insights.compare'],completed=>{
    for(const job of completed){
      const expected=queuedComparisons.current.get(job.id);queuedComparisons.current.delete(job.id);
      const comparison=job.result as unknown as ReportComparison|undefined;
      if(!expected||!comparison||expected.generation!==generation.current||expected.before!==selected.current.before||expected.after!==selected.current.after||comparison.before_report_id!==expected.before||comparison.after_report_id!==expected.after)continue;
      setResult(comparison);
    }
  });
  useEffect(()=>()=>{generation.current++;queuedComparisons.current.clear();},[ready]);
  useEffect(()=>{for(const job of jobs.jobs)if(job.state==='cancelled'||job.state==='failed'||job.state==='interrupted')queuedComparisons.current.delete(job.id);},[jobs.jobs]);
  const choose=(side:'before'|'after',value:string)=>{generation.current++;selected.current={...selected.current,[side]:value};setResult(null);(side==='before'?setBefore:setAfter)(value);};
  async function compare(event:FormEvent){
    event.preventDefault();if(jobs.pending||!before||!after||before===after)return;
    const expected={before,after,generation:++generation.current};setResult(null);
    await run('Queuing preserved insight comparison',async()=>{
      const value=await request<{job:EngineJob}>('insights.compare',{before_report_id:expected.before,after_report_id:expected.after});
      queuedComparisons.current.set(value.job.id,expected);jobs.queue(value.job);
    });
  }
  const label=(report:InsightReport)=>`${report.scope.source_key} · ${number(report.scope.interval_start_s,'s')}–${number(report.scope.interval_end_s,'s')} · ${report.created_at||report.id}${report.scope.synthetic?' [synthetic]':''}`;
  return <section className="panel" aria-label="Compare preserved insight reports"><div className="panel-heading"><div><h2>Settings changes &amp; flight performance</h2><p>Use any recorded flight as your reference. Compare similar phases and conditions to investigate what changed.</p></div></div><div className="padded">
    {reports.length<2?<p>Save reports for two flight intervals to compare their observed parameters, performance, and recorded software context. No earlier baseline is needed to record your first flight.</p>:<form onSubmit={compare}><div className="form-grid"><label>Earlier insight report<select required value={before} onChange={event=>choose('before',event.target.value)}><option value="">Choose the reference report</option>{reports.map(report=><option key={report.id} value={report.id}>{label(report)}</option>)}</select></label><label>Later insight report<select required value={after} onChange={event=>choose('after',event.target.value)}><option value="">Choose the comparison report</option>{reports.map(report=><option key={report.id} value={report.id}>{label(report)}</option>)}</select></label></div><button className="button secondary" disabled={!ready||!jobs.available||Boolean(busy)||jobs.pending||!before||!after||before===after}>Compare insight reports</button></form>}
    {jobs.error&&<Notes title="Comparison operations unavailable" items={[jobs.error]}/>}
    <BackgroundJobs jobs={jobs.jobs} ready={ready} busy={busy} run={run} cancel={jobs.cancel} labels={{'insights.compare':'Preserved insight comparison'}}/>
    {result&&<section aria-label="Insight report comparison result"><div className="line-between"><h3>Observed differences</h3><Badge tone="warn">{readable(result.status)} · association only</Badge></div><Notes title="What limits this comparison" items={result.confounders.map(item=>item.detail)}/><div className="table-scroll"><table><caption className="sr-only">Performance differences between preserved reports</caption><thead><tr><th>Metric</th><th>Earlier</th><th>Later</th><th>Difference</th><th>Evidence</th></tr></thead><tbody>{result.metrics.map(metric=><tr key={metric.name}><td>{metricLabel(metric.name)}</td><td>{number(metric.before,metric.before_unit||'unit unknown')}<small>{number(metric.before_coverage.coverage_fraction==null?null:metric.before_coverage.coverage_fraction*100,'%')} coverage</small></td><td>{number(metric.after,metric.after_unit||'unit unknown')}<small>{number(metric.after_coverage.coverage_fraction==null?null:metric.after_coverage.coverage_fraction*100,'%')} coverage</small></td><td>{number(metric.delta,metric.unit)}<small>{number(metric.percent_change,'%')}</small></td><td>{readable(metric.status)}<small>{metric.limitations.join(' ')}</small></td></tr>)}</tbody></table></div>
      <h3>Parameter changes and plausible effects</h3>
      <ParameterDifferences difference={result.parameter_diff}/>
      {result.configuration_comparison&&!result.configuration_comparison.eligible_for_parameter_review&&<Notes title="Complete preflight comparison unavailable" items={result.configuration_comparison.reasons}/>}
      {result.parameter_diff.basis==='complete_typed_preflight_readbacks'&&result.observed_parameter_diff&&<details className="technical"><summary>Separate parameter observations from the logs</summary><ParameterDifferences difference={result.observed_parameter_diff}/></details>}
      <div className="recommendation-grid">{result.parameter_explanations.map(item=><ParameterExplanationCard key={item.parameter} item={item}/>)}</div>
      {result.parameter_explanations_truncated&&<p>Only the first 32 parameter changes have explanations in this review. Inspect the full parameter differences before drawing conclusions.</p>}
      <h3>Computer and software changes</h3><Badge>{readable(result.environment_diff.status)}</Badge>{result.environment_diff.changes.length>0&&<div className="table-scroll"><table><thead><tr><th>Recorded context</th><th>Earlier</th><th>Later</th><th>Source</th></tr></thead><tbody>{result.environment_diff.changes.map((change,index)=><tr key={index}><td>{readable(change.field)}</td><td>{display(change.before)}</td><td>{display(change.after)}</td><td>{readable(change.provenance)}</td></tr>)}</tbody></table></div>}<Notes title="Software context coverage" items={result.environment_diff.limitations}/><Notes title="Interpretation limits" items={result.limitations}/>
    </section>}
  </div></section>;
}

const parameterType=(value:number|undefined)=>value==null?'Not recorded':({1:'Unsigned 8-bit integer',2:'Signed 8-bit integer',3:'Unsigned 16-bit integer',4:'Signed 16-bit integer',5:'Unsigned 32-bit integer',6:'Signed 32-bit integer',7:'Unsigned 64-bit integer',8:'Signed 64-bit integer',9:'32-bit floating point',10:'64-bit floating point'}[value]||`MAVLink type ${value}`);
function ParameterDifferences({difference}:{difference:ParameterDifference}) {
  const full=difference.basis==='complete_typed_preflight_readbacks';
  return <>
    <p>{full?'The complete preserved preflight readbacks were compared, including recorded value types.':'This comparison uses parameter observations from the selected logs.'} Earlier: {readable(difference.before_state)}. Later: {readable(difference.after_state)}.</p>
    <p className="field-help">{full?'A complete enumeration is collected over time and does not establish which reboot-dependent settings were active in flight.':'An unobserved value is unknown; matching subsets do not establish an unchanged full configuration.'} {difference.counts.compared} shared parameters compared.</p>
    {!difference.observations_eligible_for_review&&<p className="field-help">These parameter observations are uncertain. The explanations below describe hypotheses and cannot attribute a measured change to a setting.</p>}
    {difference.changed.length===0&&<p>{full?'No value or type changes were found among parameters present in both complete readbacks.':'No changed values were found among parameters observed in both reports. This does not establish that the full configuration was unchanged.'}</p>}
    <details className="technical"><summary>{full?'Complete readback parameter differences':'All observed parameter differences'} · {difference.counts.changed} changed</summary>
      <p>{difference.counts.before_only} present only earlier · {difference.counts.after_only} present only later. {full?'These describe the recorded enumerations; firmware applicability still needs review.':'These are gaps in observation, not confirmed additions or removals on the aircraft.'}</p>
      <div className="table-scroll"><table><thead><tr><th>Parameter</th><th>Earlier</th><th>Later</th>{full&&<th>Recorded value type</th>}</tr></thead><tbody>
        {difference.changed.map(row=><tr key={row.name}><td className="mono">{row.name}</td><td>{display(row.before)}</td><td>{display(row.after)}</td>{full&&<td>{row.type_changed?`${parameterType(row.before_type)} → ${parameterType(row.after_type)}`:parameterType(row.before_type)}{row.before_token&&row.after_token&&<details><summary>Exact recorded representations</summary><p className="mono break-word">{row.before_token} → {row.after_token}</p></details>}</td>}</tr>)}
        {difference.before_only.map(row=><tr key={row.name}><td className="mono">{row.name}</td><td>{display(row.value)}</td><td>{full?'Absent from this readback':'Not observed'}</td>{full&&<td>Present only earlier</td>}</tr>)}
        {difference.after_only.map(row=><tr key={row.name}><td className="mono">{row.name}</td><td>{full?'Absent from this readback':'Not observed'}</td><td>{display(row.value)}</td>{full&&<td>Present only later</td>}</tr>)}
      </tbody></table></div>
      {difference.truncated&&<p>{full?'All preserved parameters were compared, but the displayed difference rows are limited. Counts include changes beyond the visible rows.':'Parameter observations are incomplete or truncated. Visible counts do not describe the full aircraft configuration.'}</p>}
    </details>
    <Notes title="Parameter comparison limits" items={difference.limitations}/>
  </>;
}
