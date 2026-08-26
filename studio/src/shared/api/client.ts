/* ═══════════════════════════════════════════════════════════════════
   API Client — typed wrapper around the Helix Engine REST API

   All calls go through /api which Vite proxies to localhost:8100.
   ═══════════════════════════════════════════════════════════════════ */

import type {
  DeployResponse,
  InstanceDetail,
  InstanceListResponse,
  ProcessListResponse,
} from "@shared/types";
import { getAccessToken, attemptRefresh } from "../../auth/tokenManager";

const BASE = "/api";

function authHeaders(token?: string): Record<string, string> {
  const t = token ?? getAccessToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

/** Open or save a document through an authenticated request.
 *
 * A plain `<a href="/api/v1/documents/{id}/download">` cannot be used: browser
 * navigation does not send the `Authorization` header, and since 2026-08-15
 * those routes require either a Bearer token (operators) or a signed grant
 * (portal customers). Before that they were unauthenticated, which is exactly
 * why the anchors worked — and why every document in the deployment was
 * readable by UUID.
 *
 * Portal code must NOT use this: it has no token, and the server already hands
 * it a `download_url` carrying a grant. Use that URL as-is.
 */
export async function openDocument(
  documentId: string,
  opts: { preview?: boolean; filename?: string } = {},
): Promise<void> {
  const path = opts.preview ? "preview" : "download";
  const res = await fetch(`/api/v1/documents/${documentId}/${path}`, {
    headers: { ...authHeaders() },
  });
  if (!res.ok) throw await errorFromResponse(res, `Document ${path} failed (${res.status})`);

  const url = URL.createObjectURL(await res.blob());
  try {
    if (opts.preview) {
      window.open(url, "_blank", "noreferrer");
    } else {
      const a = document.createElement("a");
      a.href = url;
      a.download = opts.filename || "document";
      document.body.appendChild(a);
      a.click();
      a.remove();
    }
  } finally {
    // Give the browser a moment to start the download/tab before revoking.
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  }
}

/** Parse a success response, tolerating an empty body.
 *
 * A 204 has no body by definition, so `res.json()` on one always rejects with
 * "Unexpected end of JSON input" — which surfaced as a *failed* delete even
 * though the server had already deleted the row, leaving the caller's list
 * stale and an error alert on screen. Every DELETE in this file returns 204.
 */
async function parseBody<T>(res: Response): Promise<T> {
  if (res.status === 204) return null as T;
  try {
    return await res.json();
  } catch {
    // An empty 200 lands here too. Either way the caller wanted a value that
    // is not there, and null says so better than a SyntaxError does.
    return null as T;
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  // ...options is spread FIRST: a caller passing its own `headers` must merge
  // with (not replace) the auth header — replacing it drops Authorization and
  // the resulting 401 force-logs-out the user via the global interceptor.
  const res = await fetch(`${BASE}${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...authHeaders(), ...options?.headers },
  });

  if (res.status === 401) {
    const newToken = await attemptRefresh();
    if (newToken) {
      const retry = await fetch(`${BASE}${path}`, {
        ...options,
        headers: { "Content-Type": "application/json", ...authHeaders(newToken), ...options?.headers },
      });
      if (retry.status === 401) {
        window.dispatchEvent(new Event("velaris:unauthorized"));
        throw new ApiError(401, "Session expired. Please log in again.");
      }
      if (!retry.ok) {
        const body = await retry.json().catch(() => ({ detail: retry.statusText }));
        throw new ApiError(retry.status, typeof body.detail === "object" ? JSON.stringify(body.detail) : (body.detail || retry.statusText), codedFrom(body));
      }
      return parseBody<T>(retry);
    }
    window.dispatchEvent(new Event("velaris:unauthorized"));
    throw new ApiError(401, "Session expired. Please log in again.");
  }

  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new ApiError(res.status, typeof body.detail === 'object' ? JSON.stringify(body.detail) : (body.detail || res.statusText), codedFrom(body));
  }

  return parseBody<T>(res);
}

export class ApiError extends Error {
  /**
   * Stable error code from `core/error_codes.py`, e.g. `VEL-DOC-STORAGE-503`.
   *
   * The backend has emitted `{"detail": …, "error": {"code", "docs"}}` since
   * the catalog was wired, but nothing read it — so the code appeared in the
   * response and the server log and then died at the network boundary, which
   * is the one place a user could have used it. Undefined for the many
   * subsystems that still raise a plain `HTTPException`.
   */
  code?: string;
  /** Deep link the server supplies alongside the code, e.g. `/docs/errors#VEL-…`. */
  docs?: string;

  constructor(public status: number, message: string, coded?: { code?: string; docs?: string }) {
    super(message);
    this.name = "ApiError";
    this.code = coded?.code;
    this.docs = coded?.docs;
  }
}

/**
 * Pull the coded-error block off a parsed error body.
 *
 * Defensive on purpose: this runs on a response that already failed, so a
 * body in an unexpected shape must yield "no code" rather than throw and
 * replace a useful HTTP error with a TypeError.
 */
/**
 * Build an ApiError from a failed Response, reading the coded block off it.
 *
 * Several modules predate the shared client and hand-roll their own fetch
 * helpers. Those threw `new Error(\`${url} → ${status}\`)` without ever reading
 * the body — so the error code the API returns died at the network boundary
 * and `<ErrorCodeLink>` had nothing to show. This gives them a one-line way to
 * keep it without adopting the whole client.
 *
 * Never throws: it runs on a response that has already failed, and a
 * non-JSON body (a gateway HTML page, say) must still yield a useful error
 * rather than a parse exception.
 */
export async function errorFromResponse(res: Response, fallback?: string): Promise<ApiError> {
  let body: any = null;
  try {
    body = await res.json();
  } catch {
    /* non-JSON error body — still an error, just an uncoded one */
  }
  const detail =
    typeof body?.detail === "string" ? body.detail
    : body?.detail ? JSON.stringify(body.detail)
    : fallback || res.statusText || `Request failed (${res.status})`;
  return new ApiError(res.status, detail, codedFrom(body));
}

export function codedFrom(body: any): { code?: string; docs?: string } | undefined {
  const e = body?.error;
  if (!e || typeof e !== "object") return undefined;
  const code = typeof e.code === "string" ? e.code : undefined;
  return code ? { code, docs: typeof e.docs === "string" ? e.docs : undefined } : undefined;
}

/* ── Processes ──────────────────────────────────────────────────── */

export async function listProcesses(): Promise<ProcessListResponse> {
  return request("/processes");
}

export async function getProcess(processId: string): Promise<DeployResponse> {
  return request(`/processes/${processId}`);
}

export async function deployProcess(
  bpmnXml: string,
  name?: string,
  tags?: Record<string, string>
): Promise<DeployResponse> {
  return request("/processes/deploy", {
    method: "POST",
    body: JSON.stringify({ bpmn_xml: bpmnXml, name, tags }),
  });
}

/* ── Instances ─────────────────────────────────────────────────── */

export async function listInstances(processId: string): Promise<InstanceListResponse> {
  return request(`/processes/${processId}/instances`);
}

export async function getInstance(
  processId: string,
  instanceId: string
): Promise<InstanceDetail> {
  return request(`/processes/${processId}/instances/${instanceId}`);
}

export async function startInstance(
  processId: string,
  variables?: Record<string, unknown>,
  businessKey?: string
): Promise<{ instance_id: string; status: string }> {
  return request(`/processes/${processId}/start`, {
    method: "POST",
    body: JSON.stringify({ variables: variables || {}, business_key: businessKey }),
  });
}

export async function cancelInstance(
  processId: string,
  instanceId: string
): Promise<InstanceDetail> {
  return request(`/processes/${processId}/instances/${instanceId}/cancel`, {
    method: "POST",
  });
}

/* ── Health ─────────────────────────────────────────────────────── */

export async function getHealth(): Promise<{ status: string }> {
  return request("/health");
}

export async function getReady(): Promise<{
  status: string;
  database: string;
  temporal: string;
}> {
  return request("/ready");
}

export async function deleteProcess(processId: string): Promise<void> {
  await request(`/processes/${processId}`, { method: "DELETE" });
}

export async function getProcessDetail(processId: string): Promise<DeployResponse & { bpmn_xml: string | null }> {
  return request(`/processes/${processId}`);
}

export async function completeUserTask(
  processId: string,
  instanceId: string,
  taskId: string,
  variables: Record<string, unknown>,
): Promise<InstanceDetail> {
  return request(`/processes/${processId}/instances/${instanceId}/complete-task`, {
    method: "POST",
    body: JSON.stringify({ task_id: taskId, variables }),
  });
}

export async function createProcessSchedule(
  processId: string,
  cron: string,
  variables?: Record<string, unknown>,
  description?: string,
): Promise<{ schedule_id: string; process_id: string; cron: string; status: string }> {
  return request(`/processes/${processId}/schedules`, {
    method: "POST",
    body: JSON.stringify({ cron, variables: variables || {}, description }),
  });
}

/* ═══════════════════════════════════════════════════════════════════
   Case Management API — calls go through /api/v1 → case-service:8200
   ═══════════════════════════════════════════════════════════════════ */

import type {
  CaseAssignment,
  CaseAuditEntry,
  CaseListResponse,
  CaseRelationship,
  CaseSummary,
  CaseTypeListResponse,
  CaseTypeSummary,
  QueueStats,
  SLAStatusInfo,
  WorkQueueSummary,
} from "@shared/types";

const CASE_BASE = "/api/v1";

/**
 * Join a caller-supplied path onto CASE_BASE, tolerating a path that already
 * carries the prefix.
 *
 * Callers are meant to pass `/risk/packs`, not `/api/v1/risk/packs`. Passing the
 * full path used to produce `/api/v1/api/v1/risk/packs` — a plain routing 404
 * that screens then misread as "feature not enabled", because HxSentinel's
 * dark-mode gate answers 404 by design and the two are indistinguishable by
 * status alone. Normalising here makes both spellings correct.
 */
function caseUrl(path: string): string {
  return path.startsWith(`${CASE_BASE}/`) ? path : `${CASE_BASE}${path}`;
}

async function caseRequest<T>(path: string, options?: RequestInit): Promise<T> {
  // Same spread order as request(): options first, headers last — a caller's
  // `headers` must never replace the merged object and drop Authorization.
  const res = await fetch(caseUrl(path), {
    ...options,
    headers: { "Content-Type": "application/json", ...authHeaders(), ...options?.headers },
  });

  if (res.status === 401) {
    const newToken = await attemptRefresh();
    if (newToken) {
      const retry = await fetch(caseUrl(path), {
        ...options,
        headers: { "Content-Type": "application/json", ...authHeaders(newToken), ...options?.headers },
      });
      if (retry.status === 401) {
        window.dispatchEvent(new Event("velaris:unauthorized"));
        throw new ApiError(401, "Session expired. Please log in again.");
      }
      if (!retry.ok) {
        const body = await retry.json().catch(() => ({ detail: retry.statusText }));
        throw new ApiError(retry.status, body.detail || retry.statusText, codedFrom(body));
      }
      return parseBody<T>(retry);
    }
    window.dispatchEvent(new Event("velaris:unauthorized"));
    throw new ApiError(401, "Session expired. Please log in again.");
  }

  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new ApiError(res.status, body.detail || res.statusText, codedFrom(body));
  }

  return parseBody<T>(res);
}

/* ── HxStream ──────────────────────────────────────────────────── */

/** Fire-and-forget UI interaction event — never throws. */
export function emitUIEvent(
  action: string,
  payload: Record<string, unknown> = {},
  caseId?: string,
): void {
  fetch(`${CASE_BASE}/hxstream/event`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({
      event_type: "ui_interaction",
      case_id: caseId ?? null,
      payload: { action, ...payload },
    }),
  }).catch(() => { /* swallow — HxStream must never break the UI */ });
}

/* ── Case Types ────────────────────────────────────────────────── */

export async function listCaseTypes(page = 1, tenantId?: string): Promise<CaseTypeListResponse> {
  const qs = new URLSearchParams({ page: String(page) });
  if (tenantId) qs.set("tenant_id", tenantId);
  return caseRequest(`/case-types?${qs}`);
}

export async function getCaseType(id: string): Promise<CaseTypeSummary> {
  return caseRequest(`/case-types/${id}`);
}

export async function getDataModel(id: string): Promise<{ id: string; name: string; definition_json: { fields: { name: string; field_type: string }[] } }> {
  return caseRequest(`/data-models/${id}`);
}

export async function deployCaseType(body: {
  name: string;
  version: string;
  tenant_id?: string | null;      // null / undefined = global
  lifecycle_process_id?: string | null;
  definition_json: Record<string, unknown>;
  default_priority?: string;
  description?: string;
  tags?: string[];
  icon?: string;
  color?: string;
}): Promise<CaseTypeSummary> {
  return caseRequest("/case-types", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function deleteCaseType(id: string): Promise<void> {
  await caseRequest(`/case-types/${id}`, { method: "DELETE" });
}

/* ── Cases ─────────────────────────────────────────────────────── */

export async function listCases(params?: {
  status?: string;
  priority?: string;
  case_type_id?: string;
  /** Indexed-variable filters, each "namespace.name:value" (AND semantics) */
  vars?: string[];
  page?: number;
  page_size?: number;
}): Promise<CaseListResponse> {
  const qs = new URLSearchParams();
  if (params?.status) qs.set("status", params.status);
  if (params?.priority) qs.set("priority", params.priority);
  if (params?.case_type_id) qs.set("case_type_id", params.case_type_id);
  for (const v of params?.vars ?? []) qs.append("var", v);
  if (params?.page) qs.set("page", String(params.page));
  if (params?.page_size) qs.set("page_size", String(params.page_size));
  const q = qs.toString();
  return caseRequest(`/cases${q ? `?${q}` : ""}`);
}

export async function getCase(id: string): Promise<CaseSummary> {
  return caseRequest(`/cases/${id}`);
}

/**
 * Everything the case detail view needs, in one request.
 *
 * Replaces five calls — case, history, SLA, case type, my-task — one of which
 * was a real waterfall: the case type could not be requested until the case had
 * returned its `case_type_id`.
 *
 * The individual getters stay exported and live; other callers still use them,
 * and marketplace packages call the REST paths directly. This is additive.
 */
export async function getCaseDetail(id: string): Promise<{
  case: CaseSummary;
  history: CaseAuditEntry[];
  slas: SLAStatusInfo[];
  case_type: CaseTypeSummary | null;
  my_task: MyTaskResult | null;
}> {
  return caseRequest(`/cases/${id}/detail`);
}

export async function createCase(body: {
  case_type_id: string;
  data?: Record<string, unknown>;
  priority?: string;
}): Promise<CaseSummary> {
  return caseRequest("/cases", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function updateCaseData(
  id: string,
  data: Record<string, unknown>
): Promise<CaseSummary> {
  return caseRequest(`/cases/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ data }),
  });
}

export async function changeCaseStatus(
  id: string,
  status: string,
  reason?: string
): Promise<CaseSummary> {
  return caseRequest(`/cases/${id}/status`, {
    method: "POST",
    body: JSON.stringify({ status, reason }),
  });
}

export async function changeCasePriority(
  id: string,
  priority: string
): Promise<CaseSummary> {
  return caseRequest(`/cases/${id}/priority`, {
    method: "POST",
    body: JSON.stringify({ priority }),
  });
}

export async function resolveCase(
  id: string,
  resolution?: Record<string, unknown>
): Promise<CaseSummary> {
  return caseRequest(`/cases/${id}/resolve`, {
    method: "POST",
    body: JSON.stringify({ resolution }),
  });
}

export async function closeCase(id: string): Promise<CaseSummary> {
  return caseRequest(`/cases/${id}/close`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export async function reopenCase(id: string): Promise<CaseSummary> {
  return caseRequest(`/cases/${id}/reopen`, { method: "POST", body: JSON.stringify({}) });
}

export async function cancelCase(id: string, reason?: string): Promise<CaseSummary> {
  return caseRequest(`/cases/${id}/cancel`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

export async function transitionStage(
  id: string,
  targetStageId: string
): Promise<CaseSummary> {
  return caseRequest(`/cases/${id}/stage`, {
    method: "POST",
    body: JSON.stringify({ target_stage_id: targetStageId }),
  });
}

export async function getCaseHistory(id: string): Promise<CaseAuditEntry[]> {
  return caseRequest(`/cases/${id}/history`);
}

// P38 — Step completions
export async function completeStep(
  caseId: string,
  stepId: string,
  body: { stage_id: string; step_type: string; status: string; data: Record<string, unknown> }
): Promise<{ auto_advanced: boolean; advance_held_by_risk?: boolean; status: string; [k: string]: unknown }> {
  return caseRequest(`/cases/${caseId}/steps/${stepId}/complete`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function listStepCompletions(
  caseId: string,
  stageId?: string
): Promise<Array<{ step_id: string; status: string; data: Record<string, unknown>; step_type: string }>> {
  const qs = stageId ? `?stage_id=${stageId}` : "";
  return caseRequest(`/cases/${caseId}/step-completions${qs}`);
}

export interface MyTaskResult {
  assignment_id: string;
  case_id: string;
  case_number: string | null;
  case_description: string | null;
  case_priority: string | null;
  stage_id: string;
  step_id: string;
  step_def: Record<string, unknown>;
  form_id: string | null;
  completion: Record<string, unknown> | null;
  locked_by: string | null;
  lock_expires_at: string | null;
  is_locked_by_me: boolean;
}

export async function getMyTask(caseId: string): Promise<MyTaskResult | null> {
  const res = await fetch(`${BASE}/v1/cases/${caseId}/my-task`, {
    headers: { "Content-Type": "application/json", ...authHeaders() },
  });
  if (!res.ok) return null;
  const json = await res.json();
  return json ?? null;
}

export async function unlockStep(caseId: string, stepId: string): Promise<void> {
  await fetch(`${BASE}/v1/cases/${caseId}/steps/${encodeURIComponent(stepId)}/unlock`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
  });
}

export async function getCaseRelationships(id: string): Promise<CaseRelationship[]> {
  return caseRequest(`/cases/${id}/relationships`);
}

export async function getCaseChildren(id: string): Promise<CaseSummary[]> {
  return caseRequest(`/cases/${id}/children`);
}

export async function getCaseSLA(id: string): Promise<SLAStatusInfo[]> {
  return caseRequest(`/cases/${id}/sla`);
}

/* ── Assignments ───────────────────────────────────────────────── */

export async function getMyAssignments(): Promise<CaseAssignment[]> {
  return caseRequest("/my/assignments");
}

export async function getMyWorkload(): Promise<{
  user_id: string;
  active_count: number;
  assignment_ids: string[];
}> {
  return caseRequest("/my/workload");
}

export async function claimAssignment(
  assignmentId: string,
  userId: string
): Promise<CaseAssignment> {
  return caseRequest(`/assignments/${assignmentId}/claim`, {
    method: "POST",
    body: JSON.stringify({ user_id: userId }),
  });
}

export async function dismissAssignment(assignmentId: string): Promise<void> {
  await caseRequest(`/assignments/${assignmentId}`, { method: "DELETE" });
}

export async function completeAssignment(
  assignmentId: string,
  result?: Record<string, unknown>
): Promise<CaseAssignment> {
  return caseRequest(`/assignments/${assignmentId}/complete`, {
    method: "POST",
    body: JSON.stringify({ result }),
  });
}

/* ── Work Queues ───────────────────────────────────────────────── */

export async function listQueues(tenantId?: string): Promise<WorkQueueSummary[]> {
  const qs = tenantId ? `?tenant_id=${tenantId}` : "";
  return caseRequest(`/queues${qs}`);
}

export async function getQueueItems(
  queueId: string,
  page = 1,
  pageSize = 50,
  status?: string,
  search?: string,
): Promise<CaseAssignment[]> {
  const p = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  if (status) p.set("status", status);
  if (search)  p.set("search", search);
  return caseRequest(`/queues/${queueId}/items?${p}`);
}

export async function getQueueItemCount(
  queueId: string,
  status?: string,
  search?: string,
): Promise<{ count: number }> {
  const p = new URLSearchParams();
  if (status) p.set("status", status);
  if (search)  p.set("search", search);
  return caseRequest(`/queues/${queueId}/items/count?${p}`);
}

export async function getQueueStats(queueId: string): Promise<QueueStats> {
  return caseRequest(`/queues/${queueId}/stats`);
}

/* ── Forms (Phase 7) ───────────────────────────────────────────── */

export async function listForms(): Promise<any> {
  return caseRequest("/forms");
}

export async function getForm(formId: string): Promise<any> {
  return caseRequest(`/forms/${formId}`);
}

export async function createForm(body: {
  name: string;
  version: string;
  data_model_id?: string;
  definition_json: Record<string, unknown>;
}): Promise<any> {
  return caseRequest("/forms", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function updateForm(formId: string, body: {
  definition_json?: Record<string, unknown>;
  data_model_id?: string;
  version?: string;
}): Promise<any> {
  return caseRequest(`/forms/${formId}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export async function deleteForm(formId: string): Promise<void> {
  await caseRequest(`/forms/${formId}`, { method: "DELETE" });
}

export async function getAssignmentForm(assignmentId: string): Promise<{
  has_form: boolean;
  form: { id: string; name: string; version: string; definition_json: Record<string, any> } | null;
}> {
  return caseRequest(`/form-submissions/${assignmentId}/form`);
}

export async function getCaseVariables(caseId: string): Promise<{
  case_id: string;
  variables: Record<string, any>;
}> {
  return caseRequest(`/variables/cases/${caseId}`);
}

export async function listCaseShares(caseId: string): Promise<
  { user_id: string; relation: string; username?: string | null; display_name?: string | null; created_by: string | null; created_at: string | null }[]
> {
  return caseRequest(`/cases/${caseId}/shares`);
}

export async function shareCase(caseId: string, userId: string, relation: string): Promise<void> {
  await caseRequest(`/cases/${caseId}/shares`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: userId, relation }),
  });
}

export async function unshareCase(caseId: string, userId: string, relation: string): Promise<void> {
  await caseRequest(`/cases/${caseId}/shares?user_id=${encodeURIComponent(userId)}&relation=${encodeURIComponent(relation)}`, {
    method: "DELETE",
  });
}

/* ── HxMeet — real-time case sessions ─────────────────────────── */

export interface CaseSession {
  id: string;
  case_id: string;
  driver: string;
  provider: string;
  status: string;
  record_intent?: boolean;
  recording_status?: string;   // none | recording | processing | sealed | failed
  recording_document_id?: string | null;
  transcript_status?: string;  // none | sealed | failed (P4a-live-2)
  transcript_document_id?: string | null;
  title: string | null;
  join_url: string | null;
  external_meeting_id: string | null;
  started_by: string;
  started_at: string | null;
  ended_at: string | null;
  created_at: string | null;
}

export interface MeetProvider {
  connector_id: string;
  name: string;
  provider: string;
  is_default: boolean;
}

export interface MeetRoomToken {
  url: string;
  token: string;
  room: string;
  identity: string;
  display_name?: string | null;
  title?: string | null;
  session_id?: string;
  record_intent?: boolean;
}

export interface MeetGuestInvite {
  participant_id: string;
  identity: string;
  invite_token: string;
  join_path: string;
  /** Absolute, when the deployment has said where Studio lives
   *  (`VELARIS_CASE_PUBLIC_BASE_URL`). Null otherwise — the browser's own
   *  origin is the fallback, which is right for a link a host copies out and
   *  wrong for one a server sends on. */
  join_url: string | null;
  expires_at: string;
}

export interface MeetParticipant {
  id: string;
  identity: string;
  display_name: string | null;
  role: string;
  invited_by: string | null;
  joined_at: string | null;
  left_at: string | null;
}

export async function listMeetProviders(): Promise<{
  driver: string; embedded_available: boolean; providers: MeetProvider[];
}> {
  return caseRequest(`/meet/providers`);
}

export async function getMeetSessionToken(sessionId: string): Promise<MeetRoomToken> {
  return caseRequest(`/meet/sessions/${sessionId}/token`, { method: "POST" });
}

export async function startMeetRecording(sessionId: string): Promise<CaseSession> {
  return caseRequest(`/meet/sessions/${sessionId}/recording/start`, { method: "POST" });
}

export async function stopMeetRecording(sessionId: string): Promise<CaseSession> {
  return caseRequest(`/meet/sessions/${sessionId}/recording/stop`, { method: "POST" });
}

export async function verifyMeetRecording(sessionId: string): Promise<{
  verified: boolean; sha256?: string; sealed_sha256?: string | null; anchor_ref?: string; reason?: string;
}> {
  return caseRequest(`/meet/sessions/${sessionId}/recording/verify`);
}

// ── HxMeet P4a — session intelligence (local transcription + summary) ──

export type SessionIntelligence = {
  status: "none" | "pending" | "running" | "completed" | "failed";
  transcript_document_id?: string | null;
  summary?: string | null;
  action_items?: string[];
  language?: string | null;
  duration_seconds?: number | null;
  model_versions?: Record<string, string>;
  error?: string | null;
  completed_at?: string | null;
};

export async function runSessionIntelligence(sessionId: string): Promise<{ status: string }> {
  return caseRequest(`/meet/sessions/${sessionId}/intelligence`, { method: "POST" });
}

export async function getSessionIntelligence(sessionId: string): Promise<SessionIntelligence> {
  return caseRequest(`/meet/sessions/${sessionId}/intelligence`);
}

// ── HxMeet P4b — document verification (the document-first KYC gate) ──

export type DocCheck = { name: string; result: "pass" | "fail" | "skipped"; detail: string };

export async function verifyDocument(documentId: string, body: {
  status: "passed" | "failed" | "review";
  mrz_line2?: string; expiry_date?: string;
  checklist?: Record<string, boolean>; notes?: string;
}): Promise<{ id: string; status: string; checks: DocCheck[] }> {
  return caseRequest(`/documents/${documentId}/verify`, { method: "POST", body: JSON.stringify(body) });
}

export async function listDocumentVerifications(documentId: string): Promise<{
  verifications: { id: string; status: string; checks: DocCheck[]; verified_by: string; notes: string | null; created_at: string | null }[];
}> {
  return caseRequest(`/documents/${documentId}/verifications`);
}

// ── HxMeet P4c — Video-KYC liveness challenges ──

export type MeetChallenge = {
  id: string;
  session_id: string;
  kind: "head_turn" | "phrase_readback" | "document_tilt";
  payload: { instruction?: string; phrase?: string; sequence?: string[]; side?: string };
  issued_by: string;
  issued_at: string | null;
  result: "pending" | "passed" | "failed" | "skipped";
  result_notes: string | null;
  result_by: string | null;
  result_at: string | null;
};

export async function issueMeetChallenges(sessionId: string, kinds?: string[]): Promise<{ challenges: MeetChallenge[] }> {
  return caseRequest(`/meet/sessions/${sessionId}/challenges`, {
    method: "POST", body: JSON.stringify({ kinds: kinds ?? null }),
  });
}

export async function listMeetChallenges(sessionId: string): Promise<{ challenges: MeetChallenge[] }> {
  return caseRequest(`/meet/sessions/${sessionId}/challenges`);
}

export async function recordMeetChallengeResult(
  sessionId: string, challengeId: string, result: "passed" | "failed" | "skipped", notes?: string,
): Promise<MeetChallenge> {
  return caseRequest(`/meet/sessions/${sessionId}/challenges/${challengeId}/result`, {
    method: "POST", body: JSON.stringify({ result, notes: notes ?? null }),
  });
}

export type KycCheck = {
  name: string; score?: number; skipped?: boolean; detail: string; model?: string; challenge_id?: string;
};

export type KycAnalysis = {
  status: "none" | "pending" | "running" | "completed" | "failed";
  risk_score?: number | null;
  review_recommended?: boolean | null;
  review_threshold?: number;
  checks?: KycCheck[];
  challenge_checks?: KycCheck[];
  model_versions?: Record<string, string>;
  error?: string | null;
  completed_at?: string | null;
};

export async function runKycAnalysis(sessionId: string): Promise<{ status: string }> {
  return caseRequest(`/meet/sessions/${sessionId}/kyc-analysis`, { method: "POST" });
}

export async function getKycAnalysis(sessionId: string): Promise<KycAnalysis> {
  return caseRequest(`/meet/sessions/${sessionId}/kyc-analysis`);
}

/** Public, non-consuming invite preview — recording notice before consent. */
export async function previewMeetGuestInvite(inviteToken: string): Promise<{
  title: string | null; display_name: string | null; record_intent: boolean;
  biometric_notice?: boolean;
}> {
  const res = await fetch(`/api/v1/meet/guest/preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ invite_token: inviteToken }),
  });
  if (!res.ok) throw new ApiError(res.status, "This invite link is invalid, already used, or expired.");
  return res.json();
}

export async function inviteMeetGuest(
  sessionId: string,
  body: { customer_id?: string; email?: string; display_name?: string },
): Promise<MeetGuestInvite> {
  return caseRequest(`/meet/sessions/${sessionId}/invites`, {
    method: "POST", body: JSON.stringify(body),
  });
}

export async function listMeetParticipants(
  sessionId: string,
): Promise<{ participants: MeetParticipant[] }> {
  return caseRequest(`/meet/sessions/${sessionId}/participants`);
}

/** Public guest exchange — no auth header on purpose (guests have no account). */
export async function exchangeMeetGuestToken(inviteToken: string): Promise<MeetRoomToken> {
  const res = await fetch(`/api/v1/meet/guest/token`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ invite_token: inviteToken }),
  });
  if (!res.ok) throw new ApiError(res.status, res.status === 404
    ? "This invite link is invalid, already used, or expired."
    : "Could not join the session.");
  return res.json();
}

export async function listCaseSessions(caseId: string): Promise<{ sessions: CaseSession[] }> {
  return caseRequest(`/meet/cases/${caseId}/sessions`);
}

export async function startCaseSession(
  caseId: string,
  body: { title?: string; provider?: string; connector_id?: string },
): Promise<CaseSession> {
  return caseRequest(`/meet/cases/${caseId}/sessions`, { method: "POST", body: JSON.stringify(body) });
}

export async function endCaseSession(sessionId: string, cancelled = false): Promise<CaseSession> {
  return caseRequest(`/meet/sessions/${sessionId}/end?cancelled=${cancelled}`, { method: "POST" });
}

/** Sealed-recording stream for the in-Studio player (meet.recording.view).
 * Returns a blob object-URL — caller MUST URL.revokeObjectURL() when done. */
export async function fetchRecordingUrl(sessionId: string): Promise<string> {
  const res = await fetch(`/api/v1/meet/sessions/${sessionId}/recording`, {
    headers: { ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}) },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new ApiError(res.status, body.detail || res.statusText, codedFrom(body));
  }
  return URL.createObjectURL(await res.blob());
}

/** Sealed live transcript, unsealed server-side (meet.recording.view; every
 * fetch is audited like a recording view). Returns plain text. */
export async function fetchSessionTranscript(sessionId: string): Promise<string> {
  const res = await fetch(`/api/v1/meet/sessions/${sessionId}/transcript`, {
    headers: { ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}) },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new ApiError(res.status, body.detail || res.statusText, codedFrom(body));
  }
  return res.text();
}

export async function verifySessionTranscript(sessionId: string): Promise<{
  verified: boolean; sha256?: string; sealed_sha256?: string; anchor_ref?: string; reason?: string;
}> {
  return caseRequest(`/meet/sessions/${sessionId}/transcript/verify`);
}

// ── HxNexus case-scoped Q&A ────────────────────────────────────────

export type CaseAskResult = {
  answer: string;
  sources: { sid: string; kind: string; label: string }[];
  withheld: string[];
  external_ai: boolean;
};

export async function askCase(caseId: string, question: string): Promise<CaseAskResult> {
  return caseRequest(`/hxnexus/cases/${caseId}/ask`, {
    method: "POST", body: JSON.stringify({ question }),
  });
}

// ── Case messages (Portal v2 P4 — worker ↔ customer thread) ────────

export type CaseMessage = {
  id: string; author: string; author_name: string | null;
  body: string; portal_visible: boolean; created_at: string | null;
};

export async function listCaseMessages(caseId: string): Promise<{ messages: CaseMessage[] }> {
  return caseRequest(`/cases/${caseId}/messages`);
}

export async function postCaseMessage(caseId: string, body: string, portalVisible = true): Promise<CaseMessage> {
  return caseRequest(`/cases/${caseId}/messages`, {
    method: "POST",
    body: JSON.stringify({ body, portal_visible: portalVisible }),
  });
}

export async function submitForm(assignmentId: string, body: {
  form_id: string;
  values: Record<string, any>;
  completed_by?: string;
}): Promise<any> {
  return caseRequest(`/form-submissions/${assignmentId}`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function getAssignmentsForCase(caseId: string): Promise<any[]> {
  return caseRequest(`/cases/${caseId}/assignments`);
}

/* ── Analytics (Phase 9) ───────────────────────────────────────── */

export interface AnalyticsDashboard {
  overview: {
    total_cases: number;
    open_cases: number;
    resolved_cases: number;
    closed_cases: number;
    cancelled_cases: number;
    avg_resolution_hours: number | null;
    cases_created_today: number;
    cases_resolved_today: number;
  };
  status_breakdown: { status: string; count: number }[];
  priority_breakdown: { priority: string; count: number }[];
  case_type_breakdown: { case_type_id: string; case_type_name: string; count: number }[];
  sla_compliance: {
    total_sla_instances: number;
    on_track: number;
    at_risk: number;
    breached: number;
    paused: number;
    compliance_rate: number;
  };
  assignments: {
    total_assignments: number;
    active: number;
    completed: number;
    avg_completion_hours: number | null;
    unassigned: number;
  };
  cases_over_time: { date: string; count: number }[];
  resolved_over_time: { date: string; count: number }[];
}

export async function getAnalyticsDashboard(params?: {
  days?: number;
  case_type_id?: string;
  tenant_id?: string;
}): Promise<AnalyticsDashboard> {
  const qs = new URLSearchParams();
  if (params?.days) qs.set("days", String(params.days));
  if (params?.case_type_id) qs.set("case_type_id", params.case_type_id);
  if (params?.tenant_id) qs.set("tenant_id", params.tenant_id);
  const q = qs.toString();
  return caseRequest(`/analytics/dashboard${q ? `?${q}` : ""}`);
}

/* ── Admin (Phase 11) ──────────────────────────────────────────── */

export async function getSystemInfo(): Promise<any> {
  return caseRequest("/admin/system-info");
}

export async function searchAuditLog(params?: {
  action?: string;
  actor_id?: string;
  case_id?: string;
  days?: number;
  page?: number;
  page_size?: number;
}): Promise<any> {
  const qs = new URLSearchParams();
  if (params?.action) qs.set("action", params.action);
  if (params?.actor_id) qs.set("actor_id", params.actor_id);
  if (params?.case_id) qs.set("case_id", params.case_id);
  if (params?.days) qs.set("days", String(params.days));
  if (params?.page) qs.set("page", String(params.page));
  if (params?.page_size) qs.set("page_size", String(params.page_size));
  const q = qs.toString();
  return caseRequest(`/admin/audit-log${q ? `?${q}` : ""}`);
}

export async function getAuditActions(): Promise<{ actions: string[] }> {
  return caseRequest("/admin/audit-log/actions");
}

export async function createQueue(body: Record<string, any>): Promise<any> {
  return caseRequest("/admin/queues", { method: "POST", body: JSON.stringify(body) });
}

export async function updateQueue(queueId: string, body: Record<string, any>): Promise<any> {
  return caseRequest(`/admin/queues/${queueId}`, { method: "PATCH", body: JSON.stringify(body) });
}

export async function deleteQueue(queueId: string): Promise<void> {
  await caseRequest(`/admin/queues/${queueId}`, { method: "DELETE" });
}

export async function listWebhooks(): Promise<any[]> {
  return caseRequest("/webhooks");
}

export async function createWebhook(body: {
  name: string; url: string; events: string[];
  secret?: string; case_type_id?: string;
  headers?: Record<string, string>; retry_count?: number; timeout_seconds?: number;
}): Promise<any> {
  return caseRequest("/webhooks", { method: "POST", body: JSON.stringify(body) });
}

export async function updateWebhook(id: string, body: Record<string, any>): Promise<any> {
  return caseRequest(`/webhooks/${id}`, { method: "PATCH", body: JSON.stringify(body) });
}

export async function testWebhook(id: string): Promise<{ success: boolean; status_code?: number; error?: string }> {
  return caseRequest(`/webhooks/${id}/test`, { method: "POST" });
}

export async function listWebhookEvents(): Promise<{ events: string[] }> {
  return caseRequest("/webhooks/events");
}

export async function deleteWebhook(id: string): Promise<void> {
  await caseRequest(`/webhooks/${id}`, { method: "DELETE" });
}

// ── SSO providers (Admin Console → SSO) ──────────────────────────────────────
// The admin listing is a different endpoint from the one the login page calls:
// that one is anonymous and withholds tenant_id, config and disabled rows.
export interface SamlCertHealth {
  fingerprint: string;
  not_after: string | null;
  days_remaining: number | null;
  expired: boolean;
}

export interface SsoProviderAdmin {
  id: string;
  provider: string;
  client_id: string;
  enabled: boolean;
  tenant_id: string | null;
  config: Record<string, any>;
  has_secret: boolean;
  created_at: string | null;
  /** SAML rows only. Certificate expiry is computed server-side — it means
   *  parsing DER, and the screen that warns about an expiring certificate must
   *  not be the thing that breaks when one cannot be read. */
  saml_health?: {
    certs: SamlCertHealth[];
    metadata_sync: {
      last_checked?: string; last_error?: string | null; last_error_at?: string;
      certs_added?: number; drift?: Record<string, { configured: string; published: string }> | null;
    } | null;
    /** The SP's own key pair — a summary, never the key. */
    sp_key: (SamlCertHealth & { created_at: string | null }) | null;
  };
}

/** Create (or replace) the SP key pair. Returns the certificate to hand the IdP. */
export async function generateSamlSpKey(tenant: string, regenerate = false): Promise<{
  certificate: string;
  summary: (SamlCertHealth & { created_at: string | null }) | null;
  next_step: string;
}> {
  return caseRequest(`/auth/saml/${encodeURIComponent(tenant)}/sp-key`,
    { method: "POST", body: JSON.stringify({ regenerate }) });
}

export interface SamlMetadata {
  idp_entity_id: string;
  idp_sso_url: string;
  idp_slo_url: string | null;
  idp_x509_certs: string[];
  name_id_format: string | null;
  certs: SamlCertHealth[];
  source: "url" | "xml";
}

/** Read an IdP's federation metadata. Returns the fields; saves nothing. */
export async function parseSamlMetadata(body: { url?: string; xml?: string }): Promise<SamlMetadata> {
  return caseRequest("/auth/saml/metadata/parse", { method: "POST", body: JSON.stringify(body) });
}

export async function listSsoProvidersAdmin(): Promise<SsoProviderAdmin[]> {
  const d = await caseRequest<{ providers: SsoProviderAdmin[] }>("/auth/real/sso/providers/manage");
  return d.providers ?? [];
}

export async function createSsoProvider(body: {
  provider: string; client_id: string; client_secret: string;
  tenant_id?: string | null; config?: Record<string, any>;
}): Promise<SsoProviderAdmin> {
  return caseRequest("/auth/real/sso/providers", { method: "POST", body: JSON.stringify(body) });
}

/** Omit `client_secret` (or send it blank) to keep the stored one. */
export async function updateSsoProvider(
  id: string,
  body: Partial<{
    client_id: string; client_secret: string; enabled: boolean;
    tenant_id: string | null; config: Record<string, any>;
  }>,
): Promise<SsoProviderAdmin> {
  return caseRequest(`/auth/real/sso/providers/${id}`, { method: "PATCH", body: JSON.stringify(body) });
}

export async function deleteSsoProvider(id: string): Promise<void> {
  await caseRequest(`/auth/real/sso/providers/${id}`, { method: "DELETE" });
}

/* ── Enterprise identity (SAML + SCIM) ───────────────────────────── */

export interface ScimToken {
  id: string; name: string; tenant_id: string | null; created_by: string;
  created_at: string | null; last_used_at: string | null;
  expires_at: string | null; revoked_at: string | null;
}

export async function listScimTokens(): Promise<ScimToken[]> {
  const d = await caseRequest<{ tokens: ScimToken[] }>("/scim/tokens");
  return d.tokens ?? [];
}

/** The response carries the plaintext token, and it is the only time it exists
 *  anywhere outside the IdP — the server stores a SHA-256 and cannot show it
 *  again. */
export async function createScimToken(body: {
  name: string; tenant_id?: string | null; expires_at?: string | null;
}): Promise<{ id: string; name: string; token: string; warning: string }> {
  return caseRequest("/scim/tokens", { method: "POST", body: JSON.stringify(body) });
}

export async function revokeScimToken(id: string): Promise<void> {
  await caseRequest(`/scim/tokens/${id}`, { method: "DELETE" });
}

export interface IdentityGroupMapping {
  id: string; tenant_id: string; source: string; external_group: string;
  access_group_id: string; access_group_name: string | null;
  priority: number; created_by: string; created_at: string | null;
}

export async function listGroupMappings(params?: { tenant?: string; source?: string }):
  Promise<IdentityGroupMapping[]> {
  const q = new URLSearchParams();
  if (params?.tenant) q.set("tenant", params.tenant);
  if (params?.source) q.set("source", params.source);
  const suffix = q.toString() ? `?${q}` : "";
  const d = await caseRequest<{ mappings: IdentityGroupMapping[] }>(
    `/identity/provisioning/group-mappings${suffix}`);
  return d.mappings ?? [];
}

export async function createGroupMapping(body: {
  tenant: string; source: string; external_group: string;
  access_group_id: string; priority?: number;
}): Promise<IdentityGroupMapping> {
  return caseRequest("/identity/provisioning/group-mappings",
    { method: "POST", body: JSON.stringify(body) });
}

export async function deleteGroupMapping(id: string): Promise<void> {
  await caseRequest(`/identity/provisioning/group-mappings/${id}`, { method: "DELETE" });
}

export interface ProvisionLogEntry {
  id: string; tenant_id: string | null; source: string;
  external_id: string | null; username: string | null;
  action: string; result: string; detail: Record<string, any>;
  created_at: string | null;
}

export async function listProvisionLog(params?: {
  source?: string; username?: string; action?: string; limit?: number;
}): Promise<ProvisionLogEntry[]> {
  const q = new URLSearchParams();
  if (params?.source) q.set("source", params.source);
  if (params?.username) q.set("username", params.username);
  if (params?.action) q.set("action", params.action);
  if (params?.limit) q.set("limit", String(params.limit));
  const suffix = q.toString() ? `?${q}` : "";
  const d = await caseRequest<{ entries: ProvisionLogEntry[] }>(
    `/identity/provisioning/log${suffix}`);
  return d.entries ?? [];
}

/** An account and the IdP identity bound to it, if any. */
export interface AccountLink {
  user_id: string; username: string; email: string;
  display_name: string | null; external_id: string | null;
  /** OIDC keys on this pair and never reads `external_id`. */
  sso_provider: string | null; sso_subject: string | null;
  is_active: boolean; has_password: boolean;
}

/** Accounts bound to an IdP identity — or, with `linked_only: false`, any
 *  account matching the search, which is how an admin finds the one to link. */
export async function listAccountLinks(params?: {
  q?: string; linked_only?: boolean; limit?: number;
}): Promise<AccountLink[]> {
  const query = new URLSearchParams();
  if (params?.q) query.set("q", params.q);
  if (params?.linked_only === false) query.set("linked_only", "false");
  if (params?.limit) query.set("limit", String(params.limit));
  const suffix = query.toString() ? `?${query}` : "";
  const d = await caseRequest<{ links: AccountLink[] }>(
    `/identity/provisioning/links${suffix}`);
  return d.links ?? [];
}

/** Bind an existing account to the identity its IdP asserts.
 *
 *  A SAML login refuses to adopt an account it matches only by email address —
 *  an assertion naming an address is not proof of owning the account that holds
 *  it. This is how the link gets made deliberately instead. */
export async function linkAccount(identifier: string, binding: {
  external_id?: string; sso_provider?: string; sso_subject?: string;
}): Promise<AccountLink> {
  return caseRequest("/identity/provisioning/links", {
    method: "POST", body: JSON.stringify({ identifier, ...binding }),
  });
}

export async function unlinkAccount(identifier: string): Promise<AccountLink> {
  return caseRequest("/identity/provisioning/links/unlink", {
    method: "POST", body: JSON.stringify({ identifier }),
  });
}

export async function listUnmappedGroups():
  Promise<{ source: string; external_group: string; seen: number }[]> {
  const d = await caseRequest<{ unmapped: { source: string; external_group: string; seen: number }[] }>(
    "/identity/provisioning/unmapped-groups");
  return d.unmapped ?? [];
}

/** Dry-run a pasted assertion. Issues nothing, provisions nobody, and does not
 *  consume the assertion id — an admin can paste the same capture repeatedly
 *  while fixing an attribute map. */
export async function testSamlAssertion(tenant: string, samlResponse: string): Promise<any> {
  return caseRequest(`/auth/saml/${encodeURIComponent(tenant)}/test`,
    { method: "POST", body: JSON.stringify({ saml_response: samlResponse }) });
}

/** Access groups, as the mapping editor's target list. */
export async function listAccessGroupsBrief():
  Promise<{ id: string; name: string; tenant_id: string }[]> {
  return caseRequest("/access-groups");
}

export async function createCalendar(body: {
  name: string; timezone: string; work_days: number[];
  work_start_hour: number; work_end_hour: number;
  description?: string; holidays?: string[];
}): Promise<any> {
  return caseRequest("/admin/calendars", { method: "POST", body: JSON.stringify(body) });
}

export async function updateCalendar(id: string, body: Record<string, any>): Promise<any> {
  return caseRequest(`/admin/calendars/${id}`, { method: "PATCH", body: JSON.stringify(body) });
}

export async function listCalendars(): Promise<any[]> {
  return caseRequest("/admin/calendars");
}

// ── Calendar — people time ─────────────────────────────────────────────────
//
// Distinct from the business calendars above: those describe the ORGANISATION's
// hours (what the SLA clock counts), these describe a PERSON's.

export interface Absence {
  id: string;
  user_id: string;
  starts_at: string;
  ends_at: string;
  all_day: boolean;
  status: "requested" | "approved" | "cancelled";
  delegate_user_id: string | null;
  source: string;
  approved_by: string | null;
  approved_at: string | null;
  created_by: string;
  created_at: string;
  // Personal — present only where the caller is the person, their manager or an
  // admin. A colleague sees busy, never why.
  kind?: string;
  note?: string;
}

export async function listAbsences(params: {
  user_id?: string; status?: string; start?: string; end?: string;
} = {}): Promise<Absence[]> {
  const q = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => { if (v) q.set(k, v); });
  const qs = q.toString();
  return caseRequest(`/calendar/absences${qs ? `?${qs}` : ""}`);
}

export async function createAbsence(body: {
  user_id?: string; kind?: string; starts_at: string; ends_at: string;
  all_day?: boolean; delegate_user_id?: string | null; note?: string;
}): Promise<Absence> {
  return caseRequest("/calendar/absences", { method: "POST", body: JSON.stringify(body) });
}

export async function approveAbsence(id: string): Promise<Absence> {
  return caseRequest(`/calendar/absences/${id}/approve`, { method: "POST" });
}

export async function cancelAbsence(id: string): Promise<Absence> {
  return caseRequest(`/calendar/absences/${id}/cancel`, { method: "POST" });
}

export async function getAvailability(userIds: string[], at?: string): Promise<{
  at: string;
  users: { user_id: string; available: boolean; delegate_user_id?: string | null }[];
}> {
  const q = new URLSearchParams();
  userIds.forEach(u => q.append("user_ids", u));
  if (at) q.set("at", at);
  return caseRequest(`/calendar/availability?${q.toString()}`);
}

export async function getWorkPattern(userId: string): Promise<{
  user_id: string; work_days: number[]; work_start_hour: number;
  work_end_hour: number; timezone: string;
}> {
  return caseRequest(`/calendar/patterns/${encodeURIComponent(userId)}`);
}

export async function saveWorkPattern(body: {
  user_id: string; business_calendar_id?: string | null; work_days?: number[] | null;
  work_start_hour?: number | null; work_end_hour?: number | null; timezone?: string | null;
}): Promise<any> {
  return caseRequest("/calendar/patterns", { method: "PUT", body: JSON.stringify(body) });
}

export interface Recurrence { freq: "daily" | "weekly" | "monthly"; interval?: number; until?: string; count?: number }

export interface CalendarEvent {
  id: string; title: string; description: string;
  starts_at: string; ends_at: string; timezone: string;
  organiser_user_id: string; attendee_user_ids: string[];
  external_emails: string[];
  case_id: string | null; meet_session_id: string | null;
  meeting_url: string | null;
  recurrence: Recurrence | null; excluded_dates: string[];
  /** `active`, `ended`, `cancelled`, or null when the meeting has no room. */
  room_status?: string | null;
  visibility: "team" | "private"; status: string;
  created_by: string; created_at: string; updated_by?: string | null;
  // Present on listings, which expand a series into its occurrences.
  occurrence_date?: string; is_recurring?: boolean;
}

export async function listEvents(params: { start?: string; end?: string } = {}): Promise<CalendarEvent[]> {
  const q = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => { if (v) q.set(k, v); });
  const qs = q.toString();
  return caseRequest(`/calendar/events${qs ? `?${qs}` : ""}`);
}

export async function createEvent(body: {
  title: string; description?: string; starts_at: string; ends_at: string;
  timezone?: string; attendee_user_ids?: string[]; external_emails?: string[];
  case_id?: string | null; meet_session_id?: string | null;
  meeting_url?: string | null; recurrence?: Recurrence | null;
  visibility?: "team" | "private";
  /** A start time in the past is refused unless this says it is deliberate. */
  allow_past?: boolean;
  /** Ask HxMeet for a room as part of scheduling, so the invitation has a link. */
  create_room?: boolean;
  /** Email the guests now. On by default at creation; an edit never sends. */
  send_invites?: boolean;
}): Promise<CalendarEvent & {
  join_url?: string | null;
  room_error?: string;
  invitations?: { sent: string[]; failed: { email: string; error: string }[]; from?: string };
}> {
  return caseRequest("/calendar/events", { method: "POST", body: JSON.stringify(body) });
}

export async function updateEvent(id: string, body: Record<string, unknown>): Promise<CalendarEvent> {
  return caseRequest(`/calendar/events/${id}`, { method: "PATCH", body: JSON.stringify(body) });
}

/** Cancel one occurrence of a series — "we skip next Monday". */
export async function skipOccurrence(id: string, on: string): Promise<CalendarEvent> {
  return caseRequest(`/calendar/events/${id}/skip?on=${encodeURIComponent(on)}`, { method: "POST" });
}

/** Ask HxMeet for a room. HxMeet owns rooms; this only remembers the answer. */
/** Ask HxMeet for a room. A live one is reused; one LiveKit already closed is
 *  replaced, so rejoining a stand-up puts you back in a room. */
export async function createMeetingRoom(id: string): Promise<CalendarEvent & { join_url: string | null; driver: string; reused: boolean }> {
  return caseRequest(`/calendar/events/${id}/meeting`, { method: "POST" });
}

export async function inviteGuests(id: string): Promise<{ sent: string[]; failed: { email: string; error: string }[] }> {
  return caseRequest(`/calendar/events/${id}/invite`, { method: "POST" });
}

export async function updateAbsence(id: string, body: Record<string, unknown>): Promise<Absence> {
  return caseRequest(`/calendar/absences/${id}`, { method: "PATCH", body: JSON.stringify(body) });
}

export async function cancelEvent(id: string): Promise<CalendarEvent> {
  return caseRequest(`/calendar/events/${id}/cancel`, { method: "POST" });
}

export async function proposeSlots(body: {
  user_ids: string[]; start: string; end: string; minutes?: number; limit?: number;
}): Promise<{ slots: { starts_at: string; ends_at: string }[]; searched: Record<string, unknown> }> {
  return caseRequest("/calendar/slots", { method: "POST", body: JSON.stringify(body) });
}

export async function listRules(): Promise<any> {
  return caseRequest("/rules");
}

export async function createRule(body: {
  name: string; version: string; rule_type: string;
  scope?: string; scope_target_id?: string | null;
  definition_json: Record<string, any>;
  enabled?: boolean; priority?: number;
}): Promise<any> {
  return caseRequest("/rules", { method: "POST", body: JSON.stringify(body) });
}

export async function updateRule(id: string, body: {
  definition_json?: Record<string, any>;
  enabled?: boolean;
  priority?: number;
}): Promise<any> {
  return caseRequest(`/rules/${id}`, { method: "PATCH", body: JSON.stringify(body) });
}

export async function deleteRule(id: string): Promise<void> {
  await caseRequest(`/rules/${id}`, { method: "DELETE" });
}


/* ── Process Mining (Phase 14) ─────────────────────────────────── */

export async function getPMSummary(caseTypeId?: string, days = 30, tenantId?: string): Promise<any> {
  const qs = new URLSearchParams();
  if (caseTypeId) qs.set("case_type_id", caseTypeId);
  if (tenantId) qs.set("tenant_id", tenantId);
  qs.set("days", String(days));
  return caseRequest(`/process-mining/summary?${qs}`);
}

export async function getPMActivityStats(caseTypeId?: string, days = 30, tenantId?: string): Promise<any[]> {
  const qs = new URLSearchParams();
  if (caseTypeId) qs.set("case_type_id", caseTypeId);
  if (tenantId) qs.set("tenant_id", tenantId);
  qs.set("days", String(days));
  return caseRequest(`/process-mining/activity-stats?${qs}`);
}

export async function getPMBottlenecks(caseTypeId?: string, days = 30, tenantId?: string): Promise<any[]> {
  const qs = new URLSearchParams();
  if (caseTypeId) qs.set("case_type_id", caseTypeId);
  if (tenantId) qs.set("tenant_id", tenantId);
  qs.set("days", String(days));
  return caseRequest(`/process-mining/bottlenecks?${qs}`);
}

export async function getPMVariants(caseTypeId?: string, days = 30, tenantId?: string): Promise<any[]> {
  const qs = new URLSearchParams();
  if (caseTypeId) qs.set("case_type_id", caseTypeId);
  if (tenantId) qs.set("tenant_id", tenantId);
  qs.set("days", String(days));
  return caseRequest(`/process-mining/variants?${qs}`);
}

export async function getPMFlowGraph(caseTypeId?: string, days = 30, tenantId?: string): Promise<any> {
  const qs = new URLSearchParams();
  if (caseTypeId) qs.set("case_type_id", caseTypeId);
  if (tenantId) qs.set("tenant_id", tenantId);
  qs.set("days", String(days));
  return caseRequest(`/process-mining/flow-graph?${qs}`);
}

export async function getPMConformance(caseTypeId: string, days = 30): Promise<any> {
  return caseRequest(`/process-mining/conformance/${caseTypeId}?days=${days}`);
}

export async function getPMEvents(caseId?: string, limit = 100): Promise<any[]> {
  const qs = new URLSearchParams();
  if (caseId) qs.set("case_id", caseId);
  qs.set("limit", String(limit));
  return caseRequest(`/process-mining/events?${qs}`);
}

/* ── NLP (Phase 15) ────────────────────────────────────────────── */

export async function getNLPStatus(): Promise<any> {
  return caseRequest("/nlp/status");
}

export async function generateCaseTypeFromText(
  description: string, deploy = false, nameOverride?: string
): Promise<any> {
  return caseRequest("/nlp/generate-case-type", {
    method: "POST",
    body: JSON.stringify({ description, deploy, ...(nameOverride ? { name_override: nameOverride } : {}) }),
  });
}

export async function generateFullCaseType(
  description: string, deploy = false, nameOverride?: string
): Promise<any> {
  return caseRequest("/nlp/generate-full", {
    method: "POST",
    body: JSON.stringify({ description, deploy, ...(nameOverride ? { name_override: nameOverride } : {}) }),
  });
}

/* ── Payments P48 ─────────────────────────────────────────────── */

export async function initiatePaymentRequest(
  caseId: string, stepId: string, amountCents: number,
  currency: string, description: string, customerEmail?: string
): Promise<any> {
  return caseRequest(`/payments/cases/${caseId}/charge`, {
    method: "POST",
    body: JSON.stringify({ step_id: stepId, amount_cents: amountCents, currency, description, customer_email: customerEmail }),
  });
}

export async function listCasePaymentRequests(caseId: string): Promise<any[]> {
  return caseRequest(`/payments/cases/${caseId}/requests`);
}

export async function confirmDisbursement(
  caseId: string, stepId: string, amountCents: number,
  currency: string, description: string, bankReference?: string, notes?: string
): Promise<any> {
  return caseRequest(`/payments/cases/${caseId}/disburse`, {
    method: "POST",
    body: JSON.stringify({ step_id: stepId, amount_cents: amountCents, currency, description, bank_reference: bankReference, notes }),
  });
}

export async function listCaseDisbursements(caseId: string): Promise<any[]> {
  return caseRequest(`/payments/cases/${caseId}/disbursements`);
}

export async function markDisbursementSent(disbursementId: string): Promise<any> {
  return caseRequest(`/payments/disbursements/${disbursementId}/mark-sent`, { method: "POST" });
}

/* ── KYC / E-sign P49 ─────────────────────────────────────────── */

export async function initiateIdentityVerification(
  caseId: string, stepId: string, firstName: string, lastName: string
): Promise<any> {
  return caseRequest(`/identity/cases/${caseId}/verify`, {
    method: "POST",
    body: JSON.stringify({ step_id: stepId, first_name: firstName, last_name: lastName }),
  });
}

export async function listCaseVerifications(caseId: string): Promise<any[]> {
  return caseRequest(`/identity/cases/${caseId}/verifications`);
}

export async function sendESignRequest(
  caseId: string, stepId: string, signerEmail: string, signerName: string, documentName: string
): Promise<any> {
  return caseRequest(`/esign/cases/${caseId}/send`, {
    method: "POST",
    body: JSON.stringify({ step_id: stepId, signer_email: signerEmail, signer_name: signerName, document_name: documentName }),
  });
}

export async function listCaseESignRequests(caseId: string): Promise<any[]> {
  return caseRequest(`/esign/cases/${caseId}/requests`);
}

/* ── CRM / Invoices P50 ───────────────────────────────────────── */

export async function syncToCrm(
  caseId: string, stepId: string, firstName: string, lastName: string,
  email: string, subject: string, description: string
): Promise<any> {
  return caseRequest(`/crm/cases/${caseId}/sync`, {
    method: "POST",
    body: JSON.stringify({ step_id: stepId, first_name: firstName, last_name: lastName, email, subject, description }),
  });
}

export async function listCaseCrmRecords(caseId: string): Promise<any[]> {
  return caseRequest(`/crm/cases/${caseId}/records`);
}

export async function generateInvoice(
  caseId: string, stepId: string, contactName: string, description: string,
  amountCents: number, currency: string, lineItems: any[], reference: string
): Promise<any> {
  return caseRequest(`/invoices/cases/${caseId}/generate`, {
    method: "POST",
    body: JSON.stringify({ step_id: stepId, contact_name: contactName, description, amount_cents: amountCents, currency, line_items: lineItems, reference }),
  });
}

export async function listCaseInvoices(caseId: string): Promise<any[]> {
  return caseRequest(`/invoices/cases/${caseId}/records`);
}

/* ── P51 Communications ─────────────────────────────────────────── */

export async function sendSms(caseId: string, data: { step_id: string; to_number: string; body: string; from_number?: string; connector_id?: string }): Promise<any> {
  return caseRequest(`/comms/sms/cases/${caseId}/send`, { method: "POST", body: JSON.stringify(data) });
}

export async function listCaseSms(caseId: string): Promise<any[]> {
  return caseRequest(`/comms/sms/cases/${caseId}/messages`);
}

export async function sendSlack(caseId: string, data: { step_id: string; message: string; channel?: string; blocks?: any[]; connector_id?: string }): Promise<any> {
  return caseRequest(`/comms/slack/cases/${caseId}/send`, { method: "POST", body: JSON.stringify(data) });
}

export async function listCaseSlack(caseId: string): Promise<any[]> {
  return caseRequest(`/comms/slack/cases/${caseId}/notifications`);
}

export async function listCommsConnectors(): Promise<any[]> {
  return caseRequest(`/comms/connectors`);
}

/* ── P52 Document Intelligence & Storage ───────────────────────── */

export async function extractDocument(caseId: string, data: { step_id: string; source_url?: string; document_name?: string; connector_id?: string }): Promise<any> {
  return caseRequest(`/docintel/cases/${caseId}/extract`, { method: "POST", body: JSON.stringify(data) });
}

export async function listCaseExtractions(caseId: string): Promise<any[]> {
  return caseRequest(`/docintel/cases/${caseId}/extractions`);
}

export async function routeToStorage(caseId: string, data: { step_id: string; document_name: string; content_type?: string; size_bytes?: number; connector_id?: string }): Promise<any> {
  return caseRequest(`/docintel/cases/${caseId}/store`, { method: "POST", body: JSON.stringify(data) });
}

export async function listCaseStorageRoutes(caseId: string): Promise<any[]> {
  return caseRequest(`/docintel/cases/${caseId}/storage`);
}

export async function listDocIntelConnectors(): Promise<any[]> {
  return caseRequest(`/docintel/connectors`);
}

/* ── Scout (Phase 16) ──────────────────────────────────────────── */

export async function listScoutPlatforms(): Promise<any> {
  return caseRequest("/scout/platforms");
}

export async function createScoutScan(
  name: string, content: string, source_platform = "", filename = ""
): Promise<any> {
  return caseRequest("/scout/scan", {
    method: "POST",
    body: JSON.stringify({ name, content, source_platform, filename }),
  });
}

export async function listScoutScans(): Promise<any[]> {
  return caseRequest("/scout/scans");
}

export async function getScoutScan(id: string): Promise<any> {
  return caseRequest(`/scout/scans/${id}`);
}

export async function getScoutPlan(id: string): Promise<any> {
  return caseRequest(`/scout/scans/${id}/plan`);
}

export async function deleteScoutScan(id: string): Promise<void> {
  await caseRequest(`/scout/scans/${id}`, { method: "DELETE" });
}

/* ── Tenants (Phase 17) ────────────────────────────────────────── */

export interface Tenant {
  id: string;
  slug: string;
  name: string;
  description: string;
  status: string;
  settings: Record<string, any>;
  max_cases: number | null;
  max_users: number | null;
  created_at: string;
  updated_at: string;
}

export async function listTenants(): Promise<Tenant[]> {
  return caseRequest("/tenants");
}

export async function createTenant(body: {
  slug: string; name: string; description?: string;
  max_cases?: number | null; max_users?: number | null;
}): Promise<Tenant> {
  return caseRequest("/tenants", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function updateTenant(id: string, body: Partial<Tenant>): Promise<Tenant> {
  return caseRequest(`/tenants/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export async function deleteTenant(id: string): Promise<void> {
  await caseRequest(`/tenants/${id}`, { method: "DELETE" });
}

export async function permanentDeleteTenant(id: string): Promise<void> {
  await caseRequest(`/tenants/${id}/permanent`, { method: "DELETE" });
}

export async function listTenantMembers(id: string): Promise<any[]> {
  return caseRequest(`/tenants/${id}/members`);
}

export async function addTenantMember(
  id: string, user_id: string, role = "member"
): Promise<any> {
  return caseRequest(`/tenants/${id}/members`, {
    method: "POST",
    body: JSON.stringify({ user_id, role }),
  });
}

export async function updateTenantMember(
  id: string, user_id: string, role: string
): Promise<any> {
  return caseRequest(`/tenants/${id}/members/${user_id}`, {
    method: "PATCH",
    body: JSON.stringify({ user_id, role }),
  });
}

export async function removeTenantMember(id: string, user_id: string): Promise<void> {
  await caseRequest(`/tenants/${id}/members/${user_id}`, { method: "DELETE" });
}

/* ── Codegen (Phase 18) ────────────────────────────────────────── */

export async function listCodegenPlatforms(): Promise<any> {
  return caseRequest("/codegen/platforms");
}

export async function previewApp(config: any): Promise<any> {
  return caseRequest("/codegen/preview", {
    method: "POST",
    body: JSON.stringify(config),
  });
}

export async function downloadGeneratedApp(config: any): Promise<Blob> {
  const resp = await fetch(`${CASE_BASE}/codegen/generate`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(config),
  });
  if (!resp.ok) throw new Error("Generation failed");
  return resp.blob();
}

/* ── Scout AI (Phase 19) ───────────────────────────────────────── */

export async function getScoutAIStatus(): Promise<any> {
  return caseRequest("/scout-ai/status");
}

export async function analyzeArtifact(body: {
  code: string; artifact_type?: string; source_platform?: string;
  identifier?: string; save?: boolean; scan_id?: string | null;
}): Promise<any> {
  return caseRequest("/scout-ai/analyze", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function generateHelixCode(body: {
  code: string; artifact_type?: string; source_platform?: string;
}): Promise<any> {
  return caseRequest("/scout-ai/generate-code", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function listArtifactAnalyses(scanId?: string): Promise<any[]> {
  const q = scanId ? `?scan_id=${scanId}` : "";
  return caseRequest(`/scout-ai/analyses${q}`);
}

/* ── Enterprise (Phase 20) ─────────────────────────────────────── */

export async function listSecurityEvents(params: any = {}): Promise<any[]> {
  const qs = new URLSearchParams(params).toString();
  return caseRequest(`/enterprise/security-events${qs ? "?" + qs : ""}`);
}

export async function getSecurityEventStats(days = 30): Promise<any> {
  return caseRequest(`/enterprise/security-events/stats?days=${days}`);
}

export async function createGDPRRequest(body: any): Promise<any> {
  return caseRequest("/enterprise/gdpr/requests", {
    method: "POST", body: JSON.stringify(body),
  });
}

export async function listGDPRRequests(): Promise<any[]> {
  return caseRequest("/enterprise/gdpr/requests");
}

export async function lookupUserData(userId: string): Promise<{ user_id: string; exists: boolean; counts: Record<string, number> }> {
  return caseRequest(`/enterprise/gdpr/lookup/${encodeURIComponent(userId)}`);
}

export async function downloadUserExport(userId: string): Promise<Blob> {
  const resp = await fetch(`${CASE_BASE}/enterprise/gdpr/export/${userId}`, {
    headers: authHeaders(),
  });
  if (!resp.ok) throw new Error("Export failed");
  return resp.blob();
}

export async function anonymizeUser(userId: string): Promise<any> {
  return caseRequest(`/enterprise/gdpr/anonymize/${userId}`, { method: "POST" });
}

export async function listRetentionPolicies(): Promise<any[]> {
  return caseRequest("/enterprise/retention-policies");
}

export async function updateRetentionPolicy(id: string, body: any): Promise<any> {
  return caseRequest(`/enterprise/retention-policies/${id}`, {
    method: "PATCH", body: JSON.stringify(body),
  });
}

export async function getEnterpriseSystemInfo(): Promise<any> {
  return caseRequest("/enterprise/system-info");
}

export async function resolveGDPRRequest(requestId: string, status: "completed" | "rejected" = "completed"): Promise<any> {
  return caseRequest(`/enterprise/gdpr/requests/${requestId}`, {
    method: "PATCH", body: JSON.stringify({ status }),
  });
}

// (getSystemInfo defined elsewhere — Phase 20 uses /enterprise/system-info via a different name)

/* ── Site Map (Phase 20) ───────────────────────────────────────── */

export async function listModules(): Promise<any> {
  return caseRequest("/sitemap/modules");
}

export async function listPhases(): Promise<any> {
  return caseRequest("/sitemap/phases");
}

export async function searchModules(q: string): Promise<any> {
  return caseRequest(`/sitemap/search?q=${encodeURIComponent(q)}`);
}


/* ── Orchestrator (Phase 21) ───────────────────────────────────── */

export async function createMigrationProject(name: string, scan_id: string): Promise<any> {
  return caseRequest("/orchestrator/projects", {
    method: "POST", body: JSON.stringify({ name, scan_id }),
  });
}

export async function listMigrationProjects(): Promise<any[]> {
  return caseRequest("/orchestrator/projects");
}

export async function getMigrationProject(id: string): Promise<any> {
  return caseRequest(`/orchestrator/projects/${id}`);
}

export async function deleteMigrationProject(id: string): Promise<void> {
  await caseRequest(`/orchestrator/projects/${id}`, { method: "DELETE" });
}

export async function getMigrationRoadmap(id: string): Promise<any> {
  return caseRequest(`/orchestrator/projects/${id}/roadmap`);
}

export async function listMigrationTasks(projectId: string): Promise<any[]> {
  return caseRequest(`/orchestrator/projects/${projectId}/tasks`);
}

export async function analyzeTask(taskId: string): Promise<any> {
  return caseRequest(`/orchestrator/tasks/${taskId}/analyze`, { method: "POST" });
}

export async function generateTaskCode(taskId: string): Promise<any> {
  return caseRequest(`/orchestrator/tasks/${taskId}/generate`, { method: "POST" });
}

export async function markTaskPorted(taskId: string): Promise<any> {
  return caseRequest(`/orchestrator/tasks/${taskId}/mark-ported`, { method: "POST" });
}

export async function runFullPipeline(projectId: string, maxTasks = 50): Promise<any> {
  return caseRequest(`/orchestrator/projects/${projectId}/run-all?max_tasks=${maxTasks}`, {
    method: "POST",
  });
}

export async function downloadMigrationZip(projectId: string): Promise<Blob> {
  const resp = await fetch(`${CASE_BASE}/orchestrator/projects/${projectId}/export`, {
    headers: authHeaders(),
  });
  if (!resp.ok) throw new Error("Export failed");
  return resp.blob();
}

// ── HxBranch v2 ──────────────────────────────────────────────────

export async function listBranches(params: {
  status?: string;
  owner_id?: string;
  assigned_reviewer_id?: string;
  artifact_type?: string;
} = {}): Promise<{ branches: any[]; total: number }> {
  const qs = new URLSearchParams();
  if (params.status)               qs.set("status", params.status);
  if (params.owner_id)             qs.set("owner_id", params.owner_id);
  if (params.assigned_reviewer_id) qs.set("assigned_reviewer_id", params.assigned_reviewer_id);
  if (params.artifact_type)        qs.set("artifact_type", params.artifact_type);
  const resp = await fetch(`${CASE_BASE}/branches${qs.toString() ? "?" + qs : ""}`, {
    headers: { "Content-Type": "application/json", ...authHeaders() },
  });
  if (!resp.ok) throw new Error(await resp.text());
  return resp.json();
}

export async function getBranchDiff(branchId: string): Promise<any> {
  const resp = await fetch(`${CASE_BASE}/branches/${branchId}/diff`, {
    headers: { "Content-Type": "application/json", ...authHeaders() },
  });
  if (!resp.ok) throw new Error(await resp.text());
  return resp.json();
}

export async function postBranchReview(branchId: string, decision: "approved" | "rejected" | "changes_requested", comments?: string): Promise<any> {
  const resp = await fetch(`${CASE_BASE}/branches/${branchId}/reviews`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ decision, comments }),
  });
  if (!resp.ok) throw new Error(await resp.text());
  return resp.json();
}

export async function createBranch(params: {
  name: string;
  artifact_type: string;
  artifact_id: string;
  description?: string;
  assigned_reviewer_id?: string;
}): Promise<any> {
  const resp = await fetch(`${CASE_BASE}/branches`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ branch_type: "artifact", ...params }),
  });
  if (!resp.ok) throw new Error(await resp.text());
  return resp.json();
}

export async function getBranch(branchId: string): Promise<any> {
  const resp = await fetch(`${CASE_BASE}/branches/${branchId}`, {
    headers: { "Content-Type": "application/json", ...authHeaders() },
  });
  if (!resp.ok) throw new Error(await resp.text());
  return resp.json();
}

export async function patchBranchContent(branchId: string, content: Record<string, unknown>): Promise<any> {
  const resp = await fetch(`${CASE_BASE}/branches/${branchId}/content`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ content_snapshot: content }),
  });
  if (!resp.ok) throw new Error(await resp.text());
  return resp.json();
}

