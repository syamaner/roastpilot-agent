"""Bounded, transport-independent admission of synthetic collector bundles.

Only strict JSONL bytes enter this module.  It validates their digest and
syntax, then isolates payload and sequence faults to the affected stream.  It
does not read files, contact providers, import a hardware-control surface, or
interpret evidence.
"""

import hashlib
import json
import math
import re
import typing

import pydantic

from roastpilot_agent.diagnostic_collector.bundle_schema import (
    AdmittedBundle,
    BundleHeader,
    ClockName,
    CollectorAdmissionError,
    CollectorAdmissionReason,
    CollectorAuthority,
    CollectorEnvelope,
    CollectorPayload,
    CollectorStream,
    CollectorStreamEntry,
    CollectorStreamReason,
    CollectorStreamState,
    CommandPayload,
    EndpointPayload,
    HumanFirstCrackPayload,
    IdentityPayload,
    InferenceConfigPayload,
    Instant,
    OperatorObservationPayload,
    PersistenceAckPayload,
    RecordingSegmentPayload,
    RoomReadingPayload,
    ShadowEventPayload,
    ShadowWindowPayload,
    TemperatureTurnPayload,
)

MAX_BUNDLE_BYTES = 4 * 1024 * 1024
MAX_LINE_BYTES = 4_096
MAX_RECORDS = 16_384
MAX_STREAM_RECORDS = 8_192
MAX_STRING_BYTES = 128
MAX_NESTING = 6
MAX_SEQUENCE = (2**53) - 1
_TOKEN = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
_DIGEST = re.compile(r"\A[0-9a-f]{64}\Z")
_STREAM_ORDER = tuple(CollectorStream)
_STREAM_NAMES = frozenset(stream.value for stream in CollectorStream)
_CLOCK_NAMES = frozenset(clock.value for clock in ClockName)
JsonObject: typing.TypeAlias = dict[str, object]

_PAYLOAD_MODELS: dict[CollectorStream, type[CollectorPayload]] = {
    CollectorStream.IDENTITY: IdentityPayload,
    CollectorStream.TEMPERATURE_TURN: TemperatureTurnPayload,
    CollectorStream.ROOM_READING: RoomReadingPayload,
    CollectorStream.HUMAN_FC: HumanFirstCrackPayload,
    CollectorStream.SHADOW_EVENT: ShadowEventPayload,
    CollectorStream.SHADOW_WINDOW: ShadowWindowPayload,
    CollectorStream.INFERENCE_CONFIG: InferenceConfigPayload,
    CollectorStream.RECORDING_SEGMENT: RecordingSegmentPayload,
    CollectorStream.COMMAND: CommandPayload,
    CollectorStream.PERSISTENCE_ACK: PersistenceAckPayload,
    CollectorStream.OPERATOR_OBSERVATION: OperatorObservationPayload,
    CollectorStream.ENDPOINT: EndpointPayload,
}


class _InvalidJson(ValueError):
    """Sentinel for strict JSON parser failures without retaining source text."""


class _DuplicateJsonKey(ValueError):
    """Sentinel for a duplicate object key at any JSON depth."""


def _refuse(reason: CollectorAdmissionReason) -> typing.NoReturn:
    """Raise a fixed, source-free whole-bundle refusal."""

    raise CollectorAdmissionError(reason) from None


def _exact_keys(value: object, keys: frozenset[str]) -> typing.TypeGuard[JsonObject]:
    """Return whether a JSON object has exactly the given key set."""

    if not isinstance(value, dict):
        return False
    mapping = typing.cast(JsonObject, value)
    return set(mapping) == set(keys)


def _strict_int(value: object, *, maximum: int = MAX_SEQUENCE) -> typing.TypeGuard[int]:
    """Return whether ``value`` is a non-boolean bounded JSON integer."""

    return type(value) is int and 0 <= value <= maximum


def _finite_number(value: object) -> typing.TypeGuard[int | float]:
    """Return whether ``value`` is a finite JSON measurement number."""

    if type(value) is int:
        return True
    return type(value) is float and math.isfinite(value)


def _representable_measurement(value: object) -> bool:
    """Return whether a finite JSON measurement fits the retained float domain."""

    if not _finite_number(value):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _token(value: object) -> bool:
    """Return whether ``value`` is a bounded synthetic identifier token."""

    return isinstance(value, str) and _TOKEN.fullmatch(value) is not None


def _digest(value: object) -> bool:
    """Return whether ``value`` is a lowercase SHA-256 digest."""

    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON object while refusing duplicate keys at this object depth."""

    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey
        value[key] = item
    return value


def _reject_constant(_: str) -> typing.NoReturn:
    """Refuse JSON's non-finite constant spellings."""

    raise _InvalidJson


