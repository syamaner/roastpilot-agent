"""Hardware-free behavioural coverage for cold host-bound readers."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

from roastpilot_agent.cold_characterisation.host import (
    HOST_MIN_FREE_BYTES_BEFORE,
    HOST_MIN_FREE_BYTES_DURING,
    HOST_MIN_MEM_AVAILABLE_BYTES,
    ColdHostBoundError,
    ColdHostBoundFailure,
    HostBoundSample,
    HostBoundsConfig,
    HostBoundsReader,
    LinuxHostBoundsReader,
)


class RecordingRunner:
    """Injectable command runner that records only list argv."""

    def __init__(self, result: subprocess.CompletedProcess[bytes] | BaseException) -> None:
        """Store the result or error that a command invocation should produce."""
        self.calls: list[list[str]] = []
        self.result = result

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[bytes]:
        """Record *argv* and return the configured completed process."""
        self.calls.append(argv)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class OversizedGrammarBytes(bytes):
    """Oversized bytes whose decode is grammar-valid only if the cap is bypassed."""

    def decode(self, encoding: str = "utf-8", errors: str = "strict") -> str:
        """Return canonical throttle text while retaining the oversized byte length."""
        del encoding, errors
        return "throttled=0x0"


class FakePopen:
    """Small pipe-backed stand-in for the bounded production runner."""

    def __init__(
        self,
        stdout: bytes = b"throttled=0x0\n",
        *,
        keep_open: bool = False,
        wait_timeout: bool = False,
        already_exited: bool = False,
    ) -> None:
        """Expose controlled stdout and lifecycle observations."""
        read_fd, self._write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, "rb", buffering=0)
        self.args: list[str] = []
        self.killed = False
        self.reaped = False
        self.reaper_event = threading.Event()
        self.wait_timeout = wait_timeout
        self.already_exited = already_exited
        self.wait_timeouts: list[float | None] = []
        if stdout:
            os.write(self._write_fd, stdout)
        if not keep_open:
            os.close(self._write_fd)
            self._write_fd = -1

    def poll(self) -> int | None:
        """Keep the fake live until the runner explicitly reaps it."""
        return 0 if self.already_exited else None

    def kill(self) -> None:
        """Record deterministic termination and unblock a waiting reader."""
        self.killed = True
        if self._write_fd >= 0:
            os.close(self._write_fd)
            self._write_fd = -1

    def wait(self, *, timeout: float | None = None) -> int:
        """Record reaping and return a successful controlled exit."""
        self.wait_timeouts.append(timeout)
        if self.wait_timeout and timeout is not None:
            raise subprocess.TimeoutExpired(self.args, timeout)
        if self.wait_timeout:
            self.reaper_event.set()
        self.reaped = True
        return 0


def _completed(
    *,
    returncode: int = 0,
    stdout: bytes = b"throttled=0x0\n",
    stderr: bytes = b"",
) -> subprocess.CompletedProcess[bytes]:
    """Return a controlled byte-oriented command result."""
    return subprocess.CompletedProcess(["vcgencmd", "get_throttled"], returncode, stdout, stderr)


def _reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    thermal: str = "79900\n",
    meminfo: str | None = None,
    command_path: Path | None = None,
    runner: RecordingRunner | None = None,
    free_bytes: int = HOST_MIN_FREE_BYTES_BEFORE,
    thermal_path: Path | None = None,
    meminfo_path: Path | None = None,
    use_default_runner: bool = False,
    timeout_seconds: float = 5.0,
) -> LinuxHostBoundsReader:
    """Build a Linux reader with files and seams wholly inside ``tmp_path``."""
    configured_thermal_path = thermal_path or tmp_path / "thermal"
    if thermal_path is None:
        configured_thermal_path.write_text(thermal, encoding="ascii")
    configured_meminfo_path = meminfo_path or tmp_path / "meminfo"
    if meminfo_path is None:
        configured_meminfo_path.write_text(
            f"MemAvailable: {HOST_MIN_MEM_AVAILABLE_BYTES // 1024} kB\n"
            if meminfo is None
            else meminfo,
            encoding="ascii",
        )
    binary_path = command_path or tmp_path / "vcgencmd"
    if command_path is None:
        binary_path.touch()

    def fake_statvfs(_path: object) -> Any:
        return SimpleNamespace(f_bavail=free_bytes, f_bfree=free_bytes + 17, f_frsize=1)

    monkeypatch.setattr(os, "statvfs", fake_statvfs)
    config = HostBoundsConfig(
        vcgencmd_path=binary_path,
        thermal_zone_temp_path=configured_thermal_path,
        meminfo_path=configured_meminfo_path,
        vcgencmd_timeout_seconds=timeout_seconds,
    )
    reader = object.__new__(LinuxHostBoundsReader)
    object.__setattr__(reader, "_config", config)
    runner_factory = reader._make_default_command_runner  # pyright: ignore[reportPrivateUsage]
    object.__setattr__(
        reader,
        "_command_runner",
        runner_factory() if use_default_runner else runner or RecordingRunner(_completed()),
    )
    return reader


def _assert_failure(expected: ColdHostBoundFailure, action: Any) -> None:
    """Assert that an observation fails with one closed failure member."""
    with pytest.raises(ColdHostBoundError) as raised:
        action()
    assert raised.value.failure is expected


def test_bound_constants_are_literal_ac15_pins() -> None:
    """AC15 bounds cannot drift from their literal binary-unit values."""
    assert HOST_MIN_MEM_AVAILABLE_BYTES == 512 * 2**20
    assert HOST_MIN_FREE_BYTES_BEFORE == 2 * 2**30
    assert HOST_MIN_FREE_BYTES_DURING == 2**30


def test_host_config_defaults_are_exact_contract_pins() -> None:
    """The standalone config model preserves its exact ratified defaults."""
    config = HostBoundsConfig()
    assert config.vcgencmd_path == Path("/usr/bin/vcgencmd")
    assert config.thermal_zone_temp_path == Path("/sys/class/thermal/thermal_zone0/temp")
    assert config.meminfo_path == Path("/proc/meminfo")
    assert config.vcgencmd_timeout_seconds == 5.0


@pytest.mark.skipif(sys.platform != "linux", reason="real constructor admits Linux only")
def test_linux_constructor_wires_an_injected_runner() -> None:
    """The production constructor retains explicit config and runner wiring on Linux."""
    config = HostBoundsConfig()
    runner = RecordingRunner(_completed())
    reader = LinuxHostBoundsReader(config, command_runner=runner)
    assert reader._config is config  # pyright: ignore[reportPrivateUsage]
    assert reader._command_runner is runner  # pyright: ignore[reportPrivateUsage]


@pytest.mark.skipif(sys.platform != "linux", reason="real constructor admits Linux only")
def test_linux_constructor_wires_defaults_without_executing_a_child() -> None:
    """The production constructor builds its default runner without running it."""
    reader = LinuxHostBoundsReader()
    assert reader._config == HostBoundsConfig()  # pyright: ignore[reportPrivateUsage]
    assert callable(reader._command_runner)  # pyright: ignore[reportPrivateUsage]


def test_closed_failure_enum_has_the_ratified_member_count() -> None:
    """The public closed failure grammar stays at the ratified 15 members."""
    assert len(ColdHostBoundFailure) == 15


def test_thermal_parses_safe_millidegrees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A safe millidegree source is returned in Celsius."""
    assert _reader(tmp_path, monkeypatch).read_thermal_c() == 79.9


