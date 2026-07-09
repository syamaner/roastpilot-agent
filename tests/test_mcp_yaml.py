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
    MANAGED_YAML_PATHS,
    _deep_merge,  # pyright: ignore[reportPrivateUsage]
    _device_config_to_overlay,  # pyright: ignore[reportPrivateUsage]
    read_yaml_value,
    render_mcp_yaml,
    resolve_mcp_yaml_source_path,
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


def test_overlay_ambient_fields() -> None:
    """ambient_mode/ambient_device/ambient_poll_interval_seconds → ambient section (D85, #474)."""
    cfg = MCPDeviceConfig(
        ambient_mode="yoctopuce", ambient_device="METEOMK2-1", ambient_poll_interval_seconds=15.0
    )
    overlay = _device_config_to_overlay(cfg)
    assert overlay == {
        "ambient": {
            "mode": "yoctopuce",
            "device": "METEOMK2-1",
            "poll_interval_seconds": 15.0,
        }
    }


def test_overlay_ambient_partial_fields_only_non_none() -> None:
    """A partially-set ambient config only writes the non-None ambient keys."""
    cfg = MCPDeviceConfig(ambient_mode="disabled")
    overlay = _device_config_to_overlay(cfg)
    assert overlay == {"ambient": {"mode": "disabled"}}


def test_overlay_all_none_ambient_omits_section() -> None:
    """All-None ambient fields (the default) produce no ambient key in the overlay
    (back-compat: does not disturb an existing yaml's ambient block, D85/#474)."""
    cfg = MCPDeviceConfig(serial_port="/dev/ttyUSB0")  # ambient_* left at default None
    overlay = _device_config_to_overlay(cfg)
    assert "ambient" not in overlay


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
        ambient_mode="disabled",
        ambient_device="METEOMK2-NEW",
        ambient_poll_interval_seconds=10.0,
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
    # Ambient (D85, #474): the known-good yaml's hand-authored ambient: block
    # (mode: yoctopuce, device: null, poll_interval_seconds: 30.0) is fully
    # overlaid here since all three managed keys are set.
    assert result["ambient"]["mode"] == "disabled"
    assert result["ambient"]["device"] == "METEOMK2-NEW"
    assert result["ambient"]["poll_interval_seconds"] == 10.0


def test_render_ambient_overlay_onto_hand_authored_block(tmp_path: Path) -> None:
    """Passthrough-merge: an ambient field managed by MCPDeviceConfig overwrites only
    that key; a hand-authored ambient key NOT managed here survives (D85, #474).

    The MCP's AmbientConfig has exactly mode/device/poll_interval_seconds — all
    three are managed here, so this test uses a 4th, made-up key to prove the
    merge is key-level (not section-replace), matching the roaster/audio/session
    overlay behaviour exactly.
    """
    src = tmp_path / "source.yaml"
    src.write_text(
        "ambient:\n"
        "  mode: yoctopuce\n"
        "  device: OLD-SERIAL\n"
        "  poll_interval_seconds: 30.0\n"
        "  future_unmanaged_key: keep-me\n",
        encoding="utf-8",
    )
    dest = tmp_path / "rendered.yaml"

    render_mcp_yaml(
        MCPDeviceConfig(ambient_device="NEW-SERIAL"),
        source_path=src,
        dest_path=dest,
    )

    result = yaml.safe_load(dest.read_text(encoding="utf-8"))
    # Managed key overwritten.
    assert result["ambient"]["device"] == "NEW-SERIAL"
    # Unmanaged fields in the same section (and mode, not touched by this edit)
    # survive byte-for-byte — the passthrough-merge invariant.
    assert result["ambient"]["mode"] == "yoctopuce"
    assert result["ambient"]["poll_interval_seconds"] == 30.0
    assert result["ambient"]["future_unmanaged_key"] == "keep-me"


