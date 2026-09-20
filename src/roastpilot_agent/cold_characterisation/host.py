"""Fail-closed Linux host-bound readers for cold characterisation.

The module intentionally has no MCP transport or actuator surface.  It reads
host conditions that later slices use to refuse unsafe cold sessions.
"""

from __future__ import annotations

import math
import os
import re
import selectors
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import IO, Final, Protocol, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from roastpilot_agent.config import FINITE_NUMERIC_MODEL_CONFIG

HOST_MAX_TEMP_C: Final = 80.0
HOST_MIN_MEM_AVAILABLE_BYTES: Final = 512 * 2**20
HOST_MIN_FREE_BYTES_BEFORE: Final = 2 * 2**30
HOST_MIN_FREE_BYTES_DURING: Final = 1 * 2**30

_MAX_THROTTLE_STDOUT_BYTES: Final = 4096
_MAX_THERMAL_BYTES: Final = 128
_MAX_MEMINFO_BYTES: Final = 65536
_MAX_MEM_AVAILABLE_DIGITS: Final = 16
_CLEANUP_RESERVE_SECONDS: Final = 0.1
_THROTTLE_FAILURE_MASK: Final = 0x000F000F
_THERMAL_PATTERN: Final = re.compile(r"-?[0-9]{1,7}")
_THROTTLE_PATTERN: Final = re.compile(r"throttled=0x[0-9a-fA-F]{1,8}")
_MEM_AVAILABLE_PATTERN: Final = re.compile(
    rf"MemAvailable:[ \t]+([0-9]{{1,{_MAX_MEM_AVAILABLE_DIGITS}}}) kB"
)
_CHILD_ENV: Final[Mapping[str, str]] = MappingProxyType({"LC_ALL": "C", "PATH": "/usr/bin:/bin"})
_HOST_FINITE_NUMERIC_MODEL_CONFIG: Final[ConfigDict] = cast(
    ConfigDict, {**FINITE_NUMERIC_MODEL_CONFIG, "frozen": True}
)

CommandRunner: TypeAlias = Callable[[list[str]], subprocess.CompletedProcess[bytes]]


class _ThrottleOutputOverflow(RuntimeError):
    """Signal that bounded stdout exceeded its fixed cap."""


class ColdHostBoundFailure(Enum):
    """Closed failure grammar for cold host-bound observations."""

    THERMAL_UNREADABLE = "thermal_unreadable"
    THERMAL_MALFORMED = "thermal_malformed"
    THERMAL_EXCEEDED = "thermal_exceeded"
    THROTTLE_BINARY_MISSING = "throttle_binary_missing"
    THROTTLE_INVOCATION_FAILED = "throttle_invocation_failed"
    THROTTLE_TIMEOUT = "throttle_timeout"
    THROTTLE_OUTPUT_MALFORMED = "throttle_output_malformed"
    THROTTLE_BITS_SET = "throttle_bits_set"
    MEMINFO_UNREADABLE = "meminfo_unreadable"
    MEMINFO_MALFORMED = "meminfo_malformed"
    MEMINFO_BELOW_BOUND = "meminfo_below_bound"
    DISK_UNREADABLE = "disk_unreadable"
    DISK_BELOW_START_BOUND = "disk_below_start_bound"
    DISK_BELOW_RUN_BOUND = "disk_below_run_bound"
    PLATFORM_UNSUPPORTED = "platform_unsupported"


class ColdHostBoundError(RuntimeError):
    """Raised when a host observation is unavailable, malformed, or unsafe."""

    failure: ColdHostBoundFailure

    def __init__(self, failure: ColdHostBoundFailure) -> None:
        """Create an error without incorporating uncontrolled source text.

        Args:
            failure: The closed reason for refusing the observation.
        """
        super().__init__("Cold host bound check failed.")
        self.failure = failure


class HostBoundsConfig(BaseModel):
    """Fixed source locations and timeout for Linux host observations."""

    model_config = _HOST_FINITE_NUMERIC_MODEL_CONFIG

    vcgencmd_path: Path = Path("/usr/bin/vcgencmd")
    thermal_zone_temp_path: Path = Path("/sys/class/thermal/thermal_zone0/temp")
    meminfo_path: Path = Path("/proc/meminfo")
    vcgencmd_timeout_seconds: float = Field(default=5.0, gt=0.0, le=30.0)

    @field_validator("thermal_zone_temp_path", "meminfo_path")
    @classmethod
    def _validate_absolute_source_path(cls, value: Path) -> Path:
        """Reject relative host-source paths before a reader can use them."""
        if not value.is_absolute():
            raise ValueError("host source paths must be absolute")
        return value


