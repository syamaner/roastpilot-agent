"""Contract tests for D230 S1 synthetic collector bundle admission."""

import ast
import hashlib
import json
import os
import subprocess
import sys
import typing
from pathlib import Path

import pydantic
import pytest

import roastpilot_agent.diagnostic_collector as collector_package
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
    RoomReadingPayload,
    ShadowEventPayload,
    TemperatureTurnPayload,
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
    assert raised.value.__context__ is None


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


def test_staged_mixed_fault_order_uses_prechecks_then_decoded_traversal() -> None:
    """Ratified mixed faults select byte prechecks, then key/value traversal order."""

    _expect(
        CollectorAdmissionReason.LINE_LIMIT,
        b"\xff\n" + b"x" * (bundle_reader.MAX_LINE_BYTES + 1) + b"\n",
    )
    oversized = b"x" * (bundle_reader.MAX_STRING_BYTES + 1)
    _expect(CollectorAdmissionReason.STRING_LIMIT, b'{"x":"' + oversized + b'","y":1e400}\n')
    _expect(CollectorAdmissionReason.JSON_INVALID, b'{"y":1e400,"x":"' + oversized + b'"}\n')


def test_staged_fault_order_across_raw_parse_header_envelope_and_inventory() -> None:
    """Each stage wins only after every earlier stage has admitted the bytes."""

    _expect(CollectorAdmissionReason.NESTING_LIMIT, b"[[[[[[[\xff\n")
    _expect(CollectorAdmissionReason.NON_CANONICAL, b'{ "x":0}\n[[[[[[[]]]]]]]\n')

    lines = _canonical_lines()
    lines[0]["evidence_class"] = "live"
    _expect(CollectorAdmissionReason.JSON_INVALID, _bundle(lines) + b'{"x":\n')

    lines = _canonical_lines()
    _object_field(lines[0], "inventory")[CollectorStream.HUMAN_FC.value] = 2
    _stream(lines, CollectorStream.ENDPOINT)["session_id"] = "foreign"
    _expect(CollectorAdmissionReason.ENVELOPE_INVALID, _bundle(lines))


def test_precheck_order_is_framing_then_record_count_then_line_length() -> None:
    """Whole-bundle byte prechecks retain their ratified priority."""

    _expect(CollectorAdmissionReason.FRAMING, b"\n" * (bundle_reader.MAX_RECORDS + 3))
    _expect(
        CollectorAdmissionReason.RECORD_LIMIT,
        b"{}\n" * (bundle_reader.MAX_RECORDS + 2)
        + b"x" * (bundle_reader.MAX_LINE_BYTES + 1)
        + b"\n",
    )


def test_utf8_string_limit_counts_encoded_bytes_not_characters() -> None:
    """A two-byte decoded character reaches 128 bytes at 64 copies, not 128 copies."""

    lines = _canonical_lines()
    _object_field(_stream(lines, CollectorStream.HUMAN_FC), "payload")["event_id"] = "é" * 64
    result = _read(lines)
    human = next(entry for entry in result.streams if entry.stream is CollectorStream.HUMAN_FC)
    assert human.state is CollectorStreamState.MALFORMED
    assert human.reason is CollectorStreamReason.PAYLOAD_INVALID

    lines = _canonical_lines()
    _object_field(_stream(lines, CollectorStream.HUMAN_FC), "payload")["event_id"] = "é" * 65
    _expect(CollectorAdmissionReason.STRING_LIMIT, _bundle(lines))


def test_json_digit_limit_value_error_is_a_sanitised_json_refusal() -> None:
    """A lowered interpreter digit guard cannot leak a raw parser ValueError."""

    previous = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(640)
        _expect(CollectorAdmissionReason.JSON_INVALID, b'{"x":' + b"1" * 700 + b"}\n")
    finally:
        sys.set_int_max_str_digits(previous)


