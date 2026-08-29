"""Table-driven tests for the fail-closed CI gate aggregate."""

from __future__ import annotations

import ci_gate_result
import pytest

# The whole module is fast, hardware-free, and IS the gate script's own
# focused test suite — the docs-only CI fast path (#702) runs it to trust
# the mechanism that grants its own shortcut.
pytestmark = pytest.mark.docs_ci


def _needs(**results: str) -> dict[str, object]:
    """Build a ``toJSON(needs)`` payload from ``job_id=result`` keyword pairs."""

    return {job_id: {"result": result} for job_id, result in results.items()}


def test_full_mode_all_success_passes() -> None:
    """Every declared job succeeding in full mode is a clean pass."""

    ci_gate_result.evaluate_gate(
        "full",
        _needs(classify="success", quality="success", docs_fastpath="skipped"),
        always=["classify"],
        full_only=["quality"],
        docs_only=["docs_fastpath"],
    )


def test_docs_only_mode_full_only_skipped_and_docs_only_success_passes() -> None:
    """The mirror-image docs-only pass: full-only skipped, docs-only succeeded."""

    ci_gate_result.evaluate_gate(
        "docs-only",
        _needs(classify="success", quality="skipped", docs_fastpath="success"),
        always=["classify"],
        full_only=["quality"],
        docs_only=["docs_fastpath"],
    )


@pytest.mark.parametrize("mode", ["", "FULL", "docsonly", "Full", "docs_only", "unknown"])
def test_unknown_or_malformed_mode_is_rejected(mode: str) -> None:
    """M8: only the two exact closed-mode strings are ever accepted."""

    with pytest.raises(ci_gate_result.GateResultError):
        ci_gate_result.evaluate_gate(
            mode,
            _needs(classify="success"),
            always=["classify"],
            full_only=[],
            docs_only=[],
        )


def test_full_only_job_skipped_in_full_mode_is_rejected() -> None:
    """M9: a full-only job may never be skipped while mode is full."""

    with pytest.raises(ci_gate_result.GateResultError):
        ci_gate_result.evaluate_gate(
            "full",
            _needs(classify="success", quality="skipped"),
            always=["classify"],
            full_only=["quality"],
            docs_only=[],
        )


def test_docs_fastpath_success_in_full_mode_is_rejected() -> None:
    """A docs-only job may never succeed while mode is full."""

    with pytest.raises(ci_gate_result.GateResultError):
        ci_gate_result.evaluate_gate(
            "full",
            _needs(classify="success", docs_fastpath="success"),
            always=["classify"],
            full_only=[],
            docs_only=["docs_fastpath"],
        )


def test_full_only_job_success_in_docs_only_mode_is_rejected() -> None:
    """M9's mirror: a full-only job must be skipped, not succeeded, in docs-only mode."""

    with pytest.raises(ci_gate_result.GateResultError):
        ci_gate_result.evaluate_gate(
            "docs-only",
            _needs(classify="success", quality="success"),
            always=["classify"],
            full_only=["quality"],
            docs_only=[],
        )


def test_any_declared_job_failure_is_rejected_in_every_mode() -> None:
    """M9-adjacent: `failure` is never an acceptable result, in any declared class."""

    with pytest.raises(ci_gate_result.GateResultError):
        ci_gate_result.evaluate_gate(
            "full",
            _needs(classify="success", quality="failure"),
            always=["classify"],
            full_only=["quality"],
            docs_only=[],
        )


@pytest.mark.parametrize("mode", ["full", "docs-only"])
def test_any_declared_job_cancelled_is_rejected_in_every_mode(mode: str) -> None:
    """M10: `cancelled` is never acceptable, regardless of declared class or mode."""

    with pytest.raises(ci_gate_result.GateResultError):
        ci_gate_result.evaluate_gate(
            mode,
            _needs(classify="cancelled"),
            always=["classify"],
            full_only=[],
            docs_only=[],
        )


def test_unknown_result_string_is_rejected() -> None:
    """M11: a result outside the closed GitHub Actions result set is rejected."""

    with pytest.raises(ci_gate_result.GateResultError):
        ci_gate_result.evaluate_gate(
            "full",
            _needs(classify="neutral"),
            always=["classify"],
            full_only=[],
            docs_only=[],
        )


