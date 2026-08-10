"""Advisor smoke / bake-off harness (E8-S2 manual validation; E8-S4 runner).

Builds the real ``PydanticAIAdvisor`` from environment-loaded ``AdvisorConfig``
(D5 + D18) and runs one realistic ``AdvisorContext`` — grounded in an actual
development-phase telemetry row from a coffee-roaster-mcp live-roast export —
through ``get_recommendation``. Prints the source row, the returned
``RoastDecision`` (or the typed ``AdvisorError`` subclass), and the wall-clock
latency, for N iterations.

**Manual / local only.** This makes live network calls to a configured
provider; it must never run in CI (the hardware-free / no-network guardrail
stays — CI keeps using ``FakeAdvisor``).

Usage (against a local LM Studio, for example)::

    ROASTPILOT_ADVISOR__PROVIDER=openai_compatible \\
    ROASTPILOT_ADVISOR__PROVIDER_BASE_URL=http://127.0.0.1:1234/v1 \\
    ROASTPILOT_ADVISOR__API_KEY_ENV=LMSTUDIO_API_KEY \\
    ROASTPILOT_ADVISOR__MODEL_SLUG=qwen/qwen3.6-35b-a3b \\
    LMSTUDIO_API_KEY=lm-studio \\
    python scripts/advisor_smoke.py --iterations 1

Swap ``ROASTPILOT_ADVISOR__PROVIDER`` / ``MODEL_SLUG`` to compare candidates
for the E8-S4 bake-off — same script, no code change (D18).
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from roastpilot_agent.advisor import (  # noqa: E402
    AdvisorContext,
    AdvisorError,
    PydanticAIAdvisor,
    RoastDecision,
)
from roastpilot_agent.config import AppConfig  # noqa: E402
from roastpilot_agent.models import RoastPhase  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = (
    REPO_ROOT / "tests" / "fixtures" / "live-roast-2026-06-07" / "session-1" / "roast.jsonl"
)
# How far back to look when estimating rate-of-rise from raw temperatures.
_ROR_WINDOW_SECONDS = 60.0
# How many recent telemetry rows to hand the advisor as context.
_RECENT_SAMPLES = 6


def _load(fixture: Path) -> tuple[list[dict[str, Any]], dict[str, float]]:
    """Return (telemetry rows, {event kind -> monotonic_seconds}) for a roast."""
    telemetry: list[dict[str, Any]] = []
    events: dict[str, float] = {}
    for line in fixture.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("type") == "telemetry":
            telemetry.append(row)
        elif row.get("type") == "event":
            events[str(row["kind"])] = float(row["monotonic_seconds"])
    return telemetry, events


def _ror(rows: list[dict[str, Any]], index: int, field: str) -> float | None:
    """Estimate °C/min for ``field`` at ``rows[index]`` over the prior ~60 s."""
    now = rows[index]
    now_t = float(now["monotonic_seconds"])
    for past in reversed(rows[:index]):  # rows strictly before now
        dt = now_t - float(past["monotonic_seconds"])
        if dt >= _ROR_WINDOW_SECONDS:
            return round((float(now[field]) - float(past[field])) / dt * 60.0, 3)
    return None


def build_context(
    fixture: Path, row_offset_seconds: float
) -> tuple[AdvisorContext, dict[str, Any]]:
    """Build a development-phase ``AdvisorContext`` from a real roast.

    Picks the telemetry row closest to ``first_crack + row_offset_seconds`` (a
    point inside development), computes rate-of-rise from the raw temperatures,
    and uses the actual achieved drop temperature as the profile target. Returns
    the context plus the source row for reporting.
    """
    telemetry, events = _load(fixture)
    t0 = events.get("beans_added")
    fc = events.get("first_crack_detected")
    drop = events.get("beans_dropped")
    if t0 is None or fc is None or drop is None:
        raise SystemExit(f"fixture {fixture} lacks beans_added/first_crack/beans_dropped events")

    target_mono = fc + row_offset_seconds
    dev_rows = [r for r in telemetry if fc <= float(r["monotonic_seconds"]) <= drop]
    if not dev_rows:
        raise SystemExit(f"fixture {fixture} has no development-phase telemetry rows")
    row = min(dev_rows, key=lambda r: abs(float(r["monotonic_seconds"]) - target_mono))
    index = telemetry.index(row)
    mono = float(row["monotonic_seconds"])

    drop_row = min(telemetry, key=lambda r: abs(float(r["monotonic_seconds"]) - drop))
    recent = telemetry[max(0, index - _RECENT_SAMPLES + 1) : index + 1]

    context = AdvisorContext(
        phase=RoastPhase.DEVELOPMENT,
        roast_elapsed_seconds=round(mono - t0, 3),
        development_elapsed_seconds=round(mono - fc, 3),
        current_bean_temp_c=float(row["bean_temp_c"]),
        current_env_temp_c=float(row["env_temp_c"]),
        bean_ror_c_per_min=_ror(telemetry, index, "bean_temp_c"),
        env_ror_c_per_min=_ror(telemetry, index, "env_temp_c"),
        target_drop_temp_c=float(drop_row["bean_temp_c"]),
        profile_name=f"{fixture.parent.parent.name}/{fixture.parent.name}",
        recent_telemetry_samples=[
            {
                "monotonic_seconds": float(r["monotonic_seconds"]),
                "bean_temp_c": float(r["bean_temp_c"]),
                "env_temp_c": float(r["env_temp_c"]),
                "heat_level_percent": int(r["heat_level_percent"]),
                "fan_level_percent": int(r["fan_level_percent"]),
            }
            for r in recent
        ],
        first_crack_detected=True,
        first_crack_timestamp_seconds=round(fc - t0, 3),
        # #497: the real roast's ACTUATED heat/fan at the source row — never
        # null. ``post_fc_loop_active`` stays the default False: this fixture
        # predates the deterministic post-FC RoR-taper loop (#405/D88, still
        # flag-off in production), so the recorded levers are advisor-driven,
        # never taper-actuated.
        current_heat_percent=int(row["heat_level_percent"]),
        current_fan_percent=int(row["fan_level_percent"]),
    )
    return context, row


def _print_decision(decision: RoastDecision) -> None:
    print(
        f"  RoastDecision: heat={decision.target_heat}% fan={decision.target_fan}% "
        f"drop={decision.should_drop} confidence={decision.confidence} "
        f"rationale={decision.rationale!r}"
    )


async def run(iterations: int, fixture: Path, row_offset_seconds: float) -> int:
    app_config = AppConfig()
    config = app_config.advisor
    # The LIVE budget a roast actually enforces (D151 dropped it to 5.0 s).
    # Not AdvisorConfig.timeout_seconds, which has no runtime consumer in the
    # agent: bounding the harness by that knob let a candidate returning
    # between the two numbers pass here while every production call timed out
    # (local Codex P2, folded pre-open).
    live_budget = app_config.controller.advisory_timeout_seconds
    # The model this run will genuinely call, resolved the way the advisor
    # resolves it (#747): this fixture is a DEVELOPMENT-phase context, and
    # ``PydanticAIAdvisor`` picks its agent with ``model_for(context.phase)``.
    # Printing the base ``model_slug`` here reported an arm the run was not
    # measuring whenever a phase override existed — the same trap that put a
    # gpt-4o hardware roast on record as a gpt-4.1-mini one. Both are printed,
    # and the shadowing is named, because the base slug still drives the
    # reachability probe and so is not merely noise.
    called = config.model_for(RoastPhase.DEVELOPMENT)
    shadowed = "" if called == config.model_slug else " (SHADOWED by model_slug_by_phase)"
    print("=== advisor smoke / bake-off ===")
    print(
        f"provider={config.provider} model={called!r} "
        f"model_slug={config.model_slug!r}{shadowed} "
        f"base_url={config.provider_base_url!r} "
        f"prompt_version={config.prompt_version} temperature={config.temperature} "
        f"live_advisory_budget={live_budget:g}s"
    )
    # Report whether the key is PRESENT rather than echoing ``api_key_env``.
    # CodeQL flags printing that field as clear-text logging of sensitive data:
    # a false positive on its face (the field holds the env var's NAME, never
    # the key, which is read from ``os.environ`` at call time), but GitHub code
    # scanning does not honour inline CodeQL suppressions, so the choice was
    # between a standing high-severity alert and removing the taint. Removing it
    # costs little and arguably reads better: "cannot authenticate" is answered
    # by whether the key is set, and the var's NAME is already on the /config
    # page and the launcher banner.
    print(f"api_key_present={bool(os.environ.get(config.api_key_env))}")

    context, source_row = build_context(fixture, row_offset_seconds)
    print(
        f"\nsource row: {fixture.relative_to(REPO_ROOT)} "
        f"@ monotonic={source_row['monotonic_seconds']} "
        f"recorded_at_utc={source_row['recorded_at_utc']}"
    )
    print("context:")
    print("  " + context.model_dump_json(indent=2).replace("\n", "\n  "))

    advisor = PydanticAIAdvisor(config)

    latencies: list[float] = []
    failures = 0
    for i in range(1, iterations + 1):
        print(f"\n--- iteration {i}/{iterations} ---")
        started = time.perf_counter()
        try:
            # Bound the call by the budget a ROAST enforces —
            # ControllerConfig.advisory_timeout_seconds, the value the
            # controller wraps the advisory call in. A characterisation run
            # that wants longer sets ROASTPILOT_CONTROLLER__ADVISORY_TIMEOUT_
            # SECONDS, which moves both together. A model that runs long
            # surfaces here as TimeoutError, exactly as it would live, rather
            # than as silently slow advice.
            decision = await asyncio.wait_for(
                advisor.get_recommendation(context), timeout=live_budget
            )
        except TimeoutError:
            elapsed = time.perf_counter() - started
            failures += 1
            print(
                f"  TimeoutError after {elapsed:.2f}s "
                f"(exceeded the live advisory budget of {live_budget:g}s)"
            )
            continue
        except AdvisorError as exc:
            elapsed = time.perf_counter() - started
            failures += 1
            print(f"  {type(exc).__name__}: {exc}")
            print(f"  latency: {elapsed:.2f}s")
            continue
        elapsed = time.perf_counter() - started
        latencies.append(elapsed)
        _print_decision(decision)
        print(f"  latency: {elapsed:.2f}s")

    print(f"\n=== summary: {len(latencies)}/{iterations} ok, {failures} failed ===")
    if latencies:
        print(
            f"latency s: min={min(latencies):.2f} max={max(latencies):.2f} "
            f"mean={sum(latencies) / len(latencies):.2f}"
        )
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=1, help="number of advisory calls")
    parser.add_argument(
        "--fixture", type=Path, default=DEFAULT_FIXTURE, help="live-roast roast.jsonl path"
    )
    parser.add_argument(
        "--offset-seconds",
        type=float,
        default=45.0,
        help="target the development row this many seconds after first crack",
    )
    args = parser.parse_args()
    if not args.fixture.exists():
        parser.error(f"fixture not found: {args.fixture}")
    return asyncio.run(run(args.iterations, args.fixture, args.offset_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
