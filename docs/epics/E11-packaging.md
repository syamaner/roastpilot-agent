# E11 — Packaging

**5 Oct 2026 — D210/D211 retrospective cold qualification:** the original
completed supervised 10+10 cold run returned `NOT_CONFORMANT`, exit 6, because
its original D210 evaluator misclassified the exact MCP prepared-session reason.
That result, original evaluator/wheel identities and sealed bytes remain unchanged.
A separate offline policy 4 interpretation revision 1 assessment returned
`temperature_screened_conformant` with zero findings; the corrected wheel was
not installed on Pi. Independent safety, MCP-contract, QA, security and direct
Pi-evidence reviews completed. The final independent Pi reviewer returned PASS
for the narrow retrospective 10+10 cold qualification, accepting the separate
supplementary artifact receipt as host attestation. #954 stays OPEN; E11-S3 is
in progress. The separately authorised live acceptance and release gates remain
outstanding. This supersedes earlier E11-S3 physical-run and review status
snapshots. Historical policies 1–3 and original policy 4 remain reproducible.
Current runtime requires the revised exact result class/revision. See the D211
section of `docs/deployment/cold-characterisation-runbook.md` for evidence limits.


## Goal

Ship RoastPilot as a **headless Raspberry Pi 5 appliance, installed NATIVELY** (D27):
a `roastpilot-agent[pi]` PyPI wheel (bundles the built `web/dist`, declares the
**torch-free** `coffee-roaster-mcp` + Pi extras) installed via **pipx**, a one-line
installer, a **bundled/offline FC model**, **one systemd unit** (agent spawns MCP as
child, per D6), mDNS, and a deployment doc. **No Docker image; no PyTorch on the Pi**
(D27).

> **Cross-repo dependency (D27 / `torch-free-pi-appliance.md`):** E11-S1's D27
> dependency/publication gate is **cleared** by the published torch-free
> `coffee-roaster-mcp==0.2.0` release. This PR delivers E11-S1's `[pi]` extra
> against that exact release. `coffee-roaster-mcp#157` and #194 remain open for
> their separate hardware and acceptance boundaries; this package delivery is not
> Pi hardware-readiness or acceptance evidence.

> **Operator manual-test gate (D28) — ✅ CLEARED (28 Jun 2026).** Both operator-owned
> (@syamaner) manual test tasks are Done. **#135** (E10-S6 manual Safari/iPadOS SSE on
> real devices) is **✅ DONE/CLOSED** (13 Jun, iPad + iPhone Safari). **#134** (E12-S1
> supervised hardware roast through the agent harness, D17 criterion 3) is **✅ VALIDATED
> by roast 6** (27 Jun — auto-FC detection + advisor dev%-gated drop + full charge→drop
> recording, supervised, clean light roast) and **re-confirmed by roast 8** (28 Jun —
> LLM-advised drop through the safety box, clean medium ~193 °C / 21 % DTR).
> **The D28 gate no longer blocks E11.**
>
> **D27 E11-S1 dependency/publication gate — ✅ CLEARED:** the published torch-free
> `coffee-roaster-mcp==0.2.0` release permits the exact `[pi]` pin delivered here.
> E11-S2 is complete; E11-S3 is in progress. E11-S3 (the Pi soak) still depends on the
> recording bundle that shipped in MCP 0.1.10/0.1.11 (see below); the narrow
> D210/D211 cold qualification is recorded below, with separate
> live acceptance and release gates still outstanding.

## Plan links

- **D27** (Pi appliance distribution: native-only, bundled model, torch-free) +
  the cross-repo rollout `roastpilot-plan/torch-free-pi-appliance.md`.
- Component plan §7 (packaging paragraph), §11.3 (hatchling build-hook open
  item): `roastpilot-plan/roastpilot-agent/plan.md`
- 00-repository-structure D1 (SPA ships inside the wheel):
  `roastpilot-plan/00-repository-structure.md`

## Stories

### E11-S1 — Wheel with bundled SPA + the `[pi]` extra