def test_render_all_none_ambient_preserves_hand_authored_block(tmp_path: Path) -> None:
    """Back-compat: an all-None ambient device config leaves an existing
    hand-authored ambient: block in the source yaml entirely unchanged (D85, #474)."""
    src = tmp_path / "source.yaml"
    src.write_text(
        "ambient:\n  mode: yoctopuce\n  device: METEOMK2-1\n  poll_interval_seconds: 45.0\n",
        encoding="utf-8",
    )
    dest = tmp_path / "rendered.yaml"

    render_mcp_yaml(MCPDeviceConfig(), source_path=src, dest_path=dest)

    result = yaml.safe_load(dest.read_text(encoding="utf-8"))
    assert result["ambient"] == {
        "mode": "yoctopuce",
        "device": "METEOMK2-1",
        "poll_interval_seconds": 45.0,
    }


def test_render_missing_source_raises(tmp_path: Path) -> None:
    """A resolved source_path that does not exist raises FileNotFoundError (fail closed).

    This replaces the previous silent-empty behaviour: a missing explicit source
    must not mask the real MCP's own ConfigError on a missing config path.
    """
    missing = tmp_path / "does_not_exist.yaml"
    dest = tmp_path / "rendered.yaml"
    cfg = MCPDeviceConfig(serial_port="/dev/ttyUSB0")

    with pytest.raises(FileNotFoundError, match="does not exist"):
        render_mcp_yaml(cfg, source_path=missing, dest_path=dest)


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


# ---------------------------------------------------------------------------
# P1 regression: roast-live.sh flow preservation
# ---------------------------------------------------------------------------


def test_build_server_parameters_roast_live_sh_flow(tmp_path: Path) -> None:
    """P1 regression: COFFEE_ROASTER_MCP_CONFIG in os.environ + all-None mcp_device.

    This mirrors the roast-live.sh flow where the operator exports their proven
    Hottop yaml via COFFEE_ROASTER_MCP_CONFIG and the agent passes a default
    (all-None) mcp_device.  The MCP child must receive the operator's yaml, not
    an empty one.
    """
    import os

    from roastpilot_agent.mcp_client import MCPServerProcess

    # Write a yaml that resembles the operator's known-good Hottop config.
    operator_yaml = tmp_path / "operator.yaml"
    operator_yaml.write_text(
        "roaster:\n  driver: hottop_kn8828b_2k_plus\n  port: /dev/cu.usbserial-XXXX\n"
        "first_crack:\n  mode: audio\n  confidence_threshold: 0.6\n",
        encoding="utf-8",
    )

    # Default mcp_device: all fields None (the default construction).
    proc = MCPServerProcess(MCPConfig(), device_config=MCPDeviceConfig())
    try:
        # Simulate roast-live.sh exporting COFFEE_ROASTER_MCP_CONFIG.
        env_backup = os.environ.get("COFFEE_ROASTER_MCP_CONFIG")
        os.environ["COFFEE_ROASTER_MCP_CONFIG"] = str(operator_yaml)
        try:
            params = proc.build_server_parameters()
        finally:
            if env_backup is None:
                os.environ.pop("COFFEE_ROASTER_MCP_CONFIG", None)
            else:
                os.environ["COFFEE_ROASTER_MCP_CONFIG"] = env_backup  # pragma: no cover

        # The MCP child must be pointed at the rendered yaml (passthrough copy).
        assert params.env is not None
        rendered_path = Path(params.env["COFFEE_ROASTER_MCP_CONFIG"])
        assert rendered_path.exists()
        result = yaml.safe_load(rendered_path.read_text(encoding="utf-8"))
        # Serial, driver, and FC settings from the operator's yaml must survive.
        assert result["roaster"]["driver"] == "hottop_kn8828b_2k_plus"
        assert result["roaster"]["port"] == "/dev/cu.usbserial-XXXX"
        assert result["first_crack"]["mode"] == "audio"
        assert result["first_crack"]["confidence_threshold"] == 0.6
    finally:
        if proc._rendered_yaml_dir is not None:  # pyright: ignore[reportPrivateUsage]
            shutil.rmtree(proc._rendered_yaml_dir, ignore_errors=True)  # pyright: ignore[reportPrivateUsage]


