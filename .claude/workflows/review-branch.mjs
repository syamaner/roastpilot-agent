export const meta = {
  name: 'review-branch',
  description:
    'Cross-checked roster review of the current branch diff: fan out the role agents as lenses (correctness, qa, product-pm, + safety/security/ui when relevant), adversarially verify each finding, then a single pr-triage verdict.',
  whenToUse:
    'Run on a PR/branch before merge for a multi-lens, cross-checked review you can rerun. Pass {base:"<ref>"} to diff against a non-default base.',
  phases: [
    { title: 'Scope', detail: 'classify what the diff touches' },
    { title: 'Review', detail: 'roster lenses over the diff' },
    { title: 'Verify', detail: 'adversarially refute each finding' },
    { title: 'Triage', detail: 'consolidate into one verdict' },
  ],
}

// Diff base — override with args {base:"<ref>"} (e.g. a release branch).
// origin/main (not local main) so a stale local branch doesn't add noise.
const base = (args && args.base) || 'origin/main'
const diffScope = `\`git diff ${base}...HEAD\``

// --- Phase 1: Scope — what does the diff touch? -------------------------------
phase('Scope')
const SCOPE_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: [
    'touchesWeb',
    'touchesPython',
    'touchesSafetyControllerEnums',
    'touchesExternalInput',
    'addsProviderCallPath',
    'changedFiles',
    'summary',
  ],
  properties: {
    touchesWeb: { type: 'boolean' },
    touchesPython: { type: 'boolean' },
    touchesSafetyControllerEnums: { type: 'boolean' },
    // Capability-based, NOT file-based (AGENTS.md): the #587 lesson is that the
    // highest-risk diff touched no safety file, so a path allow-list would never
    // have fired.
    //
    // FOUR COPIES — CHANGE THEM TOGETHER. This trigger test is stated in four
    // places and they must agree or the routing silently no-ops: the scope prompt
    // below, the Scope section of .claude/agents/security-reviewer.md, the
    // "When this applies" test in docs/review/untrusted-input-checklist.md, and the
    // reviewer-routing list in .claude/skills/pr-preflight/SKILL.md. The
    // agent definition is the one that bites — it ends with "if the diff matches
    // none of these, say so and stop", so a reviewer routed here by a widened
    // workflow will still stop if its own copy stayed narrow. Widening one copy
    // and not the others is the most repeated mistake in this file's history.
    touchesExternalInput: { type: 'boolean' },
    // Checklist class 6: a NEW provider-calling path can contend with the roast
    // advisor, so AGENTS.md routes it to safety-reviewer as well as security-reviewer.
    // Tracked separately so a plain parse/fetch change doesn't spin up the Opus
    // safety lens it doesn't need.
    addsProviderCallPath: { type: 'boolean' },
    changedFiles: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string' },
  },
}
// Model pin for the workflow's own inline stages (scope, the correctness lens,
// and the finding verifiers). Lenses that name an agentType resolve their model
// and effort from that (now full-ID) agent definition instead; this only pins
// the stages that have no agent of their own, so they never silently inherit the
// caller's model. Single source for all three.
const REVIEW_MODEL = 'claude-sonnet-5'
// Safety-lens findings come from the always-Opus safety-reviewer; verify them on
// the same tier, so a lower-tier Sonnet verifier can't return survives=false and
// silently discard a subtle safety finding before triage. Other lenses verify on
// the review model.
const SAFETY_VERIFY_MODEL = 'claude-opus-5'
const scope = await agent(
  `Scope a code review of the current branch. Run \`git fetch origin -q\` first so the base ref is current, then \`git diff --name-only ${base}...HEAD\` and \`git diff --stat ${base}...HEAD\` in this repo. Report which areas changed: web/ (the SPA), Python (src/roastpilot_agent or tests/), and specifically safety.py / controller.py / a models.py enum (the safety-critical surface). List the changed files and a one-line summary.

Also decide \`touchesExternalInput\`. Judge this by CAPABILITY, not by file path — read the changed hunks, because the highest-risk case (#587) touched no safety-critical file and a path allow-list would have missed it. Set it true if the diff, ON THE SERVER, does any of:
- fetches a URL / opens a connection whose target is influenced by operator or external input (a pasted URL, a redirect \`Location\`, a webhook, a config value);
- parses or decodes untrusted bytes/strings (URL parsing, HTML/JSON/charset decode, number/port parsing, deserialization);
- adds an external-input endpoint (a route taking client-supplied data);
- adds a **new** LLM/model-provider call path — ANY provider or model service, not only the one the roast advisor uses, and wherever it runs: the "ON THE SERVER" qualifier above does NOT apply to this bullet, so a new provider-calling CLI, offline job, script or test harness counts too. The provider risks the checklist covers (secret hygiene, fail-soft, resource exhaustion, prompt injection) apply regardless of which service is called or which process calls it.
Otherwise set it false — do not stretch the test to fit an unrelated diff.

Finally, set \`addsProviderCallPath\` for the narrower CONTENTION case, which routes the safety lens. The test is capability to contend with the roast advisor, NOT whether the remote backend is byte-identical: a path to a *separate* model service still contends if it can consume the same host CPU, event loop, memory, network, or provider rate limit during an active roast (checklist class 6 explicitly covers bounding provider AND CPU contention). Set it true whenever the diff adds such a path, EVEN IF it already appears to carry an active-roast admission guard, a lock, or a queue — whether that mitigation is correct is precisely what the safety lens exists to verify, so a present-looking guard is a reason to route the review, never a reason to skip it. "New path" means new REACHABILITY, not a new call site: a diff adding an endpoint, route, job, or service method that reaches a provider through an EXISTING helper still counts. Set it FALSE when the diff only modifies an EXISTING provider path without creating a new way to reach one (tweaking the roast advisor's own integration is not a new path), and when it reaches no provider at all.

Do NOT review the content yet.`,
  { label: 'scope', phase: 'Scope', schema: SCOPE_SCHEMA, model: REVIEW_MODEL, effort: 'high' },
)
if (scope) log(scope.summary)