class HostBoundSample(BaseModel):
    """One complete finite host-bound observation."""

    model_config = _HOST_FINITE_NUMERIC_MODEL_CONFIG

    captured_at_utc: str
    monotonic_seconds: float
    soc_temp_c: float
    throttled_word_hex: str
    mem_available_bytes: int
    free_bytes: int


class HostBoundsReader(Protocol):
    """Read and enforce the host bounds required by cold characterisation."""

    def read_thermal_c(self) -> float:
        """Read a safe finite system-on-chip temperature in Celsius."""
        ...

    def read_throttled_word(self) -> int:
        """Read a throttle word whose current and sticky guarded bits are clear."""
        ...

    def read_mem_available_bytes(self) -> int:
        """Read available memory after enforcing the minimum bound."""
        ...

    def read_free_bytes(self, path: Path) -> int:
        """Read unprivileged free bytes for *path*."""
        ...

    def check_start_bounds(self, evidence_root: Path) -> None:
        """Enforce all host bounds, including the start-of-run disk floor."""
        ...

    def check_run_bounds(self, evidence_root: Path) -> None:
        """Enforce all host bounds, including the during-run disk floor."""
        ...

    def sample(self, evidence_root: Path) -> HostBoundSample:
        """Return a during-run sample; start admission must use ``check_start_bounds``."""
        ...


