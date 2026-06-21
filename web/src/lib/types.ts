/**
 * TypeScript mirror of the agent's REST + SSE contract
 * (src/roastpilot_agent/models.py).
 *
 * Hand-written rather than codegen'd: the contract is small and stable, and a
 * deliberate mirror forces a conscious edit when the Python side moves (a
 * codegen drift would land silently). Every union member equals the Python
 * enum's `.value` byte-for-byte. All temperatures are Celsius.
 */

// --- Phases (models.RoastPhase) — the operator-facing truth (component plan §3).
// PHASE COMES FROM THE SERVER ONLY: this union is the vocabulary the SPA
// *renders*, never one it derives. The SSE reducer sets phase solely from
// `phase_changed` events + the hydrate snapshot — never inferred from telemetry.
export type RoastPhase =
  | "idle"
  | "starting"
  | "preheating"
  | "roasting_pre_first_crack"
  | "development"
  | "cooling"
  | "complete"
  | "faulted"
  | "operator_recovery_required";

/** Phases where the chart's preheat charge band is shown (preheating only). */
export const CHARGE_BAND_PHASE: RoastPhase = "preheating";

// --- SSE event types (models.SseEventType) — the `event:` field of every frame.
export type SseEventType =
  | "run_started"
  | "phase_changed"
  | "charge_guidance"
  | "t0_detected"
  | "first_crack"
  | "advisory"
  | "command_executed"
  | "command_failed"
  | "safety_alert"
  | "fault"
  | "recovery_required"
  | "recovery_acknowledged"
  | "logs_exported"
  | "run_completed"
  | "telemetry"
  | "heartbeat";

/** One typed SSE frame (models.SseEvent). `data` shape varies by `event`. */
export interface SseEvent<T = Record<string, unknown>> {
  event: SseEventType;
  data: T;
  id?: number | null;
}

// --- Microphone / first-crack capture-alive health (models.MicHealth /
// models.MicStatus, #197). Pure observability: a read-only projection of the MCP
// first-crack pipeline the SPA renders as a green/red/amber mic icon. NEVER a
// control or safety signal, and NEVER inferred client-side — it rides the server's
// telemetry frame + the run snapshot, identical to every other rendered field.

/** The derived health the icon color maps to (`MicHealth`'s `.value`):
 *  `ok` → green, `error` → red, `idle` → amber/grey. `null` mic_status → idle. */
export type MicHealth = "ok" | "error" | "idle";

/** The MCP first-crack runtime status (models.FirstCrackStatusLiteral). */
export type FcStatus =
  | "disabled"
  | "manual"
  | "pending"
  | "detected"
  | "faulted"
  | "unavailable";

/**
 * Capture-alive health of the mic / first-crack audio pipeline (models.MicStatus).
 *
 * `mic_health` is the value the icon tints by; the remaining fields back the
 * hover tooltip. Carries only counters the MCP already computes (Pi performance:
 * no per-window RMS/level work). The configured device NAME is deliberately
 * absent — the contract does not promise it.
 */
export interface MicStatus {
  mic_health: MicHealth;
  audio_running: boolean;
  fc_status: FcStatus;
  queued_window_count: number;
  emitted_window_count: number;
  dropped_window_count: number;
  processed_window_count: number;
  reason: string | null;
}

// --- Per-tick telemetry payload (models.TelemetryEventData). The live reading
// the SPA renders each tick; carries the server-authoritative phase.
export interface TelemetryEventData {
  agent_phase: RoastPhase;
  bean_temp_c: number;
  env_temp_c: number;
  bean_ror_c_per_min: number | null;
  env_ror_c_per_min: number | null;
  heat_percent: number | null;
  fan_percent: number | null;
  cooling_on: boolean;
  elapsed_seconds: number | null;
  // Development time + DTR (#220), server-authoritative. Both null before first
  // crack (the readouts show "—"). `development_elapsed_seconds` is the duration
  // since FC; `development_percent` is DTR — that duration as a share of the
  // WHOLE roast on the CHARGE-referenced clock (consistent with the advisor's
  // DTR, #219), NOT the run/serve clock. Two DISTINCT readouts, not a ratio of
  // each other. The SPA renders these directly (no client-side derivation).
  development_elapsed_seconds: number | null;
  development_percent: number | null;
  t0_detected: boolean;
  first_crack_detected: boolean;
  // Capture-alive mic / first-crack health (#197); nullable — null = no active
  // session / no info, which the icon renders as idle (NOT error/red).
  mic_status: MicStatus | null;
}

