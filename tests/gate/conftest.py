"""Collection guard for validation-gate-only environment checks."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

collect_ignore_glob = [] if os.environ.get("RP_VALIDATION_GATE") == "1" else ["test_*.py"]


def _is_validation_gate_target(argument: str, rootpath: Path) -> bool:
    """Return whether one explicit pytest argument addresses ``tests/gate``."""
    candidate = Path(argument.split("::", 1)[0]).expanduser()
    root = rootpath.resolve()
    target = (candidate if candidate.is_absolute() else root / candidate).resolve()
    gate = (root / "tests" / "gate").resolve()
    return target == gate or gate in target.parents


def pytest_sessionstart(session: pytest.Session) -> None:
    """Fail closed when an explicit validation-gate target lacks its sentinel."""
    requested = tuple(str(argument) for argument in session.config.args)
    if (
        os.environ.get("RP_VALIDATION_GATE") != "1"
        and requested
        and any(
            _is_validation_gate_target(argument, session.config.rootpath) for argument in requested
        )
    ):
        pytest.exit("validation gate sentinel is absent", returncode=5)
