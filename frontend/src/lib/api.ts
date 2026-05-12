// API response types shared across pages. Fetches happen inline via SWR.

export type StatusPayload = {
  backend: string;
  summary: {
    entities?: number;
    relations?: number;
    repos_indexed?: number;
    unresolved_external_components?: number;
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
};

export type GraphPayload = {
  nodes: Array<{ id: string; label: string; kind: string; system?: string; tagline?: string }>;
  edges: Array<{ id: string; source: string; target: string; type: string; confidence?: string }>;
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
};