/**
 * Payload of a `phase_changed` SSE frame — the sole driver of phase in the UI.
 *
 * The wire shape is `{phase, enabled_actions}`: the controller emits `{"phase":
 * <value>}` (controller.py) and the API enriches it with `enabled_actions`
 * (api.py `_phase_changed_with_actions`). NOTE the field is `phase`, NOT
 * `agent_phase` — the latter is the `RoastDetail` snapshot's field (hydrate
 * path), a deliberately different shape from this event.
 */
export interface PhaseChangedEventData {
  phase: RoastPhase;
  // The E7 `enabled_actions` contract (option (a), D25) — the action bar mirrors
  // this server-provided set and never hardcodes a command×phase matrix.
  enabled_actions?: OperatorAction[];
}

// --- Operator actions (models.OperatorAction) — the POST body action values.
export type OperatorAction =
  | "mark_beans_added"
  | "mark_first_crack"
  | "pause_advisory"
  | "resume_advisory"
  | "drop_beans"
  | "start_cooling"
  | "stop_cooling"
  | "emergency_stop"
  | "acknowledge_recovery"
  | "acknowledge_fault";

export interface OperatorActionRequest {
  action: OperatorAction;
  payload?: Record<string, unknown> | null;
}

export interface OperatorActionResult {
  action: OperatorAction;
  result: "accepted" | "rejected" | "failed";
  reason: string;
  queued: boolean;
}

// --- Safety verdicts (safety.SafetyVerdict / D15) — six values, three badges.
// Wire form is lowercase (models.TimelineVerdict). The verdict helper maps these
// to the three advisory badges; the other three are modal/banner/phase, not badges.
export type SafetyVerdict =
  | "allow"
  | "clamp"
  | "reject"
  | "recovery"
  | "fault"
  | "emergency_stop";

// --- REST response models (models.py §6) ---

export type RoastOutcome = "completed" | "aborted" | "faulted";

/** Botanical bean species (#164) — mirrors `models.BeanSpecies` (a Literal, not
 *  an enum). Distinct from `bean_varietal` (cultivar). */
export type BeanSpecies = "arabica" | "robusta" | "liberica" | "excelsa";

/** Post-harvest processing method (#291) — mirrors `models.ProcessingMethod` (a
 *  Literal, not an enum). One of the per-origin axes the learning loop (D42)
 *  keys on; distinct from any free-text process notes in `description`. */
export type ProcessingMethod =
  | "washed"
  | "natural"
  | "honey"
  | "anaerobic"
  | "wet_hulled"
  | "other";

export interface RoastProfile {
  name: string;
  bean_origin: string;
  bean_varietal: string | null;
  // Bean identity (#164): all optional / defaulted for back-compat with frozen
  // pre-#164 profiles. For a blend (`is_blend`), the structured fields describe
  // the primary bean and the secondaries are recorded in `description`.
  country?: string | null;
  farm?: string | null;
  description?: string | null;
  bean_species?: BeanSpecies | null;
  is_blend?: boolean;
  // Per-origin learning-loop axes (#291): post-harvest process + growing altitude.
  // Optional / defaulted for back-compat with frozen pre-#291 profiles.
  processing?: ProcessingMethod | null;
  altitude_m?: number | null;
  bean_weight_grams: number;
  charge_guidance_min_c: number;
  charge_guidance_max_c: number;
  initial_heat_percent: number;
  initial_fan_percent: number;
  target_drop_temp_c: number;
  target_development_percent: number;
}