export async function submitBranchForReview(branchId: string, assignedReviewerId: string): Promise<any> {
  const resp = await fetch(`${CASE_BASE}/branches/${branchId}/submit`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ assigned_reviewer_id: assignedReviewerId }),
  });
  if (!resp.ok) throw new Error(await resp.text());
  return resp.json();
}

export async function recallBranch(branchId: string): Promise<any> {
  const resp = await fetch(`${CASE_BASE}/branches/${branchId}/recall`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
  });
  if (!resp.ok) throw new Error(await resp.text());
  return resp.json();
}

export interface UserDirectoryEntry {
  user_id: string;
  email: string | null;
  display_name: string | null;
  tenant_id: string | null;
  access_group_ids: string[];
}

export async function searchUsers(q: string, accessGroupId?: string, limit = 20): Promise<UserDirectoryEntry[]> {
  const params = new URLSearchParams({ active_only: "true", limit: String(limit) });
  if (q) params.set("q", q);
  if (accessGroupId) params.set("access_group_id", accessGroupId);
  return caseRequest(`/user-directory?${params}`);
}

export async function getMyDirectoryEntry(userId: string): Promise<UserDirectoryEntry & { access_group_ids: string[] }> {
  return caseRequest(`/user-directory/${encodeURIComponent(userId)}`);
}

