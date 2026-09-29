"""Behavioural and fail-closed tests for the versioned strict evidence reader."""

import hashlib
import json
import typing
from pathlib import Path

import pytest

from roastpilot_agent.cold_characterisation import evidence_builders as builders
from roastpilot_agent.cold_characterisation import evidence_reader as reader
from roastpilot_agent.cold_characterisation import evidence_schema as schema
from roastpilot_agent.cold_characterisation import evidence_store as store
from roastpilot_agent.cold_characterisation.identity import (
    AgentBuildProvenance,
    ColdRunIdentity,
    EffectiveMCPProfile,
    ManagedDeviceIdentity,
    ModelManifestEntry,
)
from roastpilot_agent.cold_characterisation.mcp import ColdRoastFanOutcome
from roastpilot_agent.mcp_client import RuntimeConfigSnapshot, ServerInfo
from tests.test_cold_characterisation_evidence_builders import (
    RUN_ID,
    abort_for,
    device_state,
    header_for,
    make_identity,
    observation,
    roast_fan_state,
    tick_for,
)
from tests.test_cold_characterisation_evidence_store import (
    OFF,
    ON,
    SECRET,
    Failure,
    craft_manifest,
    expect,
    open_writer,
    run_dir,
    write_full_run,
)

HEADER_OFF = "records/recording_off/header.jsonl"
TICK_OFF = "records/recording_off/tick.jsonl"
HOST_OFF = "records/recording_off/host.jsonl"
TICK_ON = "records/recording_on/tick.jsonl"


LineMaker = typing.Callable[[str], bytes]


def line_maker(function: LineMaker) -> LineMaker:
    """Type one parametrized crafted-line factory."""
    return function


def read(root: str, digest: str) -> reader.ColdRetainedRun:
    """Read the shared test run from a root."""
    return reader.read_retained_run(root, run_id=RUN_ID, expected_manifest_sha256=digest)


def rewrite(root: str, relative_path: str, data: bytes) -> str:
    """Replace one retained file's bytes and re-seal the manifest pair; return its digest."""
    path = run_dir(root) / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return craft_manifest(run_dir(root))


def first_line(root: str, relative_path: str) -> dict[str, typing.Any]:
    """Return the first decoded line of one retained stream file."""
    return json.loads((run_dir(root) / relative_path).read_bytes().split(b"\n")[0])


def line_of(document: object) -> bytes:
    """Return one canonical LF-terminated record line."""
    return store.canonical_json(document).encode() + b"\n"


