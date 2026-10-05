"""Offline assessment with synthetic sealed inputs and external receipts only."""

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, cast

import assess_cold_evidence_retrospectively as assessor
import pytest

from roastpilot_agent.cold_characterisation.temperature_conformance import (
    ColdRevisedConformanceResult,
)
from tests.test_cold_characterisation_duration_policy import current_run
from tests.test_cold_characterisation_evidence_builders import RUN_ID
from tests.test_cold_characterisation_revision1 import P, prepared


def hashes(root: Path) -> dict[str, str]:
    """Fingerprint every synthetic sealed file before and after assessment."""
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def arguments(tmp_path: Path) -> dict[str, str]:
    """Generate synthetic evidence; take the receipt from the successful seal/read chain."""
    run = current_run(tmp_path / "input", prepared)
    (tmp_path / "output").mkdir(mode=0o700)
    return {
        "root": str(tmp_path / "input" / "pi"),
        "run_id": RUN_ID,
        "receipt": run.run.manifest_sha256,
        "output": str(tmp_path / "output" / "assessment.json"),
        "original_candidate_sha256": "a" * 64,
        "evaluator_commit": "b" * 40,
        "evaluator_tree": "c" * 40,
        "evaluator_wheel_sha256": "d" * 64,
        "review_attestations_sha256": "e" * 64,
    }


def test_assessment_recomputes_baseline_preserves_bytes_and_sanitises(tmp_path: Path) -> None:
    """A separate technical result cannot replace the original failure or qualify hardware."""
    args = arguments(tmp_path)
    before = hashes(Path(args["root"]))
    assessor.assess(**args)
    assert hashes(Path(args["root"])) == before
    assert stat.S_IMODE(Path(args["output"]).stat().st_mode) == 0o600
    payload = Path(args["output"]).read_text()
    document = json.loads(payload)
    assert document["original_policy4_baseline"]["outcome"] == "not_conformant"
    assert document["revised_policy4"]["findings"] == []
    assert document["revised_policy4"]["interpretation_revision"] == 1
    assert document["bindings"]["manifest_receipt_sha256"] == args["receipt"]
    assert document["physical_qualification"] == "not_assessed"
    for private in (RUN_ID, args["root"], P, "session-recording", "sk-live", "/synthetic"):
        assert private not in payload
    with pytest.raises(assessor.AssessmentRefusedError, match="^Offline assessment refused.$"):
        assessor.assess(**args)
    assert Path(args["output"]).read_text() == payload


@pytest.mark.parametrize("case", ["receipt", "altered", "inside", "symlink", "digest", "parent"])
def test_assessment_refuses_untrusted_receipt_bytes_or_output(tmp_path: Path, case: str) -> None:
    """Alteration, self-output and overwrite paths refuse without leaking private input."""
    args = arguments(tmp_path)
    if case == "receipt":
        args["receipt"] = "0" * 64
    elif case == "altered":
        target = next((Path(args["root"]) / RUN_ID / "records").rglob("*.jsonl"))
        target.write_bytes(target.read_bytes() + b"\n")
    elif case == "inside":
        args["output"] = str(Path(args["root"]) / RUN_ID / "assessment.json")
    elif case == "symlink":
        Path(args["output"]).symlink_to(Path(args["root"]) / RUN_ID / "manifest.json")
    elif case == "digest":
        args["evaluator_commit"] = "private-identifier"
    else:
        args["output"] = str(tmp_path / "missing-parent" / "assessment.json")
    before = hashes(Path(args["root"]))
    with pytest.raises(assessor.AssessmentRefusedError, match="^Offline assessment refused.$"):
        assessor.assess(**args)
    assert hashes(Path(args["root"])) == before


