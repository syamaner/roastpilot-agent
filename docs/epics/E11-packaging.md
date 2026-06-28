# E11 — Packaging

## Goal

Ship RoastPilot as a **headless Raspberry Pi 5 appliance, installed NATIVELY** (D27):
a `roastpilot-agent[pi]` PyPI wheel (bundles the built `web/dist`, declares the
**torch-free** `coffee-roaster-mcp` + Pi extras) installed via **pipx**, a one-line
installer, a **bundled/offline FC model**, **one systemd unit** (agent spawns MCP as
child, per D6), mDNS, and a deployment doc. **No Docker image; no PyTorch on the Pi**
(D27).

> **Cross-repo dependency (D27 / `torch-free-pi-appliance.md`):** E11 is Phase 3 of a
> 3-repo rollout, **gated on the torch-free `coffee-roaster-mcp`** (Phase 2), which is
> gated on the FC-detection repo's librosa-filterbank swap + accuracy gate (Phase 1).
> E11 can be *specced/built* against the contract now, but the `[pi]` extra pins the
> torch-free MCP release.

> **Operator manual-test gate (D28) — ✅ CLEARED (28 Jun 2026).** Both operator-owned
> (@syamaner) manual test tasks are Done. **#135** (E10-S6 manual Safari/iPadOS SSE on
> real devices) is **✅ DONE/CLOSED** (13 Jun, iPad + iPhone Safari). **#134** (E12-S1
> supervised hardware roast through the agent harness, D17 criterion 3) is **✅ VALIDATED
> by roast 6** (27 Jun — auto-FC detection + advisor dev%-gated drop + full charge→drop
> recording, supervised, clean light roast). **The D28 gate no longer blocks E11.**
>
> **Still gated on D27 (independent):** the **torch-free `coffee-roaster-mcp`** chain
> (Phase 1 `coffee-first-crack-detection#54` → Phase 2 `coffee-roaster-mcp#157`, both
> cross-repo) — E11's `[pi]` extra pins the torch-free MCP release, so do not pin/ship
> the `[pi]` extra until that lands. Contract-buildable scaffolding (S1/S2 against the
> contract) is now startable on operator opt-in; E11-S3 (the Pi soak) depends on the
> recording bundle that shipped in MCP 0.1.10/0.1.11 (see below). (Prove the harness on
> real hardware + devices before packaging it.)

## Plan links

- **D27** (Pi appliance distribution: native-only, bundled model, torch-free) +
  the cross-repo rollout `roastpilot-plan/torch-free-pi-appliance.md`.
- Component plan §7 (packaging paragraph), §11.3 (hatchling build-hook open
  item): `roastpilot-plan/roastpilot-agent/plan.md`
- 00-repository-structure D1 (SPA ships inside the wheel):
  `roastpilot-plan/00-repository-structure.md`

## Stories

### E11-S1 — Wheel with bundled SPA + the `[pi]` extra

Acceptance criteria:

- [ ] `web/dist` built in CI (Node step) and included in the wheel via a
  hatchling force-include/build hook. (**`api.py` serving the SPA as static files is
  already DONE** — the static mount + `serve`/`--replay` `--spa-dir` landed early in the
  13 Jun live-serve bridge, #143/#154; what remains here is the CI build + wheel
  force-include of `web/dist`.)
- [ ] A **`pi` optional-dependency extra** declares the **torch-free**
  `coffee-roaster-mcp` (pinned) + the Pi runtime deps (`onnxruntime`, `librosa`,
  `soundfile`, `sounddevice`, `numpy`, …) — **no `torch`/`transformers`**. The base
  wheel stays lean; `roastpilot-agent[pi]` pulls the appliance set.
- [ ] Built-wheel smoke test in CI (install into clean venv, CLI + health route);
  add an **arm64** smoke (qemu or an arm runner) so the Pi target isn't unverified.
- [ ] Build-hook approach recorded in plan §11 (closes open item 3;
  fallback: commit built dist for the first release).

### E11-S2 — Native installer, systemd unit, bundled model, deploy doc

Acceptance criteria:

- [ ] **One-line installer** (`curl … | bash`, idempotent): `apt install
  libportaudio2`; `pipx install roastpilot-agent[pi]`; place the **bundled/pinned FC
  model** locally (offline — a roast never waits on a live HF pull; verify checksum);
  add the operator to `dialout`+`audio`; write the systemd unit; enable **avahi/mDNS**.
- [ ] **systemd unit:** one service, agent spawns MCP stdio child; restart lands in
  the recovery flow (**never auto-resumes heat/fan**); `journalctl` logs.
- [ ] **Headless UX:** power on → autostart → reach the UI at
  `http://roastpilot.local:<port>` from any device on the LAN (no local display).
