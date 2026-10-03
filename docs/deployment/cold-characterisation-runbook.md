# Cold characterisation runbook (#954)

This runbook describes the operator-supervised cold characterisation action,
`roastpilot-agent cold-characterisation`. It covers prerequisites, the command,
monitoring, exit codes, evidence handling, signals and the known residuals.
Temperatures are Celsius throughout. The advisor is an LLM that is advisory
only: it returns typed data, it never receives a roaster write and it never
controls hardware.

## 1. Scope

The cold mode is explicit and default-off (D194). Nothing starts it except the
command below, and normal `serve` behaviour is unchanged.

The software for #954 is delivered. **Every operator-supervised gate in this
runbook is unexecuted**, and each one is evidence-led: it passes only on the
recorded evidence of a supervised run. Nothing here authorises beans, a live
roast, a readiness claim, a detector-accuracy claim or a deployment-acoustics
claim. #954 stays open, and E11-S3 is not started.

## 2. Prerequisites

- The roaster is empty and cold, and the run is supervised in person.
- **The independent operator emergency stop is required throughout the run.**
  The hosted app is read-only and cannot actuate an emergency stop.
- The appliance service is stopped, so neither the serial port nor port 8000 is
  held by another process.
- The shell exports no `COFFEE_*` variable, and the configured `mcp.env` is
  empty. The cold composition refuses either one.
- `ROASTPILOT_CONFIG_FILE` names a cold-specific configuration file whose
  recording fields are unset and whose MCP YAML source is an absolute path.
- The advisor credential is present in the environment under its configured
  variable name. Never paste it into the command line.
- The installed package pins `coffee-roaster-mcp==0.2.2`.
- The terminal session survives a disconnect (for example `tmux` or `screen`).
  SIGHUP is not handled.

## 3. Command

Every option except `--protected-root` may appear only once; a repeat, an
unknown option or a malformed value prints one fixed usage line and exits 2
without echoing the value. Config, input and host-fact refusals print only the
closed summary.

```bash
roastpilot-agent cold-characterisation \
  --profile-name '<explicit profile name>' \
  --target-drop-temp-c <Celsius, finite> \
  --evidence-dir /absolute/primary/evidence/root \
  --secondary-evidence-dir /absolute/secondary/evidence/root \
  --protected-root /absolute/protected/root \
  --audio-device-identity '<primary microphone identity>' \
  --serial-port-path /dev/<roaster serial device> \
  --stimulus-block '<stimulus block>' \
  --operator-host-notes '<host notes>' \
  --operator-psu-notes '<power supply notes>' \
  --operator-cooling-notes '<cooling notes>' \
  --source-revision <40 lowercase hex> \
  --source-tree clean \
  --artefact-kind wheel \
  --artefact-sha256 <64 lowercase hex> \
  --host 127.0.0.1 \
  --port 8000
```

- `--profile-name` and `--target-drop-temp-c` are explicit on every run. There
  is no default and no range: the target drop only has to be a finite Celsius
  value. No charge guidance is accepted.
- `--secondary-evidence-dir` is recorded only; the software never opens or
  writes it.
- `--protected-root` is optional and repeatable; nothing is added implicitly.
- `--artefact-sha256` is required exactly when `--artefact-kind` is `wheel` or
  `sdist`, and refused for `editable_source`.
- `--host` defaults to `127.0.0.1` and `--port` to `8000`. `--spa-dir` is
  optional; without it the bundled SPA build is used, and a missing build
  refuses the run.
- The provenance options (`--source-revision`, `--source-tree`,
  `--artefact-kind`, `--artefact-sha256`) are an **operator assertion**, not an
  attestation. The self-reported versions, the temporary directories and the
  source checkout do not attest the bytes or packages that actually run.

Once the HTTP server has started, the process prints one fixed line,
`roastpilot-agent cold-characterisation: run starting`, and only then invokes
the run.

## 4. Monitoring

- Prefer the loopback bind with an SSH tunnel. With a LAN bind, up to **four
  unauthenticated display clients** can occupy every cold-stream slot. That
  limits display availability; it does not affect run safety.
- The cold stream shows **retained display ticks before engine
  classification**. A displayed tick is never an accepted, safe or qualified
  tick, and the stream is never proof that observation continues during a clock
  stall.
- A disconnected client's slot is released at the next tick or heartbeat, when
  the server sees the ASGI disconnect. That is a bounded release, not a
  watchdog.
- Health reports `mcp_child: not_configured`. That **does not prove that no
  cold child exists**.
- The inherited read-only GET routes, including configuration and device
  enumeration, stay reachable. This is a known residual.
- The HTTP access log is off and every uvicorn log record is detail-free
  (`cold-http <LEVEL>`). Other package loggers are unchanged.

## 5. Exit codes and summary