// Fail CLOSED on an unusable scope result. agent() returns null on a schema-validation
// failure, a terminal API error, or a user skip — and every conditional lens below is
// gated on `scope`, so a null would silently drop safety, security AND ui, letting the
// always-on lenses come back empty and the run report CLEAR TO MERGE with no security
// review having happened. An unknown diff is treated as touching everything.
const scopeUnknown = !scope
if (scopeUnknown) {
  log('scope agent returned no usable result — running ALL conditional lenses (failing closed)')
}

// --- Phase 2: Review — fan out the roster as lenses ---------------------------
phase('Review')
const FINDINGS_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['findings'],
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['title', 'severity', 'file', 'detail'],
        properties: {
          title: { type: 'string' },
          severity: { type: 'string', enum: ['must-fix', 'fix-now', 'nit'] },
          file: { type: 'string' },
          line: { type: ['integer', 'null'] },
          detail: { type: 'string' },
          suggestion: { type: 'string' },
        },
      },
    },
  },
}
const reviewBase = `Review ONLY the changes in ${diffScope} of this repo (read the changed files for context; ignore pre-existing issues outside the diff). Return findings as structured data — an empty list if the diff is clean from your lens.\n\nCOVERAGE: report EVERY in-scope issue you find, including uncertain and low-severity ones — attach a severity to each, and state your confidence in the detail when a finding is uncertain. Do NOT self-filter for importance or withhold a finding you judge below some bar: a separate downstream verify stage adversarially filters false positives, so your job at this stage is recall, not precision.\n\nSCOPE OVERRIDE: use ${diffScope} as the diff for this run. If your own agent definition tells you to scope with a different command (several hard-code a diff against origin/main), ignore that instruction here — this run may target a non-default base.\n\nSEVERITY MAPPING: the schema accepts EXACTLY 'must-fix', 'fix-now' or 'nit'. Your own agent definition probably uses a different vocabulary. Do NOT emit yours. Map EVERY finding onto one of the three by rank, whatever words you would normally use: your most severe / blocking tier -> 'must-fix'; your middle tier -> 'fix-now'; your lowest, informational or cosmetic tier -> 'nit'. (Worked examples: security-reviewer's blocker/medium/low and safety-reviewer's blocker/concern/note both map in that order.) If a severity has no obvious rank, choose the closest and say so in the detail. Never emit a value outside the three — the response fails schema validation, and this workflow treats that as your lens having failed to run at all, which blocks the whole review.`