/**
 * Authenticated fetch against the case-service base (/api/v1).
 * Handles 401 refresh and re-auth the same way as all other case endpoints.
 * Use this instead of raw fetch() so auth headers are always included.
 */
export async function apiFetch(path: string, options?: RequestInit): Promise<Response> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...authHeaders(),
    ...(options?.headers as Record<string, string> | undefined),
  };
  const res = await fetch(caseUrl(path), { ...options, headers });
  if (res.status === 401) {
    const newToken = await attemptRefresh();
    if (newToken) {
      const retry = await fetch(caseUrl(path), {
        ...options,
        headers: { ...headers, ...authHeaders(newToken) },
      });
      return retry;
    }
    window.dispatchEvent(new Event("velaris:unauthorized"));
  }
  return res;
}

/* ── Omnichannel (SMS / WhatsApp / voice) ────────────────────────── */

export interface ChannelConversation {
  id: string; endpoint_id: string; endpoint_address: string | null;
  tenant_id: string; peer_address: string; case_id: string | null;
  status: string; assigned_to: string | null; unread_count: number;
  last_message_at: string | null; window_expires_at: string | null;
}

export interface ChannelMessage {
  id: string; direction: string; channel: string; author: string | null;
  peer_address: string; body: string; media: unknown[];
  template_name: string | null; status: string; error: string | null;
  is_read: boolean; created_at: string | null;
}

