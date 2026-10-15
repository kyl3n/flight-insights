import { Badge, Notes, number, readable } from './ui';

interface HealthEvidence {time_s:number|null;record_index:number|null;byte_offset?:number|null;message_type:string}
interface HealthMetric {
  id:string;label:string;unit:string;status:string;sample_count:number;minimum:number|null;maximum:number|null;mean:number|null;
  first_time_s:number|null;last_time_s:number|null;gap_count:number;excluded_count:number;
  minimum_evidence:HealthEvidence|null;maximum_evidence:HealthEvidence|null;statistics_basis:string;
}
export interface FlightHealthData {
  algorithm_version:string;status:string;barrier_count:number;excluded_records:Record<string,number>;limitations:string[];
  domains:{id:string;title:string;status:string;limitations:string[];
    channels:{instance_id:string;message_type:string;metrics:HealthMetric[];observations:Record<string,number>}[];
    observed_instances?:string[];matched_frame_spreads?:HealthMetric[];frames_observed?:number;spreads_truncated?:boolean;retained_frame_limit?:number}[];
  findings:{id:string;domain:string;severity:string;title:string;explanation:string;observation_count:number;
    evidence:HealthEvidence[];recommended_checks:string[];parameter_families:string[];reference_ids:string[]}[];
  references:{id:string;repository:string;path:string;revision:string;sha256:string;retrieved_at:string;url:string}[];
}

function Evidence({value}:{value:HealthEvidence|null}) {
  return value?<span className="evidence-reference">{number(value.time_s,'s')} · {value.message_type} · record {value.record_index??'unknown'}{value.byte_offset!=null?` · byte ${value.byte_offset}`:''}</span>:<span>Unavailable</span>;
}
function Measurements({metrics,caption}:{metrics:HealthMetric[];caption:string}) {
  return <div className="table-scroll"><table><caption className="sr-only">{caption}</caption><thead><tr><th>Measurement</th><th>Recorded range</th><th>Sample mean</th><th>Evidence coverage</th></tr></thead><tbody>{metrics.map(metric=><tr key={metric.id}><td>{metric.label}<small>{metric.unit}</small></td><td>{metric.sample_count?`${number(metric.minimum)}–${number(metric.maximum)}`:'Unavailable'}<details className="technical"><summary>Range source records</summary><p>Minimum: <Evidence value={metric.minimum_evidence}/></p><p>Maximum: <Evidence value={metric.maximum_evidence}/></p></details></td><td>{number(metric.mean,metric.unit)}<small>{metric.statistics_basis}</small></td><td>{metric.sample_count} valid samples · {metric.excluded_count} excluded<small>{number(metric.first_time_s,'s')} to {number(metric.last_time_s,'s')} · {metric.gap_count} gaps or barriers</small></td></tr>)}</tbody></table></div>;
}

export default function FlightHealth({health}:{health?:FlightHealthData}) {
  if(!health)return null;
  return <section className="panel flight-health"><div className="panel-heading"><div><h2>Navigation, ESC, and system observations</h2><p>Review receiver, estimator, propulsion telemetry, scheduling, and board-power evidence in the selected interval.</p></div><Badge tone={health.status==='observed'?'blue':'warn'}>{readable(health.status)}</Badge></div><div className="padded">
    {health.findings.map(finding=><article className="recommendation" key={finding.id}><div className="line-between"><h3>{finding.title}</h3><Badge tone="warn">{readable(finding.severity)}</Badge></div><p>{finding.explanation}</p><p className="field-help">{finding.observation_count} qualifying observations</p>{finding.evidence.map((ref,index)=><p key={index}><Evidence value={ref}/></p>)}<Notes title="Checks to investigate" items={finding.recommended_checks}/>{finding.parameter_families.length>0&&<p>Related parameter families: <span className="mono">{finding.parameter_families.join(', ')}</span>. Verify applicability to the installed firmware.</p>}<ul>{health.references.filter(reference=>finding.reference_ids.includes(reference.id)).map(reference=><li key={reference.id}><a href={reference.url} target="_blank" rel="noreferrer">{reference.path} · revision {reference.revision.slice(0,8)}</a></li>)}</ul></article>)}
    {health.domains.map(domain=><details className="conditions" key={domain.id} open={domain.status==='observed'}><summary>{domain.title} · {readable(domain.status)}</summary>{!domain.channels.length&&<p>No supported, schema-qualified observations were available for this domain.</p>}{domain.channels.map(channel=><section key={`${channel.message_type}-${channel.instance_id}`}><h3>{channel.message_type} · {channel.instance_id==='board'?'board':`instance ${channel.instance_id}`}</h3><Measurements metrics={channel.metrics} caption={`${domain.title}, ${channel.message_type} instance ${channel.instance_id}`}/>{Object.keys(channel.observations).length>0&&<p className="field-help">{Object.entries(channel.observations).map(([name,count])=>`${readable(name)}: ${count}`).join(' · ')}</p>}</section>)}{domain.matched_frame_spreads&&<section><h3>ESC spreads in matching recorded frames</h3><p>Observed instances: {domain.observed_instances?.join(', ')||'unknown'}. The expected motor set and freshness of each telemetry field are unknown.</p><Measurements metrics={domain.matched_frame_spreads} caption="ESC spreads at matching recorded timestamps"/>{domain.spreads_truncated&&<p className="field-help">Spread summaries use at most {domain.retained_frame_limit} retained frames from {domain.frames_observed} observed frames. Choose a shorter interval to inspect later evidence.</p>}</section>}<Notes title="Interpretation limits" items={domain.limitations}/></details>)}
    <Notes title="Health evidence limits" items={health.limitations}/><details className="technical"><summary>Health analysis sources and exclusions</summary><p>{health.algorithm_version} · {health.barrier_count} data boundaries</p>{Object.entries(health.excluded_records).map(([reason,count])=><p key={reason}>{readable(reason)}: {count}</p>)}<ul>{health.references.map(reference=><li key={reference.id}><a href={reference.url} target="_blank" rel="noreferrer">{reference.repository} · {reference.path}</a><p className="mono break-word">Revision {reference.revision} · SHA-256 {reference.sha256}</p></li>)}</ul></details>
  </div></section>;
}
