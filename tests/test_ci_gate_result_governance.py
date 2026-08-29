"""Governance tests that keep the inert CI result gate safe to trust later."""

from __future__ import annotations

import ast
import stat
import sys
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "scripts" / "ci_gate_result.py"


def test_workflows_remain_inert_and_do_not_reference_the_future_gate_authority() -> None:
    """No current workflow can consume this prerequisite or the trusted-base path."""

    for workflow in sorted((_REPO / ".github" / "workflows").glob("*.y*ml")):
        source = workflow.read_text(encoding="utf-8")
        assert "ci_gate_result" not in source
        assert ".ci-gate-base" not in source
        assert "needs.classify.outputs" not in source
        loaded = yaml.safe_load(source)
        if not isinstance(loaded, dict) or not isinstance(loaded.get("jobs"), dict):
            continue
        for name, job in loaded["jobs"].items():
            if name == "classify" or not isinstance(job, dict):
                continue
            needs = job.get("needs", [])
            values = {needs} if isinstance(needs, str) else set(needs)
            assert "classify" not in values


def test_helper_file_is_present_at_its_exact_regular_repository_path() -> None:
    """Future deletion or replacement with a symlink cannot silently strand trusted-base CI."""

    assert _SCRIPT.is_file()
    assert stat.S_ISREG(_SCRIPT.stat().st_mode)


def _import_roots(source: str) -> set[str]:
    """Return root module names imported by a source file's AST."""

    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            roots.add(node.module.split(".")[0])
    return roots


def test_helper_imports_only_stdlib_and_has_no_template_network_or_environment_dump_surface() -> (
    None
):
    """The inert helper is dependency-free and cannot fetch, spawn, or dump its environment."""

    source = _SCRIPT.read_text(encoding="utf-8")
    roots = _import_roots(source)
    assert roots <= sys.stdlib_module_names | {"__future__"}
    assert "${{" not in source
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