// --- Bean-profile library (#303, models.BeanProfile / BeanProfileInput /
// BeanProfileList) — the Start-Roast dropdown's saved-profile contract (D45).
//
// A `BeanProfile` is every reusable `RoastProfile` field EXCEPT the per-roast
// `bean_weight_grams`, PLUS a server-owned `id` + `created_at` / `updated_at`
// timestamps and a `default_bean_weight_grams` that pre-fills (but does not fix)
// each roast's charge weight. `BeanProfileInput` is the POST/PUT body (the same
// reusable fields + `default_bean_weight_grams`, WITHOUT the server-owned id /
// timestamps). All temperatures are Celsius. Editing a saved profile only affects
// FUTURE roasts — a started roast freezes its own `RoastProfile` snapshot.

/** The reusable saved-profile fields shared by `BeanProfile` and
 *  `BeanProfileInput` — every `RoastProfile` field except the per-roast
 *  `bean_weight_grams`, plus the template's `default_bean_weight_grams`. */
export interface BeanProfileFields {
  name: string;
  bean_origin: string;
  bean_varietal: string | null;
  country?: string | null;
  farm?: string | null;
  description?: string | null;
  bean_species?: BeanSpecies | null;
  is_blend?: boolean;
  processing?: ProcessingMethod | null;
  altitude_m?: number | null;
  charge_guidance_min_c: number;
  charge_guidance_max_c: number;
  initial_heat_percent: number;
  initial_fan_percent: number;
  target_drop_temp_c: number;
  target_development_percent: number;
  /** Pre-fills (but does not fix) each roast's charge weight; adjustable per roast. */
  default_bean_weight_grams: number;
}

/** `POST` / `PUT /api/bean-profiles` request body (models.BeanProfileInput). */
export type BeanProfileInput = BeanProfileFields;

/** A saved bean-profile library entry (models.BeanProfile) — the create/update/get
 *  response and the dropdown's row. Adds the server-owned id + timestamps. */
export interface BeanProfile extends BeanProfileFields {
  id: string;
  created_at: string;
  updated_at: string;
}

/** `GET /api/bean-profiles` envelope (models.BeanProfileList). */
export interface BeanProfileList {
  profiles: BeanProfile[];
}

/** `DELETE /api/bean-profiles/{id}` response (api.delete_bean_profile). */
export interface BeanProfileDeleteResult {
  id: string;
  result: "archived";
}

export interface LogManifest {
  log_dir: string;
  jsonl_path: string;
  csv_path: string;
  summary_path: string;
  ready: boolean;
  note: string | null;
}

export interface RoastSummary {
  id: string;
  started_at_utc: string;
  completed_at_utc: string | null;
  // UTC ISO-8601 first-crack time (#111), projected server-side from the earliest
  // persisted `first_crack` roast event; `null` when the run never reached first
  // crack (back-compat). The history FC-time column renders from this.
  first_crack_at_utc: string | null;
  agent_phase: RoastPhase;
  outcome: RoastOutcome | null;
  bean_origin: string;
  bean_varietal: string | null;
  // Bean identity (#164) projected from the frozen profile; typed optional for
  // forward/back-compat tolerance (a pre-#164 build or fixture may omit them).
  // The current server always serializes `is_blend` (it defaults to `false`),
  // so consumers treat a missing value identically to `false`.
  country?: string | null;
  bean_species?: BeanSpecies | null;
  is_blend?: boolean;
  // #291 per-origin axes projected from the frozen profile; optional for
  // forward/back-compat (a pre-#291 build or fixture may omit them).
  processing?: ProcessingMethod | null;
  altitude_m?: number | null;
  rating: number | null;
  development_percent: number | null;
  // Advisor stats (#184) aggregated server-side from `advisor_decisions`, so the
  // history advisor column renders without N+1ing `GET /api/roasts/{id}/timeline`.
  // `advisor_consults` is every persisted consult; `advisor_failed` the non-`ok`
  // statuses; `advisor_clamped` / `advisor_rejected` count a consult against the
  // safety verdict at its tick. All default to `0` for a run with no consults.
  advisor_consults: number;
  advisor_clamped: number;
  advisor_rejected: number;
  advisor_failed: number;
}

export interface RoastHistory {
  runs: RoastSummary[];
}

