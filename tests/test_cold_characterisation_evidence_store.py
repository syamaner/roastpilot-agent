"""Behavioural and fail-closed tests for the private cold evidence store."""

import ast
import hashlib
import json
import os
import shutil
import stat
import threading
import typing
from pathlib import Path

import pytest

from roastpilot_agent.cold_characterisation import evidence_reader as reader
from roastpilot_agent.cold_characterisation import evidence_schema as schema
from roastpilot_agent.cold_characterisation import evidence_store as store
from tests.test_cold_characterisation_evidence_builders import (
    COLD_PACKAGE,
    NEW_MODULES,
    RUN_ID,
    abort_for,
    advisory_for,
    finalisation_for,
    header_for,
    host_for,
    make_identity,
    tick_for,
)

OFF = schema.ColdPhaseKind.RECORDING_OFF
ON = schema.ColdPhaseKind.RECORDING_ON
Failure = store.ColdEvidenceStoreFailure
SECRET = "sk-live-AbCdEfGhIjKlMnOpQrStUvWx0123"


Mutation = typing.Callable[[Path], object]
Transform = typing.Callable[[dict[str, typing.Any]], dict[str, typing.Any]]


def mutation(function: Mutation) -> Mutation:
    """Type one parametrized tree mutation."""
    return function


def transform(function: Transform) -> Transform:
    """Type one parametrized manifest transform."""
    return function


def make_root(tmp_path: Path, name: str = "pi") -> str:
    """Create one real, symlink-free evidence root."""
    path = tmp_path.resolve() / name
    path.mkdir(parents=True)
    return str(path)


def open_writer(tmp_path: Path, name: str = "pi") -> tuple[store.ColdEvidenceWriter, str]:
    """Admit a fresh root and open the shared test run beneath it."""
    root = make_root(tmp_path, name)
    return store.open_run(store.admit_evidence_root(root), RUN_ID), root


def on_header(tmp_path: Path, root: str) -> schema.ColdRunHeader:
    """Build a recording-on header whose identity differs from recording-off's."""
    from roastpilot_agent.cold_characterisation import evidence_builders as builders

    return builders.build_run_header(
        identity=make_identity(tmp_path, pi_root=root, audio_device="USB microphone two"),
        phase=ON,
        recorded_at_utc="2026-09-26T12:20:00Z",
        monotonic_seconds=1200.0,
    )


def write_full_run(
    tmp_path: Path, name: str = "pi"
) -> tuple[str, store.ColdSealedRun, list[schema.ColdEvidenceRecord]]:
    """Write and seal a two-phase run holding all six record kinds."""
    writer, root = open_writer(tmp_path, name)
    off = header_for(tmp_path, root, OFF)
    on = on_header(tmp_path, root)
    records: list[schema.ColdEvidenceRecord] = [
        off,
        tick_for(off, 0),
        tick_for(off, 1),
        host_for(off),
        advisory_for(off),
        on,
        tick_for(on, 0),
        host_for(on),
        finalisation_for(on),
        abort_for(on),
    ]
    for record in records:
        writer.append(record)
    return root, writer.seal(), records


def write_phase_one_run(tmp_path: Path, name: str = "pi") -> tuple[str, store.ColdSealedRun]:
    """Write and seal a phase-1-only aborted run."""
    writer, root = open_writer(tmp_path, name)
    off = header_for(tmp_path, root, OFF)
    for record in (off, tick_for(off, 0), tick_for(off, 1), abort_for(off)):
        writer.append(record)
    return root, writer.seal()


def run_dir(root: str) -> Path:
    """Return the shared test run directory beneath a root."""
    return Path(root) / RUN_ID


def copy_run(root: str, tmp_path: Path, name: str = "laptop") -> str:
    """Copy one sealed run to a second root, as the operator runbook would."""
    destination = make_root(tmp_path, name)
    shutil.copytree(run_dir(root), run_dir(destination))
    return destination


def craft_manifest(
    directory: Path,
    *,
    transform: typing.Callable[[dict[str, typing.Any]], dict[str, typing.Any]] | None = None,
    raw: bytes | None = None,
    sidecar: bytes | None = None,
) -> str:
    """Rewrite a tree's manifest pair from its current files; return the manifest digest."""
    previous = json.loads((directory / store.MANIFEST_JSON_NAME).read_bytes())
    for name in (store.MANIFEST_JSON_NAME, store.MANIFEST_SIDECAR_NAME):
        (directory / name).unlink()
    paths = sorted(
        (
            path.relative_to(directory).as_posix()
            for path in directory.rglob("*")
            if path.is_file() and not path.is_symlink()
        ),
        key=lambda value: value.encode(),
    )
    document: dict[str, typing.Any] = {
        "schema_version": 1,
        "run_id": previous["run_id"],
        "identity_bindings": previous["identity_bindings"],
        "entries": [
            {
                "relative_path": path,
                "size_bytes": len((directory / path).read_bytes()),
                "sha256": hashlib.sha256((directory / path).read_bytes()).hexdigest(),
            }
            for path in paths
        ],
    }
    if transform is not None:
        document = transform(document)
    manifest = raw if raw is not None else store.canonical_json(document).encode()
    rendered = "".join(
        f"{entry['sha256']}  {entry['relative_path']}\n" for entry in document["entries"]
    ).encode()
    (directory / store.MANIFEST_JSON_NAME).write_bytes(manifest)
    (directory / store.MANIFEST_SIDECAR_NAME).write_bytes(
        sidecar if sidecar is not None else rendered
    )
    return hashlib.sha256(manifest).hexdigest()


def snapshot_tree(*roots: str) -> dict[str, tuple[int, int, int, int, int]]:
    """Return ``(dev, ino, mode, mtime_ns, size)`` for every node under roots."""
    nodes: dict[str, tuple[int, int, int, int, int]] = {}
    for root in roots:
        for path in [Path(root), *Path(root).rglob("*")]:
            result = os.lstat(path)
            nodes[str(path)] = (
                result.st_dev,
                result.st_ino,
                result.st_mode,
                result.st_mtime_ns,
                result.st_size,
            )
    return nodes


def expect(failure: store.ColdEvidenceStoreFailure, call: typing.Callable[[], object]) -> None:
    """Assert one call raises exactly one closed, chain-free store failure."""
    with pytest.raises(store.ColdEvidenceStoreError) as raised:
        call()
    assert raised.value.failure is failure
    assert raised.value.args == ("Cold evidence store operation failed.",)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


# ------------------------------------------------------------------ admission


def test_absolute_real_root_is_admitted(tmp_path: Path) -> None:
    """An absolute, symlink-free directory outside protected roots is admitted."""
    root = make_root(tmp_path)
    admitted = store.admit_evidence_root(root)
    assert (admitted.path, admitted.realpath) == (root, root)