def _raw_nesting_exceeds(line: bytes) -> bool:
    """Check bracket nesting before decoding, ignoring quoted JSON content."""

    depth = 0
    in_string = False
    escaped = False
    for byte in line:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                in_string = False
            continue
        if byte == 34:
            in_string = True
        elif byte in (123, 91):
            depth += 1
            if depth > MAX_NESTING:
                return True
        elif byte in (125, 93) and depth > 0:
            depth -= 1
    return False


def _walk_json(value: object, depth: int = 1) -> CollectorAdmissionReason | None:
    """Apply post-parse nesting, string, and finite-number limits."""

    if isinstance(value, str):
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            return CollectorAdmissionReason.JSON_INVALID
        return CollectorAdmissionReason.STRING_LIMIT if len(encoded) > MAX_STRING_BYTES else None
    if type(value) is float and not math.isfinite(value):
        return CollectorAdmissionReason.JSON_INVALID
    if isinstance(value, dict):
        mapping = typing.cast(JsonObject, value)
        if depth > MAX_NESTING:
            return CollectorAdmissionReason.NESTING_LIMIT
        for key, item in mapping.items():
            key_reason = _walk_json(key, depth)
            if key_reason is not None:
                return key_reason
            item_reason = _walk_json(item, depth + 1)
            if item_reason is not None:
                return item_reason
    elif isinstance(value, list):
        items = typing.cast(list[object], value)
        if depth > MAX_NESTING:
            return CollectorAdmissionReason.NESTING_LIMIT
        for item in items:
            item_reason = _walk_json(item, depth + 1)
            if item_reason is not None:
                return item_reason
    return None


def _parse_lines(data: bytes) -> list[object]:
    """Apply all framing, count, line, syntax, and canonical JSON checks."""

    if not data or not data.endswith(b"\n") or data.startswith(b"\xef\xbb\xbf") or b"\r" in data:
        _refuse(CollectorAdmissionReason.FRAMING)
    lines = data[:-1].split(b"\n")
    if any(not line for line in lines):
        _refuse(CollectorAdmissionReason.FRAMING)
    if len(lines) - 1 > MAX_RECORDS:
        _refuse(CollectorAdmissionReason.RECORD_LIMIT)
    if any(len(line) > MAX_LINE_BYTES for line in lines):
        _refuse(CollectorAdmissionReason.LINE_LIMIT)

    parsed: list[object] = []
    for line in lines:
        if _raw_nesting_exceeds(line):
            _refuse(CollectorAdmissionReason.NESTING_LIMIT)
        try:
            text = line.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            _refuse(CollectorAdmissionReason.FRAMING)
        try:
            value = json.loads(
                text,
                object_pairs_hook=_object_pairs,
                parse_constant=_reject_constant,
            )
        except (_DuplicateJsonKey, _InvalidJson, json.JSONDecodeError, RecursionError):
            _refuse(CollectorAdmissionReason.JSON_INVALID)
        walked = _walk_json(value)
        if walked is not None:
            _refuse(walked)
        try:
            canonical = json.dumps(
                value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
            )
        except (TypeError, ValueError):
            _refuse(CollectorAdmissionReason.JSON_INVALID)
        if canonical != text:
            _refuse(CollectorAdmissionReason.NON_CANONICAL)
        parsed.append(value)
    return parsed


def _instant(value: object) -> Instant:
    """Validate one raw instant with no cross-clock comparison."""

    if not _exact_keys(value, frozenset({"clock", "value"})):
        raise ValueError
    if (
        not isinstance(value["clock"], str)
        or value["clock"] not in _CLOCK_NAMES
        or not _finite_number(value["value"])
        or value["value"] < 0
    ):
        raise ValueError
    if value["clock"] == ClockName.RECORDING_SAMPLES.value and not _strict_int(value["value"]):
        raise ValueError
    return Instant.model_validate(value)


def _payload_invalid() -> typing.NoReturn:
    """Raise the private payload validation sentinel without source details."""

    raise ValueError


