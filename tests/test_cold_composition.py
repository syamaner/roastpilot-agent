"""Cold-run composition (#954 U1): admission order, closed child environment, YAML bounds.

Hardware-free.  Most tests substitute the composition's own
``ColdMCPServerProcess`` construction seam with a recording fake; the native-spawn
tests use the real cold subclass with a recorder at the existing
``_spawn_stdio_session`` seam so the native factory and process-group hook stay
owned by the base class.  No provider, network, host ``/proc``, serial or audio
is touched.  Results are only ever projected to closed fields, never formatted.

PYTEST_DONT_REWRITE: assertion introspection is disabled so a failure never
formats a whole run result, a real inherited environment or a secret value.
"""

# pyright: reportPrivateUsage=false

import ast
import contextlib
import copy
import enum
import hashlib
import inspect
import json
import logging
import os
import stat
import types
import typing
from collections.abc import AsyncGenerator, Callable, Mapping
from pathlib import Path

import mcp.client.stdio as mcp_stdio
import pydantic
import pytest
from mcp import StdioServerParameters

from roastpilot_agent import cold_composition, mcp_client
from roastpilot_agent.advisor import (
    AdvisorContext,
    AdvisorDescriptor,
    AdvisorUsage,
    RoastAdvisor,
    RoastDecision,
)
from roastpilot_agent.cold_characterisation.advisory_sampler import ColdAdvisorySpec
from roastpilot_agent.cold_characterisation.evidence_lifecycle import ColdRunTerminationReason
from roastpilot_agent.cold_characterisation.evidence_schema import ColdPhaseKind, ColdRunHeader
from roastpilot_agent.cold_characterisation.evidence_store import ColdAdmittedRoot
from roastpilot_agent.cold_characterisation.host import HostBoundSample
from roastpilot_agent.cold_characterisation.identity import (
    AgentBuildProvenance,
    ColdArtefactKind,
)
from roastpilot_agent.cold_characterisation.mcp import ColdCharacterisationMCPClient
from roastpilot_agent.cold_characterisation.two_phase import (
    ColdChildOwnership,
    ColdRunStartRefusal,
    ColdTwoPhaseAdvisoryPath,
    ColdTwoPhaseOutcome,
    ColdTwoPhaseProviderCheck,
    ColdTwoPhaseResult,
)
from roastpilot_agent.cold_composition import (
    ColdCompositionChildError,
    ColdCompositionInputs,
    ColdCompositionRefusal,
    ColdCompositionResources,
    ColdHostFacts,
    ColdIdentitySource,
    ColdMCPChild,
    ColdMCPServerProcess,
)
from roastpilot_agent.config import DEFAULT_MCP_COMMAND, AppConfig, MCPConfig, MCPDeviceConfig
from roastpilot_agent.live import build_advisor
from roastpilot_agent.mcp_client import MCPServerProcess, resolve_mcp_command
from roastpilot_agent.mcp_yaml import render_mcp_yaml
from roastpilot_agent.models import AdvisorHealth, RoastPhase
from roastpilot_agent.safety import SafetyPolicy

R = ColdCompositionRefusal
OFF = ColdPhaseKind.RECORDING_OFF
ON = ColdPhaseKind.RECORDING_ON
CANARY = "sk-or-v1-canary-0d9e8f7a6b5c4d3e2f1a0b9c8d7e6f5a"
FIXTURES = Path(__file__).parent / "fixtures" / "mcp-tool-results"
TEMPLATE_YAML = """roaster:
  driver: mock
first_crack:
  mode: audio
  revision: test-revision
  precision: int8
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
extra:
  - a
  - b
"""
_CLEAN_DESCRIPTOR = AdvisorDescriptor(provider="alt", model="alt-model", prompt_version="v-alt")


def _project(result: object) -> tuple[object, ...]:
    """Project a result to its closed fields only; never format the whole result."""
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


class _Clock:
    """Fake engine clock with a settable UTC instant."""

    def __init__(self, instant: str = "2026-10-03T12:00:00+00:00") -> None:
        self.instant = instant
        self._now = 10.0

    def monotonic(self) -> float:
        self._now += 0.001
        return self._now

    def utc_now_iso(self) -> str:
        return self.instant

    async def sleep(self, seconds: float) -> None:
        self._now += seconds


class _RaisingClock(_Clock):
    def utc_now_iso(self) -> str:
        raise RuntimeError(CANARY)


class _Host:
    def check_start_bounds(self, evidence_root: Path) -> None:
        raise AssertionError("not reached")

    def sample(self, evidence_root: Path) -> HostBoundSample:
        raise AssertionError("not reached")


class _AltAdvisor(RoastAdvisor):
    """A non-PydanticAI advisor implementing the sampler port."""

    def __init__(self) -> None:
        self.last_usage: AdvisorUsage | None = None
        self.descriptor_calls: list[RoastPhase] = []

    @property
    def descriptor(self) -> AdvisorDescriptor:
        return _CLEAN_DESCRIPTOR

    def descriptor_for(self, phase: RoastPhase) -> AdvisorDescriptor:
        self.descriptor_calls.append(phase)
        return self.descriptor

    async def get_recommendation(self, context: AdvisorContext) -> RoastDecision:
        raise AssertionError("no recommendation is requested in these tests")

    async def healthcheck(self) -> AdvisorHealth:
        raise AssertionError("no healthcheck")


class _Builder:
    """Counting advisor builder."""

    def __init__(self, advisor: RoastAdvisor | None = None) -> None:
        self.advisor: RoastAdvisor | None = advisor if advisor is not None else _AltAdvisor()
        self.calls = 0
        self.before: Callable[[], None] | None = None

    def __call__(self, config: AppConfig) -> RoastAdvisor | None:
        self.calls += 1
        if self.before is not None:
            self.before()
        return self.advisor


def _payloads(mode: str = "audio", precision: str = "int8") -> dict[str, object]:
    server: dict[str, object] = json.loads(
        (FIXTURES / "get_server_info.json").read_text(encoding="utf-8")
    )
    runtime: dict[str, object] = json.loads(
        (FIXTURES / "get_runtime_config.json").read_text(encoding="utf-8")
    )
    server = copy.deepcopy(server)
    runtime = copy.deepcopy(runtime)
    server["first_crack_mode"] = mode
    runtime["first_crack_mode"] = mode
    runtime["model_precision"] = precision
    return {"get_server_info": server, "get_runtime_config": runtime}


class _Spawns:
    """Recording substitute for the composition's process construction seam."""

    def __init__(self) -> None:
        self.processes: list[_FakeProcess] = []
        self.payloads = _payloads()
        self.calls: list[str] = []


class _FakeProcess:
    """Recording fake process; never spawns."""

    spawns: typing.ClassVar[_Spawns]

    def __init__(
        self, config: MCPConfig, *, child_environment: Mapping[str, str], family: str
    ) -> None:
        self.config = config
        self.family = family
        self.child_environment = child_environment
        self.running = False
        self.stop_unconfirmed = False
        self.started = 0
        self.stopped = 0
        type(self).spawns.processes.append(self)

    async def start(self) -> None:
        self.started += 1
        self.running = True

    async def stop(self) -> None:
        self.stopped += 1
        self.running = False

    async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
        type(self).spawns.calls.append(name)
        return copy.deepcopy(type(self).spawns.payloads[name])


class _RuntimeStub:
    """Records the runtime call and returns one fixed, strictly validated result."""

    def __init__(self) -> None:
        self.calls: list[dict[str, typing.Any]] = []
        self.result = ColdTwoPhaseResult(
            outcome=ColdTwoPhaseOutcome.EVIDENCE_NOT_SEALED,
            start_refusal=None,
            termination_reason=ColdRunTerminationReason.UNEXPECTED_FAILURE,
            child_ownership=ColdChildOwnership.OWNED_STOP_UNCONFIRMED,
            manifest_sha256=None,
            conformance=None,
            advisory_path=ColdTwoPhaseAdvisoryPath.FAILED_RUN_TERMINAL,
            provider_check=ColdTwoPhaseProviderCheck.PENDING_AT_CHECK,
        )

    async def __call__(self, **kwargs: typing.Any) -> ColdTwoPhaseResult:
        self.calls.append(kwargs)
        return self.result


SYNTHETIC_HOME = "/nonexistent-rp954/home"


