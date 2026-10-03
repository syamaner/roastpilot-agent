"""``roastpilot-agent cold-characterisation``: grammar, inputs, host facts, output (#954).

This synchronous layer parses one explicit argv grammar, builds the admitted
composition inputs, reads bounded host facts and hands one run to
:func:`roastpilot_agent.cold_runner.run_hosted` under ``asyncio.run``.  Every step
before the run prints only the closed summary with ``run_invoked=false``, which
means only that this invocation started no cold child.

Output hygiene: a usage error prints one fixed line and never echoes argv; help is
fixed grammar; config, input and host-fact failures print fixed closed tokens and
never exception text.  Provenance flags are an operator assertion, never an
attestation of the live bytes.  Temperatures are Celsius.
"""

import argparse
import asyncio
import importlib.metadata
import math
import os
import platform
import re
import sys
import typing
from collections.abc import Callable, Sequence
from pathlib import Path

import pydantic

from roastpilot_agent import cold_runner
from roastpilot_agent.cold_characterisation.advisory_sampler import ColdAdvisorySpec
from roastpilot_agent.cold_characterisation.engine import ColdEngineHost
from roastpilot_agent.cold_characterisation.host import ColdHostBoundError, LinuxHostBoundsReader
from roastpilot_agent.cold_characterisation.identity import (
    BOOT_ID_PATH,
    AgentBuildProvenance,
    ColdArtefactKind,
    ColdIdentityError,
)
from roastpilot_agent.cold_composition import ColdCompositionInputs, ColdHostFacts
from roastpilot_agent.config_store import ConfigFileError, load_app_config
from roastpilot_agent.live import default_spa_dir

PROG: typing.Final = "roastpilot-agent cold-characterisation"
USAGE_ERROR_LINE: typing.Final = f"{PROG}: usage error; see --help\n"
DEFAULT_HOST: typing.Final = "127.0.0.1"
DEFAULT_PORT: typing.Final = 8000
PI_MODEL_PATH: typing.Final = Path("/proc/device-tree/model")
CPUINFO_PATH: typing.Final = Path("/proc/cpuinfo")
_PI_MODEL_LIMIT: typing.Final = 256
_CPUINFO_LIMIT: typing.Final = 64 * 1024
_REVISION_LINE: typing.Final = re.compile(r"Revision\s*:\s*([0-9a-f]{4,8})")
_INPUT_EXIT: typing.Final = 2
_REFUSAL_EXIT: typing.Final = 3
_CANCELLED_EXIT: typing.Final = 130


class _Unset:
    """The private sentinel default of a single-use option."""


_UNSET: typing.Final = _Unset()


class _ColdParser(argparse.ArgumentParser):
    """Parser whose every error is one fixed line: no argv value is ever echoed."""

    def error(self, message: str) -> typing.NoReturn:
        """Discard ``message``; write the fixed usage line and exit 2."""
        del message
        sys.stderr.write(USAGE_ERROR_LINE)
        sys.exit(_INPUT_EXIT)