**Delivered 5 Sep 2026:** E11-S1 publishes a `pi` optional dependency extra
pinned exactly to a torch-free `coffee-roaster-mcp` release; it was delivered
against `0.2.0`, and #954 has since unified the `[pi]` extra and the
development group on the same published `coffee-roaster-mcp==0.2.2`. The base
wheel remains lean: it has no unconditional MCP, `torch`, `torchaudio`, or
`transformers` requirement. The regenerated contract fixtures validate that
single release, and the Pi smokes still use their own separate venvs. The
package lane covers the base wheel on x86 and a
native hosted ARM64 runner covers `wheel[pi]`, its exact MCP pin, the CLI, and
the replay-mode bundled SPA. Hosted ARM64 evidence is package compatibility
only; it is not Raspberry Pi hardware validation.

Acceptance criteria:

- [x] `web/dist` built in CI (Node step) and included in the wheel via a
  hatchling force-include/build hook. (**`api.py` serving the SPA as static files is
  already DONE** — the static mount + `serve`/`--replay` `--spa-dir` landed early in the
  13 Jun live-serve bridge, #143/#154; what remains here is the CI build + wheel
  force-include of `web/dist`.) **DONE:** `hatch_build.py` custom hook (npm ci && npm
  run build → force-include at `roastpilot_agent/_web_dist`); `live.default_spa_dir()`
  now resolves packaged data via `importlib.resources` first, falling back to the
  source-checkout `web/dist` (editable installs skip the hook entirely). CI `package`
  job builds the real wheel + smoke-tests it in a clean venv.
- [x] A **`pi` optional-dependency extra** declares exactly the pinned,
  torch-free `coffee-roaster-mcp==0.2.2`; package metadata and clean-venv
  tests reject `torch`, `torchaudio`, and `transformers`. The base wheel stays
  lean; `roastpilot-agent[pi]` pulls only the appliance MCP dependency.
- [x] Built-wheel smoke tests in CI: x86_64 installs the base wheel; native
  hosted ARM64 builds its own wheel, installs `wheel[pi]` in a separate clean
  venv, verifies `aarch64` and MCP 0.2.2, and runs CLI and replay-mode SPA
  smokes. This hosted-runner proof is not Pi hardware validation.
- [x] Build-hook approach recorded in plan §11 (closes open item 3; fallback: commit
  built dist for the first release — **not needed**, the build hook shipped).

### E11-S2 — Native installer, systemd unit, bundled model, deploy doc

Acceptance criteria:

- [x] **One-line installer** (`curl … | bash`, idempotent): `apt install
  libportaudio2`; `pipx install roastpilot-agent[pi]`; place the **bundled/pinned FC
  model** locally (offline — a roast never waits on a live HF pull; verify checksum);
  add the operator to `dialout`+`audio`; write the systemd unit; enable **avahi/mDNS**.
- [x] **systemd unit:** one service, agent spawns MCP stdio child; restart lands in
  the recovery flow (**never auto-resumes heat/fan**); `journalctl` logs.
- [x] **Headless UX:** power on → autostart → reach the UI at
  `http://roastpilot.local:<port>` from any device on the LAN (no local display).
- [x] **Deployment doc:** Pi 5 + **official 27 W PSU + active cooler** prereqs (the
  FC inference is CPU-heavy), config (env: OpenRouter key, port), data location,
  upgrade (`pipx upgrade`), log access, the mDNS access story. Follows the plan's
  accuracy boundaries: it makes no autonomous-operation or release-suitability claim
  before the outstanding hardware evidence.

### E11-S3 — Pi 5 single-primary-mic complete-appliance cold characterisation (overflow validation)

**Scope (D194):** one primary microphone delivering one mono 16 kHz / 16-bit
stream, on the complete appliance (Pi 5, Hottop serial link, first-crack
detector and advisory-only advisor), run under
`docs/deployment/cold-characterisation-runbook.md`. Multi-microphone capture is
deferred. The #954 software for the supervised cold run is delivered; the
completed supervised 10+10 cold run now has narrow retrospective qualification
under policy 4 interpretation revision 1. This story is in progress, not done.

Acceptance criteria:

These are the active D194 criteria; the locked D191 limits apply unchanged.

