"""Guards for coverage of CI tooling and flattened script imports."""

from __future__ import annotations

import re
import runpy
import sys
import tomllib
import xml.etree.ElementTree as ElementTree
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


def test_ci_normalizes_coverage_filenames_before_codecov_upload() -> None:
    """CI rewrites local coverage paths before the Codecov action reads them."""
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    pytest_index = workflow.index("- name: Run tests with coverage")
    normalize_index = workflow.index("- name: Normalize coverage filenames for Codecov")
    upload_index = workflow.index("- name: Upload coverage to Codecov")

    assert pytest_index < normalize_index < upload_index
    assert "run: python scripts/tooling_coverage.py coverage.xml" in workflow


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


def test_stale_synthetic_skill_root_fails_registration_guard() -> None:
    """A stale configured skill root fails closed."""
    discovered = (".agents/skills/current/scripts",)
    with pytest.raises(ValueError, match="stale/scripts"):
        tooling_coverage.require_registered_roots(
            discovered,
            {"coverage": (*discovered, ".agents/skills/stale/scripts")},
        )


def test_duplicate_synthetic_skill_root_fails_registration_guard() -> None:
    """A duplicated configured skill root fails closed."""
    discovered = (".agents/skills/current/scripts",)
    with pytest.raises(ValueError, match="current/scripts"):
        tooling_coverage.require_registered_roots(
            discovered,
            {"coverage": (*discovered, *discovered)},
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


def test_synthetic_module_and_package_name_collision_fails_guard(tmp_path: Path) -> None:
    """A module and package collide while root ``__init__`` stays ignored."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "__init__.py").touch()
    (tmp_path / "src" / "shared_name.py").touch()
    package = tmp_path / "scripts" / "shared_name"
    package.mkdir(parents=True)
    (package / "__init__.py").touch()

    with pytest.raises(ValueError, match="shared_name"):
        tooling_coverage.require_unique_module_stems(tmp_path, ("src", "scripts"))


def test_current_flattened_roots_have_unique_module_stems() -> None:
    """Current direct modules from flattened roots do not shadow one another."""
    tooling_coverage.require_unique_module_stems(
        REPO_ROOT, ("src", "scripts", *tooling_coverage.skill_script_roots(REPO_ROOT))
    )


def test_normalize_coverage_xml_rewrites_tooling_filenames(tmp_path: Path) -> None:
    """Rootless tooling names become unique repository-relative XML paths."""
    coverage_xml = _coverage_fixture(
        tmp_path, ("src/roastpilot_agent/app.py", "script_only.py", "skill_only.py")
    )

    tooling_coverage.normalize_coverage_xml(coverage_xml, tmp_path)

    assert _coverage_filenames(coverage_xml) == (
        "src/roastpilot_agent/app.py",
        "scripts/script_only.py",
        ".agents/skills/demo/scripts/skill_only.py",
    )


def test_normalize_coverage_xml_rejects_missing_tooling_match(tmp_path: Path) -> None:
    """An XML filename without a tooling file match fails closed."""
    coverage_xml = _coverage_fixture(tmp_path, ("missing.py",))

    with pytest.raises(ValueError, match="resolve exactly once"):
        tooling_coverage.normalize_coverage_xml(coverage_xml, tmp_path)


def test_normalize_coverage_xml_rejects_ambiguous_tooling_match(tmp_path: Path) -> None:
    """An XML filename shared by tooling roots fails closed."""
    coverage_xml = _coverage_fixture(tmp_path, ("shared.py",))
    (tmp_path / "scripts" / "shared.py").touch()
    (tmp_path / ".agents" / "skills" / "demo" / "scripts" / "shared.py").touch()

    with pytest.raises(ValueError, match="resolve exactly once"):
        tooling_coverage.normalize_coverage_xml(coverage_xml, tmp_path)


def test_normalize_coverage_xml_rejects_unsafe_filename(tmp_path: Path) -> None:
    """Traversal and non-POSIX filenames fail before filesystem resolution."""
    coverage_xml = _coverage_fixture(tmp_path, ("../escape.py",))

    with pytest.raises(ValueError, match="unsafe"):
        tooling_coverage.normalize_coverage_xml(coverage_xml, tmp_path)


def test_normalize_coverage_xml_rejects_a_class_without_filename(tmp_path: Path) -> None:
    """A malformed class entry cannot bypass path verification."""
    coverage_xml = _coverage_fixture(tmp_path, ())
    report = ElementTree.parse(coverage_xml)
    classes = report.find(".//classes")
    assert classes is not None
    ElementTree.SubElement(classes, "class")
    report.write(coverage_xml, encoding="utf-8", xml_declaration=True)

    with pytest.raises(ValueError, match="has no filename"):
        tooling_coverage.normalize_coverage_xml(coverage_xml, tmp_path)


def test_normalize_coverage_xml_rejects_unexpected_prefixed_filename(tmp_path: Path) -> None:
    """Only the app's established repository-relative prefix is permitted."""
    coverage_xml = _coverage_fixture(tmp_path, ("scripts/script_only.py",))

    with pytest.raises(ValueError, match="unexpected prefix"):
        tooling_coverage.normalize_coverage_xml(coverage_xml, tmp_path)


def test_normalize_coverage_xml_rejects_duplicate_final_names(tmp_path: Path) -> None:
    """Duplicate final names cannot silently collapse Codecov evidence."""
    coverage_xml = _coverage_fixture(tmp_path, ("script_only.py", "script_only.py"))

    with pytest.raises(ValueError, match="duplicate final filenames"):
        tooling_coverage.normalize_coverage_xml(coverage_xml, tmp_path)


def test_normalize_coverage_xml_requires_each_tooling_root_with_classes(tmp_path: Path) -> None:
    """A report omitting a populated tooling root fails before upload."""
    coverage_xml = _coverage_fixture(tmp_path, ("src/roastpilot_agent/app.py",))

    with pytest.raises(ValueError, match="missing filenames for tooling root"):
        tooling_coverage.normalize_coverage_xml(coverage_xml, tmp_path)


def test_normalize_coverage_xml_rejects_malformed_or_oversized_input(tmp_path: Path) -> None:
    """Malformed and bounded-size-invalid local artifacts fail closed."""
    malformed = tmp_path / "malformed.xml"
    malformed.write_text("<coverage>", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed"):
        tooling_coverage.normalize_coverage_xml(malformed, tmp_path)

    oversized = tmp_path / "oversized.xml"
    oversized.write_bytes(b"x" * (tooling_coverage.MAX_COVERAGE_XML_BYTES + 1))
    with pytest.raises(ValueError, match="size limit"):
        tooling_coverage.normalize_coverage_xml(oversized, tmp_path)


def test_normalize_coverage_xml_rejects_symlink_candidate(tmp_path: Path) -> None:
    """A symlink candidate never becomes a trusted coverage source path."""
    coverage_xml = _coverage_fixture(tmp_path, ("linked.py",))
    target = tmp_path / "outside.py"
    target.touch()
    (tmp_path / "scripts" / "linked.py").symlink_to(target)

    with pytest.raises(ValueError, match="regular non-symlink"):
        tooling_coverage.normalize_coverage_xml(coverage_xml, tmp_path)


def test_normalize_coverage_xml_cleans_up_after_atomic_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed replacement removes the temporary XML artifact."""
    coverage_xml = _coverage_fixture(tmp_path, ("script_only.py", "skill_only.py"))

    def fail_replace(source: Path, destination: Path) -> None:
        """Simulate a local CI replacement failure."""
        del source, destination
        raise OSError("replace failed")

    monkeypatch.setattr(tooling_coverage.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        tooling_coverage.normalize_coverage_xml(coverage_xml, tmp_path)
    assert not tuple(tmp_path.glob(".coverage-*"))


def test_main_normalizes_coverage_xml_from_current_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CI command entry point normalizes exactly one local report."""
    coverage_xml = _coverage_fixture(tmp_path, ("script_only.py", "skill_only.py"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["tooling_coverage.py", str(coverage_xml)])

    assert tooling_coverage.main() == 0


def test_script_entrypoint_normalizes_coverage_xml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The direct CI script command exits successfully after normalization."""
    coverage_xml = _coverage_fixture(tmp_path, ("script_only.py", "skill_only.py"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["tooling_coverage.py", str(coverage_xml)])

    with pytest.raises(SystemExit) as exit_status:
        runpy.run_path(str(REPO_ROOT / "scripts" / "tooling_coverage.py"), run_name="__main__")

    assert exit_status.value.code == 0


def test_main_rejects_an_invalid_argument_count() -> None:
    """The CI command requires exactly one report path."""
    with pytest.raises(ValueError, match="usage"):
        tooling_coverage.main(())


def _coverage_fixture(tmp_path: Path, filenames: tuple[str, ...]) -> Path:
    """Create local tooling files and a minimal Coverage.py XML report."""
    scripts = tmp_path / "scripts"
    skills = tmp_path / ".agents" / "skills" / "demo" / "scripts"
    scripts.mkdir()
    skills.mkdir(parents=True)
    (scripts / "script_only.py").touch()
    (skills / "skill_only.py").touch()
    coverage_xml = tmp_path / "coverage.xml"
    root = ElementTree.Element("coverage")
    classes = ElementTree.SubElement(ElementTree.SubElement(root, "packages"), "classes")
    for filename in filenames:
        ElementTree.SubElement(classes, "class", filename=filename)
    ElementTree.ElementTree(root).write(coverage_xml, encoding="utf-8", xml_declaration=True)
    return coverage_xml


def _coverage_filenames(coverage_xml: Path) -> tuple[str, ...]:
    """Return class filenames from a local Coverage.py XML report."""
    return tuple(
        class_element.attrib["filename"]
        for class_element in ElementTree.parse(coverage_xml).findall(".//class")
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
