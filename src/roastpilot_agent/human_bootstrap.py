"""Pure strict readback grammar for a HUMAN manual/mock bootstrap (#1078 C2a).

This domain module owns the closed grammar of the three bootstrap reads a
HUMAN (manual/mock) child must answer after attach (``get_server_info``,
``get_runtime_config`` and ``get_capture_evidence``), the launch-framing
precondition over the C1 :class:`~roastpilot_agent.launch_composition.LaunchRecord`,
and the typed evidence and refusal values.

It performs no I/O and imports no MCP SDK, filesystem or network code.  The
infrastructure transport (:mod:`roastpilot_agent.mcp_client`) captures the
current session and epoch, performs the bounded reads, and hands each decoded
envelope member here unchanged.

Evidence is bootstrap readback only: the child *reported* manual authority,
the mock driver, audio mode, Celsius units, override permission and its
configured capture predicate.  It is not executable identity, measured
audio, physical sensor units, recording readiness or Start admission, and it
grants no control permission.  The JSON text bound applies to the
independent text decoder here, not to transport frames, and this grammar
does not claim full frame validation (the SDK may already have collapsed
duplicate keys in the structured member).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from enum import Enum
from typing import cast

from roastpilot_agent.human_protection import ControlAuthority
from roastpilot_agent.launch_composition import (
    LaunchRecord,
    RequestedCapture,
    render_human_pins,
)

#: Upper bound, in Unicode code points, on one independent JSON text block.
MAX_BOOTSTRAP_TEXT_CODE_POINTS = 16_384

#: The capture-evidence request protocol and intent (also echoed in the reply).
CAPTURE_EVIDENCE_PROTOCOL = "capture_evidence/v1"
CAPTURE_EVIDENCE_INTENT = "bootstrap"
#: The only admitted capture-evidence capability.
MANUAL_MOCK_CAPTURE = "manual_mock_capture"

#: The bound producer's exact ordered bootstrap tool inventory.  Neither this
#: inventory nor the reported version attests executable identity.
BOOTSTRAP_TOOL_INVENTORY = (
    "get_server_info",
    "get_runtime_config",
    "start_roast_session",
    "get_roast_state",
    "set_heat",
    "set_fan",
    "mark_beans_added",
    "mark_first_crack",
    "drop_beans",
    "start_cooling",
    "stop_cooling",
    "export_roast_log",
    "emergency_stop",
    "set_recording_metadata",
    "finalise_cold_characterisation_session",
)


class BootstrapRead(Enum):
    """The three bootstrap reads, valued by their MCP tool name."""

    SERVER_INFO = "get_server_info"
    RUNTIME_CONFIG = "get_runtime_config"
    CAPTURE_EVIDENCE = "get_capture_evidence"


#: The exact read order; each read is made at most once.
BOOTSTRAP_READ_ORDER = (
    BootstrapRead.SERVER_INFO,
    BootstrapRead.RUNTIME_CONFIG,
    BootstrapRead.CAPTURE_EVIDENCE,
)


class BootstrapRefusal(Enum):
    """Closed, value-free reasons a bootstrap readback is refused."""

    MISSING_LAUNCH_FRAMING = "missing_launch_framing"
    DISPLACED_FRAMING = "displaced_framing"
    READ_FAILED = "read_failed"
    INVALID_PAYLOAD = "invalid_payload"


class EnvelopeMember(Enum):
    """Marker for an envelope member the reply omitted."""

    ABSENT = "absent"


@dataclass(frozen=True, slots=True)
class HumanBootstrapRefused:
    """A refused readback: a closed reason and the read it stopped at, if any.

    It carries no payload, private value or exception text.
    """

    reason: BootstrapRefusal
    read: BootstrapRead | None = None


@dataclass(frozen=True, slots=True)
class HumanBootstrapEvidence:
    """Strictly validated bootstrap readback for one captured child epoch.

    Reported configuration only; it grants no control permission and retains
    no configuration path, roaster port, log directory or model selector.
    """

    child_epoch: int
    server_version: str
    started_at_utc: str
    config_present: bool
    roaster_port_present: bool
    roaster_baudrate: int
    command_interval_seconds: int | float
    sample_interval_seconds: int | float
    auto_t0_detection_enabled: bool
    auto_t0_drop_threshold_c: int | float
    capture_capability: str


HumanBootstrapReadback = HumanBootstrapEvidence | HumanBootstrapRefused


def bootstrap_arguments(read: BootstrapRead) -> dict[str, object]:
    """Return a fresh copy of the exact arguments for one read.

    Args:
        read: The bootstrap read.

    Returns:
        ``{}`` for the two identity reads; the closed capture request otherwise.
    """
    if read is BootstrapRead.CAPTURE_EVIDENCE:
        return {"capture_protocol": CAPTURE_EVIDENCE_PROTOCOL, "intent": CAPTURE_EVIDENCE_INTENT}
    return {}


def check_launch_framing(
    requested: RequestedCapture | None, epoch: object, record: LaunchRecord | None
) -> HumanBootstrapRefused | None:
    """Check that the captured launch record frames this HUMAN request.

    Args:
        requested: The capture the transport was constructed with.
        epoch: The child epoch captured before the first await.
        record: The launch record for exactly that epoch, if any.

    Returns:
        ``None`` when the framing holds, else a ``MISSING_LAUNCH_FRAMING`` refusal.
    """
    missing = HumanBootstrapRefused(BootstrapRefusal.MISSING_LAUNCH_FRAMING)
    if type(epoch) is not int or epoch <= 0:
        return missing
    if type(requested) is not RequestedCapture or type(record) is not LaunchRecord:
        return missing
    evidence = record.evidence
    if (
        type(record.child_epoch) is not int
        or record.child_epoch != epoch
        or evidence.authority is not ControlAuthority.HUMAN
        or type(evidence.capture) is not RequestedCapture
        or evidence.capture != requested
        or type(evidence.pinned_env) is not tuple
        or evidence.pinned_env != render_human_pins(requested)
        or type(evidence.config_present) is not bool
    ):
        return missing
    return None


# --------------------------------------------------------------------------- text


class _Undecodable(Enum):
    TOKEN = "undecodable"


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_name: str) -> object:
    raise ValueError("nonfinite constant")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("nonfinite number")
    return value


def _decode_text(text: str) -> object:
    if len(text) > MAX_BOOTSTRAP_TEXT_CODE_POINTS:
        return _Undecodable.TOKEN
    try:
        return cast(
            "object",
            json.loads(
                text,
                object_pairs_hook=_reject_duplicates,
                parse_constant=_reject_constant,
                parse_float=_finite_float,
            ),
        )
    except (ValueError, RecursionError):
        # ValueError covers JSONDecodeError and over-long integer literals.
        return _Undecodable.TOKEN


def _single_text(content: object) -> object:
    if type(content) is not list:
        return _Undecodable.TOKEN
    blocks = cast("list[object]", content)
    if len(blocks) != 1 or type(blocks[0]) is not dict:
        return _Undecodable.TOKEN
    block = cast("dict[object, object]", blocks[0])
    if block.keys() != {"type", "text"} or not _is_text(block["type"], "text"):
        return _Undecodable.TOKEN
    text = block["text"]
    if type(text) is not str:
        return _Undecodable.TOKEN
    return _decode_text(text)


# --------------------------------------------------------------------------- shapes


def _is_text(value: object, expected: str) -> bool:
    return type(value) is str and value == expected


def _nonempty_text(value: object) -> bool:
    return type(value) is str and value != ""


def _optional_text(value: object) -> bool:
    return value is None or _nonempty_text(value)


def _finite_number(value: object) -> bool:
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is int:
        try:
            return math.isfinite(float(value))
        except OverflowError:
            return False
    return False


def _object(value: object, keys: frozenset[str]) -> dict[str, object] | None:
    if type(value) is not dict:
        return None
    data = cast("dict[str, object]", value)
    if data.keys() != keys:
        return None
    return data


_SERVER_CONSTANTS = {
    "product_name": "RoastPilot",
    "package_name": "coffee-roaster-mcp",
    "transport": "stdio",
    "current_phase": "bootstrap",
    "roaster_driver": "mock",
    "first_crack_mode": "audio",
    "session_presence": "none",
}
_SERVER_KEYS = frozenset(
    {*_SERVER_CONSTANTS, "version", "started_at_utc", "bootstrap_safe", "available_bootstrap_tools"}
)

_RUNTIME_CONSTANTS = {
    "roaster_driver": "mock",
    "temperature_unit": "celsius",
    "first_crack_mode": "audio",
    "first_crack_event_authority": "manual",
    "first_crack_observer_protocol": "manual/v1",
}
_RUNTIME_OPTIONAL_TEXT = ("config_source", "roaster_port")
_RUNTIME_NUMBERS = (
    "command_interval_seconds",
    "sample_interval_seconds",
    "auto_t0_drop_threshold_c",
)
_RUNTIME_TEXT = ("model_repo_id", "model_precision", "log_dir")
_RUNTIME_KEYS = frozenset(
    {
        *_RUNTIME_CONSTANTS,
        *_RUNTIME_OPTIONAL_TEXT,
        *_RUNTIME_NUMBERS,
        *_RUNTIME_TEXT,
        "roaster_baudrate",
        "allow_manual_override",
        "auto_t0_detection_enabled",
    }
)

_CAPTURE_EVIDENCE = {
    "capture_protocol": CAPTURE_EVIDENCE_PROTOCOL,
    "intent": CAPTURE_EVIDENCE_INTENT,
    "capability": MANUAL_MOCK_CAPTURE,
}
_CAPTURE_KEYS = frozenset(_CAPTURE_EVIDENCE)


def _server_info_shape(value: object) -> bool:
    data = _object(value, _SERVER_KEYS)
    if data is None:
        return False
    tools = data["available_bootstrap_tools"]
    return (
        all(_is_text(data[key], expected) for key, expected in _SERVER_CONSTANTS.items())
        and data["bootstrap_safe"] is False
        and _nonempty_text(data["version"])
        and _nonempty_text(data["started_at_utc"])
        and type(tools) is list
        and len(cast("list[object]", tools)) == len(BOOTSTRAP_TOOL_INVENTORY)
        and all(
            _is_text(item, expected)
            for item, expected in zip(
                cast("list[object]", tools), BOOTSTRAP_TOOL_INVENTORY, strict=True
            )
        )
    )


def _runtime_config_shape(value: object) -> bool:
    data = _object(value, _RUNTIME_KEYS)
    if data is None:
        return False
    baudrate = data["roaster_baudrate"]
    return (
        all(_is_text(data[key], expected) for key, expected in _RUNTIME_CONSTANTS.items())
        and data["allow_manual_override"] is True
        and all(_optional_text(data[key]) for key in _RUNTIME_OPTIONAL_TEXT)
        and all(_finite_number(data[key]) for key in _RUNTIME_NUMBERS)
        and all(_nonempty_text(data[key]) for key in _RUNTIME_TEXT)
        and type(baudrate) is int
        and baudrate > 0
        and type(data["auto_t0_detection_enabled"]) is bool
    )


def _capture_evidence_shape(value: object) -> bool:
    data = _object(value, _CAPTURE_KEYS)
    return data is not None and all(
        _is_text(data[key], expected) for key, expected in _CAPTURE_EVIDENCE.items()
    )


_SHAPES = {
    BootstrapRead.SERVER_INFO: _server_info_shape,
    BootstrapRead.RUNTIME_CONFIG: _runtime_config_shape,
    BootstrapRead.CAPTURE_EVIDENCE: _capture_evidence_shape,
}


def _same(left: object, right: object) -> bool:
    """Type-sensitive equality over already-validated bounded JSON shapes."""
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        left_map = cast("dict[str, object]", left)
        right_map = cast("dict[str, object]", right)
        return left_map.keys() == right_map.keys() and all(
            _same(item, right_map[key]) for key, item in left_map.items()
        )
    if type(left) is list:
        left_list = cast("list[object]", left)
        right_list = cast("list[object]", right)
        return len(left_list) == len(right_list) and all(
            _same(a, b) for a, b in zip(left_list, right_list, strict=True)
        )
    return left == right


def decode_bootstrap_reply(
    read: BootstrapRead,
    *,
    is_error: object,
    content: object,
    structured: object,
    config_present: bool,
) -> dict[str, object] | None:
    """Validate one bootstrap reply against its closed grammar.

    The structured member and the single independent JSON text block must
    each satisfy the exact per-read shape, then agree type-sensitively.

    Args:
        read: Which read this reply answers.
        is_error: The decoded ``isError`` member (``False`` by protocol default).
        content: The decoded ``content`` member, or :attr:`EnvelopeMember.ABSENT`.
        structured: The decoded ``structuredContent`` member, or ``ABSENT``.
        config_present: Whether C1 requested a present child configuration.

    Returns:
        The validated result object, or ``None`` on any refusal.
    """
    if is_error is not False:
        return None
    if read is BootstrapRead.CAPTURE_EVIDENCE:
        wrapper = _object(structured, frozenset({"result"}))
        if wrapper is None:
            return None
        structured = wrapper["result"]
    text_value = _single_text(content)
    shape = _SHAPES[read]
    if not shape(structured) or not shape(text_value) or not _same(structured, text_value):
        return None
    result = cast("dict[str, object]", structured)
    if read is BootstrapRead.RUNTIME_CONFIG and (result["config_source"] is not None) != (
        config_present
    ):
        return None
    return result


def build_bootstrap_evidence(
    child_epoch: int,
    server_info: dict[str, object],
    runtime_config: dict[str, object],
    capture_evidence: dict[str, object],
) -> HumanBootstrapEvidence:
    """Project three validated replies into evidence without private values.

    Args:
        child_epoch: The captured child epoch.
        server_info: The validated ``get_server_info`` result.
        runtime_config: The validated ``get_runtime_config`` result.
        capture_evidence: The validated capture-evidence result.

    Returns:
        The evidence.
    """
    return HumanBootstrapEvidence(
        child_epoch=child_epoch,
        server_version=cast("str", server_info["version"]),
        started_at_utc=cast("str", server_info["started_at_utc"]),
        config_present=runtime_config["config_source"] is not None,
        roaster_port_present=runtime_config["roaster_port"] is not None,
        roaster_baudrate=cast("int", runtime_config["roaster_baudrate"]),
        command_interval_seconds=cast("int | float", runtime_config["command_interval_seconds"]),
        sample_interval_seconds=cast("int | float", runtime_config["sample_interval_seconds"]),
        auto_t0_detection_enabled=cast("bool", runtime_config["auto_t0_detection_enabled"]),
        auto_t0_drop_threshold_c=cast("int | float", runtime_config["auto_t0_drop_threshold_c"]),
        capture_capability=cast("str", capture_evidence["capability"]),
    )


class StartAttemptState(Enum):
    """Process-local one-shot state, without durable or control authority."""

    UNATTEMPTED = "unattempted"
    RESERVED = "reserved"
    REFUSED_BEFORE_CLAIM = "refused_before_claim"
    CLAIMED = "claimed"
    ACCEPTED = "accepted"


class StartRefusal(Enum):
    """Reasons known to precede the private Start claim."""

    ALREADY_ATTEMPTED = "already_attempted"
    NO_PROOF = "no_proof"
    INVALID_METADATA_REQUEST = "invalid_metadata_request"
    METADATA_FAILED = "metadata_failed"
    DISPLACED_BEFORE_CLAIM = "displaced_before_claim"
    GATE_REFUSED = "gate_refused"


class StartUncertainty(Enum):
    """Reasons that cannot exclude producer entry."""

    DISPLACED_AFTER_CLAIM = "displaced_after_claim"
    AUTHORITY_LOST = "authority_lost"
    TRANSPORT_FAILED = "transport_failed"
    PRODUCER_ERROR = "producer_error"
    INVALID_REPLY = "invalid_reply"


@dataclass(frozen=True, slots=True)
class HumanStartRefused:
    """Value-free refusal before any Start request was claimed."""

    reason: StartRefusal


@dataclass(frozen=True, slots=True)
class HumanStartUncertain:
    """Value-free uncertainty after possible Start entry."""

    reason: StartUncertainty


@dataclass(frozen=True, slots=True)
class HumanStartAccepted:
    """Validated acknowledgement, without capture or actuation permission."""

    session_id: str
    child_epoch: int


HumanStartResult = HumanStartRefused | HumanStartUncertain | HumanStartAccepted


def start_arguments() -> dict[str, object]:
    """Return the private manual mock Start arguments."""
    return {"purpose": "roast", "first_crack_observer_ack": "manual/v1"}


def valid_start_metadata(origin: object, roast_num: object) -> bool:
    """Validate an omitted pair or bounded canonical recording metadata.

    Args:
        origin: Optional canonical lower-case origin slug.
        roast_num: Optional paired integer roast number.

    Returns:
        Whether the complete pair is admitted on this private mock path.
    """
    if origin is None and roast_num is None:
        return True
    if type(origin) is not str or type(roast_num) is not int:
        return False
    return (
        0 < len(origin) <= 200
        and all(char in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in origin)
        and not origin.startswith("-")
        and not origin.endswith("-")
        and "--" not in origin
        and 0 <= roast_num <= 2**63 - 1
    )


def _bounded_start_json(value: object) -> bool:
    pending: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > 2048 or depth > 16:
            return False
        if type(item) is dict:
            mapping = cast("dict[object, object]", item)
            if len(mapping) > 2048 - nodes or any(
                type(key) is not str or len(key) > MAX_BOOTSTRAP_TEXT_CODE_POINTS for key in mapping
            ):
                return False
            pending.extend((child, depth + 1) for child in mapping.values())
        elif type(item) is list:
            sequence = cast("list[object]", item)
            if len(sequence) > 2048 - nodes:
                return False
            pending.extend((child, depth + 1) for child in sequence)
        elif type(item) is str:
            if len(item) > MAX_BOOTSTRAP_TEXT_CODE_POINTS:
                return False
        elif item is not None and type(item) is not bool and not _finite_number(item):
            return False
    return True


def _start_reply(is_error: object, content: object, structured: object) -> dict[str, object] | None:
    if is_error is not False:
        return None
    text = _single_text(content)
    if (
        not _bounded_start_json(structured)
        or not _bounded_start_json(text)
        or not _same(structured, text)
        or type(structured) is not dict
    ):
        return None
    return cast("dict[str, object]", structured)


def decode_metadata_reply(
    *, is_error: object, content: object, structured: object, origin: str, roast_num: int
) -> bool:
    """Require the exact canonical metadata echo in both representations.

    Args:
        is_error: Raw SDK error member.
        content: Raw SDK text blocks.
        structured: Raw structured member.
        origin: Previously validated origin sent to the child.
        roast_num: Previously validated roast number sent to the child.

    Returns:
        Whether both bounded representations echo the exact request.
    """
    value = _start_reply(is_error, content, structured)
    return value is not None and _same(value, {"origin": origin, "roast_num": roast_num})


def decode_start_reply(*, is_error: object, content: object, structured: object) -> str | None:
    """Validate bounded independent Start representations and project only identity.

    Args:
        is_error: Raw SDK error member.
        content: Raw SDK text blocks.
        structured: Raw structured member.

    Returns:
        The strict session ID, or None for any refusal.
    """
    value = _start_reply(is_error, content, structured)
    if value is None or value.keys() != {"session"} or type(value["session"]) is not dict:
        return None
    session = cast("dict[str, object]", value["session"])
    ident = session.get("session_id")
    if (
        type(ident) is not str
        or len(ident) != 32
        or any(char not in "0123456789abcdef" for char in ident)
        or session.get("active") is not True
        or not _is_text(session.get("phase"), "pre_roast")
        or not _is_text(session.get("session_purpose"), "roast")
        or any(
            name not in session or session[name] is not None
            for name in ("stopped_at_utc", "faulted_at_utc", "first_crack_at_utc")
        )
    ):
        return None
    return ident
