export type SourceKey = "razorpay" | "bank" | "erp";

export interface SourceInfo {
  key: SourceKey;
  label: string;
  required_columns: string[];
  optional_columns: string[];
}

export interface DatabaseHealth {
  connected: boolean;
  latency_ms: number | null;
  error: string | null;
}

export interface AIHealth {
  reasoning_provider: string;
  reasoning_model: string;
  reasoning_mode: string;
  embedding_provider: string;
  embedding_model: string;
}

export interface HealthResponse {
  status: string;
  app_version: string;
  env: string;
  database: DatabaseHealth;
  ai: AIHealth;
}

export interface UploadResponse {
  ingestion_id: string;
  source: SourceKey;
  filename: string;
  checksum_sha256: string;
  rows_total: number;
  rows_inserted: number;
  rows_skipped_duplicate: number;
  rows_rejected: number;
  status: string;
  rejections_preview: string[];
}

export interface RunResponse {
  transactions_scanned: number;
  exact_auto_resolved: number;
  fuzzy_auto_resolved: number;
  incomplete_proposed: number;
  exceptions_opened: number;
  conflicts: Record<string, unknown>[];
  duration_ms: number;
  ai_candidates_evaluated: number;
  ai_auto_resolved: number;
  ai_proposed: number;
  ai_no_match: number;
  batch_candidates_generated: number;
  batch_ai_evaluated: number;
  batch_auto_resolved: number;
  batch_proposed: number;
  batch_no_match: number;
}

export interface TransactionRow {
  id: string;
  ingestion_id: string | null;
  source: SourceKey;
  external_ref: string;
  amount: string;
  direction: "credit" | "debit";
  transaction_type: "settlement" | "refund";
  currency: string;
  txn_date: string;
  narration: string | null;
  counterparty: string | null;
  status: string | null;
}

export interface TransactionPage {
  total: number;
  limit: number;
  offset: number;
  items: TransactionRow[];
}

export type QueueItemType = "exception" | "proposal";

export interface QueueItem {
  item_type: QueueItemType;
  id: string;
  title: string;
  status: string;
  priority: string;
  amount_impact: string | null;
  opened_at: string;
  refs: string[];
  confidence: string | null;
  match_type: string | null;
  exception_type: string | null;
  rationale: string | null;
}

export interface QueueResponse {
  items: QueueItem[];
  counts: { exceptions: number; proposals: number };
}

export interface TxnDetail {
  id: string;
  external_ref: string;
  source: SourceKey;
  amount: string;
  direction: "credit" | "debit";
  transaction_type: "settlement" | "refund";
  currency: string;
  txn_date: string;
  narration: string | null;
  counterparty: string | null;
  status: string | null;
  raw: Record<string, unknown>;
}

export interface Candidate {
  transaction_id: string;
  external_ref: string;
  source: SourceKey;
  amount: string;
  txn_date: string;
  narration: string | null;
  score: number;
}

export interface MatchSummary {
  id: string;
  match_type: string;
  confidence_score: string;
  status: string;
  resolved_by: string | null;
  decided_by: string | null;
  rationale: string | null;
}

export interface Recommendation {
  verdict: string | null;
  stage: string | null;
  confidence_score: string | null;
  similarity: number | null;
  similarity_autoresolve_min: number | null;
  floor_met: boolean | null;
  confidence_autoresolve_min: number | null;
  confidence_floor_met: boolean | null;
  blocked_reason: string | null;
  rationale: string | null;
  incomplete_reason: string | null;
  analysis: Analysis | null;
}

export type AnalysisClassification =
  | "likely_pending"
  | "data_quality"
  | "manual_investigation";

export interface BelowThresholdCandidate {
  transaction_id: string;
  external_ref: string;
  source: SourceKey;
  amount: string;
  txn_date: string;
  narration: string | null;
  similarity: number;
}

export interface Analysis {
  label: string;
  classification: AnalysisClassification;
  confidence: number | null;
  rationale: string;
  missing_sources: string[];
  below_threshold_candidates: BelowThresholdCandidate[];
  model: string | null;
}

