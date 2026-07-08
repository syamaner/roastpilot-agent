/**
 * Config UI field schema — reconciled from the REAL S1 AppConfigSnapshot API.
 *
 * This file is DATA ONLY (no components, no hooks). It maps the six UI
 * categories to the real field keys returned by GET /api/config and defines
 * per-field display metadata. PR2 and PR3 consume this; do not add component
 * logic here.
 *
 * D78 reconciliations applied (30 Jun 2026):
 *  1. "advisory cadence 15s" → REMOVED (no scalar exists; #171/D32 retired it)
 *  2. "bean drop ceiling 215" → real read-only triad:
 *       max_bean_temp_c / bitter_ceiling_temp_c / emergency_drop_temp_c
 *  3. "command rate limit 60/min" → min_seconds_between_commands (seconds,
 *       read-only, safety)
 *  4. "max heat 100% / max fan 100%" → REMOVED (caps are RoastDecision
 *       0–100 bounds; no SafetyLimits fields)
 *  5. "adaptive gain/damping" → real late_maillard_trim coefficients:
 *       k_ror / k_eta / ror_ref / eta_ref / min_trim / max_trim
 *       + trim_depth_deadband_pp / trim_depth_slew_pp_per_tick (#412, #443)
 *  6. "API key" → masked read-only; label shows `api_key_env` env-var name
 *  7. "controller tick" → read-only 1.0 s (hardware-pinned)
 *  8. Hardware (driver/serialPort/baud) → NOT in AppConfigSnapshot;
 *       MCP config is managed by S3 yaml passthrough-merge (#420).
 *       The agent's MCPConfig only holds `command`/timeouts/env — no device
 *       fields. Serial + audio devices come from GET /api/config/devices
 *       (DevicesSnapshot) and belong to MCP yaml (S3). Omitted from M1 form.
 *  9. Safety = ALL READ-ONLY in M1 (decision 2, D78). No edit-gate dialog.
 *       SafetyLimitsSnapshot fields are rendered with a "Guarded" chip and
 *       a locked input; the PUT body never includes safety.
 * 10. "First-crack detection" category → audio device selection belongs to
 *       MCP yaml (S3). FC threshold params are MCP-config, not AppConfig.
 *       Category is DEFERRED to S3; no fields rendered in M1.
 *
 * Field key path conventions (mirrors ConfigFieldMeta nesting in the API):
 *   controller.<field>   → AppConfigSnapshot.controller.<field>
 *   advisor.<field>      → AppConfigSnapshot.advisor.<field>
 *   safety.<field>       → AppConfigSnapshot.safety.<field>
 *
 * env_var values here are informational documentation only — the server
 * returns the live env_overridden flag per ConfigFieldMeta.
 */

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type FieldType =
  | "text"              // arbitrary string
  | "number"            // numeric input (integer or float)
  | "boolean"           // toggle / checkbox
  | "select"            // closed-set enum — `options` must be provided
  | "masked"            // like text but value masked (api key)
  | "deviceSelect"      // single-select from GET /api/config/devices
  | "deviceMultiSelect" // multi-select from GET /api/config/devices (recording_devices)
  | "micTestButton";    // placeholder action button (M1: "not available")

export type FieldCategory =
  | "Advisor"
  | "Pre-FC Control"
  | "Late-Maillard Trim"
  | "Safety"
  | "Hardware"
  | "Audio"
  | "FC-Detection";

export interface FieldOption {
  value: string;
  label: string;
}

export interface ConfigFieldDef {
  /** Dot-path into AppConfigSnapshot, e.g. "controller.pre_fc_heat_target_percent". */
  key: string;

  /** Display label shown in the form row. */
  label: string;

  /** One-sentence hint shown under the input or in a tooltip. */
  hint: string;

  /** Input control type. */
  type: FieldType;

  /**
   * Allowed values for "select" fields. Omit for other types.
   * For device dropdowns (audio/serial) this is populated dynamically from
   * GET /api/config/devices — leave undefined here and handle in PR3.
   */
  options?: FieldOption[];

  /** Unit label appended to the value display, e.g. "°C", "s", "%". */
  unit?: string;

  /**
   * Whether this field is always read-only regardless of the server flag.
   * All safety fields are read-only in M1 (D78 decision 2).
   * Some controller fields are hardware-pinned (tick_interval_seconds).
   * The server's ConfigFieldMeta.read_only is the authoritative source;
   * this mirrors it for faster rendering without a per-field API lookup.
   */
  readOnlyStatic: boolean;

  /**
   * For number fields: minimum permitted value (inclusive).
   * Mirrors the server-side Pydantic Field constraints.
   */
  min?: number;

  /**
   * For number fields: maximum permitted value (inclusive).
   */
  max?: number;

  /**
   * For number fields: step between values in the control.
   */
  step?: number;

  /**
   * The name of the backing env-var. Shown in the env-overridden badge.
   * Corresponds to ConfigFieldMeta.env_var from the API response.
   */
  envVar: string | null;

  /**
   * Which device list from GET /api/config/devices to show in the dropdown.
   * Only set for `deviceSelect` and `deviceMultiSelect` fields.
   * - "serial"      → DevicesSnapshot.serial + serial_error
   * - "audio_input" → DevicesSnapshot.audio_input + audio_input_error
   */
  deviceSource?: "serial" | "audio_input";