/** `GET /api/roasts/{id}` — the hydrate snapshot the SSE hook reads on connect. */
export interface RoastDetail {
  id: string;
  agent_phase: RoastPhase;
  profile: RoastProfile;
  outcome: RoastOutcome | null;
  started_at_utc: string;
  completed_at_utc: string | null;
  fault_reason: string | null;
  rating: number | null;
  notes: string | null;
  export_manifest: LogManifest | null;
  // Forward-compatible with the planned E7 `enabled_actions` addition (option
  // (a), separate PR). Optional until that contract change lands.
  enabled_actions?: OperatorAction[];
  // Capture-alive mic / first-crack health (#197), server-derived. Populated only
  // for the *active* run (the live MCP state); historical runs carry null. Lets the
  // header paint the mic icon on first paint, before the first telemetry frame.
  mic_status?: MicStatus | null;
}

export interface TelemetryPoint {
  tick: number;
  elapsed_seconds: number | null;
  agent_phase: RoastPhase;
  bean_temp_c: number | null;
  env_temp_c: number | null;
  bean_ror_c_per_min: number | null;
  env_ror_c_per_min: number | null;
  heat_level_percent: number | null;
  fan_level_percent: number | null;
  cooling_on: boolean | null;
  development_percent: number | null;
}

export interface TelemetrySeries {
  run_id: string;
  downsample: number;
  point_count: number;
  points: TelemetryPoint[];
}

// --- Decision-trace timeline (models.RoastTimeline) ---

export type RoastEventKind =
  | "run_started"
  | "phase_changed"
  | "charge_guidance"
  | "t0_detected"
  | "first_crack"
  | "advisory"
  | "command_executed"
  | "command_failed"
  | "safety_alert"
  | "fault"
  | "recovery_required"
  | "recovery_acknowledged"
  | "logs_exported"
  | "run_completed";

export type RoastEventSource =
  | "controller"
  | "mcp"
  | "operator"
  | "advisor"
  | "safety";

export type AdvisorTraceStatus = "ok" | "timeout" | "malformed" | "provider_error";
export type CommandTraceStatus = "ok" | "failed";
export type CommandTraceSource =
  | "policy"
  | "advisor"
  | "operator"
  | "safety"
  | "recovery";

// --- MCP write-command tool names (models.RoastCommand) — the `tool` field of a
// timeline command. Mirrors the Python enum's wire values so the decision-trace
// timeline carries a literal union, not a bare string.
export type RoastCommandTool =
  | "start_roast_session"
  | "set_heat"
  | "set_fan"
  | "mark_beans_added"
  | "mark_first_crack"
  | "drop_beans"
  | "start_cooling"
  | "stop_cooling"
  | "export_roast_log"
  | "emergency_stop";

export interface TimelineEvent {
  kind: RoastEventKind;
  source: RoastEventSource;
  monotonic_seconds: number | null;
  recorded_at_utc: string;
  payload: Record<string, unknown> | null;
}

export interface TimelineSafetyEvaluation {
  tick: number;
  rule: string;
  verdict: SafetyVerdict;
  input_heat: number | null;
  input_fan: number | null;
  adjusted_heat: number | null;
  adjusted_fan: number | null;
  reason: string;
  recorded_at_utc: string;
}

export interface TimelineAdvisorDecision {
  tick: number;
  provider: string;
  model: string;
  prompt_version: string;
  latency_ms: number | null;
  status: AdvisorTraceStatus;
  decision: Record<string, unknown> | null;
  recorded_at_utc: string;
}

export interface TimelineCommand {
  tick: number;
  tool: RoastCommandTool;
  source: CommandTraceSource;
  status: CommandTraceStatus;
  args: Record<string, unknown> | null;
  result: Record<string, unknown> | null;
  recorded_at_utc: string;
}

export interface RoastTimeline {
  run_id: string;
  events: TimelineEvent[];
  safety_evaluations: TimelineSafetyEvaluation[];
  advisor_decisions: TimelineAdvisorDecision[];
  commands: TimelineCommand[];
}

export interface OperatorRatingRequest {
  stars: 1 | 2 | 3 | 4 | 5;
  notes?: string | null;
}

// --- Health (models.HealthResponse) ---

export type MCPChildStatus = "running" | "stopped" | "not_configured";

export interface HealthResponse {
  status: "ok";
  version: string;
  mcp_child: MCPChildStatus;
  active_run_id: string | null;
}