def envelope_of(document: object) -> schema.ColdSealedEnvelope:
    """Build a canonical identity envelope over one JSON document."""
    canonical = store.canonical_json(document)
    return schema.ColdSealedEnvelope(
        kind=schema.ColdEnvelopeKind.IDENTITY,
        schema_version=1,
        canonical_json=canonical,
        canonical_byte_length=len(canonical.encode()),
        sha256=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def identity_document(tmp_path: Path, root: str = "/synthetic/pi") -> dict[str, typing.Any]:
    """Return one v1 identity document as the writer emits it."""
    return make_identity(tmp_path, pi_root=root).model_dump(mode="json")


# ------------------------------------------------------------------ round trip


def test_all_six_record_kinds_round_trip(tmp_path: Path) -> None:
    """Writer, seal, then read returns records equal to the originals in file order."""
    root, sealed, records = write_full_run(tmp_path)
    retained = read(root, sealed.manifest_sha256)
    assert retained.run_id == RUN_ID
    assert retained.manifest_sha256 == sealed.manifest_sha256
    by_stream = {(item.phase, item.stream): list(item.records) for item in retained.streams}
    stream = schema.ColdEvidenceStream
    assert by_stream == {
        (OFF, stream.HEADER): [records[0]],
        (OFF, stream.TICK): records[1:3],
        (OFF, stream.HOST): [records[3]],
        (OFF, stream.ADVISORY): [records[4]],
        (ON, stream.HEADER): [records[5]],
        (ON, stream.TICK): [records[6]],
        (ON, stream.HOST): [records[7]],
        (ON, stream.FINALISATION): [records[8]],
        (ON, stream.ABORT): [records[9]],
    }
    assert [item.header for item in retained.headers] == [records[0], records[5]]
    assert retained.headers[0].identity.pi_evidence_root == root
    assert not any(
        "outcome" in name or "verdict" in name for name in reader.ColdRetainedRun.model_fields
    )


def test_t_e7_every_roast_fan_outcome_and_absent_device_round_trip(tmp_path: Path) -> None:
    """T-E2/T-E7: nested device, ``device=None``, and all five outcomes survive losslessly."""
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    observations = [
        observation(
            device_state(),
            roast_fan=roast_fan_state(
                outcome, 0 if outcome is ColdRoastFanOutcome.OBSERVED else None
            ),
        )
        for outcome in ColdRoastFanOutcome
    ]
    observations.append(
        observation(None, roast_fan=roast_fan_state(ColdRoastFanOutcome.UNREADABLE, None))
    )
    observations.append(
        observation(
            device_state(heat_level_percent=150, fan_level_percent=-1, cooling_on=True),
            roast_fan=roast_fan_state(ColdRoastFanOutcome.OBSERVED, 100),
        )
    )
    ticks: list[schema.ColdEvidenceRecord] = [
        builders.build_tick_record(
            header=off,
            tick=index,
            recorded_at_utc="2026-09-26T12:00:01Z",
            monotonic_seconds=2.0 + index,
            observation=source,
        )
        for index, source in enumerate(observations)
    ]
    for record in (off, *ticks):
        writer.append(record)
    sealed = writer.seal()
    retained = read(root, sealed.manifest_sha256)
    (tick_stream,) = (
        item for item in retained.streams if item.stream is schema.ColdEvidenceStream.TICK
    )
    assert list(tick_stream.records) == ticks
    for original, decoded in zip(ticks, tick_stream.records, strict=True):
        assert store.canonical_json(decoded.model_dump(mode="json")) == store.canonical_json(
            original.model_dump(mode="json")
        )
    decoded_ticks = typing.cast(tuple[schema.ColdTickRecord, ...], tick_stream.records)
    assert [tick.roast_fan.outcome for tick in decoded_ticks[:5]] == list(
        schema.ColdTickRoastFanOutcome
    )
    assert decoded_ticks[5].device is None
    lines = (run_dir(root) / TICK_OFF).read_bytes().split(b"\n")
    assert json.loads(lines[5])["device"] is None
    assert b'"device":null' in lines[5]
    unsafe = decoded_ticks[6].device
    assert unsafe is not None
    assert (unsafe.heat_level_percent, unsafe.fan_level_percent, unsafe.cooling_on) == (
        150,
        -1,
        True,
    )


def test_every_legal_abort_pair_round_trips(tmp_path: Path) -> None:
    """Abort records decode exactly by domain for every legal domain/reason pair."""
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    writer.append(off)
    aborts = [
        abort_for(off, domain, reason)
        for domain, reasons in reader.ABORT_REASON_BY_DOMAIN.items()
        for reason in reasons
    ]
    for record in aborts:
        writer.append(record)
    sealed = writer.seal()
    retained = read(root, sealed.manifest_sha256)
    assert list(retained.streams[-1].records) == aborts
    assert {record.domain for record in aborts} == set(schema.ColdAbortDomain)


def test_reader_pairing_table_equals_the_schema_validator() -> None:
    """The reader-local pairing table is exactly ``ColdAbortRecord``'s pairing."""
    for domain in schema.ColdAbortDomain:
        for reason_type in set(reader.ABORT_REASON_BY_DOMAIN.values()):
            member = next(iter(reason_type))
            try:
                abort_for(
                    schema.ColdRunHeader.model_construct(
                        run_id=RUN_ID, phase=OFF, identity_sha256="a" * 64
                    ),
                    domain,
                    member,
                )
            except schema.ColdEvidenceError:
                accepted = False
            else:
                accepted = True
            assert accepted is (reader.ABORT_REASON_BY_DOMAIN[domain] is reason_type)


@pytest.mark.parametrize(
    "update",
    [
        {"domain": "host", "reason": "operator_stop"},
        {"domain": "weather", "reason": "operator_stop"},
        {"reason": 1},
        {"phase": "recording_sideways"},
        {"reason": None},
    ],
)
def test_abort_decoding_refuses_cross_domain_and_raw_values(
    tmp_path: Path, update: dict[str, object]
) -> None:
    """Cross-domain, unknown, and raw non-enum abort values refuse."""
    root, _sealed, _records = write_full_run(tmp_path)
    path = "records/recording_on/abort.jsonl"
    digest = rewrite(root, path, line_of({**first_line(root, path), **update}))
    expect(Failure.LINE_MALFORMED, lambda: read(root, digest))


def test_abort_with_an_extra_field_refuses_strictly(tmp_path: Path) -> None:
    """Strictness is preserved for every non-enum abort field."""
    root, _sealed, _records = write_full_run(tmp_path)
    path = "records/recording_on/abort.jsonl"
    digest = rewrite(root, path, line_of({**first_line(root, path), "extra": 1}))
    expect(Failure.LINE_MALFORMED, lambda: read(root, digest))


# -------------------------------------------------------------- line hardening


def _tick_line(root: str, **update: object) -> bytes:
    """Return the first OFF tick as a canonical line after one field update."""
    return line_of({**first_line(root, TICK_OFF), **update})


def _tick_device_document(root: str, **device_update: object) -> dict[str, typing.Any]:
    """Return the first OFF tick with fields merged into its nested ``device`` object."""
    document = first_line(root, TICK_OFF)
    assert type(document["device"]) is dict
    return {**document, "device": {**document["device"], **device_update}}


def _tick_device_line(root: str, **device_update: object) -> bytes:
    """Return the first OFF tick as a canonical line after one nested device update."""
    return line_of(_tick_device_document(root, **device_update))


def test_nested_device_forge_keeps_the_exact_top_level_key_set(tmp_path: Path) -> None:
    """T-E16: device negatives never rely on an unknown top-level tick key."""
    root, _sealed, _records = write_full_run(tmp_path)
    for update in ({"cooling_on": 1}, {"raw_vendor_data": {SECRET: SECRET}}):
        document = _tick_device_document(root, **update)
        assert set(document) == set(schema.ColdTickRecord.model_fields)
        assert set(document["device"]) == set(schema.ColdTickDeviceEvidence.model_fields)


def _replaced(old: bytes, new: bytes) -> LineMaker:
    """Return a factory replacing literal bytes inside the canonical first tick."""

    def make(root: str) -> bytes:
        line = _tick_line(root)
        assert old in line
        return line.replace(old, new)

    return make


@pytest.mark.parametrize(
    ("make", "failure"),
    [
        (line_maker(lambda r: _tick_line(r, tick="1")), Failure.LINE_MALFORMED),
        (line_maker(lambda r: _tick_device_line(r, cooling_on=1)), Failure.LINE_MALFORMED),
        (
            line_maker(lambda r: _tick_device_line(r, bean_temp_c=20)),
            Failure.LINE_NOT_CANONICAL,
        ),
        (line_maker(lambda r: _tick_line(r, monotonic_seconds=2)), Failure.LINE_NOT_CANONICAL),
        (line_maker(lambda r: _tick_line(r, schema_version=2)), Failure.SCHEMA_VERSION_UNKNOWN),
        (line_maker(lambda r: _tick_line(r, schema_version=True)), Failure.SCHEMA_VERSION_UNKNOWN),
        (line_maker(lambda r: _tick_line(r, stream="host")), Failure.LINE_MALFORMED),
        (line_maker(lambda r: _tick_line(r, stream=None)), Failure.LINE_MALFORMED),
        (line_maker(lambda r: _tick_line(r, phase="recording_on")), Failure.LINE_MALFORMED),
        (_replaced(b'"tick":0', b'"tick":0,"tick":0'), Failure.JSON_DUPLICATE_KEY),
        (
            _replaced(b'"monotonic_seconds":2.0', b'"monotonic_seconds":NaN'),
            Failure.JSON_NOT_FINITE,
        ),
        (
            _replaced(b'"monotonic_seconds":2.0', b'"monotonic_seconds":Infinity'),
            Failure.JSON_NOT_FINITE,
        ),
        (line_maker(lambda r: _tick_line(r).replace(b",", b", ")), Failure.LINE_NOT_CANONICAL),
        (line_maker(lambda r: _tick_line(r)[:-1]), Failure.LINE_MALFORMED),
        (line_maker(lambda r: _tick_line(r) + b"\n"), Failure.LINE_MALFORMED),
        (line_maker(lambda _r: b""), Failure.LINE_MALFORMED),
        (line_maker(lambda _r: b"[1]\n"), Failure.LINE_MALFORMED),
        (line_maker(lambda _r: b"\xff\n"), Failure.LINE_MALFORMED),
        (line_maker(lambda _r: b"{\n"), Failure.LINE_MALFORMED),
    ],
    ids=[
        "string-for-int",
        "int-for-bool",
        "device-int-for-float",
        "int-for-float",
        "version-2",
        "version-bool",
        "stream-mismatch",
        "stream-null",
        "phase-mismatch",
        "duplicate-key",
        "nan",
        "infinity",
        "whitespace",
        "torn",
        "empty-line",
        "empty-file",
        "not-object",
        "utf8",
        "syntax",
    ],
)
def test_line_hardening_refuses(
    tmp_path: Path, make: typing.Callable[[str], bytes], failure: store.ColdEvidenceStoreFailure
) -> None:
    """Every malformed, coerced, non-canonical, or torn line refuses."""
    root, _sealed, _records = write_full_run(tmp_path)
    digest = rewrite(root, TICK_OFF, make(root))
    expect(failure, lambda: read(root, digest))


def test_missing_schema_version_is_unknown(tmp_path: Path) -> None:
    """A line without a version is never read as version 1."""
    root, _sealed, _records = write_full_run(tmp_path)
    document = first_line(root, TICK_OFF)
    del document["schema_version"]
    digest = rewrite(root, TICK_OFF, line_of(document))
    expect(Failure.SCHEMA_VERSION_UNKNOWN, lambda: read(root, digest))


def test_oversized_line_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A line above the line bound refuses before it is parsed."""
    root, sealed, _records = write_full_run(tmp_path)
    monkeypatch.setattr(reader, "MAX_LINE_BYTES", 100)
    expect(Failure.LINE_TOO_LARGE, lambda: read(root, sealed.manifest_sha256))


def test_line_bound_is_the_record_bound_plus_lf() -> None:
    """The line bound admits exactly one maximal record plus its LF."""
    assert reader.MAX_LINE_BYTES == schema.MAX_RECORD_BYTES + 1


def test_walker_bounds_apply_to_decoded_lines(tmp_path: Path) -> None:
    """Decoded lines pass the schema's JSON walker before any model validation."""
    root, _sealed, _records = write_full_run(tmp_path)
    nested: object = 1
    for _ in range(schema.MAX_JSON_DEPTH + 2):
        nested = [nested]
    digest = rewrite(root, TICK_OFF, _tick_device_line(root, raw_vendor_data={"deep": nested}))
    with pytest.raises(schema.ColdEvidenceError) as raised:
        read(root, digest)
    assert raised.value.failure is schema.ColdEvidenceFailure.JSON_DEPTH_EXCEEDED


@pytest.mark.parametrize(
    "relative_path",
    [
        "records/recording_off/other.jsonl",
        "records/loose.jsonl",
        "records/recording_sideways/tick.jsonl",
        "records/recording_off/tick.jsonl.bak",
    ],
)
def test_unknown_record_files_refuse(tmp_path: Path, relative_path: str) -> None:
    """Every ``records/`` entry must be exactly ``records/<phase>/<stream>.jsonl``."""
    root, _sealed, _records = write_full_run(tmp_path)
    digest = rewrite(root, relative_path, b"{}\n")
    expect(Failure.ENTRY_PATH_INVALID, lambda: read(root, digest))


def test_non_record_artefacts_are_verified_but_not_parsed(tmp_path: Path) -> None:
    """Slice-4 artefacts outside ``records/`` are integrity-checked, never decoded."""
    root, _sealed, _records = write_full_run(tmp_path)
    digest = rewrite(root, "artefacts/primary.bin", b"\x00\xff not json")
    retained = read(root, digest)
    assert len(retained.streams) == 9


def test_header_missing_duplicated_or_unbound_refuses(tmp_path: Path) -> None:
    """Headers must exist once per phase and match the manifest bindings."""
    root, _sealed, _records = write_full_run(tmp_path)
    (run_dir(root) / "records/recording_on/header.jsonl").unlink()
    expect(Failure.HEADER_MISSING, lambda: read(root, craft_manifest(run_dir(root))))

    root, _sealed, _records = write_full_run(tmp_path / "duplicate")
    header = (run_dir(root) / HEADER_OFF).read_bytes()
    digest = rewrite(root, HEADER_OFF, header + header)
    expect(Failure.HEADER_DUPLICATED, lambda: read(root, digest))

    root, _sealed, _records = write_full_run(tmp_path / "unbound")
    digest = craft_manifest(
        run_dir(root),
        transform=lambda doc: {**doc, "identity_bindings": doc["identity_bindings"][:1]},
    )
    expect(Failure.HEADER_BINDING_MISMATCHED, lambda: read(root, digest))


def test_unverified_bytes_are_never_parsed(tmp_path: Path) -> None:
    """A tampered stream fails verification before any line is decoded."""
    root, sealed, _records = write_full_run(tmp_path)
    (run_dir(root) / TICK_OFF).write_bytes(b"not json at all\n")
    with pytest.raises(store.ColdEvidenceStoreError) as raised:
        read(root, sealed.manifest_sha256)
    assert raised.value.failure is Failure.FILE_DIGEST_MISMATCHED


def test_reader_errors_carry_no_line_content(tmp_path: Path) -> None:
    """A credential-shaped value in a rejected line reaches no error channel."""
    root, _sealed, _records = write_full_run(tmp_path)
    digest = rewrite(
        root,
        TICK_OFF,
        _tick_device_line(root, raw_vendor_data={SECRET: SECRET}).replace(b":", b": "),
    )
    with pytest.raises(store.ColdEvidenceStoreError) as raised:
        read(root, digest)
    assert raised.value.failure is store.ColdEvidenceStoreFailure.LINE_NOT_CANONICAL
    rendered = f"{raised.value!s}{raised.value!r}{raised.value.args}"
    assert SECRET not in rendered
    assert raised.value.__cause__ is None and raised.value.__context__ is None


# --------------------------------------------------------- historical identity


def test_historical_identity_with_other_constants_is_read(tmp_path: Path) -> None:
    """History that differs from installed constants is recorded, not refused."""
    root = str(tmp_path.resolve())
    document = identity_document(tmp_path, root)
    document["agent_version"] = "0.0.1-historical"
    document["model_revision"] = "0123456789abcdef"
    document["runtime_config"]["future_runtime_key"] = {"nested": [1, 2.5]}
    document["server_info"]["future_server_key"] = "kept"
    envelope = envelope_of(document)
    identity = store.read_identity_v1(envelope)
    assert identity.known["agent_version"] == "0.0.1-historical"
    assert identity.known["model_revision"] == "0123456789abcdef"
    assert identity.runtime_config_extras == {"future_runtime_key": {"nested": [1, 2.5]}}
    assert identity.server_info_extras == {"future_server_key": "kept"}
    reassembled: dict[str, object] = dict(identity.known)
    reassembled["runtime_config"] = {
        **typing.cast(dict[str, object], identity.known["runtime_config"]),
        **identity.runtime_config_extras,
    }
    reassembled["server_info"] = {
        **typing.cast(dict[str, object], identity.known["server_info"]),
        **identity.server_info_extras,
    }
    assert store.canonical_json(reassembled) == envelope.canonical_json
    with pytest.raises(ValueError, match="packaged identity constants"):
        ColdRunIdentity.model_validate(document)


def test_historical_identity_round_trips_through_the_writer_and_reader(tmp_path: Path) -> None:
    """A retained v1 header with historical constants writes, seals, and reads."""
    writer, root = open_writer(tmp_path)
    document = identity_document(tmp_path, root)
    document["agent_version"] = "0.0.1-historical"
    envelope = envelope_of(document)
    header = schema.ColdRunHeader(
        schema_version=1,
        stream="header",
        run_id=RUN_ID,
        phase=OFF,
        recorded_at_utc="2026-09-26T12:00:00Z",
        monotonic_seconds=1.0,
        identity_sha256=envelope.sha256,
        identity=envelope,
    )
    writer.append(header)
    writer.append(abort_for(header))
    sealed = writer.seal()
    retained = read(root, sealed.manifest_sha256)
    assert retained.headers[0].identity.known["agent_version"] == "0.0.1-historical"


def _set(
    path: tuple[str | int, ...], value: object
) -> typing.Callable[[dict[str, typing.Any]], None]:
    """Return a mutation setting one nested document path."""

    def mutate(document: dict[str, typing.Any]) -> None:
        target: typing.Any = document
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


def _delete(path: tuple[str, ...]) -> typing.Callable[[dict[str, typing.Any]], None]:
    """Return a mutation deleting one nested document key."""

    def mutate(document: dict[str, typing.Any]) -> None:
        target: typing.Any = document
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]

    return mutate