def test_build_server_parameters_roast_live_sh_via_mcp_env(tmp_path: Path) -> None:
    """COFFEE_ROASTER_MCP_CONFIG in MCPConfig.env (forward_coffee_env path) is used as source."""
    from roastpilot_agent.mcp_client import MCPServerProcess

    operator_yaml = tmp_path / "operator.yaml"
    operator_yaml.write_text(
        "roaster:\n  driver: hottop_kn8828b_2k_plus\n  port: /dev/ttyUSB0\n",
        encoding="utf-8",
    )
    # This is how forward_coffee_env delivers the operator's yaml path.
    mcp_cfg = MCPConfig(env={"COFFEE_ROASTER_MCP_CONFIG": str(operator_yaml)})
    proc = MCPServerProcess(mcp_cfg, device_config=MCPDeviceConfig())
    try:
        params = proc.build_server_parameters()
        assert params.env is not None
        result = yaml.safe_load(
            Path(params.env["COFFEE_ROASTER_MCP_CONFIG"]).read_text(encoding="utf-8")
        )
        assert result["roaster"]["driver"] == "hottop_kn8828b_2k_plus"
        assert result["roaster"]["port"] == "/dev/ttyUSB0"
    finally:
        if proc._rendered_yaml_dir is not None:  # pyright: ignore[reportPrivateUsage]
            shutil.rmtree(proc._rendered_yaml_dir, ignore_errors=True)  # pyright: ignore[reportPrivateUsage]


def test_build_server_parameters_skip_when_no_managed_and_no_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No managed fields + no COFFEE_ROASTER_MCP_CONFIG → render skipped entirely.

    The skip condition prevents an empty temp yaml from overwriting
    COFFEE_ROASTER_MCP_CONFIG when there is no config to merge.
    """
    from roastpilot_agent.mcp_client import MCPServerProcess

    monkeypatch.delenv("COFFEE_ROASTER_MCP_CONFIG", raising=False)
    proc = MCPServerProcess(MCPConfig(), device_config=MCPDeviceConfig())
    params = proc.build_server_parameters()
    # No env key injected, no temp dir created.
    assert proc._rendered_yaml_dir is None  # pyright: ignore[reportPrivateUsage]
    assert params.env is None or "COFFEE_ROASTER_MCP_CONFIG" not in (params.env or {})


def test_build_server_parameters_explicit_source_wins_over_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mcp_yaml_source_path takes priority over COFFEE_ROASTER_MCP_CONFIG env var."""
    from roastpilot_agent.mcp_client import MCPServerProcess

    explicit = tmp_path / "explicit.yaml"
    explicit.write_text("roaster:\n  driver: from_explicit\n", encoding="utf-8")
    ambient = tmp_path / "ambient.yaml"
    ambient.write_text("roaster:\n  driver: from_env\n", encoding="utf-8")

    monkeypatch.setenv("COFFEE_ROASTER_MCP_CONFIG", str(ambient))
    cfg = MCPDeviceConfig(mcp_yaml_source_path=explicit)
    proc = MCPServerProcess(MCPConfig(), device_config=cfg)
    try:
        params = proc.build_server_parameters()
        result = yaml.safe_load(
            Path(params.env["COFFEE_ROASTER_MCP_CONFIG"]).read_text(encoding="utf-8")  # type: ignore[index]
        )
        assert result["roaster"]["driver"] == "from_explicit"
    finally:
        if proc._rendered_yaml_dir is not None:  # pyright: ignore[reportPrivateUsage]
            shutil.rmtree(proc._rendered_yaml_dir, ignore_errors=True)  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# Fail-closed: non-mapping source yaml
# ---------------------------------------------------------------------------


def test_render_non_mapping_source_raises(tmp_path: Path) -> None:
    """A valid-YAML-but-non-mapping source (list, scalar) raises ValueError.

    Silently treating a list or scalar as empty base would drop all operator
    config — we fail closed instead.
    """
    src = tmp_path / "bad.yaml"
    src.write_text("- item1\n- item2\n", encoding="utf-8")
    dest = tmp_path / "out.yaml"
    with pytest.raises(ValueError, match="not a mapping"):
        render_mcp_yaml(MCPDeviceConfig(), source_path=src, dest_path=dest)


