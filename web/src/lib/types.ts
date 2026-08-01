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
  | "turning_point"
  | "drying_end"
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
  // Overflow diagnostics (MCP 0.1.13, coffee-roaster-mcp#190, #539):
  // capture-side frame-loss visibility, surfaced in the dashboard
  // diagnostics drawer (plan §7's anticipated audio pipeline counters).
  overflow_count_last_minute: number;
  estimated_lost_audio_ms_last_minute: number;
  total_overflow_count: number;
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
  // Charge-referenced roast clock (#308): seconds since charge/T0 — the
  // operator-facing ROAST TIME and the chart x-axis origin (Artisan convention,
  // 0:00 = charge). `null` PRE-charge (preheat), since-charge after, FROZEN at
  // drop. Distinct from `elapsed_seconds`, which stays serve-referenced (seconds
  // since the run started) and now backs only the preheat display. Server-
  // authoritative (controller `_charge_elapsed_seconds`, the same charge/T0
  // instant the advisor's DTR uses); never derived client-side.
  charge_elapsed_seconds: number | null;
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
  // Live/latest ambient triad (#464, D86), mirrored each tick from the MCP's
  // ~30 s-cached ambient status — the SAME mirror-and-render pattern as
  // `mic_status` above. `null` per-field when ambient is uncaptured, disabled,
  // or unavailable this tick. DISTINCT from `RoastDetail.ambient_temp_c` (a
  // one-time charge-instant capture, #342/D85); this is the live/current
  // reading. Pure observability — never read by any safety gate or control path.
  ambient_temp_c: number | null;
  ambient_humidity_pct: number | null;
  ambient_pressure_hpa: number | null;
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
  // Product / source URL (#315): where the bean was bought. Optional / defaulted
  // for back-compat with frozen pre-#315 profiles. A validated http(s) URL or null.
  source_url?: string | null;
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
  /** Product / source URL (#315) — where the bean was bought; a validated http(s) URL or null. */
  source_url?: string | null;
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

// --- Draft-from-URL (#573 phase 1, #627, #637): models.BeanProfileDraft. ---

/** Per-field provenance for a `BeanProfileDraftResponse` (models.BeanFieldSource):
 *  `"on_page"` when the value was read off the vendor page, `"origin_estimated"`
 *  when it was imputed (a conservative first-roast target, or a value the page
 *  never stated). A constrained literal, not an enum — bean metadata, not a
 *  safety verdict. */
export type BeanFieldSource = "on_page" | "origin_estimated";

/** `POST /api/beans/draft-from-url` response (models.BeanProfileDraft) — a
 *  drafted, NOT-YET-SAVED profile the operator reviews/edits/saves via the
 *  existing `createBeanProfile` action. It is never persisted as a saved
 *  profile; a sanitized field-value baseline (excluding URL, evidence, and
 *  prose) has a 24-hour correction-correlation deadline and is cleared on claim
 *  or orderly shutdown, or at the deadline (including after restart). Carries
 *  every `BeanProfileFields` field plus honest per-field provenance
 *  (`field_sources`), the model-cited vendor-page quotes backing the four
 *  typed fields (`field_evidence`), and the conservative "scouting run"
 *  framing (`scouting_note`). `is_blend` overrides the shared base's plain
 *  `boolean` with a tri-state: `null` means the page never addressed
 *  blending at all (distinct from the page confirming single-origin). */
