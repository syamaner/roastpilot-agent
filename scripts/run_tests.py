"""Run the repository's pytest lanes through explicit, fixed test-run profiles.

This is a test-only execution and selection interface over native pytest and the
existing marker/lane partition. It is not a qualified speedup and not a complete
reproduction of hosted CI: it is coverage-neutral (no ``--cov``), and coverage,
the Node/npm environment and the runner all differ from CI. It never replaces the
mandatory single-process, all-marker ``python -m pytest`` full gate.

Profiles:

* ``ci``: CI's pull-request/push selections, which omit only the closed
  ``exhaustive_only`` inventory. Default lane ``all``.
* ``local``: the same selections as ``ci``; default lane ``ordinary`` (the fast
  parallel loop, never full-suite or PR proof).
* ``nightly``: CI's schedule/dispatch selections, which include the
  ``exhaustive_only`` inventory. Default lane ``all``.

``--workers`` (default 4) applies only to the ordinary lane; serial, stress and
packaging always run single-process. Each lane runs from the repository root
through the launching interpreter, without a shell, stopping at the first failure.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKERS = 4

_IGNORE_PACKAGING = "--ignore=tests/test_packaging.py"
_ORDINARY_SELECTION = "not serial and not stress"
_FAST_SERIAL_SELECTION = "serial and not stress and not exhaustive_only"
_EXHAUSTIVE_SERIAL_SELECTION = "serial and not stress"
_WORKERS_PATTERN = re.compile(r"[1-9][0-9]*")


class Profile(Enum):
    """A closed test-run profile."""

    CI = "ci"
    LOCAL = "local"
    NIGHTLY = "nightly"


class Lane(Enum):
    """A closed lane choice; ``ALL`` expands to the four lanes in CI job order."""

    ALL = "all"
    ORDINARY = "ordinary"
    SERIAL = "serial"
    STRESS = "stress"
    PACKAGING = "packaging"


_SINGLE_LANES = (Lane.ORDINARY, Lane.SERIAL, Lane.STRESS, Lane.PACKAGING)


@dataclass(frozen=True)
class LanePlan:
    """One lane's exact child argv.

    Attributes:
        lane: The single lane this argv selects.
        argv: The argv executed verbatim, without a shell.
    """

    lane: Lane
    argv: tuple[str, ...]


Runner = Callable[..., subprocess.CompletedProcess[Any]]


def _lane_arguments(profile: Profile, lane: Lane, workers: int) -> tuple[str, ...]:
    """Return the pytest arguments that select one single lane."""
    if lane is Lane.ORDINARY:
        return (
            "-n",
            str(workers),
            "--dist",
            "worksteal",
            "-m",
            _ORDINARY_SELECTION,
            _IGNORE_PACKAGING,
        )
    if lane is Lane.SERIAL:
        selection = (
            _EXHAUSTIVE_SERIAL_SELECTION if profile is Profile.NIGHTLY else _FAST_SERIAL_SELECTION
        )
        return ("-m", selection, _IGNORE_PACKAGING)
    if lane is Lane.STRESS:
        return ("-m", "stress", _IGNORE_PACKAGING)
    assert lane is Lane.PACKAGING, "Lane.ALL is expanded before argument selection"
    return ("tests/test_packaging.py",)


def build_plan(profile: Profile, lane: Lane, workers: int, python: str) -> tuple[LanePlan, ...]:
    """Build the ordered lane plan without I/O or environment reads.

    Args:
        profile: The selection profile.
        lane: A single lane, or ``Lane.ALL`` for ordinary, serial, stress, packaging.
        workers: The xdist worker count, used only by the ordinary lane.
        python: The interpreter placed at ``argv[0]`` of every lane.

    Returns:
        One ``LanePlan`` per selected lane, in execution order.
    """
    lanes = _SINGLE_LANES if lane is Lane.ALL else (lane,)
    return tuple(
        LanePlan(each, (python, "-m", "pytest", *_lane_arguments(profile, each, workers)))
        for each in lanes
    )


def _cwd_line(cwd: Path) -> str:
    """Return the printed working-directory line."""
    return f"run_tests: cwd {shlex.quote(str(cwd))}"


def _lane_line(lane_plan: LanePlan) -> str:
    """Return the printed lane line; the command is ``shlex.join`` of the exact argv."""
    return f"run_tests: lane {lane_plan.lane.value}: {shlex.join(lane_plan.argv)}"


def run_plan(plan: Sequence[LanePlan], *, cwd: Path, runner: Runner | None = None) -> int:
    """Run each lane in order, stopping at the first non-zero child.

    Children inherit stdout, stderr and the environment unchanged, so pytest's own
    skip, xfail and failure reporting stays visible.

    Args:
        plan: The lanes to run, in order.
        cwd: The working directory for every child.
        runner: The subprocess runner; ``None`` uses ``subprocess.run``.

    Returns:
        0 when every child exits 0; otherwise the first failing child's code, with a
        signal-killed child (negative code ``-N``) mapped to ``128 + N``.
    """
    active_runner = subprocess.run if runner is None else runner
    print(_cwd_line(cwd), flush=True)
    for index, lane_plan in enumerate(plan):
        print(_lane_line(lane_plan), flush=True)
        completed = active_runner(list(lane_plan.argv), cwd=cwd, shell=False, check=False)
        code = completed.returncode
        if code != 0:
            remaining = ", ".join(rest.lane.value for rest in plan[index + 1 :]) or "none"
            print(
                f"run_tests: lane {lane_plan.lane.value} exited {code}; not run: {remaining}",
                file=sys.stderr,
                flush=True,
            )
            return 128 - code if code < 0 else code
    print(
        "run_tests: all selected lanes exited 0; this is not the mandatory "
        "single-process full gate",
        flush=True,
    )
    return 0


class _OnceAction(argparse.Action):
    """Store an option's value, rejecting any repeat (even with the same value).

    The parser is rebuilt for every ``main()`` call, so the per-instance flag is
    scoped to one parse.
    """

    _seen = False

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        """Record the option once, or fail the parse on a repeat."""
        if self._seen:
            parser.error(f"{option_string} may be given at most once")
        self._seen = True
        setattr(namespace, self.dest, self.const if self.nargs == 0 else values)


def _parse_workers(token: str) -> int:
    """Parse a positive decimal worker count with no sign, padding or leading zero."""
    if _WORKERS_PATTERN.fullmatch(token) is None:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {token!r}")
    return int(token)


def _parser() -> argparse.ArgumentParser:
    """Build the closed command-line grammar."""
    parser = argparse.ArgumentParser(
        prog="run_tests.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument(
        "--profile",
        action=_OnceAction,
        required=True,
        choices=[profile.value for profile in Profile],
    )
    parser.add_argument(
        "--lane",
        action=_OnceAction,
        choices=[lane.value for lane in Lane],
        help="default: ordinary for local, all for ci and nightly",
    )
    parser.add_argument(
        "--workers",
        action=_OnceAction,
        type=_parse_workers,
        help=f"ordinary-lane xdist workers (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--dry-run",
        action=_OnceAction,
        nargs=0,
        const=True,
        default=False,
        help="print the exact commands without running them",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse and validate the arguments, then dry-run or run the plan.

    All validation completes before anything is printed or spawned; a usage error
    exits 2 via ``argparse``.

    Args:
        argv: The arguments, excluding the program name; ``None`` uses ``sys.argv``.

    Returns:
        The process exit status.
    """
    parser = _parser()
    arguments = parser.parse_args(argv)
    profile = Profile(cast(str, arguments.profile))
    lane_value = cast(str | None, arguments.lane)
    if lane_value is None:
        lane = Lane.ORDINARY if profile is Profile.LOCAL else Lane.ALL
    else:
        lane = Lane(lane_value)
    explicit_workers = cast(int | None, arguments.workers)
    if explicit_workers is not None and lane not in (Lane.ALL, Lane.ORDINARY):
        parser.error(
            f"--workers applies only to the ordinary lane; lane {lane.value} runs single-process"
        )
    if os.environ.get("PYTEST_ADDOPTS", ""):
        parser.error("PYTEST_ADDOPTS is set; unset it so printed commands describe execution")
    workers = DEFAULT_WORKERS if explicit_workers is None else explicit_workers
    plan = build_plan(profile, lane, workers, sys.executable)
    if cast(bool, arguments.dry_run):
        print(_cwd_line(ROOT))
        for lane_plan in plan:
            print(_lane_line(lane_plan))
        return 0
    return run_plan(plan, cwd=ROOT)


if __name__ == "__main__":  # pragma: no cover - runs only as a subprocess, unmeasured there
    raise SystemExit(main())
