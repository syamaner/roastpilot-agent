"""Passthrough-merge render of managed config fields into the MCP yaml (D78-4, #420).

The ``coffee-roaster-mcp`` child reads its configuration from a YAML file
(``coffee-roaster-mcp.yaml``).  The agent manages a subset of those fields via
the unified config (``AppConfig``); the rest — pinned model revision,
``onnx_threads``, Pi-vs-Mac inference profile, WAV-replay paths, custom
``log_dir``, etc. — are hand-authored by the operator and must **pass through
unchanged**.

This module provides a single function, :func:`render_mcp_yaml`, that:

1. Reads the **existing** yaml at the source path (empty dict if absent — a
   fresh install renders defaults only).
2. Overlays the managed fields from :class:`MCPDeviceConfig` into the
   appropriate sections.
3. Writes the result to *dest_path* using an atomic rename (temp file →
   :func:`os.replace`), so the MCP child never reads a torn file.

The merge is **non-destructive**: keys not covered by ``MCPDeviceConfig`` are
copied verbatim from the source.  A field set to ``None`` in the device config
is **not written** — it stays at whatever value the existing yaml carries (or
absent/default if the yaml has no value).

The rendered yaml is written once on each (re)spawn (before
:meth:`~roastpilot_agent.mcp_client.MCPServerProcess.start`) and pointed to
via the ``COFFEE_ROASTER_MCP_CONFIG`` environment variable so the child reads
the merged config rather than the raw operator yaml.

Design note: ``mcp_yaml.py`` is intentionally a thin, pure-function module
with no async, no FastAPI, no MCP client calls — it is IO-only (file read/
write) and is safe to call from a thread (``asyncio.to_thread``) or directly
from a synchronous context (e.g. ``build_server_parameters``).
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import Any, cast

import yaml

from roastpilot_agent.config import MCPDeviceConfig


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict with *overlay* merged recursively into *base*.

    Keys present only in *base* survive unchanged (the passthrough guarantee).
    Keys present in *overlay* overwrite the corresponding key in *base* at the
    leaf level.  For nested mappings, the merge descends recursively so inner
    keys of *base* that are absent from *overlay* also survive.

    Args:
        base: The existing yaml dict (the operator's hand-authored values).
        overlay: The managed-field dict derived from ``MCPDeviceConfig``
            (only non-``None`` fields are included).

    Returns:
        A new dict (neither ``base`` nor ``overlay`` is mutated).
    """
    merged: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        existing = merged.get(key)
        if isinstance(value, dict) and isinstance(existing, dict):
            merged[key] = _deep_merge(
                cast(dict[str, Any], existing),
                cast(dict[str, Any], value),
            )
        else:
            merged[key] = value
    return merged


def _device_config_to_overlay(cfg: MCPDeviceConfig) -> dict[str, Any]:
    """Convert *cfg* to a sparse overlay dict for passthrough-merge.

    Only fields that are not ``None`` are included so that unset fields do not
    overwrite the operator's existing values in the source yaml.

    Args:
        cfg: The agent-side device config holding the managed fields.

    Returns:
        A nested dict mirroring the MCP yaml section/key structure, containing
        only the non-``None`` managed fields.
    """
    overlay: dict[str, Any] = {}

    # --- roaster section (serial port + driver) ---
    roaster: dict[str, Any] = {}
    if cfg.serial_port is not None:
        roaster["port"] = cfg.serial_port
    if cfg.roaster_driver is not None:
        roaster["driver"] = cfg.roaster_driver
    if roaster:
        overlay["roaster"] = roaster

    # --- audio section (input device) ---
    audio: dict[str, Any] = {}
    if cfg.audio_input_device is not None:
        audio["input_device"] = cfg.audio_input_device
    if audio:
        overlay["audio"] = audio

    # --- recording section ---
    recording: dict[str, Any] = {}
    if cfg.recording_enabled is not None:
        recording["enabled"] = cfg.recording_enabled
    if cfg.recording_autocapture is not None:
        recording["autocapture"] = cfg.recording_autocapture
    if cfg.recording_devices is not None:
        # Pydantic stores as tuple; yaml expects a list.
        recording["devices"] = list(cfg.recording_devices)
    if recording:
        overlay["recording"] = recording

    # --- first_crack section ---
    fc: dict[str, Any] = {}
    if cfg.fc_mode is not None:
        fc["mode"] = cfg.fc_mode
    if cfg.fc_confidence_threshold is not None:
        fc["confidence_threshold"] = cfg.fc_confidence_threshold
    if fc:
        overlay["first_crack"] = fc

    # --- session section (auto-T0) ---
    session: dict[str, Any] = {}
    if cfg.auto_t0_detection_enabled is not None:
        session["auto_t0_detection_enabled"] = cfg.auto_t0_detection_enabled
    if cfg.auto_t0_drop_threshold_c is not None:
        session["auto_t0_drop_threshold_c"] = cfg.auto_t0_drop_threshold_c
    if session:
        overlay["session"] = session

    return overlay


