export const meta = {
  name: 'review-branch',
  description:
    'Cross-checked roster review of the current branch diff: fan out the role agents as lenses (correctness, qa, product-pm, + safety/ui when relevant), adversarially verify each finding, then a single pr-triage verdict.',
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
const base = (args && args.base) || 'main'
const diffScope = `\`git diff ${base}...HEAD\``

// --- Phase 1: Scope — what does the diff touch? -------------------------------
phase('Scope')
const SCOPE_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['touchesWeb', 'touchesPython', 'touchesSafetyControllerEnums', 'changedFiles', 'summary'],
  properties: {
    touchesWeb: { type: 'boolean' },
    touchesPython: { type: 'boolean' },
    touchesSafetyControllerEnums: { type: 'boolean' },
    changedFiles: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string' },
  },
}
const scope = await agent(
  `Scope a code review of the current branch. Run \`git diff --name-only ${base}...HEAD\` and \`git diff --stat ${base}...HEAD\` in this repo. Report which areas changed: web/ (the SPA), Python (src/roastpilot_agent or tests/), and specifically safety.py / controller.py / a models.py enum (the safety-critical surface). List the changed files and a one-line summary. Do NOT review the content yet.`,
  { label: 'scope', phase: 'Scope', schema: SCOPE_SCHEMA },
)
if (scope) log(scope.summary)

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

// Always-on lenses; safety + ui are added only when the diff touches them.
const lenses = [
  { key: 'correctness', prompt: `${reviewBase}\n\nLens: CORRECTNESS — logic bugs, edge cases, error handling, races, off-by-one, missing await, broken invariants.` },
  { key: 'qa', agentType: 'qa', prompt: `${reviewBase}\n\nLens: TEST QUALITY — do tests assert real behavior (not smoke)? Coverage delta, acceptance criteria with no test, Playwright/screenshot paths, over-mocking. Findings = weak/missing tests.` },
  { key: 'product', agentType: 'product-pm', prompt: `${reviewBase}\n\nLens: PRODUCT/PLAN — does it match the plan/epic/decisions? Dropped requirements, undefined "done", drift between registry/epic tables and code, violated architecture invariants. Review only — do NOT edit anything.` },
]
if (scope && scope.touchesSafetyControllerEnums) {
  lenses.push({ key: 'safety', agentType: 'safety-reviewer', prompt: `${reviewBase}\n\nLens: SAFETY (adversarial) — any roaster write bypassing safety, transition-table errors, string-compared verdicts, restart auto-resume, non-Celsius, fail-open paths.` })
}
if (scope && scope.touchesWeb) {
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
    verdict: 'CLEAR TO MERGE',
    scope: scope ? scope.summary : null,
    lenses: lenses.map((l) => l.key),
    rawFindings: 0,
    survivors: 0,
    note: 'no findings from any lens',
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
      `A reviewer raised this finding on ${diffScope}:\n\nTitle: ${f.title}\nSeverity: ${f.severity}\nFile: ${f.file}${f.line ? ':' + f.line : ''}\nDetail: ${f.detail}\n\nAdversarially VERIFY it against the actual diff + code. Try to REFUTE it. Does it survive — a real, in-scope issue this change introduced? Default to survives=false if uncertain, already-handled, pre-existing, or out of scope.`,
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
  `You are the pr-triage adjudicator. Consolidate these adversarially-verified findings on the current branch into a single triage report. Classify each: must-fix (correctness/safety/unmet acceptance/coverage regression — blocks merge), fix-now (cheap, clearly correct), defer (file a follow-up issue), rejected (with reason). Then a single verdict: BLOCK if any must-fix, else CLEAR TO MERGE. Be the skeptical second opinion — do not rubber-stamp.\n\nFindings:\n${JSON.stringify(payload, null, 2)}`,
  { label: 'triage', phase: 'Triage', schema: TRIAGE_SCHEMA, agentType: 'pr-triage' },
)

return {
  verdict: triage ? triage.verdict : 'BLOCK',
  scope: scope ? scope.summary : null,
  lenses: lenses.map((l) => l.key),
  rawFindings: allFindings.length,
  survivors: survivors.length,
  triage,
}