Every ordinary exit prints one closed summary of exactly 15 `key=value` lines:
`mode`, `run_invoked`, `result`, `cli_refusal`, `composition_refusal`,
`outcome`, `start_refusal`, `termination_reason`, `child_ownership`,
`advisory_path`, `provider_check`, `conformance_outcome`, `manifest_sha256`,
`signal` and `exit_code`. Values are closed tokens; no host, port, path,
profile, note, credential or exception text is printed.

| Code | Meaning | What it does not mean |
|---|---|---|
| 0 | `ADVISORY_CONFORMANT` | Not qualification, readiness or acceptance |
| 2 | Usage error, or input the models refuse (`run_invoked=false`) | |
| 3 | CLI refusal: config, host facts, host reader, SPA, store, bind, HTTP start, or already run (`run_invoked=false`) | |
| 4 | Composition refusal; this invocation constructed no child | Not proof that no other child exists |
| 5 | `REFUSED_BEFORE_EVIDENCE` | |
| 6 | `NOT_CONFORMANT` | |
| 7 | `EVIDENCE_NOT_SEALED` | |
| 8 | Unadmitted result or propagated exception; this invocation's child status is unknown | Not proof that the child is absent |
| 130 / 143 | Cancelled by SIGINT / SIGTERM; before the run `run_invoked=false`, during it the child is unknown | Not proof that the child is absent |
| 80-83 | Pending provider call at the single post-seal check; self-termination | See below |
| 1 | Unexpected failure outside this runbook, including a cancellation no signal requested | |

- `run_invoked=false` means only that **this invocation started no cold
  child**. `child_ownership=none` means the run was not invoked;
  `child_ownership=unknown` means it was invoked without an admitted result.
- `NOT_OWNED`, `unknown`, exit 8, exit 130/143 and `OWNED_STOP_UNCONFIRMED`
  do not prove that the child is absent. Verify and contain the roaster
  independently before **any** further invocation. Each process runs one cold
  run only; a second attempt in the same process refuses with `already_run`.
- **Exit codes 80-83** mean the process terminated itself because the provider
  call was pending at the single post-seal check. The process prints nothing
  after the start line and therefore records **no receipt**, so that evidence
  tree has **no trusted receipt** and is diagnostic only. Codes 80 and 81 mean
  the engine's seal returned a digest; 82 and 83 mean it did not. Codes 81 and
  83 mean the stop of the owned child is unconfirmed. The private 0700
  `roastpilot-cold-*` and `roastpilot-cold-store-*` temporary directories
  (holding the operator YAML and the SQLite store) remain for operator cleanup,
  and the MCP child may be orphaned.

## 6. Evidence

On an ordinary exit with a `manifest_sha256` value, record that value
**externally** (for example in the supervision notes) as the receipt. Never
derive the receipt from the evidence tree itself.

The software writes only the primary root. The operator copies the run tree to
the secondary root. The run directory under the primary root is named by the
run ID. There is no CLI verifier; verification uses the existing Python
function:

```bash
python -c 'from roastpilot_agent.cold_characterisation.evidence_store import verify_retained_copies; print(verify_retained_copies("/absolute/primary/evidence/root", "/absolute/secondary/evidence/root", run_id="<run ID>", expected_manifest_sha256="<recorded receipt>"))'
```

Independent Pi evidence review follows separately.

## 7. Signals

- The first Ctrl-C or SIGTERM before the run starts means the run is never
  invoked; the process cleans up and exits 130 or 143.
- The first signal during the run triggers the engine's shielded cleanup and
  then the ordinary teardown.
- **Repeated signals neither force an exit nor cancel again.** There is no
  second-Ctrl-C force exit.
- An HTTP startup, port call or teardown that never returns is a progress
  limitation: there is no watchdog and no timeout.
- An operating-system kill is an operator action outside this software and
  leaves the child and evidence status uncertain.

## 8. Safety residuals

- Numeric roaster-temperature plausibility is **unresolved**: values are
  checked for finiteness only, with no range.
- Clock progress is a port contract, not a watchdog.
- The **D195 six-dimension envelope (heat, roast fan, main fan, drum, cooling,
  solenoid/drop) is checked only at eligible finalisation, never after a
  retained abort.**
- Per tick, only **heat, roast fan (D197) and cooling** are observed, and they
  are commanded state, not sensing.
- The independent operator emergency stop governs throughout.

## 9. Deployment residuals

- The live `PATH` is used to resolve tools.
- Same-user tampering is not prevented.
- Hugging Face model availability is outside this software.
- The MCP loader strips `PATH` as its own policy.
- Package and MCP versions are self-reported.
- Build provenance is operator-asserted.
- Inherited package loggers keep their normal behaviour.
- Operator text is judged by the identity admission only after the child has
  started, which surfaces as `identity_not_frozen`.

## 10. Gate chain

The gate chain is evidence-led and is **not executed**:

1. The cold run's acceptance criteria hold on its recorded evidence.
2. Both retained copies verify against the externally recorded receipt.
3. Independent Pi evidence review clears.
4. The full pre-roast gate is clean.
5. The operator gives a separate, immediate live GO.

Supervised live acceptance is at least 20 continuous minutes.
