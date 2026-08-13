"""Guards for coverage of CI tooling and flattened script imports."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import cast

import pytest
import tooling_coverage
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = "roastpilot_agent"
TOOLING_ROOTS = tooling_coverage.TOOLING_ROOTS
EXPECTED_SOURCES = (APP_SOURCE, *TOOLING_ROOTS)
APP_CODECOV_PATH = "src/roastpilot_agent/"
TOOLING_CODECOV_PATHS = ("scripts/", "\\.agents/skills/")


def _pyproject() -> dict[str, object]:
    """Load the repository's Pyproject configuration."""
    with (REPO_ROOT / "pyproject.toml").open("rb") as config_file:
        return cast(dict[str, object], tomllib.load(config_file))


def _coverage_sources(config: dict[str, object]) -> tuple[str, ...]:
    """Read the configured coverage roots as a tuple."""
    tool = _mapping(config["tool"])
    coverage = _mapping(tool["coverage"])
    run = _mapping(coverage["run"])
    source = run["source"]
    return _strings(source)


def _workflow_cov_values() -> tuple[str, ...]:
    """Return the CI coverage roots from the pytest invocation."""
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    command = re.search(r"python -m pytest (?P<arguments>[^\n]+)", workflow)
    assert command is not None
    return tuple(re.findall(r"--cov=([^\s]+)", command.group("arguments")))


def _codecov() -> dict[str, object]:
    """Load the Codecov configuration."""
    return _mapping(yaml.safe_load((REPO_ROOT / "codecov.yml").read_text(encoding="utf-8")))


def _status(config: dict[str, object], status_type: str, name: str) -> dict[str, object]:
    """Return one named Codecov status configuration."""
    coverage = _mapping(config["coverage"])
    statuses = _mapping(coverage["status"])
    status_group = _mapping(statuses[status_type])
    return _mapping(status_group[name])


def _paths(status: dict[str, object]) -> tuple[str, ...]:
    """Return the paths from a Codecov status."""
    return _strings(status["paths"])


def test_coverage_sources_and_ci_values_are_exactly_in_parity() -> None:
    """Coverage configuration and CI must collect exactly the same roots."""
    sources = _coverage_sources(_pyproject())
    ci_sources = _workflow_cov_values()

    assert sources == EXPECTED_SOURCES
    assert ci_sources == sources


def test_each_discovered_skill_root_is_registered_in_flattened_settings() -> None:
    """Every skill scripts directory must be covered and importable everywhere."""
    config = _pyproject()
    tool = _mapping(config["tool"])
    pytest_config = _mapping(tool["pytest"])
    pytest_options = _mapping(pytest_config["ini_options"])
    pyright = _mapping(tool["pyright"])
    discovered = tooling_coverage.skill_script_roots(REPO_ROOT)

    configured: dict[str, Iterable[str]] = {
        "coverage": _coverage_sources(config),
        "pytest pythonpath": _strings(pytest_options["pythonpath"]),
        "Pyright include": _strings(pyright["include"]),
        "Pyright extraPaths": _strings(pyright["extraPaths"]),
    }
    tooling_coverage.require_registered_roots(discovered, configured)


def test_missing_synthetic_skill_root_fails_registration_guard() -> None:
    """A newly discovered but unregistered skill root fails closed."""
    with pytest.raises(ValueError, match="synthetic/scripts"):
        tooling_coverage.require_registered_roots(
            (".agents/skills/synthetic/scripts",),
            {"coverage": EXPECTED_SOURCES},
        )


def test_missing_skill_script_roots_fail_discovery(tmp_path: Path) -> None:
    """A repository without any skill script roots fails closed."""
    with pytest.raises(ValueError, match="no .agents/skills"):
        tooling_coverage.skill_script_roots(tmp_path)


def test_codecov_keeps_default_statuses_app_scoped_and_adds_tooling() -> None:
    """The required defaults stay app-only while tooling has named statuses."""
    config = _codecov()
    default_patch = _status(config, "patch", "default")
    default_project = _status(config, "project", "default")

    assert _paths(default_patch) == (APP_CODECOV_PATH,)
    assert default_patch["if_not_found"] == "success"
    assert default_patch["threshold"] == "2%"
    assert _paths(default_project) == (APP_CODECOV_PATH,)
    assert default_project["if_not_found"] == "success"
    assert default_project["threshold"] == "1%"
    assert _paths(_status(config, "patch", "tooling")) == TOOLING_CODECOV_PATHS
    tooling_project = _status(config, "project", "tooling")
    assert _paths(tooling_project) == TOOLING_CODECOV_PATHS
    assert tooling_project["informational"] is True


def test_synthetic_module_stem_collision_fails_guard(tmp_path: Path) -> None:
    """Flattened roots reject duplicate module names instead of warning."""
    for root in ("src", "scripts"):
        directory = tmp_path / root
        directory.mkdir(parents=True)
        (directory / "shared_name.py").touch()

    with pytest.raises(ValueError, match="shared_name"):
        tooling_coverage.require_unique_module_stems(tmp_path, ("src", "scripts"))


def test_current_flattened_roots_have_unique_module_stems() -> None:
    """Current direct modules from flattened roots do not shadow one another."""
    tooling_coverage.require_unique_module_stems(
        REPO_ROOT, ("src", "scripts", *tooling_coverage.skill_script_roots(REPO_ROOT))
    )


def _strings(value: object) -> tuple[str, ...]:
    """Narrow a TOML list to strings for configuration checks."""
    assert isinstance(value, list)
    values = cast(list[object], value)
    assert all(isinstance(item, str) for item in values)
    return tuple(cast(str, item) for item in values)


def _mapping(value: object) -> dict[str, object]:
    """Narrow an untyped parsed configuration mapping."""
    assert isinstance(value, dict)
    return cast(dict[str, object], value)