def test_relative_root_refuses_before_any_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absoluteness is checked on the unresolved string, before resolution."""
    resolved: list[str] = []
    real = os.path.realpath

    def spy(path: str) -> str:
        resolved.append(path)
        return real(path)

    monkeypatch.chdir(tmp_path)
    Path("relative").mkdir()
    monkeypatch.setattr(os.path, "realpath", spy)
    expect(Failure.ROOT_NOT_ABSOLUTE, lambda: store.admit_evidence_root("relative"))
    assert resolved == []


def test_package_directory_and_checkout_are_protected(tmp_path: Path) -> None:
    """The installed package directory and any source checkout can never hold evidence."""
    package = Path(store.__file__).resolve().parents[1]
    expect(Failure.ROOT_PROTECTED, lambda: store.admit_evidence_root(str(package)))
    expect(Failure.ROOT_PROTECTED, lambda: store.admit_evidence_root(str(package.parent)))
    checkout = store._source_checkout_root(store.__file__)  # pyright: ignore[reportPrivateUsage]
    assert checkout is not None or "site-packages" in store.__file__
    if checkout is not None:
        expect(Failure.ROOT_PROTECTED, lambda: store.admit_evidence_root(checkout))
        inside = str(Path(checkout) / "docs")
        expect(Failure.ROOT_PROTECTED, lambda: store.admit_evidence_root(inside))


def test_declared_protected_roots_only_add(tmp_path: Path) -> None:
    """Declared roots add protection, must be absolute, and never remove the defaults."""
    root = make_root(tmp_path)
    expect(
        Failure.ROOT_PROTECTED,
        lambda: store.admit_evidence_root(root, protected_roots=(str(tmp_path.resolve()),)),
    )
    expect(
        Failure.ROOT_NOT_ABSOLUTE,
        lambda: store.admit_evidence_root(root, protected_roots=("relative",)),
    )
    unrelated = make_root(tmp_path, "unrelated")
    assert store.admit_evidence_root(root, protected_roots=(unrelated,)).path == root


def test_symlinked_file_and_missing_components_refuse(tmp_path: Path) -> None:
    """Traversal is per-component and no-follow; non-directories refuse."""
    real = make_root(tmp_path, "real")
    (Path(real) / "child").mkdir()
    link = tmp_path.resolve() / "link"
    link.symlink_to(real)
    expect(Failure.ROOT_UNUSABLE, lambda: store.admit_evidence_root(str(link)))
    expect(Failure.ROOT_UNUSABLE, lambda: store.admit_evidence_root(str(link / "child")))
    file_component = tmp_path.resolve() / "file"
    file_component.write_text("x")
    expect(Failure.ROOT_UNUSABLE, lambda: store.admit_evidence_root(str(file_component)))
    expect(Failure.ROOT_UNUSABLE, lambda: store.admit_evidence_root(str(file_component / "x")))
    missing = str(tmp_path.resolve() / "missing")
    expect(Failure.ROOT_UNUSABLE, lambda: store.admit_evidence_root(missing))
    expect(Failure.ROOT_UNUSABLE, lambda: store.admit_evidence_root(f"{real}/child/.."))
    expect(Failure.ROOT_UNUSABLE, lambda: store.admit_evidence_root(real + "\x00"))


def test_platform_without_descriptor_support_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing descriptor-relative support fails closed before any path work."""
    monkeypatch.setattr(os, "supports_dir_fd", set[object]())
    expect(Failure.PLATFORM_UNSUPPORTED, lambda: store.admit_evidence_root(str(tmp_path)))


def test_checkout_probe_is_silent_when_the_layout_is_absent(tmp_path: Path) -> None:
    """A packaged runtime's probe contributes nothing and never raises."""
    module = tmp_path.resolve() / "a" / "b" / "c" / "d" / "module.py"
    assert store._source_checkout_root(str(module)) is None  # pyright: ignore[reportPrivateUsage]
    assert store._source_checkout_root("\x00") is None  # pyright: ignore[reportPrivateUsage]
    checkout = tmp_path.resolve() / "checkout"
    package = checkout / "src" / "roastpilot_agent" / "cold_characterisation"
    package.mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("")
    probe = store._source_checkout_root(str(package / "evidence_store.py"))  # pyright: ignore[reportPrivateUsage]
    assert probe == str(checkout)


def test_constructors_outside_admission_refuse(tmp_path: Path) -> None:
    """Admitted roots, writers, and verified trees exist only through their functions."""
    expect(Failure.ROOT_UNUSABLE, lambda: store.ColdAdmittedRoot("/", "/", token=object()))
    expect(
        Failure.ROOT_UNUSABLE,
        lambda: store.ColdEvidenceWriter(root_path="/", run_id=RUN_ID, run_fd=-1, token=None),
    )
    expect(
        Failure.ROOT_UNUSABLE,
        lambda: store.ColdVerifiedTree(
            root=typing.cast(typing.Any, None),
            manifest=typing.cast(typing.Any, None),
            manifest_bytes=b"",
            sidecar_bytes=b"",
            nodes={},
            token=None,
        ),
    )
    expect(Failure.ROOT_UNUSABLE, lambda: store.open_run(typing.cast(typing.Any, "/"), RUN_ID))


# ----------------------------------------------------------- run dir and layout


@pytest.mark.parametrize("mask", [0o000, 0o077, 0o277])
def test_modes_and_owner_hold_under_any_umask(tmp_path: Path, mask: int) -> None:
    """Explicit fchmod yields 0700/0600 owned by the euid regardless of umask."""
    root = make_root(tmp_path)
    off = header_for(tmp_path, root, OFF)
    tick = tick_for(off)
    previous = os.umask(mask)
    try:
        writer = store.open_run(store.admit_evidence_root(root), RUN_ID)
        assert not (run_dir(root) / "records").exists()
        writer.append(off)
        writer.append(tick)
        writer.close()
    finally:
        os.umask(previous)
    for path in [run_dir(root), *run_dir(root).rglob("*")]:
        result = os.lstat(path)
        expected = 0o700 if stat.S_ISDIR(result.st_mode) else 0o600
        assert stat.S_IMODE(result.st_mode) == expected, path
        assert result.st_uid == os.geteuid()


def test_open_run_refuses_reuse_bad_ids_and_unwritable_roots(tmp_path: Path) -> None:
    """An existing run directory is never reused and bad run ids never create one."""
    writer, root = open_writer(tmp_path)
    writer.close()
    admitted = store.admit_evidence_root(root)
    expect(Failure.RUN_DIR_EXISTS, lambda: store.open_run(admitted, RUN_ID))
    expect(Failure.RUN_ID_MISMATCHED, lambda: store.open_run(admitted, "../escape"))
    locked = make_root(tmp_path, "locked")
    locked_root = store.admit_evidence_root(locked)
    os.chmod(locked, 0o500)
    try:
        expect(Failure.ROOT_UNUSABLE, lambda: store.open_run(locked_root, RUN_ID))
    finally:
        os.chmod(locked, 0o700)


def test_foreign_owner_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A run directory not owned by the effective uid fails closed."""
    root = make_root(tmp_path)
    admitted = store.admit_evidence_root(root)
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    expect(Failure.OWNERSHIP_OR_MODE_MISMATCH, lambda: store.open_run(admitted, RUN_ID))


def test_foreign_owner_during_lazy_layout_poisons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ownership failure while creating layout is a poisoning write failure."""
    writer, root = open_writer(tmp_path)
    header = header_for(tmp_path, root, OFF)
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    expect(Failure.WRITE_FAILED, lambda: writer.append(header))
    expect(Failure.WRITER_POISONED, lambda: writer.append(header))


def test_appends_follow_the_held_descriptor_after_a_symlink_swap(tmp_path: Path) -> None:
    """Renaming the run directory and planting a symlink cannot redirect writes."""
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    writer.append(off)
    moved = Path(root) / "moved"
    decoy = make_root(tmp_path, "decoy")
    run_dir(root).rename(moved)
    run_dir(root).symlink_to(decoy)
    writer.append(tick_for(off))
    assert (moved / "records" / "recording_off" / "tick.jsonl").is_file()
    assert list(Path(decoy).iterdir()) == []
    writer.close()


# ----------------------------------------------------------- phase-1-only run


def test_phase_one_only_aborted_run_seals_verifies_and_reads(tmp_path: Path) -> None:
    """A phase-1-only abort verifies with one binding and no phase-2 placeholder."""
    root, sealed = write_phase_one_run(tmp_path)
    assert [binding.phase for binding in sealed.identity_bindings] == [OFF]
    assert not (run_dir(root) / "records" / "recording_on").exists()
    laptop = copy_run(root, tmp_path)
    verified = store.verify_retained_copies(
        root, laptop, run_id=RUN_ID, expected_manifest_sha256=sealed.manifest_sha256
    )
    assert verified.identity_bindings == sealed.identity_bindings
    assert verified.entry_count == sealed.entry_count == 3
    retained = reader.read_retained_run(
        laptop, run_id=RUN_ID, expected_manifest_sha256=sealed.manifest_sha256
    )
    assert [item.header.phase for item in retained.headers] == [OFF]
    assert [(item.phase, item.stream) for item in retained.streams] == [
        (OFF, schema.ColdEvidenceStream.HEADER),
        (OFF, schema.ColdEvidenceStream.TICK),
        (OFF, schema.ColdEvidenceStream.ABORT),
    ]