- [x] A supervised cold run on the Pi 5 complete appliance, with the single
  primary mono 16 kHz / 16-bit stream and the detector active, across the full
  10-minute recording-off phase followed by the 10-minute recording-on phase,
  meets the locked D191 limits unchanged: at most N = 1 consecutive overflow,
  and at most X = 200 ms of peak trailing-60-second lost audio. The production
  fatal consecutive-overflow streak of 30 is unchanged. These locked limits
  permit their stated margin; failing to meet them fails qualification, and no
  limit is loosened in the light of results. Method: the `audio.py`
  "overflowed (N consecutive)" log and the dashboard mic status, recorded as
  run evidence. Cleared by the narrow D210/D211 retrospective policy 4
  interpretation revision 1 assessment of the completed supervised 10+10 run;
  the original `NOT_CONFORMANT`, exit 6, result and original evaluator identity
  remain unchanged.
- [x] Both retained evidence copies verify against the externally recorded
  receipt, and independent Pi evidence review clears. Cleared for the same
  narrow revision-1 qualification by contemporaneous two-copy SHA-256 equality
  proof and the final independent Pi review PASS. The separate supplementary
  artifact receipt was accepted as host attestation; the reviewer did not hear
  the audio or independently inspect the recovered WAV bytes. The original
  sealed tree and exit-6 result remain unchanged.
- [ ] The deployment doc notes the recording CPU cost and the frozen appliance
  configuration the run characterised (`onnx_threads`, flush threshold).

This cold run authorises no tuning. Any optimisation (for example the teed
WAV writer, the flush threshold, the detector window, overlap or threads, or a
separate capture process) is a separate, separately authorised change, never
part of this run. A material change to the frozen hardware, software, device or
configuration identity requires a fresh characterisation.

