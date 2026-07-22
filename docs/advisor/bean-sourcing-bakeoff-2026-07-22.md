# Bean-sourcing bake-off — post-#590 re-run with reasoning arms (22 Jul 2026)

The #601 re-confirm: the full 8-model default-arm screening re-run on the post-#590
pipeline, plus the off-vs-light reasoning arms, executed through the #601
spend-integrity harness (charge ledger, enforced output cap, exact request
accounting via disabled transport retries, actual-spend breaker at `--max-spend
2.00`; reserve-inclusive ledger total $0.5066 (of which $0.0126 is a disclosed
safety reserve, the rest captured usage), breaker never tripped, zero timeouts,
zero schema failures on any arm).

## Interpretation (screening-grade, N≈9 pages — see the caveat in the report)

- **Default screening, macro F1:** `gpt-5-mini` 0.770 > `gpt-5.6-luna` 0.766 >
  `claude-haiku-4.5` 0.764 > `grok-4.3` 0.753 > `gpt-4.1-mini` 0.729. On
  CombinedScore (which rewards honest abstention), `grok-4.3` 0.620 and `luna`
  0.612 lead; `haiku` falls to last (0.478) on 12 spurious fills despite top
  recall — the recall/faithfulness trade is the roster's clearest axis.
- **The blend pages remain the discriminator:** only `gpt-5-mini` and `haiku` survive both
  blend fixtures; most arms ERR/MIS them (light-arm `luna` fixes one of the
  two, counterculture-big-trouble, and still errors klatch-blue-thunder).