def test_empty_phase_directory_refuses_seal_and_verification(tmp_path: Path) -> None:
    """An empty directory is never evidence: seal and verification both refuse it."""
    writer, root = open_writer(tmp_path)
    writer.append(header_for(tmp_path, root, OFF))
    (run_dir(root) / "records" / "recording_on").mkdir(mode=0o700)
    expect(Failure.SEAL_TREE_INVALID, writer.seal)
    root, sealed = write_phase_one_run(tmp_path, "second")
    laptop = copy_run(root, tmp_path)
    (run_dir(laptop) / "records" / "recording_on").mkdir()
    expect(
        Failure.INVENTORY_MISMATCHED,
        lambda: store.verify_retained_copies(
            root, laptop, run_id=RUN_ID, expected_manifest_sha256=sealed.manifest_sha256
        ),
    )


# --------------------------------------------------------------------- binding


def test_header_ticks_host_and_finalisation_bind(tmp_path: Path) -> None:
    """A correctly bound sequence is accepted and sealed with both phase bindings."""
    root, sealed, records = write_full_run(tmp_path)
    assert [binding.phase for binding in sealed.identity_bindings] == [OFF, ON]
    off, on = records[0], records[5]
    assert [binding.identity_sha256 for binding in sealed.identity_bindings] == [
        off.identity_sha256,
        on.identity_sha256,
    ]
    assert off.identity_sha256 != on.identity_sha256
    assert (run_dir(root) / store.MANIFEST_JSON_NAME).is_file()


def test_binding_refusals_do_not_poison_or_bind(tmp_path: Path) -> None:
    """Each binding rule refuses its own violation and leaves the writer usable."""
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    expect(Failure.HEADER_MISSING, lambda: writer.append(tick_for(off)))
    foreign_header = header_for(tmp_path, root, OFF, run_id="20260926T120000Z-foreign")
    expect(Failure.RUN_ID_MISMATCHED, lambda: writer.append(foreign_header))
    digest_mismatch = off.model_copy(update={"identity_sha256": "0" * 64})
    expect(Failure.IDENTITY_DIGEST_MISMATCHED, lambda: writer.append(digest_mismatch))
    envelope_run = foreign_header.model_copy(update={"run_id": RUN_ID})
    expect(Failure.HEADER_BINDING_MISMATCHED, lambda: writer.append(envelope_run))
    other_root = header_for(tmp_path, "/elsewhere", OFF)
    expect(Failure.HEADER_BINDING_MISMATCHED, lambda: writer.append(other_root))
    writer.append(off)
    expect(Failure.HEADER_DUPLICATED, lambda: writer.append(off))
    on = on_header(tmp_path, root)
    writer.append(on)
    wrong_phase_digest = tick_for(on).model_copy(update={"identity_sha256": off.identity_sha256})
    expect(Failure.IDENTITY_DIGEST_MISMATCHED, lambda: writer.append(wrong_phase_digest))
    foreign_tick = tick_for(off).model_copy(update={"run_id": "20260926T120000Z-foreign"})
    expect(Failure.RUN_ID_MISMATCHED, lambda: writer.append(foreign_tick))
    writer.append(tick_for(on))
    assert writer.seal().entry_count == 3


@pytest.mark.parametrize(
    ("streaming", "update"),
    [
        (False, {"session_id": "another-session"}),
        (False, {"status": schema.ColdFinalisationStatus.PARTIAL}),
        (False, {"clean": False}),
        (
            False,
            {
                "observed_command_streaming_required": True,
                "applied_branch": schema.ColdCapabilityBranch.STREAMING,
            },
        ),
        (False, {"observed_command_streaming_required": None, "applied_branch": None}),
        (
            None,
            {
                "observed_command_streaming_required": False,
                "applied_branch": schema.ColdCapabilityBranch.NON_STREAMING,
            },
        ),
        (
            True,
            {
                "observed_command_streaming_required": False,
                "applied_branch": schema.ColdCapabilityBranch.NON_STREAMING,
            },
        ),
    ],
)
def test_each_finalisation_index_field_is_rederived(
    tmp_path: Path, streaming: bool | None, update: dict[str, object]
) -> None:
    """A caller-supplied index, including a fabricated ``False``, never binds."""
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    writer.append(off)
    record = finalisation_for(off, streaming=streaming)
    expect(
        Failure.FINALISATION_INDEX_MISMATCHED,
        lambda: writer.append(record.model_copy(update=update)),
    )
    writer.append(record)


def test_generic_envelope_never_binds_as_finalisation(tmp_path: Path) -> None:
    """A format-only envelope accepted by the pure schema fails the binding."""
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    writer.append(off)
    canonical = store.canonical_json({"a": 1})
    envelope = schema.ColdSealedEnvelope(
        kind=schema.ColdEnvelopeKind.FINALISATION,
        schema_version=1,
        canonical_json=canonical,
        canonical_byte_length=len(canonical),
        sha256=hashlib.sha256(canonical.encode()).hexdigest(),
    )
    generic = finalisation_for(off).model_copy(update={"envelope": envelope})
    expect(Failure.FINALISATION_INDEX_MISMATCHED, lambda: writer.append(generic))


def test_schema_errors_propagate_unchanged(tmp_path: Path) -> None:
    """A record failing ``validate_record`` raises the schema error, not a store error."""
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    with pytest.raises(schema.ColdEvidenceError):
        writer.append(off.model_copy(update={"monotonic_seconds": float("nan")}))
    writer.append(off)


# ---------------------------------------------------------------- writer state


def test_lines_are_canonical_lf_terminated_and_byte_equal(tmp_path: Path) -> None:
    """Each stream file is exactly the canonical snapshot lines, LF-terminated."""
    root, _sealed, records = write_full_run(tmp_path)
    tick_file = run_dir(root) / "records" / "recording_off" / "tick.jsonl"
    expected = b"".join(
        schema._canonical_json(record.model_dump(mode="json")).encode() + b"\n"  # pyright: ignore[reportPrivateUsage]
        for record in records[1:3]
    )
    assert tick_file.read_bytes() == expected


def test_write_failure_poisons_and_leaves_a_torn_unsealable_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed write poisons the writer; append and seal then refuse."""
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    writer.append(off)
    real_write = store._write_chunk  # pyright: ignore[reportPrivateUsage]

    def torn(descriptor: int, data: memoryview) -> int:
        real_write(descriptor, data[:10])
        raise OSError("disk full")

    monkeypatch.setattr(store, "_write_chunk", torn)
    expect(Failure.WRITE_FAILED, lambda: writer.append(tick_for(off)))
    monkeypatch.setattr(store, "_write_chunk", real_write)
    expect(Failure.WRITER_POISONED, lambda: writer.append(tick_for(off)))
    expect(Failure.WRITER_POISONED, writer.seal)
    torn_file = run_dir(root) / "records" / "recording_off" / "tick.jsonl"
    assert len(torn_file.read_bytes()) == 10
    assert not (run_dir(root) / store.MANIFEST_JSON_NAME).exists()


def _no_progress(_descriptor: int, _data: memoryview) -> int:
    """Simulate a write that makes no progress."""
    return 0


def test_zero_length_write_poisons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A write that makes no progress is a write failure, not a silent loss."""
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    monkeypatch.setattr(store, "_write_chunk", _no_progress)
    expect(Failure.WRITE_FAILED, lambda: writer.append(off))