export interface WhatsAppTemplate {
  id: string; endpoint_id: string; name: string; language: string;
  provider_template_id: string | null; body_params: string[];
  approval_status: string;
}

export async function listChannelConversations(params?: {
  status?: string; case_id?: string; unread_only?: boolean;
}): Promise<ChannelConversation[]> {
  const q = new URLSearchParams();
  if (params?.status) q.set("status", params.status);
  if (params?.case_id) q.set("case_id", params.case_id);
  if (params?.unread_only) q.set("unread_only", "true");
  const suffix = q.toString() ? `?${q}` : "";
  const d = await caseRequest<{ conversations: ChannelConversation[] }>(
    `/channels/conversations${suffix}`);
  return d.conversations ?? [];
}

export async function getChannelConversation(id: string):
  Promise<{ conversation: ChannelConversation; messages: ChannelMessage[] }> {
  return caseRequest(`/channels/conversations/${id}`);
}

/** Refused with a specific code when the number opted out (403) or the
 *  WhatsApp 24-hour window has closed without a template (409). */
export async function replyToConversation(id: string, body: {
  body: string; template_name?: string; template_params?: string[];
}): Promise<ChannelMessage> {
  return caseRequest(`/channels/conversations/${id}/reply`,
    { method: "POST", body: JSON.stringify(body) });
}