@pytest.mark.parametrize("thermal", ["80000", "80100"])
def test_thermal_rejects_the_limit_and_above(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, thermal: str
) -> None:
    """Exactly 80 C and a hotter observation both fail closed."""
    _assert_failure(
        ColdHostBoundFailure.THERMAL_EXCEEDED,
        lambda: _reader(tmp_path, monkeypatch, thermal=thermal).read_thermal_c(),
    )


@pytest.mark.parametrize(
    "thermal",
    ["", "abc", "45.5", "79900\n1", "x79900", " 79900", "79900 ", "12345678"],
)
def test_thermal_rejects_malformed_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, thermal: str
) -> None:
    """Only the closed millidegree grammar is accepted."""
    _assert_failure(
        ColdHostBoundFailure.THERMAL_MALFORMED,
        lambda: _reader(tmp_path, monkeypatch, thermal=thermal).read_thermal_c(),
    )


def test_thermal_missing_source_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing thermal source never becomes a clean default."""
    reader = _reader(tmp_path, monkeypatch, thermal_path=tmp_path / "missing")
    _assert_failure(ColdHostBoundFailure.THERMAL_UNREADABLE, reader.read_thermal_c)


def test_thermal_non_ascii_source_is_malformed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decoding failure is malformed rather than an acceptable temperature."""
    thermal_path = tmp_path / "thermal"
    thermal_path.write_bytes(b"\xff")
    reader = _reader(tmp_path, monkeypatch, thermal_path=thermal_path)
    _assert_failure(ColdHostBoundFailure.THERMAL_MALFORMED, reader.read_thermal_c)