def test_render_null_yaml_treated_as_empty(tmp_path: Path) -> None:
    """An empty file (null yaml) is silently treated as an empty base, not an error."""
    src = tmp_path / "empty.yaml"
    src.write_text("", encoding="utf-8")
    dest = tmp_path / "out.yaml"
    render_mcp_yaml(MCPDeviceConfig(serial_port="/dev/ttyUSB0"), source_path=src, dest_path=dest)
    result = yaml.safe_load(dest.read_text(encoding="utf-8"))
    assert result["roaster"]["port"] == "/dev/ttyUSB0"


# ---------------------------------------------------------------------------
# config_store: apply_config_edit mcp_device coverage
# ---------------------------------------------------------------------------


def test_apply_config_edit_mcp_device_all_fields(tmp_path: Path) -> None:
    """apply_config_edit wires all mcp_device fields including recording_devices."""
    from roastpilot_agent.config_store import MCPDeviceConfigEdit, apply_config_edit

    edit_data = MCPDeviceConfigEdit(
        serial_port="/dev/ttyUSB1",
        roaster_driver="mock",
        audio_input_device="USB PnP",
        recording_enabled=True,
        recording_autocapture=False,
        recording_devices=["USB PnP", "Built-in"],
        fc_mode="audio",
        fc_confidence_threshold=0.75,
        auto_t0_detection_enabled=True,
        auto_t0_drop_threshold_c=18.0,
        ambient_mode="yoctopuce",
        ambient_device="METEOMK2-1",
        ambient_poll_interval_seconds=15.0,
    )
    from roastpilot_agent.config_store import AppConfigEdit

    result = apply_config_edit(AppConfigEdit(mcp_device=edit_data), existing_saved={})

    dev = result["mcp_device"]
    assert dev["serial_port"] == "/dev/ttyUSB1"
    assert dev["roaster_driver"] == "mock"
    assert dev["audio_input_device"] == "USB PnP"
    assert dev["recording_enabled"] is True
    assert dev["recording_autocapture"] is False
    # recording_devices is stored as tuple (pydantic-compatible for round-trip).
    assert dev["recording_devices"] == ("USB PnP", "Built-in")
    assert dev["fc_mode"] == "audio"
    assert dev["fc_confidence_threshold"] == 0.75
    assert dev["auto_t0_detection_enabled"] is True
    assert dev["auto_t0_drop_threshold_c"] == 18.0
    assert dev["ambient_mode"] == "yoctopuce"
    assert dev["ambient_device"] == "METEOMK2-1"
    assert dev["ambient_poll_interval_seconds"] == 15.0


def test_apply_config_edit_mcp_device_none_fields_skipped(tmp_path: Path) -> None:
    """None fields in MCPDeviceConfigEdit do not overwrite existing saved values."""
    from roastpilot_agent.config_store import AppConfigEdit, MCPDeviceConfigEdit, apply_config_edit

    existing = {"mcp_device": {"serial_port": "/dev/old", "fc_mode": "disabled"}}
    edit = AppConfigEdit(mcp_device=MCPDeviceConfigEdit(serial_port="/dev/new"))
    result = apply_config_edit(edit, existing_saved=existing)
    dev = result["mcp_device"]
    assert dev["serial_port"] == "/dev/new"
    assert dev["fc_mode"] == "disabled"  # untouched


# ---------------------------------------------------------------------------
# config_store: apply_config_edit ambient fields (D85, #474)
# ---------------------------------------------------------------------------


def test_apply_config_edit_ambient_mode_disabled_to_yoctopuce() -> None:
    """PUT ambient_mode disabled→yoctopuce persists into the saved section (#474)."""
    from roastpilot_agent.config_store import AppConfigEdit, MCPDeviceConfigEdit, apply_config_edit

    existing = {"mcp_device": {"ambient_mode": "disabled"}}
    edit = AppConfigEdit(
        mcp_device=MCPDeviceConfigEdit(
            ambient_mode="yoctopuce",
            ambient_device="METEOMK2-1",
            ambient_poll_interval_seconds=15.0,
        )
    )
    result = apply_config_edit(edit, existing_saved=existing)
    dev = result["mcp_device"]
    assert dev["ambient_mode"] == "yoctopuce"
    assert dev["ambient_device"] == "METEOMK2-1"
    assert dev["ambient_poll_interval_seconds"] == 15.0