**History (superseded by D194; retained as history only).** The story was
originally a Pi 5 dual-mic recording plus FC-detection CPU soak. The dual-mic
roast audio capture (#176) is CPU-heavy and shares the audio path with FC
detection. On roast 5 (27 Jun), on the *Mac*, the recording WAV flush packed
each 16k-sample block one sample at a time via `struct.pack` in a Python loop
(GIL held ~3.6 ms) in the detector capture worker and the second-mic thread,
which stalled the detector read enough to overflow the mic input 30 consecutive
reads, faulting audio and aborting the roast. coffee-roaster-mcp#180 fixed it
(numpy-vectorised flush, 0.28 ms, byte-identical PCM16); a 2.5-minute Mac soak
at `onnx_threads=8` with both mics then showed at most 1 consecutive overflow
and no fault. The Pi 5 is far tighter (RP1 xHCI, fewer and slower cores,
`onnx_threads=2`, int8), so the #180 fix was judged necessary but possibly not
sufficient there. Recording shipped in MCP 0.1.9 (#176), 0.1.10 (#180, #162)
and 0.1.11 (#181, #178); the agent pin has since moved to 0.2.2. The earlier
research (27 Jun) found no published Pi 5 CPU numbers and noted that the CM4
dwc2 USB gap does not apply to the Pi 5's RP1 xHCI. The original optimisation
levers considered then (move the teed WAV write off the detector read loop,
lower the flush threshold, trim the detector cost, fall back to a separate
capture process) are historical context only and are not authorised by the
cold run.

## Status

| Story | Title | Status |
|-------|-------|--------|
| E11-S1 | Wheel with bundled SPA + the `[pi]` extra | done — base-wheel, `[pi]`, and native hosted ARM64 package smokes delivered 5 Sep 2026; hosted-runner evidence is not Pi hardware validation |
| E11-S2 | Native installer, systemd unit, bundled model, deploy doc | done — native installer, managed service/configuration, pinned local model, and deployment guide delivered 12 Sep 2026; package and documentation evidence is not Pi or physical-device validation |
| E11-S3 | Pi 5 single-primary-mic complete-appliance cold characterisation (overflow validation) | in progress |

Epic status: **in progress — E11-S1 and E11-S2 are done; E11-S3 is in progress.**
The **operator manual tests** (D28) are
both Done — **#135 ✅** (device SSE) and **#134 ✅ validated by roast 6** (27 Jun).
**E11-S1 is complete:** a hatchling custom build hook (`hatch_build.py`) runs the
SPA's `npm run build` and force-includes `web/dist` into the wheel at
`roastpilot_agent/_web_dist`; `live.default_spa_dir()` resolves it via
`importlib.resources` before falling back to the source-checkout path; a CI `package` job
builds the real wheel and smoke-tests it (CLI + a served-SPA fetch) in a clean venv. This
closes plan.md open item 3 (the build-hook approach shipped; the "commit built dist"
fallback was not needed). **Verified the base wheel stays lean:** the shipped wheel's
`Requires-Dist` has no `coffee-roaster-mcp`/`transformers`/`torch` — confirmed both from
the wheel's METADATA and from a clean-venv install's `pip list` — so nothing pulls the
heavy ML stack through transitively; `coffee-roaster-mcp` is a dev-group-only pin (tests
spawn it in mock-driver mode) and never a runtime dependency of the shipped artifact.
**E11-S1 now includes the `[pi]` extra:** it pins `coffee-roaster-mcp==0.2.2`, the same
release the development group pins for its mock-driver mirrors and fixtures (#954). The clean
native-hosted ARM64 smoke builds a wheel independently, installs `wheel[pi]` separately,
verifies its denylist and exact pin, and runs CLI/replay SPA smokes. This is package evidence
only, not validation on Pi hardware. **E11-S3
logged (historical; superseded by the D194 scope in the E11-S3 story above):** the recording bundle it soaks shipped in MCP 0.1.10/0.1.11
(#180/#162/#181/#178; agent pinned 0.1.11), so the Mac side is validated and the Pi-5 CPU
soak is the open work. Re-sliced for native-only + torch-free + bundled-model distribution
(D27, 11 Jun 2026); manual-test gate recorded as D28 (13 Jun 2026), cleared 28 Jun 2026.

**E11-S2 is complete (12 Sep 2026):** the native installer, managed systemd
unit and configuration, pinned local model installation, and
`docs/deployment/pi-appliance.md` are delivered. The guide documents explicit
`--set-hostname roastpilot` consent, inactive-only maintenance, rollback
messages, local data/configuration paths, air-gapped model sources, logs, mDNS,
and the trusted-home-LAN boundary. It does not turn hosted, mocked, package, or
documentation evidence into Pi or physical-device evidence. The narrow
D210/D211 retrospective cold qualification and independent Pi evidence review
are recorded above. Broader complete-appliance validation and separately
authorised supervised live-roast acceptance remain outstanding; E11-S3 is in progress.

**#954 cold characterisation (Oct 2026):**
At that time, the software for the supervised
cold characterisation is delivered across U1 (PR #993), U2 (PR #994), the U3
cold view (PR #995) and U4 (the cold CLI, same-loop hosting and
`docs/deployment/cold-characterisation-runbook.md`). #997 (D209) then delivered
the cold temperature software across S1 (PR #998), S2a (PR #999), T1 (PR #1000)
and T2 (the runtime activation and this runbook, registry and epic
reconciliation): each tick retains a paired temperature record, the D209 5 to
40 °C engineering screen aborts a phase without finalisation, an
operator-asserted reviewed MCP candidate is recorded per phase, and runtime
success now requires D210 conformance policy 4 for fixed 600-second phases.
Historical 30+30 evidence remains under policies 1–3; current advisory minutes
are 4–9 and the transition budget remains 60 seconds. The original D210 run
returned `NOT_CONFORMANT`, exit 6; its result and evaluator identity are preserved.
The separate D211 offline interpretation revision 1 returned
`temperature_screened_conformant`, zero findings, on the same unchanged sealed
records. The final independent Pi review passed the narrow retrospective 10+10
cold qualification, with supplementary artifact receipt accepted as host
attestation. The corrected wheel was not installed on Pi. The declared package
pin remains `coffee-roaster-mcp==0.2.2`; it does not prove installed bytes.
PR work is now permitted by operator sequencing. #954 stays OPEN and E11-S3 is
in progress; release and the separately authorised 20-minute live acceptance
remain outstanding. This establishes neither detector accuracy, calibration,
uninterrupted observation, deployment acoustics, full hardware readiness nor
30+30 completion. Bad-checksum frames skipped without a counter and
stalled-clock/observation residuals remain; independent operator emergency stop
is still required.
