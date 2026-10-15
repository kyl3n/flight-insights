import { Badge, Notes, readable } from './ui';

export interface AdviceReference {source_id:string;url:string;revision:string;retrieved_at:string;path:string}
export interface ParameterExplanation {
  parameter:string;before:unknown;after:unknown;direction:string;status:string;mechanism:string;expected_effect:string;
  evidence_requirements:string[];verification_metrics:string[];references:AdviceReference[];interpretation:string;
}
export interface PerformanceAdviceData {
  version:string;mode:string;retrieved_at:string;coverage_topics:string[];limitations:string[];
  parameter_context?:{basis:string;complete_preflight_readback:boolean;parameter_count:number;warnings:string[];numeric_disagreement_count:number;logged_only_count:number};
  recommendations:{id:string;title:string;status:string;priority:number;symptom:string;mechanism:string;
    observed_parameters:{name:string;value:unknown}[];parameter_families:string[];candidate_action:string;
    parameter_context_basis?:string;observed_parameter_count?:number;logged_parameters?:{name:string;value:unknown}[];
    alternative_hypotheses:string[];evidence_requirements:string[];tradeoffs:string[];verification_experiment:string[];
    references:AdviceReference[];firmware_scope:{scope:string}}[];
}
export function AdviceSources({references}:{references:AdviceReference[]}) {
  return <ul className="advice-sources">{references.map(reference=><li key={reference.url}><a href={reference.url} target="_blank" rel="noreferrer">{readable(reference.source_id)}</a><span className="field-help"> · reviewed {reference.retrieved_at} · revision {reference.revision.slice(0,8)}</span></li>)}</ul>;
}
export function ParameterExplanationCard({item}:{item:ParameterExplanation}) {
  return <article className="recommendation"><Badge tone="warn">Conditional explanation</Badge><h3 className="mono">{item.parameter}: {String(item.before)} → {String(item.after)}</h3><p>{item.mechanism}</p><p><strong>Expected effect to test:</strong> {item.expected_effect}</p><p className="field-help">{item.interpretation}</p><details className="technical"><summary>Evidence and verification for {item.parameter}</summary><Notes title="Evidence needed" items={item.evidence_requirements}/><Notes title="What to measure" items={item.verification_metrics}/><AdviceSources references={item.references}/></details></article>;
}
export default function PerformanceAdvice({advice}:{advice?:PerformanceAdviceData}) {
  if(!advice)return null;
  return <section className="panel"><div className="panel-heading"><div><h2>Performance improvement review</h2><p>Candidate actions, physical explanations, and tests based on this report’s evidence.</p></div><Badge>Reviewed offline guidance</Badge></div><div className="padded">
    {advice.parameter_context&&<><p className="field-help">Parameter context: {advice.parameter_context.complete_preflight_readback?`complete typed preflight readback, ${advice.parameter_context.parameter_count} preserved parameters`:'partial logged observations'}. Active settings and reboot requirements remain unverified.</p><Notes title="Parameter evidence to resolve" items={advice.parameter_context.warnings}/></>}
    <div className="recommendation-grid">{advice.recommendations.map(item=><article className="recommendation" key={item.id}><Badge tone="warn">{readable(item.status)}</Badge><h3>{item.title}</h3><p><strong>Observed context:</strong> {item.symptom}</p><p>{item.mechanism}</p><p><strong>Candidate action:</strong> {item.candidate_action}</p>
      {item.observed_parameters.length>0&&<p className="field-help break-word">{item.parameter_context_basis==='complete_typed_preflight_readback'?'Preflight readback':'Observed settings'}: {item.observed_parameters.map(parameter=>`${parameter.name} = ${String(parameter.value)}`).join(', ')}{(item.observed_parameter_count??0)>item.observed_parameters.length?` · showing ${item.observed_parameters.length} of ${item.observed_parameter_count} relevant parameters`:''}</p>}
      <details className="technical"><summary>Why, prerequisites, and a validation test</summary>{item.parameter_context_basis==='complete_typed_preflight_readback'&&<p className="field-help break-word">Logged observations, kept separately: {item.logged_parameters?.length?item.logged_parameters.map(parameter=>`${parameter.name} = ${String(parameter.value)}`).join(', '):'No corresponding logged values were observed.'}</p>}<Notes title="Evidence needed" items={item.evidence_requirements}/><Notes title="Other explanations to check" items={item.alternative_hypotheses}/><Notes title="Tradeoffs" items={item.tradeoffs}/><h4>Validation experiment</h4><ol>{item.verification_experiment.map((step,index)=><li key={index}>{step}</li>)}</ol><p className="field-help">{item.firmware_scope.scope}</p><AdviceSources references={item.references}/></details></article>)}</div><details className="technical"><summary>Research scope and limits · reviewed {advice.retrieved_at}</summary><p>{advice.coverage_topics.map(readable).join(' · ')}</p><Notes items={advice.limitations}/></details></div></section>;
}