export interface BeanProfileDraftResponse extends Omit<BeanProfileFields, "is_blend"> {
  draft_attempt_id: string;
  is_blend: boolean | null;
  field_sources: Record<string, BeanFieldSource>;
  field_evidence: Record<string, string>;
  scouting_note: string;
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
  // Operator roasted-out weight (#388) + derived weight-loss % =
  // (charge - roasted) / charge * 100. `null` until weighed; predominantly
  // moisture but also dry-matter loss, so NOT pure water loss.
  roasted_weight_grams?: number | null;
  // Operator-corrected charge/green weight (#520), or `null` when never
  // corrected. `profile.bean_weight_grams` stays the FROZEN value the
  // controller/advisor actually ran with; this is the physical-truth
  // correction and drives `weight_loss_percent` in its place when present.
  // Always show BOTH values with which one is driving the % explicit.
  corrected_charge_grams?: number | null;
  weight_loss_percent?: number | null;
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
  // Ambient triad captured ONCE at charge (#342, D85) — the "Roast conditions"
  // read the history/detail pages render. Optional/nullable for back-compat
  // (a pre-#342 run, or an ambient-disabled/unavailable MCP config). DISTINCT
  // from the live/latest `TelemetryEventData.ambient_temp_c` (#464), which is
  // the current-reading mirror on the SSE frame, not the charge-time capture.
  ambient_temp_c?: number | null;
  ambient_humidity_pct?: number | null;
  ambient_pressure_hpa?: number | null;
  // Reversible soft-exclude flag (#582). Always `false` here: the history list
  // filters excluded=1 runs out entirely, so a discarded run never appears in
  // this array. See `RoastDetail.excluded`, which DOES surface `true` (a
  // direct link to a discarded run still works). Optional for back-compat
  // with a pre-#582 fixture.
  excluded?: boolean;
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
  // Operator roasted-out weight (#388) + derived weight-loss %. `null` until
  // weighed. The green/charge weight is `profile.bean_weight_grams`.
  roasted_weight_grams?: number | null;
  // Operator-corrected charge/green weight (#520), or `null` when never
  // corrected. See RoastSummary's field doc — same semantics.
  corrected_charge_grams?: number | null;
  weight_loss_percent?: number | null;
  export_manifest: LogManifest | null;
  // Forward-compatible with the planned E7 `enabled_actions` addition (option
  // (a), separate PR). Optional until that contract change lands.
  enabled_actions?: OperatorAction[];
  // Capture-alive mic / first-crack health (#197), server-derived. Populated only
  // for the *active* run (the live MCP state); historical runs carry null. Lets the
  // header paint the mic icon on first paint, before the first telemetry frame.
  mic_status?: MicStatus | null;
  // Ambient triad captured ONCE at charge (#342, D85) — see `RoastSummary`'s
  // fields above for the back-compat / distinct-from-live notes; identical here.
  ambient_temp_c?: number | null;
  ambient_humidity_pct?: number | null;
  ambient_pressure_hpa?: number | null;
  // The server process's instance_id (#516) at the moment this RoastDetail
  // was served. Only ever non-null on the start-roast 201 response — the
  // confirm loop's capture point (see HealthResponse's field doc).
  instance_id?: string | null;
  // Reversible soft-exclude flag (#582) — `true` when the operator has
  // discarded this roast as bad-data (beans fine, but e.g. a detector-missed
  // first crack polluted the derived DTR). The run's telemetry, events,
  // decision trace, and any exported audio are all untouched — a soft flag,
  // never a delete. Optional for back-compat with a pre-#582 fixture.
  excluded?: boolean;
}

