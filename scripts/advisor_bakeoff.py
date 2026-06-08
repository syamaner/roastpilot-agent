"""Advisor bake-off driver (E8-S4; plan §11.1 → D20).

Runs the candidate slate through the real ``PydanticAIAdvisor`` (D5 + D18)
against the *same* grounded ``AdvisorContext`` at several roast moments, and
records latency / parsed advice / pass-vs-budget per cell. Reuses
``advisor_smoke.build_context`` so the context is identical to the smoke
harness (the 7 Jun live-roast fixture). Candidates differ only by config —
no advisor code changes (D18).

**Manual / local only** — live network calls; never in CI. Reads each
candidate's key from the env var named by its ``key_env`` (e.g. export
``OPENROUTER_API_KEY`` before running); the key never enters config or the
repo.

Usage::

    OPENROUTER_API_KEY=... LMSTUDIO_API_KEY=lm-studio \\
    python scripts/advisor_bakeoff.py --iterations 3 --out /tmp/bakeoff.json
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))  # advisor_smoke
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from advisor_smoke import DEFAULT_FIXTURE, build_context  # noqa: E402

from roastpilot_agent.advisor import AdvisorError, PydanticAIAdvisor  # noqa: E402
from roastpilot_agent.config import AdvisorConfig  # noqa: E402


def fetch_openrouter_pricing() -> dict[str, tuple[float, float]]:
    """Live per-model OpenRouter pricing → {slug: ($/input_token, $/output_token)}.

    Reasoning tokens are billed as completion tokens, so the model's measured
    ``output_tokens`` already includes them — output price covers the tax.
    """
    key = os.environ.get("OPENROUTER_API_KEY", "")
    req = urllib.request.Request(  # noqa: S310 — fixed https endpoint
        "https://openrouter.ai/api/v1/models",
        headers={"Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
        data = cast("dict[str, Any]", json.load(resp))
    pricing: dict[str, tuple[float, float]] = {}
    for model in cast("list[dict[str, Any]]", data.get("data", [])):
        price = cast("dict[str, Any]", model.get("pricing") or {})
        try:
            pricing[str(model["id"])] = (
                float(price.get("prompt", 0) or 0),
                float(price.get("completion", 0) or 0),
            )
        except (TypeError, ValueError):
            continue
    return pricing


OPENROUTER = "https://openrouter.ai/api/v1"
LMSTUDIO = "http://127.0.0.1:1234/v1"

# The slate (E8-S4 prompt). Cloud candidates go through OpenRouter; the local
# baseline points at LM Studio. Slugs verified live on openrouter.ai/models.
# ``tier`` is a neutral price/locality category, not a prediction — the
# outcome lives in the results JSON (latency/gate) and the bake-off doc.
CANDIDATES: list[dict[str, str]] = [
    {
        "label": "qwen3.6-35b-a3b (local, reasoning-off)",
        "tier": "baseline (free, local)",
        "base_url": LMSTUDIO,
        "model": "qwen/qwen3.6-35b-a3b",
        "key_env": "LMSTUDIO_API_KEY",
    },
    {
        "label": "google/gemini-3.5-flash",
        "tier": "cheap cloud",
        "base_url": OPENROUTER,
        "model": "google/gemini-3.5-flash",
        "key_env": "OPENROUTER_API_KEY",
    },
    {
        "label": "anthropic/claude-haiku-4.5",
        "tier": "cheap cloud",
        "base_url": OPENROUTER,
        "model": "anthropic/claude-haiku-4.5",
        "key_env": "OPENROUTER_API_KEY",
    },
    {
        "label": "openai/gpt-5-mini",
        "tier": "cheap cloud",
        "base_url": OPENROUTER,
        "model": "openai/gpt-5-mini",
        "key_env": "OPENROUTER_API_KEY",
    },
    {
        "label": "anthropic/claude-sonnet-4.6",
        "tier": "frontier",
        "base_url": OPENROUTER,
        "model": "anthropic/claude-sonnet-4.6",
        "key_env": "OPENROUTER_API_KEY",
    },
    {
        "label": "anthropic/claude-opus-4.8",
        "tier": "frontier",
        "base_url": OPENROUTER,
        "model": "anthropic/claude-opus-4.8",
        "key_env": "OPENROUTER_API_KEY",
    },
    {
        "label": "openai/gpt-5.5",
        "tier": "frontier",
        "base_url": OPENROUTER,
        "model": "openai/gpt-5.5",
        "key_env": "OPENROUTER_API_KEY",
    },
]

# Development roast moments, seconds after first crack (FC→drop window ≈ 95 s).
MOMENTS: list[tuple[str, float]] = [("early", 10.0), ("mid", 45.0), ("late", 80.0)]

GATE_SECONDS = 10.0  # the controller's tick-aligned advisory budget
MEASURE_TIMEOUT = 90.0  # generous bound so over-budget advice is still captured


def _median_int(values: list[int]) -> int | None:
    return int(statistics.median(values)) if values else None


async def run_cell(
    cand: dict[str, str],
    offset: float,
    iters: int,
    prompt_version: str,
    reasoning_effort: str | None,
    pricing: dict[str, tuple[float, float]],
) -> dict[str, object]:
    context, source_row = build_context(DEFAULT_FIXTURE, offset)
    config = AdvisorConfig(
        provider="openai_compatible",
        provider_base_url=cand["base_url"],
        api_key_env=cand["key_env"],
        model_slug=cand["model"],
        prompt_version=prompt_version,
        reasoning_effort=reasoning_effort,  # type: ignore[arg-type]
    )
    advisor = PydanticAIAdvisor(config)
    iters_out: list[dict[str, Any]] = []
    for _ in range(iters):
        started = time.perf_counter()
        try:
            decision = await asyncio.wait_for(
                advisor.get_recommendation(context), timeout=MEASURE_TIMEOUT
            )
            u = advisor.last_usage
            iters_out.append(
                {
                    "ok": True,
                    "latency": round(time.perf_counter() - started, 3),
                    "decision": decision.model_dump(),
                    "input_tokens": u.input_tokens if u else None,
                    "output_tokens": u.output_tokens if u else None,
                    "reasoning_tokens": u.reasoning_tokens if u else None,
                }
            )
        except (AdvisorError, TimeoutError) as exc:
            iters_out.append(
                {
                    "ok": False,
                    "latency": round(time.perf_counter() - started, 3),
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                }
            )
    ok = [r for r in iters_out if r["ok"]]
    lats = [float(r["latency"]) for r in ok]
    median = round(statistics.median(lats), 2) if lats else None
    decision = next((r["decision"] for r in ok), None)
    in_tok = _median_int([r["input_tokens"] for r in ok if r["input_tokens"] is not None])
    out_tok = _median_int([r["output_tokens"] for r in ok if r["output_tokens"] is not None])
    reas_tok = _median_int([r["reasoning_tokens"] for r in ok if r["reasoning_tokens"] is not None])
    # Cost per call from live OpenRouter pricing; local (LM Studio) is free.
    prompt_price, completion_price = pricing.get(cand["model"], (0.0, 0.0))
    is_local = cand["base_url"] == LMSTUDIO
    cost_usd = (
        None
        if is_local or in_tok is None or out_tok is None
        else round(in_tok * prompt_price + out_tok * completion_price, 6)
    )
    return {
        "source_row_monotonic": float(source_row["monotonic_seconds"]),
        "bean_temp_c": float(source_row["bean_temp_c"]),
        "iterations": iters_out,
        "ok_count": len(lats),
        "latency_median": median,
        "latency_min": round(min(lats), 2) if lats else None,
        "latency_max": round(max(lats), 2) if lats else None,
        "passes_gate": bool(median is not None and median <= GATE_SECONDS),
        "decision": decision,
        "input_tokens_median": in_tok,
        "output_tokens_median": out_tok,
        "reasoning_tokens_median": reas_tok,
        "cost_usd_per_call": cost_usd,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument(
        "--prompt-version", default="v2", help="advisor prompt version (default: v2)"
    )
    parser.add_argument(
        "--reasoning",
        default="default",
        choices=["default", "off", "minimal", "low", "medium", "high"],
        help="reasoning effort for the OpenAI-compatible path (default: provider default)",
    )
    parser.add_argument("--out", type=Path, default=Path("/tmp/bakeoff.json"))
    args = parser.parse_args()

    reasoning_effort = None if args.reasoning == "default" else args.reasoning
    pricing = fetch_openrouter_pricing()
    print(
        f"prompt={args.prompt_version} reasoning={args.reasoning} ({len(pricing)} models priced)",
        flush=True,
    )

    results: list[dict[str, object]] = []
    for cand in CANDIDATES:
        for moment_label, offset in MOMENTS:
            print(f"running {cand['label']} @ {moment_label} (offset {offset}s)…", flush=True)
            cell = await run_cell(
                cand, offset, args.iterations, args.prompt_version, reasoning_effort, pricing
            )
            cell.update(
                {
                    "label": cand["label"],
                    "tier": cand["tier"],
                    "model": cand["model"],
                    "moment": moment_label,
                    "offset": offset,
                    "prompt_version": args.prompt_version,
                    "reasoning": args.reasoning,
                }
            )
            results.append(cell)
            decision = cell["decision"]
            verdict = "PASS" if cell["passes_gate"] else "over-budget"
            if decision is not None:
                d = cast(dict[str, Any], decision)
                cost = cell["cost_usd_per_call"]
                cost_str = f"${cost:.4f}/call" if isinstance(cost, float) else "free"
                print(
                    f"  {verdict} median={cell['latency_median']}s "
                    f"heat={d['target_heat']} fan={d['target_fan']} drop={d['should_drop']} "
                    f"in/out={cell['input_tokens_median']}/{cell['output_tokens_median']}tok "
                    f"reason={cell['reasoning_tokens_median']}tok {cost_str}",
                    flush=True,
                )
            else:
                cells = cast(list[dict[str, Any]], cell["iterations"])
                err = next((r.get("error") for r in cells if not r["ok"]), "?")
                print(f"  FAILED {err}", flush=True)
            args.out.write_text(json.dumps(results, indent=2))
    print(f"\nwrote {len(results)} cells -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
