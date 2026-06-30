"""Tests for the MCP yaml passthrough-merge render (D78-4, #420).

Covers:
- Passthrough: unmanaged keys in the source yaml survive unchanged.
- Overlay: managed fields from MCPDeviceConfig overwrite the corresponding
  sections in the output.
- Sparse overlay: None fields in MCPDeviceConfig are NOT written.
- Fresh-install: render with no source yaml writes only managed fields.
- Deep merge: managed sub-keys overlay inner keys while unmanaged inner keys
  of the same section survive.
- Atomic write: dest is renamed over (tested indirectly by checking the output
  exists and is valid yaml; the temp-file → replace approach is safe on any
  POSIX filesystem).
- MCPServerProcess.build_server_parameters injects COFFEE_ROASTER_MCP_CONFIG
  when device_config is provided.
- MCPServerProcess renders a fresh yaml on each (re)spawn and cleans up the
  temp dir on stop.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from roastpilot_agent.config import MCPConfig, MCPDeviceConfig
from roastpilot_agent.mcp_yaml import (
    _deep_merge,  # pyright: ignore[reportPrivateUsage]
    _device_config_to_overlay,  # pyright: ignore[reportPrivateUsage]
    render_mcp_yaml,
)

# ---------------------------------------------------------------------------
# Fixture: the known-good yaml bundled with the repo
# ---------------------------------------------------------------------------

_KNOWN_GOOD = Path(__file__).parent.parent / "docs/examples/coffee-roaster-mcp.known-good.yaml"

# Keys in the known-good yaml that are unmanaged and must NOT be overwritten by
# a passthrough-merge — the invariant from D78-4.
_UNMANAGED_KEYS: dict[str, dict[str, object]] = {
    "first_crack": {
        # These are NOT managed by MCPDeviceConfig; they are operator-tuned and
        # must survive a render that overlays the managed fc_mode /
        # fc_confidence_threshold fields.
        "revision": "b349a919c34b6130472da97c01817be404e4f629",
        "onnx_threads": 8,
        "confirmation_window_seconds": 20.0,
        "min_positive_windows": 5,
    },
    "audio": {
        # Not managed: the model requires 10 s windows; overlap is per-platform.
        "window_seconds": 10.0,
        "overlap": 0.7,
    },
    "roaster": {
        # Not managed: baud-rate and temperature_unit are installation-fixed.
        "baudrate": 115200,
        "temperature_unit": "auto",
        "command_interval_seconds": 0.3,
    },
}


# ---------------------------------------------------------------------------
# Unit tests: _deep_merge
# ---------------------------------------------------------------------------


def test_deep_merge_overlay_wins_at_leaf() -> None:
    """Values in overlay overwrite base at the leaf level."""
    base = {"a": 1, "b": 2}
    overlay = {"b": 99}
    result = _deep_merge(base, overlay)
    assert result == {"a": 1, "b": 99}


def test_deep_merge_base_only_keys_survive() -> None:
    """Keys present only in base pass through untouched."""
    base = {"keep": "yes", "overwrite": "old"}
    overlay = {"overwrite": "new"}
    result = _deep_merge(base, overlay)
    assert result["keep"] == "yes"


def test_deep_merge_nested_partial_overlay() -> None:
    """For a nested mapping, only the keys in the overlay change; the rest survive."""
    base = {"section": {"a": 1, "b": 2, "c": 3}}
    overlay = {"section": {"b": 99}}
    result = _deep_merge(base, overlay)
    assert result["section"] == {"a": 1, "b": 99, "c": 3}


def test_deep_merge_does_not_mutate_inputs() -> None:
    """Neither base nor overlay is mutated by the merge."""
    base = {"x": {"y": 1}}
    overlay = {"x": {"y": 2}}
    _deep_merge(base, overlay)
    assert base["x"]["y"] == 1
    assert overlay["x"]["y"] == 2


# ---------------------------------------------------------------------------
# Unit tests: _device_config_to_overlay
# ---------------------------------------------------------------------------


def test_overlay_empty_when_all_none() -> None:
    """An all-None MCPDeviceConfig produces an empty overlay (no keys written)."""
    cfg = MCPDeviceConfig()
    assert _device_config_to_overlay(cfg) == {}


def test_overlay_serial_port_and_driver() -> None:
    """serial_port → roaster.port; roaster_driver → roaster.driver."""
    cfg = MCPDeviceConfig(serial_port="/dev/ttyUSB0", roaster_driver="mock")
    overlay = _device_config_to_overlay(cfg)
    assert overlay == {"roaster": {"port": "/dev/ttyUSB0", "driver": "mock"}}


def test_overlay_audio_input_device() -> None:
    """audio_input_device → audio.input_device."""
    cfg = MCPDeviceConfig(audio_input_device="USB PnP")
    overlay = _device_config_to_overlay(cfg)
    assert overlay == {"audio": {"input_device": "USB PnP"}}


def test_overlay_recording_devices_as_list() -> None:
    """recording_devices (tuple) → recording.devices (list) in yaml."""
    cfg = MCPDeviceConfig(recording_devices=("USB PnP", "Built-in"))
    overlay = _device_config_to_overlay(cfg)
    assert overlay["recording"]["devices"] == ["USB PnP", "Built-in"]


def test_overlay_fc_fields() -> None:
    """fc_mode → first_crack.mode; fc_confidence_threshold → first_crack.confidence_threshold."""
    cfg = MCPDeviceConfig(fc_mode="audio", fc_confidence_threshold=0.75)
    overlay = _device_config_to_overlay(cfg)
    assert overlay == {"first_crack": {"mode": "audio", "confidence_threshold": 0.75}}


def test_overlay_auto_t0() -> None:
    """auto_t0 fields → session section."""
    cfg = MCPDeviceConfig(auto_t0_detection_enabled=True, auto_t0_drop_threshold_c=12.0)
    overlay = _device_config_to_overlay(cfg)
    assert overlay == {
        "session": {"auto_t0_detection_enabled": True, "auto_t0_drop_threshold_c": 12.0}
    }


def test_overlay_none_fields_absent() -> None:
    """A partially-set config only writes the non-None fields."""
    cfg = MCPDeviceConfig(serial_port="/dev/ttyUSB0")  # all other fields None
    overlay = _device_config_to_overlay(cfg)
    # Only roaster.port should appear; no audio, recording, first_crack, or session.
    assert set(overlay.keys()) == {"roaster"}
    assert overlay["roaster"] == {"port": "/dev/ttyUSB0"}


# ---------------------------------------------------------------------------
# Integration tests: render_mcp_yaml
# ---------------------------------------------------------------------------


def test_render_no_source_writes_managed_fields(tmp_path: Path) -> None:
    """With no source yaml, only managed fields appear in the output."""
    dest = tmp_path / "rendered.yaml"
    cfg = MCPDeviceConfig(serial_port="/dev/ttyUSB0", roaster_driver="mock")
    render_mcp_yaml(cfg, source_path=None, dest_path=dest)

    assert dest.exists()
    result = yaml.safe_load(dest.read_text(encoding="utf-8"))
    assert result["roaster"]["port"] == "/dev/ttyUSB0"
    assert result["roaster"]["driver"] == "mock"
    # No unrelated keys should appear.
    assert "first_crack" not in result
    assert "audio" not in result


def test_render_empty_device_config_passthrough(tmp_path: Path) -> None:
    """An all-None device config leaves the source yaml entirely unchanged."""
    src = tmp_path / "source.yaml"
    src.write_text("roaster:\n  driver: hottop\n  port: /dev/ttyUSB0\n", encoding="utf-8")
    dest = tmp_path / "rendered.yaml"

    render_mcp_yaml(MCPDeviceConfig(), source_path=src, dest_path=dest)

    result = yaml.safe_load(dest.read_text(encoding="utf-8"))
    assert result["roaster"]["driver"] == "hottop"
    assert result["roaster"]["port"] == "/dev/ttyUSB0"


def test_render_overlay_updates_managed_key(tmp_path: Path) -> None:
    """A managed field in device_config overwrites the same key in the source yaml."""
    src = tmp_path / "source.yaml"
    src.write_text("roaster:\n  driver: hottop\n  port: /dev/old\n", encoding="utf-8")
    dest = tmp_path / "rendered.yaml"

    render_mcp_yaml(MCPDeviceConfig(serial_port="/dev/new"), source_path=src, dest_path=dest)

    result = yaml.safe_load(dest.read_text(encoding="utf-8"))
    assert result["roaster"]["port"] == "/dev/new"
    # driver was not in device_config → passes through from source.
    assert result["roaster"]["driver"] == "hottop"


def test_render_known_good_passthrough_unmanaged_keys(tmp_path: Path) -> None:
    """The real known-good yaml: managed overlay leaves all unmanaged keys intact.

    This is the primary regression guard for the passthrough-merge invariant:
    a future edit to ``_device_config_to_overlay`` that accidentally drops a
    section or overwrites an inner key will fail here.
    """
    dest = tmp_path / "rendered.yaml"
    # Overlay every managed field so the merge is not a trivial no-op.
    cfg = MCPDeviceConfig(
        serial_port="/dev/ttyUSB1",
        roaster_driver="mock",
        audio_input_device="Test Mic",
        recording_enabled=True,
        recording_autocapture=False,
        fc_mode="audio",
        fc_confidence_threshold=0.8,
        auto_t0_detection_enabled=True,
        auto_t0_drop_threshold_c=20.0,
    )
    render_mcp_yaml(cfg, source_path=_KNOWN_GOOD, dest_path=dest)

    result = yaml.safe_load(dest.read_text(encoding="utf-8"))

    # Every unmanaged key must survive byte-for-byte.
    for section, keys in _UNMANAGED_KEYS.items():
        for key, expected in keys.items():
            assert result[section][key] == expected, (
                f"Unmanaged key {section}.{key!r} was overwritten: "
                f"expected {expected!r}, got {result[section].get(key)!r}"
            )

    # Managed fields must be updated.
    assert result["roaster"]["port"] == "/dev/ttyUSB1"
    assert result["roaster"]["driver"] == "mock"
    assert result["audio"]["input_device"] == "Test Mic"
    assert result["first_crack"]["mode"] == "audio"
    assert result["first_crack"]["confidence_threshold"] == 0.8


def test_render_missing_source_treated_as_empty(tmp_path: Path) -> None:
    """A source path that does not exist is silently treated as empty (fresh install)."""
    missing = tmp_path / "does_not_exist.yaml"
    dest = tmp_path / "rendered.yaml"
    cfg = MCPDeviceConfig(serial_port="/dev/ttyUSB0")

    render_mcp_yaml(cfg, source_path=missing, dest_path=dest)

    result = yaml.safe_load(dest.read_text(encoding="utf-8"))
    assert result["roaster"]["port"] == "/dev/ttyUSB0"


def test_render_creates_parent_dirs(tmp_path: Path) -> None:
    """render_mcp_yaml creates missing parent directories for dest_path."""
    dest = tmp_path / "a" / "b" / "c" / "rendered.yaml"
    render_mcp_yaml(MCPDeviceConfig(serial_port="/dev/ttyUSB0"), source_path=None, dest_path=dest)
    assert dest.exists()


def test_render_dest_is_valid_yaml(tmp_path: Path) -> None:
    """The rendered file is valid yaml that can be round-tripped."""
    dest = tmp_path / "out.yaml"
    cfg = MCPDeviceConfig(
        fc_mode="audio",
        fc_confidence_threshold=0.6,
        auto_t0_detection_enabled=True,
    )
    render_mcp_yaml(cfg, source_path=_KNOWN_GOOD, dest_path=dest)
    # If yaml.safe_load raises, the test fails — that's the assertion.
    result = yaml.safe_load(dest.read_text(encoding="utf-8"))
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Integration tests: MCPServerProcess.build_server_parameters wiring
# ---------------------------------------------------------------------------


def test_build_server_parameters_no_device_config() -> None:
    """Without device_config the render step is skipped; no COFFEE_ROASTER_MCP_CONFIG."""
    from roastpilot_agent.mcp_client import MCPServerProcess

    proc = MCPServerProcess(MCPConfig())
    params = proc.build_server_parameters()
    # env is None when MCPConfig.env is empty and no device_config.
    assert params.env is None or "COFFEE_ROASTER_MCP_CONFIG" not in (params.env or {})


def test_build_server_parameters_with_device_config(tmp_path: Path) -> None:
    """With device_config, COFFEE_ROASTER_MCP_CONFIG is injected and points at a yaml."""
    from roastpilot_agent.mcp_client import MCPServerProcess

    cfg = MCPDeviceConfig(serial_port="/dev/ttyUSB0", roaster_driver="mock")
    proc = MCPServerProcess(MCPConfig(), device_config=cfg)
    try:
        params = proc.build_server_parameters()
        assert params.env is not None
        yaml_path = Path(params.env["COFFEE_ROASTER_MCP_CONFIG"])
        assert yaml_path.exists(), "rendered yaml must exist after build_server_parameters"
        result = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        assert result["roaster"]["port"] == "/dev/ttyUSB0"
        assert result["roaster"]["driver"] == "mock"
    finally:
        if proc._rendered_yaml_dir is not None:  # pyright: ignore[reportPrivateUsage]
            shutil.rmtree(proc._rendered_yaml_dir, ignore_errors=True)  # pyright: ignore[reportPrivateUsage]


def test_build_server_parameters_with_source_yaml(tmp_path: Path) -> None:
    """device_config.mcp_yaml_source_path is used as the passthrough base."""
    from roastpilot_agent.mcp_client import MCPServerProcess

    src = tmp_path / "base.yaml"
    src.write_text("roaster:\n  baudrate: 115200\n  driver: original\n", encoding="utf-8")
    cfg = MCPDeviceConfig(
        serial_port="/dev/ttyUSB0",
        mcp_yaml_source_path=src,
    )
    proc = MCPServerProcess(MCPConfig(), device_config=cfg)
    try:
        params = proc.build_server_parameters()
        assert params.env is not None
        result = yaml.safe_load(
            Path(params.env["COFFEE_ROASTER_MCP_CONFIG"]).read_text(encoding="utf-8")
        )
        # Managed field overlaid.
        assert result["roaster"]["port"] == "/dev/ttyUSB0"
        # Unmanaged field from source survives.
        assert result["roaster"]["baudrate"] == 115200
        # roaster.driver was not in MCPDeviceConfig → passes through from source.
        assert result["roaster"]["driver"] == "original"
    finally:
        if proc._rendered_yaml_dir is not None:  # pyright: ignore[reportPrivateUsage]
            shutil.rmtree(proc._rendered_yaml_dir, ignore_errors=True)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_build_server_parameters_fresh_render_on_respawn(tmp_path: Path) -> None:
    """Each call to build_server_parameters produces a fresh render (config change visible)."""
    from roastpilot_agent.mcp_client import MCPServerProcess

    cfg1 = MCPDeviceConfig(serial_port="/dev/ttyUSB0")
    proc = MCPServerProcess(MCPConfig(), device_config=cfg1)
    try:
        proc.build_server_parameters()
        # Simulate a config change between spawns by updating the instance's device config.
        proc._device_config = MCPDeviceConfig(serial_port="/dev/ttyUSB1")  # pyright: ignore[reportPrivateUsage]
        params2 = proc.build_server_parameters()
        path2 = Path(params2.env["COFFEE_ROASTER_MCP_CONFIG"])  # type: ignore[index]

        assert path2.exists(), "second render must produce a new yaml"
        result2 = yaml.safe_load(path2.read_text(encoding="utf-8"))
        assert result2["roaster"]["port"] == "/dev/ttyUSB1"
    finally:
        if proc._rendered_yaml_dir is not None:  # pyright: ignore[reportPrivateUsage]
            shutil.rmtree(proc._rendered_yaml_dir, ignore_errors=True)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_stop_cleans_up_rendered_yaml_dir() -> None:
    """MCPServerProcess.stop() removes the temp dir created by build_server_parameters."""
    from roastpilot_agent.mcp_client import MCPServerProcess

    cfg = MCPDeviceConfig(serial_port="/dev/ttyUSB0")
    proc = MCPServerProcess(MCPConfig(), device_config=cfg)
    # Call build_server_parameters to create the temp dir.
    proc.build_server_parameters()
    tmp_dir = proc._rendered_yaml_dir  # pyright: ignore[reportPrivateUsage]
    assert tmp_dir is not None and tmp_dir.exists(), "temp dir must be created"

    # Inject a no-op session so stop() doesn't require a real MCP child.
    mock_stack = AsyncMock()
    proc._stack = mock_stack  # pyright: ignore[reportPrivateUsage]
    proc._session = MagicMock()  # pyright: ignore[reportPrivateUsage]

    await proc.stop()

    # After stop, both the dir and the internal pointer must be gone.
    assert not tmp_dir.exists(), "stop() must clean up the temp dir"
    assert proc._rendered_yaml_dir is None  # pyright: ignore[reportPrivateUsage]
