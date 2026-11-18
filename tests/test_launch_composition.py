"""#1078 C1: launch composition, authority isolation, requested capture and launch records.

Hardware-free.  Domain tests call :mod:`roastpilot_agent.launch_composition`
directly; transport tests drive the real :class:`MCPServerProcess` through a
local recording session factory (no child is spawned), temporary YAML files and
an isolated working directory.  The bounded reader is exercised with portable
injected metadata/descriptor seams; no device path is opened.
"""

# pyright: reportPrivateUsage=false

import ast
import dataclasses
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import cast

import mcp.client.stdio as mcp_stdio
import pytest
from mcp import StdioServerParameters

from roastpilot_agent import cold_composition, live, mcp_client
from roastpilot_agent import launch_composition as lc
from roastpilot_agent.config import AppConfig, MCPConfig, MCPDeviceConfig
from roastpilot_agent.human_protection import ControlAuthority
from roastpilot_agent.launch_composition import (
    AudioSourceKind,
    AuthorityClass,
    ChildConfigRead,
    ChildConfigSelection,
    ChildEnvBase,
    ConfigReadStatus,
    ConfigSelectionKind,
    HopSource,
    LaunchEvidence,
    LaunchProofStatus,
    LaunchRecord,
    LaunchRefusal,
    LaunchRefusalError,
    RecorderStream,
    RecorderStreamSource,
    RequestedCapture,
)
from roastpilot_agent.mcp_client import (
    LaunchCompositionError,
    MCPConnectionError,
    MCPServerProcess,
)
from tests.test_mcp_client import CANNED, FakeResult

AUTH = lc.AUTHORITY_ENV_KEY
CFG = lc.CONFIG_PATH_ENV_KEY
MIC = AudioSourceKind.MICROPHONE
WAV = AudioSourceKind.WAV
MANUAL_YAML = "first_crack:\n  event_authority: manual\n"

# --------------------------------------------------------------------------- doubles


class _Session:
    """Initializable fake session; ``get_server_info`` is the only startup call."""

    def __init__(
        self,
        *,
        init_error: Exception | None = None,
        health_error: Exception | None = None,
        on_init: Callable[[], None] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, object] | None]] = []
        self._init_error = init_error
        self._health_error = health_error
        self._on_init = on_init

    async def initialize(self) -> object:
        if self._on_init is not None:
            self._on_init()
        if self._init_error is not None:
            raise self._init_error
        return None

    async def call_tool(self, name: str, arguments: dict[str, object] | None = None) -> object:
        self.calls.append((name, arguments))
        if self._health_error is not None:
            raise self._health_error
        return FakeResult(structuredContent=dict(cast("dict[str, object]", CANNED[name])))


class _Probe:
    """Session factory recording the consumed params, call count and teardown."""

    def __init__(
        self, session: _Session | None = None, on_enter: Callable[[], None] | None = None
    ) -> None:
        self.session = session if session is not None else _Session()
        self.params: StdioServerParameters | None = None
        self.calls = 0
        self.exited = False
        self.on_enter = on_enter

    def __call__(self, params: StdioServerParameters) -> "_ProbeContext":
        self.params = params
        self.calls += 1
        return _ProbeContext(self)


class _ProbeContext:
    def __init__(self, probe: _Probe) -> None:
        self._probe = probe

    async def __aenter__(self) -> _Session:
        if self._probe.on_enter is not None:
            self._probe.on_enter()
        return self._probe.session

    async def __aexit__(self, *exc_info: object) -> None:
        self._probe.exited = True


@pytest.fixture
def clean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """An isolated cwd with no default YAML and no inherited authority/config path."""
    monkeypatch.delenv(AUTH, raising=False)
    monkeypatch.delenv(CFG, raising=False)
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


def _capture(
    *,
    source: AudioSourceKind = MIC,
    detector_selector: str | None = "det",
    wav_path: str | None = None,
    rate: int = 16000,
    window: float = 10.0,
    overlap: float = 0.7,
    hop: float | None = None,
    additional: tuple[str, ...] = (),
    additional_rate: int | None = None,
) -> RequestedCapture:
    return RequestedCapture(
        source=source,
        detector_selector=detector_selector,
        wav_path=wav_path,
        detector_sample_rate_hz=rate,
        window_seconds=window,
        overlap=overlap,
        hop_seconds=hop,
        additional_selectors=additional,
        additional_sample_rate_hz=additional_rate,
    )


def _process(
    config_env: dict[str, str] | None = None,
    *,
    probe: _Probe | None = None,
    capture: RequestedCapture | None = None,
    device_config: MCPDeviceConfig | None = None,
) -> MCPServerProcess:
    return MCPServerProcess(
        MCPConfig(env=dict(config_env or {})),
        device_config=device_config,
        session_factory=probe if probe is not None else _Probe(),
        human_capture=capture,
    )


def _pending_evidence(process: MCPServerProcess) -> LaunchEvidence:
    pending = process._pending_launch
    assert pending is not None
    return pending.evidence


async def _refused(process: MCPServerProcess) -> LaunchCompositionError:
    with pytest.raises(MCPConnectionError) as raised:
        await process.start()
    assert type(raised.value) is LaunchCompositionError
    return raised.value


def _assert_not_spawned(process: MCPServerProcess, probe: _Probe, epoch: int = 0) -> None:
    assert probe.params is None and probe.calls == 0
    assert process.child_epoch == epoch
    assert process.running is False
    assert process.stop_unconfirmed is False
    assert all(process.launch_record_for_epoch(e) is None for e in range(epoch + 3))
    assert process._launch_record is None and process._pending_launch is None


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# =========================================================================== AUTOMATIC


@pytest.mark.parametrize("inherited", [None, "automatic", " Automatic "])
def test_ta1_env_none_is_preserved_and_inherited_key_logged_by_class(
    clean: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    inherited: str | None,
) -> None:
    """TA1/G3: empty config env keeps the SDK default; a present key logs key + class only."""
    if inherited is not None:
        monkeypatch.setenv(AUTH, inherited)
    process = _process()
    with caplog.at_level(logging.DEBUG, logger="roastpilot_agent"):
        params = process.build_server_parameters()
    assert params.env is None
    evidence = _pending_evidence(process)
    assert evidence.env_base is ChildEnvBase.SDK_DEFAULT
    assert evidence.config_selection is ConfigSelectionKind.CWD_DEFAULT
    assert evidence.config_present is False
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    if inherited is None:
        assert warnings == []
        assert evidence.inherited_authority is AuthorityClass.ABSENT
    else:
        assert evidence.inherited_authority is AuthorityClass.AUTOMATIC
        assert len(warnings) == 1
        assert AUTH in warnings[0] and "AUTOMATIC" in warnings[0]
        assert inherited not in warnings[0] and "automatic" not in warnings[0]