def _payload(value: object, stream: CollectorStream) -> CollectorPayload:
    """Validate the exact payload grammar selected by its closed stream."""

    if not isinstance(value, dict):
        _payload_invalid()
    value = typing.cast(JsonObject, value)

    def require(keys: set[str]) -> JsonObject:
        if not _exact_keys(value, frozenset(keys)):
            _payload_invalid()
        return value

    if stream is CollectorStream.IDENTITY:
        raw = require(
            {"agent_sha256", "mcp_sha256", "sdk_sha256", "configuration_sha256", "stream_id"}
        )
        digest_keys = ("agent_sha256", "mcp_sha256", "sdk_sha256", "configuration_sha256")
        if not all(_digest(raw[key]) for key in digest_keys) or not _token(raw["stream_id"]):
            _payload_invalid()
    elif stream is CollectorStream.TEMPERATURE_TURN:
        raw = require({"turn_id", "observed_at", "bt_c", "et_c", "status"})
        _instant(raw["observed_at"])
        valid_pair = _representable_measurement(raw["bt_c"]) and _representable_measurement(
            raw["et_c"]
        )
        null_pair = raw["bt_c"] is None and raw["et_c"] is None
        if (
            not _token(raw["turn_id"])
            or raw["status"] not in {"accepted", "invalid"}
            or (raw["status"] == "accepted" and not valid_pair)
            or (raw["status"] == "invalid" and not null_pair)
        ):
            _payload_invalid()
    elif stream is CollectorStream.ROOM_READING:
        raw = require({"observed_at", "temperature_c", "placement_id", "status"})
        _instant(raw["observed_at"])
        temperature_valid = _representable_measurement(raw["temperature_c"])
        placement_valid = raw["placement_id"] is None or _token(raw["placement_id"])
        if (
            not placement_valid
            or raw["status"] not in {"valid", "invalid"}
            or (raw["status"] == "valid" and not temperature_valid)
            or (raw["status"] == "invalid" and raw["temperature_c"] is not None)
        ):
            _payload_invalid()
    elif stream is CollectorStream.HUMAN_FC:
        raw = require({"event_id", "accepted_at"})
        _instant(raw["accepted_at"])
        if not _token(raw["event_id"]):
            _payload_invalid()
    elif stream is CollectorStream.SHADOW_EVENT:
        raw = require({"event_id", "onset_at", "decision_at", "confirming_window_seq"})
        _instant(raw["onset_at"])
        _instant(raw["decision_at"])
        if not _token(raw["event_id"]) or not _strict_int(raw["confirming_window_seq"]):
            _payload_invalid()
    elif stream is CollectorStream.SHADOW_WINDOW:
        raw = require({"start_at", "end_at", "decision_at", "eligibility", "status", "probability"})
        _instant(raw["start_at"])
        _instant(raw["end_at"])
        if raw["decision_at"] is not None:
            _instant(raw["decision_at"])
        probability = raw["probability"]
        probability_valid = _finite_number(probability) and 0 <= probability <= 1
        valid_eligibility = raw["eligibility"] in {"eligible", "pre_charge", "post_drop"}
        valid_status = raw["status"] in {
            "negative",
            "candidate",
            "confirmed",
            "error",
            "not_inferred",
        }
        if not valid_eligibility or not valid_status:
            _payload_invalid()
        if raw["eligibility"] != "eligible":
            if (
                raw["status"] != "not_inferred"
                or raw["decision_at"] is not None
                or probability is not None
            ):
                _payload_invalid()
        elif (
            raw["status"] == "not_inferred"
            or raw["decision_at"] is None
            or (raw["status"] == "error" and probability is not None)
            or (raw["status"] != "error" and not probability_valid)
        ):
            _payload_invalid()
    elif stream is CollectorStream.INFERENCE_CONFIG:
        raw = require({"effective_hop_seconds", "window_seconds"})
        if (
            not _finite_number(raw["effective_hop_seconds"])
            or not _finite_number(raw["window_seconds"])
            or raw["effective_hop_seconds"] <= 0
            or raw["window_seconds"] <= 0
        ):
            _payload_invalid()
    elif stream is CollectorStream.RECORDING_SEGMENT:
        raw = require({"recording_id", "first_sample", "sample_count", "sample_rate_hz"})
        if (
            not _token(raw["recording_id"])
            or not _strict_int(raw["first_sample"])
            or not _strict_int(raw["sample_count"])
            or not _strict_int(raw["sample_rate_hz"])
            or raw["sample_count"] <= 0
            or raw["sample_rate_hz"] <= 0
        ):
            _payload_invalid()
    elif stream is CollectorStream.COMMAND:
        raw = require(
            {
                "command_id",
                "owner_id",
                "generation",
                "observed_at",
                "disposition",
                "commanded_heat_percent",
            }
        )
        _instant(raw["observed_at"])
        heat_valid = _finite_number(raw["commanded_heat_percent"]) and (
            0 <= raw["commanded_heat_percent"] <= 100
        )
        valid_disposition = raw["disposition"] in {
            "not_dispatched",
            "returned_command_state",
            "unknown",
            "superseded",
        }
        if (
            not _token(raw["command_id"])
            or not _token(raw["owner_id"])
            or not _strict_int(raw["generation"])
            or not valid_disposition
            or (raw["disposition"] == "returned_command_state" and not heat_valid)
            or (
                raw["disposition"] != "returned_command_state"
                and raw["commanded_heat_percent"] is not None
            )
        ):
            _payload_invalid()
    elif stream is CollectorStream.PERSISTENCE_ACK:
        raw = require({"command_id", "owner_id", "generation", "observed_at", "status"})
        _instant(raw["observed_at"])
        if (
            not _token(raw["command_id"])
            or not _token(raw["owner_id"])
            or not _strict_int(raw["generation"])
            or raw["status"] not in {"acknowledged", "unavailable", "unknown"}
        ):
            _payload_invalid()
    elif stream is CollectorStream.OPERATOR_OBSERVATION:
        raw = require({"observation_id", "observed_at", "effect"})
        _instant(raw["observed_at"])
        if not _token(raw["observation_id"]) or raw["effect"] not in {
            "heat_off",
            "beans_evacuated",
            "cooling_running",
            "unknown",
        }:
            _payload_invalid()
    elif stream is CollectorStream.ENDPOINT:
        raw = require({"endpoint_id", "accepted_at", "outcome"})
        _instant(raw["accepted_at"])
        if not _token(raw["endpoint_id"]) or raw["outcome"] not in {
            "recipe_target_met",
            "human_protection_interrupted",
            "other_interruption",
            "unknown",
        }:
            _payload_invalid()
    else:
        _payload_invalid()

    try:
        return _PAYLOAD_MODELS[stream].model_validate(value)
    except pydantic.ValidationError:
        _payload_invalid()


