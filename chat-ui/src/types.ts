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

/** GET /api/me/account. Assembled server-side so the browser never reimplements the access rules. */
export interface AccountLevel {
  value: number;
  label: string;
  description: string;
}

export interface AccountAttribute {
  name: string;
  label: string;
  description: string;
  match: string;
  /** Which token claim supplied it, for this issuer - the usual answer to "why is mine wrong?". */
  claim: string | null;
  values: string[];
  /** For a hierarchical attribute: what your values also reach (UK also reaches EMEA, Global). */
  also_reaches: string[];
  level: number | null;
  levels: AccountLevel[];
  required: boolean;
  present: boolean;
  meaning: string;
}

export interface AccountAppRole {
  value: string;
  display_name: string;
  description: string;
  held: boolean;
  grants: string[];
}

export interface AccountInfo {
  subject: string;
  display_name: string;
  issuer_kind: string;
  roles: string[];
  app_roles: AccountAppRole[];
  unrecognised_roles: string[];
  attributes: AccountAttribute[];
  summary: string;
  bypass: boolean;
  deny_all: boolean;
  policy_version: number;
}

export interface VocabularyValue {
  id: string;
  label: string;
  parent: string | null;
}

export interface Facet {
  label?: string;
  /** Aggregation over the index: what EXISTS and can be filtered on. Empty when nothing matching is indexed. */
  values: FacetValue[];
  /** The facet as configured in facets.yaml: what CAN be set. This is what an upload picker offers. */
  vocabulary?: VocabularyValue[];
  closed?: boolean;
  hierarchical?: boolean;
  multi?: boolean;
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

/** GET /api/uploads. `counts` is per status for the same filters MINUS the status filter, so it labels every tab. */
export interface UploadPage extends Page<DocumentRecord> {
  counts: Record<string, number>;
}

export interface UploadAccepted {
  tracking_id: string;
  doc_id: string;
  status: string;
  /** The folder path the server derived facets from, or null when none was sent. */
  relative_path?: string | null;
  facets?: Record<string, string[]>;
  /** Per facet: source_default, path_rule:<glob> or uploader. */
  facet_sources?: Record<string, string>;
  /** Facets the path rules supplied. Empty with relative_path set means no rule matched - the wrong-root case. */
  facets_from_path?: string[];
  /** Who may read it: the document's access tags. */
  access?: Record<string, string[] | number>;
  /** 'private' (Only me) or 'shared' (everyone the access tags match). */
  visibility?: 'private' | 'shared' | null;
}

/** GET /api/uploads/options - what the upload form offers this caller. */
export interface UploadOptions {
  only_me: { allowed: boolean; default: boolean };
  clearance: { name: string; label: string; levels: { value: number; label: string }[]; default: number | null; min: number | null } | null;
  required: { name: string; label: string }[];
  can_share_widely: boolean;
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

/**
 * Settings (Security): GET/PUT /api/admin/directory. Every list here is assembled server-side from the access
 * policy, so the page never carries a copy of what may be assigned.
 */
export interface DirectoryValue {
  value: string;
  label: string;
  description: string;
}

export interface DirectoryAttributeSpec {
  name: string;
  label: string;
  description: string;
  required: boolean;
  /** Whether an existing value may be removed; a required attribute cannot be. */
  clearable: boolean;
  values: DirectoryValue[];
}

export interface DirectoryRoleSpec {
  value: string;
  display_name: string;
  description: string;
  /** True for a role that also bypasses the document access filter; the API demands confirmation for it. */
  needs_confirmation: boolean;
}

export interface DirectoryCapability {
  enabled: boolean;
  attributes: DirectoryAttributeSpec[];
  app_roles: DirectoryRoleSpec[];
  warnings: string[];
  propagation_note: string;
}

export interface DirectoryRoleHeld {
  value: string;
  /** Set when the role comes from a group. Those cannot be removed from here. */
  via_group: string | null;
  removable: boolean;
  duplicates: number;
}

export interface DirectoryUserState {
  object_id: string;
  user_principal_name: string;
  display_name: string;
  mail: string | null;
  account_enabled: boolean;
  user_type: string;
  attributes: Record<string, string | null>;
  roles: DirectoryRoleHeld[];
  etag: string;
  propagation_note: string;
}

/** The PUT response: a partial write reports ok:false with what landed rather than failing the request. */
export interface DirectoryWriteResult extends DirectoryUserState {
  ok: boolean;
  applied: string[];
  failed: string[];
}

// ---------------------------------------------------------------- query traces (#/traces)

export type StageStatus = 'ok' | 'warn' | 'fail' | 'skipped';

export interface TraceStage {
  name: string;
  label: string;
  status: StageStatus;
  duration_ms: number | null;
  summary: string;
  data: Record<string, unknown>;
}

export interface AttributeCheck {
  passed: boolean;
  doc_values: string[] | number | null;
  caller_values: string[] | number | null;
  note: string;
}

export interface NearMissDoc {
  doc_id: string;
  chunk_id: string;
  title: string;
  path: string;
  page: number | null;
  score: number;
  reranker_score: number | null;
  allowed: boolean;
  /** Declared Only me at upload: withheld from everyone but its uploader on purpose. */
  private: boolean;
  checks: Record<string, AttributeCheck>;
  problems: string[];
}

export interface NearMiss {
  ran: boolean;
  skipped_reason: string;
  relevance_bar: Record<string, number>;
  docs: NearMissDoc[];
  below_bar: number;
}

export interface TraceRow {
  id: string;
  correlation_id: string | null;
  at: string;
  subject: string;
  display_name: string;
  question: string;
  outcome: 'answered' | 'refused' | 'error';
  reason: string | null;
  verdict: string;
  failed_stage: string | null;
  duration_ms: number;
  tokens: number;
  replay_of: string | null;
}

export interface QueryTrace extends TraceRow {
  issuer: string;
  roles: string[];
  attributes: Record<string, string[] | number>;
  filters: Record<string, string[]>;
  history_turns: number;
  answer: string;
  provider: string;
  model: string;
  stages: TraceStage[];
  near_miss: NearMiss;
  caller_problems: string[];
  diagnosis: string[];
  verdict_label: string;
  is_problem: boolean;
}

export interface TraceMeta {
  enabled: boolean;
  near_miss: boolean;
  retention_days: number;
  replay_hours: number;
  stages: { name: string; label: string }[];
  verdicts: { verdict: string; label: string; problem: boolean }[];
}

export interface HealthMetric {
  key: string;
  label: string;
  value: number | null;
  threshold: number;
  status: 'ok' | 'warn' | 'fail';
  detail: string;
}

export interface TraceSummary {
  since: string;
  minutes: number;
  total: number;
  replays: number;
  status: 'ok' | 'warn' | 'fail';
  metrics: HealthMetric[];
  by_verdict: { verdict: string; label: string; count: number; problem: boolean }[];
  by_reason: Record<string, number>;
  repeated_refusals: { subject: string; display_name: string; refusals: number; problems: number; last_at: string; last_trace_id: string }[];
  failing_expectations: { id: string; question: string; detail: string; last_trace_id: string | null }[];
  last_error: { id: string; at: string; failed_stage: string | null; reason: string | null } | null;
}

export interface Expectation {
  id: string;
  question: string;
  attributes: Record<string, string[] | number>;
  roles: string[];
  filters: Record<string, string[]>;
  expected: 'answer' | 'no_answer';
  required_doc_ids: string[];
  note: string;
  created_by: string;
  created_at: string;
  from_trace_id: string | null;
  last_result: 'pass' | 'fail' | null;
  last_detail: string;
  last_run_at: string | null;
  last_trace_id: string | null;
}
