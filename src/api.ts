import { invoke } from '@tauri-apps/api/core';
import type { FlightEnvironmentContext, FlightEnvironmentSnapshotData } from './FlightEnvironment';
import type { PerformanceAdviceData, ParameterExplanation } from './PerformanceAdvice';
import type { FlightConfigurationData } from './FlightConfiguration';
import type { FlightHealthData } from './FlightHealth';

export const isDesktop = () => '__TAURI_INTERNALS__' in window;
export async function request<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
  if (!isDesktop()) throw new Error('Open the installed desktop app to access the local analysis engine. Browser previews cannot inspect this computer or import evidence.');
  return invoke<T>('engine_request', { method, params });
}
export function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === 'object' && error && 'message' in error) return String(error.message);
  return typeof error === 'string' ? error : 'The local engine could not complete this operation.';
}

export interface SourceEvidence { id: string; sha256: string; byte_size: number; original_name: string; role?:string }
export interface Snapshot {
  id: string; label: string; aircraft_id: string; board: string; firmware: string;
  source_kind: string; observed_at: string|null; synthetic: boolean; parameter_count: number;
  observation_time_us?:string|null;time_domain?:string;
  source: SourceEvidence;
}
export interface Metric {
  value: number | null; unit: string; algorithm_version?: string; interval_start_s?: number; interval_end_s?: number;
  covered_duration_s?: number; coverage_fraction?: number; sample_count?: number; valid_pair_count?: number;
  source_fields?: string[]; uncertainty?: string; interpretation?: string;
  unavailable_reason?:string;log_id?:string;source_key?:string;boot_epoch?:number;time_domain?:string;source_sha256?:string;
  exclusions?: {start_s:number;end_s:number;reason:string}[]; exclusion_count?:number; exclusions_truncated?:boolean;
  [key: string]: unknown;
}
export interface Session {
  id: string; label: string; snapshot_id: string; aircraft_id: string; board: string; firmware: string;
  phase: string; synthetic: boolean; metrics: Record<string, Metric>; source?: SourceEvidence;
  interval_start_s?: number; interval_end_s?: number; limitations?: string[]; [key: string]: unknown;
  parameter_evidence?:string;parameter_split_points_s?:number[];parameter_change_count?:number;parameter_changes_truncated?:boolean;warnings?:string[];
  parameter_change_observations?:{name:string;before:string|number|null;after:string|number|null;time_s:number;time_us:string;kind:string}[];
}
export interface ParameterChange {
  name: string; before?: string|number; after?: string|number; value?: string|number; group: string;
  units: string | null; bitmask_labels: unknown; calibration_device_specific: boolean;
}
export interface ParameterDiff {
  before_id: string; after_id: string; compatibility: { compatible: boolean; issues: string[] };
  changed: ParameterChange[]; added: ParameterChange[]; removed: ParameterChange[];
  counts: {changed: number; added: number; removed: number}; limitations: string[]; truncated?:boolean;
}
export interface Comparison {
  parameter_explanations?:ParameterExplanation[];
  parameter_explanations_truncated?:boolean;
  status: string; parameter_diff: ParameterDiff;
  metrics: {name: string; unit: string; before: number | null; after: number | null; absolute_change: number | null; percent_change: number | null;before_coverage_fraction?:number;after_coverage_fraction?:number}[];
  confounders: {field: string; reason: string; before: unknown; after: unknown}[]; limitations: string[];
}
export interface NetworkSource {
  id: string; kind: 'skypulse' | 'rtsp' | 'udp'; host: string; port?: number;
  interface?: string; local_address?: string; path?: string; expected_format?: 'mavlink' | 'rtp' | 'unknown';
  rtp_codec?:'h265';rtp_payload_type?:number;h265_donl?:false;
}
export interface NetworkInspection {
  status: string; platform: string; interfaces: {name: string; index: number; up: boolean; ipv4: {address: string; prefix: number; network: string}[]}[];
  routes: unknown[]; default_routes: unknown[];
  targets: {source_id: string; host: string; state: string; selected_interface: string | null; local_address: string | null; issues: string[]}[];
  firewall: {state: string; reason: string}; limitations: string[];
  probes?: {source_id: string; state: string; checks: Record<string, unknown>[]; details: string; recovery_action: string}[];
  profile_evidence?:Record<string,unknown>;
}
export interface NetworkCapabilities {
  current_platform:string;
  platforms:Record<string,{inspection:unknown;stream_probes:unknown;repair:{state:string;automatic_apply:boolean;reason:string;bench_available?:boolean;bench_opt_in?:boolean;qualified?:boolean};qualification:unknown}>;
  layers:{layer:string;state:string;coverage:string}[];
  repair_safety:{allowed:boolean;state:string;reasons:string[]};
}
export interface RepairPlan {
  plan_id: string; state: string; title: string; commands: string[][]; rollback_commands: string[][];
  requires_elevation: boolean; warnings: string[]; message?: string; reason?: string;
  automatic_apply?:boolean;
  native_review?:{plan_id:string;review_fingerprint:string;candidate:Record<string,unknown>;before_fingerprint:string;expires_utc_ms:number;rollback_after_ms:number;authority:string};
}
export type Run = (label: string, action: () => Promise<void>) => Promise<void>;

