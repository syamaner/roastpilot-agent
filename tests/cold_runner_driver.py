"""Test-only driver for the cold runner process tests (#954 U4); never collected.

Run as ``python -m tests.cold_runner_driver MODE SPA_DIR`` from the repository root.
It calls the real :func:`cold_runner.run_hosted` with a fake cold run, the real
``os._exit``, real loop signal handlers and a real uvicorn server on an ephemeral
loopback port.  No MCP child, provider, serial port or ``/proc`` read is involved.

Modes:
    pending: the fake run returns an admitted pending row (exit 80).
    signals: prints ``READY``; on cancellation prints ``CANCELLED`` and waits for a
        stdin release line; every signal callback is followed by ``SIG <n>``.
    http: prints ``PORT <n>`` and waits for a stdin release line, then returns an
        ordinary not-conformant row (exit 6).
    pre_sigterm: the store's initialisation prints ``READY_PRE`` and blocks forever.
"""

import asyncio
import socket
import sys
import typing
from collections.abc import Callable
from pathlib import Path

import uvicorn

from roastpilot_agent import cold_runner
from roastpilot_agent.cold_characterisation.advisory_sampler import ColdAdvisorySpec
from roastpilot_agent.cold_characterisation.evidence_lifecycle import ColdRunTerminationReason
from roastpilot_agent.cold_characterisation.host import HostBoundSample
from roastpilot_agent.cold_characterisation.identity import (
    BOOT_ID_PATH,
    AgentBuildProvenance,
    ColdArtefactKind,
)
from roastpilot_agent.cold_characterisation.two_phase import (
    ColdChildOwnership,
    ColdTwoPhaseAdvisoryPath,
    ColdTwoPhaseOutcome,
    ColdTwoPhaseProviderCheck,
    ColdTwoPhaseResult,
)
from roastpilot_agent.cold_composition import ColdCompositionInputs, ColdHostFacts
from roastpilot_agent.config import AppConfig, MCPDeviceConfig
from roastpilot_agent.store import RoastStore


def say(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def pending_row() -> ColdTwoPhaseResult:
    return ColdTwoPhaseResult(
        outcome=ColdTwoPhaseOutcome.NOT_CONFORMANT,
        start_refusal=None,
        termination_reason=ColdRunTerminationReason.PHASE_ABORTED,
        child_ownership=ColdChildOwnership.OWNED_STOP_CONFIRMED,
        manifest_sha256="d" * 64,
        conformance=None,
        advisory_path=ColdTwoPhaseAdvisoryPath.FAILED_RUN_TERMINAL,
        provider_check=ColdTwoPhaseProviderCheck.PENDING_AT_CHECK,
    )


def ordinary_row() -> ColdTwoPhaseResult:
    return ColdTwoPhaseResult(
        outcome=ColdTwoPhaseOutcome.NOT_CONFORMANT,
        start_refusal=None,
        termination_reason=None,
        child_ownership=ColdChildOwnership.OWNED_STOP_CONFIRMED,
        manifest_sha256="d" * 64,
        conformance=None,
        advisory_path=ColdTwoPhaseAdvisoryPath.NOT_APPLICABLE,
        provider_check=ColdTwoPhaseProviderCheck.NOT_CHECKED,
    )


def inputs() -> ColdCompositionInputs:
    return ColdCompositionInputs(
        spec=ColdAdvisorySpec(
            profile_name="driver-profile",
            target_drop_temp_c=200.0,
            charge_guidance_min_c=None,
            charge_guidance_max_c=None,
        ),
        build_provenance=AgentBuildProvenance(
            source_revision="0" * 40,
            source_tree_dirty=False,
            artefact_kind=ColdArtefactKind.EDITABLE_SOURCE,
            artefact_sha256=None,
        ),
        host_facts=ColdHostFacts(
            coffee_roaster_mcp_version="0.2.2",
            python_version="3.11",
            platform="driver",
            machine="driver",
            operating_system="driver",
            kernel="driver",
            pi_model="driver",
            pi_revision="c04170",
            boot_id_path=BOOT_ID_PATH,
        ),
        device_config=MCPDeviceConfig(),
        pi_evidence_root="/nonexistent/primary",
        laptop_evidence_root="/nonexistent/secondary",
        protected_roots=(),
        audio_device_identity="driver",
        serial_port_path="/nonexistent/serial",
        stimulus_block="driver",
        operator_host_notes="driver",
        operator_psu_notes="driver",
        operator_cooling_notes="driver",
    )


class NeverHost:
    def check_start_bounds(self, evidence_root: Path) -> None:
        raise AssertionError("host port reached")

    def sample(self, evidence_root: Path) -> HostBoundSample:
        raise AssertionError("host port reached")


class AnnouncingSignals:
    """The production loop port, announcing ``SIG <n>`` after each callback."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._port = cold_runner._LoopSignals(loop)  # pyright: ignore[reportPrivateUsage]

    def install(self, signum: int, callback: Callable[[], None]) -> None:
        def announced() -> None:
            callback()
            say(f"SIG {signum}")

        self._port.install(signum, announced)

    def restore(self) -> None:
        self._port.restore()


class BlockingStore(RoastStore):
    async def initialize(self) -> None:
        say("READY_PRE")
        await asyncio.Event().wait()


async def release_line() -> None:
    await asyncio.get_running_loop().run_in_executor(None, sys.stdin.readline)


async def main(mode: str, spa_dir: Path) -> int:
    ports: list[int] = []

    def config_factory(app: typing.Any, **kwargs: typing.Any) -> uvicorn.Config:
        config = uvicorn.Config(app, **kwargs)
        real_bind = config.bind_socket

        def bind() -> socket.socket:
            sock = real_bind()
            ports.append(sock.getsockname()[1])
            return sock

        config.bind_socket = bind
        return config

    async def run(*_args: object, **_kwargs: object) -> object:
        if mode == "pending":
            return pending_row()
        if mode == "http":
            say(f"PORT {ports[0]}")
            await release_line()
            return ordinary_row()
        say("READY")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            say("CANCELLED")
            await release_line()
            raise
        raise AssertionError("unreachable")

    code = await cold_runner.run_hosted(
        AppConfig(),
        inputs(),
        host_reader=NeverHost(),
        spa_dir=spa_dir,
        bind_host="127.0.0.1",
        bind_port=0,
        run=run,
        config_factory=config_factory,
        store_factory=BlockingStore if mode == "pre_sigterm" else RoastStore,
        signals=AnnouncingSignals if mode == "signals" else cold_runner._LoopSignals,  # pyright: ignore[reportPrivateUsage]
    )
    task = asyncio.current_task()
    if mode == "signals" and task is not None:
        say(f"CANCELLING {task.cancelling()}")
    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1], Path(sys.argv[2]))))