  /**
   * Dot-path key used when building the PUT /api/config body (AppConfigEdit).
   *
   * For advisor fields this is just the field name (same as the snapshot key's
   * second segment), e.g. `"model_slug"`. For controller fields the snapshot
   * denormalises nested config into flat names (e.g.
   * `late_maillard_trim_enabled`), but the edit body uses the real nesting:
   *   `"pre_first_crack_levers.late_maillard_trim.enabled"`
   *
   * Set to `null` for always-read-only fields (safety, hardware-pinned) that
   * are never sent in the PUT body.
   */
  editKey: string | null;

  /** Category this field belongs to, used to group rows in the rail. */
  category: FieldCategory;

  /**
   * Conditional reveal: only render this field when another field in the form
   * matches a given value. Used for dependent fields (e.g.
   * auto_t0_drop_threshold_c only shown when auto_t0_detection_enabled = true).
   *
   * `key` is the FLAT form key of the controlling field (e.g.
   * "mcp_device.auto_t0_detection_enabled"). The field is hidden — not
   * rendered — when `values[key] !== equals`; its value stays in form state.
   */
  revealWhen?: { key: string; equals: unknown };
}

// ---------------------------------------------------------------------------
// Provider options
// ---------------------------------------------------------------------------

const PROVIDER_OPTIONS: FieldOption[] = [
  { value: "openai_compatible", label: "OpenRouter (OpenAI-compatible)" },
  { value: "openai",            label: "OpenAI (native)" },
  { value: "anthropic",         label: "Anthropic (native)" },
  { value: "google",            label: "Google (native)" },
  { value: "ollama",            label: "Ollama (local)" },
];

// All six c-series control-teaching prompts are real and selectable (c1–c6
// confirmed in advisor.py _CONTROL_TEACHING_PROMPTS). c3 is the live default.
// c1/c2 are the original cuts (retained for A/B); c4/c5/c6 are experiment
// selectors (#396). Legacy v0/v1 prompts are internal only — not exposed here.
const PROMPT_VERSION_OPTIONS: FieldOption[] = [
  { value: "c1", label: "c1 — original (v1 baseline)" },
  { value: "c2", label: "c2 — post-FC development stretch" },
  { value: "c3", label: "c3 — default (stable, production)" },
  { value: "c4", label: "c4 — experiment" },
  { value: "c5", label: "c5 — experiment" },
  { value: "c6", label: "c6 — experiment (#396 A/B)" },
];

// ---------------------------------------------------------------------------
// Field definitions by category
// ---------------------------------------------------------------------------

// --- Advisor -----------------------------------------------------------------

const ADVISOR_FIELDS: ConfigFieldDef[] = [
  {
    key:            "advisor.model_slug",
    label:          "Model",
    hint:           "Advisor model slug via the configured provider (e.g. openai/gpt-4o for OpenRouter).",
    type:           "text",
    envVar:         "ROASTPILOT_ADVISOR__MODEL_SLUG",
    editKey:        "model_slug",
    category:       "Advisor",
    readOnlyStatic: false,
  },
  {
    key:            "advisor.prompt_version",
    label:          "Prompt version",
    hint:           "Control-teaching prompt version. c3 is the live default; c4/c5/c6 are opt-in A/B selectors (#396).",
    type:           "select",
    options:        PROMPT_VERSION_OPTIONS,
    envVar:         "ROASTPILOT_ADVISOR__PROMPT_VERSION",
    editKey:        "prompt_version",
    category:       "Advisor",
    readOnlyStatic: false,
  },
  {
    key:            "advisor.provider",
    label:          "Provider",
    // Read-only in M1: switching provider without being able to set the
    // corresponding API key env-var (api_key_env is also read-only) would
    // leave the advisor incoherent. The design's Advisor category has only
    // Model + Control-prompt as editable fields. Use env override to change.
    hint:           "Which LLM provider backend to call. openai_compatible uses OpenRouter (default). Change via ROASTPILOT_ADVISOR__PROVIDER env var.",
    type:           "select",
    options:        PROVIDER_OPTIONS,
    envVar:         "ROASTPILOT_ADVISOR__PROVIDER",
    editKey:        null,             // read-only in M1: not sent in PUT body
    category:       "Advisor",
    readOnlyStatic: true,
  },
  {
    key:            "advisor.provider_base_url",
    label:          "Provider base URL",
    // Read-only in M1 alongside provider (same coherence constraint).
    hint:           "API endpoint for OpenAI-compatible providers. Default is OpenRouter. Change via ROASTPILOT_ADVISOR__PROVIDER_BASE_URL env var.",
    type:           "text",
    envVar:         "ROASTPILOT_ADVISOR__PROVIDER_BASE_URL",
    editKey:        null,             // read-only in M1: not sent in PUT body
    category:       "Advisor",
    readOnlyStatic: true,
  },
  {
    key:            "advisor.api_key_env",
    label:          "API key env-var",
    hint:           "The environment variable that holds the advisor API key. The key itself is never stored in config — set this env var on the host.",
    type:           "masked",
    envVar:         null,           // never env-injected; server returns saved=null, read_only=true
    editKey:        null,           // read-only: never sent in PUT body
    category:       "Advisor",
    readOnlyStatic: true,
  },
  {
    key:            "advisor.timeout_seconds",
    label:          "Timeout",
    hint:           "Per-call advisor timeout (seconds). The controller tick blocks no longer than this.",
    type:           "number",
    unit:           "s",
    min:            0.1,  // gt=0 (exclusive) in AdvisorConfigEdit
    step:           1,
    envVar:         "ROASTPILOT_ADVISOR__TIMEOUT_SECONDS",
    editKey:        "timeout_seconds",
    category:       "Advisor",
    readOnlyStatic: false,
  },
  {
    key:            "advisor.temperature",
    label:          "Temperature",
    hint:           "Sampling temperature for the advisor. 0.0 (default) is fully deterministic; higher values add stochasticity.",
    type:           "number",
    min:            0,
    max:            2,
    step:           0.1,
    envVar:         "ROASTPILOT_ADVISOR__TEMPERATURE",
    editKey:        "temperature",
    category:       "Advisor",
    readOnlyStatic: false,
  },
];

