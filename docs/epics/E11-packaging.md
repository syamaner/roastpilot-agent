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

> **BLOCKED — operator manual-test gate (D28):** do **not** begin E11
> implementation until **both** operator-owned (@syamaner) manual test tasks are Done.
> **#135** (E10-S6 manual Safari/iPadOS SSE on real devices) is **✅ DONE/CLOSED**
> (13 Jun, iPad + iPhone Safari). **#134** (E12-S1 supervised hardware roast through the
> agent harness, D17 criterion 3) is the **sole remaining gate** — operator running it
> 13 Jun, now with a persistent decision trace (#161). This is *separate from and
> additional to* the D27 torch-free gate above. Contract-buildable scaffolding may be
> pre-staged only on explicit operator opt-in. (Prove the harness on real hardware +
> devices before packaging it.)

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

## Status

| Story | Title | Status |
|-------|-------|--------|
| E11-S1 | Wheel with bundled SPA + the `[pi]` extra | not started |
| E11-S2 | Native installer, systemd unit, bundled model, deploy doc | not started |

Epic status: **not started — BLOCKED.** Two gates before any story starts: (1) the
**operator manual tests #134 + #135** (D28) and (2) the **torch-free
`coffee-roaster-mcp`** (D27 rollout Phase 2, which is gated on FC-repo Phase 1 #54).
Re-sliced for native-only + torch-free + bundled-model distribution (D27, 11 Jun
2026); manual-test gate recorded as D28 (13 Jun 2026).