def test_thermal_oversize_source_is_malformed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Thermal input is capped before a large source can be parsed."""
    thermal_path = tmp_path / "thermal"
    thermal_path.write_bytes(b"7" * 129)
    reader = _reader(tmp_path, monkeypatch, thermal_path=thermal_path)
    _assert_failure(ColdHostBoundFailure.THERMAL_MALFORMED, reader.read_thermal_c)


def test_throttle_reads_canonical_lowercase_word(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clear throttle result parses to an integer used for canonical samples."""
    runner = RecordingRunner(_completed(stdout=b"throttled=0x0100\n"))
    reader = _reader(tmp_path, monkeypatch, runner=runner)
    assert reader.read_throttled_word() == 0x100
    assert runner.calls == [[str((tmp_path / "vcgencmd").resolve()), "get_throttled"]]
    assert reader.sample(tmp_path).throttled_word_hex == "0x0100"


def test_throttle_accepts_uppercase_hex_and_samples_lowercase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The closed grammar accepts vcgencmd uppercase hex but canonicalises samples."""
    reader = _reader(
        tmp_path, monkeypatch, runner=RecordingRunner(_completed(stdout=b"throttled=0x0A00\n"))
    )
    assert reader.sample(tmp_path).throttled_word_hex == "0x0a00"


@pytest.mark.parametrize("bit", [0, 1, 2, 3, 16, 17, 18, 19])
def test_throttle_rejects_each_current_and_sticky_guard_bit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bit: int
) -> None:
    """All ratified current and sticky throttle bits are independently gated."""
    word = 1 << bit
    reader = _reader(
        tmp_path,
        monkeypatch,
        runner=RecordingRunner(_completed(stdout=f"throttled=0x{word:x}".encode("ascii"))),
    )
    _assert_failure(ColdHostBoundFailure.THROTTLE_BITS_SET, reader.read_throttled_word)


@pytest.mark.parametrize("bit", [4, 15, 20])
def test_throttle_records_adjacent_unmasked_bits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bit: int
) -> None:
    """Only the ratified current/sticky groups gate an otherwise observed throttle word."""
    word = 1 << bit
    reader = _reader(
        tmp_path,
        monkeypatch,
        runner=RecordingRunner(_completed(stdout=f"throttled=0x{word:x}\n".encode("ascii"))),
    )
    assert reader.read_throttled_word() == word
    assert reader.sample(tmp_path).throttled_word_hex == f"0x{word:x}"


@pytest.mark.parametrize(
    "stdout",
    [
        b"",
        b"throttled=0x0\nsecond",
        b"throttled=abc",
        b"throttled=0x",
        b"throttled=0x123456789",
        b"x" * 5120,
        b"\xff",
    ],
)
def test_throttle_rejects_malformed_or_oversized_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: bytes
) -> None:
    """Only one bounded ASCII throttle line is admitted."""
    reader = _reader(tmp_path, monkeypatch, runner=RecordingRunner(_completed(stdout=stdout)))
    _assert_failure(ColdHostBoundFailure.THROTTLE_OUTPUT_MALFORMED, reader.read_throttled_word)


def test_injected_runner_stdout_cap_precedes_grammar_decoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An oversized injected result fails its cap even if its decode would be valid."""
    stdout = OversizedGrammarBytes(b"x" * 4097)
    reader = _reader(tmp_path, monkeypatch, runner=RecordingRunner(_completed(stdout=stdout)))
    _assert_failure(ColdHostBoundFailure.THROTTLE_OUTPUT_MALFORMED, reader.read_throttled_word)


