"""Behavioural and governance tests for the inert CI result gate helper."""

from __future__ import annotations

import json
import runpy
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import ci_gate_result as gate
import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "scripts" / "ci_gate_result.py"
_EXPECTED_MANIFESTS = {
    "checks": {
        "always": ("classify", "docs-fastpath", "codecov-upload"),
        "full-only": (
            "quality",
            "pytest-ordinary",
            "pytest-serial",
            "pytest-stress",
            "package",
            "coverage",
        ),
        "docs-only": (),
    },
    "web": {"always": ("classify",), "full-only": ("web-unit-worker",), "docs-only": ()},
    "web-snapshots": {
        "always": ("classify",),
        "full-only": ("web-snapshots-worker",),
        "docs-only": (),
    },
}


def _arguments(
    always: Sequence[str] = (), full_only: Sequence[str] = (), docs_only: Sequence[str] = ()
) -> list[str]:
    """Build repeatable frozen-interface arguments in a deterministic order."""

    arguments: list[str] = []
    for option, job_ids in (
        ("--always", always),
        ("--full-only", full_only),
        ("--docs-only", docs_only),
    ):
        for job_id in job_ids:
            arguments.extend((option, job_id))
    return arguments


def _manifest_arguments(job_name: str) -> list[str]:
    """Build the exact frozen argv for one protected aggregate job."""

    manifest = _EXPECTED_MANIFESTS[job_name]
    return _arguments(
        always=manifest["always"],
        full_only=manifest["full-only"],
        docs_only=manifest["docs-only"],
    )


def _environment(
    mode: str | None, results: Mapping[str, object] | None, job_name: str = "checks"
) -> dict[str, str]:
    """Build the only two environment inputs accepted by the helper."""

    environment = {"GITHUB_JOB": job_name}
    if mode is not None:
        environment["MODE"] = mode
    if results is not None:
        environment["NEEDS_JSON"] = json.dumps(results)
    return environment


def _needs(**results: str) -> dict[str, object]:
    """Build a valid GitHub Actions needs object from job-result strings."""

    return {job_id: {"result": result} for job_id, result in results.items()}


def _manifest_needs(job_name: str, mode: str) -> dict[str, object]:
    """Build the one all-success/skipped needs object accepted by a manifest."""

    results: dict[str, str] = {}
    for job_class, job_ids in _EXPECTED_MANIFESTS[job_name].items():
        expected = "success" if job_class == "always" else "skipped"
        if job_class == "docs-only" and mode == "docs-only":
            expected = "success"
        if job_class == "full-only" and mode == "full":
            expected = "success"
        results.update({job_id: expected for job_id in job_ids})
    return _needs(**results)


def _assert_failure(
    capsys: pytest.CaptureFixture[str], arguments: Sequence[str], environment: Mapping[str, str]
) -> str:
    """Assert the fail-closed exit and bounded expected-versus-actual diagnostic."""

    assert gate.main(arguments, environment) == 1
    output = capsys.readouterr().out
    assert output.startswith("mode=")
    assert "job\texpected\tactual\n" in output
    return output


def test_protected_gate_manifests_are_complete_and_exact() -> None:
    """The trusted helper owns the complete worker classes for every aggregate gate."""

    assert gate._MANIFESTS == _EXPECTED_MANIFESTS  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("manifest", "expected"),
    [
        (
            {
                "checks": {
                    "always": ("classify", "classify"),
                    "full-only": (),
                    "docs-only": (),
                }
            },
            "unique manifest job id\tclassify",
        ),
        (
            {"checks": {"always": (), "full-only": (), "docs-only": ()}},
            "non-empty manifest\tchecks",
        ),
    ],
)
def test_malformed_fixed_manifests_fail_closed_with_bounded_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    manifest: dict[str, dict[str, tuple[str, ...]]],
    expected: str,
) -> None:
    """Duplicate and empty trusted manifests cannot silently exempt a protected gate."""

    monkeypatch.setattr(gate, "_MANIFESTS", manifest)  # pyright: ignore[reportPrivateUsage]
    output = _assert_failure(
        capsys,
        _manifest_arguments("checks"),
        _environment("full", _needs(classify="success")),
    )
    assert expected in output