def test_cli_sanitises_failures_and_reports_technical_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI results contain no retained paths, identifiers, reasons or exception text."""
    args = arguments(tmp_path)
    argv = [item for key, value in args.items() for item in ("--" + key.replace("_", "-"), value)]
    assert assessor.main(argv) == 0
    assert capsys.readouterr().out == (
        "Offline technical assessment written; physical qualification not assessed.\n"
    )
    assert assessor.main(argv) == 1
    assert capsys.readouterr().out == "Offline assessment refused.\n"


def test_bad_cli_arguments_never_echo_private_values(capsys: pytest.CaptureFixture[str]) -> None:
    """Argparse errors share the fixed refusal channel."""
    assert assessor.main(["--private-run-id", "private-secret-value"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "Offline assessment refused.\n"
    assert captured.err == ""


@pytest.mark.parametrize("failure", ["none", "publication", "persistent"])
def test_publication_syncs_parent_after_checks_and_durably_cleans_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    """Directory sync gates success; failed publication unlinks and retries that sync."""
    args = arguments(tmp_path)
    output = Path(args["output"])
    parent_identity = output.parent.stat()
    before = hashes(Path(args["root"]))
    original_sync = os.fsync
    original_check = cast(Any, assessor)._recheck_parent  # White-box publication boundary spy.
    original_identity = cast(Any, assessor)._identity
    events: list[str] = []

    class ObservedFileIdentity(tuple[int, int]):
        """Observe the actual named-file comparison against the held file inode."""

        def __ne__(self, other: object) -> bool:
            """Record the final comparison before any parent sync can succeed."""
            unequal = super().__ne__(other)
            assert events == ["file-sync", "final-check"]
            assert other == tuple(self)
            events.append("named-inode-comparison")
            return unequal

    def identity(fd: int) -> tuple[int, int]:
        value = original_identity(fd)
        if stat.S_ISREG(os.fstat(fd).st_mode):
            return ObservedFileIdentity(value)
        return value

    def check(parent: Path, descriptors: list[int], source: tuple[int, int]) -> None:
        original_check(parent, descriptors, source)
        if events:
            events.append("final-check")

    def sync(fd: int) -> None:
        metadata = os.fstat(fd)
        if stat.S_ISREG(metadata.st_mode):
            assert stat.S_IMODE(metadata.st_mode) == 0o600
            events.append("file-sync")
        else:
            assert stat.S_ISDIR(metadata.st_mode)
            assert (metadata.st_dev, metadata.st_ino) == (
                parent_identity.st_dev,
                parent_identity.st_ino,
            )
            assert events[:3] == ["file-sync", "final-check", "named-inode-comparison"]
            intermediate = capsys.readouterr()
            assert intermediate.out == ""
            assert intermediate.err == ""
            if output.exists():
                events.append("parent-sync")
                if failure != "none":
                    raise OSError("private-sync-error")
            else:
                events.append("cleanup-sync")
                if failure == "persistent":
                    raise OSError("private-cleanup-error")
        original_sync(fd)

    monkeypatch.setattr(assessor, "_recheck_parent", check)
    monkeypatch.setattr(assessor, "_identity", identity)
    monkeypatch.setattr(os, "fsync", sync)
    argv = [item for key, value in args.items() for item in ("--" + key.replace("_", "-"), value)]
    assert assessor.main(argv) == (0 if failure == "none" else 1)
    captured = capsys.readouterr()
    assert captured.err == ""
    if failure == "none":
        assert events == ["file-sync", "final-check", "named-inode-comparison", "parent-sync"]
        assert output.is_file()
        assert captured.out == (
            "Offline technical assessment written; physical qualification not assessed.\n"
        )
    else:
        assert events == [
            "file-sync",
            "final-check",
            "named-inode-comparison",
            "parent-sync",
            "cleanup-sync",
        ]
        assert not output.exists()
        assert captured.out == "Offline assessment refused.\n"
    assert hashes(Path(args["root"])) == before


def test_script_entry_point_is_in_process_and_sanitised(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Execute the script guard without spawning a process or accessing evidence."""
    import runpy
    import sys

    monkeypatch.setattr(sys, "argv", [assessor.__file__])
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(assessor.__file__, run_name="__main__")
    assert stopped.value.code == 1
    assert capsys.readouterr().out == "Offline assessment refused.\n"


def test_case_alias_cannot_place_output_in_sealed_tree(tmp_path: Path) -> None:
    """Case-insensitive filesystem identities override lexical path separation."""
    args = arguments(tmp_path)
    source = Path(args["root"])
    alias = source.with_name(source.name.upper())
    if not alias.exists() or not alias.samefile(source):
        pytest.skip("Filesystem does not provide case-insensitive aliases")
    args["output"] = str(alias / RUN_ID / "assessment.json")
    before = hashes(source)
    with pytest.raises(assessor.AssessmentRefusedError, match="^Offline assessment refused.$"):
        assessor.assess(**args)
    assert hashes(source) == before
    assert not Path(args["output"]).exists()