export async function updateChannelConversation(id: string, body: {
  case_id?: string; assigned_to?: string; status?: string;
}): Promise<ChannelConversation> {
  return caseRequest(`/channels/conversations/${id}`,
    { method: "PATCH", body: JSON.stringify(body) });
}

export async function listChannelTemplates(endpointId?: string):
  Promise<WhatsAppTemplate[]> {
  const suffix = endpointId ? `?endpoint_id=${encodeURIComponent(endpointId)}` : "";
  const d = await caseRequest<{ templates: WhatsAppTemplate[] }>(
    `/channels/templates${suffix}`);
  return d.templates ?? [];
}

export async function listCaseConversations(caseId: string):
  Promise<ChannelConversation[]> {
  const d = await caseRequest<{ conversations: ChannelConversation[] }>(
    `/channels/cases/${caseId}/conversations`);
  return d.conversations ?? [];
}

/* ── RPA bots ────────────────────────────────────────────────────── */

export interface BotDefinition {
  id: string; tenant_id: string; name: string; description: string;
  image: string; digest: string; signature_verified: boolean;
  declared_command?: string[]; declared_env?: Record<string, unknown>;
  input_schema?: Record<string, unknown>; output_schema?: Record<string, unknown>;
  credential_refs: string[]; granted_domains: string[];
  timeout_seconds: number; max_concurrency: number; resource_profile: string;
  /** Null = no fan-out. Names the input key holding the collection to spread
   *  over; it lives on the definition so the approver reviews it. */
  fanout_input_key?: string | null; fanout_max?: number;
  status: string; created_by: string; approved_by: string | null;
  approved_at: string | null;
}

