"""Fail-closed helpers for the tooling coverage configuration guards."""

from __future__ import annotations

import os
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath

TOOLING_ROOTS = ("scripts", ".agents/skills/capture-agent-usage/scripts")
APP_COVERAGE_ROOT = "src/roastpilot_agent/"
MAX_COVERAGE_XML_BYTES = 10 * 1024 * 1024


def skill_script_roots(repo_root: Path) -> tuple[str, ...]:
    """Return every skill script root relative to ``repo_root``.

    Raises:
        ValueError: If no skill script roots exist.
    """
    _require_non_symlink_root(repo_root, ".agents/skills")
    roots: list[str] = []
    for path in sorted((repo_root / ".agents" / "skills").glob("*/scripts")):
        relative = path.relative_to(repo_root).as_posix()
        _require_non_symlink_root(repo_root, relative)
        if path.is_dir():
            roots.append(relative)
    if not roots:
        raise ValueError("no .agents/skills/*/scripts roots found")
    return tuple(roots)


def _require_non_symlink_root(repo_root: Path, root: str) -> None:
    """Reject a tooling root reached through a symlinked repository component."""
    if repo_root.is_symlink():
        raise ValueError("tooling root must not traverse a symlink")
    component = repo_root
    for part in PurePosixPath(root).parts:
        component /= part
        if component.is_symlink():
            raise ValueError("tooling root must not traverse a symlink")


def require_registered_roots(
    discovered_skill_roots: Sequence[str],
    configured_roots: Mapping[str, Iterable[str]],
) -> None:
    """Require exact skill-root registration in each flattened-tooling setting.

    Args:
        discovered_skill_roots: Roots discovered under ``.agents/skills``.
        configured_roots: Setting names mapped to their configured roots.

    Raises:
        ValueError: If a skill root is missing, stale, or duplicated in a setting.
    """
    for setting, roots in configured_roots.items():
        configured_skill_roots = tuple(root for root in roots if root.startswith(".agents/skills/"))
        if Counter(configured_skill_roots) != Counter(discovered_skill_roots):
            raise ValueError(
                f"{setting} skill roots must exactly match discovered roots: "
                f"{configured_skill_roots!r} != {tuple(discovered_skill_roots)!r}"
            )