def render_mcp_yaml(
    device_cfg: MCPDeviceConfig,
    source_path: Path | None,
    dest_path: Path,
) -> None:
    """Render the managed device fields onto the existing MCP yaml (D78-4, #420).

    Reads the operator's existing ``coffee-roaster-mcp.yaml`` at *source_path*,
    overlays only the non-``None`` fields from *device_cfg*, and writes the
    result to *dest_path* atomically.  Keys in the source yaml that are not
    covered by ``MCPDeviceConfig`` pass through byte-for-byte.

    When *source_path* is ``None`` or does not exist, the overlay is written
    alone (an agent with no hand-authored yaml gets only the managed fields;
    the MCP child fills the rest from its own defaults).

    The write is atomic: content is written to a sibling temp file in the same
    directory as *dest_path* and renamed over it via :func:`os.replace`.  The
    MCP child therefore never reads a torn/partial file even if the process is
    interrupted between the write and the rename.

    This function is **blocking** (file I/O) and intended to be called from a
    thread (e.g. ``asyncio.to_thread``) or directly from a synchronous context
    such as :meth:`~roastpilot_agent.mcp_client.MCPServerProcess.start`.  It
    has no side effects beyond the file write.

    Args:
        device_cfg: The agent's managed device configuration.  Only non-``None``
            fields are written; ``None`` fields leave the source yaml value
            intact.
        source_path: Path to the operator's existing ``coffee-roaster-mcp.yaml``,
            or ``None`` when no hand-authored yaml exists.  A missing file is
            treated as an empty yaml (all keys absent).
        dest_path: Destination path for the rendered yaml.  Parent directories
            are created if absent.

    Raises:
        yaml.YAMLError: If the source yaml cannot be parsed.
        OSError: If the source yaml cannot be read or the dest cannot be written.
    """
    # 1. Load the existing operator yaml (or start from an empty base).
    base: dict[str, Any] = {}
    if source_path is not None and source_path.exists():
        with source_path.open("r", encoding="utf-8") as fh:
            loaded: Any = yaml.safe_load(fh)
        if loaded is None:
            # Empty file — treat as no config, not an error.
            pass
        elif not isinstance(loaded, dict):
            # A valid-YAML-but-non-mapping source (list, scalar) would silently
            # drop all operator config — fail closed instead.
            raise ValueError(
                f"MCP yaml source at {source_path!r} is not a mapping "
                f"(got {type(loaded).__name__!r}); refusing to render over it"
            )
        else:
            # yaml.safe_load returns Any; cast here after isinstance guard.
            base = cast(dict[str, Any], loaded)

    # 2. Build the overlay from the managed fields (None → skip, not overwrite).
    overlay = _device_config_to_overlay(device_cfg)

    # 3. Deep-merge: unmanaged keys pass through from base.
    merged = _deep_merge(base, overlay)

    # 4. Write atomically: temp file in the same dir → os.replace.
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=dest_path.parent, suffix=".yaml.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.safe_dump(merged, fh, default_flow_style=False, allow_unicode=True)
        os.replace(tmp_name, dest_path)
    except Exception:  # pragma: no cover - requires injecting a real disk/OS write failure
        # Clean up the temp file if something goes wrong before the rename.
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