def _too_many_extras(document: dict[str, typing.Any]) -> None:
    """Exceed the tolerant-origin extras cap by one."""
    for index in range(store.MAX_IDENTITY_EXTRA_KEYS + 1):
        document["runtime_config"][f"extra_{index}"] = index


@pytest.mark.parametrize(
    "mutate",
    [
        _set(("runtime_config", "allow_manual_override"), "true"),
        _set(("runtime_config", "allow_manual_override"), 1),
        _set(("server_info", "bootstrap_safe"), 1),
        _set(("device_config", "recording_enabled"), "true"),
        _set(("build_provenance", "source_tree_dirty"), 1),
        _set(("runtime_config", "command_interval_seconds"), 1),
        _set(("effective_mcp_profile", "audio_overlap"), 1),
        _set(("effective_mcp_profile", "first_crack_onnx_threads"), 2.0),
        _set(("runtime_config", "roaster_baudrate"), "9600"),
        _set(("runtime_config", "roaster_baudrate"), True),
        _set(("controller_tick_seconds",), 1),
        _set(("credential_present",), 1),
        _set(("server_info", "available_bootstrap_tools"), [1]),
        _delete(("runtime_config", "log_dir")),
        _delete(("device_config", "fc_mode")),
        _set(("device_config", "unexpected"), None),
        _set(("build_provenance", "unexpected"), None),
        _set(("effective_mcp_profile", "unexpected"), 1),
        _set(("model_manifest", 0, "unexpected"), "x"),
        _set(("model_manifest",), {"relative_path": "x", "sha256": "y"}),
        _set(("model_manifest",), []),
        _set(("device_config", "fc_mode"), "loud"),
        _set(("build_provenance", "artefact_kind"), "zip"),
        _set(("build_provenance", "source_revision"), "B" * 40),
        _set(("effective_mcp_profile", "source_sha256"), "a" * 63),
        _delete(("pi_model",)),
        _set(("unexpected_top_level",), "x"),
        _set(("runtime_config",), []),
        _set(("device_config",), "x"),
        _too_many_extras,
    ],
)
def test_identity_v1_refuses_coercion_drift_and_closed_extras(
    tmp_path: Path, mutate: typing.Callable[[dict[str, typing.Any]], None]
) -> None:
    """Exact-type nested checks refuse coercions, missing keys, and closed extras."""
    document = identity_document(tmp_path)
    mutate(document)
    expect(Failure.IDENTITY_NOT_V1, lambda: store.read_identity_v1(envelope_of(document)))