def test_exact_resource_limits_reach_later_validation_and_above_limits_refuse() -> None:
    """Exact byte, record, and per-stream bounds admit before their next validation stage."""

    _expect(
        CollectorAdmissionReason.LINE_LIMIT,
        b" " * (bundle_reader.MAX_BUNDLE_BYTES - 1) + b"\n",
    )
    _expect(CollectorAdmissionReason.BUNDLE_SIZE, b"x" * (bundle_reader.MAX_BUNDLE_BYTES + 1))
    _expect(CollectorAdmissionReason.HEADER_INVALID, b"{}\n" * (bundle_reader.MAX_RECORDS + 1))

    lines = _canonical_lines()
    header = lines[0]
    header["inventory"] = {stream.value: 0 for stream in CollectorStream}
    header["inventory"][CollectorStream.HUMAN_FC.value] = bundle_reader.MAX_STREAM_RECORDS
    header["inventory"][CollectorStream.ENDPOINT.value] = bundle_reader.MAX_STREAM_RECORDS
    human = _stream(lines, CollectorStream.HUMAN_FC)
    endpoint = _stream(lines, CollectorStream.ENDPOINT)
    bounded = [header]
    for sequence in range(bundle_reader.MAX_STREAM_RECORDS):
        human_row = human.copy()
        human_row["seq"] = sequence
        endpoint_row = endpoint.copy()
        endpoint_row["seq"] = sequence
        bounded.extend((human_row, endpoint_row))
    result = _read(bounded)
    assert sum(len(entry.records) for entry in result.streams) == bundle_reader.MAX_RECORDS
    assert all(
        len(entry.records) == bundle_reader.MAX_STREAM_RECORDS
        for entry in result.streams
        if entry.stream in {CollectorStream.HUMAN_FC, CollectorStream.ENDPOINT}
    )

    lines = _canonical_lines()
    _object_field(lines[0], "inventory")[CollectorStream.HUMAN_FC.value] = (
        bundle_reader.MAX_STREAM_RECORDS + 1
    )
    _expect(CollectorAdmissionReason.HEADER_INVALID, _bundle(lines))


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


@pytest.mark.parametrize(
    ("stream", "field", "value"),
    [
        (CollectorStream.IDENTITY, "agent_sha256", "not-a-digest"),
        (CollectorStream.SHADOW_WINDOW, "status", "unknown"),
        (CollectorStream.INFERENCE_CONFIG, "window_seconds", 0),
        (CollectorStream.RECORDING_SEGMENT, "sample_count", 0),
        (CollectorStream.PERSISTENCE_ACK, "status", "bad"),
        (CollectorStream.OPERATOR_OBSERVATION, "effect", "bad"),
        (CollectorStream.ENDPOINT, "outcome", "bad"),
    ],
)
def test_additional_stream_payload_guards_are_isolated(
    stream: CollectorStream, field: str, value: object
) -> None:
    """Each remaining stream-local guard refuses only its own stream."""

    lines = _canonical_lines()
    _object_field(_stream(lines, stream), "payload")[field] = value
    result = _read(lines)
    entry = next(item for item in result.streams if item.stream is stream)
    assert entry.state is CollectorStreamState.MALFORMED
    assert entry.reason is CollectorStreamReason.PAYLOAD_INVALID


def test_header_and_envelope_container_guards_are_typed_refusals() -> None:
    """Container type and key faults do not escape as raw parser exceptions."""

    lines = _canonical_lines()
    lines[0]["inventory"] = []
    _expect(CollectorAdmissionReason.HEADER_INVALID, _bundle(lines))

    lines = _canonical_lines()
    _object_field(lines[0], "inventory")["identity"] = True
    _expect(CollectorAdmissionReason.HEADER_INVALID, _bundle(lines))

    lines = _canonical_lines()
    lines[0]["alignments"] = [{"source_clock": "agent_monotonic_seconds"}]
    _expect(CollectorAdmissionReason.HEADER_INVALID, _bundle(lines))

    lines = _canonical_lines()
    _stream(lines, CollectorStream.HUMAN_FC).pop("seq")
    _expect(CollectorAdmissionReason.ENVELOPE_INVALID, _bundle(lines))