export interface LogStream {
  source_key:string; system_id:number|null; component_id:number|null; boot_epoch:number; time_domain:string;
  boot_epoch_basis?:string;capture_link_id?:number|null;capture_link_id_basis?:string|null;
  origin_time_us:string|null; start_s:number|null; end_s:number|null; record_count:number; parameter_observation_count:number;
  battery_ids:string[]; power_sample_count:number; identity_observations:Record<string,unknown>[]; warnings:string[];
}
export interface FlightLog {
  id:string; label:string; format:string; source:SourceEvidence; synthetic:boolean; status:string;
  record_count:number; streams:LogStream[]; warnings:string[]; capabilities:unknown; parser_version:string;
}
export interface ParameterObservation {
  name:string; value:string|number; time_s:number|null; time_us:string|null; record_index:number;
  source_key:string; boot_epoch:number; completeness:string;
}
export interface LogDetail {
  log:FlightLog; streams:LogStream[]; parameter_observations:ParameterObservation[]; timeline_next_offset:number|null;
}
export interface EngineJob {
  id:string; state:'queued'|'running'|'succeeded'|'failed'|'cancelled'|'interrupted'; method:string; created_at?:string;
  cancel_requested?:boolean; progress?:{stage:string;completed?:number;total?:number;fraction:number|null};
  result?:{log?:FlightLog;session?:Session;[key:string]:unknown}; error?:{message:string};
}
export interface ReconciliationCandidate {
  kind:'flight_log'|'parameter_export';state:'provisional'|'rejected';log_id?:string;parameter_reference_id?:string;
  source_key?:string;boot_epoch?:number;record_span?:Record<string,unknown>;observed_uids?:unknown[];armed_intervals?:unknown[];
  supporting_evidence:(string|Record<string,unknown>)[];contradictions:string[];missing_evidence:string[];
  synthetic?:boolean;association_options?:{observed_interval_index:number;boundary_status:string;start_status:string;coverage:unknown;requires_explicit_review:boolean;maximum_association_state:string;params:{log_id:string;source_key:string;boot_epoch:number;role:string;time_or_record_spans:{kind:string;start:number;end:number}[]}}[];
}
export interface ReconciliationReport {
  id:string;state:string;flight_record_id?:string;target?:{flight_record_id:string};imported_count:number;candidate_count:number;candidate_states:Record<string,number>;
  files:{selected_path:string;original_name:string;state:string;log_id?:string;parameter_reference_id?:string;source?:SourceEvidence;error?:string}[];
  candidates:ReconciliationCandidate[];limits_reached:string[];limitations:string[];[key:string]:unknown;
}
export interface AcquisitionConnection {transport?:'shared_recorder'|'dedicated';client_system_id?:number;client_component_id?:number;local_address:string;local_port:number;remote_address:string;remote_port:number;system_id:number;component_id:number;expected_uid:string;feed_origin:'direct_bidirectional'}
export interface AcquisitionPreview {connection:AcquisitionConnection;limits:Record<string,number>;applicability_fingerprint:string;prerequisites:string[];outgoing_allowlist:string[];limitations:string[]}
export interface AcquisitionRecord {
  id:string;kind:'parameters'|'log_list'|'log_download';state:string;aircraft_id:string;connection:AcquisitionConnection;synthetic:boolean;
  source_refs:SourceEvidence[];retained_files:{path:string;byte_size:number;sealed:boolean;archived:boolean;sha256?:string;source_id?:string}[];
  interruption_reason?:string;transport_evidence?:{log_request_end_sent?:boolean;logging_resumption_verified?:boolean;recovery_warning?:string;[key:string]:unknown};
  parameter_capture?:{completeness:string;consistency:string;freshness:string;flight_time_applicability:string;atomic:boolean;decoded_count:number;expected_count:number|null;snapshot_id:string};
  entries?:{log_id:number;reported_size:number;reported_time_utc:number|null;remote_filename:null}[];listing_complete?:boolean;
  download?:{listing_id:string;log_id:number;reported_size:number;received_bytes:number;contiguous_prefix_bytes:number;transfer_complete:boolean;sha256?:string;logger_closed:string;association_state:string};
  limitations:string[];[key:string]:unknown;
}
export interface RecoveryReceipt extends RepairPlan {
  ownership:'owned'|'changed'|'absent'|'unknown'; may_have_applied:boolean; recovered:boolean;
  rollback_deadline_utc_ms?:number|null;native_state?:string;
}
export interface RecoveryResponse {status:'ready'|'unavailable';repairs:RecoveryReceipt[];warnings:string[]}
export interface CollectionProfile {
  aircraft_id:string;aircraft_profile_version:string;connection_profile_version:string;
  environment_context?:FlightEnvironmentContext;
  connection:{local_address:string;listen_port:number;expected_sender:string;system_id:number;component_id:number;expected_uid?:string;readback?:{remote_port:number;client_system_id?:number;client_component_id?:number};feed_origin:'live_forwarded';synthetic:boolean};
  required_sources:string[];
}
export interface CollectionReadiness {state:string;reasons:string[];can_observe:boolean;can_claim_complete:boolean}
export interface CaptureSession {
  uuid:string;status:string;profile:CollectionProfile;applicability_fingerprint:string;watcher_state:string;
  armed_interval_ids:string[];source_ids:string[];coverage:Record<string,unknown>;latest_event:Record<string,unknown>|null;
  archive_state?:string;archive_error?:string;artifact_count?:number;archive_job_id?:string;
  [key:string]:unknown;
}
export interface CollectionCompleteness {state:string;required:string[];present:string[];missing:string[]}
export interface FlightRecord {
  id:string;armed_interval_id:string;capture_session_id:string;display_number:number;classification:string;
  processing_state:string;started_already_armed:boolean;arm_evidence:Record<string,unknown>;disarm_evidence:Record<string,unknown>|null;
  latest_manifest_revision:number;collection_completeness:CollectionCompleteness;
  association_summary:{verified:number;provisional:number;rejected:number;missing:number};
  parameter_evidence:{preflight:Record<string,unknown>|null;postflight:Record<string,unknown>|null;applicability:string;limitations:string[]};missing_sources:string[];
}
export interface ArtifactAssociation {
  id:string;revision:number;artifact_sha256:string;role:string;vehicle_filter:Record<string,unknown>;boot_epoch:number|string|string[];
  time_or_record_spans:{kind:string;start:number|string;end:number|string|null}[];state:string;supporting_evidence:string[];
  contradictions:string[];candidate_alternatives:string[];rule_version:string;review_history:unknown[];
}
export interface CollectionStatus {
  capture_sessions:CaptureSession[];capture_session:CaptureSession|null;flight_records:FlightRecord[];
  readiness:CollectionReadiness;limitations:string[];recorder?:RecorderStatus;status_unavailable?:boolean;
  active_capture_session?:CaptureSession|null;capture_total?:number;capture_offset?:number;captures_truncated?:boolean;
  flight_record_total?:number;offset?:number;limit?:number;flight_records_truncated?:boolean;
}
export interface RecorderStatus {
  state:string;active:boolean;packets?:number;received_bytes?:number;valid_frames?:number;invalid_frames?:number;
  written_bytes?:number;durable_bytes?:number;status_age_s?:number|null;sources?:{source_id?:string;system_id?:number;component_id?:number;heartbeat_age_s?:number|null;armed_state?:string}[];
  [key:string]:unknown;
}
export interface FlightBundleDetail {
  flight_record:FlightRecord;armed_interval:Record<string,unknown>;capture_session:CaptureSession;
  environment_snapshot?:FlightEnvironmentSnapshotData;
  manifest:{revision:number;artifacts:ArtifactAssociation[];collection_completeness:CollectionCompleteness;parameter_evidence:FlightRecord['parameter_evidence'];environment_snapshot?:FlightEnvironmentSnapshotData;limitations:string[];[key:string]:unknown};
  revisions:{revision:number;created_at:string;reason:string}[];associations:ArtifactAssociation[];
  retrieval_requests:{id?:string;role?:string;state:string;recovery_action:string;observed_path_or_null:string|null;[key:string]:unknown}[];
  association_total?:number;association_offset?:number;associations_truncated?:boolean;revision_total?:number;revisions_truncated?:boolean;
}
export interface CollectionPreview {profile:CollectionProfile;applicability_fingerprint:string;capabilities:Record<string,unknown>;limitations:string[]}
export interface InsightEvidence {time_s?:number|null;time_us?:string|null;record_index?:number;byte_offset?:number;message_type?:string;[key:string]:unknown}
export interface InsightFinding {id:string;kind:string;severity:string;title:string;evidence:InsightEvidence;details:Record<string,unknown>;citation_ids:string[]}
export interface GuidanceCitation {id:string;title:string;summary:string;evidence_requirements:string[];limitations:string[];parameter_families:string[];firmware_scope:string;references:{source_id:string;repository:string;revision:string;path:string;url:string;sha256:string;retrieved_at:string;line_start?:number;line_end?:number}[]}
export interface InsightReport {
  health?:FlightHealthData;
  configuration?:FlightConfigurationData;
  performance_advice?:PerformanceAdviceData;
  flight_context?:{status:string;reason?:string;flight_record_id?:string;environment_snapshot?:FlightEnvironmentSnapshotData;limitations:string[]};
  id:string;created_at?:string;algorithm_version:string;control?:ControlEvidence;filter_model?:FilterModelEvidence;modeled_spectrum?:{status:string;reason?:string;bins?:{frequency_hz:number;observed_psd:number;modeled_psd:number|null}[];observed_band_power?:number;modeled_band_power?:number;band_power_unit?:string;band_attenuation_db?:number;band_start_hz?:number;band_end_hz?:number;interpretation?:string;[key:string]:unknown};
  scope:{log_id:string;source_sha256:string;synthetic:boolean;source_key:string;boot_epoch:number;time_domain:string;origin_time_us:string|null;interval_start_s:number;interval_end_s:number;max_gap_s:number;battery_id:string|null};
  operating_profile:Record<string,unknown>;metrics:Record<string,Metric>;
  vibration:{status:string;series:{instance_id:string;axis:string;time_s:number;value_mps2:number;record_index:number}[];distributions?:{instance_id:string;axis:string;sample_count:number;p50_mps2:number;p95_mps2:number;p99_mps2:number;maximum_mps2:number;time_above_warning_s:number;time_above_critical_s:number;covered_duration_s:number;distribution_approximate:boolean;distribution_method:string;exposure_basis:string}[];series_reduction:string;warning_threshold_mps2:number;critical_threshold_mps2:number;threshold_basis:string;excursions:InsightFinding[];coverage?:{instance_id:string;axis:string;sample_count:number;covered_duration_s:number;coverage_fraction:number;valid_pair_count:number;exclusions:unknown[];exclusion_count:number}[];clipping:{observed_deltas:Record<string,number>;resets:unknown[];reset_count?:number;resets_truncated?:boolean;gap_count:number;interpretation:string};observed_records:number;excluded_fields:number};
  parameter_evidence:{state:string;observations:{name:string;value:string|number;age_at_start_s:number;evidence:InsightEvidence}[];changes:Record<string,unknown>[];observations_truncated:boolean;changes_truncated:boolean;change_count:number;[key:string]:unknown};
  findings:InsightFinding[];finding_count:number;findings_truncated:boolean;
  recommendations:{id:string;action:string;citation_ids:string[];parameter_families:string[];proposed_values:null;parameter_attribution_state:string;evidence_ids:string[];requires_operator_review:boolean;firmware_caveat:string}[];
  spectrum:{status:string;reason?:string;bins?:{frequency_hz:number;psd:number}[];peaks?:{frequency_hz:number;psd:number}[];[key:string]:unknown};
  guidance:{version:string;mode:string;retrieved_at:string;freshness:Record<string,unknown>;firmware_compatibility:Record<string,unknown>;entries:GuidanceCitation[];limitations:string[]};
  warnings:string[];limitations:string[];evidence_features:Record<string,unknown>;
}
export async function listAll<T>(method:string,key:string,limit=100):Promise<T[]> {
  const all:T[] = [];let offset=0;
  for (;;) {
    const page=await request<Record<string,unknown>&{next_offset?:number|null}>(method,{offset,limit});
    const items=page[key];if (!Array.isArray(items)) throw new Error('The engine returned an invalid evidence catalog.');
    all.push(...items as T[]);if (page.next_offset == null) return all;
    if (page.next_offset <= offset) throw new Error('The evidence catalog page could not fit in the engine response. No partial catalog is presented as complete.');
    offset=page.next_offset;
  }
}