export interface BotCredential {
  id: string; tenant_id: string; ref: string; version: number;
  created_by: string; env_name: string; updated_at: string | null;
}

export interface BotBatch {
  batch_id: string; bot_id: string; size: number; finished: boolean;
  succeeded: number; counts: Record<string, number>; jobs: string[];
}

export interface BotJob {
  id: string; bot_id: string; case_id: string | null; trigger: string;
  requested_by: string | null; status: string; attempt: number;
  error: string | null; output: Record<string, unknown> | null;
  /** Set when this job is one of a fan-out batch: N ordinary jobs from one run,
   *  sharing a batch id so they can be reported together. */
  batch_id?: string | null; batch_index?: number | null; batch_size?: number | null;
  /** Not before this instant — a retry serving its backoff, or a queued
   *  scheduled run. */
  run_after?: string | null;
  queued_at: string | null; started_at: string | null; ended_at: string | null;
}

export interface BotProposal {
  job_id: string; case_id: string | null; checksum: string;
  changes: { field: string; current: unknown; proposed: unknown }[];
}

export async function listBots(): Promise<BotDefinition[]> {
  const d = await caseRequest<{ bots: BotDefinition[] }>("/bots");
  return d.bots ?? [];
}

/** Refused (409) when the approver is the bot's own author — approval IS the
 *  review, and reviewing your own container image is not one. */