export interface ExceptionDetail {
  id: string;
  exception_type: string;
  priority: string;
  status: string;
  amount_impact: string | null;
  opened_at: string;
  resolution_note: string | null;
  transaction: TxnDetail | null;
  original_transaction: TxnDetail | null;
  related_matches: MatchSummary[];
  candidates: Candidate[];
  recommendation: Recommendation | null;
  analysis_status: "ready" | "pending" | "none";
}

export interface MatchDetail {
  id: string;
  match_type: string;
  confidence_score: string;
  status: string;
  resolved_by: string | null;
  decided_by: string | null;
  rationale: string | null;
  proposed_at: string;
  resolved_at: string | null;
  participants: TxnDetail[];
  recommendation: Recommendation | null;
}

export interface ActionResponse {
  ok: boolean;
  message: string;
  match_id: string | null;
  exception_id: string | null;
  resolved_exceptions: number;
}

export interface AuditEntry {
  id: number;
  actor: string;
  action: string;
  entity_type: string;
  entity_id: string | null;
  before_state: Record<string, unknown> | null;
  after_state: Record<string, unknown> | null;
  details: Record<string, unknown> | null;
  request_id: string | null;
  created_at: string;
}

export interface DashboardSummary {
  open_exceptions_total: number;
  exceptions_by_type: Record<string, number>;
  exceptions_by_priority: Record<string, number>;
  proposals_awaiting_review: number;
  decisions_today_total: number;
  auto_resolved_today: number;
  human_resolved_today: number;
  exceptions_closed_today: number;
}

export interface PolicyLastChange {
  actor: string;
  at: string;
  before: unknown;
  after: unknown;
}

export interface PolicyField {
  key: string;
  group: string;
  label: string;
  description: string;
  unit: string;
  value_type: "bool" | "int" | "float" | "unknown";
  value: unknown;
  editable: boolean;
  last_changed: PolicyLastChange | null;
  updated_by: string | null;
}

export interface ReportSummary {
  generated_at: string;
  range: { from: string | null; to: string | null };
  matches: { match_type: string; status: string; resolved_by: string | null; count: number }[];
  exceptions: { exception_type: string; status: string; source: string; count: number }[];
  match_rate_by_source: {
    source: SourceKey;
    total_transactions: number;
    matched: number;
    rate: number;
  }[];
  auto_resolved_total: number;
  human_resolved_total: number;
  open_exception_aging: { under_7d: number; d7_30: number; over_30d: number };
  scope?: {
    ingestion_id: string;
    filename: string;
    source: SourceKey;
    rows_total: number;
    transactions_in_batch: number;
  };
  cross_batch_participants?: {
    match_id: string;
    match_type: string;
    transaction_id: string;
    external_ref: string;
    source: SourceKey;
    role: string;
    amount: string;
  }[];
}

export interface LoopCloseReport {
  generated_at: string;
  scope: {
    ingestion_id: string | null;
    filename: string | null;
    source: string | null;
    rows_total: number;
  } | null;
  records_scanned: number;
  matched: number;
  no_match: number;
  deferred: number;
  match_rate: number;
  match_rate_by_source: {
    source: SourceKey;
    total_transactions: number;
    matched: number;
    no_match: number;
    deferred: number;
    rate: number;
  }[];
  deferred_by_reason: Record<string, number>;
  exceptions: LoopCloseException[];
  throughput_records_per_second: number;
  execution_time_seconds: number;
  duration_ms: number;
  accuracy_available: boolean;
  decision_accuracy: number | null;
  matched_precision: number | null;
  matched_recall: number | null;
  matched_f1: number | null;
  no_match_precision: number | null;
  no_match_recall: number | null;
  deferred_precision: number | null;
  deferred_recall: number | null;
  records_evaluated: number;
  correct_predictions: number;
  total_errors: number;
}

export interface LoopCloseException {
  record_ref: string | null;
  reason_code: string;
  count: number;
}

export interface IngestionRecord {
  id: string;
  source: SourceKey;
  filename: string;
  rows_total: number;
  rows_inserted: number;
  rows_skipped_duplicate: number;
  rows_rejected: number;
  status: string;
  error_detail: string | null;
  created_at: string;
}

export interface ResolvedParticipant {
  transaction_id: string;
  external_ref: string;
  source: SourceKey;
  role: string;
  amount: string;
}

