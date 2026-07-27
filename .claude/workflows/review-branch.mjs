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
    // have fired. Mirrors the "When this applies" test in
    // docs/review/untrusted-input-checklist.md.
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
const scope = await agent(
  `Scope a code review of the current branch. Run \`git fetch origin -q\` first so the base ref is current, then \`git diff --name-only ${base}...HEAD\` and \`git diff --stat ${base}...HEAD\` in this repo. Report which areas changed: web/ (the SPA), Python (src/roastpilot_agent or tests/), and specifically safety.py / controller.py / a models.py enum (the safety-critical surface). List the changed files and a one-line summary.

Also decide \`touchesExternalInput\`. Judge this by CAPABILITY, not by file path — read the changed hunks, because the highest-risk case (#587) touched no safety-critical file and a path allow-list would have missed it. Set it true if the diff, ON THE SERVER, does any of:
- fetches a URL / opens a connection whose target is influenced by operator or external input (a pasted URL, a redirect \`Location\`, a webhook, a config value);
- parses or decodes untrusted bytes/strings (URL parsing, HTML/JSON/charset decode, number/port parsing, deserialization);
- adds an external-input endpoint (a route taking client-supplied data);
- adds a **new** LLM/model-provider call path (anything that can contend with the roast advisor for the same backend).
Otherwise set it false — do not stretch the test to fit an unrelated diff.

Finally, set \`addsProviderCallPath\` true for the last bullet specifically. "New path" means new REACHABILITY to the shared provider, not a new call site: a diff that adds an endpoint, route, job, or service method which reaches the provider through an EXISTING helper still creates a new way to contend with the roast advisor, and counts. Only a diff that merely fetches or parses, with no route to the provider, does not. This is narrower than \`touchesExternalInput\` and routes the safety lens as well.

Do NOT review the content yet.`,
  { label: 'scope', phase: 'Scope', schema: SCOPE_SCHEMA },
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
const reviewBase = `Review ONLY the changes in ${diffScope} of this repo (read the changed files for context; ignore pre-existing issues outside the diff). Return findings as structured data — an empty list if the diff is clean from your lens.`

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
  lenses.push({ key: 'safety', agentType: 'safety-reviewer', prompt: `${reviewBase}\n\nLens: SAFETY (adversarial) — any roaster write bypassing safety, transition-table errors, string-compared verdicts, restart auto-resume, non-Celsius, fail-open paths.${!scopeUnknown && scope.addsProviderCallPath ? ' This diff adds a NEW provider-calling path: also check checklist class 6 — it must not begin during an active roast or delay an operator roast start, and admission must be race-free under the roast-start lock.' : ''}` })
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
    if (l.agentType) opts.agentType = l.agentType
    const r = await agent(l.prompt, opts)
    return { lens: l.key, findings: (r && r.findings) || [] }
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
const verified = await parallel(
  allFindings.map((f) => () =>
    agent(
      `A reviewer raised this finding on ${diffScope}:\n\nTitle: ${f.title}\nSeverity: ${f.severity}\nFile: ${f.file}${f.line ? ':' + f.line : ''}\nDetail: ${f.detail}\n\nRun ${diffScope} and read the cited file for context first, then adversarially VERIFY it against the actual diff + code. Try to REFUTE it. Does it survive — a real, in-scope issue this change introduced? Default to survives=false if uncertain, already-handled, pre-existing, or out of scope.`,
      { label: `verify:${f.lens}`, phase: 'Verify', schema: VERDICT_SCHEMA },
    ).then((v) => ({ ...f, survives: v ? v.survives : false, verifyReason: v ? v.reason : 'verifier failed' })),
  ),
)
const survivors = verified.filter(Boolean).filter((f) => f.survives)
log(`${survivors.length}/${allFindings.length} findings survived adversarial verification`)

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
const payload = survivors.map((f) => ({
  lens: f.lens,
  severity: f.severity,
  title: f.title,
  file: f.file,
  detail: f.detail,
  suggestion: f.suggestion,
}))
const triage = await agent(
  `You are the pr-triage adjudicator. Consolidate these adversarially-verified findings on the current branch into a single triage report. Classify each: must-fix (correctness/safety/unmet acceptance/coverage regression — blocks merge), fix-now (cheap, clearly correct), defer (file a follow-up issue), rejected (with reason) — map any 'nit'-severity finding to defer or rejected. Then a single verdict: BLOCK if any must-fix, else CLEAR TO MERGE. Be the skeptical second opinion — do not rubber-stamp.\n\nFindings:\n${JSON.stringify(payload, null, 2)}`,
  { label: 'triage', phase: 'Triage', schema: TRIAGE_SCHEMA, agentType: 'pr-triage' },
)

return {
  verdict: triage ? triage.verdict : 'BLOCK',
  scope: scope ? scope.summary : null,
  scopeUnknown,
  lenses: lenses.map((l) => l.key),
  rawFindings: allFindings.length,
  survivors: survivors.length,
  triage,
}