export async function approveBot(id: string): Promise<BotDefinition> {
  return caseRequest(`/bots/${id}/approve`, { method: "POST" });
}

export async function listBotJobs(params?: {
  status?: string; case_id?: string; bot_id?: string;
}): Promise<BotJob[]> {
  const q = new URLSearchParams();
  if (params?.status) q.set("status", params.status);
  if (params?.case_id) q.set("case_id", params.case_id);
  if (params?.bot_id) q.set("bot_id", params.bot_id);
  const suffix = q.toString() ? `?${q}` : "";
  const d = await caseRequest<{ jobs: BotJob[] }>(`/bots/jobs${suffix}`);
  return d.jobs ?? [];
}

export async function runBot(id: string, body: {
  input?: Record<string, unknown>; case_id?: string; step_id?: string;
}): Promise<BotJob> {
  return caseRequest(`/bots/${id}/run`, { method: "POST", body: JSON.stringify(body) });
}

/** Creates a bot **draft**. It cannot run until a second person approves it. */
export async function createBot(
  body: Partial<BotDefinition>,
): Promise<BotDefinition> {
  return caseRequest("/bots", { method: "POST", body: JSON.stringify(body) });
}

/** **Any change returns the bot to draft**, clearing its approver — editing an
 *  approved container image without a second review is the thing the approval
 *  gate exists to prevent. Saving with nothing changed changes nothing. */