def test_partial_writes_are_completed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Short writes loop until the whole line is durable."""
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    real_write = store._write_chunk  # pyright: ignore[reportPrivateUsage]

    def partial(descriptor: int, data: memoryview) -> int:
        return real_write(descriptor, data[: max(1, len(data) // 3)])

    monkeypatch.setattr(store, "_write_chunk", partial)
    writer.append(off)
    monkeypatch.setattr(store, "_write_chunk", real_write)
    expected = schema._canonical_json(off.model_dump(mode="json")).encode() + b"\n"  # pyright: ignore[reportPrivateUsage]
    assert (run_dir(root) / "records" / "recording_off" / "header.jsonl").read_bytes() == expected


def test_sealed_and_closed_writers_refuse(tmp_path: Path) -> None:
    """Append and seal after a successful seal refuse; a closed writer is unusable."""
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    writer.append(off)
    writer.seal()
    expect(Failure.WRITER_SEALED, lambda: writer.append(tick_for(off)))
    expect(Failure.WRITER_SEALED, writer.seal)
    other, other_root = open_writer(tmp_path, "other")
    other.append(header_for(tmp_path, other_root, OFF))
    other.close()
    expect(Failure.WRITER_POISONED, other.seal)


def test_seal_without_a_header_refuses(tmp_path: Path) -> None:
    """A run with no header cannot be sealed."""
    writer, _root = open_writer(tmp_path)
    expect(Failure.SEAL_TREE_INVALID, writer.seal)
    expect(Failure.WRITER_POISONED, writer.seal)


# ----------------------------------------------------------------- seal races


def test_manifest_pair_matches_and_digest_is_of_reread_bytes(tmp_path: Path) -> None:
    """The manifest digest hashes on-disk bytes; the sidecar is ``sha256sum -c`` form."""
    root, sealed, _records = write_full_run(tmp_path)
    manifest_bytes = (run_dir(root) / store.MANIFEST_JSON_NAME).read_bytes()
    assert sealed.manifest_sha256 == hashlib.sha256(manifest_bytes).hexdigest()
    assert not manifest_bytes.endswith(b"\n")
    manifest = json.loads(manifest_bytes)
    sidecar = (run_dir(root) / store.MANIFEST_SIDECAR_NAME).read_text()
    lines = sidecar.splitlines()
    assert len(lines) == len(manifest["entries"]) == sealed.entry_count == 9
    for line, entry in zip(lines, manifest["entries"], strict=True):
        digest, path = line.split("  ")
        assert (
            digest
            == entry["sha256"]
            == hashlib.sha256((run_dir(root) / path).read_bytes()).hexdigest()
        )
        assert path == entry["relative_path"]
    for name in (store.MANIFEST_JSON_NAME, store.MANIFEST_SIDECAR_NAME):
        assert stat.S_IMODE(os.lstat(run_dir(root) / name).st_mode) == 0o600


def _planted(tmp_path: Path, plant: Mutation) -> None:
    """Plant one invalid node in an unsealed run and require seal refusal."""
    writer, root = open_writer(tmp_path)
    writer.append(header_for(tmp_path, root, OFF))
    plant(run_dir(root) / "records" / "recording_off")
    expect(Failure.SEAL_TREE_INVALID, writer.seal)


@pytest.mark.parametrize(
    "plant",
    [
        mutation(
            lambda directory: (directory / "link.jsonl").symlink_to(directory / "header.jsonl")
        ),
        mutation(lambda directory: os.mkfifo(directory / "fifo.jsonl", 0o600)),
        mutation(lambda directory: os.link(directory / "header.jsonl", directory / "hard.jsonl")),
        mutation(lambda directory: (directory / ".hidden").write_bytes(b"x")),
        mutation(lambda directory: os.chmod(directory / "header.jsonl", 0o644)),
        mutation(lambda directory: os.chmod(directory, 0o755)),
        mutation(
            lambda directory: (directory.parent.parent / store.MANIFEST_JSON_NAME).write_bytes(
                b"{}"
            )
        ),
    ],
    ids=["symlink", "fifo", "hard-link", "bad-segment", "file-mode", "dir-mode", "manifest"],
)
def test_seal_refuses_invalid_nodes(tmp_path: Path, plant: Mutation) -> None:
    """Symlinks, FIFOs, hard links, bad names, wrong modes, and planted manifests refuse."""
    previous = os.umask(0o077)
    try:
        _planted(tmp_path, plant)
    finally:
        os.umask(previous)


def test_file_changed_between_identity_reads_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-size rewrite with a new timestamp between identity reads is refused."""
    writer, root = open_writer(tmp_path)
    writer.append(header_for(tmp_path, root, OFF))

    def rewrite(relative_path: str) -> None:
        path = run_dir(root) / relative_path
        data = path.read_bytes()
        path.write_bytes(data[:-2] + b"X\n")
        os.utime(path, ns=(1, 1))

    monkeypatch.setattr(store, "_after_first_identity_read", rewrite)
    expect(Failure.FILE_CHANGED, writer.seal)


def test_file_changed_after_enumeration_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file whose identity moved since pass 1 is refused before it is read."""
    writer, root = open_writer(tmp_path)
    writer.append(header_for(tmp_path, root, OFF))
    real_flags = store._file_read_flags  # pyright: ignore[reportPrivateUsage]

    def touch_then_flags() -> int:
        os.utime(run_dir(root) / "records" / "recording_off" / "header.jsonl", ns=(1, 1))
        return real_flags()

    monkeypatch.setattr(store, "_file_read_flags", touch_then_flags)
    expect(Failure.FILE_CHANGED, writer.seal)


def test_short_read_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A read ending before the recorded size is incomplete, never accepted."""
    writer, root = open_writer(tmp_path)
    writer.append(header_for(tmp_path, root, OFF))

    def empty(_descriptor: int, _size: int) -> bytes:
        return b""

    monkeypatch.setattr(store, "_read_chunk", empty)
    expect(Failure.FILE_READ_INCOMPLETE, writer.seal)


def test_read_error_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An OS read failure is an incomplete read."""
    writer, root = open_writer(tmp_path)
    writer.append(header_for(tmp_path, root, OFF))

    def failing(_descriptor: int, _size: int) -> bytes:
        raise OSError("read failed")

    monkeypatch.setattr(store, "_read_chunk", failing)
    expect(Failure.FILE_READ_INCOMPLETE, writer.seal)


@pytest.mark.parametrize("change", ["add", "remove"])
def test_tree_change_between_passes_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """A late addition or removal between the two passes is refused."""
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    writer.append(off)
    writer.append(tick_for(off))
    directory = run_dir(root) / "records" / "recording_off"

    def mutate() -> None:
        if change == "add":
            (directory / "late.jsonl").write_bytes(b"x")
            os.chmod(directory / "late.jsonl", 0o600)
        else:
            (directory / "tick.jsonl").unlink()

    monkeypatch.setattr(store, "_between_enumeration_passes", mutate)
    expect(Failure.SEAL_TREE_CHANGED, writer.seal)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MAX_MANIFEST_ENTRIES", 2),
        ("MAX_MANIFEST_ENTRIES", 0),
        ("MAX_EVIDENCE_FILE_BYTES", 10),
        ("MAX_MANIFEST_BYTES", 10),
        ("MAX_PATH_SEGMENTS", 2),
    ],
)
def test_seal_bounds_plus_one_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: int
) -> None:
    """Each seal bound refuses when exceeded by the tree."""
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    for record in (off, tick_for(off), host_for(off)):
        writer.append(record)
    monkeypatch.setattr(store, name, value)
    expect(Failure.SEAL_LIMIT_EXCEEDED, writer.seal)


def test_manifest_write_failures_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed or unreadable-back manifest write is a write failure."""
    writer, root = open_writer(tmp_path)
    writer.append(header_for(tmp_path, root, OFF))
    monkeypatch.setattr(store, "_write_chunk", _no_progress)
    expect(Failure.WRITE_FAILED, writer.seal)
    monkeypatch.undo()
    second, second_root = open_writer(tmp_path, "second")
    second.append(header_for(tmp_path, second_root, OFF))

    def other_bytes(_descriptor: int, _name: str) -> bytes:
        return b"other"

    monkeypatch.setattr(store, "_read_small_file", other_bytes)
    expect(Failure.WRITE_FAILED, second.seal)


# ----------------------------------------------------------- both-tree checks


def test_two_identical_trees_verify(tmp_path: Path) -> None:
    """Two byte-identical copies verify to integrity facts only."""
    root, sealed, _records = write_full_run(tmp_path)
    laptop = copy_run(root, tmp_path)
    for path in run_dir(laptop).rglob("*"):
        os.chmod(path, 0o755 if path.is_dir() else 0o644)
    verified = store.verify_retained_copies(
        root, laptop, run_id=RUN_ID, expected_manifest_sha256=sealed.manifest_sha256
    )
    assert verified.model_dump() == {
        "run_id": RUN_ID,
        "manifest_sha256": sealed.manifest_sha256,
        "entry_count": sealed.entry_count,
        "identity_bindings": tuple(binding.model_dump() for binding in sealed.identity_bindings),
    }


def _two_trees(tmp_path: Path) -> tuple[str, str, str]:
    """Return a sealed Pi tree, its laptop copy, and the manifest digest."""
    root, sealed, _records = write_full_run(tmp_path)
    return root, copy_run(root, tmp_path), sealed.manifest_sha256


