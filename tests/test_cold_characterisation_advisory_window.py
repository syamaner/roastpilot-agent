"""The fixed D199/D200 advisory-window geometry (#954 slice 5b); hardware-free."""

import ast
import math
from pathlib import Path

import pytest

from roastpilot_agent.cold_characterisation import advisory_window as window

SOURCE = Path(window.__file__)


def test_v1_constants_and_bounds() -> None:
    """V1: the four constants and the window for a scheduled end of 1810.0."""
    assert window.ADVISORY_WINDOW_SECONDS == 300.0
    assert window.ADVISORY_WINDOW_END_MARGIN_SECONDS == 60.0
    assert window.ADVISORY_INVOCATION_ALLOWANCE_SECONDS == 1.0
    assert window.MIN_POST_COMPLETION_DWELL_SECONDS == 5.0
    assert window.advisory_window_bounds(1810.0) == (1450.0, 1750.0)
    assert window.advisory_window_bounds(3630.0) == (3270.0, 3570.0)


@pytest.mark.parametrize("value", [True, 1810, math.nan, math.inf, -math.inf, "1810.0", None])
def test_v1n_non_exact_or_non_finite_scheduled_end_raises(value: object) -> None:
    """V1n: anything but an exact finite float raises a fixed ``ValueError``."""
    with pytest.raises(ValueError, match=r"^scheduled end must be an exact finite float$"):
        window.advisory_window_bounds(value)  # pyright: ignore[reportArgumentType]


def test_window_module_imports_only_math_and_typing() -> None:
    """F-IMP (window): stdlib ``math``/``typing`` only and the exact public surface."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert imported == {"math", "typing"}
    assert not any(isinstance(node, ast.ImportFrom) for node in ast.walk(tree))
    assert window.__all__ == (
        "ADVISORY_INVOCATION_ALLOWANCE_SECONDS",
        "ADVISORY_WINDOW_END_MARGIN_SECONDS",
        "ADVISORY_WINDOW_SECONDS",
        "MIN_POST_COMPLETION_DWELL_SECONDS",
        "advisory_window_bounds",
    )