// --- Pre-FC Control ----------------------------------------------------------

const PRE_FC_FIELDS: ConfigFieldDef[] = [
  {
    key:            "controller.tick_interval_seconds",
    label:          "Tick interval",
    hint:           "Controller tick rate (seconds). Hardware-pinned to 1.0 s by the Hottop thermocouple response time — not editable.",
    type:           "number",
    unit:           "s",
    envVar:         "ROASTPILOT_CONTROLLER__TICK_INTERVAL_SECONDS",
    editKey:        null,           // hardware-pinned, read-only: never sent in PUT body
    category:       "Pre-FC Control",
    readOnlyStatic: true,
  },
  {
    key:            "controller.pre_fc_heat_target_percent",
    label:          "Pre-FC heat",
    hint:           "Heat level (%) held deterministically from charge to first crack. Default 100.",
    type:           "number",
    unit:           "%",
    min:            0,
    max:            100,
    step:           1,
    envVar:         "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__HEAT_TARGET_PERCENT",
    // Maps to ControllerConfigEdit.pre_first_crack_levers.heat_target_percent
    editKey:        "pre_first_crack_levers.heat_target_percent",
    category:       "Pre-FC Control",
    readOnlyStatic: false,
  },
  {
    key:            "controller.pre_fc_fan_target_percent",
    label:          "Pre-FC fan",
    hint:           "Fan level (%) held deterministically from charge to first crack. Default 30 (low airflow until browning).",
    type:           "number",
    unit:           "%",
    min:            0,
    max:            100,
    step:           1,
    envVar:         "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__FAN_TARGET_PERCENT",
    // Maps to ControllerConfigEdit.pre_first_crack_levers.fan_target_percent
    editKey:        "pre_first_crack_levers.fan_target_percent",
    category:       "Pre-FC Control",
    readOnlyStatic: false,
  },
];

// --- Late-Maillard Trim ------------------------------------------------------