def test_throttle_default_runner_uses_a_closed_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production runner uses fixed argv, environment, stdio, and timeout."""
    calls: list[tuple[list[str], dict[str, object]]] = []
    process = FakePopen()

    def fake_popen(argv: list[str], **kwargs: object) -> FakePopen:
        calls.append((argv, kwargs))
        process.args = argv
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    reader = _reader(tmp_path, monkeypatch, use_default_runner=True)
    assert reader.read_throttled_word() == 0
    argv, kwargs = calls[0]
    assert isinstance(argv, list)
    assert argv == [str((tmp_path / "vcgencmd").resolve()), "get_throttled"]
    assert kwargs["env"] == {"LC_ALL": "C", "PATH": "/usr/bin:/bin"}
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["cwd"] == "/"
    assert kwargs.get("shell", False) is False


@pytest.mark.parametrize("timeout_seconds", [0.01, 0.1])
def test_default_runner_retains_a_positive_window_for_short_timeouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeout_seconds: float
) -> None:
    """Every admitted positive timeout leaves time for an immediately successful child."""
    process = FakePopen()

    def fake_popen(_argv: list[str], **_kwargs: object) -> FakePopen:
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr("roastpilot_agent.cold_characterisation.host.time.monotonic", lambda: 10.0)
    reader = _reader(
        tmp_path, monkeypatch, use_default_runner=True, timeout_seconds=timeout_seconds
    )
    assert reader.read_throttled_word() == 0
    assert process.wait_timeouts == pytest.approx([timeout_seconds / 2.0])


def test_default_runner_reads_only_the_bounded_stdout_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pipe reader never asks the kernel for more than its 4097-byte sentinel."""
    process = FakePopen()
    requested_sizes: list[int] = []
    original_read = os.read

    def fake_popen(_argv: list[str], **_kwargs: object) -> FakePopen:
        return process

    def recording_read(descriptor: int, size: int) -> bytes:
        requested_sizes.append(size)
        return original_read(descriptor, size)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(os, "read", recording_read)
    reader = _reader(tmp_path, monkeypatch, use_default_runner=True)
    assert reader.read_throttled_word() == 0
    assert requested_sizes
    assert max(requested_sizes) <= 4097


def test_throttle_default_runner_nonzero_exit_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production runner's nonzero completion maps to its closed failure."""
    process = FakePopen()

    def fake_popen(_argv: list[str], **_kwargs: object) -> FakePopen:
        return process

    def nonzero_wait(*, timeout: float | None = None) -> int:
        process.wait_timeouts.append(timeout)
        return 1

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(process, "wait", nonzero_wait)
    reader = _reader(tmp_path, monkeypatch, use_default_runner=True)
    _assert_failure(ColdHostBoundFailure.THROTTLE_INVOCATION_FAILED, reader.read_throttled_word)


@pytest.mark.parametrize("size", [4096, 4097])
def test_throttle_default_runner_enforces_stdout_cap_before_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    """Exactly 4096 bytes reaches grammar validation while byte 4097 kills the child."""
    process = FakePopen(b"x" * size)

    def fake_popen(_argv: list[str], **_kwargs: object) -> FakePopen:
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    reader = _reader(tmp_path, monkeypatch, use_default_runner=True)
    _assert_failure(ColdHostBoundFailure.THROTTLE_OUTPUT_MALFORMED, reader.read_throttled_word)
    assert process.killed is (size == 4097)
    assert process.reaped is True


def test_throttle_default_runner_timeout_kills_and_reaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The configured timeout bounds a silent child and deterministically reaps it."""
    process = FakePopen(keep_open=True)

    def fake_popen(_argv: list[str], **_kwargs: object) -> FakePopen:
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    reader = _reader(tmp_path, monkeypatch, use_default_runner=True, timeout_seconds=0.101)
    _assert_failure(ColdHostBoundFailure.THROTTLE_TIMEOUT, reader.read_throttled_word)
    assert process.killed is True
    assert process.reaped is True


def test_throttle_default_runner_post_eof_wait_timeout_uses_one_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child that closes stdout but never exits still fails as a typed timeout."""
    process = FakePopen(wait_timeout=True)

    def fake_popen(_argv: list[str], **_kwargs: object) -> FakePopen:
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr("roastpilot_agent.cold_characterisation.host.time.monotonic", lambda: 10.0)
    reader = _reader(tmp_path, monkeypatch, use_default_runner=True, timeout_seconds=1.0)
    _assert_failure(ColdHostBoundFailure.THROTTLE_TIMEOUT, reader.read_throttled_word)
    assert process.killed is True
    assert process.reaper_event.wait(0.1)
    assert process.stdout.closed is True
    assert process.wait_timeouts[:2] == pytest.approx([0.9, 0.1])