@pytest.mark.parametrize("job_name", (None, "", "unknown", "quality"))
def test_missing_or_unknown_github_job_fails_closed(
    capsys: pytest.CaptureFixture[str], job_name: str | None
) -> None:
    """A protected aggregate cannot borrow another job's manifest or infer one."""

    environment = _environment("full", _manifest_needs("checks", "full"))
    if job_name is None:
        del environment["GITHUB_JOB"]
    else:
        environment["GITHUB_JOB"] = job_name
    output = _assert_failure(capsys, _manifest_arguments("checks"), environment)
    assert "known GITHUB_JOB" in output


@pytest.mark.parametrize(
    "arguments",
    [
        _arguments(
            always=("classify", "docs-fastpath"),
            full_only=(
                "quality",
                "pytest-ordinary",
                "pytest-serial",
                "pytest-stress",
                "package",
                "coverage",
            ),
        ),
        _arguments(
            always=("classify", "docs-fastpath", "codecov-upload", "quality"),
            full_only=(
                "quality",
                "pytest-ordinary",
                "pytest-serial",
                "pytest-stress",
                "package",
                "coverage",
            ),
        ),
        _arguments(
            always=("classify", "docs-fastpath", "codecov-upload"),
            full_only=(
                "quality",
                "pytest-ordinary",
                "pytest-serial",
                "pytest-stress",
                "package",
                "coverage",
                "quality",
            ),
        ),
    ],
)
def test_argv_cannot_reduce_cross_bind_or_duplicate_the_fixed_manifest(
    capsys: pytest.CaptureFixture[str], arguments: list[str]
) -> None:
    """Pull-request workflow argv is only a checked mirror of trusted manifest bytes."""

    output = _assert_failure(
        capsys, arguments, _environment("full", _manifest_needs("checks", "full"))
    )
    assert "exact manifest argv" in output


@pytest.mark.parametrize("job_name", tuple(_EXPECTED_MANIFESTS))
def test_full_mode_accepts_the_complete_fixed_manifest(job_name: str) -> None:
    """Full mode accepts every complete protected worker manifest, and no other graph."""

    assert (
        gate.main(
            _manifest_arguments(job_name),
            _environment("full", _manifest_needs(job_name, "full"), job_name),
        )
        == 0
    )


def test_docs_only_accepts_the_closed_skip_and_success_matrix() -> None:
    """Docs-only accepts only skipped full-only jobs and successful other jobs."""

    assert (
        gate.main(
            _manifest_arguments("checks"),
            _environment("docs-only", _manifest_needs("checks", "docs-only")),
        )
        == 0
    )


def test_declaration_order_does_not_change_the_accepted_result() -> None:
    """Class flags may be interleaved without changing their per-job requirement."""

    arguments = ["--full-only", "web-unit-worker", "--always", "classify"]
    assert (
        gate.main(
            arguments,
            _environment("docs-only", _manifest_needs("web", "docs-only"), "web"),
        )
        == 0
    )


@pytest.mark.parametrize(
    ("mode", "job_name", "result_overrides", "drop", "extra", "expected"),
    [
        ("", "checks", {}, None, None, "<missing>"),
        (None, "checks", {}, None, None, "<missing>"),
        ("FULL", "checks", {}, None, None, "FULL"),
        ("docsonly", "checks", {}, None, None, "docsonly"),
        ("future", "checks", {}, None, None, "future"),
        (
            "full",
            "web",
            {"web-unit-worker": "skipped"},
            None,
            None,
            "web-unit-worker\tsuccess\tskipped",
        ),
        (
            "full",
            "checks",
            {"codecov-upload": "skipped"},
            None,
            None,
            "codecov-upload\tsuccess\tskipped",
        ),
        (
            "docs-only",
            "web",
            {"web-unit-worker": "success"},
            None,
            None,
            "web-unit-worker\tskipped\tsuccess",
        ),
        (
            "docs-only",
            "checks",
            {"docs-fastpath": "skipped"},
            None,
            None,
            "docs-fastpath\tsuccess\tskipped",
        ),
        ("full", "checks", {"classify": "failure"}, None, None, "classify\tsuccess\tfailure"),
        ("full", "checks", {"classify": "cancelled"}, None, None, "classify\tsuccess\tcancelled"),
        (
            "full",
            "checks",
            {"classify": "unknown"},
            None,
            None,
            "classify\tsuccess, failure, cancelled, or skipped\tunknown",
        ),
        ("full", "checks", {}, "classify", None, "classify\tsuccess\tmissing"),
        ("full", "checks", {}, None, "extra", "extra\tdeclared job\tsuccess"),
    ],
)
def test_every_named_fail_closed_direction_reports_mode_and_expectation(
    capsys: pytest.CaptureFixture[str],
    mode: str | None,
    job_name: str,
    result_overrides: dict[str, str],
    drop: str | None,
    extra: str | None,
    expected: str,
) -> None:
    """Invalid modes, result mismatches, and membership drift cannot green the gate."""

    actual_mode = mode or "full"
    results = _manifest_needs(job_name, actual_mode)
    for job_id, result in result_overrides.items():
        results[job_id] = {"result": result}
    if drop is not None:
        del results[drop]
    if extra is not None:
        results[extra] = {"result": "success"}
    output = _assert_failure(
        capsys, _manifest_arguments(job_name), _environment(mode, results, job_name)
    )
    assert expected in output