def _flip(path: Path) -> None:
    """Flip one byte in place without changing the file size."""
    data = bytearray(path.read_bytes())
    data[5] ^= 0x01
    path.write_bytes(bytes(data))


@pytest.mark.parametrize("which", [0, 1])
@pytest.mark.parametrize(
    ("mutate", "failure"),
    [
        (
            mutation(lambda d: _flip(d / "records/recording_on/tick.jsonl")),
            Failure.FILE_DIGEST_MISMATCHED,
        ),
        (
            mutation(lambda d: (d / "records/recording_on/tick.jsonl").write_bytes(b"{}\n")),
            Failure.FILE_DIGEST_MISMATCHED,
        ),
        (
            mutation(lambda d: (d / "records/extra.jsonl").write_bytes(b"x")),
            Failure.INVENTORY_MISMATCHED,
        ),
        (
            mutation(lambda d: (d / "records/recording_on/host.jsonl").unlink()),
            Failure.INVENTORY_MISMATCHED,
        ),
        (
            mutation(lambda d: (d / "records/x" / "y").mkdir(parents=True)),
            Failure.INVENTORY_MISMATCHED,
        ),
        (mutation(lambda d: (d / "records/.bad").write_bytes(b"x")), Failure.INVENTORY_MISMATCHED),
        (
            mutation(
                lambda d: (d / "records/recording_on/link").symlink_to(d / store.MANIFEST_JSON_NAME)
            ),
            Failure.FILE_NOT_REGULAR,
        ),
        (
            mutation(lambda d: _flip(d / store.MANIFEST_SIDECAR_NAME)),
            Failure.SIDECAR_INCONSISTENT,
        ),
        (
            mutation(lambda d: _flip(d / store.MANIFEST_JSON_NAME)),
            Failure.MANIFEST_DIGEST_MISMATCHED,
        ),
        (mutation(lambda d: (d / store.MANIFEST_JSON_NAME).unlink()), Failure.MANIFEST_MALFORMED),
        (mutation(lambda d: shutil.rmtree(d)), Failure.INVENTORY_MISMATCHED),
    ],
    ids=[
        "flip",
        "resize",
        "extra",
        "missing",
        "empty-dirs",
        "bad-name",
        "symlink",
        "sidecar",
        "manifest",
        "no-manifest",
        "no-run",
    ],
)
def test_either_tree_fails_closed(
    tmp_path: Path,
    which: int,
    mutate: Mutation,
    failure: store.ColdEvidenceStoreFailure,
) -> None:
    """A defect in either copy fails the both-tree verification."""
    trees = _two_trees(tmp_path)
    mutate(run_dir(trees[which]))
    expect(
        failure,
        lambda: store.verify_retained_copies(
            trees[0], trees[1], run_id=RUN_ID, expected_manifest_sha256=trees[2]
        ),
    )


@pytest.mark.parametrize(
    ("transform", "raw", "sidecar", "failure"),
    [
        (
            transform(lambda doc: {**doc, "entries": [*doc["entries"], doc["entries"][-1]]}),
            None,
            None,
            Failure.ENTRY_DUPLICATED,
        ),
        (
            transform(
                lambda doc: {
                    **doc,
                    "entries": [
                        {**doc["entries"][0], "relative_path": "../escape"},
                        *doc["entries"],
                    ],
                }
            ),
            None,
            None,
            Failure.ENTRY_PATH_INVALID,
        ),
        (
            transform(lambda doc: {**doc, "entries": list(reversed(doc["entries"]))}),
            None,
            None,
            Failure.MANIFEST_MALFORMED,
        ),
        (
            transform(lambda doc: {**doc, "run_id": "20260926T120000Z-other"}),
            None,
            None,
            Failure.RUN_ID_MISMATCHED,
        ),
        (None, b'{"schema_version":1,"schema_version":1}', None, Failure.JSON_DUPLICATE_KEY),
        (None, b'{"schema_version":NaN}', None, Failure.JSON_NOT_FINITE),
        (None, b'{"schema_version":1e999}', None, Failure.JSON_NOT_FINITE),
        (None, b"\xff", None, Failure.MANIFEST_MALFORMED),
        (None, b'{"schema_version": 1}', None, Failure.MANIFEST_MALFORMED),
        (None, None, b"not a sidecar\n", Failure.SIDECAR_INCONSISTENT),
    ],
    ids=[
        "duplicate",
        "traversal",
        "unsorted",
        "run-id",
        "duplicate-key",
        "nan",
        "overflow",
        "utf8",
        "non-canonical",
        "sidecar-format",
    ],
)
def test_crafted_manifests_refuse(
    tmp_path: Path,
    transform: typing.Callable[[dict[str, typing.Any]], dict[str, typing.Any]] | None,
    raw: bytes | None,
    sidecar: bytes | None,
    failure: store.ColdEvidenceStoreFailure,
) -> None:
    """Crafted manifests refuse even when their digest is the expected one."""
    root, _sealed, _records = write_full_run(tmp_path)
    digest = craft_manifest(run_dir(root), transform=transform, raw=raw, sidecar=sidecar)
    expect(
        failure,
        lambda: store.verify_retained_tree(root, run_id=RUN_ID, expected_manifest_sha256=digest),
    )


def test_non_canonical_but_valid_manifest_refuses(tmp_path: Path) -> None:
    """A semantically valid manifest in non-canonical bytes is refused."""
    root, _sealed, _records = write_full_run(tmp_path)
    directory = run_dir(root)
    document = json.loads((directory / store.MANIFEST_JSON_NAME).read_bytes())
    spaced = json.dumps(document, sort_keys=True, separators=(", ", ": ")).encode()
    digest = craft_manifest(directory, raw=spaced)
    expect(
        Failure.MANIFEST_MALFORMED,
        lambda: store.verify_retained_tree(root, run_id=RUN_ID, expected_manifest_sha256=digest),
    )


def test_wrong_or_malformed_expected_digest_refuses(tmp_path: Path) -> None:
    """Only the recorded manifest digest verifies."""
    root, laptop, _digest = _two_trees(tmp_path)
    for digest in ("0" * 64, "not-a-digest"):
        expect(
            Failure.MANIFEST_DIGEST_MISMATCHED,
            lambda digest=digest: store.verify_retained_copies(
                root, laptop, run_id=RUN_ID, expected_manifest_sha256=digest
            ),
        )


def test_oversized_manifest_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A manifest above its byte bound is refused before it is read."""
    root, laptop, digest = _two_trees(tmp_path)
    monkeypatch.setattr(store, "MAX_MANIFEST_BYTES", 10)
    expect(
        Failure.MANIFEST_MALFORMED,
        lambda: store.verify_retained_copies(
            root, laptop, run_id=RUN_ID, expected_manifest_sha256=digest
        ),
    )


def test_verification_detects_change_between_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A late addition during verification is refused."""
    root, laptop, digest = _two_trees(tmp_path)
    calls: list[int] = []

    def late_addition() -> None:
        calls.append(1)
        if len(calls) == 2:
            (run_dir(laptop) / "records" / "late").write_bytes(b"x")

    monkeypatch.setattr(store, "_between_enumeration_passes", late_addition)
    expect(
        Failure.SEAL_TREE_CHANGED,
        lambda: store.verify_retained_copies(
            root, laptop, run_id=RUN_ID, expected_manifest_sha256=digest
        ),
    )