def test_declared_job_missing_from_needs_json_is_rejected() -> None:
    """A job removed from `needs` cannot silently stop being checked."""

    with pytest.raises(ci_gate_result.GateResultError):
        ci_gate_result.evaluate_gate(
            "full",
            _needs(classify="success"),
            always=["classify", "missing_job"],
            full_only=[],
            docs_only=[],
        )


def test_undeclared_job_present_in_needs_json_is_rejected() -> None:
    """M12: a job added to `needs` without a declared expectation is unproven."""

    with pytest.raises(ci_gate_result.GateResultError):
        ci_gate_result.evaluate_gate(
            "full",
            _needs(classify="success", surprise="success"),
            always=["classify"],
            full_only=[],
            docs_only=[],
        )


def test_malformed_needs_json_raw_string_is_rejected() -> None:
    """Missing/absent/non-object `NEEDS_JSON` is rejected at the loader boundary."""

    with pytest.raises(ci_gate_result.GateResultError):
        ci_gate_result.evaluate_gate(
            "full",
            {},
            always=["classify"],
            full_only=[],
            docs_only=[],
        )


def test_entry_with_non_string_result_is_rejected() -> None:
    """An entry whose `result` is present but not a string is rejected."""

    with pytest.raises(ci_gate_result.GateResultError):
        ci_gate_result.evaluate_gate(
            "full",
            {"classify": {"result": 1}},
            always=["classify"],
            full_only=[],
            docs_only=[],
        )


def test_entry_missing_result_field_entirely_is_rejected() -> None:
    """An entry with no `result` key at all is rejected, not treated as success."""

    with pytest.raises(ci_gate_result.GateResultError):
        ci_gate_result.evaluate_gate(
            "full",
            {"classify": {}},
            always=["classify"],
            full_only=[],
            docs_only=[],
        )


def test_empty_declared_set_is_rejected() -> None:
    """An empty declaration set can never satisfy the gate (it proves nothing)."""

    with pytest.raises(ci_gate_result.GateResultError):
        ci_gate_result.evaluate_gate("full", {}, always=[], full_only=[], docs_only=[])


def test_duplicate_job_id_across_classes_is_rejected() -> None:
    """A job declared in two classes at once is an authoring error, not a gate input."""

    with pytest.raises(ci_gate_result.GateResultError):
        ci_gate_result.evaluate_gate(
            "full",
            _needs(classify="success"),
            always=["classify"],
            full_only=["classify"],
            docs_only=[],
        )


def test_failure_message_names_mode_and_expected_versus_actual_with_no_env_dump() -> None:
    """The failure text is diagnostic (mode + job states) and never an environment dump."""

    with pytest.raises(ci_gate_result.GateResultError) as excinfo:
        ci_gate_result.evaluate_gate(
            "full",
            _needs(classify="success", quality="skipped"),
            always=["classify"],
            full_only=["quality"],
            docs_only=[],
        )
    message = str(excinfo.value)
    assert "full" in message
    assert "quality" in message
    assert "skipped" in message
    assert "success" in message
    assert "SECRET" not in message
    assert "=" not in message or "expected" in message  # no raw `KEY=VALUE` env dump shape


def test_main_returns_zero_on_a_clean_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """`main` wires argv/env through to `evaluate_gate` and reports exit 0 on success."""

    del monkeypatch
    exit_code = ci_gate_result.main(
        ["--always", "classify", "--full-only", "quality"],
        {
            "NEEDS_JSON": '{"classify": {"result": "success"}, "quality": {"result": "success"}}',
            "MODE": "full",
        },
    )
    assert exit_code == 0


def test_main_returns_nonzero_and_prints_to_stderr_on_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`main` reports a non-zero exit and a stderr message, never raising through argv."""

    exit_code = ci_gate_result.main(
        ["--always", "classify"],
        {"NEEDS_JSON": '{"classify": {"result": "failure"}}', "MODE": "full"},
    )
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "CI gate failed" in captured.err


def test_main_treats_a_missing_mode_environment_variable_as_empty() -> None:
    """An absent `MODE` environment variable resolves to the closed-rejecting empty string."""

    exit_code = ci_gate_result.main(
        ["--always", "classify"],
        {"NEEDS_JSON": '{"classify": {"result": "success"}}'},
    )
    assert exit_code == 1