def test_identity_v1_refuses_non_identity_or_unparseable_envelopes(tmp_path: Path) -> None:
    """Only a lossless v1 identity object is read."""
    document = identity_document(tmp_path)
    envelope = envelope_of(document)
    wrong_kind = envelope.model_copy(update={"kind": schema.ColdEnvelopeKind.FINALISATION})
    expect(Failure.IDENTITY_NOT_V1, lambda: store.read_identity_v1(wrong_kind))
    version_two = envelope.model_copy(update={"schema_version": 2})
    expect(Failure.SCHEMA_VERSION_UNKNOWN, lambda: store.read_identity_v1(version_two))
    expect(Failure.IDENTITY_NOT_V1, lambda: store.read_identity_v1(envelope_of([document])))
    duplicated = envelope.model_copy(update={"canonical_json": '{"a":1,"a":1}'})
    expect(Failure.JSON_DUPLICATE_KEY, lambda: store.read_identity_v1(duplicated))
    spaced = envelope.model_copy(
        update={"canonical_json": json.dumps(document, sort_keys=True, ensure_ascii=False)}
    )
    expect(Failure.IDENTITY_NOT_V1, lambda: store.read_identity_v1(spaced))


def test_identity_v1_key_sets_are_pinned_to_their_source_models() -> None:
    """A future identity-field change fails here and forces a deliberate v2."""
    assert set(ColdRunIdentity.model_fields) == store.V1_TOP_LEVEL_KEYS
    assert len(store.V1_TOP_LEVEL_KEYS) == 34
    assert set(store.V1_RUNTIME_CONFIG) == set(RuntimeConfigSnapshot.model_fields)
    assert set(store.V1_SERVER_INFO) == set(ServerInfo.model_fields)
    assert set(store.V1_DEVICE_CONFIG) == set(ManagedDeviceIdentity.model_fields)
    assert set(store.V1_BUILD_PROVENANCE) == set(AgentBuildProvenance.model_fields)
    assert set(store.V1_EFFECTIVE_MCP_PROFILE) == set(EffectiveMCPProfile.model_fields)
    assert set(store.V1_MODEL_MANIFEST_ENTRY) == set(ModelManifestEntry.model_fields)
    assert [len(spec) for spec in (store.V1_RUNTIME_CONFIG, store.V1_SERVER_INFO)] == [14, 10]
    assert [
        len(spec)
        for spec in (
            store.V1_DEVICE_CONFIG,
            store.V1_BUILD_PROVENANCE,
            store.V1_EFFECTIVE_MCP_PROFILE,
        )
    ] == [14, 4, 12]


