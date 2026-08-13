"""Fail-closed helpers for the tooling coverage configuration guards."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

TOOLING_ROOTS = ("scripts", ".agents/skills/capture-agent-usage/scripts")


def skill_script_roots(repo_root: Path) -> tuple[str, ...]:
    """Return every skill script root relative to ``repo_root``.

    Raises:
        ValueError: If no skill script roots exist.
    """
    roots = tuple(
        path.relative_to(repo_root).as_posix()
        for path in sorted((repo_root / ".agents" / "skills").glob("*/scripts"))
        if path.is_dir()
    )
    if not roots:
        raise ValueError("no .agents/skills/*/scripts roots found")
    return roots


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
            if package.is_dir() and (package / "__init__.py").is_file():
                found[package.name].append(root)
    return {stem: tuple(root_list) for stem, root_list in found.items() if len(root_list) > 1}


def require_unique_module_stems(repo_root: Path, roots: Iterable[str]) -> None:
    """Raise when flattened roots would expose colliding module names."""
    collisions = duplicate_module_stems(repo_root, roots)
    if collisions:
        details = ", ".join(
            f"{stem}: {', '.join(root_list)}" for stem, root_list in sorted(collisions.items())
        )
        raise ValueError(f"flattened module-name collisions: {details}")