class LinuxHostBoundsReader:
    """Fail-closed Linux implementation of :class:`HostBoundsReader`."""

    _config: HostBoundsConfig
    _command_runner: CommandRunner

    def __init__(
        self,
        config: HostBoundsConfig | None = None,
        *,
        command_runner: CommandRunner | None = None,
    ) -> None:
        """Create a Linux reader with explicit hardware-free test seams.

        Args:
            config: Source locations and command timeout.
            command_runner: Optional command seam receiving only list argv.

        Raises:
            ColdHostBoundError: If the admitted platform is not Linux.
        """
        if sys.platform != "linux":
            raise ColdHostBoundError(ColdHostBoundFailure.PLATFORM_UNSUPPORTED)
        self._config = config or HostBoundsConfig()
        runner = command_runner
        if runner is None:
            runner = self._make_default_command_runner()
        self._command_runner = runner

    def _make_default_command_runner(self) -> CommandRunner:
        """Build the only subprocess-backed runner with a closed invocation."""

        timeout = self._config.vcgencmd_timeout_seconds

        def run_command(argv: list[str]) -> subprocess.CompletedProcess[bytes]:
            process = subprocess.Popen(
                argv,
                cwd="/",
                env=_CHILD_ENV,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + timeout
            cleanup_reserve = min(_CLEANUP_RESERVE_SECONDS, timeout / 2.0)
            work_deadline = deadline - cleanup_reserve
            stdout_stream = process.stdout
            if stdout_stream is None:  # pragma: no cover - stdout=PIPE guarantees a stream
                self._kill_and_reap(process, min(deadline, time.monotonic() + cleanup_reserve))
                raise RuntimeError("vcgencmd stdout pipe unavailable")
            try:
                stdout = self._read_bounded_stdout(process, stdout_stream, work_deadline, timeout)
                remaining = work_deadline - time.monotonic()
                if remaining <= 0:  # pragma: no cover - selector timeout covers the same deadline
                    raise subprocess.TimeoutExpired(process.args, timeout)
                returncode = process.wait(timeout=remaining)
            except BaseException:
                self._kill_and_reap(process, min(deadline, time.monotonic() + cleanup_reserve))
                raise
            finally:
                with suppress(OSError):
                    stdout_stream.close()
            return subprocess.CompletedProcess(argv, returncode, stdout, None)

        return run_command

    @staticmethod
    def _kill_and_reap(process: subprocess.Popen[bytes], deadline: float | None = None) -> None:
        """Kill a failed child and bound cleanup so the caller keeps its deadline."""
        try:
            if process.poll() is None:
                process.kill()
            remaining = _CLEANUP_RESERVE_SECONDS
            if deadline is not None:
                remaining = max(0.0, deadline - time.monotonic())
            process.wait(timeout=remaining)
        except Exception:
            LinuxHostBoundsReader._reap_in_background(process)

    @staticmethod
    def _reap_in_background(process: subprocess.Popen[bytes]) -> None:
        """Hand an unreapable child to a daemon reaper after bounded cleanup."""
        threading.Thread(target=process.wait, daemon=True).start()

    @staticmethod
    def _read_bounded_stdout(
        process: subprocess.Popen[bytes],
        stdout_stream: IO[bytes],
        deadline: float,
        timeout: float,
    ) -> bytes:
        """Read no more than the throttle cap plus one sentinel byte before decode."""
        stdout = bytearray()
        try:
            descriptor = stdout_stream.fileno()
            with selectors.DefaultSelector() as selector:
                selector.register(descriptor, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:  # pragma: no cover - selector covers this deadline
                        raise subprocess.TimeoutExpired(process.args, timeout)
                    if not selector.select(remaining):
                        raise subprocess.TimeoutExpired(process.args, timeout)
                    chunk = os.read(descriptor, _MAX_THROTTLE_STDOUT_BYTES + 1 - len(stdout))
                    if not chunk:
                        return bytes(stdout)
                    stdout.extend(chunk)
                    if len(stdout) > _MAX_THROTTLE_STDOUT_BYTES:
                        raise _ThrottleOutputOverflow()
        except _ThrottleOutputOverflow:
            raise
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            raise RuntimeError("bounded stdout read failed") from None

    def read_thermal_c(self) -> float:
        """Read and enforce the Celsius thermal bound."""
        raw = self._read_ascii_bytes(
            self._config.thermal_zone_temp_path,
            _MAX_THERMAL_BYTES,
            ColdHostBoundFailure.THERMAL_UNREADABLE,
            ColdHostBoundFailure.THERMAL_MALFORMED,
        )
        value = raw.removesuffix("\n")
        if _THERMAL_PATTERN.fullmatch(value) is None:
            raise ColdHostBoundError(ColdHostBoundFailure.THERMAL_MALFORMED)
        temperature = int(value) / 1000.0
        if not math.isfinite(temperature):  # pragma: no cover - bounded integer input is finite
            raise ColdHostBoundError(ColdHostBoundFailure.THERMAL_MALFORMED)
        if temperature >= HOST_MAX_TEMP_C:
            raise ColdHostBoundError(ColdHostBoundFailure.THERMAL_EXCEEDED)
        return temperature

    def read_throttled_word(self) -> int:
        """Read and enforce the current and sticky throttle-bit bound."""
        return self._read_throttled()[0]

    def _read_throttled(self) -> tuple[int, str]:
        """Read the throttle word with its observed, lowercase-preserved hex spelling."""
        command_path = self._validated_vcgencmd_path()
        failure: ColdHostBoundFailure | None = None
        output = ""
        try:
            result = self._command_runner([str(command_path), "get_throttled"])
            returncode = result.returncode
            stdout = result.stdout
            if returncode != 0:
                failure = ColdHostBoundFailure.THROTTLE_INVOCATION_FAILED
            elif len(stdout) > _MAX_THROTTLE_STDOUT_BYTES:
                failure = ColdHostBoundFailure.THROTTLE_OUTPUT_MALFORMED
            else:
                output = stdout.decode("ascii")
        except ColdHostBoundError:
            raise
        except subprocess.TimeoutExpired:
            failure = ColdHostBoundFailure.THROTTLE_TIMEOUT
        except _ThrottleOutputOverflow:
            failure = ColdHostBoundFailure.THROTTLE_OUTPUT_MALFORMED
        except UnicodeDecodeError:
            failure = ColdHostBoundFailure.THROTTLE_OUTPUT_MALFORMED
        except Exception:
            failure = ColdHostBoundFailure.THROTTLE_INVOCATION_FAILED
        if failure is not None:
            raise ColdHostBoundError(failure)
        if output.endswith("\n"):
            output = output[:-1]
        if _THROTTLE_PATTERN.fullmatch(output) is None:
            raise ColdHostBoundError(ColdHostBoundFailure.THROTTLE_OUTPUT_MALFORMED)
        try:
            word = int(output.removeprefix("throttled=0x"), 16)
        except ValueError:  # pragma: no cover - anchored hexadecimal grammar is int-valid
            raise ColdHostBoundError(ColdHostBoundFailure.THROTTLE_OUTPUT_MALFORMED) from None
        if word & _THROTTLE_FAILURE_MASK:
            raise ColdHostBoundError(ColdHostBoundFailure.THROTTLE_BITS_SET)
        return word, f"0x{output.removeprefix('throttled=0x').lower()}"

    def read_mem_available_bytes(self) -> int:
        """Read and enforce the available-memory bound."""
        content = self._read_ascii_bytes(
            self._config.meminfo_path,
            _MAX_MEMINFO_BYTES,
            ColdHostBoundFailure.MEMINFO_UNREADABLE,
            ColdHostBoundFailure.MEMINFO_MALFORMED,
        )
        matches = [
            match.group(1)
            for line in content.split("\n")
            if (match := _MEM_AVAILABLE_PATTERN.fullmatch(line))
        ]
        if len(matches) != 1:
            raise ColdHostBoundError(ColdHostBoundFailure.MEMINFO_MALFORMED)
        try:
            available = int(matches[0]) * 1024
        except ValueError:  # pragma: no cover - anchored decimal grammar is int-valid
            raise ColdHostBoundError(ColdHostBoundFailure.MEMINFO_MALFORMED) from None
        if available < HOST_MIN_MEM_AVAILABLE_BYTES:
            raise ColdHostBoundError(ColdHostBoundFailure.MEMINFO_BELOW_BOUND)
        return available

    def read_free_bytes(self, path: Path) -> int:
        """Read free unprivileged disk space using ``f_bavail``."""
        try:
            filesystem = os.statvfs(path)
            return filesystem.f_bavail * filesystem.f_frsize
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            raise ColdHostBoundError(ColdHostBoundFailure.DISK_UNREADABLE) from None

    def check_start_bounds(self, evidence_root: Path) -> None:
        """Enforce every host bound with the stronger start disk threshold."""
        self._read_all(
            evidence_root, HOST_MIN_FREE_BYTES_BEFORE, ColdHostBoundFailure.DISK_BELOW_START_BOUND
        )

    def check_run_bounds(self, evidence_root: Path) -> None:
        """Enforce during-run bounds; it must never be used for start admission."""
        self._read_all(
            evidence_root, HOST_MIN_FREE_BYTES_DURING, ColdHostBoundFailure.DISK_BELOW_RUN_BOUND
        )

    def sample(self, evidence_root: Path) -> HostBoundSample:
        """Return one complete sample after enforcing every during-run bound."""
        thermal, throttled, memory, free = self._read_all(
            evidence_root,
            HOST_MIN_FREE_BYTES_DURING,
            ColdHostBoundFailure.DISK_BELOW_RUN_BOUND,
        )
        return HostBoundSample(
            captured_at_utc=datetime.now(UTC).isoformat(),
            monotonic_seconds=time.monotonic(),
            soc_temp_c=thermal,
            throttled_word_hex=throttled[1],
            mem_available_bytes=memory,
            free_bytes=free,
        )

    def _validated_vcgencmd_path(self) -> Path:
        """Return the configured binary only when it is absolute and regular."""
        configured_path = self._config.vcgencmd_path
        try:
            resolved_path = configured_path.resolve()
        except (OSError, RuntimeError, ValueError):
            raise ColdHostBoundError(ColdHostBoundFailure.THROTTLE_BINARY_MISSING) from None
        try:
            is_valid = configured_path.is_absolute() and resolved_path.is_file()
        except (OSError, RuntimeError, ValueError):
            raise ColdHostBoundError(ColdHostBoundFailure.THROTTLE_BINARY_MISSING) from None
        if not is_valid:
            raise ColdHostBoundError(ColdHostBoundFailure.THROTTLE_BINARY_MISSING)
        return resolved_path

    @staticmethod
    def _read_ascii_bytes(
        path: Path,
        maximum_bytes: int,
        unreadable_failure: ColdHostBoundFailure,
        malformed_failure: ColdHostBoundFailure,
    ) -> str:
        """Return a bounded strictly-ASCII LF-oriented source or a closed failure."""
        try:
            with path.open("rb") as source:
                raw = source.read(maximum_bytes + 1)
        except (OSError, RuntimeError, ValueError):
            raise ColdHostBoundError(unreadable_failure) from None
        if len(raw) > maximum_bytes:
            raise ColdHostBoundError(malformed_failure)
        try:
            return raw.decode("ascii")
        except UnicodeDecodeError:
            raise ColdHostBoundError(malformed_failure) from None

    def _read_all(
        self,
        evidence_root: Path,
        minimum_free_bytes: int,
        disk_failure: ColdHostBoundFailure,
    ) -> tuple[float, tuple[int, str], int, int]:
        """Read all host values and enforce the supplied disk threshold."""
        thermal = self.read_thermal_c()
        throttled = self._read_throttled()
        memory = self.read_mem_available_bytes()
        free = self.read_free_bytes(evidence_root)
        if free < minimum_free_bytes:
            raise ColdHostBoundError(disk_failure)
        return thermal, throttled, memory, free