// Always-on lenses; safety, security + ui are added when the diff touches them —
// or unconditionally when the scope result is unusable (fail closed, see above).
const lenses = [
  { key: 'correctness', prompt: `${reviewBase}\n\nLens: CORRECTNESS — logic bugs, edge cases, error handling, races, off-by-one, missing await, broken invariants.` },
  { key: 'qa', agentType: 'qa', prompt: `${reviewBase}\n\nLens: TEST QUALITY — do tests assert real behavior (not smoke)? Coverage delta, acceptance criteria with no test, Playwright/screenshot paths, over-mocking. Findings = weak/missing tests.` },
  { key: 'product', agentType: 'product-pm', prompt: `${reviewBase}\n\nLens: PRODUCT/PLAN — does it match the plan/epic/decisions? Dropped requirements, undefined "done", drift between registry/epic tables and code, violated architecture invariants. Review only — do NOT edit anything.` },
]
// Safety fires on the file-based surface, and ALSO on a new provider-calling path:
// checklist class 6 (contention with the roast advisor) is safety-adjacent, and
// AGENTS.md routes it to both reviewers.
if (scopeUnknown || scope.touchesSafetyControllerEnums || scope.addsProviderCallPath) {
  lenses.push({ key: 'safety', agentType: 'safety-reviewer', prompt: `${reviewBase}\n\nLens: SAFETY (adversarial) — any roaster write bypassing safety, transition-table errors, string-compared verdicts, restart auto-resume, non-Celsius, fail-open paths.${scopeUnknown ? ' Scope could not be determined for this run, so check IF this diff adds a provider-calling path; if it does, also work checklist class 6 — it must not begin during an active roast or delay an operator roast start, and admission must be race-free under the roast-start lock.' : scope.addsProviderCallPath ? ' This diff adds a NEW provider-calling path: also check checklist class 6 — it must not begin during an active roast or delay an operator roast start, and admission must be race-free under the roast-start lock.' : ''}` })
}
// Capability-based routing (AGENTS.md): a new fetch/parse surface is the highest-risk
// case and the easiest to miss, because it can touch no safety file at all — #587 is
// the specimen. Keeps the roster pass and the pre-open pr-preflight pass in agreement.
// OR'd with addsProviderCallPath even though the prompt describes it as a strict subset:
// the two are independent model-filled booleans with no enforced dependency, so an
// inconsistent {touchesExternalInput:false, addsProviderCallPath:true} would otherwise
// skip security-reviewer on exactly the class-6 path this routing exists to cover.
if (scopeUnknown || scope.touchesExternalInput || scope.addsProviderCallPath) {
  lenses.push({ key: 'security', agentType: 'security-reviewer', prompt: `${reviewBase}\n\nLens: WEB/APPLICATION SECURITY — work docs/review/untrusted-input-checklist.md in full against this diff: SSRF / fetch-destination control, secret + PII hygiene, resource exhaustion (timeouts, byte caps, bounded decompression, ReDoS, concurrency/rate bounds), fail-soft typed errors (never an unhandled 500) mapped by origin, normalization consistency, cross-feature contention, LLM prompt-injection + tool boundary, and invariant separation. Prefer a CLASS-SWEEP: on finding one instance of a class, grep for every instance and report them together. Cite file:line. A clean pass is a valid result — do not invent findings.` })
}
if (scopeUnknown || scope.touchesWeb) {
  lenses.push({ key: 'ui', agentType: 'ui-reviewer', prompt: `${reviewBase}\n\nLens: UI/UX (code-level — no live browser this run) — review changed web/ components against component plan §7, ui-prompts.md, and the frozen baselines in the plan repo sketches/: five-series curve, verdict badges (ALLOW/CLAMP/REJECT), phase-from-server-events-only, Celsius, rebuild-not-port. Check the required Playwright/screenshot states exist as tests. Note that the full visual screenshot pass needs the live replay harness (a separate step).` })
}

