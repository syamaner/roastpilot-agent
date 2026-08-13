"""Fail-closed helpers for the tooling coverage configuration guards."""

from __future__ import annotations

from collections import defaultdict
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
    """Require every discovered skill root in each flattened-tooling setting.

    Args:
        discovered_skill_roots: Roots discovered under ``.agents/skills``.
        configured_roots: Setting names mapped to their configured roots.

    Raises:
        ValueError: If a discovered root is missing or duplicated in a setting.
    """
    for setting, roots in configured_roots.items():
        root_list = tuple(roots)
        for root in discovered_skill_roots:
            if root_list.count(root) != 1:
                raise ValueError(f"{setting} must register {root!r} exactly once")


def duplicate_module_stems(repo_root: Path, roots: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """Return duplicate stems directly importable from the supplied sys.path roots.

    Package-contained modules are intentionally excluded because their package
    qualification prevents the flattened-name collision this guard targets.
    """
    found: dict[str, list[str]] = defaultdict(list)
    for root in roots:
        for module in sorted((repo_root / root).glob("*.py")):
            found[module.stem].append(root)
    return {stem: tuple(root_list) for stem, root_list in found.items() if len(root_list) > 1}


def require_unique_module_stems(repo_root: Path, roots: Iterable[str]) -> None:
    """Raise when flattened roots would expose colliding module names."""
    collisions = duplicate_module_stems(repo_root, roots)
    if collisions:
        details = ", ".join(
            f"{stem}: {', '.join(root_list)}" for stem, root_list in sorted(collisions.items())
        )
        raise ValueError(f"flattened module-name collisions: {details}")