def test_decoder_and_post_decode_defence_guards_are_closed() -> None:
    """Invalid byte decoding and redundant post-decode bounds remain fail-closed."""

    _expect(CollectorAdmissionReason.FRAMING, b"\xff\n")
    walk_json = typing.cast(
        typing.Callable[[object, int], CollectorAdmissionReason | None],
        bundle_reader._walk_json,  # pyright: ignore[reportPrivateUsage]
    )
    assert walk_json(float("inf"), 1) is CollectorAdmissionReason.JSON_INVALID
    assert walk_json({"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}, 2) is (
        CollectorAdmissionReason.NESTING_LIMIT
    )
    assert walk_json([[[[[[1]]]]]], 2) is CollectorAdmissionReason.NESTING_LIMIT


@pytest.mark.parametrize(
    ("stream", "field", "value"),
    [
        (CollectorStream.HUMAN_FC, "accepted_at", []),
        (CollectorStream.HUMAN_FC, "accepted_at", {"clock": "unknown", "value": 1}),
        (CollectorStream.HUMAN_FC, "accepted_at", {"clock": "recording_samples", "value": 1.5}),
    ],
)
def test_instant_guards_are_stream_local(
    stream: CollectorStream, field: str, value: object
) -> None:
    """Nested instant type, vocabulary, and sample-integrality faults are isolated."""

    lines = _canonical_lines()
    _object_field(_stream(lines, stream), "payload")[field] = value
    result = _read(lines)
    entry = next(item for item in result.streams if item.stream is stream)
    assert entry.state is CollectorStreamState.MALFORMED
    assert entry.reason is CollectorStreamReason.PAYLOAD_INVALID


def test_negative_instant_and_shadow_probability_combinations_are_isolated() -> None:
    """Nested numeric and nullable shadow grammar faults remain stream-local."""

    lines = _canonical_lines()
    _object_field(_stream(lines, CollectorStream.HUMAN_FC), "payload")["accepted_at"] = {
        "clock": "agent_monotonic_seconds",
        "value": -1,
    }
    result = _read(lines)
    human = next(entry for entry in result.streams if entry.stream is CollectorStream.HUMAN_FC)
    assert human.state is CollectorStreamState.MALFORMED

    for status, probability in (("not_inferred", 0.1), ("confirmed", None)):
        lines = _canonical_lines()
        payload = _object_field(_stream(lines, CollectorStream.SHADOW_WINDOW), "payload")
        payload.update(eligibility="eligible", status=status, probability=probability)
        result = _read(lines)
        shadow = next(
            entry for entry in result.streams if entry.stream is CollectorStream.SHADOW_WINDOW
        )
        assert shadow.state is CollectorStreamState.MALFORMED


def test_file_start_bom_and_later_bom_have_distinct_staged_refusals() -> None:
    """Only a file-start BOM is framing; a later prefix fails JSON parsing."""

    _expect(CollectorAdmissionReason.FRAMING, b"\xef\xbb\xbf{}\n")
    _expect(CollectorAdmissionReason.JSON_INVALID, b"{}\n\xef\xbb\xbf{}\n")


def test_empty_bytes_and_leading_lf_are_framing_refusals() -> None:
    """No empty or blank-framed input reaches later validation stages."""

    _expect(CollectorAdmissionReason.FRAMING, b"")
    _expect(CollectorAdmissionReason.FRAMING, b"\n{}\n")


def test_non_object_payload_and_later_rows_do_not_restore_a_malformed_stream() -> None:
    """A malformed stream stays empty even when later structural rows are valid."""

    lines = _canonical_lines()
    shadow = _stream(lines, CollectorStream.SHADOW_EVENT)
    shadow["payload"] = []
    later = _stream(_canonical_lines(), CollectorStream.SHADOW_EVENT)
    later["seq"] = 1
    lines.append(later)
    lines.append(later.copy())
    _object_field(lines[0], "inventory")["shadow_event"] = 3
    result = _read(lines)
    entry = next(item for item in result.streams if item.stream is CollectorStream.SHADOW_EVENT)
    assert entry.state is CollectorStreamState.MALFORMED
    assert entry.reason is CollectorStreamReason.PAYLOAD_INVALID
    assert entry.records == ()


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


def test_unknown_versions_stream_and_session_sequence_rules_are_whole_bundle_refusals() -> None:
    """Version, stream, session, and structural sequence-envelope grammar is closed."""

    lines = _canonical_lines()
    lines[0]["schema_version"] = 2
    _expect(CollectorAdmissionReason.HEADER_INVALID, _bundle(lines))

    lines = _canonical_lines()
    _stream(lines, CollectorStream.HUMAN_FC)["schema_version"] = 2
    _expect(CollectorAdmissionReason.ENVELOPE_INVALID, _bundle(lines))

    lines = _canonical_lines()
    _stream(lines, CollectorStream.HUMAN_FC)["stream"] = "unknown"
    _expect(CollectorAdmissionReason.ENVELOPE_INVALID, _bundle(lines))


def test_decreasing_sequence_isolated_without_erasing_other_streams() -> None:
    """A decreasing sequence marks only its stream malformed."""

    lines = _canonical_lines()
    _stream(lines, CollectorStream.HUMAN_FC)["seq"] = 1
    duplicate = _stream(lines, CollectorStream.HUMAN_FC).copy()
    duplicate["seq"] = 0
    lines.append(duplicate)
    _object_field(lines[0], "inventory")[CollectorStream.HUMAN_FC.value] = 2
    result = _read(lines)
    human = next(entry for entry in result.streams if entry.stream is CollectorStream.HUMAN_FC)
    shadow = next(entry for entry in result.streams if entry.stream is CollectorStream.SHADOW_EVENT)
    assert human.state is CollectorStreamState.MALFORMED
    assert human.reason is CollectorStreamReason.SEQUENCE_CONFLICT
    assert shadow.state is CollectorStreamState.ADMITTED


def test_sequence_bounds_reject_bool_negative_and_above_wire_maximum() -> None:
    """Envelope and payload count fields use the exact non-boolean wire integer range."""

    for sequence in (True, -1, 2**53):
        lines = _canonical_lines()
        _stream(lines, CollectorStream.HUMAN_FC)["seq"] = sequence
        _expect(CollectorAdmissionReason.ENVELOPE_INVALID, _bundle(lines))

    lines = _canonical_lines()
    _stream(lines, CollectorStream.HUMAN_FC)["seq"] = (2**53) - 1
    result = _read(lines)
    human = next(entry for entry in result.streams if entry.stream is CollectorStream.HUMAN_FC)
    assert human.records[0].seq == (2**53) - 1

    for count in ((2**53) - 1, 2**53):
        lines = _canonical_lines()
        _object_field(_stream(lines, CollectorStream.SHADOW_EVENT), "payload")[
            "confirming_window_seq"
        ] = count
        result = _read(lines)
        shadow = next(
            entry for entry in result.streams if entry.stream is CollectorStream.SHADOW_EVENT
        )
        assert (shadow.state is CollectorStreamState.ADMITTED) is (count == (2**53) - 1)


def test_temperature_room_and_command_nullable_status_rules_are_exact() -> None:
    """Nullable status fields accept only their ratified valid combinations."""

    lines = _canonical_lines()
    temperature = _object_field(_stream(lines, CollectorStream.TEMPERATURE_TURN), "payload")
    temperature.update(status="accepted", bt_c=None, et_c=None)
    room = _object_field(_stream(lines, CollectorStream.ROOM_READING), "payload")
    room.update(status="valid", temperature_c=None)
    command = _object_field(_stream(lines, CollectorStream.COMMAND), "payload")
    command.update(disposition="unknown", commanded_heat_percent=10)
    result = _read(lines)
    for stream in (
        CollectorStream.TEMPERATURE_TURN,
        CollectorStream.ROOM_READING,
        CollectorStream.COMMAND,
    ):
        entry = next(item for item in result.streams if item.stream is stream)
        assert entry.state is CollectorStreamState.MALFORMED
        assert entry.reason is CollectorStreamReason.PAYLOAD_INVALID

    lines = _canonical_lines()
    temperature = _object_field(_stream(lines, CollectorStream.TEMPERATURE_TURN), "payload")
    temperature.update(status="invalid", bt_c=1, et_c=2)
    result = _read(lines)
    entry = next(item for item in result.streams if item.stream is CollectorStream.TEMPERATURE_TURN)
    assert entry.state is CollectorStreamState.MALFORMED

    lines = _canonical_lines()
    temperature = _object_field(_stream(lines, CollectorStream.TEMPERATURE_TURN), "payload")
    temperature.update(status="invalid", bt_c=None, et_c=None)
    room = _object_field(_stream(lines, CollectorStream.ROOM_READING), "payload")
    room.update(status="invalid", temperature_c=None)
    result = _read(lines)
    assert all(
        next(item for item in result.streams if item.stream is stream).state
        is CollectorStreamState.ADMITTED
        for stream in (CollectorStream.TEMPERATURE_TURN, CollectorStream.ROOM_READING)
    )


def test_token_probability_and_heat_boundaries_are_exact() -> None:
    """Closed field bounds admit endpoints and reject only values outside them."""

    for identifier in ("x" * 64, "x" * 65):
        lines = _canonical_lines()
        _object_field(_stream(lines, CollectorStream.HUMAN_FC), "payload")["event_id"] = identifier
        result = _read(lines)
        human = next(entry for entry in result.streams if entry.stream is CollectorStream.HUMAN_FC)
        assert (human.state is CollectorStreamState.ADMITTED) is (len(identifier) == 64)

    for probability in (0, 1, 1.01):
        lines = _canonical_lines()
        _object_field(_stream(lines, CollectorStream.SHADOW_WINDOW), "payload")["probability"] = (
            probability
        )
        result = _read(lines)
        shadow = next(
            entry for entry in result.streams if entry.stream is CollectorStream.SHADOW_WINDOW
        )
        assert (shadow.state is CollectorStreamState.ADMITTED) is (probability in {0, 1})

    for heat in (0, 100, 100.01):
        lines = _canonical_lines()
        command_payload = _object_field(_stream(lines, CollectorStream.COMMAND), "payload")
        command_payload["commanded_heat_percent"] = heat
        result = _read(lines)
        command = next(entry for entry in result.streams if entry.stream is CollectorStream.COMMAND)
        assert (command.state is CollectorStreamState.ADMITTED) is (heat in {0, 100})

    lines = _canonical_lines()
    command_payload = _object_field(_stream(lines, CollectorStream.COMMAND), "payload")
    command_payload["commanded_heat_percent"] = None
    result = _read(lines)
    command = next(entry for entry in result.streams if entry.stream is CollectorStream.COMMAND)
    assert command.state is CollectorStreamState.MALFORMED


def test_anchor_pair_limit_admits_six_distinct_unordered_pairs_only() -> None:
    """Each unordered clock pair can occur once and at most six anchors are admitted."""

    clocks = [
        "agent_monotonic_seconds",
        "mcp_session_seconds",
        "recording_samples",
        "utc_epoch_seconds",
    ]
    pairs = [(clocks[left], clocks[right]) for left in range(4) for right in range(left + 1, 4)]
    lines = _canonical_lines()
    lines[0]["alignments"] = [
        {
            "source_clock": source,
            "source_value": 1 if source == "recording_samples" else 1.0,
            "target_clock": target,
            "target_value": 2 if target == "recording_samples" else 2.0,
        }
        for source, target in pairs
    ]
    assert _read(lines).header.alignments

    lines[0]["alignments"].append(lines[0]["alignments"][0].copy())
    _expect(CollectorAdmissionReason.HEADER_INVALID, _bundle(lines))


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


def test_large_finite_integer_measurements_are_admitted_without_float_coercion() -> None:
    """Finite JSON integer measurements retain their exact integer value."""

    lines = _canonical_lines()
    large_integer = 10**400
    temperature_payload = _object_field(_stream(lines, CollectorStream.TEMPERATURE_TURN), "payload")
    temperature_payload["bt_c"] = large_integer
    temperature_payload["et_c"] = large_integer
    room_payload = _object_field(_stream(lines, CollectorStream.ROOM_READING), "payload")
    room_payload["temperature_c"] = large_integer
    result = _read(lines)

    temperature = next(
        entry for entry in result.streams if entry.stream is CollectorStream.TEMPERATURE_TURN
    )
    room = next(entry for entry in result.streams if entry.stream is CollectorStream.ROOM_READING)
    assert temperature.state is CollectorStreamState.ADMITTED
    assert room.state is CollectorStreamState.ADMITTED
    typed_temperature = temperature.records[0].payload
    typed_room = room.records[0].payload
    assert isinstance(typed_temperature, TemperatureTurnPayload)
    assert isinstance(typed_room, RoomReadingPayload)
    assert typed_temperature.bt_c == large_integer
    assert typed_temperature.et_c == large_integer
    assert typed_room.temperature_c == large_integer
    assert type(typed_temperature.bt_c) is int
    assert type(typed_temperature.et_c) is int
    assert type(typed_room.temperature_c) is int


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


def _boundary_violations(source: str) -> set[str]:
    """Return forbidden import, file-I/O, and public-entrypoint boundary violations."""

    tree = ast.parse(source)
    allowed_stdlib = {"contextlib", "enum", "hashlib", "json", "math", "re", "typing"}
    violations: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in allowed_stdlib and root != "pydantic":
                    violations.add(f"import:{alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module
            root = module.split(".")[0] if module is not None else ""
            invalid_relative = node.level > 1 or (
                node.level == 1 and module not in {"bundle_reader", "bundle_schema"}
            )
            invalid_absolute = module is not None and (
                node.level == 0
                and root not in allowed_stdlib
                and root != "pydantic"
                and not module.startswith("roastpilot_agent.diagnostic_collector")
            )
            if invalid_relative or invalid_absolute:
                violations.add(f"from:{module}")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in {
                "open",
                "__import__",
                "eval",
                "exec",
            }:
                violations.add(node.func.id)
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "builtins"
                and node.func.attr == "open"
            ):
                violations.add("open")
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "json"
                and node.func.attr in {"load", "dump"}
            ):
                violations.add(f"json.{node.func.attr}")
    return violations


