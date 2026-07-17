# #567 reference-curve three-arm bake-off — result (18 Jul 2026)

**Verdict: NEGATIVE. The reference-curve feature (#567) stays DISABLED and is
PARKED.** The offline gate did its job: `c9` + the same-bean reference, as built,
is a mild regression, not an improvement. No hardware roast is warranted on it.

Harness: `scripts/bakeoff_reference_567.py`. Model `openai/gpt-4o`; 10 held-out
runs across the three qualifying beans (Colombia Excelso Huila ×4, Guatemala El
Durazno ×4, Sumatra Mandheling G1 ×2); 38 post-FC decision ticks per arm; 0
errors; ~$2. (The raw per-tick JSON is intentionally NOT committed — AGENTS.md
forbids checked-in roast logs outside `tests/fixtures/`; this summary + traces
are the record.) Reproduce (needs a valid `OPENROUTER_API_KEY` — note the loading
gotcha below):

```
OPENROUTER_API_KEY="$(grep -E '^OPENROUTER_API_KEY=' .env | cut -d= -f2-)" \
  .venv/bin/python scripts/bakeoff_reference_567.py --max-spend 5 --no-resume \
  --out /tmp/ref567.json --report-md /tmp/ref567.md
```

## The three arms (design note §6.4)

- **Arm 1** — `c8`, no reference.
- **Arm 2** — `c8`, reference present but UNTAUGHT (isolates the raw data, vs arm 1).
- **Arm 3** — `c9`, reference present AND taught (isolates the teaching, vs arm 2).

## Aggregate

| Arm | drop calls | mean drop DTR % | mean drop bean °C | rationale cites the reference |
|---|---|---|---|---|
| 1 — c8, no reference | 10/10 | 12.4 | 190.9 | 0/38 ticks |
| 2 — c8, reference (untaught) | 9/10 | 12.3 | 190.7 | 0/38 ticks |
| 3 — c9, reference (taught) | 10/10 | 10.6 | 189.9 | 3/38 ticks |

## Findings

1. **The reference DATA, untaught, is inert.** Arm 2 vs Arm 1: 8/10 runs give
   byte-identical drop calls, the aggregate is unchanged (12.4 → 12.3 % DTR), and
   the model never cites the reference (0/38). When the prompt does not mention
   the fields, the model ignores the injected 30-point curve. (This cuts against
   the design's "data beats prose" hope — here the data is ignored, not used.)
2. **The `c9` TEACHING moves decisions, in the WRONG direction.** Arm 3 vs Arm 2:
   the drop is pulled EARLIER and SHORTER (12.4 → 10.6 % DTR; 190.9 → 189.9 °C),
   toward under-development — the exact failure the c7/c8/joint-objective lineage
   exists to prevent. Worst case (`f3fc65fa`): drop at DTR 6.5 % / 187 °C, ~11 pp
   short. It still cites the reference only 3/38 times, so it is not reasoning
   *from* the reference — the added prose is perturbing behaviour, the fragile-prose
   failure the whole prompt-testing arc kept surfacing.

## Why (honest caveats)

- **The corpus is a weak teacher.** The Colombia references are all 3★ (mediocre),
  and a "best-rated" reference for a 3★-only bean is still 3★. The feature's value
  hinges on genuinely good references (4–5★); this corpus mostly cannot supply them.
- **n=3 beans / 10 runs / replay-only** — directional, not conclusive (a perfect
  or damning small-set score is a warning, not a verdict).
- **These committed numbers were produced by the pre-review harness.** Post-run
  code review (PR #578) hardened the harness (a prior-roast-only reference filter
  to remove a look-ahead leak, settings-aware checkpoint keying, read-only store
  access, etc.). The result was NOT regenerated (feature parked, no re-spend); the
  negative conclusion is robust to those fixes — same-bean roasts are near-identical
  in quality, so the look-ahead neither systematically favoured nor disfavoured any
  arm, and c9 regressed regardless.
- The deeper signal matches the arc's lesson: the model reasons on **evaluable
  numbers, not a 30-point curve it doesn't parse**. If #567 is ever revisited, the
  promising direction is the *representation* (surface the reference as a labelled
  comparison — "a well-rated batch of this bean dropped at X °C / Y % DTR"), NOT
  more `c9` prose. That is a redesign, not a tweak — hence PARKED, not iterated.

## Operational note (cost the earlier failed run)

The advisor reads `OPENROUTER_API_KEY` straight from `os.environ`
(`advisor.py:_build_model`); nothing loads `.env` for a script. A stale
`OPENROUTER_API_KEY` exported by the shell profile SHADOWS the valid `.env` value,
producing `401 "User not found"` on every call. Always pass the `.env` value
explicitly when running a bake-off script (see the reproduce command above).