class _SingleUse(argparse.Action):
    """Store the first occurrence; any repeat of the destination is a usage error."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[typing.Any] | None,
        option_string: str | None = None,
    ) -> None:
        """Store ``values`` once; refuse a repeat through the fixed error."""
        if getattr(namespace, self.dest) is not _UNSET:
            parser.error("duplicate option")
        setattr(namespace, self.dest, values)


def _finite_celsius(text: str) -> float:
    """Parse a finite float (Celsius); no range is applied."""
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("not finite")
    return value


def _absolute(text: str) -> str:
    """Admit a lexically absolute path string."""
    if not os.path.isabs(text):
        raise ValueError("not absolute")
    return text


def _non_empty(text: str) -> str:
    """Admit a non-empty string verbatim."""
    if not text:
        raise ValueError("empty")
    return text


def _revision(text: str) -> str:
    """Admit a 40-character lowercase hex source revision."""
    if re.fullmatch(r"[0-9a-f]{40}", text) is None:
        raise ValueError("not a revision")
    return text


def _sha256(text: str) -> str:
    """Admit a 64-character lowercase hex digest."""
    if re.fullmatch(r"[0-9a-f]{64}", text) is None:
        raise ValueError("not a digest")
    return text


def build_parser() -> argparse.ArgumentParser:
    """Build the fixed cold-characterisation grammar.

    Returns:
        The parser; only ``--protected-root`` may repeat.
    """
    parser = _ColdParser(
        prog=PROG,
        allow_abbrev=False,
        description=(
            "Run one supervised cold characterisation with the read-only Agent API, "
            "cold observation stream and SPA hosted on the same event loop. "
            "Temperatures are Celsius. See docs/deployment/cold-characterisation-runbook.md."
        ),
    )

    def single(flag: str, *, required: bool = True, **kwargs: typing.Any) -> None:
        parser.add_argument(flag, action=_SingleUse, default=_UNSET, required=required, **kwargs)

    single("--profile-name", type=_non_empty, help="explicit per-run profile name")
    single("--target-drop-temp-c", type=_finite_celsius, help="explicit target drop, Celsius")
    single("--evidence-dir", type=_absolute, help="absolute primary evidence root")
    single(
        "--secondary-evidence-dir",
        type=_absolute,
        help="absolute secondary evidence root (recorded only; never opened)",
    )
    parser.add_argument(
        "--protected-root",
        action="append",
        type=_absolute,
        default=None,
        help="absolute protected root (repeatable, zero or more)",
    )
    for flag in (
        "--audio-device-identity",
        "--serial-port-path",
        "--stimulus-block",
        "--operator-host-notes",
        "--operator-psu-notes",
        "--operator-cooling-notes",
    ):
        single(flag, type=_non_empty, help="operator-asserted text")
    single("--source-revision", type=_revision, help="asserted 40-hex source revision")
    single("--source-tree", choices=("clean", "dirty"), help="asserted source tree state")
    single(
        "--artefact-kind",
        choices=tuple(kind.value for kind in ColdArtefactKind),
        help="asserted artefact kind",
    )
    single(
        "--artefact-sha256",
        type=_sha256,
        required=False,
        help="asserted 64-hex artefact digest (packaged kinds only)",
    )
    single("--host", required=False, help="bind host (default 127.0.0.1)")
    single("--port", type=int, required=False, help="bind port (default 8000)")
    single("--spa-dir", required=False, help="built SPA directory (bundled build if omitted)")
    return parser


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    """Parse ``argv`` and resolve unset optional options to their defaults."""
    args = build_parser().parse_args(list(argv))
    for name, default in (
        ("artefact_sha256", None),
        ("spa_dir", None),
        ("host", DEFAULT_HOST),
        ("port", DEFAULT_PORT),
    ):
        if getattr(args, name) is _UNSET:
            setattr(args, name, default)
    return args


Opener = Callable[[Path, int], bytes]


def read_bounded(path: Path, limit: int) -> bytes:
    """Read at most ``limit`` bytes of ``path`` (the production opener).

    Args:
        path: A fixed module-constant path.
        limit: The byte bound.

    Returns:
        Up to ``limit`` bytes.
    """
    with path.open("rb") as handle:
        return handle.read(limit)


def _read_within(opener: Opener, path: Path, limit: int) -> bytes | None:
    """Read with a one-byte over-read check; ``None`` when absent, failing or over."""
    try:
        data = opener(path, limit + 1)
    except Exception:
        # Any ordinary probe failure refuses; BaseException (interrupts) propagates.
        return None
    return data if len(data) <= limit else None


def _pi_model(opener: Opener) -> str | None:
    """The device-tree model: bounded, strict ASCII, printable and non-empty."""
    data = _read_within(opener, PI_MODEL_PATH, _PI_MODEL_LIMIT)
    if data is None:
        return None
    try:
        text = data.rstrip(b"\x00\n").decode("ascii")
    except UnicodeDecodeError:
        return None
    return text if text and text.isprintable() else None


def _pi_revision(opener: Opener) -> str | None:
    """The single ``Revision`` value of ``/proc/cpuinfo``; zero or several refuse."""
    data = _read_within(opener, CPUINFO_PATH, _CPUINFO_LIMIT)
    if data is None:
        return None
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        return None
    found = [
        match.group(1)
        for line in text.splitlines()
        if (match := _REVISION_LINE.fullmatch(line)) is not None
    ]
    return found[0] if len(found) == 1 else None


def _platform_values() -> tuple[str, str, str, str, str]:
    """Python version, platform, machine, operating system and kernel release."""
    return (
        platform.python_version(),
        platform.platform(),
        platform.machine(),
        platform.system(),
        platform.release(),
    )


def _mcp_version() -> str:
    """The installed ``coffee-roaster-mcp`` self-reported version."""
    return importlib.metadata.version("coffee-roaster-mcp")


def read_host_facts(
    *,
    opener: Opener = read_bounded,
    mcp_version: Callable[[], str] = _mcp_version,
    platform_values: Callable[[], object] = _platform_values,
) -> ColdHostFacts | None:
    """Read the caller-observed host facts, failing closed.

    Args:
        opener: Reads at most N bytes of a fixed path.
        mcp_version: Returns the installed MCP package version.
        platform_values: Returns the five platform strings.

    Returns:
        The admitted facts, or ``None`` on any absent, oversized or malformed fact
        or any ordinary (``Exception``) probe failure.  Interrupts and other
        ``BaseException`` subclasses propagate unchanged; no exception is retained
        or formatted.
    """
    try:
        version = mcp_version()
    except Exception:
        return None
    try:
        values = platform_values()
    except Exception:
        return None
    if type(values) is not tuple:
        return None
    shape = typing.cast(tuple[object, ...], values)
    if len(shape) != 5:
        return None
    # Element types are judged by the strict ColdHostFacts model below.
    python_version, platform_text, machine, operating_system, kernel = typing.cast(
        tuple[str, str, str, str, str], shape
    )
    pi_model = _pi_model(opener)
    pi_revision = _pi_revision(opener)
    if pi_model is None or pi_revision is None:
        return None
    try:
        return ColdHostFacts(
            coffee_roaster_mcp_version=version,
            python_version=python_version,
            platform=platform_text,
            machine=machine,
            operating_system=operating_system,
            kernel=kernel,
            pi_model=pi_model,
            pi_revision=pi_revision,
            boot_id_path=BOOT_ID_PATH,
        )
    except pydantic.ValidationError:
        return None


def _refuse(refusal: cold_runner.CliRefusal, code: int) -> int:
    """Attempt the closed refusal summary (run not invoked) and return ``code``."""
    return cold_runner.report_summary(
        cold_runner.ColdRunSummary(
            run_invoked=False,
            result=cold_runner.SummaryResult.CLI_REFUSED,
            exit_code=code,
            cli_refusal=refusal,
            http_server=cold_runner.HttpServerStatus.TASK_NOT_CREATED,
        )
    )


def _resolve_spa_dir(explicit: str | None) -> Path | None:
    """The SPA directory holding ``index.html``, else ``None``.

    An unreadable, over-long or NUL-bearing path refuses like a missing build.
    """
    try:
        candidate = default_spa_dir() if explicit is None else Path(explicit)
        if candidate is None or not (candidate / "index.html").is_file():
            return None
    except (OSError, ValueError):
        return None
    return candidate


def _run(argv: Sequence[str]) -> int:
    """Steps 1-9 of the synchronous pre-loop construction."""
    args = _parse(argv)
    try:
        spec = ColdAdvisorySpec(
            profile_name=args.profile_name,
            target_drop_temp_c=args.target_drop_temp_c,
            charge_guidance_min_c=None,
            charge_guidance_max_c=None,
        )
        provenance = AgentBuildProvenance(
            source_revision=args.source_revision,
            source_tree_dirty=args.source_tree == "dirty",
            artefact_kind=ColdArtefactKind(args.artefact_kind),
            artefact_sha256=args.artefact_sha256,
        )
    except (pydantic.ValidationError, ColdIdentityError):
        return _refuse(cold_runner.CliRefusal.INPUT_NOT_ADMITTED, _INPUT_EXIT)
    try:
        config, _injected = load_app_config()
    except (ConfigFileError, pydantic.ValidationError, OSError, ValueError):
        return _refuse(cold_runner.CliRefusal.CONFIG_NOT_LOADED, _REFUSAL_EXIT)
    facts = read_host_facts()
    if facts is None:
        return _refuse(cold_runner.CliRefusal.HOST_FACTS_NOT_READ, _REFUSAL_EXIT)
    try:
        reader: ColdEngineHost = LinuxHostBoundsReader()
    except ColdHostBoundError:
        return _refuse(cold_runner.CliRefusal.HOST_READER_UNAVAILABLE, _REFUSAL_EXIT)
    spa_dir = _resolve_spa_dir(args.spa_dir)
    if spa_dir is None:
        return _refuse(cold_runner.CliRefusal.SPA_NOT_FOUND, _REFUSAL_EXIT)
    try:
        inputs = ColdCompositionInputs(
            spec=spec,
            build_provenance=provenance,
            host_facts=facts,
            device_config=config.mcp_device,
            pi_evidence_root=args.evidence_dir,
            laptop_evidence_root=args.secondary_evidence_dir,
            protected_roots=tuple(args.protected_root or ()),
            audio_device_identity=args.audio_device_identity,
            serial_port_path=args.serial_port_path,
            stimulus_block=args.stimulus_block,
            operator_host_notes=args.operator_host_notes,
            operator_psu_notes=args.operator_psu_notes,
            operator_cooling_notes=args.operator_cooling_notes,
        )
    except pydantic.ValidationError:
        return _refuse(cold_runner.CliRefusal.INPUT_NOT_ADMITTED, _INPUT_EXIT)
    return asyncio.run(
        cold_runner.run_hosted(
            config,
            inputs,
            host_reader=reader,
            spa_dir=spa_dir,
            bind_host=args.host,
            bind_port=args.port,
        )
    )


def main(argv: Sequence[str]) -> int:
    """Run the cold-characterisation action.

    A usage error or ``--help`` exits through ``SystemExit`` (2 with one fixed
    line, or 0).  An interrupt or cancellation that escapes returns 130.  If the
    runner had already begun a report attempt, nothing more is written (the
    earlier output may be partial and is never a receipt).  Otherwise one closed
    summary is written: ``cancelled_before_run`` when the engine await was never
    reached, else ``cancelled`` with the child unknown.  No signal is claimed.

    Args:
        argv: The arguments after ``cold-characterisation``.

    Returns:
        The closed exit code.
    """
    try:
        return _run(argv)
    except (KeyboardInterrupt, asyncio.CancelledError):
        if cold_runner.reported_exit_code() is not None:
            return _CANCELLED_EXIT
        invoked = cold_runner.run_invoked()
        return cold_runner.report_summary(
            cold_runner.ColdRunSummary(
                run_invoked=invoked,
                result=(
                    cold_runner.SummaryResult.CANCELLED
                    if invoked
                    else cold_runner.SummaryResult.CANCELLED_BEFORE_RUN
                ),
                exit_code=_CANCELLED_EXIT,
            )
        )
