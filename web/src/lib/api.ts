/**
 * Typed REST client for the agent API (src/roastpilot_agent/api.py).
 *
 * One backend authority: the SPA renders only from these routes, the SSE
 * stream, and snapshots — it never calls MCP. Every method maps 1:1 to a route
 * in `api.py` and returns the typed model from `./types`.
 */

import type {
  AppConfigSnapshot,
  BeanProfile,
  BeanProfileDeleteResult,
  BeanProfileDraftResponse,
  BeanProfileInput,
  BeanProfileList,
  ChargeWeightRequest,
  ClearStaleSessionRequest,
  ClearStaleSessionResult,
  DevicesSnapshot,
  HealthResponse,
  OperatorActionRequest,
  OperatorActionResult,
  OperatorRatingRequest,
  RoastDetail,
  RoastHistory,
  RoastedWeightRequest,
  RoastProfile,
  RoastTimeline,
  TastingEntryRequest,
  TastingList,
  TelemetrySeries,
} from "./types";

/** API base. Empty string in the browser: requests are same-origin and Vite
 *  proxies `/api` to the agent in dev. Overridable for tests/SSR. */
export const API_BASE = "";

/** A failed API response, carrying the HTTP status and the server's detail. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
    readonly code?: string,
  ) {
    super(`API ${status}: ${detail}`);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    throw new ApiError(
      response.status,
      await readDetail(response),
      response.headers.get("X-RoastPilot-Conflict-Code") ?? undefined,
    );
  }
  // 204/empty bodies never occur on these routes, but guard anyway.
  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

async function readDetail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
    return JSON.stringify(body.detail ?? body);
  } catch {
    return response.statusText;
  }
}

export const api = {
  /** `GET /api/health`. */
  health: () => request<HealthResponse>("/api/health"),

  /** `POST /api/roasts` — start a roast (409 if one is active). */
  startRoast: (profile: RoastProfile) =>
    request<RoastDetail>("/api/roasts", {
      method: "POST",
      body: JSON.stringify(profile),
    }),

  /** `GET /api/roasts` — history list, newest first. */
  history: () => request<RoastHistory>("/api/roasts"),

  /** `GET /api/roasts/{id}` — run detail (the SSE hydrate snapshot). */
  roast: (runId: string) => request<RoastDetail>(`/api/roasts/${runId}`),

  /** `GET /api/roasts/{id}/telemetry` — downsampled snapshot series. */
  telemetry: (runId: string, downsample = 1) =>
    request<TelemetrySeries>(
      `/api/roasts/${runId}/telemetry?downsample=${downsample}`,
    ),

  /** `GET /api/roasts/{id}/timeline` — the decision trace. */
  timeline: (runId: string) =>
    request<RoastTimeline>(`/api/roasts/${runId}/timeline`),

  /** `POST /api/roasts/{id}/rating` — operator self-rating. */
  rate: (runId: string, rating: OperatorRatingRequest) =>
    request<RoastDetail>(`/api/roasts/${runId}/rating`, {
      method: "POST",
      body: JSON.stringify(rating),
    }),

  /** `POST /api/roasts/{id}/roasted-weight` — operator roasted-out weight (#388). */
  setRoastedWeight: (runId: string, body: RoastedWeightRequest) =>
    request<RoastDetail>(`/api/roasts/${runId}/roasted-weight`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** `POST /api/roasts/{id}/charge-weight` — operator charge-weight correction (#520). */
  setChargeWeight: (runId: string, body: ChargeWeightRequest) =>
    request<RoastDetail>(`/api/roasts/${runId}/charge-weight`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** `POST /api/roasts/{id}/discard` — soft-exclude a bad-data roast (#582).
   *  Reversible (see `restoreRoast`); 404 unknown run, 409 in-progress. */
  discardRoast: (runId: string) =>
    request<RoastDetail>(`/api/roasts/${runId}/discard`, { method: "POST" }),

  /** `POST /api/roasts/{id}/restore` — reverse a discard (#582). */
  restoreRoast: (runId: string) =>
    request<RoastDetail>(`/api/roasts/${runId}/restore`, { method: "POST" }),

  /** `POST /api/roasts/{id}/clear-stale-session` — finalise a stranded STALE
   *  run (#525). A pure store write: issues no MCP command, never touches
   *  heat/fan/cooling. 404 unknown run; 409 if it's the process's own
   *  tracked active/recovering run, already finalized, or shows recent
   *  telemetry (actively driven by some process). */
  clearStaleSession: (runId: string, body: ClearStaleSessionRequest) =>
    request<ClearStaleSessionResult>(`/api/roasts/${runId}/clear-stale-session`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** `GET /api/roasts/{id}/tastings` — the run's tasting entries (#522, D91). */
  tastings: (runId: string) => request<TastingList>(`/api/roasts/${runId}/tastings`),

  /** `POST /api/roasts/{id}/tastings` — record a tasting entry (#522, D91).
   *  Always appends a NEW entry (a revisit tasting is never an overwrite);
   *  returns the full updated list. */
  addTasting: (runId: string, body: TastingEntryRequest) =>
    request<TastingList>(`/api/roasts/${runId}/tastings`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** `POST /api/roasts/{id}/operator-actions` — queue an operator action. */
  operatorAction: (runId: string, action: OperatorActionRequest) =>
    request<OperatorActionResult>(`/api/roasts/${runId}/operator-actions`, {
      method: "POST",
      body: JSON.stringify(action),
    }),

  /** Download URL for an export artifact (jsonl/csv/summary). */
  logArtifactUrl: (runId: string, artifact: "jsonl" | "csv" | "summary") =>
    `${API_BASE}/api/roasts/${runId}/log/${artifact}`,

  /** SSE stream URL for a run (consumed by the EventSource hook). */
  eventsUrl: (runId: string, lastEventId?: number | null) =>
    // `last_event_id` query param mirrors the SSE `Last-Event-ID` header for the
    // hook's EXPLICIT reconnect (#339): a freshly-constructed EventSource sends no
    // header, so on our own backoff path we carry the last applied id here and the
    // server replays the buffered gap. Native EventSource auto-reconnect still
    // uses the header; the server honors either.
    typeof lastEventId === "number"
      ? `${API_BASE}/api/roasts/${runId}/events?last_event_id=${lastEventId}`
      : `${API_BASE}/api/roasts/${runId}/events`,

  // --- Bean-profile library (#303, D45) — the Start-Roast dropdown's CRUD. ---

  /** `GET /api/bean-profiles` — the saved bean-profile library, name-ordered. */
  beanProfiles: () => request<BeanProfileList>("/api/bean-profiles"),

  /** `POST /api/bean-profiles` — create a saved profile (201; 422 invalid). */
  createBeanProfile: (input: BeanProfileInput, draftAttemptId?: string) =>
    request<BeanProfile>("/api/bean-profiles", {
      method: "POST",
      body: JSON.stringify(input),
      headers:
        draftAttemptId === undefined
          ? undefined
          : { "X-RoastPilot-Draft-Attempt-Id": draftAttemptId },
    }),

  /** `PUT /api/bean-profiles/{id}` — edit a saved profile (200; 404/422). Edits
   *  affect future roasts only — the backend never mutates a past roast's frozen
   *  snapshot. */
  updateBeanProfile: (id: string, input: BeanProfileInput) =>
    request<BeanProfile>(`/api/bean-profiles/${id}`, {
      method: "PUT",
      body: JSON.stringify(input),
    }),

  /** `DELETE /api/bean-profiles/{id}` — archive (soft-delete) a profile (200; 404). */
  deleteBeanProfile: (id: string) =>
    request<BeanProfileDeleteResult>(`/api/bean-profiles/${id}`, {
      method: "DELETE",
    }),

  /** `POST /api/beans/draft-from-url` — draft a bean profile from a vendor
   *  product page (#573 phase 1, #637). Creates no saved profile; a sanitized
   *  baseline supports bounded correction correlation until claim/cleanup.
   *  Saving stays the operator's explicit `createBeanProfile` action. 422: bad/unreachable
   *  URL, or the page yielded too little identity to draft from. 503: the
   *  extraction provider/transport failed — the page itself may be fine,
   *  retry. 409: a roast is active. 429: too many concurrent draft requests
   *  in flight, try again shortly. An optional `signal` lets a caller abort
   *  the fetch — currently UNUSED by any caller (#654 verdict round): the
   *  modal's own invalidation deliberately does NOT abort, since the backend
   *  has no disconnect check on this route and `AbortController.abort()`
   *  settles the fetch's promise immediately on the client regardless — a
   *  caller that aborted and then released its own in-flight guard on that
   *  abort-triggered settle would risk firing a fresh request into the
   *  backend's still-occupied one-at-a-time admission slot. Left threaded
   *  through as an available capability for a future caller with a genuine
   *  cancel need, not a signal this route's current caller relies on. */
  draftBeanFromUrl: (url: string, signal?: AbortSignal) =>
    request<BeanProfileDraftResponse>("/api/beans/draft-from-url", {
      method: "POST",
      body: JSON.stringify({ url }),
      signal,
    }),

  // --- Config (#419, D78) ---

  /** `GET /api/config` — full config snapshot with per-field metadata. */
  config: () => request<AppConfigSnapshot>("/api/config"),

  /**
   * `PUT /api/config` — partial update (controller + advisor only; safety is
   * read-only). The body is a partial nested object matching `AppConfigEdit`
   * on the server. Returns the updated `AppConfigSnapshot`.
   */
  saveConfig: (edit: Record<string, unknown>) =>
    request<AppConfigSnapshot>("/api/config", {
      method: "PUT",
      body: JSON.stringify(edit),
    }),

  /** `GET /api/config/devices` — enumerate connected serial + audio devices. */
  devices: () => request<DevicesSnapshot>("/api/config/devices"),
};
