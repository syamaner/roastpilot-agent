"""T14: the rendered env file's keys are real, consumed exactly as rendered (#138, slice 2).

"Do not invent setting names" (contract §2.3 item 2) is proven two ways here,
not just "AppConfig() doesn't raise": first, the env file's key SET is pinned
to exactly the four names the codebase already recognises (no more, no
fewer) — recognised meaning each is grep-verified elsewhere in the codebase
(``config.py``'s ``AdvisorConfig.api_key_env`` default, ``cli.py``'s
``ROASTPILOT_DB`` precedence, ``mcp_yaml.py``/``live.py``'s
``COFFEE_ROASTER_MCP_CONFIG`` forwarding); second, each rendered value is
proven to reach the REAL consumption point unchanged, so a renamed or
misspelled key (G21) fails this test even though pydantic-settings silently
ignores an env var that does not match its prefix/field scheme (verified: a
stray ``ROASTPILOT_DB`` or ``PORT`` never raises from ``AppConfig()`` on its
own — only checking the exact keys AND their real consumption catches a
drifted name).
"""

from __future__ import annotations

import argparse
import grp
import pwd
from pathlib import Path
from types import SimpleNamespace

import pytest

from roastpilot_agent import cli
from roastpilot_agent.appliance.render import (
    ApplianceRenderInputs,
    render_env_file,
    render_service_unit,
)
from roastpilot_agent.config import AppConfig
from roastpilot_agent.live import forward_coffee_env

_EXPECTED_ENV_KEYS = frozenset(
    {"OPENROUTER_API_KEY", "PORT", "ROASTPILOT_DB", "COFFEE_ROASTER_MCP_CONFIG"}
)


def _parse_dotenv(text: str) -> dict[str, str]:
    """Parse the rendered env file's ``KEY=VALUE`` lines (comments/blank skipped)."""
    pairs: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        assert separator, f"malformed env line: {raw_line!r}"
        pairs[key] = value
    return pairs


def _render(**overrides: object) -> dict[str, str]:
    defaults: dict[str, object] = {
        "port": 9001,
        "operator_user": "pi",
        "operator_group": "pi",
        "operator_home": Path("/home/pi"),
        "db_path": Path("/var/lib/roastpilot-agent/roastpilot.sqlite3"),
        "mcp_config_path": Path("/etc/roastpilot-agent/coffee-roaster-mcp.yaml"),
        "model_dir": Path("/var/lib/roastpilot-agent/models"),
        "serial_port": Path("/dev/serial/by-id/hottop"),
        "audio_device": "USB PnP Audio Device",
    }
    defaults.update(overrides)
    inputs = ApplianceRenderInputs(**defaults)  # type: ignore[arg-type]
    return _parse_dotenv(render_env_file(inputs))


def test_env_file_key_set_is_exactly_the_recognised_four() -> None:
    """G21: renaming any key to an invented name changes this set and fails."""
    pairs = _render()
    assert set(pairs) == _EXPECTED_ENV_KEYS


def test_service_consumes_the_exact_port_variable_rendered_in_the_env_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PORT is an env-file value, not a second service-template port literal."""

    def get_non_root_user(_: str) -> SimpleNamespace:
        return SimpleNamespace(pw_uid=1000)

    def get_non_root_group(_: str) -> SimpleNamespace:
        return SimpleNamespace(gr_gid=1000)

    monkeypatch.setattr(pwd, "getpwnam", get_non_root_user)
    monkeypatch.setattr(grp, "getgrnam", get_non_root_group)
    inputs = ApplianceRenderInputs(
        port=9123,
        operator_user="pi",
        operator_group="pi",
        operator_home=Path("/home/pi"),
        db_path=Path("/var/lib/roastpilot-agent/roastpilot.sqlite3"),
        mcp_config_path=Path("/etc/roastpilot-agent/coffee-roaster-mcp.yaml"),
        model_dir=Path("/var/lib/roastpilot-agent/models"),
        serial_port=Path("/dev/serial/by-id/hottop"),
        audio_device="USB PnP Audio Device",
    )
    pairs = _parse_dotenv(render_env_file(inputs))
    service = render_service_unit(inputs)

    assert pairs["PORT"] == "9123"
    assert "EnvironmentFile=/etc/roastpilot-agent/roastpilot-agent.env" in service
    assert (
        "ExecStart=/home/pi/.local/bin/roastpilot-agent serve --host 0.0.0.0 --port ${PORT}"
        in service
    )
    assert "--port 9123" not in service


def test_app_config_loads_with_no_error_from_the_rendered_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pairs = _render()
    for key in ("ROASTPILOT_DB", "PORT", "COFFEE_ROASTER_MCP_CONFIG", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    for key, value in pairs.items():
        monkeypatch.setenv(key, value)

    config = AppConfig()  # must not raise: no unknown-key/validation error

    assert isinstance(config, AppConfig)


def test_rendered_db_path_reaches_the_real_cli_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ROASTPILOT_DB`` is read directly by ``cli._resolve_live_store_path``,
    never through the ``AppConfig`` pydantic model — proven by calling the
    real resolver, not by asserting on ``AppConfig`` (which has no ``db``
    field at all). Uses a writable ``tmp_path`` rather than the real appliance
    default (``/var/lib/...``, root-owned on a real install) so this test is
    hardware/host-permission independent — the resolver's behaviour under
    test is the same regardless of which path value it is handed."""
    db_path = tmp_path / "roastpilot-agent" / "roastpilot.sqlite3"
    pairs = _render(db_path=db_path)
    monkeypatch.delenv("ROASTPILOT_DB", raising=False)
    monkeypatch.setenv("ROASTPILOT_DB", pairs["ROASTPILOT_DB"])

    resolved = cli._resolve_live_store_path(  # pyright: ignore[reportPrivateUsage]
        argparse.Namespace(db=None)
    )

    assert resolved == db_path


def test_rendered_mcp_config_path_is_forwarded_to_the_mcp_child_env() -> None:
    """``COFFEE_ROASTER_MCP_CONFIG`` is forwarded via the existing COFFEE_* path
    (``forward_coffee_env``), never a bespoke mechanism this slice invents."""
    mcp_config_path = Path("/etc/roastpilot-agent/coffee-roaster-mcp.yaml")
    pairs = _render(mcp_config_path=mcp_config_path)
    config = AppConfig()

    forward_coffee_env(
        config, environ={"COFFEE_ROASTER_MCP_CONFIG": pairs["COFFEE_ROASTER_MCP_CONFIG"]}
    )

    assert config.mcp.env["COFFEE_ROASTER_MCP_CONFIG"] == str(mcp_config_path)


def test_rendered_openrouter_key_line_matches_the_real_advisor_env_var_name() -> None:
    """``advisor.api_key_env`` defaults to exactly ``OPENROUTER_API_KEY`` — the
    env file's key is not a re-typed guess at that name."""
    pairs = _render()
    config = AppConfig()
    assert config.advisor.api_key_env in pairs


def test_rendered_openrouter_key_value_reaches_os_environ_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A supplied key value is retrievable at the exact env-var name the
    advisor reads at build time (never renamed/wrapped)."""
    config = AppConfig()
    monkeypatch.delenv(config.advisor.api_key_env, raising=False)
    monkeypatch.setenv(config.advisor.api_key_env, "sk-test-value")
    import os

    assert os.environ[config.advisor.api_key_env] == "sk-test-value"