export async function updateBot(
  id: string, body: Partial<BotDefinition>,
): Promise<BotDefinition> {
  return caseRequest(`/bots/${id}`, { method: "PUT", body: JSON.stringify(body) });
}

export async function disableBot(id: string):
  Promise<{ disabled: boolean; cancelled_jobs: number }> {
  return caseRequest(`/bots/${id}/disable`, { method: "POST" });
}

export async function listBotCredentials(): Promise<BotCredential[]> {
  const d = await caseRequest<{ credentials: BotCredential[] }>("/bots/credentials");
  return d.credentials ?? [];
}

/** The value is write-only: it is encrypted under the tenant's key and is never
 *  returned to anyone, including whoever stored it. */
export async function upsertBotCredential(body: {
  ref: string; secret: string; tenant_id?: string;
}): Promise<{ ref: string; version: number; env_name: string }> {
  return caseRequest("/bots/credentials", {
    method: "PUT", body: JSON.stringify(body),
  });
}

export interface BotJobAudit {
  job: BotJob;
  events: { kind: string; detail: Record<string, unknown>; at: string | null }[];
  /** The reference and the lease window — never the value, which nothing can
   *  read back at all. */
  credentials: { ref: string; leased_at: string | null;
                 expires_at: string | null; revoked_at: string | null }[];
  /** Every host the container tried to reach, allowed or blocked, from the
   *  egress gateway's own log. */
  network: { destination: string; ip: string | null; status: string;
             declared: boolean; bytes_out: number | null;
             bytes_in: number | null; at: string | null }[];
}

/** The whole account of one run: what it did, which credentials it held while
 *  doing it, and every host it tried to reach. */
export async function getBotJob(id: string): Promise<BotJobAudit> {
  return caseRequest(`/bots/jobs/${id}`);
}

export async function getBotBatch(batchId: string): Promise<BotBatch> {
  return caseRequest(`/bots/batches/${batchId}`);
}

export async function getBotLimits(): Promise<{
  default: number;
  limits: { tenant_id: string; max_concurrent_jobs: number;
            updated_by: string; updated_at: string | null }[];
}> {
  return caseRequest("/bots/limits");
}

export async function setBotLimit(body: {
  tenant_id: string; max_concurrent_jobs: number;
}): Promise<{ tenant_id: string; max_concurrent_jobs: number }> {
  return caseRequest("/bots/limits", { method: "PUT", body: JSON.stringify(body) });
}

/* ── Picker sources for the scheduler editor ─────────────────────── */

/** Named things a schedule can point at. The editor asks for these by name so
 *  nobody has to paste a UUID into a JSON box to say "run this bot". */
export async function listSchedulerTargets(): Promise<{
  bots: { id: string; name: string; status: string }[];
  connectors: { id: string; name: string; enabled: boolean }[];
  emailAccounts: { id: string; name: string; address: string }[];
  syncDestinations: { id: string; name: string }[];
}> {
  const safe = async <T,>(fn: () => Promise<T>, fallback: T): Promise<T> => {
    // One unavailable list must not empty the whole editor: a deployment may
    // legitimately have no mailer, no warehouse, or no connectors at all.
    try { return await fn(); } catch { return fallback; }
  };
  const [bots, connectors, accounts, destinations] = await Promise.all([
    safe(() => listBots(), [] as BotDefinition[]),
    safe(() => caseRequest<{ connectors?: { id: string; name: string; enabled: boolean }[] }>(
      "/hxbridge/connectors"), {}),
    safe(() => caseRequest<{ accounts?: { id: string; name: string; address: string }[] }>(
      "/email/accounts"), {}),
    safe(() => caseRequest<{ destinations?: { id: string; name: string }[] }>(
      "/sync/destinations"), {}),
  ]);
  const asArray = <T,>(v: unknown, key: string): T[] =>
    Array.isArray(v) ? v as T[] : ((v as Record<string, unknown>)?.[key] as T[]) ?? [];
  return {
    bots: bots.map(b => ({ id: b.id, name: b.name, status: b.status })),
    connectors: asArray(connectors, "connectors"),
    emailAccounts: asArray(accounts, "accounts"),
    syncDestinations: asArray(destinations, "destinations"),
  };
}

/* ── The scheduler ───────────────────────────────────────────────── */

export interface Schedule {
  id: string; tenant_id: string; name: string; description: string;
  cron: string; timezone: string; action_kind: string;
  action_config: Record<string, unknown>;
  status: string; created_by: string; approved_by: string | null;
  approved_at: string | null; next_run_at: string | null;
  last_run_at: string | null; last_status: string | null;
  last_error: string | null;
}

export interface ScheduleRun {
  id: string; status: string; fired_at: string | null;
  duration_ms: number | null; detail: Record<string, unknown>;
}

export async function listSchedules(): Promise<Schedule[]> {
  const d = await caseRequest<{ schedules: Schedule[] }>("/scheduler");
  return d.schedules ?? [];
}

export async function getSchedule(id: string): Promise<{
  schedule: Schedule; next: string[]; runs: ScheduleRun[];
}> {
  return caseRequest(`/scheduler/${id}`);
}

export async function listScheduleActions():
  Promise<{ kind: string; label: string }[]> {
  const d = await caseRequest<{ actions: { kind: string; label: string }[] }>(
    "/scheduler/actions");
  return d.actions ?? [];
}

/** The next few firing times for a cron line, so nobody saves one they misread. */
export async function previewSchedule(body: {
  cron: string; timezone?: string; count?: number;
}): Promise<{ cron: string; timezone: string; next: string[] }> {
  return caseRequest("/scheduler/preview", {
    method: "POST", body: JSON.stringify(body),
  });
}

export async function createSchedule(body: Partial<Schedule>): Promise<Schedule> {
  return caseRequest("/scheduler", { method: "POST", body: JSON.stringify(body) });
}

/** **Any change returns the schedule to draft** and clears its next firing —
 *  a changed recipient list or cron line is exactly what the reviewer needed
 *  to see. Saving with nothing changed changes nothing. */
export async function updateSchedule(
  id: string, body: Partial<Schedule>,
): Promise<Schedule> {
  return caseRequest(`/scheduler/${id}`, {
    method: "PUT", body: JSON.stringify(body),
  });
}

/** Refused (409) when the approver wrote it — approval IS the review. */
export async function approveSchedule(id: string): Promise<Schedule> {
  return caseRequest(`/scheduler/${id}/approve`, { method: "POST" });
}

export async function disableSchedule(id: string): Promise<Schedule> {
  return caseRequest(`/scheduler/${id}/disable`, { method: "POST" });
}

/** Fires it now. The timetable is untouched, so testing a schedule does not
 *  silently skip tonight's run. */
export async function runScheduleNow(id: string): Promise<{
  status: string; detail: Record<string, unknown>;
}> {
  return caseRequest(`/scheduler/${id}/run`, { method: "POST" });
}

export async function deleteSchedule(id: string): Promise<void> {
  await caseRequest(`/scheduler/${id}`, { method: "DELETE" });
}

export async function cancelBotJob(id: string): Promise<BotJob> {
  return caseRequest(`/bots/jobs/${id}/cancel`, { method: "POST" });
}

export async function getBotProposal(id: string): Promise<BotProposal> {
  return caseRequest(`/bots/jobs/${id}/proposal`);
}

/** The checksum is the concurrency guard: a case that changed while the diff
 *  was open answers 409 rather than being silently overwritten. */
export async function acceptBotProposal(
  id: string, expectedChecksum: string, fields?: string[],
): Promise<{ applied: string[] }> {
  return caseRequest(`/bots/jobs/${id}/accept`, {
    method: "POST",
    body: JSON.stringify({ expected_checksum: expectedChecksum, fields }),
  });
}