def _synthetic_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Controlled synthetic execution values only; no host value is relied on."""
    for name in list(os.environ):
        upper = name.upper()
        if upper.startswith("COFFEE_") or upper in {"LANG", "LC_ALL", "LC_CTYPE", "TZ"}:
            monkeypatch.delenv(name)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", SYNTHETIC_HOME)
    monkeypatch.setenv("USER", "cold-test")
    monkeypatch.setenv("LOGNAME", "cold-test")
    monkeypatch.setenv("TMPDIR", "/nonexistent-rp954/tmp")
    monkeypatch.setenv("SHELL", "/bin/synthetic-shell")
    monkeypatch.setenv("TERM", "synthetic-term")
    monkeypatch.setenv("OPENROUTER_API_KEY", CANARY)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _synthetic_environment(monkeypatch)


@pytest.fixture
def spawns(monkeypatch: pytest.MonkeyPatch) -> _Spawns:
    recorder = _Spawns()
    fake = type("_BoundFakeProcess", (_FakeProcess,), {"spawns": recorder})
    monkeypatch.setattr(cold_composition, "ColdMCPServerProcess", fake)
    return recorder


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> _RuntimeStub:
    stub = _RuntimeStub()
    monkeypatch.setattr(cold_composition, "run_two_phase_characterisation", stub)
    return stub


@pytest.fixture
def resources() -> typing.Iterator[ColdCompositionResources]:
    with ColdCompositionResources() as entered:
        yield entered


def _source(tmp_path: Path, text: str | bytes = TEMPLATE_YAML) -> Path:
    path = tmp_path / "operator" / "coffee-roaster-mcp.yaml"
    path.parent.mkdir(exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def _inputs(
    tmp_path: Path, source: Path | None = None, **device: typing.Any
) -> ColdCompositionInputs:
    evidence = tmp_path / "evidence"
    evidence.mkdir(exist_ok=True)
    boot_id = tmp_path / "boot_id"
    boot_id.write_text("01234567-89ab-cdef-0123-456789abcdef\n", encoding="ascii")
    fields: dict[str, typing.Any] = {
        "mcp_yaml_source_path": source if source is not None else _source(tmp_path),
        "roaster_driver": "mock",
        "recording_devices": ["test-mic"],
    }
    fields.update(device)
    return ColdCompositionInputs(
        spec=ColdAdvisorySpec(
            profile_name="cold-u1",
            target_drop_temp_c=205.0,
            charge_guidance_min_c=None,
            charge_guidance_max_c=None,
        ),
        build_provenance=AgentBuildProvenance(
            source_revision="a" * 40,
            source_tree_dirty=False,
            artefact_kind=ColdArtefactKind.EDITABLE_SOURCE,
            artefact_sha256=None,
        ),
        host_facts=ColdHostFacts(
            coffee_roaster_mcp_version="0.2.2",
            python_version="3.11.9",
            platform="test-platform",
            machine="aarch64",
            operating_system="test-os",
            kernel="test-kernel",
            pi_model="test-pi",
            pi_revision="test-rev",
            boot_id_path=boot_id,
        ),
        device_config=MCPDeviceConfig(**fields),
        pi_evidence_root=str(evidence),
        laptop_evidence_root="/laptop/evidence",
        protected_roots=(),
        audio_device_identity="test-audio",
        serial_port_path="/dev/test-serial",
        stimulus_block="none",
        operator_host_notes="none",
        operator_psu_notes="none",
        operator_cooling_notes="none",
    )


async def _run(
    tmp_path: Path,
    resources: ColdCompositionResources,
    *,
    config: AppConfig | None = None,
    inputs: ColdCompositionInputs | None = None,
    builder: Callable[[AppConfig], RoastAdvisor | None] | None = None,
    clock: _Clock | None = None,
    suffix: Callable[[], str] = lambda: "abc",
) -> ColdTwoPhaseResult | ColdCompositionRefusal:
    return await cold_composition.run_cold_characterisation(
        config if config is not None else AppConfig(),
        inputs if inputs is not None else _inputs(tmp_path),
        resources=resources,
        clock=clock if clock is not None else _Clock(),
        host=_Host(),
        advisor_builder=builder if builder is not None else _Builder(),
        run_suffix=suffix,
    )


# --------------------------------------------------------------- T-C1 single instances


@pytest.mark.asyncio
async def test_t_c1_valid_inputs_call_the_runtime_once_with_single_instances(
    tmp_path: Path,
    resources: ColdCompositionResources,
    spawns: _Spawns,
    runtime: _RuntimeStub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-C1: one process, one client, one child, one identity source, one runtime call."""
    builder = _Builder()
    # Set after the snapshot: an ambient COFFEE_* name added here must not refuse the run.
    builder.before = lambda: monkeypatch.setenv("COFFEE_LATE", "1")
    clock = _Clock()
    config = AppConfig()
    result = await _run(tmp_path, resources, config=config, builder=builder, clock=clock)
    same = result is runtime.result
    assert same
    assert builder.calls == 1
    assert len(spawns.processes) == 1 and len(runtime.calls) == 1
    process = spawns.processes[0]
    kwargs = runtime.calls[0]
    assert set(kwargs) == {
        "root",
        "mcp",
        "child",
        "identities",
        "host",
        "clock",
        "advisor_factory",
        "spec",
        "configured_call_bound_seconds",
        "configured_dwell_seconds",
        "evaluator",
    }
    client, child, identities = kwargs["mcp"], kwargs["child"], kwargs["identities"]
    assert type(client) is ColdCharacterisationMCPClient
    assert client._call_tool == process.call_tool
    assert type(child) is ColdMCPChild and child._process is process
    assert type(identities) is ColdIdentitySource and identities._client is client
    assert kwargs["clock"] is clock
    assert type(kwargs["root"]) is ColdAdmittedRoot
    assert kwargs["root"].path == str(tmp_path / "evidence")
    assert identities._root is kwargs["root"]
    assert kwargs["configured_call_bound_seconds"] == 5.0
    assert kwargs["configured_dwell_seconds"] == 5.0
    assert type(kwargs["configured_call_bound_seconds"]) is float
    assert type(kwargs["evaluator"]) is SafetyPolicy
    assert kwargs["evaluator"].limits == config.safety
    advisor = builder.advisor
    factory = kwargs["advisor_factory"]
    assert factory() is advisor
    with pytest.raises(ColdCompositionChildError):
        factory()
    assert process.config is config.mcp
    assert dict(process.child_environment)["COFFEE_ROASTER_MCP_CONFIG"] == str(
        resources.directory and resources.directory / "active.yaml"
    )
    assert process.started == 0 and spawns.calls == []


@pytest.mark.asyncio
async def test_t_c1_timing_comes_from_config(
    tmp_path: Path,
    resources: ColdCompositionResources,
    spawns: _Spawns,
    runtime: _RuntimeStub,
) -> None:
    """T-C1: call bound and dwell are the configured controller values, as floats."""
    config = AppConfig.model_validate(
        {"controller": {"advisory_timeout_seconds": 7, "post_fc_min_consult_interval_seconds": 9}}
    )
    await _run(tmp_path, resources, config=config)
    assert runtime.calls[0]["configured_call_bound_seconds"] == 7.0
    assert runtime.calls[0]["configured_dwell_seconds"] == 9.0
    assert type(runtime.calls[0]["configured_dwell_seconds"]) is float


@pytest.mark.asyncio
async def test_t_c1_unentered_resources_refused_first(
    tmp_path: Path, spawns: _Spawns, runtime: _RuntimeStub
) -> None:
    """T-C1 RESOURCES_NOT_ADMITTED: before the environment, builder and construction."""
    builder = _Builder()
    unentered = ColdCompositionResources()
    assert unentered.directory is None
    result = await _run(
        tmp_path, unentered, config=AppConfig(mcp=MCPConfig(env={"X": "1"})), builder=builder
    )
    assert result is R.RESOURCES_NOT_ADMITTED
    assert builder.calls == 0 and spawns.processes == [] and runtime.calls == []


class _StrSubclass(str):
    pass


_POSIX_SDK = ["HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER"]