@pytest.mark.parametrize(
    "environment",
    [
        {"MODE": "full"},
        {"MODE": "full", "NEEDS_JSON": ""},
        {"MODE": "full", "NEEDS_JSON": "not-json"},
        {"MODE": "full", "NEEDS_JSON": "[]"},
        {"MODE": "full", "NEEDS_JSON": json.dumps({"quality": "success"})},
        {"MODE": "full", "NEEDS_JSON": json.dumps({"quality": {"result": None}})},
        {"MODE": "full", "NEEDS_JSON": json.dumps({"quality": {"result": 1}})},
    ],
)
def test_malformed_needs_inputs_fail_without_echoing_raw_json(
    capsys: pytest.CaptureFixture[str], environment: dict[str, str]
) -> None:
    """Missing and structurally malformed needs values never become an exemption."""

    environment["GITHUB_JOB"] = "checks"
    output = _assert_failure(capsys, _manifest_arguments("checks"), environment)
    assert "not-json" not in output


@pytest.mark.parametrize(
    "arguments",
    [
        _arguments(),
        _arguments(always=("quality", "quality")),
        _arguments(always=("quality",), full_only=("quality",)),
        _arguments(always=("",)),
    ],
)
def test_inconsistent_or_duplicate_declarations_fail_closed(
    capsys: pytest.CaptureFixture[str], arguments: list[str]
) -> None:
    """Workflow argv cannot alter, duplicate, or empty a fixed job manifest."""

    _assert_failure(capsys, arguments, _environment("full", _manifest_needs("checks", "full")))


def test_failure_output_never_dumps_the_environment(capsys: pytest.CaptureFixture[str]) -> None:
    """A diagnostic reports only resolved mode and rows, never unrelated values."""

    environment = _environment("full", _needs(quality="failure"))
    environment["TOP_SECRET"] = "do-not-print"
    output = _assert_failure(capsys, _arguments(always=("quality",)), environment)
    assert "TOP_SECRET" not in output
    assert "do-not-print" not in output


def test_unexpected_exception_keeps_the_nonzero_gate_polarity(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Internal exceptions fail the gate instead of converting uncertainty to success."""

    def explode(*_arguments: object) -> tuple[bool, tuple[tuple[str, str, str], ...]]:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(gate, "_evaluate", explode)
    output = _assert_failure(
        capsys, _arguments(always=("quality",)), _environment("full", _needs(quality="success"))
    )
    assert "<internal>\tsuccessful evaluation\texception" in output


def test_script_execution_uses_the_frozen_environment_and_argument_interface() -> None:
    """The standalone helper works under the active Python interpreter without installation."""

    result = subprocess.run(
        [sys.executable, str(_SCRIPT), *_manifest_arguments("checks")],
        check=False,
        capture_output=True,
        text=True,
        env=_environment("full", _manifest_needs("checks", "full")),
    )
    assert result.returncode == 0
    assert result.stdout == ""


def test_module_entrypoint_preserves_the_closed_nonzero_failure_polarity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The script entrypoint propagates the gate result instead of swallowing failures."""

    monkeypatch.setattr(sys, "argv", [str(_SCRIPT), "--always", "quality"])
    monkeypatch.setenv("MODE", "full")
    monkeypatch.setenv("NEEDS_JSON", json.dumps(_needs(quality="failure")))
    with pytest.raises(SystemExit) as result:
        runpy.run_path(str(_SCRIPT), run_name="__main__")
    assert result.value.code == 1
