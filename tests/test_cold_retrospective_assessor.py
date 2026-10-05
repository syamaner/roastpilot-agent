"""Offline assessment with synthetic sealed inputs and external receipts only."""

import hashlib
import json
from pathlib import Path

import assess_cold_evidence_retrospectively as assessor
import pytest

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
    return {
        "root": str(tmp_path / "input" / "pi"),
        "run_id": RUN_ID,
        "receipt": run.run.manifest_sha256,
        "output": str(tmp_path / "assessment.json"),
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