def test_copies_must_be_byte_equal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Divergent verified manifest pairs are refused."""
    root, laptop, digest = _two_trees(tmp_path)
    real = store._verify_admitted_tree  # pyright: ignore[reportPrivateUsage]
    calls: list[int] = []

    def diverging(
        admitted: store.ColdAdmittedRoot, *, run_id: str, expected_manifest_sha256: str
    ) -> store.ColdVerifiedTree:
        tree = real(admitted, run_id=run_id, expected_manifest_sha256=expected_manifest_sha256)
        calls.append(1)
        if len(calls) == 1:
            return tree
        return store.ColdVerifiedTree(
            root=tree.root,
            manifest=tree.manifest,
            manifest_bytes=tree.manifest_bytes,
            sidecar_bytes=tree.sidecar_bytes + b"x",
            nodes={},
            token=store._ADMISSION_TOKEN,  # pyright: ignore[reportPrivateUsage]
        )

    monkeypatch.setattr(store, "_verify_admitted_tree", diverging)
    expect(
        Failure.MANIFEST_COPIES_DIFFER,
        lambda: store.verify_retained_copies(
            root, laptop, run_id=RUN_ID, expected_manifest_sha256=digest
        ),
    )


def test_overlapping_or_equal_roots_refuse(tmp_path: Path) -> None:
    """The two copies must be distinct, non-nested roots."""
    root, _sealed, _records = write_full_run(tmp_path)
    nested = str(Path(root) / RUN_ID)
    for first, second in ((root, root), (root, nested), (nested, root)):
        expect(
            Failure.ROOTS_OVERLAP,
            lambda first=first, second=second: store.verify_retained_copies(
                first, second, run_id=RUN_ID, expected_manifest_sha256="0" * 64
            ),
        )


def test_verification_and_reading_modify_nothing(tmp_path: Path) -> None:
    """Verify and read are read-only: every node's identity is unchanged."""
    root, laptop, digest = _two_trees(tmp_path)
    before = snapshot_tree(root, laptop)
    store.verify_retained_copies(root, laptop, run_id=RUN_ID, expected_manifest_sha256=digest)
    reader.read_retained_run(laptop, run_id=RUN_ID, expected_manifest_sha256=digest)
    _flip(run_dir(laptop) / "records/recording_on/tick.jsonl")
    during = snapshot_tree(root, laptop)
    with pytest.raises(store.ColdEvidenceStoreError):
        store.verify_retained_copies(root, laptop, run_id=RUN_ID, expected_manifest_sha256=digest)
    assert snapshot_tree(root, laptop) == during
    assert {path: value for path, value in before.items() if "tick.jsonl" not in path} == {
        path: value for path, value in during.items() if "tick.jsonl" not in path
    }


def test_read_verified_file_refuses_changed_or_unknown_entries(tmp_path: Path) -> None:
    """Re-reads after verification require unchanged identity and manifest digest."""
    root, _sealed, _records = write_full_run(tmp_path)
    digest = hashlib.sha256((run_dir(root) / store.MANIFEST_JSON_NAME).read_bytes()).hexdigest()
    tree = store.verify_retained_tree(root, run_id=RUN_ID, expected_manifest_sha256=digest)
    expect(Failure.ENTRY_PATH_INVALID, lambda: store.read_verified_file(tree, "records/none"))
    path = run_dir(root) / "records/recording_off/tick.jsonl"
    _flip(path)
    expect(
        Failure.FILE_CHANGED,
        lambda: store.read_verified_file(tree, "records/recording_off/tick.jsonl"),
    )
    os.utime(run_dir(root), ns=(1, 1))
    expect(
        Failure.FILE_CHANGED,
        lambda: store.read_verified_file(tree, "records/recording_off/host.jsonl"),
    )


def test_read_verified_file_requires_the_manifest_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bytes whose digest differs from the manifest entry are never returned."""
    root, _sealed, _records = write_full_run(tmp_path)
    digest = hashlib.sha256((run_dir(root) / store.MANIFEST_JSON_NAME).read_bytes()).hexdigest()
    tree = store.verify_retained_tree(root, run_id=RUN_ID, expected_manifest_sha256=digest)

    def wrong_digest(*_args: object, **_kwargs: object) -> tuple[str, bytes]:
        return "0" * 64, b"x"

    monkeypatch.setattr(store, "_read_node", wrong_digest)
    expect(
        Failure.FILE_DIGEST_MISMATCHED,
        lambda: store.read_verified_file(tree, "records/recording_off/tick.jsonl"),
    )


# ----------------------------------------------------------- error containment


def test_errors_never_carry_paths_keys_or_values(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A credential-shaped value in a path or key appears in no error channel or log."""
    secret_root = tmp_path.resolve() / SECRET
    errors: list[store.ColdEvidenceStoreError] = []
    for call in (
        lambda: store.admit_evidence_root(SECRET),
        lambda: store.admit_evidence_root(str(secret_root)),
        lambda: store.load_strict_json(
            f'{{"{SECRET}":1,"{SECRET}":2}}'.encode(), malformed=Failure.LINE_MALFORMED
        ),
        lambda: store.load_strict_json(f'{{"{SECRET}":'.encode(), malformed=Failure.LINE_MALFORMED),
    ):
        with pytest.raises(store.ColdEvidenceStoreError) as raised:
            call()
        errors.append(raised.value)
    for error in errors:
        rendered = f"{error!s}{error!r}{error.args}"
        assert SECRET not in rendered
        assert error.__cause__ is None
        assert error.__context__ is None
    assert SECRET not in caplog.text


# ------------------------------------------------------------- source sweeps


def _calls(path: Path) -> list[ast.Call]:
    """Return every call node in one module."""
    return [node for node in ast.walk(ast.parse(path.read_text())) if isinstance(node, ast.Call)]


def _call_name(node: ast.Call) -> str:
    """Render a call target as dotted text."""
    return ast.unparse(node.func)