@pytest.mark.parametrize("case", ["parent-link", "ancestor-link", "traversal"])
def test_output_ancestry_refuses_links_and_traversal(tmp_path: Path, case: str) -> None:
    """Every output component is opened without following links."""
    args = arguments(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    (real / "child").mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    args["output"] = str(
        alias / "assessment.json"
        if case == "parent-link"
        else alias / "child" / "assessment.json"
        if case == "ancestor-link"
        else real / ".." / "assessment.json"
    )
    before = hashes(Path(args["root"]))
    with pytest.raises(assessor.AssessmentRefusedError, match="^Offline assessment refused.$"):
        assessor.assess(**args)
    assert hashes(Path(args["root"])) == before
    assert list(real.rglob("*.json")) == []
    assert not (tmp_path / "assessment.json").exists()


@pytest.mark.parametrize("component", ["parent", "ancestor", "into-evidence", "symlink"])
def test_destination_swap_during_assessment_refuses_without_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: str
) -> None:
    """Replace an admitted parent/ancestor deterministically during evaluation."""
    args = arguments(tmp_path)
    ancestor = tmp_path / "destination"
    parent = ancestor / "child"
    parent.mkdir(mode=0o700, parents=True)
    args["output"] = str(parent / "assessment.json")
    source = Path(args["root"])
    before = hashes(source)
    original = assessor.check_revised_conformance

    def swap(retained: object) -> ColdRevisedConformanceResult:
        result = original(retained)
        target = ancestor if component == "ancestor" else parent
        moved = source / "moved" if component == "into-evidence" else tmp_path / "moved"
        target.rename(moved)
        if component == "symlink":
            target.symlink_to(source, target_is_directory=True)
        else:
            target.mkdir(mode=0o700)
            if component == "ancestor":
                (target / "child").mkdir(mode=0o700)
        return result

    monkeypatch.setattr(assessor, "check_revised_conformance", swap)
    with pytest.raises(assessor.AssessmentRefusedError, match="^Offline assessment refused.$"):
        assessor.assess(**args)
    assert hashes(source) == before
    assert not list(tmp_path.rglob("assessment.json"))
    assert not list(tmp_path.rglob(".cold-assessment-*"))


def test_sealed_evidence_mutation_after_admission_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The final reader detects mutation after both evaluations admitted the input."""
    args = arguments(tmp_path)
    target = next((Path(args["root"]) / RUN_ID / "records").rglob("*.jsonl"))
    original = assessor.check_revised_conformance
    evaluated = False

    def mutate(retained: object) -> ColdRevisedConformanceResult:
        nonlocal evaluated
        result = original(retained)
        assert result.findings == ()
        target.write_bytes(target.read_bytes() + b"\n")
        evaluated = True
        return result

    monkeypatch.setattr(assessor, "check_revised_conformance", mutate)
    with pytest.raises(assessor.AssessmentRefusedError, match="^Offline assessment refused.$"):
        assessor.assess(**args)
    assert evaluated
    assert not Path(args["output"]).exists()
    assert not list(tmp_path.rglob(".cold-assessment-*"))


@pytest.mark.parametrize("stage", ["write", "relocate", "substitute-file", "substitute-link"])
def test_direct_publication_failure_cleans_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """Hold the written inode; refuse detected late relocation or name substitution."""
    args = arguments(tmp_path)
    parent = Path(args["output"]).parent
    output = Path(args["output"])
    source = Path(args["root"])
    moved = source / "moved-output"
    original_sync = os.fsync
    original = source / RUN_ID / "manifest.json"
    before = hashes(source)
    reached_boundary = False

    def sync(fd: int) -> None:
        nonlocal reached_boundary
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            original_sync(fd)
            return
        # This boundary follows write/flush and immediately precedes the final
        # location checks, while the final output descriptor is still held.
        assert output.is_file()
        reached_boundary = True
        if stage == "write":
            raise OSError("private-path-or-error")
        original_sync(fd)
        if stage == "relocate":
            parent.rename(moved)
            parent.mkdir(mode=0o700)
        elif stage == "substitute-file":
            output.unlink()
            output.write_bytes(b"substituted bytes")
        else:
            output.unlink()
            output.symlink_to(original)

    monkeypatch.setattr(os, "fsync", sync)
    with pytest.raises(assessor.AssessmentRefusedError, match="^Offline assessment refused.$"):
        assessor.assess(**args)
    assert reached_boundary
    assert hashes(source) == before
    assert not list(tmp_path.rglob("assessment.json"))
    assert not list(tmp_path.rglob(".cold-assessment-*"))


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o710, 0o770, 0o777, 0o1700])
def test_output_parent_must_be_private(tmp_path: Path, mode: int) -> None:
    """The caller's output parent cannot grant any group/other access or special bits."""
    args = arguments(tmp_path)
    parent = Path(args["output"]).parent
    parent.chmod(mode)
    before = hashes(Path(args["root"]))
    with pytest.raises(assessor.AssessmentRefusedError, match="^Offline assessment refused.$"):
        assessor.assess(**args)
    assert hashes(Path(args["root"])) == before
    assert not Path(args["output"]).exists()