def _header(value: object) -> BundleHeader:
    """Validate the single exact v1 header record."""

    header_keys = frozenset(
        {"schema_version", "evidence_class", "bundle_id", "session_id", "inventory", "alignments"}
    )
    if not _exact_keys(value, header_keys):
        _refuse(CollectorAdmissionReason.HEADER_INVALID)
    inventory = value["inventory"]
    alignments = value["alignments"]
    if not isinstance(inventory, dict) or not isinstance(alignments, list):
        _refuse(CollectorAdmissionReason.HEADER_INVALID)
    inventory = typing.cast(JsonObject, inventory)
    alignments = typing.cast(list[object], alignments)
    invalid_header = (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["evidence_class"] != "synthetic"
        or not _token(value["bundle_id"])
        or not _token(value["session_id"])
        or set(inventory) != set(_STREAM_NAMES)
        or len(alignments) > 6
    )
    if invalid_header:
        _refuse(CollectorAdmissionReason.HEADER_INVALID)
    if not all(_strict_int(count, maximum=MAX_STREAM_RECORDS) for count in inventory.values()):
        _refuse(CollectorAdmissionReason.HEADER_INVALID)
    seen_pairs: set[frozenset[str]] = set()
    for anchor in alignments:
        anchor_keys = frozenset({"source_clock", "source_value", "target_clock", "target_value"})
        if not _exact_keys(anchor, anchor_keys):
            _refuse(CollectorAdmissionReason.HEADER_INVALID)
        source = anchor["source_clock"]
        target = anchor["target_clock"]
        invalid_anchor = (
            not isinstance(source, str)
            or not isinstance(target, str)
            or source not in _CLOCK_NAMES
            or target not in _CLOCK_NAMES
            or source == target
            or not _finite_number(anchor["source_value"])
            or not _finite_number(anchor["target_value"])
            or anchor["source_value"] < 0
            or anchor["target_value"] < 0
        )
        if invalid_anchor:
            _refuse(CollectorAdmissionReason.HEADER_INVALID)
        invalid_sample_coordinate = (
            source == ClockName.RECORDING_SAMPLES.value and not _strict_int(anchor["source_value"])
        ) or (
            target == ClockName.RECORDING_SAMPLES.value and not _strict_int(anchor["target_value"])
        )
        if invalid_sample_coordinate:
            _refuse(CollectorAdmissionReason.HEADER_INVALID)
        pair = frozenset({typing.cast(str, source), typing.cast(str, target)})
        if pair in seen_pairs:
            _refuse(CollectorAdmissionReason.HEADER_INVALID)
        seen_pairs.add(pair)
    typed_value = value.copy()
    typed_value["inventory"] = tuple(
        {"stream": stream, "count": count} for stream, count in inventory.items()
    )
    try:
        return BundleHeader.model_validate(typed_value)
    except pydantic.ValidationError:
        _refuse(CollectorAdmissionReason.HEADER_INVALID)