def test_throttle_default_runner_stdout_read_error_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Descriptor failures are contained and still close, kill, and reap the child."""
    process = FakePopen()

    def fake_popen(_argv: list[str], **_kwargs: object) -> FakePopen:
        return process

    def unavailable_read(_descriptor: int, _count: int) -> bytes:
        raise OSError("unavailable")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(os, "read", unavailable_read)
    reader = _reader(tmp_path, monkeypatch, use_default_runner=True)
    _assert_failure(ColdHostBoundFailure.THROTTLE_INVOCATION_FAILED, reader.read_throttled_word)
    assert process.killed is True
    assert process.reaped is True
    assert process.stdout.closed is True


def test_bounded_cleanup_handles_an_already_exited_child() -> None:
    """Cleanup skips kill for an exited process and also supports its fixed wait bound."""
    process = FakePopen(already_exited=True)
    cleanup = LinuxHostBoundsReader._kill_and_reap  # pyright: ignore[reportPrivateUsage]
    cleanup(cast(subprocess.Popen[bytes], process))
    assert process.killed is False
    assert process.reaped is True


@pytest.mark.parametrize(
    "result, failure",
    [
        (_completed(returncode=1), ColdHostBoundFailure.THROTTLE_INVOCATION_FAILED),
        (subprocess.TimeoutExpired(["x"], 5), ColdHostBoundFailure.THROTTLE_TIMEOUT),
    ],
)
def test_throttle_exit_and_timeout_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: subprocess.CompletedProcess[bytes] | BaseException,
    failure: ColdHostBoundFailure,
) -> None:
    """Non-zero completion and timeout never receive a substitute throttle word."""
    reader = _reader(tmp_path, monkeypatch, runner=RecordingRunner(result))
    _assert_failure(failure, reader.read_throttled_word)


def test_throttle_runner_os_error_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runner-side execution failure does not produce a substitute word."""
    reader = _reader(tmp_path, monkeypatch, runner=RecordingRunner(OSError("unavailable")))
    _assert_failure(ColdHostBoundFailure.THROTTLE_INVOCATION_FAILED, reader.read_throttled_word)


def test_throttle_runner_unexpected_exception_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Any injected runner exception is contained by the closed invocation failure."""
    secret = "OPENROUTER_API_KEY=not-for-output"
    reader = _reader(tmp_path, monkeypatch, runner=RecordingRunner(KeyError(secret)))
    with pytest.raises(ColdHostBoundError) as raised:
        reader.read_throttled_word()
    error = raised.value
    assert error.failure is ColdHostBoundFailure.THROTTLE_INVOCATION_FAILED
    assert secret not in str(error)
    assert secret not in repr(error)
    assert all(secret not in str(argument) for argument in error.args)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert secret not in caplog.text


def test_throttle_preserves_an_injected_closed_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typed failure from a trusted test seam remains unchanged."""
    reader = _reader(
        tmp_path,
        monkeypatch,
        runner=RecordingRunner(ColdHostBoundError(ColdHostBoundFailure.THROTTLE_TIMEOUT)),
    )
    _assert_failure(ColdHostBoundFailure.THROTTLE_TIMEOUT, reader.read_throttled_word)


def test_throttle_rejects_invalid_binary_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Relative, missing, and non-regular binary paths cannot run."""
    for path in (Path("relative-vcgencmd"), tmp_path / "missing", tmp_path):
        reader = _reader(tmp_path, monkeypatch, command_path=path)
        _assert_failure(ColdHostBoundFailure.THROTTLE_BINARY_MISSING, reader.read_throttled_word)


def test_throttle_rejects_an_existing_relative_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing cwd-relative regular file cannot satisfy the absolute-path guard."""
    relative_binary = tmp_path / "relative-vcgencmd"
    relative_binary.touch()
    monkeypatch.chdir(tmp_path)
    reader = _reader(tmp_path, monkeypatch, command_path=Path("relative-vcgencmd"))
    _assert_failure(ColdHostBoundFailure.THROTTLE_BINARY_MISSING, reader.read_throttled_word)


