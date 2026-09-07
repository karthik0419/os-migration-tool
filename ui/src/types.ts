export interface ClusterInfo {
  cluster_name: string | null;
  distribution: string;
  version: string | null;
  status: "green" | "yellow" | "red" | null;
  nodes: number | null;
  data_nodes: number | null;
  unassigned_shards: number | null;
  indices: number;
}

export interface IndexMismatch {
  index: string;
  src_count: number;
  tgt_count: number;
  gap: number;
  source_only: boolean;
  target_alias?: string;
}

export interface PlanResponse {
  mismatches: IndexMismatch[];
  total_gap: number;
  source_only_count: number;
  duration_s: number;
  log_file: string;
  warnings: string[];
}

export interface RunOptions {
  rps: number;
  pause: number;
  heap_threshold: number;
  unassigned_threshold: number;
  queue_threshold: number;
  start_from: number;
  sync_mapping?: boolean;
  copy_settings?: boolean;
}

export interface RunRequest extends RunOptions {
  source_url: string;
  source_auth: string;
  target_url: string;
  target_auth: string;
  indices?: string[];
  dry_run: boolean;
}

export type RunStatus = "pending" | "running" | "completed" | "failed" | "cancelled";

export interface RunSummaryEntry {
  index: string;
  result: string;
  src: number;
  tgt_after: number | null;
}

export interface RunSummary {
  timestamp: string;
  repaired: number;
  still_partial: number;
  skipped: number;
  mapping_conflicts: number;
  data_loss: number;
  total: number;
  results: RunSummaryEntry[];
}

export interface Run {
  run_id: string;
  status: RunStatus;
  source_url: string;
  target_url: string;
  indices: string[] | null;
  dry_run: boolean;
  options: RunOptions;
  started_at: string;
  finished_at: string | null;
  exit_code: number | null;
  error: string | null;
  summary: RunSummary | null;
  log_file: string;
  results_file: string;
  current_task: string | null;
  log_lines: number;
}

export type Level = "info" | "warn" | "error" | "success";

export interface LogEvent {
  seq: number;
  ts: string;
  level: Level;
  message: string;
}

export interface EndEvent {
  type: "end";
  status: RunStatus;
}