const TRIM_FIELDS: ConfigFieldDef[] = [
  {
    key:            "controller.late_maillard_trim_enabled",
    label:          "Trim enabled",
    hint:           "Enable the anticipatory heat trim in the late-Maillard → FC window. When off, the flat 100% heat floor is used to FC.",
    type:           "boolean",
    envVar:         "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__ENABLED",
    editKey:        "pre_first_crack_levers.late_maillard_trim.enabled",
    category:       "Late-Maillard Trim",
    readOnlyStatic: false,
  },
  {
    key:            "controller.late_maillard_trim_heat_percent",
    label:          "Trim heat",
    hint:           "Trimmed heat level (%) held once the late-Maillard window opens. Default 65 — a moderate reduction, not a stall.",
    type:           "number",
    unit:           "%",
    min:            10,   // ge=10 in LateMaillardTrimEdit
    max:            100,
    step:           1,
    envVar:         "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__TRIM_HEAT_PERCENT",
    editKey:        "pre_first_crack_levers.late_maillard_trim.trim_heat_percent",
    category:       "Late-Maillard Trim",
    readOnlyStatic: false,
  },
  {
    key:            "controller.late_maillard_trim_window_fc_eta_seconds",
    label:          "Window (FC-ETA)",
    hint:           "Seconds before predicted first crack at which the trim window opens. Default 60 s.",
    type:           "number",
    unit:           "s",
    min:            0.1,  // gt=0 (exclusive) in LateMaillardTrimEdit
    step:           5,
    envVar:         "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__WINDOW_FC_ETA_SECONDS",
    editKey:        "pre_first_crack_levers.late_maillard_trim.window_fc_eta_seconds",
    category:       "Late-Maillard Trim",
    readOnlyStatic: false,
  },
  {
    key:            "controller.late_maillard_trim_min_bean_temp_c",
    label:          "Min bean temp",
    hint:           "Minimum bean temperature (°C) below which the trim never engages. Default 155 °C.",
    type:           "number",
    unit:           "°C",
    min:            0.1,  // gt=0 (exclusive) in LateMaillardTrimEdit
    step:           1,
    envVar:         "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__MIN_BEAN_TEMP_C",
    editKey:        "pre_first_crack_levers.late_maillard_trim.min_bean_temp_c",
    category:       "Late-Maillard Trim",
    readOnlyStatic: false,
  },
  {
    key:            "controller.late_maillard_trim_adaptive_depth_enabled",
    label:          "Adaptive depth",
    hint:           "Enable adaptive trim depth (#386). The trim deepens on hotter approaches (high RoR, short FC-ETA). Default off.",
    type:           "boolean",
    envVar:         "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__ADAPTIVE_DEPTH_ENABLED",
    editKey:        "pre_first_crack_levers.late_maillard_trim.adaptive_depth_enabled",
    category:       "Late-Maillard Trim",
    readOnlyStatic: false,
  },
  {
    key:            "controller.late_maillard_trim_base_trim",
    label:          "Base trim",
    hint:           "Adaptive-depth baseline trim (%). When RoR and ETA gain terms are both zero, this is the output. Default 65.",
    type:           "number",
    unit:           "%",
    min:            10,   // ge=10 in LateMaillardTrimEdit
    max:            100,
    step:           1,
    envVar:         "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__BASE_TRIM",
    editKey:        "pre_first_crack_levers.late_maillard_trim.base_trim",
    category:       "Late-Maillard Trim",
    readOnlyStatic: false,
  },
  {
    key:            "controller.late_maillard_trim_k_ror",
    label:          "k_ror",
    hint:           "RoR sensitivity: each °C/min above ror_ref deepens the cut by this many pp. Default 1.5.",
    type:           "number",
    min:            0,    // ge=0.0 in LateMaillardTrimEdit — negative gains are rejected
    step:           0.1,
    envVar:         "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__K_ROR",
    editKey:        "pre_first_crack_levers.late_maillard_trim.k_ror",
    category:       "Late-Maillard Trim",
    readOnlyStatic: false,
  },
  {
    key:            "controller.late_maillard_trim_k_eta",
    label:          "k_eta",
    hint:           "ETA sensitivity: each 1 s under eta_ref deepens the cut by this many pp. Default 0.2.",
    type:           "number",
    min:            0,    // ge=0.0 in LateMaillardTrimEdit — negative gains are rejected
    step:           0.01,
    envVar:         "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__K_ETA",
    editKey:        "pre_first_crack_levers.late_maillard_trim.k_eta",
    category:       "Late-Maillard Trim",
    readOnlyStatic: false,
  },
  {
    key:            "controller.late_maillard_trim_ror_ref",
    label:          "RoR reference",
    hint:           "RoR (°C/min) below which the RoR gain term contributes 0. Default 8.0.",
    type:           "number",
    unit:           "°C/min",
    min:            0,
    step:           0.5,
    envVar:         "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__ROR_REF",
    editKey:        "pre_first_crack_levers.late_maillard_trim.ror_ref",
    category:       "Late-Maillard Trim",
    readOnlyStatic: false,
  },
  {
    key:            "controller.late_maillard_trim_eta_ref",
    label:          "ETA reference",
    hint:           "ETA (seconds) at which the ETA gain term is 0. Deepening only occurs below this. Default 60.",
    type:           "number",
    unit:           "s",
    min:            0.1,  // gt=0 (exclusive) in LateMaillardTrimEdit
    step:           5,
    envVar:         "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__ETA_REF",
    editKey:        "pre_first_crack_levers.late_maillard_trim.eta_ref",
    category:       "Late-Maillard Trim",
    readOnlyStatic: false,
  },
  {
    key:            "controller.late_maillard_trim_min_trim",
    label:          "Min trim",
    hint:           "Deepest permitted adaptive trim (%). The formula cannot go below this — prevents stalling first crack. Default 45.",
    type:           "number",
    unit:           "%",
    min:            10,   // ge=10 in LateMaillardTrimEdit
    max:            100,
    step:           1,
    envVar:         "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__MIN_TRIM",
    editKey:        "pre_first_crack_levers.late_maillard_trim.min_trim",
    category:       "Late-Maillard Trim",
    readOnlyStatic: false,
  },
  {
    key:            "controller.late_maillard_trim_max_trim",
    label:          "Max trim",
    hint:           "Shallowest permitted adaptive trim (%). Adaptive depth is always a reduction from 100 %. Default 75.",
    type:           "number",
    unit:           "%",
    min:            10,   // ge=10 in LateMaillardTrimEdit
    max:            100,
    step:           1,
    envVar:         "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__MAX_TRIM",
    editKey:        "pre_first_crack_levers.late_maillard_trim.max_trim",
    category:       "Late-Maillard Trim",
    readOnlyStatic: false,
  },
  {
    key:            "controller.late_maillard_trim_trim_depth_deadband_pp",
    label:          "Depth deadband",
    hint:           "Tick-to-tick deadband (pp). Changes smaller than this are suppressed to avoid noise-driven micro-adjustments. Must be < slew limit. Default 2.",
    type:           "number",
    unit:           "pp",
    min:            0,    // ge=0 in LateMaillardTrimEdit
    max:            20,   // le=20 in LateMaillardTrimEdit
    step:           1,
    envVar:         "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__TRIM_DEPTH_DEADBAND_PP",
    editKey:        "pre_first_crack_levers.late_maillard_trim.trim_depth_deadband_pp",
    category:       "Late-Maillard Trim",
    readOnlyStatic: false,
    revealWhen:     { key: "controller.late_maillard_trim_adaptive_depth_enabled", equals: true },
  },
  {
    key:            "controller.late_maillard_trim_trim_depth_slew_pp_per_tick",
    label:          "Depth slew limit",
    hint:           "Maximum trim-depth change per accepted write (pp). Limits how fast the depth can move between consecutive MCP writes, damping thrash from jittery RoR. Must be > deadband. Default 3.",
    type:           "number",
    unit:           "pp",
    min:            1,    // ge=1 in LateMaillardTrimEdit
    max:            20,   // le=20 in LateMaillardTrimEdit
    step:           1,
    envVar:         "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__TRIM_DEPTH_SLEW_PP_PER_TICK",
    editKey:        "pre_first_crack_levers.late_maillard_trim.trim_depth_slew_pp_per_tick",
    category:       "Late-Maillard Trim",
    readOnlyStatic: false,
    revealWhen:     { key: "controller.late_maillard_trim_adaptive_depth_enabled", equals: true },
  },
];