def test_throttle_uses_a_symlink_binarys_resolved_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trusted configured symlink executes its resolved regular-file target path."""
    target = tmp_path / "vcgencmd-target"
    target.touch()
    linked = tmp_path / "vcgencmd-link"
    linked.symlink_to(target)
    runner = RecordingRunner(_completed())
    reader = _reader(tmp_path, monkeypatch, command_path=linked, runner=runner)
    assert reader.read_throttled_word() == 0
    assert runner.calls == [[str(target.resolve()), "get_throttled"]]


def test_throttle_stderr_never_reaches_the_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Uncontrolled stderr remains absent from the fixed failure message."""
    secret = b"OPENROUTER_API_KEY=not-for-output"
    reader = _reader(
        tmp_path, monkeypatch, runner=RecordingRunner(_completed(returncode=1, stderr=secret))
    )
    with pytest.raises(ColdHostBoundError) as raised:
        reader.read_throttled_word()
    error = raised.value
    secret_text = secret.decode("ascii")
    assert secret_text not in str(error)
    assert secret_text not in repr(error)
    assert all(secret_text not in str(argument) for argument in error.args)
    assert error.__cause__ is None
    assert secret_text not in caplog.text


@pytest.mark.parametrize("timeout", [0.0, 30.1, float("inf")])
def test_timeout_config_is_finite_positive_and_capped(timeout: float) -> None:
    """The command timeout cannot disable or excessively extend the bound."""
    with pytest.raises(ValidationError):
        HostBoundsConfig(vcgencmd_timeout_seconds=timeout)


def test_timeout_config_accepts_the_exact_upper_bound() -> None:
    """Thirty seconds remains the largest admitted configured deadline."""
    assert HostBoundsConfig(vcgencmd_timeout_seconds=30.0).vcgencmd_timeout_seconds == 30.0


def test_source_paths_must_be_absolute() -> None:
    """Configured procfs and sysfs source paths cannot be cwd-relative."""
    with pytest.raises(ValidationError):
        HostBoundsConfig(thermal_zone_temp_path=Path("relative-source"))
    with pytest.raises(ValidationError):
        HostBoundsConfig(meminfo_path=Path("relative-source"))


def test_host_config_is_frozen() -> None:
    """Source configuration remains immutable after validation."""
    config = HostBoundsConfig()
    with pytest.raises(ValidationError):
        config.vcgencmd_timeout_seconds = 1.0  # type: ignore[misc]


def test_memory_accepts_the_exact_floor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exactly 512 MiB MemAvailable is sufficient."""
    assert _reader(tmp_path, monkeypatch).read_mem_available_bytes() == HOST_MIN_MEM_AVAILABLE_BYTES


@pytest.mark.parametrize(
    "meminfo",
    ["", "MemAvailable: 1 kB\nMemAvailable: 2 kB\n", "MemAvailable: x kB", "MemAvailable: 1"],
)
def test_memory_rejects_missing_or_ambiguous_grammar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, meminfo: str
) -> None:
    """Exactly one decimal-kilobyte MemAvailable line is required."""
    _assert_failure(
        ColdHostBoundFailure.MEMINFO_MALFORMED,
        lambda: _reader(tmp_path, monkeypatch, meminfo=meminfo).read_mem_available_bytes(),
    )


def test_memory_below_floor_and_missing_file_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Low or unreadable memory never looks acceptable."""
    _assert_failure(
        ColdHostBoundFailure.MEMINFO_BELOW_BOUND,
        lambda: _reader(
            tmp_path,
            monkeypatch,
            meminfo=f"MemAvailable: {(HOST_MIN_MEM_AVAILABLE_BYTES // 1024) - 1} kB",
        ).read_mem_available_bytes(),
    )
    reader = _reader(tmp_path, monkeypatch, meminfo_path=tmp_path / "missing")
    _assert_failure(ColdHostBoundFailure.MEMINFO_UNREADABLE, reader.read_mem_available_bytes)


def test_memory_non_ascii_source_is_malformed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decoding failure is malformed rather than an acceptable memory value."""
    meminfo_path = tmp_path / "meminfo"
    meminfo_path.write_bytes(b"MemAvailable: \xff kB\n")
    reader = _reader(tmp_path, monkeypatch, meminfo_path=meminfo_path)
    _assert_failure(ColdHostBoundFailure.MEMINFO_MALFORMED, reader.read_mem_available_bytes)


def test_memory_rejects_pathological_decimal_width(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Oversized decimal fields fail before integer conversion can escape the grammar."""
    reader = _reader(tmp_path, monkeypatch, meminfo="MemAvailable: " + "9" * 5000 + " kB\n")
    _assert_failure(ColdHostBoundFailure.MEMINFO_MALFORMED, reader.read_mem_available_bytes)