def _env_case(case: str, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    """Apply one T-C4/T-E2 preflight case; return the config to run with."""
    config = AppConfig()
    sdk = mcp_stdio
    if case == "config_env":
        return AppConfig(mcp=MCPConfig(env={"X": "1"}))
    if case.startswith("credential:"):
        advisor = config.advisor.model_copy(update={"api_key_env": case.split(":", 1)[1]})
        return config.model_copy(update={"advisor": advisor})
    lists: dict[str, object] = {
        "sdk_evil": [*_POSIX_SDK, "EVIL"],
        "sdk_str_subclass": [_StrSubclass("HOME"), "PATH"],
        "sdk_bytes": [b"HOME", "PATH"],
        "sdk_set": set(_POSIX_SDK),
        "sdk_bare_str": "HOME",
        "sdk_duplicates": [*_POSIX_SDK, "PATH"],
    }
    if case in lists:
        monkeypatch.setattr(sdk, "DEFAULT_INHERITED_ENV_VARS", lists[case])
    elif case == "ambient_lowercase":
        monkeypatch.setenv("coffee_x", "1")
    elif case == "ambient_upper":
        monkeypatch.setenv("COFFEE_FIRST_CRACK_MODE", "manual")
    elif case == "path_missing":
        monkeypatch.delenv("PATH")
    elif case == "user_empty":
        monkeypatch.setenv("USER", "")
    elif case == "home_function":
        monkeypatch.setenv("HOME", "() { :; }")
    elif case == "lang_function":
        monkeypatch.setenv("LANG", "()x")
    else:
        assert case == "reader_raises"

        def boom(api_key_env: str, family: str) -> None:
            raise RuntimeError(CANARY)

        monkeypatch.setattr(cold_composition, "_read_process_environment", boom)
    return config


ENV_REFUSAL_CASES = [
    "config_env",
    "ambient_lowercase",
    "ambient_upper",
    "reader_raises",
    "path_missing",
    "user_empty",
    "home_function",
    "lang_function",
    "sdk_evil",
    "sdk_str_subclass",
    "sdk_bytes",
    "sdk_set",
    "sdk_bare_str",
    "sdk_duplicates",
    *(
        f"credential:{name}"
        for name in (
            "PATH",
            "path",
            "HOME",
            "SHELL",
            "TERM",
            "LANG",
            "processor_architecture",
            "COFFEE_ROASTER_MCP_CONFIG",
            "coffee_x",
        )
    ),
]


@pytest.mark.parametrize("case", ENV_REFUSAL_CASES)
@pytest.mark.asyncio
async def test_t_e2_environment_preflight_refusals(
    case: str,
    tmp_path: Path,
    resources: ColdCompositionResources,
    spawns: _Spawns,
    runtime: _RuntimeStub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-C4/T-E2: each case => MCP_ENV_NOT_ADMITTED with 0 builder calls and 0 constructions."""
    config = _env_case(case, monkeypatch)
    builder = _Builder()
    result = await _run(tmp_path, resources, config=config, builder=builder)
    assert result is R.MCP_ENV_NOT_ADMITTED
    assert builder.calls == 0 and spawns.processes == [] and runtime.calls == []


@pytest.mark.parametrize(
    "case", ["default", "optional_empty", "sdk_strict_subset", "sdk_tuple", "lang_present"]
)
@pytest.mark.asyncio
async def test_t_e2_positive_controls_reach_the_builder(
    case: str,
    tmp_path: Path,
    resources: ColdCompositionResources,
    spawns: _Spawns,
    runtime: _RuntimeStub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-E2 positive controls: admitted values proceed to the builder and one construction."""
    sdk = mcp_stdio
    if case == "optional_empty":
        monkeypatch.setenv("LANG", "")
    elif case == "sdk_strict_subset":
        monkeypatch.setattr(sdk, "DEFAULT_INHERITED_ENV_VARS", ["PATH", "HOME"])
    elif case == "sdk_tuple":
        monkeypatch.setattr(sdk, "DEFAULT_INHERITED_ENV_VARS", tuple(_POSIX_SDK))
    elif case == "lang_present":
        monkeypatch.setenv("LANG", "C.UTF-8")
    builder = _Builder()
    result = await _run(tmp_path, resources, builder=builder)
    assert result is runtime.result
    assert builder.calls == 1 and len(spawns.processes) == 1
    directory = resources.directory
    assert directory is not None
    expected = {
        "PATH": "/usr/bin:/bin",
        "HOME": SYNTHETIC_HOME,
        "USER": "cold-test",
        "LOGNAME": "cold-test",
        "TMPDIR": "/nonexistent-rp954/tmp",
        "SHELL": "",
        "TERM": "",
        "COFFEE_ROASTER_MCP_CONFIG": str(directory / "active.yaml"),
    }
    if case == "optional_empty":
        expected["LANG"] = ""
    if case == "lang_present":
        expected["LANG"] = "C.UTF-8"
    process = spawns.processes[0]
    assert dict(process.child_environment) == expected
    assert process.family == os.name


def test_t_e2_unknown_family_is_refused_by_parameter() -> None:
    """T-C4/T-E2: an unknown family is refused through the parameter (``os.name`` untouched)."""
    assert cold_composition._admit_environment(AppConfig(), "java") is R.MCP_ENV_NOT_ADMITTED
    assert cold_composition._read_process_environment("OPENROUTER_API_KEY", "java") is None
    assert cold_composition._snapshot_environment({"PATH": "/x"}, "K", "java") is None
    assert cold_composition._admit_sdk_default_names(_POSIX_SDK, "java") is False
    assert cold_composition._admit_environment(AppConfig(), os.name) is not None


@pytest.mark.asyncio
async def test_t_e2_constructor_refusal_after_preflight_is_closed(
    tmp_path: Path,
    resources: ColdCompositionResources,
    runtime: _RuntimeStub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 9: a constructor ``ValueError`` is MCP_ENV_NOT_ADMITTED; the runtime is not called."""
    real_admit = cold_composition._admit_child_environment
    calls: list[int] = []

    def refuse(config: MCPConfig, environment: object, family: str) -> bool:
        calls.append(1)
        assert real_admit(config, environment, family) is True
        return False

    monkeypatch.setattr(cold_composition, "_admit_child_environment", refuse)
    builder = _Builder()
    result = await _run(tmp_path, resources, builder=builder)
    assert result is R.MCP_ENV_NOT_ADMITTED
    assert calls == [1] and builder.calls == 1 and runtime.calls == []


@pytest.mark.parametrize("builder_kind", ["real_build_advisor", "builder_succeeds"])
@pytest.mark.asyncio
async def test_t_c1_credential_absent_is_independent_of_the_builder(
    builder_kind: str,
    tmp_path: Path,
    resources: ColdCompositionResources,
    spawns: _Spawns,
    runtime: _RuntimeStub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-C1/C3: an absent or empty key refuses even when a builder would succeed."""
    monkeypatch.delenv("OPENROUTER_API_KEY")
    calls: list[int] = []

    def real(config: AppConfig) -> RoastAdvisor | None:
        calls.append(1)
        return build_advisor(config)

    succeeding = _Builder()
    result = await _run(
        tmp_path, resources, builder=real if builder_kind == "real_build_advisor" else succeeding
    )
    assert result is R.CREDENTIAL_ABSENT
    assert calls == [] and succeeding.calls == 0
    assert spawns.processes == [] and runtime.calls == []
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    assert await _run(tmp_path, resources, builder=succeeding) is R.CREDENTIAL_ABSENT


@pytest.mark.asyncio
async def test_t_c1_real_build_advisor_returning_none_is_unavailable(
    tmp_path: Path,
    resources: ColdCompositionResources,
    spawns: _Spawns,
    runtime: _RuntimeStub,
) -> None:
    """T-C1: a builder returning ``None`` (missing key) => ADVISOR_UNAVAILABLE, 0 constructions."""
    builder = _Builder()
    builder.advisor = None
    assert await _run(tmp_path, resources, builder=builder) is R.ADVISOR_UNAVAILABLE
    assert builder.calls == 1 and spawns.processes == [] and runtime.calls == []


def test_t_c1_default_builder_is_the_real_build_advisor() -> None:
    """U1-AC2: the production default is the real builder."""
    signature = inspect.signature(cold_composition.run_cold_characterisation)
    assert signature.parameters["advisor_builder"].default is build_advisor
    assert signature.parameters["run_suffix"].default is cold_composition.random_run_suffix
    suffix = cold_composition.random_run_suffix()
    assert len(suffix) == 16 and set(suffix) <= set("0123456789abcdef")


# ------------------------------------------------------------------ T-C2 advisor port


class _RaisingDescriptor(_AltAdvisor):
    def descriptor_for(self, phase: RoastPhase) -> AdvisorDescriptor:
        raise RuntimeError(CANARY)


class _DictDescriptor(_AltAdvisor):
    def descriptor_for(self, phase: RoastPhase) -> AdvisorDescriptor:
        return typing.cast(
            AdvisorDescriptor, {"provider": "p", "model": "m", "prompt_version": "v"}
        )


class _EmptyPrompt(_AltAdvisor):
    def descriptor_for(self, phase: RoastPhase) -> AdvisorDescriptor:
        return AdvisorDescriptor(provider="p", model="m", prompt_version="")


class _SubclassDescriptor(AdvisorDescriptor):
    pass


class _ForgedDescriptor(_AltAdvisor):
    def descriptor_for(self, phase: RoastPhase) -> AdvisorDescriptor:
        return _SubclassDescriptor(provider="p", model="m", prompt_version="v")


class _NoUsage(RoastAdvisor):
    @property
    def descriptor(self) -> AdvisorDescriptor:
        return _CLEAN_DESCRIPTOR

    async def get_recommendation(self, context: AdvisorContext) -> RoastDecision:
        raise AssertionError

    async def healthcheck(self) -> AdvisorHealth:
        raise AssertionError


class _RaisingUsage(_NoUsage):
    @property
    def last_usage(self) -> AdvisorUsage | None:
        raise RuntimeError(CANARY)


def _with(advisor: _AltAdvisor, **attributes: object) -> _AltAdvisor:
    for name, value in attributes.items():
        object.__setattr__(advisor, name, value)
    return advisor


@pytest.mark.parametrize(
    "make",
    [
        _RaisingDescriptor,
        _DictDescriptor,
        _EmptyPrompt,
        _ForgedDescriptor,
        _RaisingUsage,
        _NoUsage,
        lambda: _with(_AltAdvisor(), last_usage="x"),
        lambda: _with(_AltAdvisor(), get_recommendation=5),
    ],
    ids=[
        "descriptor_raises",
        "dict_descriptor",
        "empty_prompt_version",
        "descriptor_subclass",
        "last_usage_raises",
        "last_usage_missing",
        "last_usage_str",
        "recommendation_not_callable",
    ],
)
@pytest.mark.asyncio
async def test_t_c2_advisor_port_admission_refusals(
    make: Callable[[], RoastAdvisor],
    tmp_path: Path,
    resources: ColdCompositionResources,
    spawns: _Spawns,
    runtime: _RuntimeStub,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T-C2: behavioural port admission refuses each forged member; nothing constructed."""
    caplog.set_level(logging.DEBUG)
    builder = _Builder(make())
    assert await _run(tmp_path, resources, builder=builder) is R.ADVISOR_NOT_ADMITTED
    assert builder.calls == 1 and spawns.processes == [] and runtime.calls == []
    assert CANARY not in caplog.text


@pytest.mark.asyncio
async def test_t_c2_last_usage_reading_is_admitted(
    tmp_path: Path,
    resources: ColdCompositionResources,
    spawns: _Spawns,
    runtime: _RuntimeStub,
) -> None:
    """T-C2 positive control: an ``AdvisorUsage`` reading is admitted."""
    advisor = _with(
        _AltAdvisor(),
        last_usage=AdvisorUsage(input_tokens=1, output_tokens=1, total_tokens=2),
    )
    result = await _run(tmp_path, resources, builder=_Builder(advisor))
    assert result is runtime.result


@pytest.mark.asyncio
async def test_t_u1_alt_non_pydantic_advisor_admitted_and_descriptor_frozen(
    tmp_path: Path,
    resources: ColdCompositionResources,
    spawns: _Spawns,
    runtime: _RuntimeStub,
) -> None:
    """T-U1-ALT: a non-PydanticAI advisor is admitted; the identity carries its descriptor."""
    advisor = _AltAdvisor()
    result = await _run(tmp_path, resources, builder=_Builder(advisor))
    assert result is runtime.result
    assert advisor.descriptor_calls == [RoastPhase.PREHEATING]
    identity = await runtime.calls[0]["identities"].freeze(OFF)
    assert (identity.advisor_provider, identity.advisor_model, identity.advisor_prompt_version) == (
        "alt",
        "alt-model",
        "v-alt",
    )


# -------------------------------------------------------------- T-C3 identity source


@pytest.mark.asyncio
async def test_t_c3_identity_freezes_from_same_client_reads_and_inputs(
    tmp_path: Path,
    resources: ColdCompositionResources,
    spawns: _Spawns,
    runtime: _RuntimeStub,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T-C3: same-client reads, explicit inputs, credential name and presence only."""
    caplog.set_level(logging.DEBUG)
    inputs = _inputs(tmp_path)
    await _run(tmp_path, resources, inputs=inputs)
    identities = runtime.calls[0]["identities"]
    off = await identities.freeze(OFF)
    on = await identities.freeze(ON)
    assert spawns.calls == ["get_server_info", "get_runtime_config"] * 2
    directory = resources.directory
    assert directory is not None
    for identity, phase, name in ((off, OFF, "phase-off.yaml"), (on, ON, "phase-on.yaml")):
        data = (directory / name).read_bytes()
        assert identity.run_id == "20261003T120000Z-abc"
        assert identity.started_at_utc == "2026-10-03T12:00:00+00:00"
        assert identity.credential_env_var_name == "OPENROUTER_API_KEY"
        assert identity.credential_present is True
        assert identity.controller_tick_seconds == 1.0
        assert identity.pi_evidence_root == inputs.pi_evidence_root
        assert identity.laptop_evidence_root == "/laptop/evidence"
        assert identity.build_provenance == inputs.build_provenance
        assert identity.effective_mcp_profile.source_sha256 == hashlib.sha256(data).hexdigest()
        assert identity.effective_mcp_profile.source_byte_length == len(data)
        recording = phase is ON
        assert identity.device_config.recording_enabled is recording
        assert identity.device_config.recording_autocapture is recording
        assert identity.runtime_config.first_crack_mode == "audio"
        assert identity.server_info.version == "0.2.2"
        assert identity.machine == "aarch64" and identity.pi_model == "test-pi"
        assert identity.boot_id == "01234567-89ab-cdef-0123-456789abcdef"
        dumped = identity.model_dump_json()
        assert CANARY not in dumped
    for path in directory.iterdir():
        assert CANARY.encode() not in path.read_bytes()
    assert CANARY not in caplog.text


@pytest.mark.asyncio
async def test_t_c3_identity_exceptions_propagate_unchanged(
    tmp_path: Path,
    resources: ColdCompositionResources,
    spawns: _Spawns,
    runtime: _RuntimeStub,
) -> None:
    """T-C3: a disabled first-crack mode refusal propagates from ``freeze``."""
    spawns.payloads = _payloads(mode="disabled")
    await _run(tmp_path, resources)
    with pytest.raises(Exception) as raised:
        await runtime.calls[0]["identities"].freeze(OFF)
    assert type(raised.value).__name__ == "ColdIdentityError"


# ------------------------------------------------ T-E1/T-E3/T-E4 closed child environment


def test_t_e1_pure_mapping_posix_and_nt() -> None:
    """T-E1: exact required/optional/neutralised/config keys; credential presence only."""
    source = {
        "PATH": "/bin",
        "HOME": "/home/x",
        "USER": "u",
        "LOGNAME": "u",
        "LANG": "C",
        "SHELL": "/bin/zsh",
        "TERM": "xterm",
        "OPENROUTER_API_KEY": CANARY,
        "PYTHONPATH": "x",
        "COFFEE_X": "1",
    }
    snapshot = cold_composition._snapshot_environment(source, "OPENROUTER_API_KEY", "posix")
    assert snapshot is not None
    assert snapshot.credential_present is True and snapshot.coffee_names_present is True
    assert dict(snapshot.values) == {
        "PATH": "/bin",
        "HOME": "/home/x",
        "USER": "u",
        "LOGNAME": "u",
        "LANG": "C",
    }
    environment = cold_composition._cold_child_environment(
        snapshot.values, "posix", Path("/r/active.yaml")
    )
    assert dict(environment) == {
        "PATH": "/bin",
        "HOME": "/home/x",
        "USER": "u",
        "LOGNAME": "u",
        "LANG": "C",
        "SHELL": "",
        "TERM": "",
        "COFFEE_ROASTER_MCP_CONFIG": "/r/active.yaml",
    }
    assert isinstance(environment, types.MappingProxyType)
    with pytest.raises(TypeError):
        typing.cast(dict[str, str], environment)["PATH"] = "/evil"
    nt_source = {
        "PATH": "C:\\P",
        "PATHEXT": ".EXE",
        "SYSTEMROOT": "C:\\W",
        "TEMP": "C:\\T",
        "USERNAME": "u",
        "USERPROFILE": "C:\\U",
        "APPDATA": "C:\\A",
        "LOCALAPPDATA": "C:\\L",
        "HOMEDRIVE": "C:",
        "HOMEPATH": "\\U",
        "PROCESSOR_ARCHITECTURE": "AMD64",
        "SYSTEMDRIVE": "C:",
        "HOME": "h",
    }
    nt = cold_composition._snapshot_environment(nt_source, "K", "nt")
    assert nt is not None and nt.credential_present is False
    assert "PROCESSOR_ARCHITECTURE" not in nt.values and "HOME" not in nt.values
    assert cold_composition._admit_values(nt.values, "nt") is True
    nt_env = cold_composition._cold_child_environment(nt.values, "nt", Path("C:/r/a.yaml"))
    assert nt_env["PROCESSOR_ARCHITECTURE"] == "" and nt_env["SYSTEMDRIVE"] == ""
    assert nt_env["SYSTEMROOT"] == "C:\\W" and nt_env["PATHEXT"] == ".EXE"
    assert set(nt_env) == set(nt_source) - {"HOME"} | {"COFFEE_ROASTER_MCP_CONFIG"}
    empty = cold_composition._snapshot_environment({"K": ""}, "K", "posix")
    assert empty is not None and empty.credential_present is False


def test_t_e1_tables_match_the_contract() -> None:
    """The pinned tables are exactly the R4 tables."""
    assert cold_composition._REQUIRED["posix"] == {"HOME", "LOGNAME", "PATH", "USER"}
    assert cold_composition._NEUTRALISED["posix"] == {"SHELL", "TERM"}
    assert cold_composition._OPTIONAL["posix"] == {"LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ"}
    assert cold_composition._NEUTRALISED["nt"] == {"PROCESSOR_ARCHITECTURE", "SYSTEMDRIVE"}
    assert cold_composition._OPTIONAL["nt"] == {"WINDIR", "COMSPEC", "TMP"}
    assert cold_composition._REQUIRED["nt"] == {
        "APPDATA",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "USERNAME",
        "USERPROFILE",
    }
    assert set(mcp_stdio.DEFAULT_INHERITED_ENV_VARS) == set(_POSIX_SDK)


def test_t_e1_process_environment_reader_reads_admitted_values_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The single access site snapshots admitted values and presence only."""
    monkeypatch.setenv("TZ", "UTC")
    snapshot = cold_composition._read_process_environment("OPENROUTER_API_KEY", "posix")
    assert snapshot is not None
    assert snapshot.values["TZ"] == "UTC"
    assert set(snapshot.values) == {"PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "TZ"}
    assert snapshot.credential_present is True and snapshot.coffee_names_present is False
    monkeypatch.setenv("TZ", "changed")
    assert snapshot.values["TZ"] == "UTC"


def _frozen(tmp_path: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "USER": "cold-test",
        "LOGNAME": "cold-test",
        "SHELL": "",
        "TERM": "",
        "COFFEE_ROASTER_MCP_CONFIG": "/r/a.yaml",
    }


class _Session:
    """Fake initialisable session for the native spawn seam."""

    async def initialize(self) -> object:
        return None

    async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
        return types.SimpleNamespace(isError=False, structuredContent={"version": "0.2.2", "x": 1})


@pytest.mark.asyncio
async def test_t_c5_native_factory_and_process_group_hook_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-C5/T-C11: native factory + process-group hook retained; params frozen across spawns."""
    seen: list[tuple[StdioServerParameters, object]] = []

    @contextlib.asynccontextmanager
    async def recorder(
        params: StdioServerParameters, *, on_spawn: Callable[[int], None] | None = None
    ) -> AsyncGenerator[_Session]:
        seen.append((params, on_spawn))
        yield _Session()

    monkeypatch.setattr(mcp_client, "_spawn_stdio_session", recorder)
    original = _frozen(tmp_path)
    expected = dict(original)
    process = ColdMCPServerProcess(MCPConfig(), child_environment=original, family="posix")
    assert process._session_factory == process._default_session_factory
    monkeypatch.setenv("COFFEE_FIRST_CRACK_ONNX_THREADS", "99")
    original["COFFEE_ROASTER_MCP_CONFIG"] = "/evil.yaml"
    original["OPENROUTER_API_KEY"] = CANARY
    for _ in range(2):
        await process.start()
        assert process.running is True
        await process.stop()
        assert process.running is False and process.stop_unconfirmed is False
    assert len(seen) == 2
    for params, on_spawn in seen:
        assert params.env == expected
        assert params.args == ["serve"]
        assert params.command == resolve_mcp_command(DEFAULT_MCP_COMMAND)
        assert on_spawn == process._register_force_terminate
    assert seen[0][0].env is not seen[1][0].env


class _SpawnSentinel(OSError):
    """Raised by the SDK process-creation recorder; no process is ever created."""

    def __init__(self) -> None:
        super().__init__("spawn sentinel")


class _ProcessRecorder:
    """Stands in for the SDK's process-creation function; records ``env`` and refuses."""

    def __init__(self) -> None:
        self.envs: list[dict[str, str]] = []

    async def __call__(self, *args: object, **kwargs: object) -> object:
        env = kwargs.get("env")
        assert isinstance(env, dict)
        self.envs.append(dict(typing.cast(dict[str, str], env)))
        raise _SpawnSentinel


@pytest.mark.asyncio
async def test_t_e4_realised_sdk_env_equals_the_frozen_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-E4: the env the SDK hands to process creation equals the frozen mapping, every spawn.

    The real default factory and native shim stay in the path: the shim captures
    the recorder as its original and restores it after each attempt.  Retries
    happen on the same instance (``start`` permits a retry after the failure).
    """
    sdk = mcp_stdio
    true_original = sdk._create_platform_compatible_process  # pyright: ignore[reportPrivateUsage]
    recorder = _ProcessRecorder()
    values = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "USER": "u", "LOGNAME": "u"}
    original = dict(cold_composition._cold_child_environment(values, "posix", Path("/r/a.yaml")))
    expected = {**values, "SHELL": "", "TERM": "", "COFFEE_ROASTER_MCP_CONFIG": "/r/a.yaml"}
    errors: list[str] = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sdk, "_create_platform_compatible_process", recorder)
        process = ColdMCPServerProcess(MCPConfig(), child_environment=original, family="posix")
        assert process._session_factory == process._default_session_factory
        # Post-construction mutations: absent->present defaults, new values, overrides.
        patch.delenv("SHELL", raising=False)
        patch.delenv("TERM", raising=False)
        for attempt in range(2):
            patch.setenv("SHELL", f"/bin/evil-shell-{attempt}")
            patch.setenv("TERM", f"evil-term-{attempt}")
            patch.setenv("PATH", f"/evil/bin-{attempt}")
            patch.setenv("HOME", f"/evil/home-{attempt}")
            patch.setenv("COFFEE_FIRST_CRACK_MODE", "manual")
            patch.setenv("COFFEE_FIRST_CRACK_REVISION", "evil")
            patch.setenv("OPENROUTER_API_KEY", CANARY)
            original["PATH"] = "/evil/caller-mapping"
            with pytest.raises(mcp_client.MCPConnectionError) as raised:
                await process.start()
            errors.append(str(raised.value))
            assert process.running is False and process.stop_unconfirmed is False
            assert sdk._create_platform_compatible_process is recorder  # pyright: ignore[reportPrivateUsage]
        assert recorder.envs == [expected, expected]
        assert recorder.envs[0] is not recorder.envs[1]
    assert sdk._create_platform_compatible_process is true_original  # pyright: ignore[reportPrivateUsage]
    for recorded in recorder.envs:
        assert CANARY not in "".join(recorded.values())
    assert all(CANARY not in text and "spawn sentinel" in text for text in errors)


@pytest.mark.asyncio
async def test_t_e3b_list_mutated_after_construction_fails_before_the_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-E3b: an EVIL default name added after construction refuses the spawn; 0 SDK calls."""
    sdk = mcp_stdio
    recorder = _ProcessRecorder()
    monkeypatch.setattr(sdk, "_create_platform_compatible_process", recorder)
    process = ColdMCPServerProcess(MCPConfig(), child_environment=_frozen(tmp_path), family="posix")
    monkeypatch.setattr(sdk, "DEFAULT_INHERITED_ENV_VARS", [*_POSIX_SDK, "EVIL"])
    with pytest.raises(mcp_client.MCPConnectionError):
        await process.start()
    assert recorder.envs == []
    assert process.running is False and process.stop_unconfirmed is False
    with pytest.raises(ValueError, match=r"\ACold child environment refused\.\Z"):
        process.build_server_parameters()


def test_t_e3_valid_construction_keeps_an_immutable_copy(tmp_path: Path) -> None:
    """T-E3 positive: the stored mapping is unaffected by mutating the source mapping."""
    original = _frozen(tmp_path)
    process = ColdMCPServerProcess(MCPConfig(), child_environment=original, family="posix")
    original["PATH"] = "/evil"
    original["OPENROUTER_API_KEY"] = CANARY
    assert process.build_server_parameters().env == _frozen(tmp_path)
    assert process.build_server_parameters().env is not process.build_server_parameters().env


def test_t_c5_command_resolution_preserves_the_existing_helper(tmp_path: Path) -> None:
    """T-C5: default resolves to the pinned in-venv script; explicit override is verbatim."""
    default = ColdMCPServerProcess(MCPConfig(), child_environment=_frozen(tmp_path), family="posix")
    resolved = default.build_server_parameters().command
    assert resolved == resolve_mcp_command(DEFAULT_MCP_COMMAND)
    assert Path(resolved).is_absolute() and Path(resolved).is_file()
    explicit = ColdMCPServerProcess(
        MCPConfig(command="custom-mcp-binary"), child_environment=_frozen(tmp_path), family="posix"
    )
    params = explicit.build_server_parameters()
    assert params.command == "custom-mcp-binary"
    assert params.env == _frozen(tmp_path)


CONSTRUCTOR_REFUSALS: dict[str, tuple[dict[str, str], dict[str, str | None]]] = {
    "config_env": ({"A": "1"}, {}),
    "shell_missing": ({}, {"SHELL": None}),
    "shell_nonempty": ({}, {"SHELL": "zsh"}),
    "unknown_key": ({}, {"UNKNOWN": "1"}),
    "config_missing": ({}, {"COFFEE_ROASTER_MCP_CONFIG": None}),
    "config_empty": ({}, {"COFFEE_ROASTER_MCP_CONFIG": ""}),
    "extra_coffee": ({}, {"COFFEE_X": "1"}),
    "lower_coffee": ({}, {"coffee_roaster_mcp_config": "/x"}),
    "pythonpath": ({}, {"PYTHONPATH": "/evil"}),
    "credential": ({}, {"OPENROUTER_API_KEY": CANARY}),
    "path_empty": ({}, {"PATH": ""}),
    "home_function": ({}, {"HOME": "() { :; }"}),
    "lang_function": ({}, {"LANG": "()x"}),
    "user_missing": ({}, {"USER": None}),
}


@pytest.mark.parametrize("name", sorted(CONSTRUCTOR_REFUSALS))
def test_t_e3_constructor_refusals(name: str, tmp_path: Path) -> None:
    """T-E3: the constructor refuses anything but the exact closed mapping."""
    env_override, changes = CONSTRUCTOR_REFUSALS[name]
    environment: dict[str, str] = _frozen(tmp_path)
    for key, value in changes.items():
        if value is None:
            del environment[key]
        else:
            environment[key] = value
    with pytest.raises(ValueError, match=r"\ACold child environment refused\.\Z"):
        ColdMCPServerProcess(
            MCPConfig(env=env_override), child_environment=environment, family="posix"
        )


def test_t_e3_constructor_refuses_live_list_family_and_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-E3: an invalid live SDK list, unknown family, non-mapping or non-``str`` item."""
    bad = typing.cast(dict[str, str], {**_frozen(tmp_path), "PATH": 1})
    for environment, family in (
        (bad, "posix"),
        (_frozen(tmp_path), "java"),
        (typing.cast(dict[str, str], [("PATH", "/bin")]), "posix"),
    ):
        with pytest.raises(ValueError):
            ColdMCPServerProcess(MCPConfig(), child_environment=environment, family=family)
    monkeypatch.setattr(mcp_stdio, "DEFAULT_INHERITED_ENV_VARS", ["EVIL"])
    with pytest.raises(ValueError):
        ColdMCPServerProcess(MCPConfig(), child_environment=_frozen(tmp_path), family="posix")


def test_t_c5n_normal_spawn_behaviour_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """T-C5n: the normal process still merges the ambient environment; nothing added to it."""
    monkeypatch.setenv("RP954_PROBE", "probe")
    params = MCPServerProcess(MCPConfig(env={"A": "1"})).build_server_parameters()
    assert params.env == {**os.environ, "A": "1"}
    assert MCPServerProcess.build_server_parameters.__qualname__ == (
        "MCPServerProcess.build_server_parameters"
    )
    assert not [name for name in vars(MCPServerProcess) if "cold" in name.lower()]
    own = {name for name in vars(ColdMCPServerProcess) if not name.startswith("__")}
    assert own == {"build_server_parameters"}
    assert "__init__" in vars(ColdMCPServerProcess)
    assert ColdMCPServerProcess.__mro__[1] is MCPServerProcess


# ------------------------------------------------------------- T-C6 committed bytes


class _ChildProcess:
    def __init__(self) -> None:
        self.running = False
        self.stop_unconfirmed = False
        self.events: list[str] = []

    async def start(self) -> None:
        self.events.append("start")

    async def stop(self) -> None:
        self.events.append("stop")


@pytest.mark.asyncio
async def test_t_c6_configure_phase_installs_exactly_the_committed_bytes(
    tmp_path: Path,
) -> None:
    """T-C6: OFF then ON install the committed bytes; pass-through members; private modes."""
    process = _ChildProcess()
    directory = tmp_path / "res"
    directory.mkdir(mode=0o700)
    child = ColdMCPChild(
        typing.cast(MCPServerProcess, process),
        directory=directory,
        rendered={OFF: b"off: 1\n", ON: b"on: 2\n"},
    )
    child.configure_phase(OFF)
    assert (directory / "active.yaml").read_bytes() == b"off: 1\n"
    assert stat.S_IMODE((directory / "active.yaml").stat().st_mode) == 0o600
    child.configure_phase(ON)
    assert (directory / "active.yaml").read_bytes() == b"on: 2\n"
    assert not (directory / "active.yaml.next").exists()
    await child.start()
    await child.stop()
    assert process.events == ["start", "stop"]
    assert child.running is False and child.stop_unconfirmed is False
    process.stop_unconfirmed = True
    assert child.stop_unconfirmed is True
    process.running = True
    assert child.running is True
    with pytest.raises(ColdCompositionChildError, match=r"\ACold child configuration refused\.\Z"):
        child.configure_phase(OFF)
    assert (directory / "active.yaml").read_bytes() == b"on: 2\n"


@pytest.mark.asyncio
async def test_t_c6_tampered_install_is_child_start_failed_with_the_real_runtime(
    tmp_path: Path,
    resources: ColdCompositionResources,
    spawns: _Spawns,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-C6: other bytes landing on ``active.yaml`` => the exact CHILD_START_FAILED row."""
    real_replace = os.replace

    def tampering_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        real_replace(src, dst)
        if Path(dst).name == "active.yaml":
            Path(dst).write_bytes(b"roaster:\n  driver: hottop\n")

    monkeypatch.setattr(os, "replace", tampering_replace)
    result = await _run(tmp_path, resources)
    assert _project(result) == (
        ColdTwoPhaseOutcome.REFUSED_BEFORE_EVIDENCE,
        ColdRunStartRefusal.CHILD_START_FAILED,
        None,
        ColdChildOwnership.NOT_OWNED,
        None,
        None,
        ColdTwoPhaseAdvisoryPath.NOT_APPLICABLE,
        ColdTwoPhaseProviderCheck.NOT_CHECKED,
    )
    assert len(spawns.processes) == 1 and spawns.processes[0].started == 0
    assert list((tmp_path / "evidence").iterdir()) == []


# --------------------------------------------------------- T-C7 source and profile


def _expected_render(tmp_path: Path, text: str, recording: bool) -> bytes:
    oracle = tmp_path / f"oracle-{recording}"
    oracle.mkdir(exist_ok=True)
    (oracle / "source.yaml").write_text(text, encoding="utf-8")
    config = _inputs(tmp_path).device_config.model_copy(
        update={"recording_enabled": recording, "recording_autocapture": recording}
    )
    render_mcp_yaml(config, oracle / "source.yaml", oracle / "out.yaml")
    return (oracle / "out.yaml").read_bytes()


@pytest.mark.asyncio
async def test_t_c7_template_yaml_gives_the_expected_profiles(
    tmp_path: Path,
    resources: ColdCompositionResources,
    spawns: _Spawns,
    runtime: _RuntimeStub,
) -> None:
    """T-C7: template-shaped YAML with ``hop_seconds: null``; OFF and ON digests differ."""
    result = await _run(tmp_path, resources)
    assert result is runtime.result
    profiles = runtime.calls[0]["identities"]._profiles
    directory = resources.directory
    assert directory is not None
    off_bytes = (directory / "phase-off.yaml").read_bytes()
    on_bytes = (directory / "phase-on.yaml").read_bytes()
    assert off_bytes == _expected_render(tmp_path, TEMPLATE_YAML, False)
    assert on_bytes == _expected_render(tmp_path, TEMPLATE_YAML, True)
    assert (directory / "source.yaml").read_bytes() == TEMPLATE_YAML.encode()
    for name in ("source.yaml", "phase-off.yaml", "phase-on.yaml"):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    off, on = profiles[OFF], profiles[ON]
    assert off.source_sha256 == hashlib.sha256(off_bytes).hexdigest()
    assert on.source_sha256 == hashlib.sha256(on_bytes).hexdigest()
    assert off.source_sha256 != on.source_sha256
    comparable = off.model_dump(exclude={"source_sha256", "source_byte_length"})
    assert comparable == on.model_dump(exclude={"source_sha256", "source_byte_length"})
    assert comparable == {
        "first_crack_onnx_threads": 2,
        "first_crack_min_positive_windows": 3,
        "first_crack_confirmation_window_seconds": 30.0,
        "first_crack_revision": "test-revision",
        "audio_sample_rate": 16000,
        "audio_window_seconds": 10.0,
        "audio_overlap": 0.3,
        "audio_hop_seconds": None,
        "session_ror_window_seconds": 60,
        "session_ror_min_sample_seconds": 10,
    }


@pytest.mark.parametrize("change", ["replace", "grow"])
@pytest.mark.asyncio
async def test_t_c7_operator_source_is_read_once_into_the_private_snapshot(
    change: str,
    tmp_path: Path,
    resources: ColdCompositionResources,
    spawns: _Spawns,
    runtime: _RuntimeStub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-C7: changing the operator file after the snapshot does not change rendered bytes."""
    source = _source(tmp_path)
    real_render = render_mcp_yaml

    def render_after_change(cfg: MCPDeviceConfig, src: Path | None, dest: Path) -> None:
        if change == "replace":
            # An unmanaged key the overlay never rewrites, so a re-read would show.
            source.write_text(TEMPLATE_YAML.replace("test-revision", "swapped-revision"))
        else:
            with source.open("a", encoding="utf-8") as handle:
                handle.write("".join(f"grown{i}: 1\n" for i in range(2_000)))
        real_render(cfg, src, dest)

    monkeypatch.setattr(cold_composition, "render_mcp_yaml", render_after_change)
    result = await _run(tmp_path, resources, inputs=_inputs(tmp_path, source))
    assert result is runtime.result
    directory = resources.directory
    assert directory is not None
    assert (directory / "phase-off.yaml").read_bytes() == _expected_render(
        tmp_path, TEMPLATE_YAML, False
    )
    assert (directory / "phase-on.yaml").read_bytes() == _expected_render(
        tmp_path, TEMPLATE_YAML, True
    )


def _nested(depth: int) -> str:
    """A mapping document whose deepest value sits at ``depth`` (the root is depth 1)."""
    text = "leaf"
    for _ in range(depth - 2):
        text = "{k: " + text + "}"
    return TEMPLATE_YAML + "deep: " + text + "\n"


def _many(nodes: int) -> str:
    """A document with exactly ``nodes`` value nodes counting the root."""
    base_nodes = _count_nodes(TEMPLATE_YAML)
    extra = nodes - base_nodes - 1  # one more for the "pad" mapping value
    lines = "".join(f"  k{i}: 1\n" for i in range(extra))
    return TEMPLATE_YAML + "pad:\n" + lines


def _count_nodes(text: str) -> int:
    import yaml  # noqa: PLC0415

    stack: list[object] = [yaml.safe_load(text)]
    count = 0
    while stack:
        node = stack.pop()
        count += 1
        if isinstance(node, dict):
            stack.extend(typing.cast(dict[str, object], node).values())
        elif isinstance(node, list):
            stack.extend(typing.cast(list[object], node))
    return count


def _depth(text: str) -> int:
    import yaml  # noqa: PLC0415

    stack: list[tuple[object, int]] = [(yaml.safe_load(text), 1)]
    deepest = 0
    while stack:
        node, depth = stack.pop()
        deepest = max(deepest, depth)
        if isinstance(node, dict):
            stack.extend((v, depth + 1) for v in typing.cast(dict[str, object], node).values())
        elif isinstance(node, list):
            stack.extend((v, depth + 1) for v in typing.cast(list[object], node))
    return deepest


def _padded(size: int) -> bytes:
    data = TEMPLATE_YAML.encode()
    return data + b"#" * (size - len(data) - 1) + b"\n"


SOURCE_REFUSALS: dict[str, bytes] = {
    "oversize_65537": _padded(65_537),
    "non_utf8": TEMPLATE_YAML.encode() + b"note: \xff\xfe\n",
    "alias": (TEMPLATE_YAML + "a: &x [1, 2]\nb: *x\n").encode(),
    "billion_laughs": (
        TEMPLATE_YAML + 'l0: &l0 ["lol", "lol"]\nl1: &l1 [*l0, *l0, *l0]\nl2: &l2 [*l1, *l1, *l1]\n'
    ).encode(),
    "python_tag": (TEMPLATE_YAML + "x: !!python/object:os.system {}\n").encode(),
    "str_tag": (TEMPLATE_YAML + "x: !!str 5\n").encode(),
    "directive": ("%YAML 1.1\n---\n" + TEMPLATE_YAML).encode(),
    "depth_33": _nested(33).encode(),
    "nodes_4097": _many(4_097).encode(),
    "non_str_key": (TEMPLATE_YAML + "1: one\n").encode(),
    "list_root": b"- a\n- b\n",
    "scalar_root": b"just text\n",
    "two_documents": (TEMPLATE_YAML + "---\nb: 1\n").encode(),
    "malformed": (TEMPLATE_YAML + "x: [unclosed\n").encode(),
}


@pytest.mark.parametrize("name", sorted(SOURCE_REFUSALS))
@pytest.mark.asyncio
async def test_t_c7_source_refusals(
    name: str,
    tmp_path: Path,
    resources: ColdCompositionResources,
    spawns: _Spawns,
    runtime: _RuntimeStub,
) -> None:
    """T-C7: bounded, alias/tag/directive-free, mapping-rooted source or MCP_SOURCE_NOT_ADMITTED."""
    source = _source(tmp_path, SOURCE_REFUSALS[name])
    result = await _run(tmp_path, resources, inputs=_inputs(tmp_path, source))
    assert result is R.MCP_SOURCE_NOT_ADMITTED
    assert spawns.processes == [] and runtime.calls == []
    directory = resources.directory
    assert directory is not None
    assert not (directory / "phase-off.yaml").exists()


@pytest.mark.asyncio
async def test_t_c7_source_size_admits_exactly_the_limit(
    tmp_path: Path,
    resources: ColdCompositionResources,
    spawns: _Spawns,
    runtime: _RuntimeStub,
) -> None:
    """T-C7 positive control: a 65,536-byte source is admitted end to end."""
    source = _source(tmp_path, _padded(65_536))
    result = await _run(tmp_path, resources, inputs=_inputs(tmp_path, source))
    assert result is runtime.result


def test_t_c7_structure_bounds_admit_exactly_at_the_limit() -> None:
    """T-C7 positive controls: depth 32 and 4,096 nodes admitted; one more refused."""
    assert cold_composition._admit_yaml(_nested(32).encode())["deep"] is not None
    assert len(cold_composition._admit_yaml(_many(4_096).encode())) > 0
    for text in (_nested(33), _many(4_097)):
        with pytest.raises(ColdCompositionChildError):
            cold_composition._admit_yaml(text.encode())


def test_t_c7_bound_fixtures_hit_their_exact_counts() -> None:
    """The bound fixtures are what they claim to be."""
    assert len(_padded(65_536)) == 65_536 and len(_padded(65_537)) == 65_537
    assert _count_nodes(_many(4_096)) == 4_096
    assert _count_nodes(_many(4_097)) == 4_097
    assert _depth(_nested(32)) == 32 and _depth(_nested(33)) == 33


@pytest.mark.asyncio
async def test_t_c7_symlink_fifo_and_missing_sources_refused(
    tmp_path: Path,
    resources: ColdCompositionResources,
    spawns: _Spawns,
    runtime: _RuntimeStub,
) -> None:
    """T-C7: a symlink, a FIFO or a missing file is refused without reading."""
    target = _source(tmp_path)
    link = tmp_path / "link.yaml"
    link.symlink_to(target)
    fifo = tmp_path / "fifo.yaml"
    os.mkfifo(fifo)
    for path in (link, fifo, tmp_path / "missing.yaml"):
        with ColdCompositionResources() as fresh:
            result = await _run(tmp_path, fresh, inputs=_inputs(tmp_path, path))
            assert result is R.MCP_SOURCE_NOT_ADMITTED
    assert spawns.processes == [] and runtime.calls == []


@pytest.mark.asyncio
async def test_t_c7_no_follow_applies_at_the_actual_open(
    tmp_path: Path,
    resources: ColdCompositionResources,
    spawns: _Spawns,
    runtime: _RuntimeStub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-C7: a symlink swapped in after the regular-file check is refused by the open itself."""
    target = _source(tmp_path)
    link = tmp_path / "swapped.yaml"
    link.symlink_to(target)
    real_lstat = os.lstat

    def lying_lstat(path: typing.Any, *args: typing.Any, **kwargs: typing.Any) -> os.stat_result:
        if not args and not kwargs and Path(path) == link:
            return real_lstat(target)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", lying_lstat)
    result = await _run(tmp_path, resources, inputs=_inputs(tmp_path, link))
    assert result is R.MCP_SOURCE_NOT_ADMITTED


@pytest.mark.asyncio
async def test_t_c7_identity_change_between_check_and_open_refused(
    tmp_path: Path,
    resources: ColdCompositionResources,
    spawns: _Spawns,
    runtime: _RuntimeStub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-C7: a different regular file at open time (identity mismatch) is refused."""
    source = _source(tmp_path)
    decoy = tmp_path / "decoy.yaml"
    decoy.write_text(TEMPLATE_YAML, encoding="utf-8")
    real_lstat = os.lstat

    def other_identity(path: typing.Any, *args: typing.Any, **kwargs: typing.Any) -> os.stat_result:
        if not args and not kwargs and Path(path) == source:
            return real_lstat(decoy)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", other_identity)
    result = await _run(tmp_path, resources, inputs=_inputs(tmp_path, source))
    assert result is R.MCP_SOURCE_NOT_ADMITTED


PROFILE_REFUSALS: dict[str, str] = {
    "hop_seconds_absent": TEMPLATE_YAML.replace("  hop_seconds: null\n", ""),
    "revision_null": TEMPLATE_YAML.replace("revision: test-revision", "revision: null"),
    "revision_absent": TEMPLATE_YAML.replace("  revision: test-revision\n", ""),
    "onnx_threads_str": TEMPLATE_YAML.replace("onnx_threads: 2", 'onnx_threads: "2"'),
    "overlap_one": TEMPLATE_YAML.replace("overlap: 0.3", "overlap: 1.0"),
    "credential_revision": TEMPLATE_YAML.replace(
        "revision: test-revision", "revision: sk-abcdefghijklmnopqrstuvwxyz"
    ),
    "session_missing": TEMPLATE_YAML.replace(
        "session:\n  ror_window_seconds: 60\n  ror_min_sample_seconds: 10\n", ""
    ),
    "section_not_mapping": TEMPLATE_YAML.replace(
        "session:\n  ror_window_seconds: 60\n  ror_min_sample_seconds: 10\n", "session: 5\n"
    ),
}


@pytest.mark.parametrize("name", sorted(PROFILE_REFUSALS))
@pytest.mark.asyncio
async def test_t_c7_profile_refusals(
    name: str,
    tmp_path: Path,
    resources: ColdCompositionResources,
    spawns: _Spawns,
    runtime: _RuntimeStub,
) -> None:
    """T-C7: ten strict comparables, no defaults, or PROFILE_NOT_ADMITTED."""
    text = PROFILE_REFUSALS[name]
    assert text != TEMPLATE_YAML
    result = await _run(tmp_path, resources, inputs=_inputs(tmp_path, _source(tmp_path, text)))
    assert result is R.PROFILE_NOT_ADMITTED
    assert spawns.processes == [] and runtime.calls == []


# ----------------------------------------------------------- device config and root


@pytest.mark.parametrize(
    "device",
    [
        {"recording_enabled": True},
        {"recording_enabled": False},
        {"recording_autocapture": False},
        {"mcp_yaml_source_path": Path("relative/coffee.yaml")},
        {"mcp_yaml_source_path": None},
    ],
    ids=["enabled_true", "enabled_false", "autocapture_set", "relative_source", "no_source"],
)
@pytest.mark.asyncio
async def test_device_config_refusals(
    device: dict[str, typing.Any],
    tmp_path: Path,
    resources: ColdCompositionResources,
    spawns: _Spawns,
    runtime: _RuntimeStub,
) -> None:
    """Step 5: a recording-set base or a non-absolute source => DEVICE_CONFIG_NOT_ADMITTED."""
    inputs = _inputs(tmp_path, **device)
    assert await _run(tmp_path, resources, inputs=inputs) is R.DEVICE_CONFIG_NOT_ADMITTED
    assert spawns.processes == [] and runtime.calls == []


@pytest.mark.asyncio
async def test_device_config_copy_failure_refused(
    tmp_path: Path,
    resources: ColdCompositionResources,
    spawns: _Spawns,
    runtime: _RuntimeStub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 5: an ordinary exception while deriving the phase configs is a closed refusal."""
    inputs = _inputs(tmp_path)

    def broken(self: MCPDeviceConfig, **kwargs: object) -> MCPDeviceConfig:
        raise RuntimeError(CANARY)

    monkeypatch.setattr(MCPDeviceConfig, "model_copy", broken)
    assert await _run(tmp_path, resources, inputs=inputs) is R.DEVICE_CONFIG_NOT_ADMITTED


@pytest.mark.parametrize("root", ["relative/evidence", "/nonexistent-rp954-root/evidence"])
@pytest.mark.asyncio
async def test_root_refusals(
    root: str,
    tmp_path: Path,
    resources: ColdCompositionResources,
    spawns: _Spawns,
    runtime: _RuntimeStub,
) -> None:
    """Step 7: an unadmitted evidence root => ROOT_NOT_ADMITTED."""
    inputs = _inputs(tmp_path).model_copy(update={"pi_evidence_root": root})
    assert await _run(tmp_path, resources, inputs=inputs) is R.ROOT_NOT_ADMITTED
    assert spawns.processes == [] and runtime.calls == []


@pytest.mark.asyncio
async def test_protected_root_refused(
    tmp_path: Path,
    resources: ColdCompositionResources,
    spawns: _Spawns,
    runtime: _RuntimeStub,
) -> None:
    """Step 7: the caller's protected roots are passed through to admission."""
    inputs = _inputs(tmp_path)
    inputs = inputs.model_copy(update={"protected_roots": (str(tmp_path),)})
    assert await _run(tmp_path, resources, inputs=inputs) is R.ROOT_NOT_ADMITTED


# ---------------------------------------------------------------------- T-C8 run ID


@pytest.mark.asyncio
async def test_t_c8_run_id_minted_once_for_both_phases(
    tmp_path: Path,
    resources: ColdCompositionResources,
    spawns: _Spawns,
    runtime: _RuntimeStub,
) -> None:
    """T-C8: ``2026-10-03T12:00:00+00:00`` + ``abc`` => ``20261003T120000Z-abc``."""
    calls: list[int] = []

    def suffix() -> str:
        calls.append(1)
        return "abc"

    await _run(tmp_path, resources, suffix=suffix)
    identities = runtime.calls[0]["identities"]
    assert identities._run_id == "20261003T120000Z-abc"
    assert calls == [1]
    model = pydantic.create_model("_RunIdProbe", run_id=(str, ColdRunHeader.model_fields["run_id"]))
    assert model.model_validate({"run_id": identities._run_id}) is not None
    assert (await identities.freeze(OFF)).run_id == (await identities.freeze(ON)).run_id


@pytest.mark.parametrize(
    ("instant", "suffix"),
    [
        ("2026-10-03T12:00:00+01:00", "abc"),
        ("2026-10-03T12:00:00", "abc"),
        ("not-a-time", "abc"),
        ("2026-10-03T12:00:00+00:00", "UPPER"),
        ("2026-10-03T12:00:00+00:00", "a" * 49),
        ("2026-10-03T12:00:00+00:00", ""),
        ("2026-10-03T12:00:00+00:00", "abc\n"),
    ],
    ids=["plus_one", "naive", "garbage", "upper", "len_49", "empty", "newline"],
)
@pytest.mark.asyncio
async def test_t_c8_run_id_refusals(
    instant: str,
    suffix: str,
    tmp_path: Path,
    resources: ColdCompositionResources,
    spawns: _Spawns,
    runtime: _RuntimeStub,
) -> None:
    """T-C8: a non-UTC instant or an unadmitted suffix => RUN_ID_NOT_ADMITTED."""
    result = await _run(tmp_path, resources, clock=_Clock(instant), suffix=lambda: suffix)
    assert result is R.RUN_ID_NOT_ADMITTED
    assert spawns.processes == [] and runtime.calls == []


@pytest.mark.asyncio
async def test_t_c8_suffix_boundary_and_raising_clock(
    tmp_path: Path,
    resources: ColdCompositionResources,
    spawns: _Spawns,
    runtime: _RuntimeStub,
) -> None:
    """T-C8: 48 characters admitted; a raising clock or non-``str`` suffix refused."""
    assert await _run(tmp_path, resources, suffix=lambda: "a" * 48) is runtime.result
    assert await _run(tmp_path, resources, clock=_RaisingClock()) is R.RUN_ID_NOT_ADMITTED
    not_str = typing.cast(Callable[[], str], lambda: 5)
    assert await _run(tmp_path, resources, suffix=not_str) is R.RUN_ID_NOT_ADMITTED


# ---------------------------------------------------------------- T-C10 PENDING oracle


@pytest.mark.asyncio
async def test_t_c10_pending_result_is_returned_as_is(
    tmp_path: Path,
    resources: ColdCompositionResources,
    spawns: _Spawns,
    runtime: _RuntimeStub,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T-C10: the PENDING_AT_CHECK result is the same object; nothing cleaned or emitted."""
    caplog.set_level(logging.DEBUG)
    result = await _run(tmp_path, resources)
    same = result is runtime.result
    assert same
    assert _project(result)[-1] is ColdTwoPhaseProviderCheck.PENDING_AT_CHECK
    directory = resources.directory
    assert directory is not None and directory.is_dir()
    assert (directory / "phase-off.yaml").is_file()
    assert caplog.records == []


def test_t_c10_function_shape_has_one_final_awaited_runtime_call() -> None:
    """T-C10: the final statement is ``return await run_two_phase_characterisation(...)``."""
    tree = ast.parse(Path(cold_composition.__file__).read_text(encoding="utf-8"))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_cold_characterisation"
    )
    last = function.body[-1]
    assert isinstance(last, ast.Return) and isinstance(last.value, ast.Await)
    call = last.value.value
    assert isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    assert call.func.id == "run_two_phase_characterisation"
    nodes = list(ast.walk(function))
    assert not [node for node in nodes if isinstance(node, (ast.Try, ast.With, ast.AsyncWith))]
    assert [node for node in nodes if isinstance(node, ast.Await)] == [last.value]
    constructions = [
        node
        for node in nodes
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"_construct_process", "ColdCharacterisationMCPClient"}
    ]
    assert len(constructions) == 2


# ---------------------------------------------------------------- T-C12 closed refusals


@pytest.mark.asyncio
async def test_t_c12_builder_exception_is_closed_and_content_free(
    tmp_path: Path,
    resources: ColdCompositionResources,
    spawns: _Spawns,
    runtime: _RuntimeStub,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T-C12: an ordinary builder exception => ADVISOR_BUILD_FAILED with no canary anywhere."""
    caplog.set_level(logging.DEBUG)

    def raising(config: AppConfig) -> RoastAdvisor | None:
        raise RuntimeError(CANARY)

    result = await _run(tmp_path, resources, builder=raising)
    assert result is R.ADVISOR_BUILD_FAILED
    assert CANARY not in caplog.text
    directory = resources.directory
    assert directory is not None
    assert list(directory.iterdir()) == []
    assert spawns.processes == [] and runtime.calls == []


@pytest.mark.asyncio
async def test_t_c12_base_exception_propagates_unchanged(
    tmp_path: Path,
    resources: ColdCompositionResources,
    spawns: _Spawns,
    runtime: _RuntimeStub,
) -> None:
    """T-C12: a ``KeyboardInterrupt`` from the builder propagates as the same object."""
    interrupt = KeyboardInterrupt()

    def interrupting(config: AppConfig) -> RoastAdvisor | None:
        raise interrupt

    with pytest.raises(KeyboardInterrupt) as raised:
        await _run(tmp_path, resources, builder=interrupting)
    assert raised.value is interrupt
    assert spawns.processes == [] and runtime.calls == []


def test_t_c12_refusal_enum_is_closed_plain_and_ordered() -> None:
    """T-C12: eleven plain-``Enum`` members in check order."""
    assert [member.name for member in R] == [
        "RESOURCES_NOT_ADMITTED",
        "MCP_ENV_NOT_ADMITTED",
        "CREDENTIAL_ABSENT",
        "ADVISOR_BUILD_FAILED",
        "ADVISOR_UNAVAILABLE",
        "ADVISOR_NOT_ADMITTED",
        "DEVICE_CONFIG_NOT_ADMITTED",
        "RUN_ID_NOT_ADMITTED",
        "ROOT_NOT_ADMITTED",
        "MCP_SOURCE_NOT_ADMITTED",
        "PROFILE_NOT_ADMITTED",
    ]
    assert R.__mro__[1:] == (enum.Enum, object)


# ------------------------------------------------------------------- resources


def test_resources_are_private_single_use_and_cleaned_on_exit() -> None:
    """Resources: 0700 on entry, ``None`` outside, removed on exit, single-use."""
    resources = ColdCompositionResources()
    assert resources.directory is None
    with resources as entered:
        directory = entered.directory
        assert directory is not None and directory.name.startswith("roastpilot-cold-")
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        (directory / "file").write_bytes(b"x")
    assert resources.directory is None and not directory.exists()
    with pytest.raises(ColdCompositionChildError):
        resources.__enter__()
    resources.__exit__(None, None, None)


def test_inputs_are_strict_frozen_and_complete(tmp_path: Path) -> None:
    """Inputs: every field required, strict, frozen, extra forbidden."""
    inputs = _inputs(tmp_path)
    with pytest.raises(pydantic.ValidationError):
        inputs.model_copy(update={}).__class__.model_validate(
            {**inputs.model_dump(), "unexpected": 1}
        )
    with pytest.raises(pydantic.ValidationError):
        ColdHostFacts.model_validate({**inputs.host_facts.model_dump(), "kernel": ""})
    data = {name: getattr(inputs, name) for name in ColdCompositionInputs.model_fields}
    with pytest.raises(pydantic.ValidationError):
        ColdCompositionInputs.model_validate({**data, "protected_roots": ["/x"]})
    del data["serial_port_path"]
    with pytest.raises(pydantic.ValidationError):
        ColdCompositionInputs.model_validate(data)
    with pytest.raises(pydantic.ValidationError):
        inputs.stimulus_block = "changed"  # type: ignore[misc]
