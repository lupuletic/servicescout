// API response types shared across pages. Fetches happen inline via SWR.

export type StatusPayload = {
  backend: string;
  summary: {
    entities?: number;
    relations?: number;
    repos_indexed?: number;
    unresolved_external_components?: number;
    derived_communication_flows?: number;
    node_kinds?: Record<string, number>;
    relation_types?: Record<string, number>;
  };
  last_build_at?: string | null;
  cumulative_spend_usd?: number;
  recent_extractions?: Array<{
    repo: string;
    model: string;
    provider: string;
    cost: number;
    duration_seconds: number;
    status: string;
  }>;
  crawler?: { running: boolean; pid: number | null; uptime?: string };
  log_path?: string | null;
  log_tail?: string[];
  now?: string;
};

export type GraphPayload = {
  nodes: Array<{
    id: string;
    label: string;
    kind: string;
    system?: string;
    tagline?: string;
    confidence?: string;
    degree?: number;
    is_center?: boolean;
  }>;
  edges: Array<{ id: string; source: string; target: string; type: string; confidence?: string; properties?: Record<string, unknown> }>;
  truncated: boolean;
  node_total: number;
  edge_total: number;
};

export type EntityRecord = {
  ref: string;
  kind: string;
  name: string;
  tagline?: string;
  description?: string;
  type?: string;
  system?: string;
  source_repos?: string[];
  environments?: string[];
  confidence?: string;
};

export type CrawlRunSummary = {
  run_id: string;
  trigger: string;
  started_at?: string;
  finished_at?: string;
  observed_at?: string;
  status?: string;
  repos_checked?: number;
  repos_changed_count: number;
  budget_usd?: number;
  cost_usd?: number;
  catalog_cost_usd?: number;
  duration_seconds?: number;
  worker_duration_seconds?: number;
  crawler_returncode?: number | null;
};

export type CrawlRunsPayload = {
  runs: CrawlRunSummary[];
  total: number;
  source?: "activity" | "extractions";
};

export type CrawlStatusPayload = {
  lock: { held: boolean; pid?: number; host?: string; acquired_at?: string; stale_or_corrupt?: boolean };
  scheduler?: {
    running: boolean;
    pid: number | null;
    uptime?: string;
    managed?: boolean;
    source?: "dashboard" | "external" | "stopped";
    interval_minutes?: number;
    budget_usd?: number;
    log_path?: string | null;
    log_tail?: string[];
  };
  crawler?: { running: boolean; pid: number | null; uptime?: string };
  crawler_runtime?: { parallelism: number; batch_size: number; source?: string; path?: string; updated_at?: string };
  last_run?: CrawlRunSummary | null;
  interval_minutes: number;
  budget_usd: number;
  workspace_root?: string;
  workspace_config?: string;
  active_log_path?: string | null;
  active_log_tail?: string[];
};

export type CrawlRunDetail = CrawlRunSummary & {
  workspace_root?: string;
  repos_changed?: Array<{
    repo: string;
    path?: string;
    reason?: string;
    status?: string;
    duration_seconds?: number;
    cost_usd?: number;
    error?: string;
    failure_category?: string;
    remote_sha?: string;
    extracted_sha?: string | null;
  }>;
  events?: Array<Record<string, unknown>>;
  crawler_stdout_tail?: string;
  crawler_stderr_tail?: string;
  error?: string;
};

export type OperatorSummary = {
  catalog: {
    last_build_at?: string | null;
    repos_indexed: number;
    entities: number;
    relations: number;
  };
  cost_trend: Array<{ day: string; cost: number; repos: number; duration_seconds: number }>;
  staleness: {
    buckets: Record<"fresh" | "warm" | "aging" | "stale" | "unknown", number>;
    repos: Array<{ repo: string; extracted_at?: string | null; age_hours?: number | null; cost: number; status: string }>;
    repo_total?: number;
  };
  verifier: {
    validation_errors: number;
    evidence_quarantined: number;
    repos_with_validation_errors: number;
    repos_with_quarantined_evidence: number;
    entity_confidence: Record<string, number>;
    relation_confidence: Record<string, number>;
  };
};