def test_class_n_no_destructive_filesystem_calls_in_the_cold_package() -> None:
    """Class N: no delete, rename, replace, truncate, or shutil anywhere."""
    forbidden_attributes = {"unlink", "rmdir", "rmtree", "truncate", "ftruncate", "removedirs"}
    forbidden_calls = {"os.replace", "os.rename", "os.renames", "os.remove"}
    for path in sorted(COLD_PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in forbidden_attributes, (path.name, node.attr)
                assert ast.unparse(node) not in forbidden_calls, (path.name, ast.unparse(node))
            if isinstance(node, ast.Import):
                assert all(alias.name != "shutil" for alias in node.names), path.name
            if isinstance(node, ast.ImportFrom):
                assert node.module != "shutil", path.name


def test_class_o_and_i_new_modules_use_descriptor_relative_io_only() -> None:
    """Classes O and I: new-module opens and mkdirs pass ``dir_fd``; no subprocess."""
    for path in NEW_MODULES:
        source = path.read_text()
        assert "subprocess" not in source
        for node in _calls(path):
            name = _call_name(node)
            assert name not in {"open", "Path", "os.walk"}, (path.name, name)
            if name in {"os.open", "os.mkdir"}:
                keywords = {keyword.arg for keyword in node.keywords}
                root_traversal = name == "os.open" and ast.unparse(node.args[0]) == "'/'"
                assert "dir_fd" in keywords or root_traversal, (path.name, ast.unparse(node))
            assert name != "os.listdir", path.name
            if name == "os.scandir":
                assert ast.unparse(node.args[0]) == "descriptor"
    subprocess_users = sorted(
        path.name for path in COLD_PACKAGE.glob("*.py") if "subprocess" in path.read_text()
    )
    assert subprocess_users == ["host.py"]


def test_class_c_one_canonical_dumper_and_one_strict_loader() -> None:
    """Class C: one ``json.dumps`` and one ``json.loads`` site, both in the store."""
    counts = {"json.dumps": 0, "json.loads": 0, "model_dump_json": 0}
    for path in NEW_MODULES:
        for node in _calls(path):
            name = _call_name(node)
            for key in counts:
                if name.endswith(key):
                    counts[key] += 1
    assert counts == {"json.dumps": 1, "json.loads": 1, "model_dump_json": 0}


def test_class_d_no_prefix_or_suffix_matching_in_new_modules() -> None:
    """Class D: containment and dispatch never use ``startswith``/``endswith`` on paths."""
    for path in NEW_MODULES:
        for node in _calls(path):
            name = _call_name(node)
            if name.endswith(("startswith", "endswith")):
                assert path.name == "evidence_reader.py" and name == "data.endswith"


# ------------------------------------------------------------ residual branches


def test_run_id_grammar_refuses_non_strings() -> None:
    """Only a string matching the record grammar is a run id."""
    assert store.run_id_is_valid(RUN_ID)
    assert not store.run_id_is_valid(1)
    assert not store.run_id_is_valid("cold-1")


def test_packaged_runtime_without_a_checkout_admits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no source checkout, only the package and declared roots are protected."""

    def no_checkout(_module: str) -> None:
        return None

    monkeypatch.setattr(store, "_source_checkout_root", no_checkout)
    root = make_root(tmp_path)
    assert store.admit_evidence_root(root).path == root
    package = str(Path(store.__file__).resolve().parents[1])
    expect(Failure.ROOT_PROTECTED, lambda: store.admit_evidence_root(package))


def test_directory_change_during_hashing_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory whose identity moves after pass 1 cannot be walked again."""
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    writer.append(off)
    writer.append(tick_for(off))
    directory = run_dir(root) / "records" / "recording_off"

    def touch_directory(_path: str) -> None:
        os.utime(directory, ns=(1, 1))

    monkeypatch.setattr(store, "_after_first_identity_read", touch_directory)
    expect(Failure.SEAL_TREE_CHANGED, writer.seal)


def test_file_growing_between_identity_reads_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bytes beyond the recorded size are never silently accepted."""
    writer, root = open_writer(tmp_path)
    writer.append(header_for(tmp_path, root, OFF))

    def grow(relative_path: str) -> None:
        with (run_dir(root) / relative_path).open("ab") as handle:
            handle.write(b"late")

    monkeypatch.setattr(store, "_after_first_identity_read", grow)
    expect(Failure.FILE_READ_INCOMPLETE, writer.seal)


def test_stream_file_ownership_failure_poisons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stream file that fails its owner check poisons the writer."""
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    writer.append(off)
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    expect(Failure.WRITE_FAILED, lambda: writer.append(tick_for(off)))
    writer.close()
    expect(Failure.WRITER_POISONED, writer.seal)


def test_vanished_header_file_refuses_seal(tmp_path: Path) -> None:
    """A bound header whose file is absent at seal time is refused."""
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    writer.append(off)
    writer.append(tick_for(off))
    (run_dir(root) / "records" / "recording_off" / "header.jsonl").unlink()
    expect(Failure.SEAL_TREE_INVALID, writer.seal)


# ------------------------------------------------------------- repair round


BAD_RUN_IDS: list[object] = ["../escape", "a/b", "..", ".", "/abs/path", "", 1, None, b"x"]


class _FilesystemSpy:
    """Record every filesystem entry point the store could use."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[str] = []
        for module, name in (
            (os, "open"),
            (os, "mkdir"),
            (os, "stat"),
            (os, "lstat"),
            (os, "scandir"),
            (os, "fstat"),
            (os.path, "realpath"),
            (os.path, "isfile"),
            (os.path, "isdir"),
        ):
            monkeypatch.setattr(module, name, self._wrap(name, getattr(module, name)))

    def _wrap(self, name: str, real: typing.Callable[..., object]) -> typing.Callable[..., object]:
        def spy(*args: object, **kwargs: object) -> object:
            self.calls.append(name)
            return real(*args, **kwargs)

        return spy


@pytest.mark.parametrize("bad", BAD_RUN_IDS, ids=[repr(item) for item in BAD_RUN_IDS])
def test_bad_run_ids_refuse_before_any_filesystem_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: object
) -> None:
    """Public verify and read paths validate the run id before touching the filesystem."""
    root = make_root(tmp_path)
    laptop = make_root(tmp_path, "laptop")
    run_id = typing.cast(str, bad)
    spy = _FilesystemSpy(monkeypatch)
    expect(
        Failure.RUN_ID_MISMATCHED,
        lambda: store.verify_retained_tree(root, run_id=run_id, expected_manifest_sha256="0" * 64),
    )
    expect(
        Failure.RUN_ID_MISMATCHED,
        lambda: store.verify_retained_copies(
            root, laptop, run_id=run_id, expected_manifest_sha256="0" * 64
        ),
    )
    expect(
        Failure.RUN_ID_MISMATCHED,
        lambda: reader.read_retained_run(root, run_id=run_id, expected_manifest_sha256="0" * 64),
    )
    assert spy.calls == []


@pytest.mark.parametrize("bad", BAD_RUN_IDS, ids=[repr(item) for item in BAD_RUN_IDS])
def test_internal_run_directory_open_refuses_bad_ids_without_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: object
) -> None:
    """The shared run-directory boundary refuses a bad id before any descriptor open."""
    admitted = store.admit_evidence_root(make_root(tmp_path))
    spy = _FilesystemSpy(monkeypatch)
    expect(
        Failure.RUN_ID_MISMATCHED,
        lambda: store._open_run_directory(  # pyright: ignore[reportPrivateUsage]
            admitted, typing.cast(str, bad), Failure.INVENTORY_MISMATCHED
        ),
    )
    assert spy.calls == []


class _DescriptorLedger:
    """Account every descriptor opened and closed while installed."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.state: dict[int, str] = {}
        self.double_closes: list[int] = []
        real_open = os.open
        real_close = os.close

        def ledger_open(*args: typing.Any, **kwargs: typing.Any) -> int:
            descriptor = real_open(*args, **kwargs)
            self.state[descriptor] = "open"
            return descriptor

        def ledger_close(descriptor: int) -> None:
            if self.state.get(descriptor) == "closed":
                self.double_closes.append(descriptor)
            self.state[descriptor] = "closed"
            real_close(descriptor)

        monkeypatch.setattr(os, "open", ledger_open)
        monkeypatch.setattr(os, "close", ledger_close)
        monkeypatch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, ledger_open})

    @property
    def leaked(self) -> list[int]:
        """Descriptors opened while installed and never closed."""
        return [fd for fd, state in self.state.items() if state == "open"]


def _swap_directory(directory: Path, *, symlink: bool) -> None:
    """Move a directory away, optionally leaving a symlink to it in its place."""
    moved = directory.with_name("moved")
    directory.rename(moved)
    if symlink:
        directory.symlink_to(moved)


@pytest.mark.parametrize("symlink", [False, True], ids=["vanished", "symlink-swap"])
def test_directory_open_failure_during_seal_closes_each_descriptor_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, symlink: bool
) -> None:
    """A directory that vanishes or becomes a symlink mid-seal fails closed, closing once."""
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    writer.append(off)
    writer.append(tick_for(off))
    directory = run_dir(root) / "records" / "recording_off"
    swapped: list[str] = []

    def swap(relative_path: str) -> None:
        if not swapped:
            swapped.append(relative_path)
            _swap_directory(directory, symlink=symlink)

    monkeypatch.setattr(store, "_after_first_identity_read", swap)
    ledger = _DescriptorLedger(monkeypatch)
    expect(Failure.SEAL_TREE_CHANGED, writer.seal)
    assert swapped == ["records/recording_off/header.jsonl"]
    assert ledger.double_closes == []
    assert ledger.leaked == []


@pytest.mark.parametrize("symlink", [False, True], ids=["vanished", "symlink-swap"])
def test_directory_open_failure_during_verify_closes_each_descriptor_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, symlink: bool
) -> None:
    """The same directory failure during verification fails closed, closing once."""
    root, _sealed, _records = write_full_run(tmp_path)
    digest = hashlib.sha256((run_dir(root) / store.MANIFEST_JSON_NAME).read_bytes()).hexdigest()
    directory = run_dir(root) / "records" / "recording_off"
    swapped: list[str] = []

    def swap(relative_path: str) -> None:
        if not swapped and relative_path.startswith("records/recording_off/"):
            swapped.append(relative_path)
            _swap_directory(directory, symlink=symlink)

    monkeypatch.setattr(store, "_after_first_identity_read", swap)
    ledger = _DescriptorLedger(monkeypatch)
    expect(
        Failure.SEAL_TREE_CHANGED,
        lambda: store.verify_retained_tree(root, run_id=RUN_ID, expected_manifest_sha256=digest),
    )
    assert len(swapped) == 1
    assert ledger.double_closes == []
    assert ledger.leaked == []


def test_read_flags_are_non_blocking_and_no_follow() -> None:
    """Evidence reads open non-blocking and no-follow."""
    flags = store._file_read_flags()  # pyright: ignore[reportPrivateUsage]
    assert flags & os.O_NONBLOCK and flags & os.O_NOFOLLOW
    assert flags & (os.O_WRONLY | os.O_RDWR) == 0


