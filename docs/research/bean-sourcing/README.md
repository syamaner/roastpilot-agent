# Bean-sourcing: LLM extraction research & design recommendations

Applied-research synthesis for the bean-profile-from-URL feature (#573) and its
eval/monitoring harness (#588). The pipeline is: operator pastes a specialty-coffee
vendor product URL → the server fetches the page → an LLM returns a **typed
`BeanProfileDraft`** (origin, region, farm, variety, process, altitude, roast
guidance, tasting notes, `is_blend`) → the operator reviews and edits the draft →
save. It is **advisory-only, human-gated, never auto-saved**, and lives entirely
outside the roaster safety envelope (`bean_sourcing.py` never imports
controller/safety/mcp_client).

This note is the product of five parallel web-research passes (18 Jul 2026);
the source URLs are collected in **§7**. A caveat on the numbers: specific quantitative
figures cited here (e.g. the ~65% token reduction, the ρ=0.31 confidence-signal
correlation, the "35→3 schema violations") are each **single-source and indicative,
not settled** — several rest on 2026 preprints or vendor blogs, and the raw research
transcripts are not committed to the repo. Treat them as directional priors to falsify
against our own bake-off, never as established constants. **Bottom line: this is a
simpler problem than the general "LLM extraction" literature implies** — a single
product page is short, often already carries machine-readable structured data, and the
human review step is the safety net. The high-value work is in *preprocessing* and
*making provenance verified rather than claimed*, not in exotic decoding machinery.

---

## 1. The risk framing (why this is not the advisor)

RoastPilot now has two LLM surfaces with different risk classes:

| | **Advisor** | **Bean-sourcing** |
|---|---|---|
| Role | advisory-only: returns typed decisions to the controller, which owns the loop; controller-originated writes pass safety policy (the LLM never controls hardware) | extracts typed data for a human to review |
| Eval axis | **control safety** (bake-off + hardware gate) | **faithfulness** (does it read the page honestly / abstain) |
| Failure mode | a bad roast | a bad *draft the human catches* |
| Monitor | live roast traces | the operator's **draft→saved edits** (free ground truth) |

The dangerous failure here is **confabulation** — a plausible origin/process/altitude
that is *not on the page*, which would silently poison the advisor's downstream
context if saved. The design bias is therefore **abstain over confabulate**: a null
(or `origin_estimated`) is cheap; an invented `on_page` value is expensive.

---

## 2. Pipeline recommendations (deterministic-first, LLM-for-the-gaps)

The single biggest improvement is to **not send raw HTML to the LLM** and to
**extract machine-readable structured data first**:

1. **Extract JSON-LD / microdata deterministically, before any LLM call.** Vendor
   product pages (Shopify especially) very commonly embed a `schema.org/Product`
   block (`<script type="application/ld+json">`) plus Open Graph / microdata. The
   `extruct` library lifts all of these in one call. Whatever it yields
   (name, brand, description, offers, `additionalProperty`) is **exact and free**
   — but *not* automatically about the requested bean: a page can carry stale,
   variant, or related-product `Product` blocks, so JSON-LD is trusted only after an
   **identity match** — select the block whose `@id`/`url`/`sku`/`name`/`offers` matches
   the requested product URL, and ignore the rest. Note this is a *different* gate from
   the LLM's DOM-**locality** check (§3): JSON-LD lives in a `<script>` block, not the
   visual product region, so the locality gate would wrongly reject exactly the data
   this stage exists to recover. Use **identity-match for structured data, DOM-locality
   for LLM spans.** Verified JSON-LD then feeds the LLM as trusted context to fill only
   the prose-only gaps (variety, process, altitude, notes); unmatched blocks are dropped.
2. **Boilerplate-strip to markdown with `trafilatura`** (`output_format='markdown'`)
   and feed *that* to the LLM, never raw HTML — ~65% fewer tokens and cleaner
   extraction (nav/footer/related-products stripped so the model isn't distracted
   by *other* beans on the page). `trafilatura` also returns page metadata for free.
3. **Keep PydanticAI's default Tool-Output mode.** It already guarantees
   schema-valid typed output for a flat record. **Do not** add Instructor / BAML /
   outlines — they solve problems we don't have here. Consider Native structured-
   output mode only if the chosen OpenRouter model exposes strict schema mode.
4. **Every optional field is `X | None`, default `None`,** with per-field
   `Field(description=...)` carrying a coffee micro-glossary (variety = cultivar;
   process = washed/natural/honey; altitude in masl; …) so the schema itself
   disambiguates.
5. **Skip chunking.** A stripped product page fits every candidate model's context
   window. If one ever overflows, tighten the content selection, don't add a chunker.

---

## 3. Making `on_page` provenance *earned, not claimed*

Our `field_sources` tag (`on_page` vs `origin_estimated`) is the honest-imputation
backbone — but today it records the model's *claim*. The research consensus is to
make it **verified by code**. Four **free** levers (no extra LLM call), ship first:

1. **Evidence-quote + containment gate.** Extend the schema so each extractable
   field returns `{value, evidence_quote}`. After the call, confirm in code that
   `evidence_quote` is a substring of the fetched page text (normalised) *and* the
   value is derivable from it. If the quote isn't on the page → the model fabricated
   it → **discard the value (set the field to null/unset)**. Do *not* relabel it
   `origin_estimated`: a failed-gate value is a fabrication, not an estimate, and
   `origin_estimated` is reserved for the deterministic `_draft_from_identity` fallback
   (a value the code *derived*, e.g. an origin-typical default) — keeping the two
   distinct is what makes the provenance tag honest. This converts "the model claims
   on_page" into "the code confirmed on_page" — the highest-leverage single change.
2. **Locality check.** Verify the evidence span sits in the main product region, not
   a "related products / you may also like" block — pure containment passes on the
   *wrong bean*; locality closes that hole.
3. **Constrained enums + null-on-absence.** `process`, `species` are closed sets —
   constrain them and instruct null when absent. (Constraint removes malformed
   output but not a valid-but-wrong pick, so it still passes through the span gate.)
4. **Abstention-biased prompt + a one-line CoT nudge** ("first note whether the page
   states each field, then fill; return null if absent — never 'none'/'unknown' or
   an inferred value"). CoT was the single biggest abstention improver in the
   literature (one benchmark: 32.8% → 85.3% unanswerable-accuracy).

For the high-stakes fields that feed the advisor (origin, process, altitude), add
**one moderate-cost** signal:

5. **Self-consistency vote** (sample 3–5× at T>0, keep a field only where samples
   agree) — the best-correlating single confidence signal in the literature — and/or
   a **two-pass entailment judge** (a second call checks each `(field, value, page)`
   for entailment; non-entailed → abstain). Because each field is already one atomic
   claim, we skip RAGAS-style decomposition (its fragile part).

**Explicitly de-prioritise verbalized confidence** ("confidence: 0.9") — every source
agrees it tracks *commitment*, not correctness. Token-logprob geometric-mean is a
cheap extra tripwire *if* the provider exposes logprobs (well-calibrated on numeric
fields, overconfident on free text) — never used alone.

---

## 4. Model selection (the #588 bake-off)

Research **narrows the field; our own corpus bake-off picks the winner** — no public
benchmark measures our exact task (one-shot typed extraction from a web page with an
abstain option). Structured-output *validity* is now table stakes across the roster
(GPT-5 family & Gemini 3.x ~100% strict; Haiku 4.5 via tool-use); differentiation is
**faithfulness + cost**.

**Cost/quality frontier (list price /1M tok, verify in the OpenRouter dashboard at
run time):**

| Tier | Slug | In/Out $ | Note |
|---|---|---|---|
| Cheapest frontier | `openai/gpt-5-nano` | 0.05 / 0.40 | rock-bottom; the one to beat on price |
| | `x-ai/grok-4-fast` | 0.20 / 0.50 | cheapest output, 2M ctx; verify strict-schema |
| | `google/gemini-3.1-flash-lite` | 0.25 / 1.00 | beats GPT-5-mini on 6/8 benches; enable *light* thinking |
| Mid (reliability headroom) | `openai/gpt-5-mini` | 0.25 / 2.00 | ParseBench small-model reference; safe default |
| | `openai/gpt-4.1-mini` | 0.40 / 1.60 | battle-tested strict-SO workhorse |
| | `google/gemini-3-flash-preview` | 0.50 / 3.00 | near-Pro reasoning at ⅓ of 3.5 Flash |
| Premium small (buy abstain) | `anthropic/claude-haiku-4.5` | 1.00 / 5.00 | best at *deciding not to emit*; 90% prompt-cache discount |
| | `openai/gpt-5.6-luna` | 1.00 / 6.00 | strong text/table extraction (ParseBench day-0) |
| Dominated (ceiling only) | `google/gemini-3.5-flash`, `openai/gpt-4o` | 1.50/9, 2.50/10 | 5–50× the frontier price, no extraction edge |

- **Likely-best-cheap:** `gpt-5-nano` (if its faithfulness/abstain hold on our pages).
- **Safe default:** `gpt-5-mini` or `gemini-3.1-flash-lite`.
- Test each cheap model at **no-reasoning vs light-reasoning** — reasoning barely
  helps extraction quality but sharply helps *schema adherence* on the cheapest
  Gemini (35 → 3 violations). Use **prompt caching** on the stable schema (60–90%).
- Small-model failure modes to screen for: over-confident confabulation, "instruction
  attenuation" (ignoring the abstain rule), and dropping fields on nested/optional
  schemas — keep the schema flat.

---

## 5. Eval + monitoring harness (#588) — concrete spec

### 5.1 Scoring (offline bake-off over a labelled corpus)

Label each `(page, field)` gold state as `{value, absent}` (an unstated field is a
first-class gold target the model scores on by abstaining). Classify each model
output:

| Gold | Model output | Outcome |
|---|---|---|
| value | matching | **COR** |
| value | partial | **PAR** (½) |
| value | wrong | **INC** |
| value | null/abstain | **MIS** (omission) |
| absent | null/abstain | **ABS-COR** (correct abstention) |
| absent | any value | **SPU** (hallucination) |

Match functions per field type: canonicalise (trim/lower/whitespace) → units/numbers
to canonical unit with tolerance (`1800masl == 1800 m`, ±5 m) → enums via an
auditable in-repo synonym table (`fully washed → washed`) → names/free-text via
normalized Levenshtein (≥0.9 COR, 0.6–0.9 PAR) → blends by order-independent set
alignment. **Levenshtein alone is not sufficient for free-text/names** — a short edit
distance can hide a *contradiction* (`washed` vs `unwashed`, `natural` vs `not
natural`), so a high string-similarity match must still pass a **semantic/contradiction
check** (a synonym+antonym table for the closed-ish fields, an LLM-judge equivalence
call for genuinely free text) before it scores COR; a detected contradiction is INC,
not COR. Log every LLM-judge call so the score stays reproducible.

**Ranges (altitude especially).** Suppliers often state a range ("1,200–2,000 m"),
which the scalar `altitude_m` schema field cannot represent faithfully — a real repo
case (several seeds collapse a supplier range into one scalar). Fix the policy
explicitly, both in the schema and the scorer: label the gold value as the **range**;
the extractor's contract is to return the range's midpoint (or low bound) **marked
`origin_estimated`**, and the scorer credits COR when the model returns a scalar
inside the gold range *and* flags it estimated, MIS if it abstains, and **INC** if it
emits an out-of-range or unflagged scalar. Note the outcome is **INC, not SPU** —
the gold value is *present* (a range), so a wrong scalar is a present-value error;
**`SPU` stays reserved strictly for gold-*absent* fields** (a value invented where the
page states nothing). Overloading `SPU` onto present-but-wrong values would double-count
it in both the recall and the abstention axes. (Preferably widen the schema to an
optional `altitude_min_m`/`altitude_max_m` pair so a range can be stored faithfully.)

Report three axes plus a combined rank that **makes an honest abstainer beat a
confabulator**:

```
Field recall (coverage)  R = (COR + 0.5·PAR) / (COR+INC+PAR+MIS)
Faithfulness (precision) P = (COR + 0.5·PAR) / (COR+INC+PAR+SPU)
Abstention-correctness   A = ABS-COR / (ABS-COR + SPU)          # gold-absent only
CombinedScore = mean over (page,field) of:
   +1.0 COR · +0.5 PAR · +0.5 ABS-COR · 0.0 MIS · -0.5 INC · -1.0 SPU
```

Report **micro + macro F1** (macro is the headline for model choice — every field
counts equally). Rank by `CombinedScore`; break ties on cost + latency.

### 5.2 Small-N rigor (N≈8–10 pages is *screening*, not certification)

- **Page-level paired bootstrap for P/R/A** (10k resamples, resample *pages*),
  **not** raw Wilson. Wilson assumes independent binary Bernoulli trials, but our
  P/R/A numerators include fractional `PAR` outcomes *and* the field decisions are
  clustered within pages — so a field-decision-level Wilson interval would understate
  uncertainty and could drive the model gate wrongly. Reserve Wilson only for a
  strictly-binary decomposition (e.g. COR-vs-not, PAR excluded), and even then report
  it as indicative given the clustering.
- **The page-clustered paired bootstrap is the PRIMARY significance test** (resample
  *pages*, not field-decisions, so within-page correlation is respected) — on the
  CombinedScore gap *and* on P/R/A.
- **Paired McNemar — exact binomial** is a *secondary, indicative* check only: it
  treats each field-pair as independent, which our own clustering caveat says is false,
  so it **overstates** significance. Use it as a coarse tie-breaker on a binary
  per-field correct/incorrect view (<25 discordant pairs → exact, not χ²), never as the
  deciding test where it disagrees with the page-clustered bootstrap.
- Committed caveat text: a perfect small-set score is a **warning** (over-easy
  fixture or mislabel), not a verdict; prefer model A over B only where CIs don't
  overlap *and* the paired test is significant; else choose on cost/latency. Field
  decisions are clustered within pages, so effective N < raw decision count.

### 5.3 Runtime monitoring — the draft→saved signal (highest value, near-zero cost)

The human-gate gives free ground truth (the Langfuse "Corrections" pattern):
**log one row per extraction ATTEMPT** (not just per successful draft) in the existing
SQLite store —

- `source_url` (**redacted of any credentials**), resolved IP, fetch status/latency/bytes;
- `model` slug + prompt-version hash;
- **terminal state**: `drafted` / `fetch_failed` / `extraction_failed` — a key point:
  **attempts that never produce a draft** (DNS/SSRF rejection, fetch timeout, size-cap
  rejection, model exception, exhausted schema-validation retries) must be logged too,
  or the failure modes are invisible and the success metrics are inflated by
  survivorship;
- for a drafted attempt: `draft_output` (LLM proposal) **and** `saved_output` (operator
  accepted, if any), plus an explicit **operator outcome: `accepted` / `rejected` /
  `abandoned`** (a badly-extracted draft the operator throws away has no `saved_output`);
- `parsed_output` + which fields failed validation / reasked;
- tokens in/out + cost, total latency, any exception, on_page/estimated ratio.

Two signals, kept **separate** so a draft-level event doesn't contaminate per-field data:

- **Per-field edit rate** — `edited = draft[field] != saved[field]`, computed **only over
  `accepted` drafts**, aggregated over a trailing window. Do **not** fold `rejected`/
  `abandoned` drafts into per-field counts: a rejected draft is not evidence that *every*
  field was wrong, and an abandon may be an interruption, not a quality signal — attributing
  a draft-level outcome to each field would poison the per-field rate.
- **Draft-level outcome rate** — the `accepted`/`rejected`/`abandoned` and
  `fetch_failed`/`extraction_failed` mix over the window, tracked as its own series.
- **Drift alarm** = a rise in *either* series (a field's edit rate, or the draft-level
  reject/abandon/failure rate). **Auto-accept** a field only when it is ~0%-edited across
  a window **and** the draft-level reject/abandon rate is low — never on per-field edit
  rate alone (the survivorship trap).
- **Fixture promotion, carefully.** A saved profile is ground truth for the *desired bean
  record*, **not** for *what the page stated* — the operator may correct or add off-page
  knowledge. So a `saved_output` cannot be promoted wholesale to an eval fixture (whose
  gold is "what the page supports"). Promote only after a labelling step that re-derives
  each field's `{value, absent}` gold **from the page**, flagging fields the operator
  added that the page didn't state as gold-`absent`. Also **persist the exact fetched page
  snapshot** (locally, not committed — supplier pages mutate/disappear) so offline replay
  runs against the real input, not a re-fetch.

### 5.4 Pre-operator guardrails

Run extracted values through a Pydantic model with enum + `Field(ge/le)` range bounds
*before* showing the draft (altitude in a plausible metre range, process in the known
set, density/moisture within physical bounds). On failure, **flag the field for
review** (or reask once) — never silently show a bad value, never auto-feed
downstream. This aligns with the repo's typed / fail-closed posture.

**What's overkill for a single-operator tool:** standing up LangSmith/Langfuse/Phoenix
as infrastructure, LLM-judge *online* scoring, embedding-drift statistics. The SQLite
row + per-field edit-rate counter + offline fixture replay gives ~90% of the value at
near-zero ops cost. The *pattern* is what matters; adopt a platform only if the free
diff-viewer UI later earns its keep.

---

## 6. Server-side fetch security (SSRF) — validated against OWASP + 2026 CVEs

The fetch does a server-side GET of an operator-supplied URL; the app is a
single-operator LAN tool with no auth binding `0.0.0.0`, so the SSRF surface is real
(a LAN device, or a malicious webpage driving the endpoint via CSRF).

**Baseline (OWASP-endorsed) — all required:**

- http/https scheme allow-list; reject `userinfo@`, fragments, non-http(s).
- Resolve the host and reject via `ipaddress` boolean flags —
  `is_loopback / is_private / is_link_local / is_multicast / is_reserved /
  is_unspecified` — plus CGNAT `100.64.0.0/10`, IPv4-mapped-IPv6 (`::ffff:…`), and
  IPv6 ULA (`fc00::/7`) explicitly (classic denylist bypasses). Validate **every**
  A/AAAA record, not just the first.
- Keep httpx `follow_redirects=False`; drive redirects manually, re-running the
  **full** validator on every hop, bounded hop count.
- **Two timeouts:** per-op `httpx.Timeout` *and* an outer `asyncio.timeout()` around
  the whole multi-hop fetch (per-op can't stop a slow-drip body). Cap response bytes.

**The gap the baseline does NOT close — DNS rebinding (TOCTOU):** "resolve → validate
→ hand the *hostname* to httpx → httpx re-resolves at connect time" is defeated by an
attacker-controlled short-TTL DNS name that answers public on the check and
`169.254.169.254`/RFC1918 on the connect. This is a **live 2026 CVE class in exactly
this fetch-user-URL-for-LLM shape** (CVE-2026-27826 / GHSA-489g-7rxv-6c8q mcp-atlassian,
Prefect PR #21591, FastGPT, RAGFlow).

**The fix — socket-level IP pinning.** Wrap httpcore's async network backend: resolve
and validate immediately before every new TCP connection, then pass only a validated
IP literal to the wrapped dialer. HTTPX/httpcore retain the original hostname origin,
so cookie scoping, keepalive pooling, `Host`, TLS SNI, and certificate validation stay
natural; there is no URL rewrite or routing/TLS-header workaround. Prefect's merged
`SSRFProtectedAsyncHTTPTransport` (PR #21591) is the upstream reference.
Assessment for our tool: **worth doing, not overkill** — it is the one control that
actually holds against a competent attacker, and this exact tool shape is where the
2026 CVEs landed. (Overkill and skipped: egress proxy, DNSSEC, network-namespace
sandbox.) A natural cheap force-multiplier if ever wanted: a small **supplier-domain
allow-list** (the input domain space is tiny) as defence-in-depth over the denylist.

**Residual after pinning:** effectively closed for rebind + redirect vectors. What
remains is parser-differential risk (validate the *same* parsed host/IP you connect
to) and the separate data-trust problem that a valid public vendor page is still
attacker-influenceable content feeding the LLM (prompt-injection, out of scope for
SSRF — relevant to the extraction-trust story, not the fetch).

---

## 7. Key sources

**Extraction & preprocessing:** [PydanticAI output modes](https://pydantic.dev/docs/ai/core-concepts/output/) ·
[google/langextract (char-span grounding)](https://github.com/google/langextract) ·
[extruct (JSON-LD/microdata)](https://github.com/scrapinghub/extruct) ·
[trafilatura](https://trafilatura.readthedocs.io/en/latest/usage-python.html) ·
[WebDataCommons structured data](https://webdatacommons.org/structureddata/) ·
["Let Me Speak Freely?" (format-restriction trade-off, 2408.02442)](https://arxiv.org/pdf/2408.02442)

**Faithfulness & abstention:** [FACTS Grounding](https://www.emergentmind.com/topics/facts-grounding-benchmark) ·
[RAGAS faithfulness](https://saulius.io/blog/ragas-rag-evaluation-metrics-llm-judge) ·
[SelfCheckGPT (2303.08896)](https://arxiv.org/abs/2303.08896) ·
[When LLMs Agree, Are They Right? (2607.08065)](https://arxiv.org/pdf/2607.08065) ·
[Do LLMs Know When to NOT Answer? (2407.16221)](https://arxiv.org/html/2407.16221v1) ·
[Beyond Logprobs — multi-signal confidence for field extraction (2606.24420)](https://arxiv.org/pdf/2606.24420)

**Eval methodology:** [ExtractBench (2602.12247)](https://arxiv.org/html/2602.12247v2) ·
[davidsbatista — MUC/SemEval partial-credit scoring](https://www.davidsbatista.net/blog/2018/05/09/Named_Entity_Evaluation/) ·
[Deepgram — Slot Error Rate](https://deepgram.com/learn/slot-error-rate-developer-guide-asr-accuracy) ·
[Wilson score interval](https://statisticsfundamentals.com/confidence-intervals/wilson-score-interval/) ·
[mlxtend — McNemar exact](https://rasbt.github.io/mlxtend/user_guide/evaluate/mcnemar/) ·
[SWDE / lm-eval-harness](https://github.com/EleutherAI/lm-evaluation-harness/blob/main/lm_eval/tasks/swde/README.md)

**Model landscape:** [ParseBench (LlamaIndex, 2604.08538)](https://arxiv.org/html/2604.08538v1) ·
[Berkeley Function Calling Leaderboard V4](https://gorilla.cs.berkeley.edu/leaderboard.html) ·
[Gemini 3.1 Flash-Lite vs GPT-5-mini](https://www.digitalapplied.com/blog/gemini-3-1-flash-lite-cheapest-ai-beats-gpt-5-mini) ·
[OpenRouter structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs) ·
[GPT-5.6 Sol/Terra/Luna pricing & benchmarks](https://the-agent-report.com/2026/07/gpt-5-6-sol-terra-luna-benchmarks-pricing-analysis/)

**SSRF & monitoring:** [OWASP SSRF Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html) ·
[CVE-2026-27826 / GHSA-489g-7rxv-6c8q (DNS-rebind TOCTOU)](https://github.com/advisories/GHSA-489g-7rxv-6c8q) ·
[Prefect PR #21591 — SSRFProtectedHTTPTransport (IP pinning)](https://github.com/PrefectHQ/prefect/pull/21591) ·
[Behrad Taher — DNS rebinding vs SSRF protections](https://behradtaher.dev/DNS-Rebinding-Attacks-Against-SSRF-Protections/) ·
[httpx custom transports](https://www.python-httpx.org/advanced/transports/) ·
[Langfuse — Corrections (human-correction-as-label)](https://langfuse.com/docs/observability/features/corrections)

---

## 8. Actionable deltas (what this changes)

- **#587 + #591 (shipped):** the original URL-rewrite pin closed rebinding; #591
  replaced it with socket-level connect pinning while preserving hostname cookies,
  pooling, `Host`, and TLS identity. The denylist, per-hop redirect validation, dual
  timeouts, and compressed/decompressed response caps remain unchanged.
- **#573 extractor (near-term):** add `extruct` JSON-LD-first extraction ahead of the
  LLM, `trafilatura`-markdown instead of raw HTML, and the free provenance levers
  (evidence-quote + containment gate, locality check, constrained enums, abstention
  prompt) so `on_page` becomes *verified*. (Filed as extractor-hardening follow-up.)
- **#588 (next):** implement the §5 scoring spec + small-N stats, the draft→saved
  telemetry, and the pre-operator guardrails; run the §4 model bake-off over a
  hand-labelled corpus to pick the cost-optimal model on data.
