"""Appliance systemd/env/MCP-YAML template rendering (E11-S2, issue #138, slice 2).

This module renders the three artifacts a native Pi appliance install needs
(AC2, AC6, AC7): the systemd unit that supervises the agent, the operator
environment file it loads, and the ``pi_inference`` ``coffee-roaster-mcp``
profile. Slice 3's shell installer (``packaging/pi/install.sh``, out of scope
here) copies the rendered files under ``/etc`` and enables the unit; this
module only produces correct bytes into an ``--output-dir`` staging directory.

**Closed-token, fail-closed rendering.** Each template ships inside the wheel
under ``roastpilot_agent/appliance/templates/*.in`` with ``@@TOKEN@@``
placeholders from a small, per-template closed set. Rendering validates every
template's token set *before any file is written*: an unknown token supplied,
an unsubstituted ``@@TOKEN@@`` left in the output, or a missing required
substitution aborts the whole call with :class:`ApplianceRenderError` and
writes nothing (:func:`render_appliance_files` renders and validates all three
strings up front, then performs the atomic writes only once every template has
validated clean).

**No re-typed model identity.** ``first_crack.repo_id``/``revision`` are never
CLI-supplied — they are read directly from
:mod:`roastpilot_agent.appliance.model_manifest`, the single source of truth,
so this module cannot introduce a second, driftable copy of the pinned model
identity (AGENTS.md class-sweep discipline).

**Recording stays off.** The MCP YAML template omits the ``recording:``
section entirely. ``coffee-roaster-mcp==0.2.0``'s ``RecordingConfig.enabled``
defaults to ``False`` (verified against the installed distribution's
``config.py``), so omission is a *proven* off, not a guess — matching the
convention already used by the committed
``docs/examples/coffee-roaster-mcp.known-good.yaml``, which also carries no
``recording:`` block.

**Never auto-resumes heat or fan.** The rendered systemd unit carries no
``ExecStartPre``, and its ``ExecStart`` is exactly ``roastpilot-agent serve
--host 0.0.0.0 --port <rendered port>`` — no resume/start-run flag exists on ``serve``
to begin with, and none is introduced here. A restarted service still lands in
the controller's existing ``operator_recovery_required`` flow.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Final

from roastpilot_agent.appliance.model_manifest import REPO_ID, REVISION

#: The installed package's template directory (package data shipped in the
#: wheel — see ``[tool.hatch.build.targets.wheel]``'s default packages
#: inclusion and ``tests/test_packaging.py``'s wheel-content assertion).
_TEMPLATE_PACKAGE: Final[str] = "roastpilot_agent.appliance"
_TEMPLATE_SUBDIR: Final[str] = "templates"

_SERVICE_TEMPLATE_NAME: Final[str] = "roastpilot-agent.service.in"
_ENV_TEMPLATE_NAME: Final[str] = "roastpilot-agent.env.in"
_MCP_YAML_TEMPLATE_NAME: Final[str] = "coffee-roaster-mcp.appliance.yaml.in"

#: Rendered (``.in`` suffix dropped) output filenames inside ``--output-dir``.
SERVICE_OUTPUT_FILENAME: Final[str] = "roastpilot-agent.service"
ENV_OUTPUT_FILENAME: Final[str] = "roastpilot-agent.env"
MCP_YAML_OUTPUT_FILENAME: Final[str] = "coffee-roaster-mcp.appliance.yaml"

_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"@@([A-Z0-9_]+)@@")
_IDENTITY_PATTERN: Final[re.Pattern[str]] = re.compile(r"[a-z_][a-z0-9_-]{0,31}")

#: Per-template closed token sets. Any token supplied outside this set, or any
#: ``@@TOKEN@@`` left unsubstituted in a template's own set, aborts rendering.
_SERVICE_TOKENS: Final[frozenset[str]] = frozenset({"OPERATOR_USER", "OPERATOR_GROUP", "PORT"})
_ENV_TOKENS: Final[frozenset[str]] = frozenset({"PORT", "DB_PATH", "MCP_CONFIG_PATH"})
_MCP_YAML_TOKENS: Final[frozenset[str]] = frozenset(
    {"FC_REPO_ID", "FC_REVISION", "MODEL_DIR", "SERIAL_PORT", "AUDIO_DEVICE"}
)


class ApplianceRenderError(RuntimeError):
    """Rendering could not complete safely; nothing is written on this path."""


@dataclass(frozen=True, slots=True)
class ApplianceRenderInputs:
    """Operator-/install-time values substituted into the appliance templates.

    Attributes:
        port: HTTP bind port for the appliance's ``serve``, rendered directly
            into both the unit and environment file.
        operator_user: The systemd unit's ``User=`` — the non-root operator
            account the appliance runs as. Never ``"root"`` (rejected).
        operator_group: The systemd unit's ``Group=``.
        db_path: Persistent SQLite decision-trace path (``ROASTPILOT_DB``).
        mcp_config_path: Path the rendered ``pi_inference`` MCP YAML will be
            installed at (``COFFEE_ROASTER_MCP_CONFIG``).
        model_dir: Destination root the bundled/pinned first-crack model is
            placed at (``appliance model install``'s ``--dest``) — rendered
            into the MCP YAML's ``first_crack.local_model_dir`` so the two
            commands agree on one location.
        serial_port: Hottop USB serial device below ``/dev``.
        audio_device: USB audio input device-name substring.
    """

    port: int
    operator_user: str
    operator_group: str
    db_path: Path
    mcp_config_path: Path
    model_dir: Path
    serial_port: Path
    audio_device: str


@dataclass(frozen=True, slots=True)
class RenderedApplianceFiles:
    """Paths of the three files :func:`render_appliance_files` wrote.

    Attributes:
        output_dir: The resolved (real-path) staging directory.
        service_path: The rendered systemd unit.
        env_path: The rendered operator environment file (mode ``0600``).
        mcp_yaml_path: The rendered ``pi_inference`` MCP YAML.
    """

    output_dir: Path
    service_path: Path
    env_path: Path
    mcp_yaml_path: Path

    def to_json_dict(self) -> dict[str, object]:
        """A machine-readable summary safe to print: only local paths."""
        return {
            "output_dir": str(self.output_dir),
            "service": str(self.service_path),
            "env": str(self.env_path),
            "mcp_yaml": str(self.mcp_yaml_path),
        }


def _read_template(name: str) -> str:
    """Read one packaged template's raw text by filename."""
    traversable = resources.files(_TEMPLATE_PACKAGE) / _TEMPLATE_SUBDIR / name
    return traversable.read_text(encoding="utf-8")


def render_template_text(
    template_text: str,
    tokens: dict[str, str],
    *,
    known_tokens: frozenset[str],
    template_name: str,
) -> str:
    """Substitute a closed ``@@TOKEN@@`` set; abort on unknown or missing tokens.

    Every token actually present in ``template_text`` must be in
    ``known_tokens`` and have a value in ``tokens``; every key in ``tokens``
    must be in ``known_tokens``. Nothing about this function is specific to
    one template — the per-template ``_render_*`` functions each call this
    with their own closed set, so a corrupted or hand-edited template (an
    unknown token, or one of its own known tokens left unfilled) fails the
    same way a caller bug does.

    Args:
        template_text: The raw template text (already read from disk).
        tokens: The substitution values for this render call.
        known_tokens: The closed set of tokens this template is allowed to
            contain.
        template_name: A safe, local name used only in error messages.

    Returns:
        The fully-substituted text, guaranteed to contain no ``@@TOKEN@@``
        marker.

    Raises:
        ApplianceRenderError: An unknown token was supplied, a token in
            ``template_text`` is outside ``known_tokens``, or a known token
            has no value in ``tokens``.
    """
    unknown_supplied = set(tokens) - known_tokens
    if unknown_supplied:
        raise ApplianceRenderError(
            f"{template_name}: unknown token(s) supplied: {sorted(unknown_supplied)}"
        )
    if "@@" in _TOKEN_PATTERN.sub("", template_text):
        raise ApplianceRenderError(
            f"{template_name}: malformed, truncated, or unsubstituted token marker"
        )
    template_tokens = {match.group(1) for match in _TOKEN_PATTERN.finditer(template_text)}
    unknown_template = template_tokens - known_tokens
    if unknown_template:
        raise ApplianceRenderError(
            f"{template_name}: template contains unknown token(s): {sorted(unknown_template)}"
        )
    missing_template = known_tokens - template_tokens
    if missing_template:
        raise ApplianceRenderError(
            f"{template_name}: template is missing required token(s): {sorted(missing_template)}"
        )
    missing_values = known_tokens - set(tokens)
    if missing_values:
        raise ApplianceRenderError(
            f"{template_name}: missing substitution for token(s): {sorted(missing_values)}"
        )

    def _substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        return tokens[name]

    rendered = _TOKEN_PATTERN.sub(_substitute, template_text)
    if "@@" in rendered:
        raise ApplianceRenderError(
            f"{template_name}: malformed, truncated, or unsubstituted token marker"
        )
    return rendered


def _validate_plain_value(value: object, *, field: str) -> str:
    """Return a non-empty operator value with control and token markers rejected."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ApplianceRenderError(f"{field} must be non-empty and have no surrounding whitespace")
    if "@@" in value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ApplianceRenderError(f"{field} contains an unsafe control or token marker")
    return value


def _validate_path(value: Path, *, field: str, device: bool = False) -> str:
    """Validate a path used in an env file or YAML scalar before rendering."""
    text = _validate_plain_value(str(value), field=field)
    path = Path(text)
    if not path.is_absolute() or ".." in path.parts:
        raise ApplianceRenderError(f"{field} must be an absolute path without '..'")
    if any(character.isspace() or character in ('"', "'", "#", "=", "\\") for character in text):
        raise ApplianceRenderError(f"{field} contains an unsafe structural character")
    if device and not text.startswith("/dev/"):
        raise ApplianceRenderError(f"{field} must name a device below /dev")
    return text


def _validate_port(port: object) -> int:
    """Validate one appliance HTTP port as a non-boolean TCP port number."""
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ApplianceRenderError("port must be an integer in 1..65535")
    return port


def _validate_operator_identity(operator_user: str, operator_group: str) -> None:
    """Reject an empty or ``root`` operator identity before any rendering.

    AC2 requires the systemd unit's ``User=``/``Group=`` to be "the invoking
    operator (never root)". This is enforced here, not only documented in the
    template, so a caller cannot accidentally render a root-owned unit.
    """
    user = _validate_plain_value(operator_user, field="operator_user")
    group = _validate_plain_value(operator_group, field="operator_group")
    if not _IDENTITY_PATTERN.fullmatch(user) or not _IDENTITY_PATTERN.fullmatch(group):
        raise ApplianceRenderError(
            "operator_user and operator_group must be safe Linux identity names"
        )
    if user == "root" or group == "root":
        raise ApplianceRenderError(
            "refusing to render a systemd unit with User/Group 'root' — "
            "the appliance must run as a non-root operator account"
        )


def render_service_unit(inputs: ApplianceRenderInputs) -> str:
    """Render the systemd unit (AC2, AC7): fully substituted, ready to write.

    Args:
        inputs: The render-time values (only ``operator_user``/
            ``operator_group`` are used here).

    Returns:
        The rendered unit file text.

    Raises:
        ApplianceRenderError: ``operator_user``/``operator_group`` is empty or
            ``"root"``, or the template fails closed-token validation.
    """
    _validate_operator_identity(inputs.operator_user, inputs.operator_group)
    template_text = _read_template(_SERVICE_TEMPLATE_NAME)
    tokens = {
        "OPERATOR_USER": inputs.operator_user,
        "OPERATOR_GROUP": inputs.operator_group,
        "PORT": str(_validate_port(inputs.port)),
    }
    return render_template_text(
        template_text, tokens, known_tokens=_SERVICE_TOKENS, template_name=_SERVICE_TEMPLATE_NAME
    )


def render_env_file(inputs: ApplianceRenderInputs) -> str:
    """Render the operator environment file (AC1, AC4): fully substituted.

    Args:
        inputs: The render-time values (``port``, ``db_path``,
            ``mcp_config_path``).

    Returns:
        The rendered env file text. The ``OPENROUTER_API_KEY=`` line is always
        blank — no credential value is ever defaulted, echoed, or logged here.

    Raises:
        ApplianceRenderError: The template fails closed-token validation.
    """
    port = _validate_port(inputs.port)
    db_path = _validate_path(inputs.db_path, field="db_path")
    mcp_config_path = _validate_path(inputs.mcp_config_path, field="mcp_config_path")
    template_text = _read_template(_ENV_TEMPLATE_NAME)
    tokens = {
        "PORT": str(port),
        "DB_PATH": db_path,
        "MCP_CONFIG_PATH": mcp_config_path,
    }
    return render_template_text(
        template_text, tokens, known_tokens=_ENV_TOKENS, template_name=_ENV_TEMPLATE_NAME
    )


def render_mcp_yaml(inputs: ApplianceRenderInputs) -> str:
    """Render the ``pi_inference`` MCP YAML profile (AC6): fully substituted.

    ``repo_id``/``revision`` always come from
    :mod:`roastpilot_agent.appliance.model_manifest` — never from ``inputs`` —
    so this can never diverge from the manifest that
    ``appliance model install`` places files against.

    Args:
        inputs: The render-time values (only ``model_dir`` is used here).

    Returns:
        The rendered MCP YAML text.

    Raises:
        ApplianceRenderError: The template fails closed-token validation.
    """
    model_dir = _validate_path(inputs.model_dir, field="model_dir")
    serial_port = _validate_path(inputs.serial_port, field="serial_port", device=True)
    audio_device = _validate_plain_value(inputs.audio_device, field="audio_device")
    template_text = _read_template(_MCP_YAML_TEMPLATE_NAME)
    tokens = {
        "FC_REPO_ID": REPO_ID,
        "FC_REVISION": REVISION,
        "MODEL_DIR": json.dumps(model_dir),
        "SERIAL_PORT": json.dumps(serial_port),
        "AUDIO_DEVICE": json.dumps(audio_device),
    }
    return render_template_text(
        template_text, tokens, known_tokens=_MCP_YAML_TOKENS, template_name=_MCP_YAML_TEMPLATE_NAME
    )


def _stage_write(path: Path, content: str, *, mode: int) -> Path:
    """Stage ``content`` beside ``path`` with an exact octal ``mode``.

    Streams to a same-directory temp file, ``fsync``s, ``chmod``s to the exact
    requested mode, without replacing the destination. The temp file is
    removed on failure so no half-written artifact reaches ``path``.
    """
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, mode)
        return tmp_path
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def render_appliance_files(
    output_dir: Path, inputs: ApplianceRenderInputs
) -> RenderedApplianceFiles:
    """Render and atomically write all three appliance artifacts.

    Every template is rendered (and closed-token-validated) *before* any file
    is written — a validation failure in any one of the three leaves
    ``output_dir`` untouched, never a partial set of files.

    Args:
        output_dir: Staging directory for the rendered files (created if
            absent). This is deliberately not ``/etc`` — the shell installer
            (slice 3, out of scope here) copies the rendered files to their
            final system locations.
        inputs: The render-time substitution values.

    Returns:
        The paths of the three files written, all inside ``output_dir``.

    Raises:
        ApplianceRenderError: Any template fails closed-token validation, or
            the operator identity is empty/``root``.
        OSError: A local filesystem operation fails (e.g. an unwritable
            ``output_dir``).
    """
    # Render and validate all three before writing anything.
    service_text = render_service_unit(inputs)
    env_text = render_env_file(inputs)
    mcp_yaml_text = render_mcp_yaml(inputs)

    output_dir.mkdir(parents=True, exist_ok=True)
    service_path = output_dir / SERVICE_OUTPUT_FILENAME
    env_path = output_dir / ENV_OUTPUT_FILENAME
    mcp_yaml_path = output_dir / MCP_YAML_OUTPUT_FILENAME

    planned = (
        (service_path, service_text, 0o644),
        (env_path, env_text, 0o600),
        (mcp_yaml_path, mcp_yaml_text, 0o644),
    )
    staged: list[tuple[Path, Path]] = []
    try:
        for path, text, mode in planned:
            staged.append((path, _stage_write(path, text, mode=mode)))
        backups: dict[Path, Path] = {}
        committed: list[Path] = []
        try:
            for path, stage_path in staged:
                if path.exists():
                    backup_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.backup")
                    os.replace(path, backup_path)
                    backups[path] = backup_path
                os.replace(stage_path, path)
                committed.append(path)
        except BaseException:
            for path in reversed(committed):
                path.unlink(missing_ok=True)
                backup = backups.pop(path, None)
                if backup is not None:
                    os.replace(backup, path)
            for path, backup in backups.items():
                path.unlink(missing_ok=True)
                os.replace(backup, path)
            raise
        for backup in backups.values():
            backup.unlink(missing_ok=True)
    except BaseException:
        for _, stage_path in staged:
            stage_path.unlink(missing_ok=True)
        raise

    return RenderedApplianceFiles(
        output_dir=output_dir,
        service_path=service_path,
        env_path=env_path,
        mcp_yaml_path=mcp_yaml_path,
    )
