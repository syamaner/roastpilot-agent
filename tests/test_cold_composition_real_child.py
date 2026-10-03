"""T-U1-RC: cold composition against the real ``coffee-roaster-mcp`` 0.2.2 mock child.

Software integration evidence only.  The composition admits everything, spawns
one real mock-driver child with first-crack and ambient sensing disabled, freezes
nothing (the frozen identity refuses a disabled first-crack mode, the honest scope
here) and stops the owned child confirmed.  Counterfactual ``COFFEE_*``, ``SHELL``,
``TERM`` and ``PATH`` changes made after the environment snapshot must not reach
the child: the realised environment is recorded at the SDK's process creation.  No hardware,
microphone, model download, provider or network is touched.

PYTEST_DONT_REWRITE: assertion introspection is disabled so a failure never
formats a whole run result, a real inherited environment or a secret value.
"""

# pyright: reportPrivateUsage=false

import asyncio
import importlib.metadata
import logging
import os
import shutil
import socket
import sys
import typing
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

import mcp.client.stdio as mcp_stdio
import pytest
from mcp import StdioServerParameters
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from roastpilot_agent import cold_composition
from roastpilot_agent.advisor import AdvisorDescriptor, PydanticAIAdvisor, RoastAdvisor
from roastpilot_agent.cold_characterisation import identity as identity_module
from roastpilot_agent.cold_characterisation.advisory_sampler import ColdAdvisorySpec
from roastpilot_agent.cold_characterisation.host import HostBoundSample
from roastpilot_agent.cold_characterisation.identity import AgentBuildProvenance, ColdArtefactKind
from roastpilot_agent.cold_characterisation.mcp import ColdCharacterisationMCPClient
from roastpilot_agent.cold_characterisation.two_phase import (
    ColdChildOwnership,
    ColdRunStartRefusal,
    ColdTwoPhaseAdvisoryPath,
    ColdTwoPhaseOutcome,
    ColdTwoPhaseProviderCheck,
    ColdTwoPhaseResult,
)
from roastpilot_agent.config import (
    DEFAULT_MCP_COMMAND,
    AppConfig,
    MCPConfig,
    MCPDeviceConfig,
)
from roastpilot_agent.mcp_client import RuntimeConfigSnapshot, ServerInfo, resolve_mcp_command
from roastpilot_agent.mcp_yaml import render_mcp_yaml
from roastpilot_agent.models import RoastPhase

pytestmark = [
    pytest.mark.slow,
    pytest.mark.serial(reason="drives a real MCP child and verifies its teardown"),
]

_CANARY = "sk-or-v1-canary-7f3c2a9e5b1d4c6f8a0e2b4d6f8a1c3e"
_SOURCE_YAML = """roaster:
  driver: mock
first_crack:
  mode: disabled
  revision: test-revision
  onnx_threads: 2
  min_positive_windows: 3
  confirmation_window_seconds: 30.0
audio:
  sample_rate: 16000
  window_seconds: 10.0
  overlap: 0.3
  hop_seconds: null
session:
  ror_window_seconds: 60
  ror_min_sample_seconds: 10
ambient:
  mode: disabled
"""


class _Clock:
    """Fake engine clock: fixed UTC instant, advancing monotonic, no real sleep."""

    def __init__(self) -> None:
        self._now = 100.0

    def monotonic(self) -> float:
        self._now += 0.001
        return self._now

    def utc_now_iso(self) -> str:
        return "2026-10-03T12:00:00+00:00"

    async def sleep(self, seconds: float) -> None:
        self._now += seconds


class _Host:
    """Fake host: never reached on this path."""

    def check_start_bounds(self, evidence_root: Path) -> None:
        raise AssertionError("host start bounds must not be reached")

    def sample(self, evidence_root: Path) -> HostBoundSample:
        raise AssertionError("host sample must not be reached")


def _pid_is_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _project(result: object) -> tuple[object, ...]:
    """Project the closed fields only; never format the whole result."""
    is_result = type(result) is ColdTwoPhaseResult
    assert is_result
    assert isinstance(result, ColdTwoPhaseResult)
    return (
        result.outcome,
        result.start_refusal,
        result.termination_reason,
        result.child_ownership,
        result.manifest_sha256,
        result.conformance,
        result.advisory_path,
        result.provider_check,
    )