def duplicate_module_stems(repo_root: Path, roots: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """Return duplicate names directly importable from the supplied sys.path roots.

    Package-contained modules are intentionally excluded because their package
    qualification prevents the flattened-name collision this guard targets.
    """
    found: dict[str, list[str]] = defaultdict(list)
    for root in roots:
        root_path = repo_root / root
        for module in sorted(root_path.glob("*.py")):
            if module.name == "__init__.py":
                continue
            found[module.stem].append(root)
        for package in sorted(root_path.iterdir()):
            if _is_importable_top_level_package(package):
                found[package.name].append(root)
    return {stem: tuple(root_list) for stem, root_list in found.items() if len(root_list) > 1}


def _is_importable_top_level_package(path: Path) -> bool:
    """Return whether a direct child can provide one importable package name."""
    if not path.is_dir() or not path.name.isidentifier() or path.name == "__pycache__":
        return False
    if (path / "__init__.py").is_file():
        return True
    return any(
        module.is_file() and "__pycache__" not in module.relative_to(path).parts
        for module in path.rglob("*.py")
    )


def require_unique_module_stems(repo_root: Path, roots: Iterable[str]) -> None:
    """Raise when flattened roots would expose colliding module names."""
    collisions = duplicate_module_stems(repo_root, roots)
    if collisions:
        details = ", ".join(
            f"{stem}: {', '.join(root_list)}" for stem, root_list in sorted(collisions.items())
        )
        raise ValueError(f"flattened module-name collisions: {details}")


def normalize_coverage_xml(coverage_xml: Path, repo_root: Path) -> None:
    """Normalize and verify local Coverage.py XML filenames for Codecov.

    Args:
        coverage_xml: Local Coverage.py XML report to rewrite atomically.
        repo_root: Repository root containing the configured tooling roots.

    Raises:
        ValueError: If the report or any filename is malformed or ambiguous.
    """
    report_path = _regular_file(coverage_xml, "coverage XML")
    if report_path.stat().st_size > MAX_COVERAGE_XML_BYTES:
        raise ValueError("coverage XML exceeds the size limit")
    try:
        report = ElementTree.fromstring(report_path.read_bytes())
    except ElementTree.ParseError as error:
        raise ValueError("coverage XML is malformed") from error

    tooling_roots = ("scripts", *skill_script_roots(repo_root))
    for root in tooling_roots:
        _require_non_symlink_root(repo_root, root)
    final_names: list[str] = []
    for class_element in report.findall(".//class"):
        filename = class_element.get("filename")
        final_name = _normalize_coverage_filename(filename, repo_root, tooling_roots)
        class_element.set("filename", final_name)
        final_names.append(final_name)

    if len(final_names) != len(set(final_names)):
        raise ValueError("coverage XML contains duplicate final filenames")
    _require_tooling_coverage_filenames(repo_root, tooling_roots, final_names)
    _write_xml_atomically(report_path, report)


def _normalize_coverage_filename(
    filename: str | None, repo_root: Path, tooling_roots: Sequence[str]
) -> str:
    """Resolve one trusted Coverage.py filename against tooling roots."""
    if filename is None or not filename:
        raise ValueError("coverage XML class has no filename")
    path = PurePosixPath(filename)
    if "\\" in filename or path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise ValueError(f"coverage XML filename is unsafe: {filename!r}")
    if any(filename == root or filename.startswith(f"{root}/") for root in tooling_roots):
        raise ValueError(f"coverage XML filename has an unexpected prefix: {filename!r}")

    matches: list[str] = []
    if filename.startswith(APP_COVERAGE_ROOT):
        _require_non_symlink_components(repo_root, path, filename)
        app_candidate = repo_root / filename
        if app_candidate.exists() or app_candidate.is_symlink():
            _regular_file(app_candidate, f"coverage XML app candidate for {filename!r}")
            matches.append(APP_COVERAGE_ROOT)
    for root in tooling_roots:
        _require_non_symlink_root(repo_root, root)
        candidate = repo_root / root / filename
        if candidate.exists() or candidate.is_symlink():
            _require_non_symlink_components(repo_root / root, path, filename)
            _regular_file(candidate, f"coverage XML candidate for {filename!r}")
            matches.append(root)
    if len(matches) != 1:
        raise ValueError(f"coverage XML filename must resolve exactly once: {filename!r}")
    if matches[0] == APP_COVERAGE_ROOT:
        return filename
    return f"{matches[0]}/{filename}"


def _require_non_symlink_components(root: Path, path: PurePosixPath, filename: str) -> None:
    """Reject a nested tooling candidate whose path escapes through a symlink."""
    component = root
    for part in path.parts:
        component /= part
        if component.is_symlink():
            raise ValueError(
                f"coverage XML candidate for {filename!r} must be a regular non-symlink file"
            )


def _regular_file(path: Path, description: str) -> Path:
    """Return a local regular file, rejecting symlinks and other inputs."""
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{description} must be a regular non-symlink file")
    return path


def _require_tooling_coverage_filenames(
    repo_root: Path, tooling_roots: Sequence[str], final_names: Sequence[str]
) -> None:
    """Require a final XML name for each tooling root that has Python files."""
    for root in tooling_roots:
        root_path = repo_root / root
        has_classes = _has_regular_python_content(root_path)
        if has_classes and not any(name.startswith(f"{root}/") for name in final_names):
            raise ValueError(f"coverage XML is missing filenames for tooling root {root!r}")


def _has_regular_python_content(root: Path) -> bool:
    """Return whether a tooling root has non-cache regular Python content at any depth."""
    for file in root.rglob("*.py"):
        relative = file.relative_to(root)
        if "__pycache__" in relative.parts or file.is_symlink() or not file.is_file():
            continue
        if any(
            (root / Path(*relative.parts[:index])).is_symlink()
            for index in range(1, len(relative.parts))
        ):
            continue
        return True
    return False


def _write_xml_atomically(report_path: Path, report: ElementTree.Element) -> None:
    """Write a validated report to a sibling temporary file before replacement."""
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=report_path.parent, prefix=".coverage-", delete=False
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            ElementTree.ElementTree(report).write(
                temporary_file, encoding="utf-8", xml_declaration=True
            )
        os.replace(temporary_path, report_path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def main(argv: Sequence[str] | None = None) -> int:
    """Normalize one Coverage.py XML report from the current repository root."""
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        raise ValueError("usage: tooling_coverage.py COVERAGE_XML")
    normalize_coverage_xml(Path(arguments[0]), Path.cwd())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
