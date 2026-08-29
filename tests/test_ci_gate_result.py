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


def _environment(mode: str | None, results: Mapping[str, object] | None) -> dict[str, str]:
    """Build the only two environment inputs accepted by the helper."""

    environment: dict[str, str] = {}
    if mode is not None:
        environment["MODE"] = mode
    if results is not None:
        environment["NEEDS_JSON"] = json.dumps(results)
    return environment


def _needs(**results: str) -> dict[str, object]:
    """Build a valid GitHub Actions needs object from job-result strings."""

    return {job_id: {"result": result} for job_id, result in results.items()}


def _assert_failure(
    capsys: pytest.CaptureFixture[str], arguments: Sequence[str], environment: Mapping[str, str]
) -> str:
    """Assert the fail-closed exit and bounded expected-versus-actual diagnostic."""

    assert gate.main(arguments, environment) == 1
    output = capsys.readouterr().out
    assert output.startswith("mode=")
    assert "job\texpected\tactual\n" in output
    return output


def test_full_mode_accepts_every_declared_success_and_realistic_eight_job_set() -> None:
    """Full mode accepts a representative declared aggregate dependency graph."""

    jobs = _needs(
        quality="success",
        package="success",
        web="success",
        stress="success",
        serial="success",
        docs="skipped",
        codeql="success",
        codecov="skipped",
    )
    assert (
        gate.main(
            _arguments(
                always=("quality", "package"),
                full_only=("web", "stress", "serial", "codeql"),
                docs_only=("docs", "codecov"),
            ),
            _environment("full", jobs),
        )
        == 0
    )


def test_docs_only_accepts_the_closed_skip_and_success_matrix() -> None:
    """Docs-only accepts only skipped full-only jobs and successful other jobs."""

    assert (
        gate.main(
            _arguments(always=("quality",), full_only=("web",), docs_only=("docs",)),
            _environment("docs-only", _needs(quality="success", web="skipped", docs="success")),
        )
        == 0
    )


def test_declaration_order_does_not_change_the_accepted_result() -> None:
    """Class flags may be interleaved without changing their per-job requirement."""

    arguments = ["--docs-only", "docs", "--always", "quality", "--full-only", "web"]
    assert (
        gate.main(
            arguments,
            _environment("docs-only", _needs(quality="success", web="skipped", docs="success")),
        )
        == 0
    )


@pytest.mark.parametrize(
    ("mode", "arguments", "results", "expected"),
    [
        ("", _arguments(always=("quality",)), _needs(quality="success"), "<missing>"),
        (None, _arguments(always=("quality",)), _needs(quality="success"), "<missing>"),
        ("FULL", _arguments(always=("quality",)), _needs(quality="success"), "FULL"),
        ("docsonly", _arguments(always=("quality",)), _needs(quality="success"), "docsonly"),
        ("future", _arguments(always=("quality",)), _needs(quality="success"), "future"),
        ("full", _arguments(full_only=("web",)), _needs(web="skipped"), "web\tsuccess\tskipped"),
        ("full", _arguments(docs_only=("docs",)), _needs(docs="success"), "docs\tskipped\tsuccess"),
        (
            "docs-only",
            _arguments(full_only=("web",)),
            _needs(web="success"),
            "web\tskipped\tsuccess",
        ),
        (
            "docs-only",
            _arguments(docs_only=("docs",)),
            _needs(docs="skipped"),
            "docs\tsuccess\tskipped",
        ),
        (
            "full",
            _arguments(always=("quality",)),
            _needs(quality="failure"),
            "quality\tsuccess\tfailure",
        ),
        (
            "full",
            _arguments(always=("quality",)),
            _needs(quality="cancelled"),
            "quality\tsuccess\tcancelled",
        ),
        (
            "full",
            _arguments(always=("quality",)),
            _needs(quality="unknown"),
            "quality\tsuccess, failure, cancelled, or skipped\tunknown",
        ),
        ("full", _arguments(always=("quality",)), _needs(), "quality\tsuccess\tmissing"),
        (
            "full",
            _arguments(always=("quality",)),
            _needs(quality="success", extra="success"),
            "extra\tdeclared job\tsuccess",
        ),
    ],
)
def test_every_named_fail_closed_direction_reports_mode_and_expectation(
    capsys: pytest.CaptureFixture[str],
    mode: str | None,
    arguments: list[str],
    results: dict[str, object],
    expected: str,
) -> None:
    """Invalid modes, result mismatches, and membership drift cannot green the gate."""

    output = _assert_failure(capsys, arguments, _environment(mode, results))
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

    output = _assert_failure(capsys, _arguments(always=("quality",)), environment)
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
def test_empty_or_duplicate_declarations_fail_closed(
    capsys: pytest.CaptureFixture[str], arguments: list[str]
) -> None:
    """Every received job needs exactly one non-empty declared class."""

    _assert_failure(capsys, arguments, _environment("full", _needs(quality="success")))


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
        [sys.executable, str(_SCRIPT), "--always", "quality"],
        check=False,
        capture_output=True,
        text=True,
        env=_environment("full", _needs(quality="success")),
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