// --- Safety (all read-only in M1, D78 decision 2) ----------------------------

const SAFETY_FIELDS: ConfigFieldDef[] = [
  {
    key:            "safety.max_bean_temp_c",
    label:          "Bean temp ceiling",
    hint:           "Hard bean-temperature ceiling (°C). The safety box faults above this. Default 230 °C.",
    type:           "number",
    unit:           "°C",
    envVar:         "ROASTPILOT_SAFETY__MAX_BEAN_TEMP_C",
    editKey:        null,   // safety: all read-only in M1 (D78 decision 2)
    category:       "Safety",
    readOnlyStatic: true,
  },
  {
    key:            "safety.max_env_temp_c",
    label:          "Env temp ceiling",
    hint:           "Hard environment-temperature ceiling (°C). Readings above this indicate a fault. Default 240 °C.",
    type:           "number",
    unit:           "°C",
    envVar:         "ROASTPILOT_SAFETY__MAX_ENV_TEMP_C",
    editKey:        null,
    category:       "Safety",
    readOnlyStatic: true,
  },
  {
    key:            "safety.pre_t0_max_bean_temp_c",
    label:          "Pre-charge bean ceiling",
    hint:           "Max bean temp (°C) permitted before T0 (charge) is confirmed. Exceeding this triggers the pre-T0 overrun policy. Default 200 °C.",
    type:           "number",
    unit:           "°C",
    envVar:         "ROASTPILOT_SAFETY__PRE_T0_MAX_BEAN_TEMP_C",
    editKey:        null,
    category:       "Safety",
    readOnlyStatic: true,
  },
  {
    key:            "safety.overrun_safe_fan_percent",
    label:          "Overrun fan",
    hint:           "Fan level (%) applied during a pre-T0 overrun. Heat is cut to 0 %; fan provides ventilation. Default 100.",
    type:           "number",
    unit:           "%",
    envVar:         "ROASTPILOT_SAFETY__OVERRUN_SAFE_FAN_PERCENT",
    editKey:        null,
    category:       "Safety",
    readOnlyStatic: true,
  },
  {
    // Literal["recovery", "fault"] in SafetyLimits — rendered as a disabled
    // select (read-only in M1) to communicate the closed value set clearly.
    key:            "safety.pre_t0_overrun_severity",
    label:          "Overrun severity",
    hint:           "'recovery' requires operator acknowledgement; 'fault' halts immediately. Default 'recovery'.",
    type:           "select",
    options: [
      { value: "recovery", label: "recovery — operator acknowledgement required" },
      { value: "fault",    label: "fault — immediate halt" },
    ],
    envVar:         "ROASTPILOT_SAFETY__PRE_T0_OVERRUN_SEVERITY",
    editKey:        null,
    category:       "Safety",
    readOnlyStatic: true,
  },
  {
    key:            "safety.min_seconds_between_commands",
    label:          "Min command interval",
    hint:           "Minimum seconds between roaster commands. Writes more frequent than this have no effect at ~1 Hz serial. Default 2.0 s.",
    type:           "number",
    unit:           "s",
    envVar:         "ROASTPILOT_SAFETY__MIN_SECONDS_BETWEEN_COMMANDS",
    editKey:        null,
    category:       "Safety",
    readOnlyStatic: true,
  },
  {
    key:            "safety.max_consecutive_mcp_failures",
    label:          "MCP failure limit",
    hint:           "Consecutive MCP read failures tolerated before a fault. Default 3 (~3 s at 1 Hz).",
    type:           "number",
    envVar:         "ROASTPILOT_SAFETY__MAX_CONSECUTIVE_MCP_FAILURES",
    editKey:        null,
    category:       "Safety",
    readOnlyStatic: true,
  },
  {
    key:            "safety.max_consecutive_advisor_failures",
    label:          "Advisor failure limit",
    hint:           "Consecutive advisor availability failures before failing closed. Default 3.",
    type:           "number",
    envVar:         "ROASTPILOT_SAFETY__MAX_CONSECUTIVE_ADVISOR_FAILURES",
    editKey:        null,
    category:       "Safety",
    readOnlyStatic: true,
  },
  {
    key:            "safety.bitter_ceiling_temp_c",
    label:          "Bitter ceiling",
    hint:           "Drop/bitter ceiling (°C). Bean temperature past which a medium roast turns bitter. Advisor and control are told this. Default 196 °C.",
    type:           "number",
    unit:           "°C",
    envVar:         "ROASTPILOT_SAFETY__BITTER_CEILING_TEMP_C",
    editKey:        null,
    category:       "Safety",
    readOnlyStatic: true,
  },
  {
    key:            "safety.emergency_drop_temp_c",
    label:          "Emergency drop",
    hint:           "Emergency-drop temperature (°C). Above this the roast must be dropped regardless of development. 2 °C above bitter ceiling by design. Default 198 °C.",
    type:           "number",
    unit:           "°C",
    envVar:         "ROASTPILOT_SAFETY__EMERGENCY_DROP_TEMP_C",
    editKey:        null,
    category:       "Safety",
    readOnlyStatic: true,
  },
];

