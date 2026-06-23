/**
 * Typed REST client for the agent API (src/roastpilot_agent/api.py).
 *
 * One backend authority: the SPA renders only from these routes, the SSE
 * stream, and snapshots — it never calls MCP. Every method maps 1:1 to a route
 * in `api.py` and returns the typed model from `./types`.
 */

import type {
  BeanProfile,
  BeanProfileDeleteResult,
  BeanProfileInput,
  BeanProfileList,
  HealthResponse,
  OperatorActionRequest,
  OperatorActionResult,
  OperatorRatingRequest,
  RoastDetail,
  RoastHistory,
  RoastProfile,
  RoastTimeline,
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
  ) {
    super(`API ${status}: ${detail}`);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!response.ok) {
    throw new ApiError(response.status, await readDetail(response));
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
  createBeanProfile: (input: BeanProfileInput) =>
    request<BeanProfile>("/api/bean-profiles", {
      method: "POST",
      body: JSON.stringify(input),
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
};