def test_writer_emitted_identity_reads_back_as_v1(tmp_path: Path) -> None:
    """Everything the current writer emits is a lossless v1 identity."""
    header = header_for(tmp_path, "/synthetic/pi", OFF)
    identity = store.read_identity_v1(header.identity)
    assert identity.run_id == RUN_ID
    assert identity.pi_evidence_root == "/synthetic/pi"
    assert identity.runtime_config_extras == {} and identity.server_info_extras == {}
    assert tick_for(header).identity_sha256 == header.identity_sha256


# ------------------------------------------------ reader-side shared binding


ON_HEADER = "records/recording_on/header.jsonl"
ON_FINALISATION = "records/recording_on/finalisation.jsonl"


def _tampered(root: str, relative_path: str, **update: object) -> str:
    """Replace a stream's first line with a canonical, schema-valid tampered line."""
    lines = (run_dir(root) / relative_path).read_bytes().split(b"\n")
    lines[0] = line_of({**json.loads(lines[0]), **update})[:-1]
    return rewrite(root, relative_path, b"\n".join(lines))


def test_reader_binds_non_header_run_ids(tmp_path: Path) -> None:
    """A valid foreign run id on an OFF tick is refused by the reader's shared binding."""
    root, _sealed, _records = write_full_run(tmp_path)
    digest = _tampered(root, TICK_OFF, run_id="20260926T120000Z-foreign")
    expect(Failure.RUN_ID_MISMATCHED, lambda: read(root, digest))