// --- Hardware ----------------------------------------------------------------
// Managed MCP device fields rendered into coffee-roaster-mcp.yaml (#429).
// `roaster_driver` is a free-text field (driver names are MCP constants;
// not enumerated at runtime). `serial_port` uses the serial device list.

const HARDWARE_FIELDS: ConfigFieldDef[] = [
  {
    key:            "mcp_device.serial_port",
    label:          "Serial port",
    hint:           "Serial port path for the Hottop roaster (e.g. /dev/cu.usbserial-XXXXX on macOS, /dev/ttyUSB0 on Linux). Maps to roaster.port in the MCP yaml.",
    type:           "deviceSelect",
    deviceSource:   "serial",
    envVar:         "ROASTPILOT_MCP_DEVICE__SERIAL_PORT",
    editKey:        "serial_port",
    category:       "Hardware",
    readOnlyStatic: false,
  },
  {
    key:            "mcp_device.roaster_driver",
    label:          "Roaster driver",
    hint:           "coffee-roaster-mcp driver name. Use 'hottop_kn8828b_2k_plus' for the real roaster or 'mock' for development. Maps to roaster.driver in the MCP yaml.",
    type:           "text",
    envVar:         "ROASTPILOT_MCP_DEVICE__ROASTER_DRIVER",
    editKey:        "roaster_driver",
    category:       "Hardware",
    readOnlyStatic: false,
  },
];

// --- Ambient / environment (#342, #474) ---------------------------------------
// Ambient environmental sensor (Yoctopuce Yocto-Meteo) config, rendered into
// the MCP yaml's `ambient.*` section. Same tri-state inherit/override
// semantics as the other mcp_device fields (#439) — null = inherit from the
// hand-authored yaml. `ambient_mode` is the primary toggle; device + poll
// interval are advanced/optional overrides.

const AMBIENT_MODE_OPTIONS: FieldOption[] = [
  { value: "disabled",  label: "Disabled" },
  { value: "yoctopuce", label: "Yoctopuce (Yocto-Meteo USB probe)" },
];

const AMBIENT_FIELDS: ConfigFieldDef[] = [
  {
    key:            "mcp_device.ambient_mode",
    label:          "Ambient sensor mode",
    hint:           "Whether an ambient temp/humidity/pressure probe is read each roast. 'yoctopuce' reads a Yocto-Meteo USB probe; 'disabled' turns ambient capture off. Maps to ambient.mode in the MCP yaml.",
    type:           "select",
    options:        AMBIENT_MODE_OPTIONS,
    envVar:         "ROASTPILOT_MCP_DEVICE__AMBIENT_MODE",
    editKey:        "ambient_mode",
    category:       "Hardware",
    readOnlyStatic: false,
  },
  {
    key:            "mcp_device.ambient_device",
    label:          "Ambient probe device",
    hint:           "Leave blank to auto-detect the first Yoctopuce Yocto-Meteo device. Set a serial number or logical name to pin a specific probe. Maps to ambient.device in the MCP yaml.",
    type:           "text",
    envVar:         "ROASTPILOT_MCP_DEVICE__AMBIENT_DEVICE",
    editKey:        "ambient_device",
    category:       "Hardware",
    readOnlyStatic: false,
  },
  {
    key:            "mcp_device.ambient_poll_interval_seconds",
    label:          "Ambient poll interval",
    hint:           "Minimum seconds between ambient USB reads. Leave blank to inherit the MCP default (30 s). Maps to ambient.poll_interval_seconds in the MCP yaml.",
    type:           "number",
    unit:           "s",
    min:            0.1,  // gt=0 (exclusive) in MCPDeviceConfigEdit
    step:           1,
    envVar:         "ROASTPILOT_MCP_DEVICE__AMBIENT_POLL_INTERVAL_SECONDS",
    editKey:        "ambient_poll_interval_seconds",
    category:       "Hardware",
    readOnlyStatic: false,
  },
];

// --- Audio -------------------------------------------------------------------
// Audio device configuration + mic test placeholder.
// `audio_input_device` is a single-select; `recording_devices` is a
// multi-select (ordered list of device substrings for the recorder).

const AUDIO_FIELDS: ConfigFieldDef[] = [
  {
    key:            "mcp_device.audio_input_device",
    label:          "Audio input device",
    hint:           "PortAudio input device name substring matched case-insensitively (e.g. 'USB PnP'). Used for FC detection. Maps to audio.input_device in the MCP yaml.",
    type:           "deviceSelect",
    deviceSource:   "audio_input",
    envVar:         "ROASTPILOT_MCP_DEVICE__AUDIO_INPUT_DEVICE",
    editKey:        "audio_input_device",
    category:       "Audio",
    readOnlyStatic: false,
  },
  {
    key:            "mcp_device.recording_enabled",
    label:          "Recording enabled",
    hint:           "Whether the MCP audio recorder is active. Maps to recording.enabled in the MCP yaml.",
    type:           "boolean",
    envVar:         "ROASTPILOT_MCP_DEVICE__RECORDING_ENABLED",
    editKey:        "recording_enabled",
    category:       "Audio",
    readOnlyStatic: false,
  },
  {
    key:            "mcp_device.recording_autocapture",
    label:          "Auto-capture",
    hint:           "Start recording automatically at the beginning of each roast session. Maps to recording.autocapture in the MCP yaml.",
    type:           "boolean",
    envVar:         "ROASTPILOT_MCP_DEVICE__RECORDING_AUTOCAPTURE",
    editKey:        "recording_autocapture",
    category:       "Audio",
    readOnlyStatic: false,
  },
  {
    key:            "mcp_device.recording_devices",
    label:          "Recording devices",
    hint:           "Ordered list of capture device-name substrings. First entry is the FC detector's device; additional entries are independent capture streams. Maps to recording.devices in the MCP yaml.",
    type:           "deviceMultiSelect",
    deviceSource:   "audio_input",
    envVar:         "ROASTPILOT_MCP_DEVICE__RECORDING_DEVICES",
    editKey:        "recording_devices",
    category:       "Audio",
    readOnlyStatic: false,
  },
  {
    key:            "mcp_device._mic_test",
    label:          "Test microphone",
    hint:           "Sample briefly from the configured audio input device and display the captured level. Not available in M1 — the backend sample endpoint is deferred.",
    type:           "micTestButton",
    envVar:         null,
    editKey:        null,   // not a saveable value; renders a button only
    category:       "Audio",
    readOnlyStatic: true,
  },
];