def test_apply_config_edit_ambient_clear_back_to_inherit() -> None:
    """Explicit null for an ambient field clears the saved key (tri-state, #439 pattern)."""
    from roastpilot_agent.config_store import AppConfigEdit, MCPDeviceConfigEdit, apply_config_edit

    existing = {"mcp_device": {"ambient_mode": "yoctopuce", "ambient_device": "METEOMK2-1"}}
    edit = AppConfigEdit(mcp_device=MCPDeviceConfigEdit.model_validate({"ambient_mode": None}))
    result = apply_config_edit(edit, existing_saved=existing)
    dev = result["mcp_device"]
    assert "ambient_mode" not in dev
    # Untouched field (not in the PUT body) survives.
    assert dev["ambient_device"] == "METEOMK2-1"


def test_apply_config_edit_ambient_blank_device_treated_as_inherit() -> None:
    """A blank ambient_device string must not write device:'' to the MCP yaml."""
    from roastpilot_agent.config_store import AppConfigEdit, MCPDeviceConfigEdit, apply_config_edit

    existing = {"mcp_device": {"ambient_device": "METEOMK2-1"}}
    edit = AppConfigEdit(mcp_device=MCPDeviceConfigEdit.model_validate({"ambient_device": ""}))
    result = apply_config_edit(edit, existing_saved=existing)
    assert "ambient_device" not in result.get("mcp_device", {})


def test_apply_config_edit_ambient_unset_field_is_unchanged() -> None:
    """An ambient field absent from the PUT body leaves the saved value intact."""
    from roastpilot_agent.config_store import AppConfigEdit, MCPDeviceConfigEdit, apply_config_edit

    existing = {"mcp_device": {"ambient_mode": "yoctopuce", "ambient_poll_interval_seconds": 30.0}}
    edit = AppConfigEdit(mcp_device=MCPDeviceConfigEdit(ambient_device="METEOMK2-1"))
    result = apply_config_edit(edit, existing_saved=existing)
    dev = result["mcp_device"]
    assert dev["ambient_mode"] == "yoctopuce"
    assert dev["ambient_poll_interval_seconds"] == 30.0
    assert dev["ambient_device"] == "METEOMK2-1"


# ---------------------------------------------------------------------------
# P2 regression: fail-closed on explicit-but-missing source paths
# ---------------------------------------------------------------------------


def test_render_explicit_source_missing_raises(tmp_path: Path) -> None:
    """P2-a: a resolved source path that does not exist raises FileNotFoundError.

    Silently falling to base={} would mask the real MCP's own ConfigError on a
    missing explicit config path, causing a live roast to start on defaults.
    """
    missing = tmp_path / "not_here.yaml"
    assert not missing.exists()
    with pytest.raises(FileNotFoundError, match="does not exist"):
        render_mcp_yaml(
            MCPDeviceConfig(serial_port="/dev/ttyUSB0"),
            source_path=missing,
            dest_path=tmp_path / "out.yaml",
        )