def test_reader_binds_non_header_phase_digests(tmp_path: Path) -> None:
    """An OFF tick carrying the ON header's identity digest is refused by the reader."""
    root, _sealed, _records = write_full_run(tmp_path)
    on_digest = first_line(root, ON_HEADER)["identity_sha256"]
    assert on_digest != first_line(root, TICK_OFF)["identity_sha256"]
    digest = _tampered(root, TICK_OFF, identity_sha256=on_digest)
    expect(Failure.IDENTITY_DIGEST_MISMATCHED, lambda: read(root, digest))


@pytest.mark.parametrize(
    "update",
    [
        {"clean": False},
        {"observed_command_streaming_required": None, "applied_branch": None},
    ],
    ids=["clean-flipped", "null-fabricated"],
)
def test_reader_rederives_the_finalisation_index(tmp_path: Path, update: dict[str, object]) -> None:
    """Scalars disagreeing with the trusted envelope are refused by the reader binding."""
    root, _sealed, _records = write_full_run(tmp_path)
    original = first_line(root, ON_FINALISATION)
    assert original["clean"] is True
    assert original["observed_command_streaming_required"] is False
    digest = _tampered(root, ON_FINALISATION, **update)
    expect(Failure.FINALISATION_INDEX_MISMATCHED, lambda: read(root, digest))


def test_identity_extras_are_admitted_exactly_at_the_cap(tmp_path: Path) -> None:
    """Exactly the maximum number of tolerant-origin extras is retained."""
    document = identity_document(tmp_path)
    for index in range(store.MAX_IDENTITY_EXTRA_KEYS):
        document["runtime_config"][f"extra_{index}"] = index
    identity = store.read_identity_v1(envelope_of(document))
    assert len(identity.runtime_config_extras) == store.MAX_IDENTITY_EXTRA_KEYS


def test_lone_surrogate_line_is_refused_by_the_walker(tmp_path: Path) -> None:
    """A JSON-escaped lone surrogate parses but is refused before model validation."""
    root, _sealed, _records = write_full_run(tmp_path)
    document = _tick_device_document(root, raw_vendor_data={"k": "\ud800"})
    line = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
    digest = rewrite(root, TICK_OFF, line)
    with pytest.raises(schema.ColdEvidenceError) as raised:
        read(root, digest)
    assert raised.value.failure is schema.ColdEvidenceFailure.JSON_VALUE_TYPE_NOT_ADMITTED