export interface TelemetryPoint {
  tick: number;
  elapsed_seconds: number | null;
  // Charge-referenced roast clock (#308), persisted per snapshot: seconds since
  // charge/T0. `null` for pre-charge ticks (the chart lead-in). The curve x-axis
  // re-origins on THIS (0:00 = charge); `elapsed_seconds` stays serve-referenced.
  charge_elapsed_seconds: number | null;
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
  // The post-charge bean-temp minimum landmark (#409): the tick the bean RoR first
  // crosses zero after the charge dip. Persisted to the timeline so the detail page
  // re-hydrates its chart marker on reload. Payload: {bean_temp_c,
  // elapsed_since_charge_seconds}; no tick (not a tick-keyed trace record).
  | "turning_point"
  // The pre-FC drying→browning landmark (#351), persisted to the timeline so the
  // detail page re-hydrates its chart marker on reload. Payload: {bean_temp_c,
  // threshold_c}; no tick (it is not a tick-keyed trace record).
  | "drying_end"
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

/** `POST /api/roasts/{id}/roasted-weight` body (#388). Grams, must be > 0. */
export interface RoastedWeightRequest {
  roasted_weight_grams: number;
}

/** `POST /api/roasts/{id}/charge-weight` body (#520). Grams, must be > 0. */
export interface ChargeWeightRequest {
  corrected_charge_grams: number;
}

/** `POST /api/roasts/{id}/clear-stale-session` body (#525). A required,
 *  non-empty reason — no silent no-reason clears. */
export interface ClearStaleSessionRequest {
  reason: string;
}

/** The outcome of a successful {@link ClearStaleSessionRequest} (#525).
 *  Always `outcome: "aborted"` — this action only ever finalises a stranded
 *  run as abandoned, never reclassifies what happened during it. */
export interface ClearStaleSessionResult {
  run_id: string;
  outcome: "aborted";
  completed_at_utc: string;
}

/** Explicit physical hardware-clear confirmation after an unconfirmed MCP
 * teardown (#668). The incident id binds the decision to the generation the
 * operator inspected; `hardware_clear` is always the literal JSON `true`. */
export interface HardwareClearAcknowledgementRequest {
  hardware_clear: true;
  teardown_incident_id: string;
  reason: string;
}

export interface HardwareClearAcknowledgementResult {
  result: "accepted";
  hardware_clear: true;
  teardown_incident_id: string;
  fresh_spawn_permitted: true;
}

// --- Tastings (models.RoastTasting / TastingEntryRequest / TastingList, #522, D91) ---

export type BrewMethod =
  | "espresso"
  | "pour_over"
  | "french_press"
  | "aeropress"
  | "moka_pot"
  | "drip"
  | "cupping"
  | "other";

/** Positive attribute tags (D91 §4). */
export type TastingAttribute = "sweetness" | "acidity" | "body";

/** Defect tags (D91 §4) — the roast-13 "flat -> grassy" refinement signal. */
export type TastingDefect = "grassy" | "baked" | "bitter" | "flat";

/** `POST /api/roasts/{id}/tastings` body. Every field beyond `stars` is
 *  optional — stars alone is a valid tasting entry. Always appends a NEW
 *  entry; a revisit tasting is never an overwrite. */
export interface TastingEntryRequest {
  stars: 1 | 2 | 3 | 4 | 5;
  notes?: string | null;
  tasted_at_utc?: string | null;
  brew_method?: BrewMethod | null;
  grind_note?: string | null;
  attributes?: TastingAttribute[];
  defects?: TastingDefect[];
}

/** One persisted tasting entry. */
export interface RoastTasting {
  id: number;
  tasted_at_utc: string | null;
  recorded_at_utc: string;
  stars: number;
  notes: string | null;
  brew_method: BrewMethod | null;
  grind_note: string | null;
  attributes: TastingAttribute[];
  defects: TastingDefect[];
}

/** `GET /api/roasts/{id}/tastings` and the POST response envelope. */
export interface TastingList {
  run_id: string;
  tastings: RoastTasting[];
}

// --- Health (models.HealthResponse) ---

export type MCPChildStatus = "running" | "stopped" | "not_configured";

export interface HealthResponse {
  status: "ok";
  version: string;
  // A uuid4 minted once per server process (#516) — never compare this on a
  // passive read (nav chip, dashboard); only the start-roast confirm loop
  // does, to detect a DIFFERENT process answering than the one that
  // accepted the start (the #513 port-impostor signature). See
  // HealthResponse's Python docstring for the full rationale.
  instance_id: string;
  mcp_child: MCPChildStatus;
  mcp_hardware_clear_required: boolean;
  mcp_teardown_incident_id: string | null;
  active_run_id: string | null;
}

// --- Config (config_store.AppConfigSnapshot / GET + PUT /api/config) ---
// Hand-mirror of the Python models; see config_store.py for the canonical schema.
// All safety fields are read_only=true in M1 (D78 decision 2).

/** Per-field metadata returned by GET /api/config (ConfigFieldMeta in Python). */
export interface ConfigFieldMeta {
  /** Value written to the saved-config file; null when not set in the file. */
  saved_value: unknown;
  /** Fully resolved effective value (env override > saved file > default). */
  effective_value: unknown;
  /** Schema default — shown in the "Default <value>" meta line per field.
   *  Named "default" to match the server's JSON key exactly (Python `model_dump`
   *  serialises the `default` field name verbatim). */
  default: unknown;
  /** True when the env var is set in the host environment (not injected from the
   *  saved file by the agent). PR3 renders the env-override badge when it is true,
   *  paired with the static `ConfigFieldDef.envVar` name from the schema.
   *  NOTE: The server does NOT return `env_var` — the name comes from the static
   *  schema; only this boolean is returned per field. */
  env_overridden: boolean;
  /** True when the field cannot be edited from the UI (hardware-pinned or safety). */
  read_only: boolean;
  /** One-sentence description shown in the field's left column. */
  description: string;
  /**
   * The value currently in the hand-authored coffee-roaster-mcp.yaml (#482).
   * Populated only for `mcp_device` fields — the agent's own config layer
   * defaults every such field to `null`, so `effective_value` alone cannot
   * tell the operator what the MCP will actually use when the field is
   * unconfigured (e.g. `fc_mode: null` does NOT mean "FC detection is off" —
   * it means "the yaml's first_crack.mode governs"). `null` for
   * non-`mcp_device` fields, when no hand-authored yaml is resolvable, or
   * when the key is simply absent from it (server fails soft — never a
   * reason for GET /api/config to 500).
   */
  yaml_value: unknown;
}

export interface ControllerConfigSnapshot {
  tick_interval_seconds: ConfigFieldMeta;
  pre_fc_heat_target_percent: ConfigFieldMeta;
  pre_fc_fan_target_percent: ConfigFieldMeta;
  late_maillard_trim_enabled: ConfigFieldMeta;
  late_maillard_trim_heat_percent: ConfigFieldMeta;
  late_maillard_trim_window_fc_eta_seconds: ConfigFieldMeta;
  late_maillard_trim_min_bean_temp_c: ConfigFieldMeta;
  late_maillard_trim_adaptive_depth_enabled: ConfigFieldMeta;
  late_maillard_trim_base_trim: ConfigFieldMeta;
  late_maillard_trim_k_ror: ConfigFieldMeta;
  late_maillard_trim_k_eta: ConfigFieldMeta;
  late_maillard_trim_ror_ref: ConfigFieldMeta;
  late_maillard_trim_eta_ref: ConfigFieldMeta;
  late_maillard_trim_min_trim: ConfigFieldMeta;
  late_maillard_trim_max_trim: ConfigFieldMeta;
  late_maillard_trim_trim_depth_deadband_pp: ConfigFieldMeta;
  late_maillard_trim_trim_depth_slew_pp_per_tick: ConfigFieldMeta;
}

export interface AdvisorConfigSnapshot {
  model_slug: ConfigFieldMeta;
  prompt_version: ConfigFieldMeta;
  provider: ConfigFieldMeta;
  provider_base_url: ConfigFieldMeta;
  /** api_key_env: always read_only=true; saved_value=null; env_overridden=false. */
  api_key_env: ConfigFieldMeta;
  timeout_seconds: ConfigFieldMeta;
  temperature: ConfigFieldMeta;
}

export interface SafetyLimitsSnapshot {
  /** All safety fields are read_only=true in M1 (D78 decision 2). */
  max_bean_temp_c: ConfigFieldMeta;
  max_env_temp_c: ConfigFieldMeta;
  pre_t0_max_bean_temp_c: ConfigFieldMeta;
  overrun_safe_fan_percent: ConfigFieldMeta;
  pre_t0_overrun_severity: ConfigFieldMeta;
  min_seconds_between_commands: ConfigFieldMeta;
  max_consecutive_mcp_failures: ConfigFieldMeta;
  max_consecutive_advisor_failures: ConfigFieldMeta;
  bitter_ceiling_temp_c: ConfigFieldMeta;
  emergency_drop_temp_c: ConfigFieldMeta;
}

/** Managed MCP device fields (MCPDeviceConfigSnapshot in Python, #429). */
export interface MCPDeviceConfigSnapshot {
  serial_port: ConfigFieldMeta;
  roaster_driver: ConfigFieldMeta;
  audio_input_device: ConfigFieldMeta;
  recording_enabled: ConfigFieldMeta;
  recording_autocapture: ConfigFieldMeta;
  recording_devices: ConfigFieldMeta;
  fc_mode: ConfigFieldMeta;
  fc_confidence_threshold: ConfigFieldMeta;
  auto_t0_detection_enabled: ConfigFieldMeta;
  auto_t0_drop_threshold_c: ConfigFieldMeta;
  /** Ambient environmental sensor config (D85, #342/#474). Tri-state — see
   *  serial_port/fc_mode above for the same inherit/override semantics. */
  ambient_mode: ConfigFieldMeta;
  ambient_device: ConfigFieldMeta;
  ambient_poll_interval_seconds: ConfigFieldMeta;
}

/** GET /api/config response body (AppConfigSnapshot in Python). */
export interface AppConfigSnapshot {
  controller: ControllerConfigSnapshot;
  advisor: AdvisorConfigSnapshot;
  safety: SafetyLimitsSnapshot;
  mcp_device: MCPDeviceConfigSnapshot;
}

// AppConfigEdit is not mirrored here — the SPA sends a raw partial object built
// from the form's dirty values. Only controller + advisor + mcp_device fields
// are accepted; safety fields are never sent. The server validates via Pydantic.

// --- Device enumeration (GET /api/config/devices, D78 PR(c), #418) -----------
// Hand-mirror of the Python DeviceOption / DevicesSnapshot models in api.py.
// `value` is the machine id stored in config (a serial port path, or a
// sounddevice index cast to string). `note` is secondary display detail.

/** One enumerated device returned by GET /api/config/devices. */
export interface DeviceOption {
  value: string;
  label: string;
  note: string;
}

/** GET /api/config/devices response body (DevicesSnapshot in Python). */
export interface DevicesSnapshot {
  serial: DeviceOption[];
  serial_error: string | null;
  audio_input: DeviceOption[];
  audio_input_error: string | null;
}
