"""Collection guard for validation-gate-only environment checks."""

from __future__ import annotations

import os

import pytest

collect_ignore_glob = [] if os.environ.get("RP_VALIDATION_GATE") == "1" else ["test_*.py"]


def pytest_sessionstart(session: pytest.Session) -> None:
    """Fail closed when an explicit validation-gate target lacks its sentinel."""
    requested = tuple(str(argument) for argument in session.config.args)
    if (
        os.environ.get("RP_VALIDATION_GATE") != "1"
        and requested
        and all(argument.startswith("tests/gate") for argument in requested)
    ):
        pytest.exit("validation gate sentinel is absent", returncode=5)