def test_fifo_swap_is_refused_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A regular file swapped for a FIFO is refused at once; a bounded rescue proves it."""
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    writer.append(off)
    writer.append(tick_for(off))
    fifo = run_dir(root) / "records" / "recording_off" / "tick.jsonl"
    real_flags = store._file_read_flags  # pyright: ignore[reportPrivateUsage]
    calls: list[int] = []

    def swap_then_flags() -> int:
        calls.append(1)
        if len(calls) == 2:
            fifo.unlink()
            os.mkfifo(fifo, 0o600)
        return real_flags()

    monkeypatch.setattr(store, "_file_read_flags", swap_then_flags)
    finished = threading.Event()
    rescued: list[bool] = []

    def rescue() -> None:
        if finished.wait(5.0):
            return
        try:
            descriptor = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            return
        rescued.append(True)
        os.close(descriptor)

    thread = threading.Thread(target=rescue, daemon=True)
    thread.start()
    try:
        expect(Failure.FILE_CHANGED, writer.seal)
    finally:
        finished.set()
        thread.join(10.0)
    assert rescued == []


class _CountingScan:
    """Delegate one real directory scan, counting yielded entries and closure."""

    opened: typing.ClassVar[list["_CountingScan"]] = []

    def __init__(self, real: typing.Any, *, fail_after: int | None = None) -> None:
        self._real = real
        self._fail_after = fail_after
        self.yielded = 0
        self.closed = False
        _CountingScan.opened.append(self)

    def __enter__(self) -> "_CountingScan":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.closed = True
        self._real.close()

    def __iter__(self) -> "_CountingScan":
        return self

    def __next__(self) -> typing.Any:
        if self._fail_after is not None and self.yielded >= self._fail_after:
            raise OSError("scan failed")
        entry = next(self._real)
        self.yielded += 1
        return entry


def _install_counting_scan(
    monkeypatch: pytest.MonkeyPatch, *, fail_after: int | None = None
) -> list[_CountingScan]:
    """Replace ``os.scandir`` with a counting delegate; return the opened scans."""
    real_scandir = os.scandir
    _CountingScan.opened = []

    def counting(descriptor: int) -> _CountingScan:
        return _CountingScan(real_scandir(descriptor), fail_after=fail_after)

    monkeypatch.setattr(os, "scandir", counting)
    return _CountingScan.opened


def test_enumeration_stops_at_the_bound_without_listing_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Listing is incremental: the bound stops the scan early and every scan is closed."""
    writer, root = open_writer(tmp_path)
    writer.append(header_for(tmp_path, root, OFF))
    directory = run_dir(root) / "records" / "recording_off"
    for index in range(50):
        path = directory / f"extra{index}.jsonl"
        path.write_bytes(b"x")
        os.chmod(path, 0o600)
    monkeypatch.setattr(store, "MAX_MANIFEST_ENTRIES", 1)
    scans = _install_counting_scan(monkeypatch)
    expect(Failure.SEAL_LIMIT_EXCEEDED, writer.seal)
    assert sum(scan.yielded for scan in scans) <= 4
    assert scans and all(scan.closed for scan in scans)


def test_scan_errors_fail_closed_and_close_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listing error mid-scan is a closed tree failure, and the scan is still closed."""
    writer, root = open_writer(tmp_path)
    writer.append(header_for(tmp_path, root, OFF))
    scans = _install_counting_scan(monkeypatch, fail_after=0)
    expect(Failure.SEAL_TREE_INVALID, writer.seal)
    assert scans and all(scan.closed for scan in scans)


def _verified_with(tmp_path: Path, data: bytes) -> store.ColdVerifiedTree:
    """Return a verified tree whose OFF tick file holds exactly ``data``."""
    root, _sealed, _records = write_full_run(tmp_path)
    (run_dir(root) / "records/recording_off/tick.jsonl").write_bytes(data)
    digest = craft_manifest(run_dir(root))
    return store.verify_retained_tree(root, run_id=RUN_ID, expected_manifest_sha256=digest)


def _count_reads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count bytes returned by every subsequent chunk read."""
    real_read = store._read_chunk  # pyright: ignore[reportPrivateUsage]
    counts: list[int] = []

    def counting(descriptor: int, size: int) -> bytes:
        chunk = real_read(descriptor, size)
        counts.append(len(chunk))
        return chunk

    monkeypatch.setattr(store, "_read_chunk", counting)
    return counts


def test_overlong_line_is_refused_before_buffering_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An overlong line stops the re-read early instead of buffering the whole file."""
    tree = _verified_with(tmp_path, b"x" * 10_000)
    monkeypatch.setattr(store, "_READ_CHUNK_BYTES", 64)
    counts = _count_reads(monkeypatch)
    expect(
        Failure.LINE_TOO_LARGE,
        lambda: store.read_verified_lines(
            tree, "records/recording_off/tick.jsonl", max_line_bytes=100
        ),
    )
    assert sum(counts) <= 100 + 64


def test_line_framing_is_exact_across_chunk_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lines split across chunks reassemble exactly; the bound is inclusive of LF."""
    lines = [b"a" * 99, b"", b"b" * 3, b"c" * 50]
    data = b"\n".join(lines) + b"\n"
    tree = _verified_with(tmp_path, data)
    monkeypatch.setattr(store, "_READ_CHUNK_BYTES", 7)
    path = "records/recording_off/tick.jsonl"
    assert store.read_verified_lines(tree, path, max_line_bytes=100) == tuple(lines)
    expect(Failure.LINE_TOO_LARGE, lambda: store.read_verified_lines(tree, path, max_line_bytes=99))


@pytest.mark.parametrize("data", [b"torn", b"", b"ok\ntorn"], ids=["torn", "empty", "tail"])
def test_line_framing_refuses_torn_or_empty_files(tmp_path: Path, data: bytes) -> None:
    """A final line without LF, or an empty file, is malformed."""
    tree = _verified_with(tmp_path, data)
    expect(
        Failure.LINE_MALFORMED,
        lambda: store.read_verified_lines(
            tree, "records/recording_off/tick.jsonl", max_line_bytes=100
        ),
    )


def test_lines_are_returned_only_after_the_digest_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-read bytes that differ from the manifest digest are never returned."""
    tree = _verified_with(tmp_path, b"alpha\nbeta\n")
    real_read = store._read_chunk  # pyright: ignore[reportPrivateUsage]

    def flipping(descriptor: int, size: int) -> bytes:
        chunk = real_read(descriptor, size)
        return chunk.replace(b"alpha", b"alphA")

    monkeypatch.setattr(store, "_read_chunk", flipping)
    expect(
        Failure.FILE_DIGEST_MISMATCHED,
        lambda: store.read_verified_lines(
            tree, "records/recording_off/tick.jsonl", max_line_bytes=100
        ),
    )


def test_aliased_run_directories_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two root paths naming one run directory inode are refused before verification."""
    root, laptop, digest = _two_trees(tmp_path)

    def same_identity(_root: store.ColdAdmittedRoot, _run_id: str) -> tuple[int, int]:
        return 1, 1

    verified: list[str] = []

    def never(*_args: object, **_kwargs: object) -> store.ColdVerifiedTree:
        verified.append("called")
        raise AssertionError

    monkeypatch.setattr(store, "_run_directory_identity", same_identity)
    monkeypatch.setattr(store, "_verify_admitted_tree", never)
    expect(
        Failure.ROOTS_OVERLAP,
        lambda: store.verify_retained_copies(
            root, laptop, run_id=RUN_ID, expected_manifest_sha256=digest
        ),
    )
    assert verified == []


def test_case_alias_of_one_directory_refuses_where_the_filesystem_folds_case(
    tmp_path: Path,
) -> None:
    """On a case-insensitive filesystem two spellings of one root are one copy."""
    root, _sealed, _records = write_full_run(tmp_path, "Pi")
    alias = str(Path(root).with_name("pi"))
    if not os.path.isdir(alias):
        pytest.skip("filesystem is case-sensitive; the patched alias test covers the rule")
    digest = hashlib.sha256((run_dir(root) / store.MANIFEST_JSON_NAME).read_bytes()).hexdigest()
    expect(
        Failure.ROOTS_OVERLAP,
        lambda: store.verify_retained_copies(
            root, alias, run_id=RUN_ID, expected_manifest_sha256=digest
        ),
    )