def test_memory_accepts_realistic_multiline_meminfo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unrelated Linux meminfo lines do not interfere with the exact field match."""
    reader = _reader(
        tmp_path,
        monkeypatch,
        meminfo="MemTotal: 8192000 kB\nBuffers: 1000 kB\nMemAvailable: 524288 kB\nSwapFree: 0 kB\n",
    )
    assert reader.read_mem_available_bytes() == HOST_MIN_MEM_AVAILABLE_BYTES


def test_unresolvable_binary_path_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error resolving the binary path is a closed missing-binary failure."""
    reader = _reader(tmp_path, monkeypatch)

    def unavailable_resolve(_path: Path, **_kwargs: object) -> Path:
        raise OSError("unavailable")

    monkeypatch.setattr(Path, "resolve", unavailable_resolve)
    _assert_failure(ColdHostBoundFailure.THROTTLE_BINARY_MISSING, reader.read_throttled_word)


def test_binary_is_file_error_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regular-file probing errors do not escape the closed binary failure."""
    reader = _reader(tmp_path, monkeypatch)

    def unavailable_is_file(_path: Path) -> bool:
        raise OSError("unavailable")

    monkeypatch.setattr(Path, "is_file", unavailable_is_file)
    _assert_failure(ColdHostBoundFailure.THROTTLE_BINARY_MISSING, reader.read_throttled_word)


def test_disk_uses_available_not_free_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reserved blocks cannot inflate the space reported to an unprivileged run."""
    reader = _reader(tmp_path, monkeypatch, free_bytes=123)

    def fake_statvfs(_path: object) -> Any:
        return SimpleNamespace(f_bavail=123, f_bfree=999, f_frsize=10)

    monkeypatch.setattr(os, "statvfs", fake_statvfs)
    assert reader.read_free_bytes(tmp_path) == 1230


def test_disk_start_and_run_bounds_are_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The weaker run floor cannot accidentally authorise a start."""
    reader = _reader(tmp_path, monkeypatch, free_bytes=HOST_MIN_FREE_BYTES_DURING)
    reader.check_run_bounds(tmp_path)
    _assert_failure(
        ColdHostBoundFailure.DISK_BELOW_START_BOUND, lambda: reader.check_start_bounds(tmp_path)
    )


@pytest.mark.parametrize("method_name", ["check_start_bounds", "check_run_bounds", "sample"])
def test_all_bound_aggregates_pass_the_caller_evidence_root_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method_name: str
) -> None:
    """Every aggregate checks disk space at the exact root supplied by its caller."""
    reader = _reader(tmp_path, monkeypatch)
    evidence_root = tmp_path / "evidence-root"
    seen_paths: list[object] = []

    def recording_statvfs(path: object) -> Any:
        seen_paths.append(path)
        return SimpleNamespace(
            f_bavail=HOST_MIN_FREE_BYTES_BEFORE,
            f_bfree=HOST_MIN_FREE_BYTES_BEFORE,
            f_frsize=1,
        )

    monkeypatch.setattr(os, "statvfs", recording_statvfs)
    getattr(reader, method_name)(evidence_root)
    assert seen_paths == [evidence_root]


def test_sample_accepts_the_during_floor_while_start_refuses_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sampling retains its ratified during-run floor rather than becoming start admission."""
    reader = _reader(tmp_path, monkeypatch, free_bytes=HOST_MIN_FREE_BYTES_DURING)
    assert reader.sample(tmp_path).free_bytes == HOST_MIN_FREE_BYTES_DURING
    _assert_failure(
        ColdHostBoundFailure.DISK_BELOW_START_BOUND, lambda: reader.check_start_bounds(tmp_path)
    )


def test_disk_start_exact_floor_passes_and_one_byte_below_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Start admission preserves the inclusive 2 GiB threshold."""
    _reader(tmp_path, monkeypatch, free_bytes=2 * 2**30).check_start_bounds(tmp_path)
    reader = _reader(tmp_path, monkeypatch, free_bytes=2 * 2**30 - 1)
    _assert_failure(
        ColdHostBoundFailure.DISK_BELOW_START_BOUND, lambda: reader.check_start_bounds(tmp_path)
    )


def test_disk_below_during_run_floor_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One byte below the during-run floor produces its distinct failure."""
    reader = _reader(tmp_path, monkeypatch, free_bytes=HOST_MIN_FREE_BYTES_DURING - 1)
    _assert_failure(
        ColdHostBoundFailure.DISK_BELOW_RUN_BOUND, lambda: reader.check_run_bounds(tmp_path)
    )