const reviews = await parallel(
  lenses.map((l) => async () => {
    const opts = { label: `review:${l.key}`, phase: 'Review', schema: FINDINGS_SCHEMA }
    // A lens with an agentType resolves its model + effort from that (now full-ID)
    // agent definition; a lens without one (correctness) would inherit the caller,
    // so pin it explicitly to the review model.
    if (l.agentType) opts.agentType = l.agentType
    else {
      opts.model = REVIEW_MODEL
      opts.effort = 'high'
    }
    const r = await agent(l.prompt, opts)
    // Fail CLOSED, same reasoning as the scope gate: a lens that returned nothing did
    // NOT pass — it failed to run. Flattening that to an empty findings list makes a
    // dead security-reviewer indistinguishable from a clean one, which is the single
    // most dangerous way for this workflow to be wrong. Emit a synthetic must-fix so
    // the run blocks and says why.
    if (!r) {
      return {
        lens: l.key,
        findings: [
          {
            title: `The ${l.key} lens returned no result — it did not review this diff`,
            severity: 'must-fix',
            file: '.claude/workflows/review-branch.mjs',
            line: null,
            detail: `The ${l.key} review agent returned nothing (schema-validation failure, terminal API error, or skip). This is NOT a clean pass from that lens — the diff went unreviewed by it. Re-run before trusting any verdict.`,
            suggestion: `Re-run review-branch, or run the ${l.key} reviewer standalone against the branch.`,
            lensFailure: true,
          },
        ],
      }
    }
    return { lens: l.key, findings: r.findings || [] }
  }),
)
const allFindings = reviews
  .filter(Boolean)
  .flatMap((r) => r.findings.map((f) => ({ ...f, lens: r.lens })))
log(`${allFindings.length} raw findings across ${lenses.length} lenses`)

if (allFindings.length === 0) {
  return {
    verdict: scopeUnknown ? 'CLEAR TO MERGE (degraded — scope unknown)' : 'CLEAR TO MERGE',
    scope: scope ? scope.summary : null,
    scopeUnknown,
    lenses: lenses.map((l) => l.key),
    rawFindings: 0,
    survivors: 0,
    note: scopeUnknown
      ? 'no findings from any lens, but the scope agent failed — every conditional lens was run blind; re-run before trusting this verdict'
      : 'no findings from any lens',
  }
}

// --- Phase 3: Verify — adversarially refute each finding ----------------------
phase('Verify')
const VERDICT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['survives', 'reason'],
  properties: { survives: { type: 'boolean' }, reason: { type: 'string' } },
}
// A lens-failure finding is a FACT about this run, not a claim about the diff, so it
// never goes to a refuter — an adversarial verifier reading the cited file would find
// nothing wrong with it and vote survives=false, silently restoring the fail-open the
// synthetic finding exists to prevent. It bypasses Verify and lands straight in Triage.
const lensFailures = allFindings.filter((f) => f.lensFailure)
// A ui lens FAILURE is advisory exactly like a ui finding — D24 keeps direction-match
// review off the merge gate — so it must never enter the triage payload (#680 P2).
// Leaving it in `survivors` let the unconstrained triage agent return BLOCK on a
// ui-only failure, and `forcedBlock` (which correctly excludes ui) could not veto that
// verdict. Blocking (non-ui) lens failures DO stay in `survivors` so the triage report
// lists them; they force BLOCK deterministically in code regardless.
const blockingLensFailures = lensFailures.filter((f) => f.lens !== 'ui')
const uiLensFailures = lensFailures.filter((f) => f.lens === 'ui')
const claimFindings = allFindings.filter((f) => !f.lensFailure)
if (blockingLensFailures.length) {
  log(`${blockingLensFailures.length} lens(es) failed to return a result — bypassing Verify, blocking`)
}
if (uiLensFailures.length) {
  log(`${uiLensFailures.length} ui lens failure(s) — advisory (D24), reported but not blocking`)
}
const verified = await parallel(
  claimFindings.map((f) => () =>
    agent(
      `A reviewer raised this finding on ${diffScope}:\n\nTitle: ${f.title}\nSeverity: ${f.severity}\nFile: ${f.file}${f.line ? ':' + f.line : ''}\nDetail: ${f.detail}\n\nRun ${diffScope} and read the cited file for context first, then adversarially VERIFY it against the actual diff + code. Try to REFUTE it. Does it survive — a real, in-scope issue this change introduced? Default to survives=false if uncertain, already-handled, pre-existing, or out of scope.`,
      f.lens === 'safety'
        ? { label: `verify:${f.lens}`, phase: 'Verify', schema: VERDICT_SCHEMA, model: SAFETY_VERIFY_MODEL, effort: 'xhigh' }
        : { label: `verify:${f.lens}`, phase: 'Verify', schema: VERDICT_SCHEMA, model: REVIEW_MODEL, effort: 'high' },
    ).then((v) => ({
      ...f,
      // A dead verifier is NOT a refutation (#680 P1). agent() returns null on a
      // schema-validation failure, a terminal API error, or a user skip; treating
      // that as survives=false would silently DROP a real finding — and if it were
      // the only finding, triage would receive an empty set and return CLEAR TO
      // MERGE. Keep the finding unrefuted (survives=true) so it escalates to triage
      // instead. Failing closed here means at worst an unverified finding reaches
      // triage, which is the safe direction — mirrors the lens-failure treatment.
      survives: v ? v.survives : true,
      // `verified` records whether the verifier actually RAN (vs. died). Kept alive
      // above is only half the fix — the marker must reach triage + the report, or an
      // unverified finding is indistinguishable from a verified survivor and can be
      // silently rejected (#680 Codex P2). Propagated into the payload and used for a
      // deterministic severity-gated block below.
      verified: !!v,
      verifyReason: v ? v.reason : 'verifier failed to run — kept unrefuted (fail closed)',
    })),
  ),
)
const survivors = [...blockingLensFailures, ...verified.filter(Boolean).filter((f) => f.survives)]
log(
  `${survivors.length - blockingLensFailures.length}/${claimFindings.length} findings survived adversarial verification` +
    (blockingLensFailures.length ? ` (+${blockingLensFailures.length} blocking lens failure(s), unrefutable)` : ''),
)