def test_collector_module_boundary_rejects_import_io_and_entrypoint_drift() -> None:
    """All collector modules stay stdlib/Pydantic-only, with one reader entrypoint."""

    modules = (collector_package, bundle_schema, bundle_reader)
    for module in modules:
        assert module.__file__ is not None
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert _boundary_violations(source) == set()
        assert _boundary_violations(source + "\nimport roastpilot_agent.controller\n")
        assert _boundary_violations(source + "\nimport roastpilot_agent.safety\n")
        assert _boundary_violations(source + "\nimport roastpilot_agent.mcp_client\n")
        assert _boundary_violations(source + "\nopen('forbidden')\n")
        assert _boundary_violations(source + "\n__import__('roastpilot_agent.controller')\n")
        assert _boundary_violations(source + "\neval('1')\n")
        assert _boundary_violations(source + "\nexec('pass')\n")
        assert _boundary_violations(source + "\njson.load(None)\n")
        assert _boundary_violations(source + "\njson.dump({}, None)\n")
        assert _boundary_violations(source + "\nfrom .. import store\n")
        assert _boundary_violations(source + "\nfrom .bundle_schema import BundleHeader\n") == set()

    reader_tree = ast.parse(Path(bundle_reader.__file__).read_text(encoding="utf-8"))
    public_functions = [
        node.name
        for node in reader_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    ]
    assert public_functions == ["read_synthetic_bundle"]
    assert collector_package.__all__[-1] == "read_synthetic_bundle"


def test_fresh_collector_import_loads_no_runtime_or_control_modules() -> None:
    """A fresh interpreter imports the boundary without runtime/control dependencies."""

    program = (
        "import sys\n"
        "import roastpilot_agent.diagnostic_collector\n"
        "blocked = {'controller', 'safety', 'mcp_client', 'live', 'mcp'}\n"
        "loaded = {name.rsplit('.', 1)[-1] for name in sys.modules}\n"
        "raise SystemExit(bool(loaded & blocked))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", program],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": os.defpath},
    )
    assert completed.returncode == 0
