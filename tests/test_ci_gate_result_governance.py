"""Governance tests that keep the trusted-base CI gate/classifier files safe.

D180 (#702, slice 3) replaces the slice-1/2a inertness guards — "no job may
consume this yet" — with equally mechanical CONSUMPTION guards now that
``ci_gate_result.py`` and ``ci_change_classifier.py`` are both live,
base-trusted protected authority (see D180 §2.9). Nothing here is deleted:
each guard below is the direct, stronger replacement of its slice-1/2a
predecessor, and the file-presence/stdlib-import guards are extended to
cover both protected scripts rather than just one.
"""

from __future__ import annotations

import ast
import shutil
import stat
import sys
from pathlib import Path
from typing import cast

import ci_gate_result as gate_result
import pytest
import yaml

_REPO = Path(__file__).resolve().parents[1]
_GATE_SCRIPT = _REPO / "scripts" / "ci_gate_result.py"
_CLASSIFIER_SCRIPT = _REPO / "scripts" / "ci_change_classifier.py"
_PROTECTED_SCRIPTS = (_GATE_SCRIPT, _CLASSIFIER_SCRIPT)
_BASE_REF_EXPRESSION = (
    "${{ github.event_name == 'pull_request' && github.event.pull_request.base.sha || github.sha }}"
)
_GATE_JOB_NAMES = frozenset({"checks", "web", "web-snapshots"})


@pytest.mark.parametrize(
    ("mode", "expected"),
    (("full", "skipped"), ("docs-only", "success")),
)
def test_expected_result_maps_docs_only_jobs_in_both_modes(mode: str, expected: str) -> None:
    """Docs-only jobs skip in full mode and must succeed in docs-only mode."""
    assert gate_result._expected_result(mode, "docs-only") == expected  # pyright: ignore[reportPrivateUsage]


def _load_workflow(path: Path) -> dict[str, object]:
    """Load one workflow file as a structural mapping."""

    loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return cast(dict[str, object], loaded)


def _step_mapping(step: object) -> dict[str, object] | None:
    """Narrow one workflow step to a mapping, or ``None`` if it is not one."""

    return cast(dict[str, object], step) if isinstance(step, dict) else None


def _is_checkout_step(step: dict[str, object]) -> bool:
    """Return whether a step invokes ``actions/checkout``."""

    uses = step.get("uses")
    return isinstance(uses, str) and uses.startswith("actions/checkout@")


def _is_base_checkout_step(step: dict[str, object]) -> bool:
    """Return whether a checkout step targets the ``.ci-gate-base`` path."""

    with_block = step.get("with")
    return (
        _is_checkout_step(step)
        and isinstance(with_block, dict)
        and cast(dict[str, object], with_block).get("path") == ".ci-gate-base"
    )


def _job_steps(job: dict[str, object]) -> list[dict[str, object]]:
    """Return one job's steps as mappings, skipping any non-mapping entries."""

    steps = job.get("steps")
    if not isinstance(steps, list):
        return []
    mapped = (_step_mapping(step) for step in cast(list[object], steps))
    return [step for step in mapped if step is not None]