// --- FC-Detection ------------------------------------------------------------
// First-crack detection mode, confidence threshold, and auto-T0 settings.

const FC_DETECTION_OPTIONS = [
  { value: "disabled", label: "Disabled" },
  { value: "audio",    label: "Audio (PortAudio, live)" },
  { value: "manual",   label: "Manual (operator-triggered only)" },
];

const FC_DETECTION_FIELDS: ConfigFieldDef[] = [
  {
    key:            "mcp_device.fc_mode",
    label:          "FC detection mode",
    hint:           "How first crack is detected. 'audio' uses the configured PortAudio device; 'manual' requires the operator to mark FC explicitly. Maps to first_crack.mode in the MCP yaml.",
    type:           "select",
    options:        FC_DETECTION_OPTIONS,
    envVar:         "ROASTPILOT_MCP_DEVICE__FC_MODE",
    editKey:        "fc_mode",
    category:       "FC-Detection",
    readOnlyStatic: false,
  },
  {
    key:            "mcp_device.fc_confidence_threshold",
    label:          "Confidence threshold",
    hint:           "Detector confidence threshold [0, 1]. Events below this score are suppressed. Default 0.5. Maps to first_crack.confidence_threshold in the MCP yaml.",
    type:           "number",
    min:            0,
    max:            1,
    step:           0.05,
    envVar:         "ROASTPILOT_MCP_DEVICE__FC_CONFIDENCE_THRESHOLD",
    editKey:        "fc_confidence_threshold",
    category:       "FC-Detection",
    readOnlyStatic: false,
  },
  {
    key:            "mcp_device.auto_t0_detection_enabled",
    label:          "Auto-T0 detection",
    hint:           "Whether the MCP auto-detects the charge-drop (T0) from the bean-temperature drop. Maps to session.auto_t0_detection_enabled in the MCP yaml.",
    type:           "boolean",
    envVar:         "ROASTPILOT_MCP_DEVICE__AUTO_T0_DETECTION_ENABLED",
    editKey:        "auto_t0_detection_enabled",
    category:       "FC-Detection",
    readOnlyStatic: false,
  },
  {
    key:            "mcp_device.auto_t0_drop_threshold_c",
    label:          "T0 drop threshold",
    hint:           "Bean-temperature drop (°C) that triggers automatic T0 detection. Maps to session.auto_t0_drop_threshold_c in the MCP yaml.",
    type:           "number",
    unit:           "°C",
    min:            0.1,
    step:           0.5,
    envVar:         "ROASTPILOT_MCP_DEVICE__AUTO_T0_DROP_THRESHOLD_C",
    editKey:        "auto_t0_drop_threshold_c",
    category:       "FC-Detection",
    readOnlyStatic: false,
    // Only shown when auto-T0 detection is enabled — no point configuring the
    // threshold when the detector is off.
    revealWhen:     { key: "mcp_device.auto_t0_detection_enabled", equals: true },
  },
];

// ---------------------------------------------------------------------------
// Category ordering
// ---------------------------------------------------------------------------

/**
 * A named group of fields within a category. Groups add visual structure:
 * an `h3` subheading + hairline divider above the first field in the group.
 * Groups are optional — a category without groups renders its fields flat.
 */
export interface ConfigGroup {
  /** Displayed as an `h3` uppercase label above the group's fields. */
  title: string;
  fields: ConfigFieldDef[];
}

export interface ConfigCategory {
  id: FieldCategory;
  label: string;
  /**
   * Short description shown in the category rail.
   */
  description: string;
  /**
   * When groups is provided, fields is derived from them (concatenation).
   * When absent, fields is defined directly and no group subheadings render.
   */
  groups?: ConfigGroup[];
  fields: ConfigFieldDef[];
}

/**
 * Ordered list of config categories for the /config view rail.
 *
 * Ordering follows the design handoff (Hardware → Audio → FC-Detection →
 * Advisor → Pre-FC Control → Late-Maillard Trim → Safety), which puts the
 * device/hardware configuration first — the operator typically connects hardware
 * before tuning the advisor or roast control parameters.
 *
 * Hardware / Audio / FC-Detection are backed by the mcp_device snapshot
 * section added in S3 (#429) and wired by PR3 of #419 (slice 3c).
 */