export interface ResolvedMatch {
  match_id: string;
  match_type: string;
  status: string;
  confidence_score: string;
  resolved_by: string | null;
  decided_by: string | null;
  rationale: string | null;
  proposed_at: string;
  resolved_at: string | null;
  participants: ResolvedParticipant[];
  stage: string | null;
  actor: string | null;
  action: string | null;
  note: string | null;
}

export interface ResolvedMatchesResponse {
  resolution: "auto" | "human";
  auto: number;
  human: number;
  items: ResolvedMatch[];
}

export interface ReviewedItem {
  item_type: "match" | "exception";
  id: string;
  status: string;
  action: string;
  actor: string | null;
  note: string | null;
  reviewed_at: string | null;
  match_type: string | null;
  confidence_score: string | null;
  participants: ResolvedParticipant[];
  rationale: string | null;
  exception_type: string | null;
  priority: string | null;
  amount_impact: string | null;
  transaction_ref: string | null;
}

export interface ReviewedItemsResponse {
  items: ReviewedItem[];
  match_count: number;
  exception_count: number;
}

export interface WebhookDelivery {
  id: number;
  action: "webhook.delivered" | "webhook.failed";
  actor: string;
  details: {
    url?: string;
    attempts?: number;
    batch_size?: number | null;
    errors?: string[] | null;
    status_code?: number;
  } | null;
  created_at: string;
}

export interface AnalyticsBucket {
  date: string;
  matches_created: number;
  auto_resolved: number;
  human_resolved: number;
  rejected: number;
  exceptions_opened: number;
  exceptions_resolved: number;
}

export interface AnalyticsOverview {
  generated_at: string;
  range: { from: string; to: string };
  buckets: AnalyticsBucket[];
  by_match_type: { match_type: string; count: number }[];
  resolution_split: { auto: number; human: number };
}

let currentActor = localStorage.getItem("recon.actor") ?? "";

export function getActor(): string {
  return currentActor;
}

export function setActor(name: string): void {
  currentActor = name.trim();
  if (currentActor) localStorage.setItem("recon.actor", currentActor);
  else localStorage.removeItem("recon.actor");
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  if (currentActor) headers.set("X-Actor", currentActor);
  const response = await fetch(path, { ...init, headers });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (body.detail) detail = String(body.detail);
    } catch {
      // keep statusText fallback
    }
    throw new Error(`API error ${response.status}: ${detail}`);
  }
  return (await response.json()) as T;
}

function queryString(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : "";
}

export interface ReviewQueueParams {
  [key: string]: string | number | undefined;
  item_type?: QueueItemType;
  status?: string;
  exception_type?: string;
  priority?: string;
  sort_by?: "amount_impact" | "opened_at";
  order?: "asc" | "desc";
}

