// Wire shapes of the RAG-OS API as consumed by the UI (see README "API contract").

export interface PublicConfig {
  /** 'entra' = sign in with Microsoft, 'dev' = local principal picker, 'none' = bring your own token. */
  auth_mode: 'entra' | 'dev' | 'none';
  embed_origins: string[];
  dev_auth_enabled: boolean;
  app_name: string;
  entra_tenant_id: string | null;
  entra_client_id: string | null;
  entra_api_scope: string | null;
}

export interface Principal {
  id: string;
  display_name: string;
  attributes: Record<string, unknown>;
  roles: string[];
}

export interface TokenResponse {
  token: string;
  expires_in: number;
}

export interface Me {
  subject: string;
  issuer_kind: string;
  display_name?: string | null;
  attributes: Record<string, unknown>;
  roles: string[];
}

export interface FacetValue {
  id: string;
  label?: string;
  count?: number;
}

export interface Facet {
  label?: string;
  values: FacetValue[];
}

export interface FacetsResponse {
  facets: Record<string, Facet>;
}

export interface Citation {
  index: number;
  doc_id: string;
  chunk_id: string;
  title?: string | null;
  path?: string | null;
  page?: number | null;
  heading?: string | null;
  score?: number | null;
  snippet?: string | null;
}

export interface Usage {
  input?: number;
  output?: number;
  cache_read?: number;
  cache_write?: number;
  embedding?: number;
  calls?: number;
  by_purpose?: Record<string, unknown>;
}

export interface ChatResponse {
  answer: string;
  refused: boolean;
  refusal_reason?: string | null;
  citations: Citation[];
  usage?: Usage;
  provider?: string;
  model?: string;
  timings_ms?: Record<string, number>;
  correlation_id?: string;
}

export interface ChatTurn {
  role: 'user' | 'assistant';
  content: string;
}

export type DocStatus =
  | 'DISCOVERED'
  | 'QUEUED'
  | 'PARSING'
  | 'CLASSIFIED'
  | 'CHUNKED'
  | 'EMBEDDED'
  | 'INDEXED'
  | 'SKIPPED_UNCHANGED'
  | 'FAILED'
  | 'DELETED';

export const PIPELINE_STATUSES: DocStatus[] = [
  'DISCOVERED',
  'QUEUED',
  'PARSING',
  'CLASSIFIED',
  'CHUNKED',
  'EMBEDDED',
  'INDEXED',
  'SKIPPED_UNCHANGED',
  'FAILED',
  'DELETED',
];

export const IN_FLIGHT_STATUSES: DocStatus[] = ['DISCOVERED', 'QUEUED', 'PARSING', 'CLASSIFIED', 'CHUNKED', 'EMBEDDED'];
export const TERMINAL_STATUSES = new Set<string>(['INDEXED', 'SKIPPED_UNCHANGED', 'FAILED', 'DELETED']);

export interface TagSet {
  facets: Record<string, string[]>;
  acl: Record<string, string[] | number>;
  sources: Record<string, string>;
}

export interface DocumentRecord {
  doc_id: string;
  source_id: string;
  item_id: string;
  path: string;
  status: DocStatus | string;
  stage?: string | null;
  attempts?: number;
  error_type?: string | null;
  error_message?: string | null;
  title?: string | null;
  content_type?: string | null;
  size?: number;
  tags?: TagSet;
  review_status?: string;
  chunk_count?: number;
  embedding_fp?: string | null;
  run_id?: string | null;
  tracking_id?: string | null;
  correlation_id?: string | null;
  discovered_at?: string | null;
  updated_at?: string | null;
  indexed_at?: string | null;
}

export interface Page<T> {
  items: T[];
  next: string | null;
}

export interface UploadAccepted {
  tracking_id: string;
  doc_id: string;
  status: string;
}

export interface LaneDepth {
  active: number;
  dead_letter: number;
}

export interface Controls {
  paused: boolean;
  max_concurrency: number;
  paused_sources: string[];
}

export interface IngestionSummary {
  totals: Record<string, number>;
  by_source: { source_id: string; status: string; count: number }[];
  by_facet: { value: string; status: string; count: number }[];
  queue: { priority: LaneDepth; bulk: LaneDepth };
  controls: Controls;
}

export interface Run {
  run_id: string;
  source_id: string;
  trigger: string;
  status: string;
  started_at?: string | null;
  finished_at?: string | null;
  discovered?: number;
  queued?: number;
  unchanged?: number;
  deleted?: number;
  error?: string | null;
}

export interface RunDetail {
  run: Run;
  progress: Record<string, number>;
  percent: number;
  throughput_per_min?: number | null;
  eta_seconds?: number | null;
  errors: { stage: string; error_type: string; count: number; example?: string | null }[];
}

export interface DocumentEvent {
  status: string;
  at: string;
  stage?: string | null;
  message?: string | null;
  correlation_id?: string | null;
}

export interface DocumentDetail {
  record: DocumentRecord;
  events: DocumentEvent[];
}

export interface DlqMessage {
  doc_id: string;
  version_key: string;
  source_id: string;
  lane: string;
  run_id?: string | null;
}

export interface Source {
  id: string;
  type: string;
  enabled: boolean;
  domain?: string | null;
  lane?: string | null;
  schedule?: string | null;
  last_run_started?: string | null;
}

export interface ConfigDoc {
  kind: string;
  yaml: string;
  etag: string;
}

export interface ExplainResult {
  filter: unknown;
  deny_all: boolean;
  bypass: boolean;
  attributes_used: unknown;
}