// Nothing left to adjudicate: every claim finding was refuted and no blocking lens
// failed. A ui-only lens failure lands here too — report it (advisory, D24) but do NOT
// spin up triage, so a null/degraded triage result can never flip a ui-only failure to
// BLOCK (#680 P2).
if (survivors.length === 0) {
  return {
    verdict: scopeUnknown ? 'CLEAR TO MERGE (degraded — scope unknown)' : 'CLEAR TO MERGE',
    scope: scope ? scope.summary : null,
    scopeUnknown,
    lenses: lenses.map((l) => l.key),
    rawFindings: allFindings.length,
    survivors: 0,
    uiLensFailures: uiLensFailures.length
      ? uiLensFailures.map((f) => `${f.lens} lens failed to run (advisory, D24 — not blocking)`)
      : undefined,
    note: uiLensFailures.length
      ? 'all claim findings refuted; ui lens failed to run (advisory — reported, not blocking)'
      : 'all findings refuted under adversarial verification',
  }
}

// --- Phase 4: Triage — one consolidated verdict -------------------------------
phase('Triage')
const TRIAGE_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['verdict', 'summary', 'mustFix', 'fixNow', 'defer', 'rejected'],
  properties: {
    verdict: { type: 'string', enum: ['CLEAR TO MERGE', 'BLOCK'] },
    summary: { type: 'string' },
    mustFix: { type: 'array', items: { type: 'string' } },
    fixNow: { type: 'array', items: { type: 'string' } },
    defer: { type: 'array', items: { type: 'string' } },
    rejected: { type: 'array', items: { type: 'string' } },
  },
}
// A verifier that DIED (returned null) leaves the finding unrefuted, not confirmed.
// Surface that to triage AND force a deterministic block for anything that would
// actually gate merge — a must-fix/fix-now finding that was never adversarially
// verified must not be silently rejected into CLEAR TO MERGE. A nit that couldn't be
// verified is surfaced but left to triage (it can't block on its own anyway). This
// mirrors the lens-failure treatment at the finding grain (#680 P1 + Codex P2).
// EXCLUDE ui exactly as blockingLensFailures does: a ui direction-match/visual finding
// is advisory under D24 and must never deterministically block, so an unverified ui
// finding goes to triage (where the advisory-vs-SPA-invariant-violation call is made)
// rather than force-blocking here solely because verification failed (#680 Codex P2 #2).
const unverifiedBlockers = survivors.filter(
  (f) =>
    f.verified === false &&
    f.lens !== 'ui' &&
    (f.severity === 'must-fix' || f.severity === 'fix-now'),
)
const payload = survivors.map((f) => ({
  lens: f.lens,
  severity: f.severity,
  title: f.title,
  file: f.file,
  detail: f.detail,
  suggestion: f.suggestion,
  // undefined for lens-failure synthetics (they force-block in code regardless).
  verified: f.verified,
  verifyReason: f.verified === false ? f.verifyReason : undefined,
}))
const triage = await agent(
  `You are the pr-triage adjudicator. Consolidate these adversarially-verified findings on the current branch into a single triage report. Classify each: must-fix (correctness/safety/unmet acceptance/coverage regression — blocks merge), fix-now (cheap, clearly correct), defer (file a follow-up issue), rejected (with reason) — map any 'nit'-severity finding to defer or rejected. Then a single verdict: BLOCK if any must-fix OR any fix-now remains outstanding, else CLEAR TO MERGE. 'fix-now' means the fix has not been made yet, so it is not clear to merge — a security or safety finding of middling severity must not read as CLEAR TO MERGE just because it was cheap to fix. EXCEPTION, and read it precisely: a 'ui' finding is advisory ONLY when it is a direction-match or visual-judgement deviation — D24 keeps THAT off the merge gate, because the scripted Playwright snapshot suite is the gate. Classify those defer and never let one alone drive BLOCK. This does NOT extend to architecture invariants: a ui finding that the SPA infers roast phase locally, calls MCP directly, renders Fahrenheit, or otherwise breaks an AGENTS.md invariant is must-fix and DOES block, exactly like the same violation from any other lens. The ui lens is often the only one positioned to see those, so advisory-by-default must not swallow them. Be the skeptical second opinion — do not rubber-stamp.\n\nNOTE: any finding titled 'The <lens> lens returned no result' means that reviewer FAILED TO RUN. It is not a claim you can refute or defer — that lens simply did not review the diff, so classify it must-fix. (A 'ui' lens failure is advisory under D24 and is filtered out upstream, so it never reaches you; every lens failure you DO see also forces BLOCK in code, so a contrary verdict here would just be inconsistent.)\n\nNOTE: a finding with \`verified: false\` was kept alive because its adversarial verifier DIED (its \`verifyReason\` says so), NOT because it was confirmed real. Do not treat that as low confidence and reject it as unfounded — treat it as unresolved. A non-ui unverified must-fix or fix-now finding forces BLOCK deterministically in code, so calling it CLEAR TO MERGE would just be inconsistent. A 'ui' unverified finding is the ONE exception: it still follows the ui rule above (advisory when a direction-match/visual deviation, must-fix only when it breaks an AGENTS.md SPA invariant) — being unverified does not upgrade it to a blocker.\n\nFindings:\n${JSON.stringify(payload, null, 2)}`,
  { label: 'triage', phase: 'Triage', schema: TRIAGE_SCHEMA, agentType: 'pr-triage' },
)