def test_disk_missing_source_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreadable filesystem source raises the closed disk failure."""
    reader = _reader(tmp_path, monkeypatch)

    def fail_statvfs(_path: object) -> Any:
        raise OSError("no disk")

    monkeypatch.setattr(os, "statvfs", fail_statvfs)
    _assert_failure(ColdHostBoundFailure.DISK_UNREADABLE, lambda: reader.read_free_bytes(tmp_path))


def test_disk_real_statvfs_accepts_tmp_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production statvfs path works against an ordinary temporary filesystem."""
    reader = _reader(tmp_path, monkeypatch)
    monkeypatch.undo()
    assert reader.read_free_bytes(tmp_path) > 0


def test_sample_is_finite_and_populated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean sweep returns all host values without a soft path."""
    sample = _reader(tmp_path, monkeypatch).sample(tmp_path)
    assert sample.soc_temp_c == 79.9
    assert sample.mem_available_bytes == HOST_MIN_MEM_AVAILABLE_BYTES
    assert sample.free_bytes == HOST_MIN_FREE_BYTES_BEFORE
    assert sample.throttled_word_hex == "0x0"
    assert sample.captured_at_utc
    assert datetime.fromisoformat(sample.captured_at_utc).tzinfo is UTC
    assert sample.monotonic_seconds >= 0.0


@pytest.mark.parametrize(
    "thermal,free_bytes,failure",
    [
        ("80000\n", HOST_MIN_FREE_BYTES_DURING, ColdHostBoundFailure.THERMAL_EXCEEDED),
        ("79900\n", HOST_MIN_FREE_BYTES_DURING - 1, ColdHostBoundFailure.DISK_BELOW_RUN_BOUND),
    ],
)
def test_sample_is_always_a_during_run_full_sweep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    thermal: str,
    free_bytes: int,
    failure: ColdHostBoundFailure,
) -> None:
    """Sampling retains its contractually weaker during-run disk floor and all sources."""
    reader = _reader(tmp_path, monkeypatch, thermal=thermal, free_bytes=free_bytes)
    _assert_failure(failure, lambda: reader.sample(tmp_path))


@pytest.mark.parametrize("method_name", ["check_start_bounds", "check_run_bounds", "sample"])
@pytest.mark.parametrize(
    "reader_method,failure",
    [
        ("read_thermal_c", ColdHostBoundFailure.THERMAL_UNREADABLE),
        ("_read_throttled", ColdHostBoundFailure.THROTTLE_TIMEOUT),
        ("read_mem_available_bytes", ColdHostBoundFailure.MEMINFO_UNREADABLE),
        ("read_free_bytes", ColdHostBoundFailure.DISK_UNREADABLE),
    ],
)
def test_aggregators_propagate_each_closed_source_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    reader_method: str,
    failure: ColdHostBoundFailure,
) -> None:
    """Start, run, and sampling aggregates preserve every closed source failure."""
    reader = _reader(tmp_path, monkeypatch)

    def fail(*_args: object) -> object:
        raise ColdHostBoundError(failure)

    monkeypatch.setattr(reader, reader_method, fail)
    method = getattr(reader, method_name)
    _assert_failure(failure, lambda: method(tmp_path))


def test_sample_rejects_non_finite_temperature() -> None:
    """The frozen sample schema cannot serialise a non-finite temperature."""
    with pytest.raises(ValidationError):
        HostBoundSample(
            captured_at_utc="2026-01-01T00:00:00+00:00",
            monotonic_seconds=1.0,
            soc_temp_c=float("nan"),
            throttled_word_hex="0x0",
            mem_available_bytes=HOST_MIN_MEM_AVAILABLE_BYTES,
            free_bytes=HOST_MIN_FREE_BYTES_BEFORE,
        )


def test_production_platform_refusal_uses_sys_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Platform admission directly uses the production platform value."""
    monkeypatch.setattr("roastpilot_agent.cold_characterisation.host.sys.platform", "darwin")
    _assert_failure(
        ColdHostBoundFailure.PLATFORM_UNSUPPORTED,
        LinuxHostBoundsReader,
    )


def test_reader_satisfies_protocol(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The production reader remains assignable to its narrow composition Protocol."""
    reader: HostBoundsReader = _reader(tmp_path, monkeypatch)
    assert reader.read_thermal_c() == 79.9