def test_ta2_merge_withholds_only_the_inherited_authority(
    clean: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TA2/G2: merged env equals the legacy merge minus only the inherited authority."""
    monkeypatch.setenv(AUTH, "automatic")
    monkeypatch.setenv("C1_UNRELATED_SENTINEL", "keep-me")
    monkeypatch.setenv("COFFEE_FIRST_CRACK_MODE", "audio")
    config_env = {"COFFEE_ROASTER_DRIVER": "mock"}
    params = _process(config_env).build_server_parameters()
    assert params.env == {k: v for k, v in os.environ.items() if k != AUTH} | config_env
    assert params.env is not None and AUTH not in params.env
    assert params.env["C1_UNRELATED_SENTINEL"] == "keep-me"
    assert params.env["COFFEE_FIRST_CRACK_MODE"] == "audio"


@pytest.mark.parametrize("value", ["manual", " Manual ", "MANUAL", "garbage", ""])
@pytest.mark.parametrize("config_env", [{}, {"COFFEE_ROASTER_DRIVER": "mock"}])
@pytest.mark.asyncio
async def test_ta3_inherited_non_automatic_is_refused_before_spawn(
    clean: Path, monkeypatch: pytest.MonkeyPatch, value: str, config_env: dict[str, str]
) -> None:
    """TA3/G5: inherited non-automatic authority is refused under both env bases."""
    monkeypatch.setenv(AUTH, value)
    probe = _Probe()
    process = _process(config_env, probe=probe)
    exc = await _refused(process)
    assert exc.refusal is LaunchRefusal.INHERITED_AUTHORITY_INCOMPATIBLE
    assert exc.key == AUTH
    _assert_not_spawned(process, probe)


@pytest.mark.parametrize("value", ["automatic", " Automatic "])
def test_ta4_explicit_automatic_is_passed_verbatim(clean: Path, value: str) -> None:
    """TA4/G11: an explicit agreeing value reaches the child byte-identical."""
    process = _process({AUTH: value})
    params = process.build_server_parameters()
    assert params.env is not None and params.env[AUTH] == value
    assert _pending_evidence(process).override_authority is AuthorityClass.AUTOMATIC


@pytest.mark.parametrize("value", ["manual", "MANUAL", "auto", "", "true"])
@pytest.mark.asyncio
async def test_ta5_explicit_non_automatic_is_refused_before_spawn(clean: Path, value: str) -> None:
    """TA5/G4/G18/G26: refusal is a typed MCPConnectionError, not re-wrapped by start()."""
    probe = _Probe()
    process = _process({AUTH: value}, probe=probe)
    exc = await _refused(process)
    assert isinstance(exc, MCPConnectionError)
    assert exc.refusal is LaunchRefusal.EXPLICIT_AUTHORITY_INCOMPATIBLE
    assert str(exc) == f"EXPLICIT_AUTHORITY_INCOMPATIBLE: {AUTH}"
    _assert_not_spawned(process, probe)


def _receipt_yaml(directory: Path, driver: str) -> Path:
    return _write(
        directory / "receipt.yaml",
        f"first_crack:\n  event_authority: manual\nroaster:\n  driver: {driver}\n",
    )


@pytest.mark.parametrize("driver", ["mock", "hottop_kn8828b_2k_plus"])
@pytest.mark.parametrize("route", ["device_source", "config_env", "os_environ", "cwd_default"])
@pytest.mark.asyncio
async def test_ta6_receipt_counterexamples_are_refused_on_every_route(
    clean: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, route: str, driver: str
) -> None:
    """TA6/AC8/G6/G10: manual YAML never becomes an admissible AUTOMATIC child."""
    probe = _Probe()
    config_env: dict[str, str] = {}
    device_config: MCPDeviceConfig | None = None
    if route == "cwd_default":
        _write(
            clean / lc.DEFAULT_CHILD_CONFIG_FILENAME,
            MANUAL_YAML + f"roaster:\n  driver: {driver}\n",
        )
    else:
        source = _receipt_yaml(tmp_path, driver)
        if route == "device_source":
            device_config = MCPDeviceConfig(mcp_yaml_source_path=source)
        elif route == "config_env":
            config_env = {CFG: str(source)}
        else:
            monkeypatch.setenv(CFG, str(source))
            config_env = {"C1_UNRELATED": "x"}
    process = _process(config_env, probe=probe, device_config=device_config)
    exc = await _refused(process)
    assert exc.refusal is LaunchRefusal.CONFIG_AUTHORITY_INCOMPATIBLE
    _assert_not_spawned(process, probe)
    assert process._rendered_yaml_dir is None


@pytest.mark.parametrize(
    ("text", "authority"),
    [
        ("first_crack:\n  event_authority: ' AUTOMATIC '\n", AuthorityClass.AUTOMATIC),
        ("first_crack:\n  mode: audio\n", AuthorityClass.ABSENT),
        ("roaster:\n  driver: mock\n", AuthorityClass.ABSENT),
        ("", AuthorityClass.ABSENT),
        ("# only a comment\n# another\n", AuthorityClass.ABSENT),
    ],
)
def test_ta7_accepted_automatic_yaml(clean: Path, text: str, authority: AuthorityClass) -> None:
    """TA7/G11: agreeing or absent YAML authority is accepted unchanged."""
    _write(clean / lc.DEFAULT_CHILD_CONFIG_FILENAME, text)
    process = _process()
    assert process.build_server_parameters().env is None
    evidence = _pending_evidence(process)
    assert evidence.config_present is True
    assert evidence.config_authority is authority


_INVALID_DOCUMENTS = [
    "first_crack:\n  event_authority: 1\n",
    "first_crack:\n  event_authority: null\n",
    "first_crack:\n  event_authority: garbage\n",
    "first_crack: null\n",
    "first_crack: []\n",
    "roaster: null\n",
    "first_crack:\n  mode: garbage\n",
    "roaster:\n  driver: 1\n",
    "- a\n- b\n",
    "42\n",
]


@pytest.mark.parametrize("text", _INVALID_DOCUMENTS)
@pytest.mark.asyncio
async def test_ta8_structurally_invalid_config_is_refused(clean: Path, text: str) -> None:
    """TA8/G9: structural/type refusals mirror the producer loader, before spawn."""
    _write(clean / lc.DEFAULT_CHILD_CONFIG_FILENAME, text)
    probe = _Probe()
    process = _process(probe=probe)
    exc = await _refused(process)
    assert exc.refusal is LaunchRefusal.CONFIG_SOURCE_INVALID
    _assert_not_spawned(process, probe)


@pytest.mark.parametrize("case", ["syntax", "non_utf8", "directory", "missing_env_path"])
@pytest.mark.asyncio
async def test_ta8_unreadable_config_is_refused(clean: Path, tmp_path: Path, case: str) -> None:
    """TA8/G7/G8: parse/decode/type failures and a missing env path are unreadable."""
    config_env: dict[str, str] = {}
    default = clean / lc.DEFAULT_CHILD_CONFIG_FILENAME
    if case == "syntax":
        _write(default, "first_crack: [\n")
    elif case == "non_utf8":
        default.write_bytes(b"first_crack:\n  mode: \xff\xfe\n")
    elif case == "directory":
        default.mkdir()
    else:
        config_env = {CFG: str(tmp_path / "missing.yaml")}
    probe = _Probe()
    process = _process(config_env, probe=probe)
    exc = await _refused(process)
    assert exc.refusal is LaunchRefusal.CONFIG_SOURCE_UNREADABLE
    assert str(exc) == f"CONFIG_SOURCE_UNREADABLE: {CFG}"
    _assert_not_spawned(process, probe)


def test_ta8_whitespace_env_path_falls_back_to_missing_cwd_default(clean: Path) -> None:
    """TA8/G10: a whitespace-only env path selects the (missing) cwd default; accepted."""
    process = _process({CFG: "   "})
    params = process.build_server_parameters()
    assert params.env is not None and params.env[CFG] == "   "
    evidence = _pending_evidence(process)
    assert evidence.config_selection is ConfigSelectionKind.CWD_DEFAULT
    assert evidence.config_present is False


def test_ta8_symlink_to_regular_file_is_admitted(clean: Path, tmp_path: Path) -> None:
    """TA8/G36: an ordinary symlink to a regular file is resolved and read."""
    target = _write(tmp_path / "real.yaml", "first_crack:\n  event_authority: automatic\n")
    link = tmp_path / "link.yaml"
    link.symlink_to(target)
    process = _process({CFG: str(link)})
    process.build_server_parameters()
    evidence = _pending_evidence(process)
    assert evidence.config_selection is ConfigSelectionKind.ENV_PATH
    assert evidence.config_authority is AuthorityClass.AUTOMATIC


def _comment_file(path: Path, size: int) -> Path:
    path.write_bytes(b"#" + b"x" * (size - 2) + b"\n")
    assert path.stat().st_size == size
    return path


def test_ta8_exact_byte_limit_accepted_and_one_more_refused(tmp_path: Path) -> None:
    """TA8/G36: a file of exactly the limit is read; limit+1 is unreadable."""
    limit = mcp_client._MAX_LAUNCH_CONFIG_BYTES
    assert limit == 131_072
    exact = _comment_file(tmp_path / "exact.yaml", limit)
    over = _comment_file(tmp_path / "over.yaml", limit + 1)
    select = ConfigSelectionKind.ENV_PATH
    assert mcp_client._read_child_config(ChildConfigSelection(select, str(exact))) == (
        ChildConfigRead(ConfigReadStatus.LOADED, None)
    )
    assert mcp_client._read_child_config(ChildConfigSelection(select, str(over))) == (
        ChildConfigRead(ConfigReadStatus.UNREADABLE)
    )


class _Ops:
    """Recording wrapper over the real file ops with injectable metadata."""

    def __init__(self) -> None:
        self.opened: list[int] = []
        self.closed: list[int] = []
        self.lstat_mode: int | None = None
        self.fstat_ino_delta = 0
        self.read_error: OSError | None = None

    def build(self, **overrides: object) -> mcp_client._ConfigFileOps:
        real = mcp_client._CONFIG_FILE_OPS

        def lstat(path: str) -> os.stat_result:
            result = real.lstat(path)
            if self.lstat_mode is None:
                return result
            values = list(result)
            values[0] = self.lstat_mode
            return os.stat_result(values)

        def open_(path: str, flags: int) -> int:
            fd = real.open(path, flags)
            self.opened.append(fd)
            return fd

        def fstat(fd: int) -> os.stat_result:
            result = real.fstat(fd)
            values = list(result)
            values[1] = values[1] + self.fstat_ino_delta
            return os.stat_result(values)

        def read(fd: int, size: int) -> bytes:
            if self.read_error is not None:
                raise self.read_error
            return real.read(fd, size)

        def close(fd: int) -> None:
            self.closed.append(fd)
            real.close(fd)

        ops = dataclasses.replace(
            real, lstat=lstat, open=open_, fstat=fstat, read=read, close=close
        )
        return dataclasses.replace(ops, **overrides)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    ("case", "status"),
    [
        ("success", ConfigReadStatus.LOADED),
        ("fifo_metadata", ConfigReadStatus.UNREADABLE),
        ("char_device_metadata", ConfigReadStatus.UNREADABLE),
        ("final_symlink_swap", ConfigReadStatus.UNREADABLE),
        ("identity_changed", ConfigReadStatus.UNREADABLE),
        ("read_error", ConfigReadStatus.UNREADABLE),
        ("oversized", ConfigReadStatus.UNREADABLE),
        ("no_nofollow", ConfigReadStatus.UNREADABLE),
        ("no_nonblock", ConfigReadStatus.UNREADABLE),
    ],
)
def test_ta8_descriptor_guards_and_close_in_finally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, status: ConfigReadStatus
) -> None:
    """TA8/G36: non-regular metadata refuses without open; every opened fd is closed."""
    path = _write(tmp_path / "c.yaml", "roaster:\n  driver: mock\n")
    recorder = _Ops()
    overrides: dict[str, object] = {}
    if case == "fifo_metadata":
        recorder.lstat_mode = stat.S_IFIFO | 0o600
    elif case == "char_device_metadata":
        recorder.lstat_mode = stat.S_IFCHR | 0o600
    elif case == "final_symlink_swap":
        recorder.lstat_mode = stat.S_IFLNK | 0o777
    elif case == "identity_changed":
        recorder.fstat_ino_delta = 1
    elif case == "read_error":
        recorder.read_error = OSError("simulated read failure")
    elif case == "oversized":
        _comment_file(path, mcp_client._MAX_LAUNCH_CONFIG_BYTES + 1)
    elif case == "no_nofollow":
        overrides["nofollow_flag"] = None
    elif case == "no_nonblock":
        overrides["nonblock_flag"] = None
    monkeypatch.setattr(mcp_client, "_CONFIG_FILE_OPS", recorder.build(**overrides))
    read = mcp_client._read_child_config(
        ChildConfigSelection(ConfigSelectionKind.ENV_PATH, str(path))
    )
    assert read.status is status
    if case in {"fifo_metadata", "char_device_metadata", "final_symlink_swap"} or case.startswith(
        "no_"
    ):
        assert recorder.opened == []
    else:
        assert len(recorder.opened) == 1
    assert recorder.closed == recorder.opened
    if status is ConfigReadStatus.LOADED:
        assert read.document == {"roaster": {"driver": "mock"}}


def test_ta8_open_flags_are_nofollow_nonblocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TA8/G36: the descriptor is opened read-only, no-follow and nonblocking."""
    path = _write(tmp_path / "c.yaml", "")
    seen: list[int] = []
    real = mcp_client._CONFIG_FILE_OPS

    def open_(p: str, flags: int) -> int:
        seen.append(flags)
        return real.open(p, flags)

    monkeypatch.setattr(mcp_client, "_CONFIG_FILE_OPS", dataclasses.replace(real, open=open_))
    mcp_client._read_child_config(ChildConfigSelection(ConfigSelectionKind.ENV_PATH, str(path)))
    assert len(seen) == 1
    assert seen[0] & os.O_NOFOLLOW and seen[0] & os.O_NONBLOCK
    assert seen[0] & (os.O_WRONLY | os.O_RDWR) == 0


@pytest.mark.parametrize("case", ["recursion", "non_utf8", "nul_path"])
def test_ta8_parser_and_decode_failures_are_unreadable(tmp_path: Path, case: str) -> None:
    """TA8/G8: recursion, decode and invalid-path errors become UNREADABLE."""
    path = tmp_path / "c.yaml"
    if case == "recursion":
        path.write_text("[" * 5000 + "]" * 5000, encoding="utf-8")
    elif case == "non_utf8":
        path.write_bytes(b"\xff\xfe\xfd")
    path_text = str(path) + ("\x00" if case == "nul_path" else "")
    read = mcp_client._read_child_config(
        ChildConfigSelection(ConfigSelectionKind.ENV_PATH, path_text)
    )
    assert read == ChildConfigRead(ConfigReadStatus.UNREADABLE)


def test_ta9_sdk_default_env_ignores_an_os_environ_config_path(
    clean: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TA9/G10: with env=None the child never sees os.environ's config path."""
    monkeypatch.setenv(CFG, str(_write(tmp_path / "manual.yaml", MANUAL_YAML)))
    process = _process()
    assert process.build_server_parameters().env is None
    assert _pending_evidence(process).config_selection is ConfigSelectionKind.CWD_DEFAULT


@pytest.mark.parametrize("carrier", ["explicit", "inherited"])
@pytest.mark.asyncio
async def test_ta10_conflicting_automatic_carrier_and_manual_yaml_refused(
    clean: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, carrier: str
) -> None:
    """TA10/DP-1/G6: an agreeing automatic carrier never outvotes manual YAML."""
    source = _write(tmp_path / "manual.yaml", MANUAL_YAML)
    config_env = {CFG: str(source)}
    if carrier == "explicit":
        config_env[AUTH] = "automatic"
    else:
        monkeypatch.setenv(AUTH, "automatic")
    probe = _Probe()
    process = _process(config_env, probe=probe)
    exc = await _refused(process)
    assert exc.refusal is LaunchRefusal.CONFIG_AUTHORITY_INCOMPATIBLE
    _assert_not_spawned(process, probe)


def test_ta11_refusal_after_render_removes_that_calls_temp_dir(
    clean: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TA11/G19: the render made by the refused call is removed and the pointer cleared."""
    created: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp

    def recording_mkdtemp(suffix: str | None = None, prefix: str | None = None) -> str:
        directory = real_mkdtemp(suffix, prefix)
        created.append(Path(directory))
        return directory

    monkeypatch.setattr(tempfile, "mkdtemp", recording_mkdtemp)
    source = _write(tmp_path / "manual.yaml", MANUAL_YAML)
    process = _process(device_config=MCPDeviceConfig(mcp_yaml_source_path=source))
    with pytest.raises(LaunchCompositionError) as raised:
        process.build_server_parameters()
    assert raised.value.refusal is LaunchRefusal.CONFIG_AUTHORITY_INCOMPATIBLE
    assert len(created) == 1
    assert not created[0].exists()
    assert process._rendered_yaml_dir is None
    assert process._pending_launch is None


def test_ta11_non_refusal_error_after_render_also_cleans_up(
    clean: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TA11/G19: any exception after this call's render removes it and propagates unchanged."""

    class _Boom(Exception):
        pass

    def boom(command: str) -> str:
        raise _Boom

    monkeypatch.setattr(mcp_client, "resolve_mcp_command", boom)
    source = _write(tmp_path / "ok.yaml", "roaster:\n  driver: mock\n")
    process = _process(device_config=MCPDeviceConfig(mcp_yaml_source_path=source))
    rendered: list[Path] = []
    real_rmtree = shutil.rmtree

    def recording_rmtree(path: Path, ignore_errors: bool = False) -> None:
        rendered.append(Path(path))
        real_rmtree(path, ignore_errors=ignore_errors)

    monkeypatch.setattr(shutil, "rmtree", recording_rmtree)
    with pytest.raises(_Boom):
        process.build_server_parameters()
    assert len(rendered) == 1 and not rendered[0].exists()
    assert rendered[0].name.startswith("rp-mcp-yaml-")
    assert process._rendered_yaml_dir is None
    assert process._pending_launch is None


@pytest.mark.asyncio
async def test_ta12_live_build_fails_closed_on_composition_refusal(
    clean: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TA12/G26: build_live_service surfaces the refusal after stopping the child."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    probe = _Probe()
    stops: list[MCPServerProcess] = []

    class _Tracked(MCPServerProcess):
        async def stop(self) -> None:
            stops.append(self)
            await super().stop()

    def factory(config: MCPConfig, **kwargs: object) -> MCPServerProcess:
        return _Tracked(
            config,
            device_config=cast("MCPDeviceConfig | None", kwargs.get("device_config")),
            session_factory=probe,
        )

    monkeypatch.setattr(live, "MCPServerProcess", factory)
    config = AppConfig(mcp=MCPConfig(env={AUTH: "manual"}))
    with pytest.raises(LaunchCompositionError) as raised:
        await live.build_live_service(config, store_path=tmp_path / "live.sqlite3")
    assert raised.value.refusal is LaunchRefusal.EXPLICIT_AUTHORITY_INCOMPATIBLE
    assert len(stops) == 1
    assert probe.calls == 0


@pytest.mark.asyncio
async def test_ta13_same_instance_across_refusal_recovery_and_respawn(clean: Path) -> None:
    """TA13: a refused start is retryable; a later refusal leaves no stale record."""
    default = clean / lc.DEFAULT_CHILD_CONFIG_FILENAME
    _write(default, MANUAL_YAML)
    probe = _Probe()
    process = _process(probe=probe)
    await _refused(process)
    assert process.child_epoch == 0 and probe.calls == 0

    _write(default, "first_crack:\n  event_authority: automatic\n")
    await process.start()
    epoch = process.child_epoch
    record = process.launch_record_for_epoch(epoch)
    assert epoch == 1 and record is not None and record.child_epoch == 1
    assert record.evidence.config_authority is AuthorityClass.AUTOMATIC

    await process.stop()
    stopped_epoch = process.child_epoch
    _write(default, MANUAL_YAML)
    await _refused(process)
    assert process.running is False
    assert process.child_epoch == stopped_epoch
    assert probe.calls == 1
    assert all(process.launch_record_for_epoch(e) is None for e in range(stopped_epoch + 2))
    assert process._launch_record is None


# =========================================================================== HUMAN

_PIN_NAMES = frozenset(
    {
        "COFFEE_FIRST_CRACK_EVENT_AUTHORITY",
        "COFFEE_ROASTER_DRIVER",
        "COFFEE_FIRST_CRACK_MODE",
        "COFFEE_AUDIO_SOURCE",
        "COFFEE_AUDIO_INPUT_DEVICE",
        "COFFEE_AUDIO_WAV_PATH",
        "COFFEE_AUDIO_REPLAY_MODE",
        "COFFEE_AUDIO_SAMPLE_RATE",
        "COFFEE_AUDIO_WINDOW_SECONDS",
        "COFFEE_AUDIO_OVERLAP",
        "COFFEE_AUDIO_HOP_SECONDS",
        "COFFEE_RECORDING_ENABLED",
        "COFFEE_RECORDING_AUTOCAPTURE",
        "COFFEE_RECORDING_SAMPLE_RATE",
        "COFFEE_RECORDING_DEVICES",
    }
)

_COMMON_PINS = {
    "COFFEE_FIRST_CRACK_EVENT_AUTHORITY": "manual",
    "COFFEE_ROASTER_DRIVER": "mock",
    "COFFEE_FIRST_CRACK_MODE": "audio",
    "COFFEE_AUDIO_REPLAY_MODE": "realtime",
    "COFFEE_RECORDING_ENABLED": "true",
    "COFFEE_RECORDING_AUTOCAPTURE": "true",
}

_PIN_CASES: dict[str, tuple[RequestedCapture, dict[str, str]]] = {
    "single_named": (
        _capture(),
        {
            "COFFEE_AUDIO_SOURCE": "microphone",
            "COFFEE_AUDIO_INPUT_DEVICE": "det",
            "COFFEE_AUDIO_WAV_PATH": "",
            "COFFEE_AUDIO_SAMPLE_RATE": "16000",
            "COFFEE_AUDIO_WINDOW_SECONDS": "10.0",
            "COFFEE_AUDIO_OVERLAP": "0.7",
            "COFFEE_AUDIO_HOP_SECONDS": "",
            "COFFEE_RECORDING_SAMPLE_RATE": "",
            "COFFEE_RECORDING_DEVICES": "det",
        },
    ),
    "single_default": (
        _capture(detector_selector=None, hop=0.5),
        {
            "COFFEE_AUDIO_SOURCE": "microphone",
            "COFFEE_AUDIO_INPUT_DEVICE": "",
            "COFFEE_AUDIO_WAV_PATH": "",
            "COFFEE_AUDIO_SAMPLE_RATE": "16000",
            "COFFEE_AUDIO_WINDOW_SECONDS": "10.0",
            "COFFEE_AUDIO_OVERLAP": "0.7",
            "COFFEE_AUDIO_HOP_SECONDS": "0.5",
            "COFFEE_RECORDING_SAMPLE_RATE": "",
            "COFFEE_RECORDING_DEVICES": "",
        },
    ),
    "paired_explicit_rate": (
        _capture(additional=("a1", "a2"), additional_rate=48000),
        {
            "COFFEE_AUDIO_SOURCE": "microphone",
            "COFFEE_AUDIO_INPUT_DEVICE": "det",
            "COFFEE_AUDIO_WAV_PATH": "",
            "COFFEE_AUDIO_SAMPLE_RATE": "16000",
            "COFFEE_AUDIO_WINDOW_SECONDS": "10.0",
            "COFFEE_AUDIO_OVERLAP": "0.7",
            "COFFEE_AUDIO_HOP_SECONDS": "",
            "COFFEE_RECORDING_SAMPLE_RATE": "48000",
            "COFFEE_RECORDING_DEVICES": "det,a1,a2",
        },
    ),
    "paired_derived_rate": (
        _capture(additional=("a1",)),
        {
            "COFFEE_AUDIO_SOURCE": "microphone",
            "COFFEE_AUDIO_INPUT_DEVICE": "det",
            "COFFEE_AUDIO_WAV_PATH": "",
            "COFFEE_AUDIO_SAMPLE_RATE": "16000",
            "COFFEE_AUDIO_WINDOW_SECONDS": "10.0",
            "COFFEE_AUDIO_OVERLAP": "0.7",
            "COFFEE_AUDIO_HOP_SECONDS": "",
            "COFFEE_RECORDING_SAMPLE_RATE": "",
            "COFFEE_RECORDING_DEVICES": "det,a1",
        },
    ),
    "wav": (
        _capture(source=WAV, detector_selector=None, wav_path="/data/roast.wav", rate=22050),
        {
            "COFFEE_AUDIO_SOURCE": "wav",
            "COFFEE_AUDIO_INPUT_DEVICE": "",
            "COFFEE_AUDIO_WAV_PATH": "/data/roast.wav",
            "COFFEE_AUDIO_SAMPLE_RATE": "22050",
            "COFFEE_AUDIO_WINDOW_SECONDS": "10.0",
            "COFFEE_AUDIO_OVERLAP": "0.7",
            "COFFEE_AUDIO_HOP_SECONDS": "",
            "COFFEE_RECORDING_SAMPLE_RATE": "",
            "COFFEE_RECORDING_DEVICES": "",
        },
    ),
}


def test_th1_pin_key_set_is_exactly_the_fifteen_names() -> None:
    """TH1: the pin set is compared as a set, never as a count."""
    assert lc.HUMAN_PIN_KEYS == _PIN_NAMES
    authority_keys = frozenset({"COFFEE_FIRST_CRACK_EVENT_AUTHORITY"})
    assert authority_keys == lc.HUMAN_AUTHORITY_ENV_KEYS


@pytest.mark.parametrize("case", sorted(_PIN_CASES))
def test_th1_exact_pins_per_capture_shape(clean: Path, case: str) -> None:
    """TH1/G14: each shape's pins reach the child env with the literal expected values."""
    capture, specific = _PIN_CASES[case]
    process = _process(capture=capture)
    env = process.build_server_parameters().env
    assert env is not None
    expected = _COMMON_PINS | specific
    assert {k: env[k] for k in lc.HUMAN_PIN_KEYS} == expected
    evidence = _pending_evidence(process)
    assert evidence.authority is ControlAuthority.HUMAN
    assert dict(evidence.pinned_env) == expected
    assert [k for k, _ in evidence.pinned_env] == sorted(expected)
    assert lc.render_human_pins(capture) == evidence.pinned_env


@pytest.mark.parametrize("value", [0.1, 1 / 3, 1e-07, 0.9999999999999999])
def test_th1_float_pins_round_trip(value: float) -> None:
    """TH1: rendered floats parse back to the identical float."""
    as_window = dict(lc.render_human_pins(_capture(window=value, overlap=0.0)))
    as_overlap = dict(lc.render_human_pins(_capture(overlap=value)))
    as_hop = dict(lc.render_human_pins(_capture(window=1.0, overlap=0.0, hop=value)))
    assert float(as_window["COFFEE_AUDIO_WINDOW_SECONDS"]) == value
    assert float(as_overlap["COFFEE_AUDIO_OVERLAP"]) == value
    assert float(as_hop["COFFEE_AUDIO_HOP_SECONDS"]) == value


def test_th2_pins_override_inherited_and_yaml_capture(
    clean: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TH2/G14: pins win over inherited values; YAML capture is overridden by the loader."""
    monkeypatch.setenv("COFFEE_AUDIO_SAMPLE_RATE", "48000")
    monkeypatch.setenv("COFFEE_RECORDING_DEVICES", "x,y")
    _write(
        clean / lc.DEFAULT_CHILD_CONFIG_FILENAME,
        "audio:\n  sample_rate: 44100\nrecording:\n  devices: [p, q]\n  sample_rate: 8000\n",
    )
    process = _process(capture=_capture())
    env = process.build_server_parameters().env
    assert env is not None
    assert env["COFFEE_AUDIO_SAMPLE_RATE"] == "16000"
    assert env["COFFEE_RECORDING_DEVICES"] == "det"
    assert env["COFFEE_RECORDING_SAMPLE_RATE"] == ""
    assert _pending_evidence(process).config_present is True


_PIN_CONFLICTS = [(key, "differs-c1") for key in sorted(_PIN_NAMES)] + [
    ("COFFEE_AUDIO_SAMPLE_RATE", "16000.0"),
    ("COFFEE_RECORDING_ENABLED", "1"),
    ("COFFEE_FIRST_CRACK_EVENT_AUTHORITY", " manual"),
]


@pytest.mark.parametrize(("key", "value"), _PIN_CONFLICTS)
@pytest.mark.asyncio
async def test_th3_explicit_pin_conflict_is_refused_before_spawn(
    clean: Path, key: str, value: str
) -> None:
    """TH3/G12: an explicit value that is not byte-equal to the pin names that key."""
    probe = _Probe()
    process = _process({key: value}, probe=probe, capture=_capture())
    exc = await _refused(process)
    assert exc.refusal is LaunchRefusal.EXPLICIT_PIN_CONFLICT
    assert exc.key == key
    assert str(exc) == f"EXPLICIT_PIN_CONFLICT: {key}"
    _assert_not_spawned(process, probe)


def test_th3_byte_equal_explicit_pins_are_accepted(clean: Path) -> None:
    """TH3: explicit values identical to every pin are accepted."""
    capture = _capture()
    explicit = dict(lc.render_human_pins(capture))
    env = _process(explicit, capture=capture).build_server_parameters().env
    assert env is not None and {k: env[k] for k in explicit} == explicit


@pytest.mark.parametrize(
    "text",
    [
        "first_crack:\n  event_authority: automatic\n",
        "roaster:\n  driver: hottop_kn8828b_2k_plus\n",
        "roaster:\n  driver: ' mock'\n",
        "first_crack:\n  mode: disabled\n",
        "first_crack:\n  mode: manual\n",
    ],
)
@pytest.mark.asyncio
async def test_th4_config_identity_conflict_is_refused(clean: Path, text: str) -> None:
    """TH4/G13: HUMAN refuses non-manual authority, non-mock driver or non-audio mode."""
    _write(clean / lc.DEFAULT_CHILD_CONFIG_FILENAME, text)
    probe = _Probe()
    process = _process(probe=probe, capture=_capture())
    exc = await _refused(process)
    assert exc.refusal is LaunchRefusal.CONFIG_IDENTITY_CONFLICT
    _assert_not_spawned(process, probe)


@pytest.mark.parametrize(
    "text",
    [
        "first_crack:\n  event_authority: ' Manual '\n",
        "roaster:\n  driver: mock\n",
        "first_crack:\n  mode: ' AUDIO '\n",
        "first_crack: {}\nroaster: {}\n",
    ],
)
def test_th4_agreeing_config_identity_is_accepted(clean: Path, text: str) -> None:
    """TH4/G11: strip/lower authority and mode; raw driver equality."""
    _write(clean / lc.DEFAULT_CHILD_CONFIG_FILENAME, text)
    process = _process(capture=_capture())
    assert process.build_server_parameters().env is not None
    assert _pending_evidence(process).config_present is True


_VALID = _capture()


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"source": "microphone"}, TypeError),
        ({"detector_sample_rate_hz": 0}, ValueError),
        ({"detector_sample_rate_hz": -1}, ValueError),
        ({"detector_sample_rate_hz": True}, TypeError),
        ({"detector_sample_rate_hz": 16000.0}, TypeError),
        ({"detector_sample_rate_hz": None}, TypeError),
        ({"additional_sample_rate_hz": 48000}, ValueError),
        ({"additional_selectors": ("a1",), "additional_sample_rate_hz": 0}, ValueError),
        ({"additional_selectors": ("a1",), "additional_sample_rate_hz": True}, TypeError),
        ({"window_seconds": 0.0}, ValueError),
        ({"window_seconds": -1.0}, ValueError),
        ({"window_seconds": float("inf")}, ValueError),
        ({"window_seconds": float("nan")}, ValueError),
        ({"window_seconds": 1}, TypeError),
        ({"overlap": -0.1}, ValueError),
        ({"overlap": 1.0}, ValueError),
        ({"overlap": float("nan")}, ValueError),
        # Isolated rows: an explicit hop keeps the effective-hop clause satisfied,
        # so only the window/overlap clause itself can refuse.
        ({"overlap": 1.0, "hop_seconds": 1.0}, ValueError),
        ({"overlap": float("nan"), "hop_seconds": 1.0}, ValueError),
        ({"window_seconds": float("inf"), "hop_seconds": 1.0}, ValueError),
        ({"overlap": 0}, TypeError),
        ({"hop_seconds": 0.0}, ValueError),
        ({"hop_seconds": 10.5}, ValueError),
        ({"hop_seconds": float("inf")}, ValueError),
        ({"hop_seconds": float("nan")}, ValueError),
        ({"hop_seconds": 1}, TypeError),
        ({"window_seconds": 5e-324, "overlap": 0.5}, ValueError),
        ({"source": WAV, "detector_selector": None}, ValueError),
        ({"source": WAV, "wav_path": "/w.wav"}, ValueError),
        (
            {
                "source": WAV,
                "detector_selector": None,
                "wav_path": "/w.wav",
                "additional_selectors": ("a1",),
            },
            ValueError,
        ),
        ({"wav_path": "/w.wav"}, ValueError),
        ({"detector_selector": None, "additional_selectors": ("a1",)}, ValueError),
        ({"detector_selector": "a,b"}, ValueError),
        ({"detector_selector": " a"}, ValueError),
        ({"detector_selector": "a "}, ValueError),
        ({"detector_selector": ""}, ValueError),
        ({"detector_selector": "a\x00"}, ValueError),
        ({"detector_selector": 1}, TypeError),
        ({"source": WAV, "detector_selector": None, "wav_path": " p"}, ValueError),
        ({"source": WAV, "detector_selector": None, "wav_path": ""}, ValueError),
        ({"source": WAV, "detector_selector": None, "wav_path": "p\x00"}, ValueError),
        ({"additional_selectors": ["a1"]}, TypeError),
        ({"additional_selectors": ("a1", "a1")}, ValueError),
        ({"additional_selectors": ("det",)}, ValueError),
        ({"additional_selectors": ("x,y",)}, ValueError),
        ({"additional_selectors": tuple(f"a{i}" for i in range(8))}, ValueError),
    ],
)
def test_th5_requested_capture_rejects_every_clause(
    changes: dict[str, object], error: type[Exception]
) -> None:
    """TH5/G16: one row per refused clause; wrong types are TypeError."""
    with pytest.raises(error):
        dataclasses.replace(_VALID, **changes)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    "capture",
    [
        _capture(),
        _capture(detector_selector=None),
        _capture(source=WAV, detector_selector=None, wav_path="p"),
        _capture(additional=tuple(f"a{i}" for i in range(7))),
        _capture(additional=("a1",), additional_rate=48000),
        _capture(window=1.0, overlap=0.0, hop=1.0),
        _capture(window=1.0, overlap=0.0),
    ],
)
def test_th5_valid_minimal_cases_are_accepted(capture: RequestedCapture) -> None:
    """TH5: each valid minimal shape constructs, including the 8-stream maximum."""
    assert type(capture) is RequestedCapture
    assert 1 + len(capture.additional_selectors) <= lc.MAX_CAPTURE_STREAMS


def test_th6_topology_matches_producer_wiring() -> None:
    """TH6/G17: labels, sources and rates follow the producer's recorder construction."""
    named = _capture().topology()
    assert named.recorder_primary == RecorderStream("det", RecorderStreamSource.DETECTOR_TEE, 16000)
    assert named.recorder_additional == ()
    assert named.detector.selector == "det" and named.detector.source is MIC

    default = _capture(detector_selector=None).topology()
    assert default.recorder_primary.label is None

    wav = _capture(source=WAV, detector_selector=None, wav_path="p", rate=22050).topology()
    assert wav.recorder_primary == RecorderStream(None, RecorderStreamSource.DETECTOR_TEE, 22050)
    assert wav.detector.source is WAV and wav.detector.wav_path == "p"

    paired = _capture(additional=("a1", "a2"), additional_rate=48000).topology()
    assert paired.recorder_primary.label == "det"
    assert paired.recorder_additional == (
        RecorderStream("a1", RecorderStreamSource.OPENED_SELECTOR, 48000),
        RecorderStream("a2", RecorderStreamSource.OPENED_SELECTOR, 48000),
    )
    derived = _capture(additional=("a1",)).topology()
    assert derived.recorder_additional[0].sample_rate_hz == 16000

    explicit = _capture(window=2.0, overlap=0.5, hop=0.25).topology().detector
    assert explicit.hop_source is HopSource.EXPLICIT
    assert explicit.explicit_hop_seconds == 0.25 and explicit.effective_hop_seconds == 0.25
    derived_hop = _capture(window=2.0, overlap=0.5).topology().detector
    assert derived_hop.hop_source is HopSource.DERIVED_FROM_OVERLAP
    assert derived_hop.explicit_hop_seconds is None
    assert derived_hop.effective_hop_seconds == 2.0 * (1 - 0.5)


def test_th7_pin_self_check_refuses_an_unconsumed_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """TH7/G15: if the merge let the base win, the pin self-check refuses."""

    def base_last(
        *,
        base: dict[str, str],
        explicit: dict[str, str],
        pins: tuple[tuple[str, str], ...],
    ) -> dict[str, str]:
        merged = dict(explicit)
        merged.update(pins)
        merged.update(base)
        return merged

    monkeypatch.setattr(lc, "_merge_child_env", base_last)
    with pytest.raises(LaunchRefusalError) as raised:
        lc.prepare_child_launch(
            explicit_env={},
            inherited_env={"COFFEE_AUDIO_SAMPLE_RATE": "48000"},
            capture=_capture(),
        )
    assert raised.value.refusal is LaunchRefusal.PIN_NOT_CONSUMED
    assert raised.value.key == "COFFEE_AUDIO_SAMPLE_RATE"


@dataclasses.dataclass(frozen=True, slots=True)
class _CaptureSubclass(RequestedCapture):
    pass


class _DuckCapture:
    source = MIC
    detector_selector = "det"


@pytest.mark.parametrize("bad", ["subclass", "duck"])
def test_th8_exact_capture_type(bad: str) -> None:
    """TH8/G24: only an exact RequestedCapture selects HUMAN."""
    if bad == "subclass":
        value: object = _CaptureSubclass(MIC, "det", None, 16000, 10.0, 0.7, None)
    else:
        value = _DuckCapture()
    with pytest.raises(TypeError):
        MCPServerProcess(human_capture=cast("RequestedCapture", value))
    with pytest.raises(TypeError):
        lc.prepare_child_launch(
            explicit_env={}, inherited_env={}, capture=cast("RequestedCapture", value)
        )
    with pytest.raises(TypeError):
        lc.render_human_pins(cast("RequestedCapture", value))


@pytest.mark.parametrize(
    ("inherited", "refused"), [("manual", False), ("automatic", True), ("garbage", True)]
)
def test_th9_inherited_authority_in_human(
    clean: Path, monkeypatch: pytest.MonkeyPatch, inherited: str, refused: bool
) -> None:
    """TH9/G5: inherited manual is withheld and accepted; anything else is refused."""
    monkeypatch.setenv(AUTH, inherited)
    process = _process(capture=_capture())
    if refused:
        with pytest.raises(LaunchCompositionError) as raised:
            process.build_server_parameters()
        assert raised.value.refusal is LaunchRefusal.INHERITED_AUTHORITY_INCOMPATIBLE
        return
    env = process.build_server_parameters().env
    assert env is not None and env[AUTH] == "manual"
    assert _pending_evidence(process).inherited_authority is AuthorityClass.MANUAL


def test_th10_human_env_is_never_none(clean: Path) -> None:
    """TH10: HUMAN always merges, even with an empty config env."""
    process = _process(capture=_capture())
    assert process.build_server_parameters().env is not None
    assert _pending_evidence(process).env_base is ChildEnvBase.AGENT_ENVIRONMENT_MERGE


# =========================================================================== records


class _Spy(MCPServerProcess):
    """Counts the public builder and the base composition; records returned params."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # pyright: ignore[reportArgumentType]
        self.builder_calls = 0
        self.compose_calls = 0
        self.returned: list[StdioServerParameters] = []

    def build_server_parameters(self) -> StdioServerParameters:
        self.builder_calls += 1
        params = super().build_server_parameters()
        self.returned.append(params)
        return params

    def _compose_launch(self) -> tuple[StdioServerParameters, LaunchEvidence]:
        self.compose_calls += 1
        return super()._compose_launch()


@pytest.mark.parametrize("capture", [None, _capture()])
@pytest.mark.asyncio
async def test_tr1_one_composition_is_consumed_and_a_preview_is_not(
    clean: Path, capture: RequestedCapture | None
) -> None:
    """TR1/G22/G32: one builder + one composition per start; the record binds that object."""
    probe = _Probe()
    process = _Spy(session_factory=probe, human_capture=capture)
    preview = process.build_server_parameters()
    assert process.launch_record_for_epoch(process.child_epoch) is None
    await process.start()
    assert process.builder_calls == 2 and process.compose_calls == 2
    consumed = process.returned[-1]
    assert probe.params is consumed and probe.params is not preview
    record = process.launch_record_for_epoch(process.child_epoch)
    assert record is not None and record.child_epoch == process.child_epoch
    assert consumed.env is not None or capture is None
    for key, value in record.evidence.pinned_env:
        assert consumed.env is not None and consumed.env[key] == value
    await process.stop()


@pytest.mark.asyncio
async def test_tr2_stale_epochs_never_answer(clean: Path) -> None:
    """TR2/G20/G21: only the exact int current epoch answers; stop and respawn clear it."""
    process = _process()
    await process.start()
    epoch = process.child_epoch
    assert epoch == 1
    record = process.launch_record_for_epoch(epoch)
    assert record is not None
    for stale in (epoch - 1, epoch + 1, True, "1", 1.0, None):
        assert process.launch_record_for_epoch(stale) is None
    await process.stop()
    assert process.launch_record_for_epoch(epoch) is None
    assert process._launch_record is None and process._pending_launch is None
    await process.start()
    new_epoch = process.child_epoch
    assert new_epoch == epoch + 2
    assert process.launch_record_for_epoch(epoch) is None
    renewed = process.launch_record_for_epoch(new_epoch)
    assert renewed is not None and renewed.child_epoch == new_epoch and renewed is not record
    await process.stop()


@pytest.mark.parametrize("failure", ["health", "initialize"])
@pytest.mark.asyncio
async def test_tr3_startup_failures_leave_no_record(clean: Path, failure: str) -> None:
    """TR3/G20: a failed start clears the record and proof in private state."""
    session = (
        _Session(health_error=RuntimeError("health"))
        if failure == "health"
        else _Session(init_error=RuntimeError("init"))
    )
    process = _process(probe=_Probe(session))
    with pytest.raises(MCPConnectionError):
        await process.start()
    assert process.running is False
    assert all(process.launch_record_for_epoch(e) is None for e in range(process.child_epoch + 2))
    assert process._launch_record is None and process._pending_launch is None


def test_tr4_injected_session_has_no_record() -> None:
    """TR4/G21: an injected session (epoch 1) has no launch record."""
    process = MCPServerProcess(session=_Session())
    assert process.child_epoch == 1
    assert process.launch_record_for_epoch(1) is None


_SECRET = "c1-secret-sentinel-7f3a"
_AUTH_SENTINEL = "manual-sentinel-xyz"
_PATH_SENTINEL = "path-sentinel-q9"


def _assert_no_sentinels(*texts: str) -> None:
    for text in texts:
        assert _SECRET not in text
        assert _AUTH_SENTINEL not in text
        assert _PATH_SENTINEL not in text


@pytest.mark.parametrize("case", ["authority", "missing_path", "manual_path"])
@pytest.mark.asyncio
async def test_tr5_refusals_are_key_only(
    clean: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    case: str,
) -> None:
    """TR5/G25: refusal logs and messages carry enum and key names only."""
    monkeypatch.setenv("ROASTPILOT_TEST_SECRET", _SECRET)
    directory = tmp_path / _PATH_SENTINEL
    directory.mkdir()
    if case == "authority":
        config_env = {AUTH: _AUTH_SENTINEL}
        expected = LaunchRefusal.EXPLICIT_AUTHORITY_INCOMPATIBLE
    elif case == "missing_path":
        config_env = {CFG: str(directory / "missing.yaml")}
        expected = LaunchRefusal.CONFIG_SOURCE_UNREADABLE
    else:
        config_env = {CFG: str(_write(directory / "c.yaml", MANUAL_YAML))}
        expected = LaunchRefusal.CONFIG_AUTHORITY_INCOMPATIBLE
    process = _process(config_env)
    with caplog.at_level(logging.INFO, logger="roastpilot_agent"):
        exc = await _refused(process)
    assert exc.refusal is expected
    assert exc.__cause__ is None and exc.__suppress_context__ is True
    _assert_no_sentinels(caplog.text, str(exc), repr(exc))
    assert expected.name in caplog.text
    if case == "authority":
        assert AUTH in caplog.text
    assert process.launch_record_for_epoch(process.child_epoch) is None


@pytest.mark.parametrize("capture", [None, _capture()])
@pytest.mark.asyncio
async def test_tr5_accepted_launch_record_and_logs_are_private(
    clean: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capture: RequestedCapture | None,
) -> None:
    """TR5/G25: an accepted launch's record, logs and pending proof hold no secret."""
    monkeypatch.setenv("ROASTPILOT_TEST_SECRET", _SECRET)
    process = _process(capture=capture)
    with caplog.at_level(logging.INFO, logger="roastpilot_agent"):
        process.build_server_parameters()
        pending = process._pending_launch
        assert pending is not None
        pending_repr = repr(pending)
        assert pending_repr.startswith("<") and "parameters" not in pending_repr
        await process.start()
    record = process.launch_record_for_epoch(process.child_epoch)
    assert record is not None
    _assert_no_sentinels(caplog.text, repr(record), str(record), pending_repr)
    await process.stop()


def test_tr6_closed_evidence_and_record_field_sets() -> None:
    """TR6: no full env, config path, command, version or digest fields."""
    assert {f.name for f in dataclasses.fields(LaunchEvidence)} == {
        "authority",
        "env_base",
        "config_selection",
        "config_present",
        "config_authority",
        "override_authority",
        "inherited_authority",
        "pinned_env",
        "capture",
        "topology",
    }
    assert {f.name for f in dataclasses.fields(LaunchRecord)} == {"child_epoch", "evidence"}


@pytest.mark.asyncio
async def test_tr7_no_record_and_no_pending_proof_at_factory_entry(clean: Path) -> None:
    """TR7/G23: the record binds only after attach; the proof is consumed before the factory."""
    process: MCPServerProcess | None = None
    seen: list[tuple[object, object]] = []

    def on_enter() -> None:
        assert process is not None
        seen.append((process._launch_record, process._pending_launch))

    process = _process(probe=_Probe(on_enter=on_enter))
    for _ in range(2):
        await process.start()
        assert process.launch_record_for_epoch(process.child_epoch) is not None
        await process.stop()
    assert seen == [(None, None), (None, None)]


def _frozen_cold_env(tmp_path: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "USER": "cold-test",
        "LOGNAME": "cold-test",
        "SHELL": "",
        "TERM": "",
        "COFFEE_ROASTER_MCP_CONFIG": "/r/a.yaml",
    }


class _ColdSpy(cold_composition.ColdMCPServerProcess):
    calls = 0

    def build_server_parameters(self) -> StdioServerParameters:
        type(self).calls += 1
        return super().build_server_parameters()


@pytest.mark.asyncio
async def test_tr8_cold_override_remains_the_spawn_seam_without_a_record(
    clean: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TR8/G29/G30: the cold builder is called, frozen, unmerged and record-free."""
    monkeypatch.setenv(AUTH, "manual")  # ignored by the frozen cold mapping
    _write(clean / lc.DEFAULT_CHILD_CONFIG_FILENAME, MANUAL_YAML)
    _ColdSpy.calls = 0
    process = _ColdSpy(MCPConfig(), child_environment=_frozen_cold_env(tmp_path), family="posix")
    probe = _Probe()
    process._session_factory = probe
    await process.start()
    assert _ColdSpy.calls == 1
    assert probe.params is not None and probe.params.env == _frozen_cold_env(tmp_path)
    assert process._rendered_yaml_dir is None
    assert process.launch_record_for_epoch(process.child_epoch) is None
    await process.stop()

    monkeypatch.setattr(
        mcp_stdio, "DEFAULT_INHERITED_ENV_VARS", [*mcp_stdio.DEFAULT_INHERITED_ENV_VARS, "EVIL"]
    )
    probe.params = None
    with pytest.raises(MCPConnectionError):
        await process.start()
    assert probe.params is None and _ColdSpy.calls == 2
    assert process.launch_record_for_epoch(process.child_epoch) is None


_Base = Callable[[], StdioServerParameters]


class _Override(MCPServerProcess):
    """A subclass builder whose behaviour receives the base builder."""

    def __init__(
        self, behaviour: Callable[[_Base], StdioServerParameters], **kwargs: object
    ) -> None:
        super().__init__(**kwargs)  # pyright: ignore[reportArgumentType]
        self._behaviour = behaviour

    def build_server_parameters(self) -> StdioServerParameters:
        return self._behaviour(super().build_server_parameters)


def _mutate_env(base: _Base) -> StdioServerParameters:
    params = base()
    params.env = {**(params.env or {}), "C1_INJECTED": "x"}
    return params


def _mutate_env_in_place(base: _Base) -> StdioServerParameters:
    params = base()
    assert params.env is not None
    params.env["C1_INJECTED"] = "x"
    return params


def _mutate_command(base: _Base) -> StdioServerParameters:
    params = base()
    params.command = "/bin/other"
    return params


def _mutate_args(base: _Base) -> StdioServerParameters:
    params = base()
    params.args = ["serve", "--config", "x.yaml"]
    return params


def _mutate_cwd(base: _Base) -> StdioServerParameters:
    params = base()
    params.cwd = "/"
    return params


def _equal_copy(base: _Base) -> StdioServerParameters:
    return base().model_copy(deep=True)


def _earlier_of_two(base: _Base) -> StdioServerParameters:
    first = base()
    base()
    return first


_MISMATCHES: dict[str, Callable[[_Base], StdioServerParameters]] = {
    "env": _mutate_env,
    "env_in_place": _mutate_env_in_place,
    "command": _mutate_command,
    "args": _mutate_args,
    "cwd": _mutate_cwd,
    "equal_copy": _equal_copy,
    "earlier_of_two": _earlier_of_two,
}


@pytest.mark.parametrize("capture", [None, _capture()])
@pytest.mark.asyncio
async def test_tr9_base_delegating_unchanged_override_gets_the_record(
    clean: Path, capture: RequestedCapture | None
) -> None:
    """TR9/G31: delegating to the base and returning the exact object binds the record."""
    probe = _Probe()
    process = _Override(lambda base: base(), session_factory=probe, human_capture=capture)
    await process.start()
    record = process.launch_record_for_epoch(process.child_epoch)
    assert record is not None
    expected = ControlAuthority.AUTOMATIC if capture is None else ControlAuthority.HUMAN
    assert record.evidence.authority is expected
    await process.stop()


@pytest.mark.parametrize("capture", [None, _capture()])
@pytest.mark.parametrize("case", sorted(_MISMATCHES))
@pytest.mark.asyncio
async def test_tr9_changed_or_substituted_params_are_refused_before_factory(
    clean: Path, case: str, capture: RequestedCapture | None
) -> None:
    """TR9/G31: any change, an equal copy or an earlier preview refuses in both authorities."""
    probe = _Probe()
    # A non-empty config env gives AUTOMATIC a mutable merged env for the in-place row.
    config = MCPConfig(env={"C1_BASE": "1"} if case == "env_in_place" else {})
    process = _Override(
        _MISMATCHES[case], config=config, session_factory=probe, human_capture=capture
    )
    exc = await _refused(process)
    assert exc.refusal is LaunchRefusal.LAUNCH_COMPOSITION_UNPROVEN
    _assert_not_spawned(process, probe)


@pytest.mark.parametrize("capture", [None, _capture()])
@pytest.mark.asyncio
async def test_tr9_mutation_during_initialize_refuses_before_attach(
    clean: Path, capture: RequestedCapture | None
) -> None:
    """TR9/G33: the post-initialize recheck refuses a mutated object; no record."""
    holder: list[StdioServerParameters] = []

    def mutate() -> None:
        holder[0].args = ["serve", "--mutated"]

    probe = _Probe(_Session(on_init=mutate))

    def keep(base: _Base) -> StdioServerParameters:
        params = base()
        holder[:] = [params]
        return params

    process = _Override(keep, session_factory=probe, human_capture=capture)
    exc = await _refused(process)
    assert exc.refusal is LaunchRefusal.LAUNCH_COMPOSITION_UNPROVEN
    assert probe.calls == 1 and probe.exited is True
    assert probe.session.calls == []
    assert process.running is False and process.child_epoch == 0
    assert process._launch_record is None and process._pending_launch is None


def _legacy(base: _Base) -> StdioServerParameters:
    return StdioServerParameters(command="legacy-mcp", args=["serve"], env=None)


@pytest.mark.asyncio
async def test_tr9_legacy_builder_keeps_automatic_without_record_but_refuses_human(
    clean: Path,
) -> None:
    """TR9/G30: no proof → AUTOMATIC legacy launch with no record; HUMAN refuses."""
    probe = _Probe()
    automatic = _Override(_legacy, session_factory=probe)
    await automatic.start()
    assert probe.params is not None and probe.params.command == "legacy-mcp"
    assert automatic.launch_record_for_epoch(automatic.child_epoch) is None
    await automatic.stop()

    human_probe = _Probe()
    human = _Override(_legacy, session_factory=human_probe, human_capture=_capture())
    exc = await _refused(human)
    assert exc.refusal is LaunchRefusal.LAUNCH_COMPOSITION_UNPROVEN
    _assert_not_spawned(human, human_probe)


@pytest.mark.parametrize("capture", [None, _capture()])
@pytest.mark.asyncio
async def test_tr9_cached_preview_is_never_consumed_as_proof(
    clean: Path, capture: RequestedCapture | None
) -> None:
    """TR9/G32: a cached base preview returned later without base composition is unproven."""
    cache: list[StdioServerParameters] = []

    def cached(base: _Base) -> StdioServerParameters:
        if not cache:
            cache.append(base())
        return cache[0]

    probe = _Probe()
    process = _Override(cached, session_factory=probe, human_capture=capture)
    preview = process.build_server_parameters()
    assert process._pending_launch is not None
    if capture is None:
        await process.start()
        assert probe.params is preview
        assert process.launch_record_for_epoch(process.child_epoch) is None
        await process.stop()
    else:
        exc = await _refused(process)
        assert exc.refusal is LaunchRefusal.LAUNCH_COMPOSITION_UNPROVEN
        _assert_not_spawned(process, probe)


# =========================================================================== proof policy


def test_tp1_proof_policy_matrix() -> None:
    """TP1/G34: MATCHED admits both; MISMATCHED refuses both; ABSENT admits AUTOMATIC only."""
    for authority in ControlAuthority:
        lc.require_launch_proof(authority, LaunchProofStatus.MATCHED)
        with pytest.raises(LaunchRefusalError) as raised:
            lc.require_launch_proof(authority, LaunchProofStatus.MISMATCHED)
        assert raised.value.refusal is LaunchRefusal.LAUNCH_COMPOSITION_UNPROVEN
    lc.require_launch_proof(ControlAuthority.AUTOMATIC, LaunchProofStatus.ABSENT)
    with pytest.raises(LaunchRefusalError) as absent:
        lc.require_launch_proof(ControlAuthority.HUMAN, LaunchProofStatus.ABSENT)
    assert absent.value.refusal is LaunchRefusal.LAUNCH_COMPOSITION_UNPROVEN


@pytest.mark.parametrize(
    ("authority", "status"),
    [
        ("automatic", LaunchProofStatus.MATCHED),
        (ControlAuthority.AUTOMATIC, "matched"),
        (True, LaunchProofStatus.MATCHED),
        (ControlAuthority.AUTOMATIC, False),
        (LaunchProofStatus.MATCHED, LaunchProofStatus.MATCHED),
        (ControlAuthority.AUTOMATIC, ControlAuthority.AUTOMATIC),
        (ControlAuthority.AUTOMATIC, AuthorityClass.ABSENT),
    ],
)
def test_tp1_proof_policy_rejects_wrong_types(authority: object, status: object) -> None:
    """TP1: raw strings, bools and wrong enum types never select a default."""
    with pytest.raises(TypeError):
        lc.require_launch_proof(
            cast("ControlAuthority", authority), cast("LaunchProofStatus", status)
        )


@pytest.mark.parametrize("capture", [None, _capture()])
@pytest.mark.asyncio
async def test_tp1_transport_uses_the_pure_policy_before_factory_and_after_initialize(
    clean: Path, monkeypatch: pytest.MonkeyPatch, capture: RequestedCapture | None
) -> None:
    """TP1/G34: the domain function is the only policy, called pre-factory and post-init."""
    events: list[object] = []
    real = lc.require_launch_proof

    def spy(authority: ControlAuthority, status: LaunchProofStatus) -> None:
        events.append((authority, status))
        real(authority, status)

    monkeypatch.setattr(mcp_client, "require_launch_proof", spy)
    probe = _Probe(_Session(on_init=lambda: events.append("init")), lambda: events.append("enter"))
    process = _process(probe=probe, capture=capture)
    await process.start()
    authority = ControlAuthority.AUTOMATIC if capture is None else ControlAuthority.HUMAN
    matched = (authority, LaunchProofStatus.MATCHED)
    assert events == [matched, "enter", "init", matched]
    await process.stop()


# =========================================================================== architecture

_ALLOWED_DOMAIN_IMPORTS = {
    "__future__",
    "collections.abc",
    "dataclasses",
    "enum",
    "math",
    "typing",
    "roastpilot_agent.human_protection",
}
_FORBIDDEN_NAMES = {
    "open",
    "os",
    "pathlib",
    "io",
    "yaml",
    "tempfile",
    "shutil",
    "logging",
    "mcp",
    "MCPConnectionError",
    "mcp_client",
    "mcp_yaml",
}


def test_td1_domain_imports_are_closed() -> None:
    """TD1/G27: the domain module imports only the allowlist and names no I/O."""
    tree = ast.parse(Path(lc.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module is not None
            imported.add(node.module)
    assert imported <= _ALLOWED_DOMAIN_IMPORTS
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not (names | attributes) & _FORBIDDEN_NAMES
    assert "roastpilot_agent.config" not in imported


@pytest.mark.slow
def test_td1_domain_import_loads_no_infrastructure() -> None:
    """TD1/G27: importing the domain in isolation loads no MCP client, SDK or YAML."""
    script = (
        "import sys, roastpilot_agent.launch_composition\n"
        "bad = sorted(m for m in ('roastpilot_agent.mcp_client', 'roastpilot_agent.mcp_yaml',"
        " 'roastpilot_agent.config', 'mcp', 'yaml') if m in sys.modules)\n"
        "print(bad)\n"
        "raise SystemExit(1 if bad else 0)\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "[]"


def test_td2_production_builder_override_inventory_is_closed() -> None:
    """TD2/G35: exactly the cold subclass overrides the public spawn seam."""
    package = Path(lc.__file__).parent
    overrides: set[str] = set()
    for path in sorted(package.rglob("*.py")):
        module = ".".join(("roastpilot_agent", *path.relative_to(package).with_suffix("").parts))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and any(
                isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
                and item.name == "build_server_parameters"
                for item in node.body
            ):
                overrides.add(f"{module}.{node.name}")
    assert overrides - {"roastpilot_agent.mcp_client.MCPServerProcess"} == {
        "roastpilot_agent.cold_composition.ColdMCPServerProcess"
    }
    assert "roastpilot_agent.mcp_client.MCPServerProcess" in overrides


# =========================================================================== domain units


def test_classify_and_select_grammar() -> None:
    """G-A/G-P unit rows."""
    assert lc.classify_authority(" AuToMaTiC ") is AuthorityClass.AUTOMATIC
    assert lc.classify_authority("manual\n") is AuthorityClass.MANUAL
    assert lc.classify_authority(None) is AuthorityClass.INVALID
    assert lc.classify_authority(b"manual") is AuthorityClass.INVALID
    assert lc.select_child_config(None) == ChildConfigSelection(
        ConfigSelectionKind.CWD_DEFAULT, "coffee-roaster-mcp.yaml"
    )
    assert lc.select_child_config({CFG: " /a.yaml "}) == ChildConfigSelection(
        ConfigSelectionKind.ENV_PATH, "/a.yaml"
    )
    assert lc.select_child_config({CFG: ""}).kind is ConfigSelectionKind.CWD_DEFAULT


def test_child_config_identity_type_guards() -> None:
    """G-Y: exact read type and status are required."""
    selection = lc.select_child_config(None)
    with pytest.raises(TypeError):
        ChildConfigRead(cast("ConfigReadStatus", "loaded"))
    with pytest.raises(TypeError):
        lc.child_config_identity(cast("ChildConfigRead", object()), selection)
    forged = ChildConfigRead(ConfigReadStatus.LOADED, {})
    object.__setattr__(forged, "status", "loaded")  # bypass __post_init__
    with pytest.raises(TypeError):
        lc.child_config_identity(forged, selection)
    loaded = lc.child_config_identity(
        ChildConfigRead(ConfigReadStatus.LOADED, {"roaster": {"driver": "x"}}), selection
    )
    assert loaded == lc.ChildConfigIdentity(True, AuthorityClass.ABSENT, "x", None)


def test_finalize_and_bind_type_guards() -> None:
    """Exact prepared/evidence/epoch types are required."""
    prepared = lc.prepare_child_launch(explicit_env={}, inherited_env={}, capture=None)
    with pytest.raises(TypeError):
        lc.finalize_child_launch(
            cast("lc.PreparedLaunch", object()), ChildConfigRead(ConfigReadStatus.MISSING)
        )
    evidence = lc.finalize_child_launch(
        prepared, ChildConfigRead(ConfigReadStatus.MISSING)
    ).evidence
    assert lc.bind_launch_record(evidence, 3) == LaunchRecord(3, evidence)
    for bad in (True, 1.0, "1"):
        with pytest.raises(TypeError):
            lc.bind_launch_record(evidence, cast("int", bad))
    with pytest.raises(ValueError):
        lc.bind_launch_record(evidence, 0)
    with pytest.raises(TypeError):
        lc.bind_launch_record(cast("LaunchEvidence", object()), 1)


def test_prepare_never_mutates_inputs() -> None:
    """Inputs are copied; the explicit and inherited maps are unchanged."""
    explicit = {"COFFEE_ROASTER_DRIVER": "mock"}
    inherited = {AUTH: "manual", "X": "1"}
    before = (dict(explicit), dict(inherited))
    lc.prepare_child_launch(explicit_env=explicit, inherited_env=inherited, capture=_capture())
    assert (explicit, inherited) == before


def test_launch_refusal_error_message_is_key_only() -> None:
    """LaunchRefusalError carries the refusal and key; message is name[: key]."""
    error = LaunchRefusalError(LaunchRefusal.PIN_NOT_CONSUMED, "K")
    assert (error.refusal, error.key, str(error)) == (
        LaunchRefusal.PIN_NOT_CONSUMED,
        "K",
        "PIN_NOT_CONSUMED: K",
    )
    assert str(LaunchRefusalError(LaunchRefusal.CONFIG_SOURCE_INVALID)) == "CONFIG_SOURCE_INVALID"