export const api = {
  health: () => request<HealthResponse>("/api/v1/health"),

  sources: () => request<SourceInfo[]>("/api/v1/sources"),

  transactions: (params: { source?: SourceKey; limit?: number; offset?: number }) =>
    request<TransactionPage>(`/api/v1/transactions${queryString(params)}`),

  uploadCsv: (file: File, source: SourceKey) => {
    const form = new FormData();
    form.append("source", source);
    form.append("file", file);
    return request<UploadResponse>("/api/v1/uploads", {
      method: "POST",
      body: form,
    });
  },

  runReconciliation: () =>
    request<RunResponse>("/api/v1/reconciliation/run", {
      method: "POST",
      body: JSON.stringify({}),
    }),

  reviewQueue: (params: ReviewQueueParams = {}) =>
    request<QueueResponse>(`/api/v1/review-queue${queryString(params)}`),

  exceptionDetail: (id: string) =>
    request<ExceptionDetail>(`/api/v1/review-queue/exceptions/${id}`),

  exceptionAnalysis: (id: string) =>
    request<Analysis | null>(`/api/v1/review-queue/exceptions/${id}/analysis`),

  matchDetail: (id: string) =>
    request<MatchDetail>(`/api/v1/review-queue/matches/${id}`),

  approveMatch: (matchId: string, note = "") =>
    request<ActionResponse>(`/api/v1/review/matches/${matchId}/approve`, {
      method: "POST",
      body: JSON.stringify({ note }),
    }),

  rejectMatch: (matchId: string, note = "") =>
    request<ActionResponse>(`/api/v1/review/matches/${matchId}/reject`, {
      method: "POST",
      body: JSON.stringify({ note }),
    }),

  manualMatch: (transactionIds: string[], note = "", replaceProposedMatchId?: string) =>
    request<ActionResponse>("/api/v1/review/matches/manual", {
      method: "POST",
      body: JSON.stringify({
        transaction_ids: transactionIds,
        note,
        replace_proposed_match_id: replaceProposedMatchId ?? null,
      }),
    }),

  dismissException: (exceptionId: string, note = "") =>
    request<ActionResponse>(`/api/v1/review/exceptions/${exceptionId}/dismiss`, {
      method: "POST",
      body: JSON.stringify({ note }),
    }),

  escalateException: (exceptionId: string, note = "") =>
    request<ActionResponse>(`/api/v1/review/exceptions/${exceptionId}/escalate`, {
      method: "POST",
      body: JSON.stringify({ note }),
    }),

  dashboardSummary: () =>
    request<DashboardSummary>("/api/v1/dashboard/summary"),

  auditLogs: (params: { entity_id?: string; action?: string; limit?: number } = {}) =>
    request<AuditEntry[]>(`/api/v1/audit${queryString(params)}`),

  policy: () => request<PolicyField[]>("/api/v1/policy"),

  updatePolicy: (key: string, value: unknown) =>
    request<{ key: string; value: unknown; before: unknown; changed: boolean; actor: string }>(
      `/api/v1/policy/${encodeURIComponent(key)}`,
      { method: "PATCH", body: JSON.stringify({ value }) },
    ),

  policyHistory: (key: string) =>
    request<AuditEntry[]>(`/api/v1/policy/${encodeURIComponent(key)}/history`),

  reportSummary: (params: { from?: string; to?: string; ingestion_id?: string } = {}) =>
    request<ReportSummary>(`/api/v1/reports/summary${queryString(params)}`),

  loopCloseReport: (params: { ingestion_id?: string } = {}) =>
    request<LoopCloseReport>(`/api/v1/reports/loop-close${queryString(params)}`),

  ingestions: () => request<IngestionRecord[]>("/api/v1/ingestions"),

  resolvedMatches: (params: {
    resolution: "auto" | "human";
    match_type?: string;
    action?: string;
    actor?: string;
    sort_by?: "resolved_at" | "proposed_at" | "confidence_score";
    order?: "asc" | "desc";
    limit?: number;
  }) => request<ResolvedMatchesResponse>(`/api/v1/matches/resolved${queryString(params)}`),

  reviewedItems: (params: {
    action?: string;
    actor?: string;
    item_type?: "match" | "exception";
    limit?: number;
  } = {}) => request<ReviewedItemsResponse>(`/api/v1/reviewed${queryString(params)}`),

  erpPush: (url?: string) =>
    request<{ ok: boolean; pushed_items: number; attempts: number; url: string }>(
      "/api/v1/integrations/erp/push",
      { method: "POST", body: JSON.stringify(url ? { url } : {}) },
    ),

  erpDeliveries: (limit = 15) =>
    request<WebhookDelivery[]>(`/api/v1/integrations/erp/deliveries?limit=${limit}`),

  analytics: (params: { from?: string; to?: string } = {}) =>
    request<AnalyticsOverview>(`/api/v1/analytics/overview${queryString(params)}`),
};

/** Downloads a report blob with the actor header attached, then saves it. */
export async function downloadReport(
  format: "csv" | "pdf",
  params: { from?: string; to?: string; ingestion_id?: string },
): Promise<{ filename: string; size: number }> {
  const search = new URLSearchParams();
  if (params.from) search.set("from", params.from);
  if (params.to) search.set("to", params.to);
  if (params.ingestion_id) search.set("ingestion_id", params.ingestion_id);
  const qs = search.toString();
  const response = await fetch(`/api/v1/reports/export.${format}${qs ? `?${qs}` : ""}`, {
    headers: currentActor ? { "X-Actor": currentActor } : undefined,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (body.detail) detail = String(body.detail);
    } catch {
      // keep fallback
    }
    throw new Error(`report failed (${response.status}): ${detail}`);
  }
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const match = /filename="?([^";]+)"?/.exec(disposition);
  const filename = match?.[1] ?? `reconciliation-report.${format}`;
  const blob = await response.blob();
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(link.href);
  return { filename, size: blob.size };
}
