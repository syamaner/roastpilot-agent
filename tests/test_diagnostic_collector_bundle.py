"""Contract tests for D230 S1 synthetic collector bundle admission."""

import ast
import hashlib
import json
import typing
from pathlib import Path

import pydantic
import pytest

from roastpilot_agent.diagnostic_collector import (
    CollectorAdmissionError,
    CollectorAdmissionReason,
    CollectorAuthority,
    CollectorStream,
    CollectorStreamReason,
    CollectorStreamState,
    bundle_reader,
    bundle_schema,
    read_synthetic_bundle,
)
from roastpilot_agent.diagnostic_collector.bundle_schema import (
    AdmittedBundle,
    HumanFirstCrackPayload,
    ShadowEventPayload,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "d230_collector" / "synthetic-v1.jsonl"


def _canonical_lines() -> list[dict[str, object]]:
    """Load the committed synthetic fixture into mutable JSON values."""

    return [
        typing.cast(dict[str, object], json.loads(line))
        for line in _FIXTURE.read_text(encoding="utf-8").splitlines()
    ]


def _bundle(lines: list[dict[str, object]]) -> bytes:
    """Encode strict canonical JSON Lines from JSON-compatible values."""

    return b"".join(
        json.dumps(
            line, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode()
        + b"\n"
        for line in lines
    )


def _read(lines: list[dict[str, object]]) -> AdmittedBundle:
    """Admit canonical test lines using their own expected digest."""

    data = _bundle(lines)
    return read_synthetic_bundle(data, expected_sha256=hashlib.sha256(data).hexdigest())


def _expect(reason: CollectorAdmissionReason, data: bytes, expected: str | None = None) -> None:
    """Assert one source-free, closed whole-bundle refusal reason."""

    with pytest.raises(CollectorAdmissionError) as raised:
        read_synthetic_bundle(data, expected_sha256=expected or hashlib.sha256(data).hexdigest())
    assert raised.value.reason is reason
    assert str(raised.value) == "synthetic collector bundle refused"
    assert raised.value.__suppress_context__ is True


def _stream(lines: list[dict[str, object]], stream: CollectorStream) -> dict[str, object]:
    """Return one fixture envelope selected by its closed stream name."""

    for line in lines[1:]:
        if line["stream"] == stream.value:
            return line
    raise AssertionError("fixture stream missing")


def _object_field(line: dict[str, object], name: str) -> dict[str, object]:
    """Return a fixture object field after asserting its JSON-object shape."""

    field = line[name]
    assert isinstance(field, dict)
    return typing.cast(dict[str, object], field)


def test_admits_all_streams_with_typed_records_and_no_readiness_claim() -> None:
    """A1/A3: retain all distinct synthetic evidence in fixed stream order."""

    result = _read(_canonical_lines())

    assert result.authority is CollectorAuthority.NONE
    assert result.model_dump().keys() == {"bundle_sha256", "header", "streams", "authority"}
    assert tuple(entry.stream for entry in result.streams) == tuple(CollectorStream)
    assert all(entry.state is CollectorStreamState.ADMITTED for entry in result.streams)
    assert all(entry.reason is None and len(entry.records) == 1 for entry in result.streams)
    human = next(entry for entry in result.streams if entry.stream is CollectorStream.HUMAN_FC)
    shadow = next(entry for entry in result.streams if entry.stream is CollectorStream.SHADOW_EVENT)
    assert isinstance(human.records[0].payload, HumanFirstCrackPayload)
    assert isinstance(shadow.records[0].payload, ShadowEventPayload)
    assert human.records[0].payload.accepted_at.value == 3.0
    assert shadow.records[0].payload.onset_at.value == 4.0
    assert shadow.records[0].payload.decision_at.value == 5.0


def test_zero_inventory_is_an_absent_stream_not_a_synthesized_record() -> None:
    """A2: an explicit zero inventory is retained as absence."""

    lines = _canonical_lines()
    _object_field(lines[0], "inventory")["endpoint"] = 0
    lines = [lines[0], *[line for line in lines[1:] if line["stream"] != "endpoint"]]

    result = _read(lines)

    endpoint = next(entry for entry in result.streams if entry.stream is CollectorStream.ENDPOINT)
    assert endpoint.state is CollectorStreamState.ABSENT
    assert endpoint.reason is None
    assert endpoint.records == ()


def test_pre_hash_size_bound_does_not_call_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard-removal witness: size refusal precedes every SHA-256 call."""

    def unexpected_digest(_: bytes) -> object:
        raise AssertionError("digest called")

    monkeypatch.setattr(hashlib, "sha256", unexpected_digest)
    _expect(
        CollectorAdmissionReason.BUNDLE_SIZE,
        b"x" * (bundle_reader.MAX_BUNDLE_BYTES + 1),
        "a" * 64,
    )


def test_digest_and_framing_order_are_closed() -> None:
    """Digest expectation and mismatch both precede JSONL framing validation."""

    _expect(CollectorAdmissionReason.EXPECTED_DIGEST_INVALID, b"not-json", "A" * 64)
    _expect(CollectorAdmissionReason.DIGEST_MISMATCH, b"not-json", "a" * 64)
    _expect(CollectorAdmissionReason.FRAMING, b"{}")
    _expect(CollectorAdmissionReason.FRAMING, b"\xef\xbb\xbf{}\n")
    _expect(CollectorAdmissionReason.FRAMING, b"{}\r\n")
    _expect(CollectorAdmissionReason.FRAMING, b"{}\n\n")


def test_record_line_nesting_json_string_and_canonical_bounds() -> None:
    """Exercise framing-independent parser bounds in contract order."""

    _expect(CollectorAdmissionReason.RECORD_LIMIT, b"{}\n" * (bundle_reader.MAX_RECORDS + 2))
    _expect(CollectorAdmissionReason.LINE_LIMIT, b" " * (bundle_reader.MAX_LINE_BYTES + 1) + b"\n")
    _expect(CollectorAdmissionReason.NESTING_LIMIT, b"[[[[[[[]]]]]]]\n")
    _expect(CollectorAdmissionReason.JSON_INVALID, b'{"x":NaN}\n')
    _expect(CollectorAdmissionReason.JSON_INVALID, b'{"x":1,"x":2}\n')
    _expect(CollectorAdmissionReason.STRING_LIMIT, b'{"' + b"x" * 129 + b'":0}\n')
    _expect(CollectorAdmissionReason.NON_CANONICAL, b'{ "x":0}\n')


def test_exact_parser_bounds_admit_the_boundary_before_later_validation() -> None:
    """Exact line, nesting, and UTF-8 bounds do not fail one item early."""

    _expect(CollectorAdmissionReason.JSON_INVALID, b" " * bundle_reader.MAX_LINE_BYTES + b"\n")
    _expect(CollectorAdmissionReason.HEADER_INVALID, b"[[[[[[]]]]]]\n")

    lines = _canonical_lines()
    _object_field(_stream(lines, CollectorStream.HUMAN_FC), "payload")["event_id"] = "x" * 128
    result = _read(lines)
    human = next(entry for entry in result.streams if entry.stream is CollectorStream.HUMAN_FC)
    assert human.state is CollectorStreamState.MALFORMED
    assert human.reason is CollectorStreamReason.PAYLOAD_INVALID


def test_nested_duplicate_keys_and_boolean_versions_are_strictly_refused() -> None:
    """Duplicate keys and boolean versions cannot be accepted through JSON equality."""

    _expect(CollectorAdmissionReason.JSON_INVALID, b'{"x":{"a":1,"a":2}}\n')

    lines = _canonical_lines()
    lines[0]["schema_version"] = True
    _expect(CollectorAdmissionReason.HEADER_INVALID, _bundle(lines))

    lines = _canonical_lines()
    _stream(lines, CollectorStream.HUMAN_FC)["schema_version"] = True
    _expect(CollectorAdmissionReason.ENVELOPE_INVALID, _bundle(lines))


def test_header_refusal_precedes_a_later_envelope_fault() -> None:
    """Whole-bundle reason ordering pins header validation before envelopes."""

    lines = _canonical_lines()
    lines[0]["evidence_class"] = "live"
    _stream(lines, CollectorStream.HUMAN_FC)["seq"] = True
    _expect(CollectorAdmissionReason.HEADER_INVALID, _bundle(lines))


def test_parser_rejects_an_escaped_lone_surrogate_as_json_invalid() -> None:
    """A canonical JSON escape must still name a valid Unicode scalar string."""

    data = _FIXTURE.read_bytes().replace(b'"bundle_1"', b'"\\ud800"', 1)
    _expect(CollectorAdmissionReason.JSON_INVALID, data)


def test_header_envelope_and_inventory_are_whole_bundle_refusals() -> None:
    """Header identity, envelope framing, and inventory cannot be isolated."""

    lines = _canonical_lines()
    lines[0]["evidence_class"] = "live"
    _expect(CollectorAdmissionReason.HEADER_INVALID, _bundle(lines))

    lines = _canonical_lines()
    _stream(lines, CollectorStream.HUMAN_FC)["session_id"] = "foreign"
    _expect(CollectorAdmissionReason.ENVELOPE_INVALID, _bundle(lines))

    lines = _canonical_lines()
    _object_field(lines[0], "inventory")["human_fc"] = 2
    _expect(CollectorAdmissionReason.INVENTORY_MISMATCH, _bundle(lines))


def test_bad_shadow_payload_is_isolated_while_human_first_crack_is_admitted() -> None:
    """A2/A3: malformed shadow evidence neither erases nor replaces human FC."""

    lines = _canonical_lines()
    _object_field(_stream(lines, CollectorStream.SHADOW_EVENT), "payload")["event_id"] = "bad id"
    result = _read(lines)

    shadow = next(entry for entry in result.streams if entry.stream is CollectorStream.SHADOW_EVENT)
    human = next(entry for entry in result.streams if entry.stream is CollectorStream.HUMAN_FC)
    assert shadow.state is CollectorStreamState.MALFORMED
    assert shadow.reason is CollectorStreamReason.PAYLOAD_INVALID
    assert shadow.records == ()
    assert human.state is CollectorStreamState.ADMITTED
    assert isinstance(human.records[0].payload, HumanFirstCrackPayload)


def test_non_representable_temperature_integer_is_isolated_without_losing_human_fc() -> None:
    """A huge JSON integer is not silently converted to a temperature float."""

    lines = _canonical_lines()
    _object_field(_stream(lines, CollectorStream.TEMPERATURE_TURN), "payload")["bt_c"] = 10**400
    result = _read(lines)

    temperature = next(
        entry for entry in result.streams if entry.stream is CollectorStream.TEMPERATURE_TURN
    )
    human = next(entry for entry in result.streams if entry.stream is CollectorStream.HUMAN_FC)
    assert temperature.state is CollectorStreamState.MALFORMED
    assert temperature.reason is CollectorStreamReason.PAYLOAD_INVALID
    assert human.state is CollectorStreamState.ADMITTED


def test_duplicate_sequence_precedes_payload_for_the_same_stream() -> None:
    """A2: strict sequence failure wins over a simultaneous payload failure."""

    lines = _canonical_lines()
    duplicate = _stream(lines, CollectorStream.SHADOW_EVENT).copy()
    duplicate["payload"] = {"event_id": "bad id"}
    lines.append(duplicate)
    _object_field(lines[0], "inventory")["shadow_event"] = 2

    result = _read(lines)

    shadow = next(entry for entry in result.streams if entry.stream is CollectorStream.SHADOW_EVENT)
    assert shadow.state is CollectorStreamState.MALFORMED
    assert shadow.reason is CollectorStreamReason.SEQUENCE_CONFLICT
    assert shadow.records == ()


@pytest.mark.parametrize("stream", tuple(CollectorStream))
def test_closed_dispatch_rejects_a_valid_competing_stream_payload(stream: CollectorStream) -> None:
    """Each stream selects only its own payload grammar, not a union fallback."""

    lines = _canonical_lines()
    target = _stream(lines, stream)
    competing_stream = (
        CollectorStream.IDENTITY
        if stream is not CollectorStream.IDENTITY
        else CollectorStream.ENDPOINT
    )
    competing = _stream(lines, competing_stream)
    target["payload"] = competing["payload"]

    result = _read(lines)

    entry = next(item for item in result.streams if item.stream is stream)
    assert entry.state is CollectorStreamState.MALFORMED
    assert entry.reason is CollectorStreamReason.PAYLOAD_INVALID
    assert entry.records == ()


@pytest.mark.parametrize(
    ("stream", "field", "value"),
    [
        (CollectorStream.TEMPERATURE_TURN, "bt_c", True),
        (CollectorStream.SHADOW_EVENT, "confirming_window_seq", True),
        (CollectorStream.COMMAND, "generation", True),
        (CollectorStream.RECORDING_SEGMENT, "sample_rate_hz", 0),
        (CollectorStream.INFERENCE_CONFIG, "window_seconds", 0.0),
        (CollectorStream.ROOM_READING, "placement_id", "bad id"),
    ],
)
def test_strict_numbers_nullable_fields_and_tokens_are_not_coerced(
    stream: CollectorStream, field: str, value: object
) -> None:
    """Booleans never become numbers; nullable and token fields remain exact."""

    lines = _canonical_lines()
    _object_field(_stream(lines, stream), "payload")[field] = value
    result = _read(lines)

    entry = next(item for item in result.streams if item.stream is stream)
    assert entry.state is CollectorStreamState.MALFORMED
    assert entry.reason is CollectorStreamReason.PAYLOAD_INVALID


@pytest.mark.parametrize(
    ("eligibility", "status", "decision_at", "probability", "valid"),
    [
        ("pre_charge", "not_inferred", None, None, True),
        ("pre_charge", "negative", None, None, False),
        ("eligible", "error", {"clock": "agent_monotonic_seconds", "value": 1.0}, None, True),
        ("eligible", "error", {"clock": "agent_monotonic_seconds", "value": 1.0}, 0.1, False),
        ("eligible", "confirmed", None, 0.1, False),
    ],
)
def test_shadow_window_nullable_and_enum_rules(
    eligibility: str, status: str, decision_at: object, probability: object, valid: bool
) -> None:
    """Shadow window nullable fields follow the closed eligibility grammar."""

    lines = _canonical_lines()
    payload = _object_field(_stream(lines, CollectorStream.SHADOW_WINDOW), "payload")
    payload.update(
        eligibility=eligibility, status=status, decision_at=decision_at, probability=probability
    )
    result = _read(lines)
    entry = next(item for item in result.streams if item.stream is CollectorStream.SHADOW_WINDOW)
    assert (entry.state is CollectorStreamState.ADMITTED) is valid


def test_bundle_header_rejects_duplicate_alignment_pair_and_recording_float() -> None:
    """Anchors preserve only distinct raw clock pairs and integral sample coordinates."""

    lines = _canonical_lines()
    lines[0]["alignments"] = [
        {
            "source_clock": "agent_monotonic_seconds",
            "source_value": 1.0,
            "target_clock": "recording_samples",
            "target_value": 1,
        },
        {
            "source_clock": "recording_samples",
            "source_value": 2,
            "target_clock": "agent_monotonic_seconds",
            "target_value": 2.0,
        },
    ]
    _expect(CollectorAdmissionReason.HEADER_INVALID, _bundle(lines))


def test_unhashable_closed_discriminants_refuse_without_a_raw_type_error() -> None:
    """Header and envelope discriminants are type-checked before closed-vocabulary lookup."""

    lines = _canonical_lines()
    lines[0]["alignments"] = [
        {
            "source_clock": [],
            "source_value": 1,
            "target_clock": "agent_monotonic_seconds",
            "target_value": 1,
        }
    ]
    _expect(CollectorAdmissionReason.HEADER_INVALID, _bundle(lines))

    lines = _canonical_lines()
    _stream(lines, CollectorStream.HUMAN_FC)["stream"] = []
    _expect(CollectorAdmissionReason.ENVELOPE_INVALID, _bundle(lines))


def test_header_inventory_is_immutable_after_admission() -> None:
    """The returned typed header has no mutable inventory mapping."""

    result = _read(_canonical_lines())
    assert isinstance(result.header.inventory, tuple)
    with pytest.raises(pydantic.ValidationError):
        result.header.inventory[0].count = 2  # type: ignore[misc]

    lines = _canonical_lines()
    lines[0]["alignments"] = [
        {
            "source_clock": "recording_samples",
            "source_value": 1.5,
            "target_clock": "agent_monotonic_seconds",
            "target_value": 2.0,
        }
    ]
    _expect(CollectorAdmissionReason.HEADER_INVALID, _bundle(lines))


def test_boundary_has_one_reader_entrypoint_and_no_hardware_or_io_imports() -> None:
    """The S1 package remains callable, stdlib/Pydantic-only, and hardware-free."""

    assert [name for name in dir(bundle_reader) if name == "read_synthetic_bundle"] == [
        "read_synthetic_bundle"
    ]
    reader_tree = ast.parse(Path(bundle_reader.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(reader_tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module.split(".")[0]
        for node in ast.walk(reader_tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )
    assert imported <= {"hashlib", "json", "math", "re", "typing", "pydantic", "roastpilot_agent"}
    assert not {"mcp_client", "controller", "live", "serial", "sounddevice"} & imported
    assert not hasattr(bundle_reader, "open")
    assert tuple(CollectorStream) == tuple(bundle_schema.CollectorStream)