export interface AcquisitionPolicy {id:string;policy_fingerprint:string;created_at:string;policy:{aircraft_id:string;connection:AcquisitionConnection;synthetic:boolean;preflight_parameters:boolean;postflight_parameters:boolean;automatic_preflight_refresh?:boolean;log_retrieval:'disabled'|'selected_after_stop';confirm_log_transfer_side_effects:boolean;preflight_freshness_s:number;max_requests_per_capture:number;limits?:Record<string,number>};adapter:Record<string,unknown>;limitations:string[]}
export interface AcquisitionPolicyApproval {policy_id:string;policy_fingerprint:string;confirm_automatic_readback:true;confirm_experimental_adapter:true;confirm_log_transfer_side_effects?:true}
export interface OrchestrationRequest {id:string;capture_session_id:string;flight_record_id:string|null;role:string;state:string;job_id:string|null;acquisition_id:string|null;association_state:string;historical_applicability:string;freshness:string;applicability:string;reason?:string;recovery_warning?:string;[key:string]:unknown}
export interface OrchestrationStatus {runs:{id:string;state:string;runtime_authorized:boolean;authorization_state:string;gate:{allowed:boolean;reason:string};[key:string]:unknown}[];requests:OrchestrationRequest[];request_total:number;run_total:number;next_offset:number|null;adapter:Record<string,unknown>;limitations:string[]}
export interface AlignmentSelection {log_id:string;source_key:string;boot_epoch:number}
export interface AlignmentEvidence {selection:AlignmentSelection;artifact_sha256:string;format:string;synthetic?:boolean;uids:unknown[];firmware:unknown[];clock_model:{state:string;[key:string]:unknown};armed_intervals:unknown[];[key:string]:unknown}
export interface AlignmentReport {id:string;created_at:string;algorithm_version:string;inputs:{reference:AlignmentSelection;candidates:AlignmentSelection[];flight_record_id?:string};reference:AlignmentEvidence;candidates:{selection:AlignmentSelection;artifact_sha256:string;format:string;state:string;matches:Record<string,unknown>[];missing_evidence:string[];contradictions:string[];competing_candidates:AlignmentSelection[];evidence:AlignmentEvidence}[];candidate_universe:{basis:string;log_count:number;stream_count:number;truncated:boolean;signature:string;alternatives:unknown[]};parameter_timeline:{state:string;observations:unknown[];changes:unknown[];attribution_allowed:false;[key:string]:unknown};parameter_links?:{acquisitions:{acquisition_id:string;state:string;matches:unknown[];completeness:string;consistency:string;missing_evidence:string[];contradictions:string[];[key:string]:unknown}[];exports:Record<string,unknown>[];automatic_effective_configuration:false};limitations:string[]}
export interface ControlEvidence {status:string;reason?:string;axis:string;unit?:string;sample_count:number;sample_limit?:number;truncated?:boolean;scope?:{start_s:number;end_s:number};tracking?:{rmse_deg_s:number|null;bias_deg_s:number|null;absolute_error_p50_deg_s:number|null;absolute_error_p95_deg_s:number|null;absolute_error_p99_deg_s:number|null;covered_duration_s:number;coverage_fraction:number;quantile_basis:string};lag?:{status:string;seconds:number|null;reason?:string;[key:string]:unknown};step?:{status:string;rise_time_s?:number;settling_time_s?:number;overshoot_fraction?:number;reason?:string;[key:string]:unknown};pid_terms?:{status:string;statistics:Record<string,{sample_count:number;mean:number;rms:number;minimum:number;maximum:number;unit:string}>;flags:Record<string,unknown>;interpretation:string};series:{time_s:number;desired_deg_s:number;actual_deg_s:number;record_index:number}[];references?:string[];limitations?:string[]}
export interface FilterModelEvidence {status:string;reason?:string;model_version?:string;sample_rate_hz?:number;stages?:unknown[];response?:{frequency_hz:number;gain:number|null;attenuation_db:number|null;phase_deg:number|null;group_delay_s:number|null;undefined_reason:string|null}[];qualification?:Record<string,unknown>;references?:{url:string;path:string;revision:string}[];validation?:Record<string,unknown>;limitations?:string[]}
