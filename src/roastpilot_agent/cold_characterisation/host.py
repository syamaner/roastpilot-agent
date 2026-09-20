"""Fail-closed Linux host-bound readers for cold characterisation.

The module intentionally has no MCP transport or actuator surface.  It reads
host conditions that later slices use to refuse unsafe cold sessions.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Final, Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict

from roastpilot_agent.config import FINITE_NUMERIC_MODEL_CONFIG

HOST_MAX_TEMP_C: Final = 80.0
HOST_MIN_MEM_AVAILABLE_BYTES: Final = 512 * 2**20
HOST_MIN_FREE_BYTES_BEFORE: Final = 2 * 2**30
HOST_MIN_FREE_BYTES_DURING: Final = 1 * 2**30

_MAX_THROTTLE_STDOUT_BYTES: Final = 4096
_THROTTLE_FAILURE_MASK: Final = 0x000F000F
_THERMAL_PATTERN: Final = re.compile(r"-?[0-9]{1,7}")
_THROTTLE_PATTERN: Final = re.compile(r"throttled=0x[0-9a-fA-F]{1,8}")
_MEM_AVAILABLE_PATTERN: Final = re.compile(r"MemAvailable:[ \t]+([0-9]+) kB")
_CHILD_ENV: Final = {"LC_ALL": "C", "PATH": "/usr/bin:/bin"}
_HOST_FINITE_NUMERIC_MODEL_CONFIG: Final[ConfigDict] = ConfigDict(
    frozen=True,
    allow_inf_nan=FINITE_NUMERIC_MODEL_CONFIG.get("allow_inf_nan", False),
)

CommandRunner: TypeAlias = Callable[[list[str]], subprocess.CompletedProcess[bytes]]


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
    vcgencmd_timeout_seconds: float = 5.0


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
        """Return a complete bounded sample for an active cold session."""
        ...


class LinuxHostBoundsReader:
    """Fail-closed Linux implementation of :class:`HostBoundsReader`."""

    def __init__(
        self,
        config: HostBoundsConfig | None = None,
        *,
        command_runner: CommandRunner | None = None,
        platform_name: str | None = None,
    ) -> None:
        """Create a Linux reader with explicit hardware-free test seams.

        Args:
            config: Source locations and command timeout.
            command_runner: Optional command seam receiving only list argv.
            platform_name: Optional platform seam used without mutating ``sys.platform``.

        Raises:
            ColdHostBoundError: If the admitted platform is not Linux.
        """
        admitted_platform = sys.platform if platform_name is None else platform_name
        if admitted_platform != "linux":
            raise ColdHostBoundError(ColdHostBoundFailure.PLATFORM_UNSUPPORTED)
        self._config = config or HostBoundsConfig()
        self._command_runner = command_runner or self._make_default_command_runner()

    def _make_default_command_runner(self) -> CommandRunner:
        """Build the only subprocess-backed runner with a closed invocation."""

        timeout = self._config.vcgencmd_timeout_seconds

        def run_command(argv: list[str]) -> subprocess.CompletedProcess[bytes]:
            return subprocess.run(
                argv,
                check=False,
                cwd=None,
                env=_CHILD_ENV,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
            )

        return run_command

    def read_thermal_c(self) -> float:
        """Read and enforce the Celsius thermal bound."""
        try:
            raw = self._config.thermal_zone_temp_path.read_text(encoding="ascii")
        except OSError as error:
            raise ColdHostBoundError(ColdHostBoundFailure.THERMAL_UNREADABLE) from error
        except UnicodeDecodeError as error:
            raise ColdHostBoundError(ColdHostBoundFailure.THERMAL_MALFORMED) from error
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
        command_path = self._validated_vcgencmd_path()
        try:
            result = self._command_runner([str(command_path), "get_throttled"])
        except subprocess.TimeoutExpired as error:
            raise ColdHostBoundError(ColdHostBoundFailure.THROTTLE_TIMEOUT) from error
        except OSError as error:
            raise ColdHostBoundError(ColdHostBoundFailure.THROTTLE_INVOCATION_FAILED) from error
        if result.returncode != 0:
            raise ColdHostBoundError(ColdHostBoundFailure.THROTTLE_INVOCATION_FAILED)
        stdout = result.stdout
        if len(stdout) > _MAX_THROTTLE_STDOUT_BYTES:
            raise ColdHostBoundError(ColdHostBoundFailure.THROTTLE_OUTPUT_MALFORMED)
        try:
            output = stdout.decode("ascii").strip()
        except UnicodeDecodeError as error:
            raise ColdHostBoundError(ColdHostBoundFailure.THROTTLE_OUTPUT_MALFORMED) from error
        if _THROTTLE_PATTERN.fullmatch(output) is None:
            raise ColdHostBoundError(ColdHostBoundFailure.THROTTLE_OUTPUT_MALFORMED)
        word = int(output.removeprefix("throttled=0x"), 16)
        if word & _THROTTLE_FAILURE_MASK:
            raise ColdHostBoundError(ColdHostBoundFailure.THROTTLE_BITS_SET)
        return word

    def read_mem_available_bytes(self) -> int:
        """Read and enforce the available-memory bound."""
        try:
            content = self._config.meminfo_path.read_text(encoding="ascii")
        except OSError as error:
            raise ColdHostBoundError(ColdHostBoundFailure.MEMINFO_UNREADABLE) from error
        except UnicodeDecodeError as error:
            raise ColdHostBoundError(ColdHostBoundFailure.MEMINFO_MALFORMED) from error
        matches = [
            match.group(1)
            for line in content.splitlines()
            if (match := _MEM_AVAILABLE_PATTERN.fullmatch(line))
        ]
        if len(matches) != 1:
            raise ColdHostBoundError(ColdHostBoundFailure.MEMINFO_MALFORMED)
        available = int(matches[0]) * 1024
        if available < HOST_MIN_MEM_AVAILABLE_BYTES:
            raise ColdHostBoundError(ColdHostBoundFailure.MEMINFO_BELOW_BOUND)
        return available

    def read_free_bytes(self, path: Path) -> int:
        """Read free unprivileged disk space using ``f_bavail``."""
        try:
            filesystem = os.statvfs(path)
        except OSError as error:
            raise ColdHostBoundError(ColdHostBoundFailure.DISK_UNREADABLE) from error
        return filesystem.f_bavail * filesystem.f_frsize

    def check_start_bounds(self, evidence_root: Path) -> None:
        """Enforce every host bound with the stronger start disk threshold."""
        self._read_all(
            evidence_root, HOST_MIN_FREE_BYTES_BEFORE, ColdHostBoundFailure.DISK_BELOW_START_BOUND
        )

    def check_run_bounds(self, evidence_root: Path) -> None:
        """Enforce every host bound with the during-run disk threshold."""
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
            throttled_word_hex=f"0x{throttled:x}",
            mem_available_bytes=memory,
            free_bytes=free,
        )

    def _validated_vcgencmd_path(self) -> Path:
        """Return the configured binary only when it is absolute and regular."""
        configured_path = self._config.vcgencmd_path
        try:
            resolved_path = configured_path.resolve()
        except OSError as error:
            raise ColdHostBoundError(ColdHostBoundFailure.THROTTLE_BINARY_MISSING) from error
        if not configured_path.is_absolute() or not resolved_path.is_file():
            raise ColdHostBoundError(ColdHostBoundFailure.THROTTLE_BINARY_MISSING)
        return resolved_path

    def _read_all(
        self,
        evidence_root: Path,
        minimum_free_bytes: int,
        disk_failure: ColdHostBoundFailure,
    ) -> tuple[float, int, int, int]:
        """Read all host values and enforce the supplied disk threshold."""
        thermal = self.read_thermal_c()
        throttled = self.read_throttled_word()
        memory = self.read_mem_available_bytes()
        free = self.read_free_bytes(evidence_root)
        if free < minimum_free_bytes:
            raise ColdHostBoundError(disk_failure)
        return thermal, throttled, memory, free
