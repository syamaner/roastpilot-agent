/**
 * Closed, bounded adapter for the read-only cold-characterisation event stream.
 *
 * The cold stream carries exactly two event names: `observation` (an opaque ID plus
 * the closed eleven-field public projection, schema version 1) and `heartbeat`
 * (transport evidence only, data exactly `{}`). Everything here is pure and total:
 * an admission function never throws, and anything outside the closed shape is
 * refused, never repaired, defaulted or partially shown.
 *
 * An admitted observation is only the "last retained display observation". It is
 * produced before any engine classification, so it is never an accepted, safe or
 * qualified tick. Heat, roast fan and cooling are commanded state reported by MCP,
 * not measurements. Temperatures are Celsius and may be `null`; no plausibility
 * range is applied. The main fan is never published (`fan_percent` is always
 * `null`).
 *
 * The event ID is opaque: it is matched against the server grammar and kept as the
 * exact string, never ordered or converted to a number.
 */

/** The single cold service endpoint (GET event stream). */
export const COLD_EVENTS_PATH = "/api/cold-characterisation/events";

/** Upper bound on one frame's data length, in UTF-16 code units. */
export const MAX_FRAME_DATA_CHARS = 4096;

/** Upper bound on one opaque event ID, in characters. */
export const MAX_EVENT_ID_CHARS = 33;

/** The server's opaque event-ID grammar: a 16-hex epoch, a dash, a positive sequence. */
export const COLD_EVENT_ID = /^[0-9a-f]{16}-[1-9][0-9]{0,15}$/;

/** Upper bound on the UTC instant's UTF-8 size (the server admission bound). */
export const MAX_UTC_UTF8_BYTES = 2048;

/**
 * UI rendering restriction (not a server invariant): C0/C1 control characters and
 * bidirectional controls are refused so the verbatim UTC cannot reorder or hide
 * surrounding text.
 */