export const CONFIG_CATEGORIES: ConfigCategory[] = [
  {
    id:          "Hardware",
    label:       "Hardware",
    description: "Serial port, roaster driver, and ambient sensor rendered into the MCP yaml on each spawn.",
    groups: [
      {
        title:  "Roaster",
        fields: HARDWARE_FIELDS,
      },
      {
        title:  "Ambient / environment",
        fields: AMBIENT_FIELDS,
      },
    ],
    fields:      [...HARDWARE_FIELDS, ...AMBIENT_FIELDS],
  },
  {
    id:          "Audio",
    label:       "Audio",
    description: "Audio input device and recording settings for the MCP child process.",
    groups: [
      {
        title:  "First-crack input",
        fields: AUDIO_FIELDS.filter((f) =>
          f.key === "mcp_device.audio_input_device",
        ),
      },
      {
        title:  "Recording",
        fields: AUDIO_FIELDS.filter((f) =>
          f.key !== "mcp_device.audio_input_device",
        ),
      },
    ],
    fields:      AUDIO_FIELDS,
  },
  {
    id:          "FC-Detection",
    label:       "FC detection",
    description: "First-crack detection mode, confidence threshold, and auto-T0 settings.",
    groups: [
      {
        title:  "Detection",
        fields: FC_DETECTION_FIELDS.filter((f) =>
          f.key === "mcp_device.fc_mode" ||
          f.key === "mcp_device.fc_confidence_threshold",
        ),
      },
      {
        title:  "Auto-T0 (charge detection)",
        fields: FC_DETECTION_FIELDS.filter((f) =>
          f.key === "mcp_device.auto_t0_detection_enabled" ||
          f.key === "mcp_device.auto_t0_drop_threshold_c",
        ),
      },
    ],
    fields:      FC_DETECTION_FIELDS,
  },
  {
    id:          "Advisor",
    label:       "Advisor",
    description: "LLM advisor — advises the control loop; never issues roaster commands directly.",
    groups: [
      {
        title:  "Model",
        fields: ADVISOR_FIELDS.filter((f) =>
          f.key === "advisor.model_slug" || f.key === "advisor.prompt_version",
        ),
      },
      {
        title:  "Parameters",
        fields: ADVISOR_FIELDS.filter((f) =>
          f.key === "advisor.timeout_seconds" || f.key === "advisor.temperature",
        ),
      },
      {
        title:  "Credentials",
        fields: ADVISOR_FIELDS.filter((f) => f.key === "advisor.api_key_env"),
      },
      {
        title:  "Provider (read-only)",
        fields: ADVISOR_FIELDS.filter((f) =>
          f.key === "advisor.provider" || f.key === "advisor.provider_base_url",
        ),
      },
    ],
    fields:      ADVISOR_FIELDS,
  },
  {
    id:          "Pre-FC Control",
    label:       "Pre-FC control",
    description: "Heat and fan levels held from charge to first crack.",
    groups: [
      {
        title:  "Levers",
        fields: PRE_FC_FIELDS,
      },
    ],
    fields:      PRE_FC_FIELDS,
  },
  {
    id:          "Late-Maillard Trim",
    label:       "Late-Maillard trim",
    description: "Anticipatory heat trim in the late-Maillard window ahead of first crack.",
    groups: [
      {
        title:  "Trim",
        fields: TRIM_FIELDS.filter((f) =>
          f.key === "controller.late_maillard_trim_enabled" ||
          f.key === "controller.late_maillard_trim_heat_percent" ||
          f.key === "controller.late_maillard_trim_window_fc_eta_seconds" ||
          f.key === "controller.late_maillard_trim_min_bean_temp_c",
        ),
      },
      {
        title:  "Adaptive depth",
        fields: TRIM_FIELDS.filter((f) =>
          f.key !== "controller.late_maillard_trim_enabled" &&
          f.key !== "controller.late_maillard_trim_heat_percent" &&
          f.key !== "controller.late_maillard_trim_window_fc_eta_seconds" &&
          f.key !== "controller.late_maillard_trim_min_bean_temp_c",
        ),
      },
    ],
    fields:      TRIM_FIELDS,
  },
  {
    id:          "Safety",
    label:       "Safety",
    description: "Safety limits — displayed for reference. All read-only in M1.",
    groups: [
      {
        title:  "Temperatures",
        fields: SAFETY_FIELDS.filter((f) =>
          f.key === "safety.max_bean_temp_c" ||
          f.key === "safety.max_env_temp_c" ||
          f.key === "safety.pre_t0_max_bean_temp_c" ||
          f.key === "safety.bitter_ceiling_temp_c" ||
          f.key === "safety.emergency_drop_temp_c",
        ),
      },
      {
        title:  "Rate limits",
        fields: SAFETY_FIELDS.filter((f) =>
          f.key === "safety.min_seconds_between_commands" ||
          f.key === "safety.max_consecutive_mcp_failures" ||
          f.key === "safety.max_consecutive_advisor_failures",
        ),
      },
      {
        title:  "Recovery",
        fields: SAFETY_FIELDS.filter((f) =>
          f.key === "safety.overrun_safe_fan_percent" ||
          f.key === "safety.pre_t0_overrun_severity",
        ),
      },
    ],
    fields:      SAFETY_FIELDS,
  },
];

/**
 * Flat lookup from field key to definition.
 * Used by the field renderer to resolve display metadata without iterating
 * all categories.
 */
export const CONFIG_FIELD_MAP: Readonly<Record<string, ConfigFieldDef>> =
  Object.fromEntries(
    CONFIG_CATEGORIES.flatMap((cat) => cat.fields.map((f) => [f.key, f])),
  );