- [ ] **Deployment doc:** Pi 5 + **official 27 W PSU + active cooler** prereqs (the
  FC inference is CPU-heavy), config (env: OpenRouter key, port), data location,
  upgrade (`pipx upgrade`), log access, the mDNS access story. Follows the plan's
  accuracy boundaries (no "fully autonomous"/"production-ready" pre-hardware-validation).

### E11-S3 — Pi 5 dual-mic recording + FC-detection CPU soak (overflow validation)

The dual-mic roast audio capture (#176) is **CPU-heavy and shares the audio path with
FC detection**, so on the constrained Pi it must be soak-validated before the appliance
ships with recording on.

**Why this story exists (roast 5, 27 Jun):** on the *Mac*, the recording WAV flush
packed each 16k-sample block one sample at a time via `struct.pack` in a Python loop
(GIL held ~3.6 ms), in the detector capture worker **and** the 2nd-mic thread — that
stalled the detector read enough to overflow the mic input **30 consecutive reads →
audio faulted, FC dead, roast aborted**. Fixed in **coffee-roaster-mcp#180**
(numpy-vectorised flush, 0.28 ms, 13×, byte-identical PCM16); a 2.5-min Mac soak at
`onnx_threads=8`, both mics, then showed **max 1 consecutive overflow, no fault**.

**The Pi risk:** the Mac has huge CPU headroom that hid the margin. The Pi 5 is far
tighter — RP1 xHCI, fewer/slower cores, **`onnx_threads=2`**, int8 — running the 2-mic
capture + 2 WAV writers + ONNX inference + the USB-serial on one budget. The #180 fix is
**necessary but may not be sufficient** on the Pi.

Acceptance criteria:

- [ ] **Sustained soak on the Pi 5** (real appliance load, several minutes, both mics +
  detector + a live/dry roast): the consecutive-overflow counter stays well under the
  fatal threshold and the audio never faults. Method: the `audio.py` "overflowed (N
  consecutive)" log + the dashboard mic-status — the same gate used on the Mac soak.
- [ ] If the Pi overflows even with #180, apply optimisation levers (in order) and
  re-soak: **(a)** move the teed WAV write off the detector read loop — bounded queue +
  a separate writer thread (the structural decouple, the #180 follow-on); **(b)** lower
  the recording flush threshold; **(c)** drop to **single-mic capture on the Pi** (the
  detector mic only, or one extra); **(d)** trim the detector cost (window/overlap/
  threads); **(e)** fall back to a separate capture process. Record which lever the Pi
  needed.
- [ ] Deployment doc notes the recording CPU cost + the validated appliance config (mic
  count, `onnx_threads`, flush threshold).

Depends on coffee-roaster-mcp#180 (the flush fix) + #176 (recording) — **both now SHIPPED:
#176 (capture) in MCP 0.1.9, #180 + #162 in MCP 0.1.10, #181 (full-roast recorder lifecycle) + #178 (live mic peak/RMS
levels) in MCP 0.1.11, agent pinned 0.1.11 (`pyproject.toml:131`); the recording now spans
charge→drop on the Mac. The Pi-5 soak is the remaining open validation.** Pairs with the
local Pi dual-mic capture validation (research 27 Jun: no published Pi-5 CPU numbers; the
CM4 dwc2 USB gap does not apply to the Pi 5's RP1 xHCI; independent streams are not
sample-locked, which is fine for FC training).

## Status

| Story | Title | Status |
|-------|-------|--------|
| E11-S1 | Wheel with bundled SPA + the `[pi]` extra | not started |
| E11-S2 | Native installer, systemd unit, bundled model, deploy doc | not started |
| E11-S3 | Pi 5 dual-mic recording + FC-detection CPU soak (overflow validation) | not started |

Epic status: **not started — D28 manual-test gate ✅ CLEARED (28 Jun 2026); now gated only
on the D27 torch-free chain.** The **operator manual tests** (D28) are both Done — **#135
✅** (device SSE) and **#134 ✅ validated by roast 6** (27 Jun). The remaining gate is the
**torch-free `coffee-roaster-mcp`** (D27 rollout Phase 2 = `coffee-roaster-mcp#157`, gated
on FC-repo Phase 1 `coffee-first-crack-detection#54` — both cross-repo, NOT this repo's
#134/#135/#54/#157); E11's `[pi]` extra pins that release. S1/S2 contract-buildable
scaffolding is now startable on operator opt-in. **E11-S3 logged:** the recording bundle it
soaks shipped in MCP 0.1.10/0.1.11 (#180/#162/#181/#178; agent pinned 0.1.11), so the Mac
side is validated and the Pi-5 CPU soak is the open work. Re-sliced for native-only +
torch-free + bundled-model distribution (D27, 11 Jun 2026); manual-test gate recorded as
D28 (13 Jun 2026), cleared 28 Jun 2026.