// eslint-disable-next-line no-control-regex
const UTC_DISPLAY_REFUSED = /[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/;

/** The public cold phase vocabulary. */
export type ColdPhase = "recording_off" | "recording_on";

/** The public commanded roast-fan read outcome. */
export type ColdRoastFanOutcome =
  | "observed"
  | "not_eligible"
  | "unsupported"
  | "unreadable"
  | "malformed";

/** One admitted cold observation (the closed eleven-field projection). */
export interface ColdObservation {
  readonly schema_version: 1;
  readonly cold_phase: ColdPhase;
  readonly recorded_at_utc: string;
  readonly device_reported: boolean;
  readonly bean_temp_c: number | null;
  readonly env_temp_c: number | null;
  readonly heat_percent: number | null;
  readonly fan_percent: null;
  readonly roast_fan_outcome: ColdRoastFanOutcome;
  readonly roast_fan_percent: number | null;
  readonly cooling_on: boolean | null;
}

/** Operator-facing phase labels (a closed table; the phase is never inferred). */
export const COLD_PHASE_LABEL: Record<ColdPhase, string> = {
  recording_off: "Recording off",
  recording_on: "Recording on",
};

/** Operator-facing labels for the commanded roast-fan read outcome. */
export const ROAST_FAN_OUTCOME_LABEL: Record<ColdRoastFanOutcome, string> = {
  observed: "Observed",
  not_eligible: "Not eligible",
  unsupported: "Unsupported",
  unreadable: "Unreadable",
  malformed: "Malformed",
};

const OBSERVATION_KEYS: ReadonlySet<string> = new Set([
  "schema_version",
  "cold_phase",
  "recorded_at_utc",
  "device_reported",
  "bean_temp_c",
  "env_temp_c",
  "heat_percent",
  "fan_percent",
  "roast_fan_outcome",
  "roast_fan_percent",
  "cooling_on",
]);

const utf8 = new TextEncoder();

function hasOwn(table: object, key: unknown): boolean {
  return typeof key === "string" && Object.prototype.hasOwnProperty.call(table, key);
}

function isFiniteOrNull(value: unknown): value is number | null {
  return value === null || (typeof value === "number" && Number.isFinite(value));
}

function isSafeIntegerOrNull(value: unknown): value is number | null {
  return value === null || Number.isSafeInteger(value);
}

function isBooleanOrNull(value: unknown): value is boolean | null {
  return value === null || typeof value === "boolean";
}

function isDisplayableUtc(value: unknown): value is string {
  if (typeof value !== "string") return false;
  const bytes = utf8.encode(value).length;
  if (bytes < 1 || bytes > MAX_UTC_UTF8_BYTES) return false;
  return !UTC_DISPLAY_REFUSED.test(value);
}

/** Whether a value is a well-formed opaque cold event ID (exact string only). */
export function isColdEventId(id: unknown): id is string {
  return typeof id === "string" && id.length <= MAX_EVENT_ID_CHARS && COLD_EVENT_ID.test(id);
}

/**
 * The cold stream URL, carrying the exact last ID on an explicit reconnect.
 *
 * @param lastId The last admitted opaque ID, or `null` for a fresh stream.
 * @returns The bare path, or the path with an encoded `last_event_id` query.
 */
export function coldEventsUrl(lastId: string | null): string {
  if (lastId === null) return COLD_EVENTS_PATH;
  return `${COLD_EVENTS_PATH}?last_event_id=${encodeURIComponent(lastId)}`;
}

/**
 * Admit one `observation` event, or refuse it.
 *
 * Total: never throws. Order: the ID grammar, then the data length bound, then the
 * JSON parse, then the exact plain-object key set, then each field's closed type.
 *
 * @param data The event's raw data (untrusted).
 * @param id The event's opaque ID (untrusted).
 * @returns A fresh closed observation, or `null` when refused.
 */
export function admitObservation(data: unknown, id: unknown): ColdObservation | null {
  if (!isColdEventId(id)) return null;
  if (typeof data !== "string" || data.length > MAX_FRAME_DATA_CHARS) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(data);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;
  if (Object.getPrototypeOf(parsed) !== Object.prototype) return null;
  const keys = Object.keys(parsed);
  if (keys.length !== OBSERVATION_KEYS.size || !keys.every((key) => OBSERVATION_KEYS.has(key))) {
    return null;
  }
  const record = parsed as Record<string, unknown>;
  const {
    schema_version: schemaVersion,
    cold_phase: phase,
    recorded_at_utc: utc,
    device_reported: deviceReported,
    bean_temp_c: bean,
    env_temp_c: env,
    heat_percent: heat,
    fan_percent: fan,
    roast_fan_outcome: outcome,
    roast_fan_percent: roastFan,
    cooling_on: cooling,
  } = record;

  if (schemaVersion !== 1) return null;
  if (!hasOwn(COLD_PHASE_LABEL, phase)) return null;
  if (!isDisplayableUtc(utc)) return null;
  if (typeof deviceReported !== "boolean") return null;
  if (!isFiniteOrNull(bean) || !isFiniteOrNull(env)) return null;
  if (!isSafeIntegerOrNull(heat)) return null;
  if (fan !== null) return null;
  if (!hasOwn(ROAST_FAN_OUTCOME_LABEL, outcome)) return null;
  if (roastFan !== null) {
    if (outcome !== "observed") return null;
    if (!Number.isInteger(roastFan) || (roastFan as number) < 0 || (roastFan as number) > 100) {
      return null;
    }
  }
  if (!isBooleanOrNull(cooling)) return null;
  if (!deviceReported && (bean !== null || env !== null || heat !== null || cooling !== null)) {
    return null;
  }

  return {
    schema_version: 1,
    cold_phase: phase as ColdPhase,
    recorded_at_utc: utc,
    device_reported: deviceReported,
    bean_temp_c: bean,
    env_temp_c: env,
    heat_percent: heat,
    fan_percent: null,
    roast_fan_outcome: outcome as ColdRoastFanOutcome,
    roast_fan_percent: roastFan as number | null,
    cooling_on: cooling,
  };
}

/**
 * Admit one `heartbeat` event: data exactly `{}`.
 *
 * The heartbeat's ID is ignored (the browser carries the previous frame's ID
 * forward), and a heartbeat never changes an observation or its time.
 *
 * @param data The event's raw data (untrusted).
 * @returns Whether the heartbeat is well formed.
 */
export function admitHeartbeat(data: unknown): boolean {
  return data === "{}";
}