def _envelope(value: object, session_id: str) -> tuple[CollectorStream, int, object]:
    """Validate whole-bundle envelope framing before isolated payload checks."""

    envelope_keys = frozenset({"schema_version", "stream", "session_id", "seq", "payload"})
    if not _exact_keys(value, envelope_keys):
        _refuse(CollectorAdmissionReason.ENVELOPE_INVALID)
    invalid_envelope = (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or not isinstance(value["stream"], str)
        or value["stream"] not in _STREAM_NAMES
        or value["session_id"] != session_id
        or not _strict_int(value["seq"])
    )
    if invalid_envelope:
        _refuse(CollectorAdmissionReason.ENVELOPE_INVALID)
    stream = CollectorStream(typing.cast(str, value["stream"]))
    return stream, typing.cast(int, value["seq"]), value["payload"]


def read_synthetic_bundle(data: bytes, *, expected_sha256: str) -> AdmittedBundle:
    """Read one bounded, digest-matched synthetic v1 evidence bundle.

    Args:
        data: Exact JSONL bytes to admit.
        expected_sha256: Caller-supplied lowercase SHA-256 expectation.

    Returns:
        The typed, isolated admission result.  Its authority is always ``none``.

    Raises:
        CollectorAdmissionError: If a whole-bundle validation rule refuses the bytes.
    """

    if len(data) > MAX_BUNDLE_BYTES:
        _refuse(CollectorAdmissionReason.BUNDLE_SIZE)
    if not _digest(expected_sha256):
        _refuse(CollectorAdmissionReason.EXPECTED_DIGEST_INVALID)
    bundle_sha256 = hashlib.sha256(data).hexdigest()
    if bundle_sha256 != expected_sha256:
        _refuse(CollectorAdmissionReason.DIGEST_MISMATCH)

    values = _parse_lines(data)
    header = _header(values[0])
    counts: dict[CollectorStream, int] = {stream: 0 for stream in _STREAM_ORDER}
    previous_sequences: dict[CollectorStream, int | None] = {
        stream: None for stream in _STREAM_ORDER
    }
    records: dict[CollectorStream, list[CollectorEnvelope]] = {
        stream: [] for stream in _STREAM_ORDER
    }
    malformed: dict[CollectorStream, CollectorStreamReason | None] = {
        stream: None for stream in _STREAM_ORDER
    }

    for value in values[1:]:
        stream, sequence, payload = _envelope(value, header.session_id)
        counts[stream] += 1
        previous = previous_sequences[stream]
        if previous is not None and sequence <= previous:
            if malformed[stream] is None:
                malformed[stream] = CollectorStreamReason.SEQUENCE_CONFLICT
            continue
        previous_sequences[stream] = sequence
        if malformed[stream] is not None:
            continue
        try:
            typed_payload = _payload(payload, stream)
            record = CollectorEnvelope(
                schema_version=1,
                stream=stream,
                session_id=header.session_id,
                seq=sequence,
                payload=typed_payload,
            )
        except (TypeError, ValueError, pydantic.ValidationError):
            malformed[stream] = CollectorStreamReason.PAYLOAD_INVALID
            continue
        records[stream].append(record)

    inventory = {entry.stream: entry.count for entry in header.inventory}
    if any(counts[stream] != inventory[stream] for stream in _STREAM_ORDER):
        _refuse(CollectorAdmissionReason.INVENTORY_MISMATCH)

    entries: list[CollectorStreamEntry] = []
    for stream in _STREAM_ORDER:
        reason = malformed[stream]
        if reason is not None:
            entries.append(
                CollectorStreamEntry(
                    stream=stream,
                    state=CollectorStreamState.MALFORMED,
                    reason=reason,
                    records=(),
                )
            )
        elif counts[stream] == 0:
            entries.append(
                CollectorStreamEntry(
                    stream=stream,
                    state=CollectorStreamState.ABSENT,
                    reason=None,
                    records=(),
                )
            )
        else:
            entries.append(
                CollectorStreamEntry(
                    stream=stream,
                    state=CollectorStreamState.ADMITTED,
                    reason=None,
                    records=tuple(records[stream]),
                )
            )
    return AdmittedBundle(
        bundle_sha256=bundle_sha256,
        header=header,
        streams=tuple(entries),
        authority=CollectorAuthority.NONE,
    )
