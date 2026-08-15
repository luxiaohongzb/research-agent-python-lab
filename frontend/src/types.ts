export type RunStatus =
  | "PENDING"
  | "RUNNING"
  | "COMPLETED"
  | "NEEDS_REVIEW"
  | "FAILED"
  | "CANCELLED";

export interface ResearchRequest {
  question: string;
  max_papers: number;
  max_iterations: number;
  max_workers: number;
  max_cost_usd: number;
}

export interface Paper {
  paper_id: string;
  title: string;
  abstract?: string;
  authors: string[];
  year?: number;
  doi?: string;
  url?: string;
  source: string;
}

export interface Evidence {
  evidence_id: string;
  paper_id: string;
  quote: string;
  relevance?: string;
}

export interface Claim {
  claim_id: string;
  text: string;
  evidence_ids: string[];
}

export interface Verification {
  claim_id: string;
  status: "SUPPORTED" | "PARTIAL" | "CONFLICT" | "UNSUPPORTED";
  confidence: number;
  rationale?: string;
}

export interface ResearchResult {
  status: RunStatus;
  report: { title: string; markdown: string };
  papers: Paper[];
  evidence: Evidence[];
  claims: Claim[];
  verifications: Verification[];
  warnings?: string[];
  budget?: {
    elapsed_ms?: number;
    estimated_cost_usd?: number;
    tool_calls_used?: number;
    total_tokens?: number;
  };
}

export interface RunSnapshot {
  run_id: string;
  status: RunStatus;
  created_at?: string;
  updated_at?: string;
  result?: ResearchResult | null;
  error?: string | null;
}

export interface RunEvent {
  event: string;
  details?: Record<string, unknown>;
  sequence?: number;
}

export interface IngestionResult {
  paper_id: string;
  title: string;
  passages_indexed: number;
  citation_edges_indexed: number;
  document_hash: string;
  parser: string;
  parser_version: string;
}

export interface RuntimeSummary {
  total_runs: number;
  active_runs: number;
  status_counts: Record<string, number>;
  total_papers: number;
  total_claims: number;
  total_workers: number;
  estimated_cost_usd: number;
}

export interface AuditEvent {
  event_id: string;
  tenant_id: string;
  actor: string;
  action: string;
  run_id?: string | null;
  created_at: string;
  details?: Record<string, unknown>;
}

export interface DeadLetter {
  message_id: string;
  run_id: string;
  error: string;
}

export interface McpServerStatus {
  name: string;
  url: string;
  connected: boolean;
  protocol_version?: string | null;
  server_name?: string | null;
  tools: string[];
  configured_tool_available: boolean;
  error_type?: string | null;
}