@pytest.mark.docs_ci
def test_every_protected_script_invocation_is_base_trusted() -> None:
    """D180 §2.9 replacement (row 2): every reference is `.ci-gate-base/`-prefixed.

    Replaces the slice-1/2a inertness assertion (no workflow may reference
    ``ci_gate_result``, ``.ci-gate-base``, or ``needs.classify.outputs``) with
    a trusted-base consumption guard: every occurrence of either protected
    script's filename in any workflow is prefixed by ``.ci-gate-base/``, every
    ``.ci-gate-base`` checkout uses the exact D180 §2.2 ``ref`` expression with
    ``persist-credentials: false``, each of the three gate jobs contains
    exactly one checkout step and it is the base one, and the classify job's
    step order is head-checkout, base-checkout, run.
    """

    for workflow in sorted((_REPO / ".github" / "workflows").glob("*.y*ml")):
        source = workflow.read_text(encoding="utf-8")
        for script in _PROTECTED_SCRIPTS:
            required_reference = f".ci-gate-base/scripts/{script.name}"
            index = source.find(script.name)
            while index != -1:
                prefix_start = index - len(required_reference) + len(script.name)
                prefix = source[max(0, prefix_start) : index + len(script.name)]
                assert prefix == required_reference, (
                    f"{workflow.name}: bare (non-base-trusted) reference to {script.name}"
                )
                index = source.find(script.name, index + 1)

        loaded = _load_workflow(workflow)
        jobs = loaded.get("jobs")
        if not isinstance(jobs, dict):
            continue
        for name, raw_job in cast(dict[str, object], jobs).items():
            if not isinstance(raw_job, dict):
                continue
            job = cast(dict[str, object], raw_job)
            steps = _job_steps(job)
            base_checkouts = [step for step in steps if _is_base_checkout_step(step)]
            for checkout in base_checkouts:
                with_block = cast(dict[str, object], checkout["with"])
                assert with_block.get("ref") == _BASE_REF_EXPRESSION, (
                    f"{workflow.name}:{name}: base checkout has an unexpected ref expression"
                )
                assert with_block.get("persist-credentials") is False, (
                    f"{workflow.name}:{name}: base checkout must set persist-credentials: false"
                )
            if name in _GATE_JOB_NAMES and workflow.name == "ci.yml":
                checkout_steps = [step for step in steps if _is_checkout_step(step)]
                assert len(checkout_steps) == 1, (
                    f"{name}: a gate job must have exactly one checkout"
                )
                assert checkout_steps[0] in base_checkouts, (
                    f"{name}: the gate job's one checkout must be the base checkout"
                )
            if name == "classify":
                checkout_steps = [step for step in steps if _is_checkout_step(step)]
                assert len(checkout_steps) == 2, "classify: expected exactly two checkout steps"
                head_checkout, base_checkout = checkout_steps
                assert head_checkout not in base_checkouts, (
                    "classify: step order must be head-checkout, base-checkout, run"
                )
                assert base_checkout in base_checkouts, (
                    "classify: step order must be head-checkout, base-checkout, run"
                )
                assert "run" in steps[-1], "classify: the final step must be the run step"


@pytest.mark.docs_ci
def test_protected_script_files_are_present_at_their_exact_regular_repository_paths() -> None:
    """Future deletion or replacement with a symlink cannot silently strand trusted-base CI."""

    for script in _PROTECTED_SCRIPTS:
        metadata = script.lstat()
        assert not script.is_symlink()
        assert stat.S_ISREG(metadata.st_mode)


@pytest.mark.serial
@pytest.mark.parametrize("script", _PROTECTED_SCRIPTS, ids=lambda script: script.name)
def test_protected_script_presence_rejects_a_symlink_mutation(tmp_path: Path, script: Path) -> None:
    """A real protected-script symlink fails closed, then restores from scratch only."""

    scratch_copy = tmp_path / script.name
    shutil.copy2(script, scratch_copy)
    try:
        script.unlink()
        script.symlink_to(scratch_copy)
        with pytest.raises(AssertionError):
            test_protected_script_files_are_present_at_their_exact_regular_repository_paths()
    finally:
        script.unlink(missing_ok=True)
        shutil.copy2(scratch_copy, script)


def _import_roots(source: str) -> set[str]:
    """Return root module names imported by a source file's AST."""

    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.docs_ci
@pytest.mark.parametrize("script", _PROTECTED_SCRIPTS, ids=lambda script: script.name)
def test_protected_scripts_import_only_the_standard_library(script: Path) -> None:
    """Both base-trusted scripts are dependency-free (D180 §2.9 replacement, row 3)."""

    source = script.read_text(encoding="utf-8")
    roots = _import_roots(source)
    assert roots <= sys.stdlib_module_names | {"__future__"}
    assert "${{" not in source


def test_gate_helper_has_no_subprocess_network_or_environment_dump_surface() -> None:
    """The aggregate gate helper stays a pure evaluator: no process, no network, no env dump."""

    source = _GATE_SCRIPT.read_text(encoding="utf-8")
    roots = _import_roots(source)
    assert "subprocess" not in roots
    assert not ({"socket", "urllib", "http", "requests"} & roots)
    tree = ast.parse(source)
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and isinstance(node.func.value.value, ast.Name)
        and node.func.value.value.id == "os"
        and node.func.value.attr == "environ"
        and node.func.attr in {"items", "values", "keys", "copy"}
        for node in ast.walk(tree)
    )
