import { Badge, Notes, number, readable } from './ui';

interface ReadbackReference {
  snapshot_id:string;acquisition_id:string;parameter_count:number;completeness:string;consistency:string;
  usable_for_full_comparison:boolean;unusable_reasons:string[];role:string;role_basis:string;
  parameters_preview:{name:string;value:string|number;mav_param_type:number}[];preview_truncated:boolean;
}
export interface FlightConfigurationData {
  status:string;reason?:string;preflight:ReadbackReference[];postflight:ReadbackReference[];
  effective_configuration:string;limitations:string[];
}
export default function FlightConfiguration({configuration}:{configuration?:FlightConfigurationData}) {
  if(!configuration)return null;
  return <section className="panel"><div className="panel-heading"><div><h2>Flight-linked full parameter readbacks</h2><p>Original readbacks can be used directly in analysis without exporting and importing parameter files.</p></div><Badge tone={configuration.preflight.some(item=>item.usable_for_full_comparison)?'blue':'warn'}>{configuration.preflight.some(item=>item.usable_for_full_comparison)?'Preflight readback preserved':'Preflight evidence incomplete'}</Badge></div><div className="padded">{configuration.reason&&<p>{configuration.reason}</p>}{(['preflight','postflight'] as const).map(role=><section key={role}><h3>{role==='preflight'?'Before flight':'After flight'}</h3>{configuration[role].length?configuration[role].map(item=><article className="evidence-card" key={item.snapshot_id}><div className="line-between"><strong>{number(item.parameter_count)} recorded parameters</strong><Badge tone={item.usable_for_full_comparison?'blue':'warn'}>{item.usable_for_full_comparison?'Full set available for comparison':readable(item.completeness)}</Badge></div><p>Consistency: {readable(item.consistency)}. Flight membership: supported by recorded identity and time evidence.</p><Notes title="Readback limits" items={item.unusable_reasons}/><details className="technical"><summary>Parameter preview and preserved snapshot</summary><p className="field-help">Snapshot {item.snapshot_id}. All captured values are preserved in the parameter library; this preview shows at most 64.</p><div className="table-scroll"><table><thead><tr><th>Parameter</th><th>Observed value</th><th>Protocol type</th></tr></thead><tbody>{item.parameters_preview.map(parameter=><tr key={parameter.name}><td className="mono">{parameter.name}</td><td>{String(parameter.value)}</td><td>{parameter.mav_param_type}</td></tr>)}</tbody></table></div>{item.preview_truncated&&<p>The preview is shortened; full comparisons read the complete preserved set.</p>}</details></article>):<p>No qualifying {role} readback was linked to this exact interval.</p>}</section>)}<Notes title="Configuration interpretation" items={configuration.limitations}/></div></section>;
}