def test_output_parent_must_belong_to_current_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A synthetic mismatched caller UID refuses before evidence is read or written."""
    args = arguments(tmp_path)
    actual = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: actual + 1)
    with pytest.raises(assessor.AssessmentRefusedError, match="^Offline assessment refused.$"):
        assessor.assess(**args)
    assert not Path(args["output"]).exists()


@pytest.mark.parametrize("mode", [0o770, 0o777, 0o1777])
def test_other_writable_output_ancestor_is_refused(tmp_path: Path, mode: int) -> None:
    """Group/world writers are refused; sticky alone does not grant the exception."""
    args = arguments(tmp_path)
    ancestor = tmp_path / "shared"
    parent = ancestor / "private"
    parent.mkdir(mode=0o700, parents=True)
    ancestor.chmod(mode)
    args["output"] = str(parent / "assessment.json")
    if os.getuid() == 0 and mode == 0o1777:
        pytest.skip("Root-owned sticky directories are explicitly admitted")
    before = hashes(Path(args["root"]))
    with pytest.raises(assessor.AssessmentRefusedError, match="^Offline assessment refused.$"):
        assessor.assess(**args)
    assert hashes(Path(args["root"])) == before
    assert not Path(args["output"]).exists()


def test_parent_permissions_are_rechecked_after_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Permission drift observed at the last check refuses and removes partial output."""
    args = arguments(tmp_path)
    parent = Path(args["output"]).parent
    original_sync = os.fsync

    def sync(fd: int) -> None:
        original_sync(fd)
        parent.chmod(0o755)

    monkeypatch.setattr(os, "fsync", sync)
    with pytest.raises(assessor.AssessmentRefusedError, match="^Offline assessment refused.$"):
        assessor.assess(**args)
    assert not Path(args["output"]).exists()


@pytest.mark.parametrize("owner", ["root", "current", "foreign"])
@pytest.mark.parametrize("timing", ["admission", "after-write"])
def test_output_ancestor_ownership_is_admitted_and_rechecked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: str, timing: str
) -> None:
    """Synthetic foreign ownership refuses even a non-writable ancestor, without chown."""
    args = arguments(tmp_path)
    ancestor = tmp_path / "ancestor"
    parent = ancestor / "private"
    parent.mkdir(mode=0o700, parents=True)
    ancestor.chmod(0o755)
    args["output"] = str(parent / "assessment.json")
    identity = ancestor.stat()
    uid = {"root": 0, "current": os.getuid(), "foreign": os.getuid() + 1}[owner]
    original_stat = os.fstat
    original_sync = os.fsync
    active = timing == "admission"
    observed = False

    def fstat(fd: int) -> os.stat_result:
        nonlocal observed
        metadata = original_stat(fd)
        if active and (metadata.st_dev, metadata.st_ino) == (identity.st_dev, identity.st_ino):
            observed = True
            fields = list(metadata)
            fields[4] = uid  # st_uid in the portable stat_result tuple
            return os.stat_result(fields)
        return metadata

    def sync(fd: int) -> None:
        nonlocal active
        original_sync(fd)
        active = True

    monkeypatch.setattr(os, "fstat", fstat)
    monkeypatch.setattr(os, "fsync", sync)
    source = Path(args["root"])
    before = hashes(source)
    if owner == "foreign":
        with pytest.raises(assessor.AssessmentRefusedError, match="^Offline assessment refused.$"):
            assessor.assess(**args)
        assert not Path(args["output"]).exists()
    else:
        assessor.assess(**args)
        assert json.loads(Path(args["output"]).read_text())["revised_policy4"]["findings"] == []
        assert stat.S_IMODE(Path(args["output"]).stat().st_mode) == 0o600
    assert observed
    assert hashes(source) == before
    assert not list(tmp_path.rglob(".cold-assessment-*"))