@pytest.mark.asyncio
async def test_t_u1_rc_real_child_closed_environment_and_committed_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T-U1-RC: one owned mock child, frozen closed env, committed bytes, confirmed stop."""
    assert importlib.metadata.version("coffee-roaster-mcp") == "0.2.2"
    assert Path(resolve_mcp_command(DEFAULT_MCP_COMMAND)).is_file()
    monkeypatch.chdir(tmp_path)
    for name in list(os.environ):
        upper = name.upper()
        if upper.startswith("COFFEE_") or upper in {"LANG", "LC_ALL", "LC_CTYPE", "TZ"}:
            monkeypatch.delenv(name)
    # Controlled synthetic execution values only (T-E5); no host value is relied on.
    home = tmp_path / "home"
    home.mkdir()
    child_tmp = tmp_path / "tmp"
    child_tmp.mkdir()
    synthetic_path = os.pathsep.join([str(Path(sys.executable).parent), "/usr/bin", "/bin"])
    monkeypatch.setenv("PATH", synthetic_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USER", "cold-test")
    monkeypatch.setenv("LOGNAME", "cold-test")
    monkeypatch.setenv("TMPDIR", str(child_tmp))
    monkeypatch.setenv("SHELL", "/bin/synthetic-shell")
    monkeypatch.setenv("TERM", "synthetic-term")
    monkeypatch.setenv("OPENROUTER_API_KEY", _CANARY)
    caplog.set_level(logging.DEBUG)

    source = tmp_path / "operator" / "coffee-roaster-mcp.yaml"
    source.parent.mkdir()
    source.write_text(_SOURCE_YAML, encoding="utf-8")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    boot_id = tmp_path / "boot_id"
    boot_id.write_text("01234567-89ab-cdef-0123-456789abcdef\n", encoding="ascii")
    base = MCPDeviceConfig(
        mcp_yaml_source_path=source,
        roaster_driver="mock",
        fc_mode="disabled",
        ambient_mode="disabled",
    )
    inputs = cold_composition.ColdCompositionInputs(
        spec=ColdAdvisorySpec(
            profile_name="cold-u1",
            target_drop_temp_c=205.0,
            charge_guidance_min_c=None,
            charge_guidance_max_c=None,
        ),
        build_provenance=AgentBuildProvenance(
            source_revision="0" * 40,
            source_tree_dirty=False,
            artefact_kind=ColdArtefactKind.EDITABLE_SOURCE,
            artefact_sha256=None,
        ),
        host_facts=cold_composition.ColdHostFacts(
            coffee_roaster_mcp_version="0.2.2",
            python_version="3.11.0",
            platform="test-platform",
            machine="test-machine",
            operating_system="test-os",
            kernel="test-kernel",
            pi_model="test-pi",
            pi_revision="test-rev",
            boot_id_path=boot_id,
        ),
        device_config=base,
        pi_evidence_root=str(evidence),
        laptop_evidence_root="/laptop/evidence",
        protected_roots=(),
        audio_device_identity="test-audio",
        serial_port_path="/dev/null-test",
        stimulus_block="none",
        operator_host_notes="none",
        operator_psu_notes="none",
        operator_cooling_notes="none",
    )
    config = AppConfig(mcp=MCPConfig(startup_timeout_seconds=15.0, call_timeout_seconds=5.0))

    recommendations: list[int] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        recommendations.append(1)
        return ModelResponse(parts=[TextPart("unused")])

    descriptor_calls: list[RoastPhase] = []

    class _CountingAdvisor(PydanticAIAdvisor):
        def descriptor_for(self, phase: RoastPhase) -> AdvisorDescriptor:
            descriptor_calls.append(phase)
            return super().descriptor_for(phase)

    builder_calls: list[int] = []

    def builder(app: AppConfig) -> RoastAdvisor | None:
        builder_calls.append(1)
        # Counterfactual, strictly after the environment snapshot: must not reach the child.
        monkeypatch.setenv("COFFEE_FIRST_CRACK_MODE", "manual")
        monkeypatch.setenv("COFFEE_FIRST_CRACK_REVISION", "evil-rev")
        monkeypatch.setenv("SHELL", "/bin/late-shell")
        monkeypatch.setenv("TERM", "late-term")
        monkeypatch.setenv("PATH", "/late/bin")
        return _CountingAdvisor(app.advisor, model=FunctionModel(respond))

    client_calls: list[str] = []
    runtimes: list[RuntimeConfigSnapshot] = []
    servers: list[ServerInfo] = []

    class _SpyClient(ColdCharacterisationMCPClient):
        async def get_server_info(self) -> ServerInfo:
            client_calls.append("get_server_info")
            server = await super().get_server_info()
            servers.append(server)
            return server

        async def get_runtime_config(self) -> RuntimeConfigSnapshot:
            client_calls.append("get_runtime_config")
            runtime = await super().get_runtime_config()
            runtimes.append(runtime)
            return runtime

    params_seen: list[StdioServerParameters] = []
    active_at_spawn: list[bytes] = []
    pids: list[int] = []
    lifecycle: list[str] = []
    processes: list[cold_composition.ColdMCPServerProcess] = []

    class _SpyProcess(cold_composition.ColdMCPServerProcess):
        def __init__(
            self, config: MCPConfig, *, child_environment: Mapping[str, str], family: str
        ) -> None:
            lifecycle.append("construct")
            super().__init__(config, child_environment=child_environment, family=family)
            processes.append(self)

        def build_server_parameters(self) -> StdioServerParameters:
            params = super().build_server_parameters()
            params_seen.append(params)
            assert params.env is not None
            active_at_spawn.append(Path(params.env["COFFEE_ROASTER_MCP_CONFIG"]).read_bytes())
            return params

        def _register_force_terminate(self, pid: int) -> None:
            pids.append(pid)
            super()._register_force_terminate(pid)

        async def start(self) -> None:
            lifecycle.append("start")
            await super().start()

        async def stop(self) -> None:
            lifecycle.append("stop")
            await super().stop()

    factory_calls: list[int] = []
    original_factory_call = cold_composition._SingleUseAdvisorFactory.__call__

    def counting_factory_call(self: cold_composition._SingleUseAdvisorFactory) -> object:
        factory_calls.append(1)
        return original_factory_call(self)

    freeze_calls: list[int] = []
    original_freeze_identity = identity_module.freeze_identity

    def counting_freeze_identity(**kwargs: typing.Any) -> typing.Any:
        freeze_calls.append(1)
        return original_freeze_identity(**kwargs)

    socket_attempts: list[object] = []

    def refuse_connect(self: socket.socket, address: object) -> None:
        socket_attempts.append(address)
        raise AssertionError("no socket connection is permitted")

    monkeypatch.setattr(cold_composition, "ColdCharacterisationMCPClient", _SpyClient)
    monkeypatch.setattr(cold_composition, "ColdMCPServerProcess", _SpyProcess)
    monkeypatch.setattr(
        cold_composition._SingleUseAdvisorFactory, "__call__", counting_factory_call
    )
    monkeypatch.setattr(socket.socket, "connect", refuse_connect)
    monkeypatch.setattr(cold_composition, "freeze_identity", counting_freeze_identity)

    sdk = mcp_stdio
    true_original = typing.cast(
        Callable[..., Awaitable[object]],
        sdk._create_platform_compatible_process,
    )
    sdk_envs: list[dict[str, str]] = []
    sdk_pids: list[object] = []

    async def delegating(*args: object, **kwargs: object) -> object:
        env = kwargs.get("env")
        assert isinstance(env, dict)
        sdk_envs.append(dict(typing.cast(dict[str, str], env)))
        created = await true_original(*args, **kwargs)
        sdk_pids.append(getattr(created, "pid", None))
        return created

    resource_files: dict[str, bytes] = {}
    with (
        pytest.MonkeyPatch.context() as sdk_patch,
        cold_composition.ColdCompositionResources() as resources,
    ):
        sdk_patch.setattr(sdk, "_create_platform_compatible_process", delegating)
        directory = resources.directory
        assert directory is not None
        try:
            result = await cold_composition.run_cold_characterisation(
                config,
                inputs,
                resources=resources,
                clock=_Clock(),
                host=_Host(),
                advisor_builder=builder,
                run_suffix=lambda: "u1rc",
            )
        finally:
            for process in processes:
                if process.running:
                    await process.stop()
        assert sdk._create_platform_compatible_process is delegating
        for path in sorted(directory.iterdir()):
            resource_files[path.name] = path.read_bytes()
        # Independent oracle for the installed recording-off bytes.
        oracle_dir = tmp_path / "oracle"
        oracle_dir.mkdir()
        shutil.copyfile(source, oracle_dir / "source.yaml")
        off_cfg = base.model_copy(
            update={"recording_enabled": False, "recording_autocapture": False}
        )
        on_cfg = base.model_copy(update={"recording_enabled": True, "recording_autocapture": True})
        render_mcp_yaml(off_cfg, oracle_dir / "source.yaml", oracle_dir / "off.yaml")
        render_mcp_yaml(on_cfg, oracle_dir / "source.yaml", oracle_dir / "on.yaml")
        active_path = directory / "active.yaml"

    assert _project(result) == (
        ColdTwoPhaseOutcome.REFUSED_BEFORE_EVIDENCE,
        ColdRunStartRefusal.IDENTITY_NOT_FROZEN,
        None,
        ColdChildOwnership.OWNED_STOP_CONFIRMED,
        None,
        None,
        ColdTwoPhaseAdvisoryPath.NOT_APPLICABLE,
        ColdTwoPhaseProviderCheck.NOT_CHECKED,
    )
    assert client_calls == ["get_server_info", "get_runtime_config"]
    assert len(runtimes) == 1
    assert runtimes[0].first_crack_mode == "disabled"
    assert runtimes[0].roaster_driver == "mock"
    # R5: the real 0.2.2 child self-reports the pin and the exact bound active path,
    # so both identity guards pass and the existing refusal comes from one delegation.
    assert len(servers) == 1
    assert servers[0].version == "0.2.2"
    assert runtimes[0].config_source == str(active_path)
    assert freeze_calls == [1]

    assert sdk._create_platform_compatible_process is true_original
    expected_env = {
        "PATH": synthetic_path,
        "HOME": str(home),
        "USER": "cold-test",
        "LOGNAME": "cold-test",
        "TMPDIR": str(child_tmp),
        "SHELL": "",
        "TERM": "",
        "COFFEE_ROASTER_MCP_CONFIG": str(active_path),
    }
    assert len(params_seen) == 1
    env = params_seen[0].env
    assert env is not None
    assert env == expected_env
    # T-E5: the realised environment at SDK process creation equals the frozen mapping.
    assert sdk_envs == [expected_env]
    assert "OPENROUTER_API_KEY" not in env
    assert [name for name in env if name.upper().startswith("COFFEE_")] == [
        "COFFEE_ROASTER_MCP_CONFIG"
    ]
    assert all("evil-rev" not in value and _CANARY not in value for value in env.values())
    assert params_seen[0].args == ["serve"]

    off_bytes = (oracle_dir / "off.yaml").read_bytes()
    on_bytes = (oracle_dir / "on.yaml").read_bytes()
    assert off_bytes != on_bytes
    assert active_at_spawn == [off_bytes]
    assert resource_files["active.yaml"] == off_bytes
    assert resource_files["phase-off.yaml"] == off_bytes
    assert resource_files["phase-on.yaml"] == on_bytes
    assert resource_files["source.yaml"] == source.read_bytes()

    assert lifecycle == ["construct", "start", "stop"]
    assert len(processes) == 1
    assert processes[0].running is False
    assert processes[0].stop_unconfirmed is False
    assert len(pids) == 1
    assert sdk_pids == pids
    for _ in range(50):
        if _pid_is_gone(pids[0]):
            break
        await asyncio.sleep(0.1)
    assert _pid_is_gone(pids[0])

    assert builder_calls == [1]
    assert descriptor_calls == [RoastPhase.PREHEATING]
    assert factory_calls == []
    assert recommendations == []

    assert list(evidence.iterdir()) == []
    assert all(_CANARY.encode() not in data for data in resource_files.values())
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert _CANARY not in messages
    assert "cancel scope" not in messages.lower() and "different task" not in messages.lower()
    assert socket_attempts == []
    projected = " ".join(str(item) for item in _project(result))
    assert _CANARY not in projected