def test_build_server_parameters_env_source_missing_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2-a: COFFEE_ROASTER_MCP_CONFIG pointing at a missing file → build raises.

    The render must not mask the MCP's fail-closed behavior by silently
    substituting an empty base.
    """
    from roastpilot_agent.mcp_client import MCPServerProcess

    monkeypatch.setenv("COFFEE_ROASTER_MCP_CONFIG", str(tmp_path / "missing.yaml"))
    proc = MCPServerProcess(MCPConfig(), device_config=MCPDeviceConfig(serial_port="/dev/ttyUSB0"))
    try:
        with pytest.raises(FileNotFoundError):
            proc.build_server_parameters()
    finally:
        if proc._rendered_yaml_dir is not None:  # pyright: ignore[reportPrivateUsage]
            shutil.rmtree(proc._rendered_yaml_dir, ignore_errors=True)  # pyright: ignore[reportPrivateUsage]


def test_build_server_parameters_mcp_env_source_missing_raises(
    tmp_path: Path,
) -> None:
    """P2-a: COFFEE_ROASTER_MCP_CONFIG in MCPConfig.env pointing at missing file → raises."""
    from roastpilot_agent.mcp_client import MCPServerProcess

    mcp_cfg = MCPConfig(env={"COFFEE_ROASTER_MCP_CONFIG": str(tmp_path / "missing.yaml")})
    proc = MCPServerProcess(mcp_cfg, device_config=MCPDeviceConfig(serial_port="/dev/ttyUSB0"))
    try:
        with pytest.raises(FileNotFoundError):
            proc.build_server_parameters()
    finally:
        if proc._rendered_yaml_dir is not None:  # pyright: ignore[reportPrivateUsage]
            shutil.rmtree(proc._rendered_yaml_dir, ignore_errors=True)  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# P2 regression: CWD default yaml fallback preserves unmanaged keys
# ---------------------------------------------------------------------------


def test_build_server_parameters_cwd_default_yaml_used_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2-b: operator uses CWD default yaml + UI edit → unmanaged keys preserved.

    When neither mcp_yaml_source_path nor COFFEE_ROASTER_MCP_CONFIG is set but
    coffee-roaster-mcp.yaml exists in the CWD (the MCP's own fallback), the
    render must merge onto it — not produce an overlay-only yaml that drops the
    operator's pinned revision, onnx_threads, etc.
    """
    import os

    from roastpilot_agent.mcp_client import MCPServerProcess

    # Write a CWD default yaml with unmanaged keys that must survive.
    cwd_yaml = tmp_path / "coffee-roaster-mcp.yaml"
    cwd_yaml.write_text(
        "roaster:\n  driver: hottop_kn8828b_2k_plus\n  baudrate: 115200\n"
        "first_crack:\n  revision: b349a919\n  onnx_threads: 8\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("COFFEE_ROASTER_MCP_CONFIG", raising=False)

    # Change CWD to tmp_path so the default file is found.
    original_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        cfg = MCPDeviceConfig(serial_port="/dev/ttyUSB1")
        proc = MCPServerProcess(MCPConfig(), device_config=cfg)
        try:
            params = proc.build_server_parameters()
            assert params.env is not None
            result = yaml.safe_load(
                Path(params.env["COFFEE_ROASTER_MCP_CONFIG"]).read_text(encoding="utf-8")
            )
            # Managed field overlaid.
            assert result["roaster"]["port"] == "/dev/ttyUSB1"
            # Unmanaged keys from the CWD default survive.
            assert result["roaster"]["driver"] == "hottop_kn8828b_2k_plus"
            assert result["roaster"]["baudrate"] == 115200
            assert result["first_crack"]["revision"] == "b349a919"
            assert result["first_crack"]["onnx_threads"] == 8
        finally:
            if proc._rendered_yaml_dir is not None:  # pyright: ignore[reportPrivateUsage]
                shutil.rmtree(proc._rendered_yaml_dir, ignore_errors=True)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.chdir(original_cwd)


def test_build_server_parameters_no_cwd_default_fresh_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2-b: no CWD default yaml → overlay-only render (fresh install, no error)."""
    import os

    from roastpilot_agent.mcp_client import MCPServerProcess

    monkeypatch.delenv("COFFEE_ROASTER_MCP_CONFIG", raising=False)
    original_cwd = os.getcwd()
    os.chdir(tmp_path)  # tmp_path has no coffee-roaster-mcp.yaml
    try:
        cfg = MCPDeviceConfig(serial_port="/dev/ttyUSB0", roaster_driver="mock")
        proc = MCPServerProcess(MCPConfig(), device_config=cfg)
        try:
            params = proc.build_server_parameters()
            assert params.env is not None
            result = yaml.safe_load(
                Path(params.env["COFFEE_ROASTER_MCP_CONFIG"]).read_text(encoding="utf-8")
            )
            # Only managed fields present — no error on missing CWD default.
            assert result["roaster"]["port"] == "/dev/ttyUSB0"
            assert result["roaster"]["driver"] == "mock"
        finally:
            if proc._rendered_yaml_dir is not None:  # pyright: ignore[reportPrivateUsage]
                shutil.rmtree(proc._rendered_yaml_dir, ignore_errors=True)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.chdir(original_cwd)


# ---------------------------------------------------------------------------
# resolve_mcp_yaml_source_path (#482) — extracted precedence, shared with
# GET /api/config's read-only yaml lookup.
# ---------------------------------------------------------------------------


def test_resolve_source_path_explicit_wins() -> None:
    """mcp_yaml_source_path takes priority over everything else."""
    cfg = MCPDeviceConfig(mcp_yaml_source_path=Path("/explicit/path.yaml"))
    result = resolve_mcp_yaml_source_path(
        cfg, mcp_env={"COFFEE_ROASTER_MCP_CONFIG": "/env/path.yaml"}
    )
    assert result == Path("/explicit/path.yaml")


def test_resolve_source_path_mcp_env_wins_over_os_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """COFFEE_ROASTER_MCP_CONFIG from mcp_env wins over os.environ (forward_coffee_env path)."""
    monkeypatch.setenv("COFFEE_ROASTER_MCP_CONFIG", "/ambient/path.yaml")
    cfg = MCPDeviceConfig()
    result = resolve_mcp_yaml_source_path(
        cfg, mcp_env={"COFFEE_ROASTER_MCP_CONFIG": "/mcp-env/path.yaml"}
    )
    assert result == Path("/mcp-env/path.yaml")


def test_resolve_source_path_os_environ_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no explicit path and no mcp_env, os.environ's COFFEE_ROASTER_MCP_CONFIG is used."""
    monkeypatch.setenv("COFFEE_ROASTER_MCP_CONFIG", "/ambient/path.yaml")
    cfg = MCPDeviceConfig()
    result = resolve_mcp_yaml_source_path(cfg, mcp_env=None)
    assert result == Path("/ambient/path.yaml")


def test_resolve_source_path_cwd_default_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nothing else set, the CWD default is used only if it exists."""
    import os

    monkeypatch.delenv("COFFEE_ROASTER_MCP_CONFIG", raising=False)
    cwd_yaml = tmp_path / "coffee-roaster-mcp.yaml"
    cwd_yaml.write_text("roaster:\n  driver: hottop\n", encoding="utf-8")
    original_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = resolve_mcp_yaml_source_path(MCPDeviceConfig(), mcp_env=None)
        assert result == Path("coffee-roaster-mcp.yaml")
    finally:
        os.chdir(original_cwd)


def test_resolve_source_path_none_when_nothing_resolvable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No explicit path, no env var, no CWD default → None."""
    import os

    monkeypatch.delenv("COFFEE_ROASTER_MCP_CONFIG", raising=False)
    original_cwd = os.getcwd()
    os.chdir(tmp_path)  # empty tmp_path, no coffee-roaster-mcp.yaml
    try:
        result = resolve_mcp_yaml_source_path(MCPDeviceConfig(), mcp_env=None)
        assert result is None
    finally:
        os.chdir(original_cwd)


# ---------------------------------------------------------------------------
# read_yaml_value (#482) — best-effort read backing ConfigFieldMeta.yaml_value.
# ---------------------------------------------------------------------------


def test_read_yaml_value_returns_present_key(tmp_path: Path) -> None:
    """A key present in the yaml is returned for its managed field name."""
    src = tmp_path / "source.yaml"
    src.write_text("first_crack:\n  mode: audio\n", encoding="utf-8")
    assert read_yaml_value(src, "fc_mode") == "audio"


def test_read_yaml_value_every_managed_path(tmp_path: Path) -> None:
    """Every entry in MANAGED_YAML_PATHS resolves against its own section/key."""
    src = tmp_path / "source.yaml"
    src.write_text(
        "roaster:\n  port: /dev/ttyUSB0\n  driver: hottop\n"
        "audio:\n  input_device: USB PnP\n"
        "recording:\n  enabled: true\n  autocapture: false\n  devices: [USB PnP]\n"
        "first_crack:\n  mode: audio\n  confidence_threshold: 0.6\n"
        "session:\n  auto_t0_detection_enabled: true\n  auto_t0_drop_threshold_c: 12.0\n"
        "ambient:\n  mode: yoctopuce\n  device: METEOMK2-1\n  poll_interval_seconds: 30.0\n",
        encoding="utf-8",
    )
    expected = {
        "serial_port": "/dev/ttyUSB0",
        "roaster_driver": "hottop",
        "audio_input_device": "USB PnP",
        "recording_enabled": True,
        "recording_autocapture": False,
        "recording_devices": ["USB PnP"],
        "fc_mode": "audio",
        "fc_confidence_threshold": 0.6,
        "auto_t0_detection_enabled": True,
        "auto_t0_drop_threshold_c": 12.0,
        "ambient_mode": "yoctopuce",
        "ambient_device": "METEOMK2-1",
        "ambient_poll_interval_seconds": 30.0,
    }
    assert set(expected.keys()) == set(MANAGED_YAML_PATHS.keys())
    for field_name, expected_value in expected.items():
        assert read_yaml_value(src, field_name) == expected_value


def test_read_yaml_value_absent_key_is_none(tmp_path: Path) -> None:
    """A key absent from the yaml (but its section present) returns None."""
    src = tmp_path / "source.yaml"
    src.write_text("roaster:\n  driver: hottop\n", encoding="utf-8")
    assert read_yaml_value(src, "serial_port") is None


def test_read_yaml_value_absent_section_is_none(tmp_path: Path) -> None:
    """A yaml with no matching section at all returns None."""
    src = tmp_path / "source.yaml"
    src.write_text("roaster:\n  driver: hottop\n", encoding="utf-8")
    assert read_yaml_value(src, "fc_mode") is None


def test_read_yaml_value_none_source_path_is_none() -> None:
    """source_path=None (no yaml resolvable) fails soft to None."""
    assert read_yaml_value(None, "fc_mode") is None


def test_read_yaml_value_unknown_field_name_is_none(tmp_path: Path) -> None:
    """A field name not in MANAGED_YAML_PATHS fails soft to None."""
    src = tmp_path / "source.yaml"
    src.write_text("roaster:\n  driver: hottop\n", encoding="utf-8")
    assert read_yaml_value(src, "not_a_real_field") is None


def test_read_yaml_value_missing_file_is_none(tmp_path: Path) -> None:
    """A resolved path that does not exist on disk fails soft to None (never raises)."""
    missing = tmp_path / "does_not_exist.yaml"
    assert read_yaml_value(missing, "fc_mode") is None


def test_read_yaml_value_unparseable_yaml_is_none(tmp_path: Path) -> None:
    """Invalid YAML syntax fails soft to None rather than propagating YAMLError."""
    src = tmp_path / "bad.yaml"
    src.write_text("roaster: [unterminated\n", encoding="utf-8")
    assert read_yaml_value(src, "fc_mode") is None


def test_read_yaml_value_non_mapping_yaml_is_none(tmp_path: Path) -> None:
    """A valid-YAML-but-non-mapping source (list) fails soft to None."""
    src = tmp_path / "list.yaml"
    src.write_text("- item1\n- item2\n", encoding="utf-8")
    assert read_yaml_value(src, "fc_mode") is None


def test_read_yaml_value_non_mapping_section_is_none(tmp_path: Path) -> None:
    """A section whose value is not a mapping (e.g. a scalar) fails soft to None."""
    src = tmp_path / "scalar-section.yaml"
    src.write_text("first_crack: audio\n", encoding="utf-8")
    assert read_yaml_value(src, "fc_mode") is None


def test_read_yaml_value_known_good_fc_mode() -> None:
    """Against the real known-good yaml, fc_mode resolves to its actual audio value.

    This is the exact scenario from #482: fc_mode is unconfigured at the agent's
    own config layer (effective_value=None) but the hand-authored yaml says
    "audio" — read_yaml_value must surface that real value.
    """
    assert read_yaml_value(_KNOWN_GOOD, "fc_mode") == "audio"
    assert read_yaml_value(_KNOWN_GOOD, "fc_confidence_threshold") == 0.6
