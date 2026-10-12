import { invoke } from '@tauri-apps/api/core';

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

export interface SourceEvidence { id: string; sha256: string; byte_size: number; original_name: string }
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
  status: string; parameter_diff: ParameterDiff;
  metrics: {name: string; unit: string; before: number | null; after: number | null; absolute_change: number | null; percent_change: number | null;before_coverage_fraction?:number;after_coverage_fraction?:number}[];
  confounders: {field: string; reason: string; before: unknown; after: unknown}[]; limitations: string[];
}
export interface NetworkSource {
  id: string; kind: 'skypulse' | 'rtsp' | 'udp'; host: string; port?: number;
  interface?: string; local_address?: string; path?: string; expected_format?: 'mavlink' | 'rtp' | 'unknown';
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
  platforms:Record<string,{inspection:unknown;stream_probes:unknown;repair:{state:string;automatic_apply:boolean;reason:string};qualification:unknown}>;
  layers:{layer:string;state:string;coverage:string}[];
  repair_safety:{allowed:boolean;state:string;reasons:string[]};
}
export interface RepairPlan {
  plan_id: string; state: string; title: string; commands: string[][]; rollback_commands: string[][];
  requires_elevation: boolean; warnings: string[]; message?: string; reason?: string;
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
  result?:{log?:FlightLog;session?:Session}; error?:{message:string};
}
export interface RecoveryReceipt extends RepairPlan {
  ownership:'owned'|'changed'|'absent'|'unknown'; may_have_applied:boolean; recovered:boolean;
}
export interface RecoveryResponse {status:'ready'|'unavailable';repairs:RecoveryReceipt[];warnings:string[]}
export interface CollectionProfile {
  aircraft_id:string;aircraft_profile_version:string;connection_profile_version:string;
  connection:{local_address:string;listen_port:number;expected_sender:string;system_id:number;component_id:number;expected_uid?:string;feed_origin:'live_forwarded';synthetic:boolean};
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
  manifest:{revision:number;artifacts:ArtifactAssociation[];collection_completeness:CollectionCompleteness;parameter_evidence:FlightRecord['parameter_evidence'];limitations:string[];[key:string]:unknown};
  revisions:{revision:number;created_at:string;reason:string}[];associations:ArtifactAssociation[];
  retrieval_requests:{id?:string;role?:string;state:string;recovery_action:string;observed_path_or_null:string|null;[key:string]:unknown}[];
  association_total?:number;association_offset?:number;associations_truncated?:boolean;revision_total?:number;revisions_truncated?:boolean;
}
export interface CollectionPreview {profile:CollectionProfile;applicability_fingerprint:string;capabilities:Record<string,unknown>;limitations:string[]}
export async function listAll<T>(method:string,key:string):Promise<T[]> {
  const all:T[] = [];let offset=0;
  for (;;) {
    const page=await request<Record<string,unknown>&{next_offset?:number|null}>(method,{offset,limit:100});
    const items=page[key];if (!Array.isArray(items)) throw new Error('The engine returned an invalid evidence catalog.');
    all.push(...items as T[]);if (page.next_offset == null) return all;
    if (page.next_offset <= offset) throw new Error('The evidence catalog page could not fit in the engine response. No partial catalog is presented as complete.');
    offset=page.next_offset;
  }
}