// A failed component BLOCKS deterministically, in code — never at the triage agent's
// discretion. pr-triage is an unconstrained LLM that could classify a "did not run"
// finding as "defer" or "rejected" and hand back CLEAR TO MERGE, which would undo the
// whole fail-closed chain at the last step. Neither "a lens did not run" nor "a
// must-fix/fix-now finding's verifier died" is a judgement call.
// The ui lens is advisory by design (D24 keeps direction-match review off the merge
// gate), so its FAILURE cannot block either — blocking on it would contradict the
// triage rule that its findings never drive BLOCK. It is still reported.
// Two deterministic block sources, neither at the triage agent's discretion: a
// non-ui lens that failed to run, and a must-fix/fix-now finding whose verifier died
// (unverifiedBlockers). Both are "a component did not run", the #678/#680 theme.
const blockReasons = [
  ...blockingLensFailures.map((f) => `${f.lens} lens failed to run`),
  ...unverifiedBlockers.map((f) => `${f.severity} finding unverified (verifier failed): ${f.title}`),
]
const forcedBlock = blockReasons.length > 0
return {
  verdict: forcedBlock ? 'BLOCK' : triage ? triage.verdict : 'BLOCK',
  blockedBy: forcedBlock ? blockReasons : undefined,
  uiLensFailures: uiLensFailures.length
    ? uiLensFailures.map((f) => `${f.lens} lens failed to run (advisory, D24 — not blocking)`)
    : undefined,
  // Every survivor whose verifier died, surfaced regardless of severity (the must-fix/
  // fix-now ones also drive forcedBlock above; nits are reported but do not block).
  verifierFailures: survivors
    .filter((f) => f.verified === false)
    .map((f) => `${f.severity} [${f.lens}] ${f.title} — ${f.verifyReason}`),
  scope: scope ? scope.summary : null,
  scopeUnknown,
  lenses: lenses.map((l) => l.key),
  rawFindings: allFindings.length,
  survivors: survivors.length,
  triage,
}
