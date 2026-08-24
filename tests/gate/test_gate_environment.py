"""Fail-closed checks for the validation gate's reconstructed environment."""

from __future__ import annotations

import os

from capture_usage_models import is_scrubbed_environment_name, render_gate_environment


def test_gate_environment_is_scrubbed_and_complete() -> None:
    """Require a clean scrubbed namespace and every closed reinstated key by name."""
    leaked_names = sorted(name for name in os.environ if is_scrubbed_environment_name(name))
    assert leaked_names == []
    missing_names = sorted(
        name for name, _value in render_gate_environment("") if name not in os.environ
    )
    assert missing_names == []
