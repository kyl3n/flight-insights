import type { EngineJob, InsightReport, Metric } from './api';
import type { ParameterExplanation } from './PerformanceAdvice';
import type { FlightEnvironmentSnapshotData } from './FlightEnvironment';

export interface LibraryFlight {
  id: string;
  kind: 'observed_flight' | 'log_interval' | 'log_segment';
  label: string;
  flight_record_id?: string | null;
  log_id?: string | null;
  source_key?: string | null;
  boot_epoch?: number | null;
  interval_start_s?: number | null;
  interval_end_s?: number | null;
  started_at: string | null;
  ended_at?: string | null;
  time_basis: 'reported_utc' | 'receive_utc' | 'unknown' | string;
  imported_at: string;
  aircraft: { id: string | null; label: string; identity_basis: string };
  status: 'ready' | 'needs_review' | 'awaiting_log' | 'pending' | string;
  report_id: string | null;
  metrics: Record<string, Metric>;
  finding_count: number;
  review_reasons: string[];
  synthetic: boolean;
  available_battery_ids?: string[];
  battery_id?: string | null;
  context_linkage?: 'capture_provenance' | 'verified' | null;
}

export interface LibrarySummary {
  total: number;
  analyzed: number;
  needs_review: number;
  awaiting_log: number;
  dated_flights: number;
  recent_flights: number;
  unknown_dates: number;
  pending_logs: number;
  period_start: string | null;
  period_end: string | null;
  recent_period_basis?: string;
}

export interface FlightLibrary {
  flights: LibraryFlight[];
  next_offset: number | null;
  total: number;
  summary: LibrarySummary;
}

export interface LibraryParameterDifference {
  basis: string;
  before_state: string;
  after_state: string;
  observations_eligible_for_review: boolean;
  changed: { name: string; before: unknown; after: unknown; before_type?: number; after_type?: number; type_changed?: boolean }[];
  before_only: { name: string; value: unknown }[];
  after_only: { name: string; value: unknown }[];
  counts: Record<string, number>;
  truncated: boolean;
  limitations: string[];
}

export interface LibraryComparison {
  before_report_id: string;
  after_report_id: string;
  status: string;
  metrics: {
    name: string; unit: string; before_unit: string | null; after_unit: string | null;
    before: number | null; after: number | null; delta: number | null; percent_change: number | null;
    status: string;
    before_coverage: { coverage_fraction: number | null };
    after_coverage: { coverage_fraction: number | null };
    limitations: string[];
  }[];
  parameter_diff: LibraryParameterDifference;
  observed_parameter_diff?: LibraryParameterDifference;
  configuration_comparison?: { status: string; eligible_for_parameter_review: boolean; reasons: string[]; limitations: string[] };
  parameter_explanations: ParameterExplanation[];
  parameter_explanations_truncated: boolean;
  environment_diff: { status: string; changes: { field: string; before: unknown; after: unknown; provenance: string }[]; limitations: string[] };
  confounders: { code: string; detail: string }[];
  limitations: string[];
}

export interface LibraryFlightDetail {
  flight: LibraryFlight;
  report: InsightReport | null;
  comparison: LibraryComparison | null;
  previous_flight: LibraryFlight | null;
  comparison_reason: string;
  comparison_basis?: string;
  comparison_pending?: boolean;
  recent_history: LibraryFlight[];
  review_reasons: string[];
  observed_context?: { flight_record_id: string; environment_snapshot: FlightEnvironmentSnapshotData | null; basis: 'capture_provenance'; limitations: string[] } | null;
  detail_omitted_reason?: string | null;
}

export interface LibraryJobResult {
  indexed_logs?: number;
  analyzed_flights?: number;
  compared_flights?: number;
  alignment_attempts?: number;
  pending_logs?: number;
  flights?: LibraryFlight[];
  summary?: LibrarySummary;
  job?: EngineJob;
}