- **Reasoning arms (#601's question):** no schema-adherence effect anywhere —
  0 schema failures on every arm, so the original motivation (35→3 violations on
  a cheap Gemini, pre-#590) is moot on the hardened pipeline; #590's
  preprocessing appears to have solved adherence outright. Quality effects are
  model-dependent and modest: `luna` off→light improves (macro F1 0.686→0.753,
  errors 2→1 — light thinking fixes a blend page); `haiku` off→light is
  byte-identical (0.764→0.764, zero delta on every axis — light thinking does
  nothing for it here); and `gpt-5-mini` at LIGHT effort (0.735) scores BELOW
  its provider-default mandatory reasoning (0.770) while being ~2× cheaper and
  3.5× faster (p50 7.9 s vs 28.0 s).
- **Cost/latency reality check:** on the DEFAULT-arm run, actual usage-priced
  costs ran 1.2–12× the chars/4 estimates (worst: `gpt-5-nano` at 12×,
  provider-default reasoning tokens); the reasoning arms ran 0.59–1.10× (the
  light arms' 4× output budgets over-predict) — the estimate-vs-actual gap the
  #601 harness now measures in both directions.
- **Selection implication (pin unchanged until operator review):** `grok-4.3`
  and `gpt-5.6-luna` lead the honest-abstention ranking at low latency;
  `gpt-5-mini` leads raw F1 at a latency cost; light reasoning is not a
  general win — it is a per-model tuning knob, and for the current pin the
  default arms remain the reference.

One reserve fired across the whole run ($0.0126 on `gpt-5-nano`, disclosed in
the cost table). Run artifacts (results.json, ledger, checkpoint) are session
files, not committed, per repo data rules; this document is the durable record.

---

## Sweep 1 — default arms (the 8-model post-#590 re-screening)

# #588 bean-sourcing extraction bake-off

- models scored: 8
- corpus pages: 9

## Per-model headline (macro F1 is the model-choice headline; latency is the cost/latency tie-break; schema F/R = schema failures/recovered, #601 F6)

| Model | COR | PAR | INC | MIS | ABS-COR | SPU | ERR | Recall | Faithful | Abstain | micro F1 | macro F1 | Combined | latency p50/p95 (s) | schema F/R |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| `openai/gpt-5-nano` | 33 | 8 | 0 | 21 | 17 | 0 | 11 | 0.597 | 0.902 | 1.000 | 0.718 | 0.635 | 0.576 | 25.9 / 39.9 | 0/0 |
| `x-ai/grok-4.3` | 37 | 7 | 0 | 18 | 17 | 0 | 11 | 0.653 | 0.920 | 1.000 | 0.764 | 0.753 | 0.620 | 7.4 / 8.4 | 0/0 |
| `google/gemini-3.1-flash-lite` | 36 | 10 | 3 | 13 | 17 | 6 | 5 | 0.661 | 0.745 | 0.739 | 0.701 | 0.684 | 0.494 | 1.2 / 1.8 | 0/0 |
| `openai/gpt-5-mini` | 40 | 11 | 4 | 7 | 25 | 3 | 0 | 0.734 | 0.784 | 0.893 | 0.758 | 0.770 | 0.589 | 28.0 / 39.0 | 0/0 |
| `openai/gpt-4.1-mini` | 38 | 9 | 1 | 14 | 18 | 4 | 6 | 0.685 | 0.817 | 0.818 | 0.746 | 0.729 | 0.560 | 3.1 / 3.8 | 0/0 |
| `anthropic/claude-haiku-4.5` | 42 | 12 | 2 | 6 | 16 | 12 | 0 | 0.774 | 0.706 | 0.571 | 0.738 | 0.764 | 0.478 | 3.9 / 4.5 | 0/0 |
| `openai/gpt-5.6-luna` | 38 | 9 | 1 | 14 | 22 | 1 | 5 | 0.685 | 0.867 | 0.957 | 0.766 | 0.766 | 0.612 | 4.8 / 5.9 | 0/0 |
| `openai/gpt-4o` | 34 | 4 | 1 | 23 | 16 | 1 | 11 | 0.581 | 0.900 | 0.941 | 0.706 | 0.656 | 0.538 | 2.4 / 3.0 | 0/0 |

## Wilson intervals (indicative only, section 5.2 -- ignores within-page clustering, so it OVERSTATES certainty like McNemar; the bootstrap above is primary)

| Model | COR / trials | proportion | 95% Wilson CI |
|---|--:|--:|--:|
| `openai/gpt-5-nano` | 33/54 | 0.611 | [0.478, 0.730] |
| `x-ai/grok-4.3` | 37/55 | 0.673 | [0.541, 0.782] |
| `google/gemini-3.1-flash-lite` | 36/52 | 0.692 | [0.557, 0.801] |
| `openai/gpt-5-mini` | 40/51 | 0.784 | [0.654, 0.875] |
| `openai/gpt-4.1-mini` | 38/53 | 0.717 | [0.584, 0.820] |
| `anthropic/claude-haiku-4.5` | 42/50 | 0.840 | [0.715, 0.917] |
| `openai/gpt-5.6-luna` | 38/53 | 0.717 | [0.584, 0.820] |
| `openai/gpt-4o` | 34/58 | 0.586 | [0.458, 0.704] |

### `openai/gpt-5-nano` -- per-page outcomes

| Page | name | origin | region | farm | variety | process | species | altitude | tasting_notes | is_blend | err |
|---|---|---|---|---|---|---|---|---|---|---|---|
| cbc-costa-rica-laminita-tarrazu | COR | COR | MIS | COR | COR | COR | ABS_COR | MIS | PAR | MIS |  |
| cbc-dominican-republic-ramirez-honey | COR | COR | COR | MIS | COR | COR | ABS_COR | COR | ABS_COR | ABS_COR |  |
| counterculture-big-trouble-blend | MIS | MIS | ERR | ERR | ERR | MIS | ERR | ERR | MIS | MIS | yes |
| counterculture-concepcion-huista | COR | COR | MIS | PAR | MIS | MIS | ABS_COR | MIS | PAR | ABS_COR |  |
| klatch-blue-thunder-blend | MIS | ERR | ERR | ERR | ERR | MIS | ERR | ERR | MIS | MIS | yes |
| klatch-el-salvador-jasal-geisha-anaerobic | COR | COR | MIS | COR | PAR | COR | ABS_COR | ABS_COR | PAR | ABS_COR |  |
| onyx-colombia-jairo-arcila-rose | COR | COR | ABS_COR | COR | COR | COR | ABS_COR | COR | PAR | ABS_COR |  |
| onyx-ecuador-la-papaya-typica | COR | COR | MIS | MIS | COR | COR | ABS_COR | COR | PAR | ABS_COR |  |
| onyx-monarch-blend | COR | COR | ABS_COR | ABS_COR | COR | COR | ABS_COR | COR | PAR | MIS |  |

### `x-ai/grok-4.3` -- per-page outcomes

| Page | name | origin | region | farm | variety | process | species | altitude | tasting_notes | is_blend | err |
|---|---|---|---|---|---|---|---|---|---|---|---|
| cbc-costa-rica-laminita-tarrazu | COR | COR | COR | COR | COR | COR | ABS_COR | MIS | PAR | COR |  |
| cbc-dominican-republic-ramirez-honey | COR | COR | COR | MIS | COR | COR | ABS_COR | COR | ABS_COR | ABS_COR |  |
| counterculture-big-trouble-blend | MIS | MIS | ERR | ERR | ERR | MIS | ERR | ERR | MIS | MIS | yes |
| counterculture-concepcion-huista | COR | COR | COR | PAR | MIS | MIS | ABS_COR | MIS | PAR | ABS_COR |  |
| klatch-blue-thunder-blend | MIS | ERR | ERR | ERR | ERR | MIS | ERR | ERR | MIS | MIS | yes |
| klatch-el-salvador-jasal-geisha-anaerobic | COR | COR | MIS | COR | PAR | COR | ABS_COR | ABS_COR | PAR | ABS_COR |  |
| onyx-colombia-jairo-arcila-rose | COR | COR | ABS_COR | COR | COR | COR | ABS_COR | COR | PAR | ABS_COR |  |
| onyx-ecuador-la-papaya-typica | COR | COR | MIS | MIS | COR | COR | ABS_COR | COR | COR | ABS_COR |  |
| onyx-monarch-blend | COR | COR | ABS_COR | ABS_COR | MIS | COR | ABS_COR | COR | PAR | COR |  |

### `google/gemini-3.1-flash-lite` -- per-page outcomes

| Page | name | origin | region | farm | variety | process | species | altitude | tasting_notes | is_blend | err |
|---|---|---|---|---|---|---|---|---|---|---|---|
| cbc-costa-rica-laminita-tarrazu | COR | COR | PAR | COR | COR | COR | ABS_COR | MIS | COR | COR |  |
| cbc-dominican-republic-ramirez-honey | COR | COR | PAR | MIS | COR | COR | ABS_COR | COR | ABS_COR | ABS_COR |  |
| counterculture-big-trouble-blend | MIS | MIS | ERR | ERR | ERR | MIS | ERR | ERR | MIS | MIS | yes |
| counterculture-concepcion-huista | COR | COR | MIS | PAR | MIS | MIS | ABS_COR | MIS | PAR | ABS_COR |  |
| klatch-blue-thunder-blend | COR | SPU | ABS_COR | ABS_COR | ABS_COR | INC | ABS_COR | ABS_COR | PAR | COR |  |
| klatch-el-salvador-jasal-geisha-anaerobic | COR | COR | MIS | INC | PAR | COR | ABS_COR | ABS_COR | PAR | ABS_COR |  |
| onyx-colombia-jairo-arcila-rose | COR | COR | ABS_COR | COR | COR | COR | SPU | COR | PAR | SPU |  |
| onyx-ecuador-la-papaya-typica | COR | COR | MIS | INC | COR | COR | SPU | COR | PAR | SPU |  |
| onyx-monarch-blend | COR | COR | ABS_COR | ABS_COR | COR | COR | SPU | COR | PAR | COR |  |

### `openai/gpt-5-mini` -- per-page outcomes

| Page | name | origin | region | farm | variety | process | species | altitude | tasting_notes | is_blend | err |
|---|---|---|---|---|---|---|---|---|---|---|---|
| cbc-costa-rica-laminita-tarrazu | COR | COR | COR | COR | COR | COR | ABS_COR | MIS | PAR | COR |  |
| cbc-dominican-republic-ramirez-honey | COR | COR | COR | MIS | COR | COR | ABS_COR | COR | SPU | ABS_COR |  |
| counterculture-big-trouble-blend | COR | PAR | ABS_COR | SPU | ABS_COR | COR | ABS_COR | ABS_COR | PAR | COR |  |
| counterculture-concepcion-huista | COR | COR | COR | PAR | MIS | MIS | ABS_COR | MIS | PAR | ABS_COR |  |
| klatch-blue-thunder-blend | COR | SPU | ABS_COR | ABS_COR | ABS_COR | INC | ABS_COR | ABS_COR | PAR | COR |  |
| klatch-el-salvador-jasal-geisha-anaerobic | COR | COR | MIS | COR | PAR | COR | ABS_COR | ABS_COR | PAR | ABS_COR |  |
| onyx-colombia-jairo-arcila-rose | COR | COR | ABS_COR | COR | COR | INC | ABS_COR | COR | PAR | ABS_COR |  |
| onyx-ecuador-la-papaya-typica | INC | COR | MIS | INC | COR | COR | ABS_COR | COR | PAR | ABS_COR |  |
| onyx-monarch-blend | COR | COR | ABS_COR | ABS_COR | COR | COR | ABS_COR | COR | PAR | COR |  |

### `openai/gpt-4.1-mini` -- per-page outcomes

| Page | name | origin | region | farm | variety | process | species | altitude | tasting_notes | is_blend | err |
|---|---|---|---|---|---|---|---|---|---|---|---|
| cbc-costa-rica-laminita-tarrazu | COR | COR | COR | COR | COR | COR | ABS_COR | MIS | PAR | COR |  |
| cbc-dominican-republic-ramirez-honey | COR | COR | COR | MIS | COR | COR | ABS_COR | COR | SPU | ABS_COR |  |
| counterculture-big-trouble-blend | COR | INC | ABS_COR | ABS_COR | ABS_COR | COR | ABS_COR | ABS_COR | PAR | COR |  |
| counterculture-concepcion-huista | COR | COR | MIS | PAR | MIS | MIS | ABS_COR | MIS | PAR | ABS_COR |  |
| klatch-blue-thunder-blend | MIS | ERR | ERR | ERR | ERR | MIS | ERR | ERR | MIS | MIS | yes |
| klatch-el-salvador-jasal-geisha-anaerobic | PAR | COR | MIS | COR | COR | COR | ABS_COR | ABS_COR | PAR | ABS_COR |  |
| onyx-colombia-jairo-arcila-rose | COR | COR | ABS_COR | COR | COR | COR | SPU | COR | PAR | SPU |  |
| onyx-ecuador-la-papaya-typica | COR | COR | MIS | MIS | COR | COR | ABS_COR | COR | PAR | SPU |  |
| onyx-monarch-blend | COR | COR | ABS_COR | ABS_COR | COR | COR | ABS_COR | COR | PAR | MIS |  |

### `anthropic/claude-haiku-4.5` -- per-page outcomes

| Page | name | origin | region | farm | variety | process | species | altitude | tasting_notes | is_blend | err |
|---|---|---|---|---|---|---|---|---|---|---|---|
| cbc-costa-rica-laminita-tarrazu | COR | COR | COR | COR | COR | COR | SPU | MIS | PAR | COR |  |
| cbc-dominican-republic-ramirez-honey | COR | COR | COR | COR | COR | COR | SPU | COR | SPU | SPU |  |
| counterculture-big-trouble-blend | COR | PAR | ABS_COR | ABS_COR | ABS_COR | COR | ABS_COR | ABS_COR | PAR | COR |  |
| counterculture-concepcion-huista | COR | COR | COR | PAR | MIS | MIS | ABS_COR | MIS | PAR | ABS_COR |  |
| klatch-blue-thunder-blend | COR | SPU | SPU | ABS_COR | ABS_COR | INC | ABS_COR | ABS_COR | PAR | COR |  |
| klatch-el-salvador-jasal-geisha-anaerobic | COR | COR | MIS | COR | PAR | COR | SPU | ABS_COR | PAR | SPU |  |
| onyx-colombia-jairo-arcila-rose | COR | COR | ABS_COR | COR | COR | INC | SPU | COR | PAR | SPU |  |
| onyx-ecuador-la-papaya-typica | COR | COR | MIS | PAR | COR | COR | SPU | COR | PAR | SPU |  |
| onyx-monarch-blend | COR | COR | ABS_COR | ABS_COR | COR | COR | ABS_COR | COR | PAR | COR |  |

### `openai/gpt-5.6-luna` -- per-page outcomes

| Page | name | origin | region | farm | variety | process | species | altitude | tasting_notes | is_blend | err |
|---|---|---|---|---|---|---|---|---|---|---|---|
| cbc-costa-rica-laminita-tarrazu | COR | COR | COR | COR | COR | COR | ABS_COR | MIS | PAR | COR |  |
| cbc-dominican-republic-ramirez-honey | COR | COR | COR | MIS | COR | COR | ABS_COR | COR | ABS_COR | ABS_COR |  |
| counterculture-big-trouble-blend | MIS | MIS | ERR | ERR | ERR | MIS | ERR | ERR | MIS | MIS | yes |
| counterculture-concepcion-huista | COR | COR | COR | PAR | MIS | MIS | ABS_COR | MIS | PAR | ABS_COR |  |
| klatch-blue-thunder-blend | COR | SPU | ABS_COR | ABS_COR | ABS_COR | INC | ABS_COR | ABS_COR | PAR | COR |  |
| klatch-el-salvador-jasal-geisha-anaerobic | COR | COR | MIS | COR | PAR | COR | ABS_COR | ABS_COR | PAR | ABS_COR |  |
| onyx-colombia-jairo-arcila-rose | COR | COR | ABS_COR | COR | COR | COR | ABS_COR | COR | PAR | ABS_COR |  |
| onyx-ecuador-la-papaya-typica | COR | COR | MIS | MIS | COR | COR | ABS_COR | COR | PAR | ABS_COR |  |
| onyx-monarch-blend | COR | COR | ABS_COR | ABS_COR | MIS | COR | ABS_COR | COR | PAR | COR |  |

### `openai/gpt-4o` -- per-page outcomes

| Page | name | origin | region | farm | variety | process | species | altitude | tasting_notes | is_blend | err |
|---|---|---|---|---|---|---|---|---|---|---|---|
| cbc-costa-rica-laminita-tarrazu | COR | COR | COR | MIS | COR | COR | ABS_COR | MIS | PAR | COR |  |
| cbc-dominican-republic-ramirez-honey | COR | COR | COR | MIS | COR | COR | ABS_COR | MIS | ABS_COR | ABS_COR |  |
| counterculture-big-trouble-blend | MIS | MIS | ERR | ERR | ERR | MIS | ERR | ERR | MIS | MIS | yes |
| counterculture-concepcion-huista | COR | COR | COR | MIS | MIS | MIS | ABS_COR | MIS | MIS | ABS_COR |  |
| klatch-blue-thunder-blend | MIS | ERR | ERR | ERR | ERR | MIS | ERR | ERR | MIS | MIS | yes |
| klatch-el-salvador-jasal-geisha-anaerobic | COR | COR | MIS | INC | COR | COR | ABS_COR | ABS_COR | PAR | SPU |  |
| onyx-colombia-jairo-arcila-rose | COR | COR | ABS_COR | COR | COR | COR | ABS_COR | COR | PAR | ABS_COR |  |
| onyx-ecuador-la-papaya-typica | COR | COR | MIS | MIS | COR | COR | ABS_COR | COR | COR | ABS_COR |  |
| onyx-monarch-blend | COR | COR | ABS_COR | ABS_COR | MIS | COR | ABS_COR | COR | PAR | MIS |  |

## Evidence-quote capture (#612) -- quote capture/authenticity rates, NOT certification: every typed-field citation VALUE gate stays permanently parked (#590); these counts describe what the extractor captured/tagged, not whether the value is correct

| Model | pages scored | field | captured | no evidence | on_page rate |
|---|--:|---|--:|--:|--:|
| `openai/gpt-5-nano` | 7 | processing | 6 | 1 | 0.381 |
|  |  | bean_species | 0 | 7 |  |
|  |  | altitude_m | 4 | 3 |  |
|  |  | is_blend | 0 | 7 |  |
| `x-ai/grok-4.3` | 7 | processing | 4 | 3 | 0.378 |
|  |  | bean_species | 0 | 7 |  |
|  |  | altitude_m | 3 | 4 |  |
|  |  | is_blend | 2 | 5 |  |
| `google/gemini-3.1-flash-lite` | 8 | processing | 7 | 1 | 0.331 |
|  |  | bean_species | 3 | 5 |  |
|  |  | altitude_m | 4 | 4 |  |
|  |  | is_blend | 1 | 7 |  |
| `openai/gpt-5-mini` | 9 | processing | 8 | 1 | 0.339 |
|  |  | bean_species | 0 | 9 |  |
|  |  | altitude_m | 4 | 5 |  |
|  |  | is_blend | 4 | 5 |  |
| `openai/gpt-4.1-mini` | 8 | processing | 4 | 4 | 0.368 |
|  |  | bean_species | 1 | 7 |  |
|  |  | altitude_m | 3 | 5 |  |
|  |  | is_blend | 3 | 5 |  |
| `anthropic/claude-haiku-4.5` | 9 | processing | 6 | 3 | 0.331 |
|  |  | bean_species | 2 | 7 |  |
|  |  | altitude_m | 4 | 5 |  |
|  |  | is_blend | 7 | 2 |  |
| `openai/gpt-5.6-luna` | 8 | processing | 5 | 3 | 0.355 |
|  |  | bean_species | 0 | 8 |  |
|  |  | altitude_m | 3 | 5 |  |
|  |  | is_blend | 2 | 6 |  |
| `openai/gpt-4o` | 7 | processing | 6 | 1 | 0.368 |
|  |  | bean_species | 0 | 7 |  |
|  |  | altitude_m | 3 | 4 |  |
|  |  | is_blend | 1 | 6 |  |

## Pairwise significance (ALL pairs, section 5.2) -- every comparison the selection relies on is generated here, not just one model versus the rest

- `openai/gpt-5-nano` vs `x-ai/grok-4.3`: CombinedScore gap -0.044 [-0.095, -0.006] (page-clustered bootstrap -- PRIMARY); recall gap -0.056 [-0.116, -0.008]; faithfulness (precision) gap -0.018 [-0.042, -0.001]; abstention gap +0.000 [0.000, 0.000]; McNemar exact p=0.2188 (secondary, indicative).
- `openai/gpt-5-nano` vs `google/gemini-3.1-flash-lite`: CombinedScore gap +0.082 [-0.036, 0.190] (page-clustered bootstrap -- PRIMARY); recall gap -0.065 [-0.183, 0.032]; faithfulness (precision) gap +0.157 [0.067, 0.248]; abstention gap +0.261 [0.077, 0.500]; McNemar exact p=0.6291 (secondary, indicative).
- `openai/gpt-5-nano` vs `openai/gpt-5-mini`: CombinedScore gap -0.013 [-0.130, 0.088] (page-clustered bootstrap -- PRIMARY); recall gap -0.137 [-0.324, 0.008]; faithfulness (precision) gap +0.118 [0.033, 0.217]; abstention gap +0.107 [0.000, 0.182]; McNemar exact p=0.0015 (secondary, indicative).
- `openai/gpt-5-nano` vs `openai/gpt-4.1-mini`: CombinedScore gap +0.016 [-0.117, 0.133] (page-clustered bootstrap -- PRIMARY); recall gap -0.089 [-0.230, 0.000]; faithfulness (precision) gap +0.085 [0.015, 0.154]; abstention gap +0.182 [0.000, 0.391]; McNemar exact p=0.2101 (secondary, indicative).
- `openai/gpt-5-nano` vs `anthropic/claude-haiku-4.5`: CombinedScore gap +0.098 [-0.092, 0.261] (page-clustered bootstrap -- PRIMARY); recall gap -0.177 [-0.347, -0.051]; faithfulness (precision) gap +0.197 [0.097, 0.293]; abstention gap +0.429 [0.206, 0.720]; McNemar exact p=0.2005 (secondary, indicative).
- `openai/gpt-5-nano` vs `openai/gpt-5.6-luna`: CombinedScore gap -0.036 [-0.093, 0.004] (page-clustered bootstrap -- PRIMARY); recall gap -0.089 [-0.195, 0.000]; faithfulness (precision) gap +0.035 [-0.015, 0.135]; abstention gap +0.043 [0.000, 0.103]; McNemar exact p=0.0063 (secondary, indicative).
- `openai/gpt-5-nano` vs `openai/gpt-4o`: CombinedScore gap +0.038 [-0.028, 0.113] (page-clustered bootstrap -- PRIMARY); recall gap +0.016 [-0.040, 0.076]; faithfulness (precision) gap +0.002 [-0.090, 0.090]; abstention gap +0.059 [0.000, 0.176]; McNemar exact p=1.0000 (secondary, indicative).
- `x-ai/grok-4.3` vs `google/gemini-3.1-flash-lite`: CombinedScore gap +0.126 [0.033, 0.218] (page-clustered bootstrap -- PRIMARY); recall gap -0.008 [-0.130, 0.074]; faithfulness (precision) gap +0.175 [0.080, 0.279]; abstention gap +0.261 [0.077, 0.500]; McNemar exact p=1.0000 (secondary, indicative).
- `x-ai/grok-4.3` vs `openai/gpt-5-mini`: CombinedScore gap +0.031 [-0.079, 0.118] (page-clustered bootstrap -- PRIMARY); recall gap -0.081 [-0.277, 0.057]; faithfulness (precision) gap +0.136 [0.046, 0.245]; abstention gap +0.107 [0.000, 0.182]; McNemar exact p=0.0192 (secondary, indicative).
- `x-ai/grok-4.3` vs `openai/gpt-4.1-mini`: CombinedScore gap +0.061 [-0.064, 0.160] (page-clustered bootstrap -- PRIMARY); recall gap -0.032 [-0.178, 0.051]; faithfulness (precision) gap +0.103 [0.032, 0.176]; abstention gap +0.182 [0.000, 0.391]; McNemar exact p=0.8145 (secondary, indicative).
- `x-ai/grok-4.3` vs `anthropic/claude-haiku-4.5`: CombinedScore gap +0.142 [-0.045, 0.289] (page-clustered bootstrap -- PRIMARY); recall gap -0.121 [-0.307, 0.000]; faithfulness (precision) gap +0.215 [0.115, 0.313]; abstention gap +0.429 [0.206, 0.720]; McNemar exact p=0.5716 (secondary, indicative).
- `x-ai/grok-4.3` vs `openai/gpt-5.6-luna`: CombinedScore gap +0.008 [-0.019, 0.021] (page-clustered bootstrap -- PRIMARY); recall gap -0.032 [-0.136, 0.022]; faithfulness (precision) gap +0.053 [0.000, 0.154]; abstention gap +0.043 [0.000, 0.103]; McNemar exact p=0.0703 (secondary, indicative).
- `x-ai/grok-4.3` vs `openai/gpt-4o`: CombinedScore gap +0.082 [0.031, 0.138] (page-clustered bootstrap -- PRIMARY); recall gap +0.073 [0.032, 0.110]; faithfulness (precision) gap +0.020 [-0.061, 0.100]; abstention gap +0.059 [0.000, 0.176]; McNemar exact p=0.2188 (secondary, indicative).
- `google/gemini-3.1-flash-lite` vs `openai/gpt-5-mini`: CombinedScore gap -0.095 [-0.172, -0.022] (page-clustered bootstrap -- PRIMARY); recall gap -0.073 [-0.241, 0.042]; faithfulness (precision) gap -0.039 [-0.084, 0.017]; abstention gap -0.154 [-0.433, 0.069]; McNemar exact p=0.0118 (secondary, indicative).
- `google/gemini-3.1-flash-lite` vs `openai/gpt-4.1-mini`: CombinedScore gap -0.065 [-0.161, 0.012] (page-clustered bootstrap -- PRIMARY); recall gap -0.024 [-0.185, 0.122]; faithfulness (precision) gap -0.072 [-0.151, 0.006]; abstention gap -0.079 [-0.276, 0.098]; McNemar exact p=0.6900 (secondary, indicative).
- `google/gemini-3.1-flash-lite` vs `anthropic/claude-haiku-4.5`: CombinedScore gap +0.016 [-0.136, 0.143] (page-clustered bootstrap -- PRIMARY); recall gap -0.113 [-0.272, 0.000]; faithfulness (precision) gap +0.040 [-0.029, 0.105]; abstention gap +0.168 [-0.133, 0.500]; McNemar exact p=0.4049 (secondary, indicative).
- `google/gemini-3.1-flash-lite` vs `openai/gpt-5.6-luna`: CombinedScore gap -0.118 [-0.200, -0.041] (page-clustered bootstrap -- PRIMARY); recall gap -0.024 [-0.076, 0.032]; faithfulness (precision) gap -0.122 [-0.198, -0.047]; abstention gap -0.217 [-0.500, -0.029]; McNemar exact p=0.0654 (secondary, indicative).
- `google/gemini-3.1-flash-lite` vs `openai/gpt-4o`: CombinedScore gap -0.044 [-0.169, 0.070] (page-clustered bootstrap -- PRIMARY); recall gap +0.081 [-0.008, 0.198]; faithfulness (precision) gap -0.155 [-0.287, -0.041]; abstention gap -0.202 [-0.478, 0.056]; McNemar exact p=0.6776 (secondary, indicative).
- `openai/gpt-5-mini` vs `openai/gpt-4.1-mini`: CombinedScore gap +0.029 [-0.011, 0.079] (page-clustered bootstrap -- PRIMARY); recall gap +0.048 [-0.041, 0.167]; faithfulness (precision) gap -0.033 [-0.107, 0.025]; abstention gap +0.075 [-0.100, 0.318]; McNemar exact p=0.0490 (secondary, indicative).
- `openai/gpt-5-mini` vs `anthropic/claude-haiku-4.5`: CombinedScore gap +0.111 [0.017, 0.200] (page-clustered bootstrap -- PRIMARY); recall gap -0.040 [-0.094, 0.000]; faithfulness (precision) gap +0.079 [0.011, 0.137]; abstention gap +0.321 [0.086, 0.632]; McNemar exact p=0.0923 (secondary, indicative).
- `openai/gpt-5-mini` vs `openai/gpt-5.6-luna`: CombinedScore gap -0.023 [-0.100, 0.071] (page-clustered bootstrap -- PRIMARY); recall gap +0.048 [-0.061, 0.217]; faithfulness (precision) gap -0.083 [-0.159, -0.014]; abstention gap -0.064 [-0.156, 0.000]; McNemar exact p=0.2266 (secondary, indicative).
- `openai/gpt-5-mini` vs `openai/gpt-4o`: CombinedScore gap +0.051 [-0.066, 0.177] (page-clustered bootstrap -- PRIMARY); recall gap +0.153 [0.000, 0.340]; faithfulness (precision) gap -0.116 [-0.260, 0.015]; abstention gap -0.048 [-0.174, 0.105]; McNemar exact p=0.0041 (secondary, indicative).
- `openai/gpt-4.1-mini` vs `anthropic/claude-haiku-4.5`: CombinedScore gap +0.082 [-0.011, 0.170] (page-clustered bootstrap -- PRIMARY); recall gap -0.089 [-0.196, -0.007]; faithfulness (precision) gap +0.111 [0.037, 0.191]; abstention gap +0.247 [0.074, 0.476]; McNemar exact p=0.8145 (secondary, indicative).
- `openai/gpt-4.1-mini` vs `openai/gpt-5.6-luna`: CombinedScore gap -0.052 [-0.148, 0.060] (page-clustered bootstrap -- PRIMARY); recall gap +0.000 [-0.139, 0.158]; faithfulness (precision) gap -0.050 [-0.137, 0.061]; abstention gap -0.138 [-0.362, 0.042]; McNemar exact p=0.5413 (secondary, indicative).
- `openai/gpt-4.1-mini` vs `openai/gpt-4o`: CombinedScore gap +0.022 [-0.111, 0.163] (page-clustered bootstrap -- PRIMARY); recall gap +0.105 [0.015, 0.241]; faithfulness (precision) gap -0.083 [-0.199, 0.042]; abstention gap -0.123 [-0.381, 0.118]; McNemar exact p=0.2632 (secondary, indicative).
- `anthropic/claude-haiku-4.5` vs `openai/gpt-5.6-luna`: CombinedScore gap -0.134 [-0.278, 0.039] (page-clustered bootstrap -- PRIMARY); recall gap +0.089 [-0.016, 0.250]; faithfulness (precision) gap -0.161 [-0.236, -0.078]; abstention gap -0.385 [-0.714, -0.153]; McNemar exact p=0.8318 (secondary, indicative).
- `anthropic/claude-haiku-4.5` vs `openai/gpt-4o`: CombinedScore gap -0.060 [-0.222, 0.125] (page-clustered bootstrap -- PRIMARY); recall gap +0.194 [0.060, 0.368]; faithfulness (precision) gap -0.194 [-0.316, -0.078]; abstention gap -0.370 [-0.681, -0.131]; McNemar exact p=0.2153 (secondary, indicative).
- `openai/gpt-5.6-luna` vs `openai/gpt-4o`: CombinedScore gap +0.074 [0.019, 0.138] (page-clustered bootstrap -- PRIMARY); recall gap +0.105 [0.031, 0.207]; faithfulness (precision) gap -0.033 [-0.168, 0.070]; abstention gap +0.015 [-0.095, 0.150]; McNemar exact p=0.0129 (secondary, indicative).

## Cost (estimated spend incurred this invocation)

**~$0.1846 ESTIMATED SPEND INCURRED** this invocation, on 8 newly-called model(s): `openai/gpt-5-nano`, `x-ai/grok-4.3`, `google/gemini-3.1-flash-lite`, `openai/gpt-5-mini`, `openai/gpt-4.1-mini`, `anthropic/claude-haiku-4.5`, `openai/gpt-5.6-luna`, `openai/gpt-4o`. A real (paid) call WAS made for each -- but see the note below: this is still this harness's cost ESTIMATE, never a verified OpenRouter billing amount.

| Model | in tok | out tok | est. USD (full corpus, 1 pass) | usage-priced USD (list price) | of which reserved | status |
|---|--:|--:|--:|--:|--:|---|
| `openai/gpt-5-nano` | 23386 | 1980 | $0.0020 | $0.0243 | $0.0126 | spend incurred (est.) |
| `x-ai/grok-4.3` | 23386 | 1980 | $0.0057 | $0.0098 | $0.0000 | spend incurred (est.) |
| `google/gemini-3.1-flash-lite` | 23386 | 1980 | $0.0078 | $0.0098 | $0.0000 | spend incurred (est.) |
| `openai/gpt-5-mini` | 23386 | 1980 | $0.0098 | $0.0404 | $0.0000 | spend incurred (est.) |
| `openai/gpt-4.1-mini` | 23386 | 1980 | $0.0125 | $0.0145 | $0.0000 | spend incurred (est.) |
| `anthropic/claude-haiku-4.5` | 23386 | 1980 | $0.0333 | $0.0574 | $0.0000 | spend incurred (est.) |
| `openai/gpt-5.6-luna` | 23386 | 1980 | $0.0353 | $0.0465 | $0.0000 | spend incurred (est.) |
| `openai/gpt-4o` | 23386 | 1980 | $0.0783 | $0.0892 | $0.0000 | spend incurred (est.) |
| **arm total (1 pass each, every requested model/reasoning arm)** | | | **$0.1846** | **$0.2917** | **$0.0126** | |

The 'est.' column uses a chars/4 heuristic over the extractor's ACTUAL post-strip prompt text (a pre-call approval figure, not a bill); prompt caching on the stable schema/instructions makes the real cost lower still. The 'usage-priced' column prices this harness's OWN captured request/response token counts against the roster's LIST price -- real usage, not a chars/4 guess -- but this harness has NO live BILLING readback, so it is still NOT a verified OpenRouter invoice: prompt caching, provider-side rounding, and any account discount can all make the real invoiced charge differ from the list-price figure shown. A self-consistency vote (sample 3-5x) or a two-pass entailment judge would multiply either figure accordingly. 'of which reserved' discloses the portion of the usage-priced figure that is NOT pure captured usage -- a timeout or provider-error page's added safety reserve (#601 fold round 13); ``$0.0000`` means the arm's actual is pure captured usage.

## Caveat

N is roughly 9 pages: this is a SCREENING harness, not certification. A perfect small-set score is a WARNING (an over-easy fixture or a mislabel), not a verdict. Prefer model A over B only where the page-clustered bootstrap CI on the CombinedScore (and on P/R/A) excludes zero AND the paired test agrees; otherwise choose on cost/latency. Field decisions are CLUSTERED within pages, so the effective N is well below the raw decision count -- the McNemar and Wilson figures treat field-pairs as independent and therefore OVERSTATE certainty; they are indicative only, and the page-clustered bootstrap is the primary test. RANGE-altitude COR is currently UNREACHABLE against the real, unmodified extractor (it never computes a range midpoint or tags altitude 'origin_estimated'), so the two RANGE-altitude pages (cbc-costa-rica-laminita-tarrazu, counterculture-concepcion-huista) cap altitude at MIS (a compliant abstention, weight 0) or INC (a leaked scalar, weight -0.5) regardless of model quality -- an asymmetric penalty whose effect on CombinedScore/macro-F1 ordering is NOT guaranteed uniform across the roster (a run where one model abstains and another leaks a scalar on these cells CAN shift the ranking; see the module docstring).



---

## Sweep 2 — reasoning arms (off vs light)

# #588 bean-sourcing extraction bake-off

- models scored: 5
- corpus pages: 9

## Per-model headline (macro F1 is the model-choice headline; latency is the cost/latency tie-break; schema F/R = schema failures/recovered, #601 F6)

| Model | COR | PAR | INC | MIS | ABS-COR | SPU | ERR | Recall | Faithful | Abstain | micro F1 | macro F1 | Combined | latency p50/p95 (s) | schema F/R |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| `openai/gpt-5-mini+reasoning-light` | 37 | 12 | 5 | 8 | 25 | 3 | 0 | 0.694 | 0.754 | 0.893 | 0.723 | 0.735 | 0.556 | 7.9 / 17.4 | 0/0 |
| `anthropic/claude-haiku-4.5+reasoning-off` | 42 | 12 | 2 | 6 | 16 | 12 | 0 | 0.774 | 0.706 | 0.571 | 0.738 | 0.764 | 0.478 | 3.6 / 4.5 | 0/0 |
| `anthropic/claude-haiku-4.5+reasoning-light` | 42 | 12 | 2 | 6 | 16 | 12 | 0 | 0.774 | 0.706 | 0.571 | 0.738 | 0.764 | 0.478 | 3.6 / 4.7 | 0/0 |
| `openai/gpt-5.6-luna+reasoning-off` | 35 | 8 | 2 | 17 | 14 | 3 | 11 | 0.629 | 0.812 | 0.824 | 0.709 | 0.686 | 0.532 | 2.0 / 2.3 | 0/0 |
| `openai/gpt-5.6-luna+reasoning-light` | 39 | 10 | 0 | 13 | 17 | 5 | 6 | 0.710 | 0.815 | 0.773 | 0.759 | 0.753 | 0.565 | 2.7 / 4.8 | 0/0 |

## Wilson intervals (indicative only, section 5.2 -- ignores within-page clustering, so it OVERSTATES certainty like McNemar; the bootstrap above is primary)

| Model | COR / trials | proportion | 95% Wilson CI |
|---|--:|--:|--:|
| `openai/gpt-5-mini+reasoning-light` | 37/50 | 0.740 | [0.604, 0.841] |
| `anthropic/claude-haiku-4.5+reasoning-off` | 42/50 | 0.840 | [0.715, 0.917] |
| `anthropic/claude-haiku-4.5+reasoning-light` | 42/50 | 0.840 | [0.715, 0.917] |
| `openai/gpt-5.6-luna+reasoning-off` | 35/54 | 0.648 | [0.515, 0.762] |
| `openai/gpt-5.6-luna+reasoning-light` | 39/52 | 0.750 | [0.618, 0.848] |

### `openai/gpt-5-mini+reasoning-light` -- per-page outcomes

| Page | name | origin | region | farm | variety | process | species | altitude | tasting_notes | is_blend | err |
|---|---|---|---|---|---|---|---|---|---|---|---|
| cbc-costa-rica-laminita-tarrazu | COR | COR | COR | COR | COR | COR | ABS_COR | MIS | PAR | COR |  |
| cbc-dominican-republic-ramirez-honey | COR | COR | COR | MIS | COR | COR | ABS_COR | COR | SPU | ABS_COR |  |
| counterculture-big-trouble-blend | COR | PAR | ABS_COR | SPU | ABS_COR | COR | ABS_COR | ABS_COR | PAR | COR |  |
| counterculture-concepcion-huista | COR | COR | COR | PAR | MIS | MIS | ABS_COR | MIS | PAR | ABS_COR |  |
| klatch-blue-thunder-blend | COR | SPU | ABS_COR | ABS_COR | ABS_COR | INC | ABS_COR | ABS_COR | PAR | COR |  |
| klatch-el-salvador-jasal-geisha-anaerobic | PAR | COR | MIS | COR | PAR | COR | ABS_COR | ABS_COR | PAR | ABS_COR |  |
| onyx-colombia-jairo-arcila-rose | INC | COR | ABS_COR | COR | COR | INC | ABS_COR | COR | PAR | ABS_COR |  |
| onyx-ecuador-la-papaya-typica | INC | COR | MIS | INC | COR | COR | ABS_COR | COR | PAR | ABS_COR |  |
| onyx-monarch-blend | COR | COR | ABS_COR | ABS_COR | COR | COR | ABS_COR | COR | PAR | MIS |  |

### `anthropic/claude-haiku-4.5+reasoning-off` -- per-page outcomes

| Page | name | origin | region | farm | variety | process | species | altitude | tasting_notes | is_blend | err |
|---|---|---|---|---|---|---|---|---|---|---|---|
| cbc-costa-rica-laminita-tarrazu | COR | COR | COR | COR | COR | COR | SPU | MIS | PAR | COR |  |
| cbc-dominican-republic-ramirez-honey | COR | COR | COR | COR | COR | COR | SPU | COR | SPU | SPU |  |
| counterculture-big-trouble-blend | COR | PAR | ABS_COR | ABS_COR | ABS_COR | COR | ABS_COR | ABS_COR | PAR | COR |  |
| counterculture-concepcion-huista | COR | COR | COR | PAR | MIS | MIS | ABS_COR | MIS | PAR | ABS_COR |  |
| klatch-blue-thunder-blend | COR | SPU | SPU | ABS_COR | ABS_COR | INC | ABS_COR | ABS_COR | PAR | COR |  |
| klatch-el-salvador-jasal-geisha-anaerobic | COR | COR | MIS | COR | PAR | COR | SPU | ABS_COR | PAR | SPU |  |
| onyx-colombia-jairo-arcila-rose | COR | COR | ABS_COR | COR | COR | INC | SPU | COR | PAR | SPU |  |
| onyx-ecuador-la-papaya-typica | COR | COR | MIS | PAR | COR | COR | SPU | COR | PAR | SPU |  |
| onyx-monarch-blend | COR | COR | ABS_COR | ABS_COR | COR | COR | ABS_COR | COR | PAR | COR |  |

### `anthropic/claude-haiku-4.5+reasoning-light` -- per-page outcomes

| Page | name | origin | region | farm | variety | process | species | altitude | tasting_notes | is_blend | err |
|---|---|---|---|---|---|---|---|---|---|---|---|
| cbc-costa-rica-laminita-tarrazu | COR | COR | COR | COR | COR | COR | SPU | MIS | PAR | COR |  |
| cbc-dominican-republic-ramirez-honey | COR | COR | COR | COR | COR | COR | SPU | COR | SPU | SPU |  |
| counterculture-big-trouble-blend | COR | PAR | ABS_COR | ABS_COR | ABS_COR | COR | ABS_COR | ABS_COR | PAR | COR |  |
| counterculture-concepcion-huista | COR | COR | COR | PAR | MIS | MIS | ABS_COR | MIS | PAR | ABS_COR |  |
| klatch-blue-thunder-blend | COR | SPU | SPU | ABS_COR | ABS_COR | INC | ABS_COR | ABS_COR | PAR | COR |  |
| klatch-el-salvador-jasal-geisha-anaerobic | COR | COR | MIS | COR | PAR | COR | SPU | ABS_COR | PAR | SPU |  |
| onyx-colombia-jairo-arcila-rose | COR | COR | ABS_COR | COR | COR | INC | SPU | COR | PAR | SPU |  |
| onyx-ecuador-la-papaya-typica | COR | COR | MIS | PAR | COR | COR | SPU | COR | PAR | SPU |  |
| onyx-monarch-blend | COR | COR | ABS_COR | ABS_COR | COR | COR | ABS_COR | COR | PAR | COR |  |

### `openai/gpt-5.6-luna+reasoning-off` -- per-page outcomes

| Page | name | origin | region | farm | variety | process | species | altitude | tasting_notes | is_blend | err |
|---|---|---|---|---|---|---|---|---|---|---|---|
| cbc-costa-rica-laminita-tarrazu | COR | COR | COR | COR | COR | COR | ABS_COR | MIS | PAR | COR |  |
| cbc-dominican-republic-ramirez-honey | COR | COR | MIS | INC | COR | COR | ABS_COR | COR | ABS_COR | ABS_COR |  |
| counterculture-big-trouble-blend | MIS | MIS | ERR | ERR | ERR | MIS | ERR | ERR | MIS | MIS | yes |
| counterculture-concepcion-huista | COR | COR | MIS | PAR | MIS | MIS | ABS_COR | MIS | PAR | ABS_COR |  |
| klatch-blue-thunder-blend | MIS | ERR | ERR | ERR | ERR | MIS | ERR | ERR | MIS | MIS | yes |
| klatch-el-salvador-jasal-geisha-anaerobic | COR | COR | MIS | COR | PAR | COR | ABS_COR | ABS_COR | PAR | ABS_COR |  |
| onyx-colombia-jairo-arcila-rose | COR | COR | ABS_COR | COR | COR | COR | ABS_COR | COR | PAR | SPU |  |
| onyx-ecuador-la-papaya-typica | COR | COR | MIS | INC | COR | COR | SPU | COR | PAR | ABS_COR |  |
| onyx-monarch-blend | COR | COR | ABS_COR | ABS_COR | COR | COR | SPU | COR | PAR | COR |  |

### `openai/gpt-5.6-luna+reasoning-light` -- per-page outcomes

| Page | name | origin | region | farm | variety | process | species | altitude | tasting_notes | is_blend | err |
|---|---|---|---|---|---|---|---|---|---|---|---|
| cbc-costa-rica-laminita-tarrazu | COR | COR | COR | COR | COR | COR | ABS_COR | MIS | PAR | COR |  |
| cbc-dominican-republic-ramirez-honey | COR | COR | COR | MIS | COR | COR | ABS_COR | COR | SPU | ABS_COR |  |
| counterculture-big-trouble-blend | COR | PAR | ABS_COR | ABS_COR | ABS_COR | COR | ABS_COR | ABS_COR | PAR | COR |  |
| counterculture-concepcion-huista | COR | COR | MIS | PAR | MIS | MIS | ABS_COR | MIS | PAR | ABS_COR |  |
| klatch-blue-thunder-blend | MIS | ERR | ERR | ERR | ERR | MIS | ERR | ERR | MIS | MIS | yes |
| klatch-el-salvador-jasal-geisha-anaerobic | COR | COR | MIS | COR | PAR | COR | ABS_COR | ABS_COR | PAR | ABS_COR |  |
| onyx-colombia-jairo-arcila-rose | COR | COR | ABS_COR | COR | COR | COR | ABS_COR | COR | PAR | SPU |  |
| onyx-ecuador-la-papaya-typica | COR | COR | MIS | MIS | COR | COR | SPU | COR | PAR | SPU |  |
| onyx-monarch-blend | COR | COR | ABS_COR | ABS_COR | COR | COR | SPU | COR | PAR | COR |  |

## Evidence-quote capture (#612) -- quote capture/authenticity rates, NOT certification: every typed-field citation VALUE gate stays permanently parked (#590); these counts describe what the extractor captured/tagged, not whether the value is correct

| Model | pages scored | field | captured | no evidence | on_page rate |
|---|--:|---|--:|--:|--:|
| `openai/gpt-5-mini+reasoning-light` | 9 | processing | 6 | 3 | 0.341 |
|  |  | bean_species | 0 | 9 |  |
|  |  | altitude_m | 3 | 6 |  |
|  |  | is_blend | 3 | 6 |  |
| `anthropic/claude-haiku-4.5+reasoning-off` | 9 | processing | 6 | 3 | 0.331 |
|  |  | bean_species | 2 | 7 |  |
|  |  | altitude_m | 4 | 5 |  |
|  |  | is_blend | 7 | 2 |  |
| `anthropic/claude-haiku-4.5+reasoning-light` | 9 | processing | 6 | 3 | 0.331 |
|  |  | bean_species | 2 | 7 |  |
|  |  | altitude_m | 4 | 5 |  |
|  |  | is_blend | 7 | 2 |  |
| `openai/gpt-5.6-luna+reasoning-off` | 7 | processing | 4 | 3 | 0.375 |
|  |  | bean_species | 2 | 5 |  |
|  |  | altitude_m | 3 | 4 |  |
|  |  | is_blend | 3 | 4 |  |
| `openai/gpt-5.6-luna+reasoning-light` | 8 | processing | 5 | 3 | 0.336 |
|  |  | bean_species | 2 | 6 |  |
|  |  | altitude_m | 3 | 5 |  |
|  |  | is_blend | 5 | 3 |  |

## Pairwise significance (ALL pairs, section 5.2) -- every comparison the selection relies on is generated here, not just one model versus the rest

- `openai/gpt-5-mini+reasoning-light` vs `anthropic/claude-haiku-4.5+reasoning-off`: CombinedScore gap +0.078 [-0.011, 0.156] (page-clustered bootstrap -- PRIMARY); recall gap -0.081 [-0.131, -0.032]; faithfulness (precision) gap +0.049 [-0.008, 0.098]; abstention gap +0.321 [0.086, 0.632]; McNemar exact p=0.4545 (secondary, indicative).
- `openai/gpt-5-mini+reasoning-light` vs `anthropic/claude-haiku-4.5+reasoning-light`: CombinedScore gap +0.078 [-0.011, 0.156] (page-clustered bootstrap -- PRIMARY); recall gap -0.081 [-0.131, -0.032]; faithfulness (precision) gap +0.049 [-0.008, 0.098]; abstention gap +0.321 [0.086, 0.632]; McNemar exact p=0.4545 (secondary, indicative).
- `openai/gpt-5-mini+reasoning-light` vs `openai/gpt-5.6-luna+reasoning-off`: CombinedScore gap +0.024 [-0.039, 0.121] (page-clustered bootstrap -- PRIMARY); recall gap +0.065 [-0.087, 0.272]; faithfulness (precision) gap -0.058 [-0.137, 0.010]; abstention gap +0.069 [-0.125, 0.254]; McNemar exact p=0.0146 (secondary, indicative).
- `openai/gpt-5-mini+reasoning-light` vs `openai/gpt-5.6-luna+reasoning-light`: CombinedScore gap -0.010 [-0.072, 0.055] (page-clustered bootstrap -- PRIMARY); recall gap -0.016 [-0.119, 0.117]; faithfulness (precision) gap -0.060 [-0.136, 0.006]; abstention gap +0.120 [-0.071, 0.381]; McNemar exact p=0.2379 (secondary, indicative).
- `anthropic/claude-haiku-4.5+reasoning-off` vs `anthropic/claude-haiku-4.5+reasoning-light`: CombinedScore gap +0.000 [0.000, 0.000] (page-clustered bootstrap -- PRIMARY); recall gap +0.000 [0.000, 0.000]; faithfulness (precision) gap +0.000 [0.000, 0.000]; abstention gap +0.000 [0.000, 0.000]; McNemar exact p=1.0000 (secondary, indicative).
- `anthropic/claude-haiku-4.5+reasoning-off` vs `openai/gpt-5.6-luna+reasoning-off`: CombinedScore gap -0.054 [-0.183, 0.115] (page-clustered bootstrap -- PRIMARY); recall gap +0.145 [0.015, 0.330]; faithfulness (precision) gap -0.107 [-0.197, -0.020]; abstention gap -0.252 [-0.588, 0.036]; McNemar exact p=0.1221 (secondary, indicative).
- `anthropic/claude-haiku-4.5+reasoning-off` vs `openai/gpt-5.6-luna+reasoning-light`: CombinedScore gap -0.088 [-0.191, 0.021] (page-clustered bootstrap -- PRIMARY); recall gap +0.065 [-0.016, 0.175]; faithfulness (precision) gap -0.109 [-0.198, -0.023]; abstention gap -0.201 [-0.450, 0.011]; McNemar exact p=0.8036 (secondary, indicative).
- `anthropic/claude-haiku-4.5+reasoning-light` vs `openai/gpt-5.6-luna+reasoning-off`: CombinedScore gap -0.054 [-0.183, 0.115] (page-clustered bootstrap -- PRIMARY); recall gap +0.145 [0.015, 0.330]; faithfulness (precision) gap -0.107 [-0.197, -0.020]; abstention gap -0.252 [-0.588, 0.036]; McNemar exact p=0.1221 (secondary, indicative).
- `anthropic/claude-haiku-4.5+reasoning-light` vs `openai/gpt-5.6-luna+reasoning-light`: CombinedScore gap -0.088 [-0.191, 0.021] (page-clustered bootstrap -- PRIMARY); recall gap +0.065 [-0.016, 0.175]; faithfulness (precision) gap -0.109 [-0.198, -0.023]; abstention gap -0.201 [-0.450, 0.011]; McNemar exact p=0.8036 (secondary, indicative).
- `openai/gpt-5.6-luna+reasoning-off` vs `openai/gpt-5.6-luna+reasoning-light`: CombinedScore gap -0.034 [-0.155, 0.033] (page-clustered bootstrap -- PRIMARY); recall gap -0.081 [-0.235, 0.000]; faithfulness (precision) gap -0.002 [-0.013, 0.011]; abstention gap +0.051 [-0.089, 0.222]; McNemar exact p=0.0654 (secondary, indicative).

## Reasoning-arm comparison (off vs light, #601) -- per-model deltas where BOTH arms were scored (never vs 'default'). 'schema F/recovered R' is the adherence proxy; 'other errors' is NOT.

| Model | macro F1 (off -> light) | Combined (off -> light) | Recall (off -> light) | Faithful (off -> light) | schema F/recovered R (off -> light) | other errors (off -> light) |
|---|---|---|---|---|---|---|
| `anthropic/claude-haiku-4.5` | 0.764 -> 0.764 | 0.478 -> 0.478 | 0.774 -> 0.774 | 0.706 -> 0.706 | 0/0 -> 0/0 | 0 -> 0 |
| `openai/gpt-5.6-luna` | 0.686 -> 0.753 | 0.532 -> 0.565 | 0.629 -> 0.710 | 0.812 -> 0.815 | 0/0 -> 0/0 | 2 -> 1 |

## Cost (estimated spend incurred this invocation)

**~$0.2242 ESTIMATED SPEND INCURRED** this invocation, on 5 newly-called model(s): `openai/gpt-5-mini+reasoning-light`, `anthropic/claude-haiku-4.5+reasoning-off`, `anthropic/claude-haiku-4.5+reasoning-light`, `openai/gpt-5.6-luna+reasoning-off`, `openai/gpt-5.6-luna+reasoning-light`. A real (paid) call WAS made for each -- but see the note below: this is still this harness's cost ESTIMATE, never a verified OpenRouter billing amount.

| Model | in tok | out tok | est. USD (full corpus, 1 pass) | usage-priced USD (list price) | of which reserved | status |
|---|--:|--:|--:|--:|--:|---|
| `openai/gpt-5-mini+reasoning-light` | 23386 | 7920 | $0.0217 | $0.0198 | $0.0000 | spend incurred (est.) |
| `anthropic/claude-haiku-4.5+reasoning-off` | 23386 | 1980 | $0.0333 | $0.0574 | $0.0000 | spend incurred (est.) |
| `anthropic/claude-haiku-4.5+reasoning-light` | 23386 | 7920 | $0.0630 | $0.0574 | $0.0000 | spend incurred (est.) |
| `openai/gpt-5.6-luna+reasoning-off` | 23386 | 1980 | $0.0353 | $0.0388 | $0.0000 | spend incurred (est.) |
| `openai/gpt-5.6-luna+reasoning-light` | 23386 | 7920 | $0.0709 | $0.0416 | $0.0000 | spend incurred (est.) |
| `anthropic/claude-haiku-4.5` | n/a | n/a | n/a | $0.0574 | $0.0000 | prior-lineage-arm (not in this invocation's --models) |
| `google/gemini-3.1-flash-lite` | n/a | n/a | n/a | $0.0098 | $0.0000 | prior-lineage-arm (not in this invocation's --models) |
| `openai/gpt-4.1-mini` | n/a | n/a | n/a | $0.0145 | $0.0000 | prior-lineage-arm (not in this invocation's --models) |
| `openai/gpt-4o` | n/a | n/a | n/a | $0.0892 | $0.0000 | prior-lineage-arm (not in this invocation's --models) |
| `openai/gpt-5-mini` | n/a | n/a | n/a | $0.0404 | $0.0000 | prior-lineage-arm (not in this invocation's --models) |
| `openai/gpt-5-nano` | n/a | n/a | n/a | $0.0243 | $0.0126 | prior-lineage-arm (not in this invocation's --models) |
| `openai/gpt-5.6-luna` | n/a | n/a | n/a | $0.0465 | $0.0000 | prior-lineage-arm (not in this invocation's --models) |
| `x-ai/grok-4.3` | n/a | n/a | n/a | $0.0098 | $0.0000 | prior-lineage-arm (not in this invocation's --models) |
| **arm total (1 pass each, every requested model/reasoning arm)** | | | **$0.2242** | **$0.5066** | **$0.0126** | |

The 'est.' column uses a chars/4 heuristic over the extractor's ACTUAL post-strip prompt text (a pre-call approval figure, not a bill); prompt caching on the stable schema/instructions makes the real cost lower still. The 'usage-priced' column prices this harness's OWN captured request/response token counts against the roster's LIST price -- real usage, not a chars/4 guess -- but this harness has NO live BILLING readback, so it is still NOT a verified OpenRouter invoice: prompt caching, provider-side rounding, and any account discount can all make the real invoiced charge differ from the list-price figure shown. A self-consistency vote (sample 3-5x) or a two-pass entailment judge would multiply either figure accordingly. 'of which reserved' discloses the portion of the usage-priced figure that is NOT pure captured usage -- a timeout or provider-error page's added safety reserve (#601 fold round 13); ``$0.0000`` means the arm's actual is pure captured usage.

## Caveat

N is roughly 9 pages: this is a SCREENING harness, not certification. A perfect small-set score is a WARNING (an over-easy fixture or a mislabel), not a verdict. Prefer model A over B only where the page-clustered bootstrap CI on the CombinedScore (and on P/R/A) excludes zero AND the paired test agrees; otherwise choose on cost/latency. Field decisions are CLUSTERED within pages, so the effective N is well below the raw decision count -- the McNemar and Wilson figures treat field-pairs as independent and therefore OVERSTATE certainty; they are indicative only, and the page-clustered bootstrap is the primary test. RANGE-altitude COR is currently UNREACHABLE against the real, unmodified extractor (it never computes a range midpoint or tags altitude 'origin_estimated'), so the two RANGE-altitude pages (cbc-costa-rica-laminita-tarrazu, counterculture-concepcion-huista) cap altitude at MIS (a compliant abstention, weight 0) or INC (a leaked scalar, weight -0.5) regardless of model quality -- an asymmetric penalty whose effect on CombinedScore/macro-F1 ordering is NOT guaranteed uniform across the roster (a run where one model abstains and another leaks a scalar on these cells CAN shift the ranking; see the module docstring).