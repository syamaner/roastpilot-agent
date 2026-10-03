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

One cold run has two fixed phases: a 30-minute recording-off phase followed by
a 30-minute recording-on phase, under the approved music stimulus (recorded in
`--stimulus-block`). This runbook prescribes no stimulus content, no other
duration and no run authority of its own.

## 2. Prerequisites

- The roaster is empty and cold, and the run is supervised in person.
- **The independent operator emergency stop is required throughout the run.**
  The hosted app is read-only and cannot actuate an emergency stop.
- The appliance service is stopped, so neither the serial port nor port 8000 is
  held by another process. A successful bind does not prove that the service
  was stopped; confirm it independently. A different HTTP port does not
  prevent contention for the shared serial port or the provider quota, and the
  software adds no automatic exclusion. The managed-service commands (stop, logs) are
  in the [Pi appliance guide](pi-appliance.md) (Upgrade and maintenance; Logs).
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
  writes it. Before choosing the roots, make the primary and secondary roots
  **distinct and non-overlapping**: neither root is the same as the other or
  nested inside it, neither is the same directory reached through another path
  (an aliased root via a symlink, bind mount or other alias), and the copied run
  directory and its files are never the same directory as, or hard-linked to,
  the primary's. The software records the secondary path only; it never opens,
  writes or compares it, and it does not check for overlap when the run starts.
- `--protected-root` is optional and repeatable; nothing is added implicitly.
- `--artefact-sha256` is required exactly when `--artefact-kind` is `wheel` or
  `sdist`, and refused for `editable_source`.
- `--host` defaults to `127.0.0.1` and `--port` to `8000`. `--spa-dir` is
  optional; without it the bundled SPA build is used, and a missing build
  refuses the run. Point `--spa-dir` only at a trusted built SPA directory:
  every file under it is served to unauthenticated clients. An explicitly empty
  `--host` is a usage error; any non-loopback bind must name its address
  explicitly and is covered by the LAN residual in §4. `--port` must be
  1-65535: `0` (which would pick an unannounced ephemeral port) and out-of-range
  values are usage errors, as is an explicitly empty `--spa-dir`; a relative
  `--spa-dir` resolves against the current working directory.
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
- With the default loopback bind, the cold view is at
  `http://127.0.0.1:8000/cold-characterisation`, opened on the host itself or
  through an operator-managed SSH local port forward to the host's loopback
  port. If `--port` or the tunnel's local port differs, only the port changes;
  the path stays `/cold-characterisation`. The cold view has no navigation link:
  `/` opens the normal home view, not the cold view.
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
- The HTTP view runs on the same event loop as the run. Nothing this software
  prints shows that the view stayed available for the whole run.

## 5. Exit codes and summary

A usage error prints only the fixed usage line on stderr (exit 2), never a
summary. Every other ordinary path that reaches the reporter attempts one closed
summary of exactly 16 `key=value` lines (delivery may be incomplete, as below):
`mode`, `run_invoked`, `result`, `cli_refusal`, `composition_refusal`,
`outcome`, `start_refusal`, `termination_reason`, `child_ownership`,
`advisory_path`, `provider_check`, `conformance_outcome`, `manifest_sha256`,
`signal`, `http_server` and `exit_code`. Values are closed tokens; no host,
port, path, profile, note, credential or exception text is printed.

`http_server` is one synchronous snapshot, taken at the single report attempt,
of this invocation's HTTP server task and its startup barrier:

| Token | Meaning | What it does not mean |
|---|---|---|
| `task_not_created` | This invocation created no HTTP server task (including an `already_run` refusal, for that call only) | Says nothing about another process or an earlier call |
| `start_not_confirmed` | A server task was created, but the startup barrier was not observed resolved to true (pending, false, cancelled or failed) | Not proof that the server never bound, listened or answered |
| `task_pending_at_report` | The start was confirmed and the server task had not finished at the report attempt | Not HTTP health, client connections, rendering, delivery or uptime |
| `task_ended_at_report` | The start was confirmed and the server task had already finished at the report attempt (normally or after a contained failure) | Not when or why it ended |
| `unknown` | No snapshot was taken (the fallback after an interrupt escaped) | Hosting status is not inferred from anything else |

- `http_server` is not uptime, health, client delivery or rendering, and it is
  not evidence. It is never written to the store, the evidence or any
  qualification input.
- It never changes the outcome, result or exit code, and it is absent when exit
  codes 80-83 apply (nothing is printed then). An HTTP server that ends does
  not stop, cancel or reclassify the run.
- Neither the engine outcome nor exit 0 is proof of view availability or of
  qualification.
- It is a single snapshot, not monitoring and not a watchdog. A failure that
  has not yet finished the server task still shows as pending; a failure that
  does not end the server task (for example one failing request) is never
  visible; a failure after the snapshot (during output or teardown) is not
  reported.

| Code | Meaning | What it does not mean |
|---|---|---|
| 0 | `ADVISORY_CONFORMANT` | Not qualification, readiness or acceptance |
| 2 | Usage error, or input the models refuse (`run_invoked=false`) | |
| 3 | CLI refusal: config, host facts, host reader, SPA, store, bind, HTTP start, or already run (`run_invoked=false`) | |
| 4 | Composition refusal; this invocation constructed no child | Not proof that no other child exists |
| 5 | `REFUSED_BEFORE_EVIDENCE` | |
| 6 | `NOT_CONFORMANT` | |
| 7 | `EVIDENCE_NOT_SEALED` | |
| 8 | Unadmitted result, propagated exception, or any other non-cancellation failure out of the run (after the engine's own cleanup); this invocation's child status is unknown | Not proof that the child is absent |
| 130 / 143 | Cancelled by SIGINT / SIGTERM; before the run `run_invoked=false`, during it the child is unknown. Exit 130 with `signal=none` means the process was interrupted or cancelled without a recorded first signal | Not proof that the child is absent |
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
- A missing or incomplete closed summary is not evidence of any outcome. In
  particular, an exit status of 0 without a complete closed summary is not
  conformance.
- If the process exit status differs from the summary's `exit_code` line, the
  process was interrupted after the summary was written, and teardown may be
  incomplete and is uncertain. A printed summary is never a delivery receipt.

## 6. Evidence

On an ordinary exit with a `manifest_sha256` value, record that value
**externally** (for example in the supervision notes) as the receipt. Never
derive the receipt from the evidence tree itself. If no `manifest_sha256` value
was printed, there is no receipt for that run.

The software writes only the primary root. The operator copies the run tree to
the secondary root. The run directory under the primary root is named by the
run ID. The verifier below refuses overlapping, aliased or shared-file copies
with `ROOTS_OVERLAP`; that check is a same-host software check and does not
prove physical storage separation. There is no CLI verifier; verification uses the existing Python
function, run with the interpreter of the installed `roastpilot-agent`
environment (the pipx venv when it was deployed by pipx), never a bare
`python`. Resolve that interpreter's actual absolute path on the host first;
the path below is a placeholder, not a universal location. Pass
`protected_roots` exactly matching the run's repeated `--protected-root` values,
in order (an empty tuple only if there were none):

```bash
RP_COLD_PYTHON='/absolute/path/to/installed/roastpilot-agent/venv/bin/python'
"$RP_COLD_PYTHON" -c 'from roastpilot_agent.cold_characterisation.evidence_store import verify_retained_copies; print(verify_retained_copies("/absolute/primary/evidence/root", "/absolute/secondary/evidence/root", run_id="<run ID>", expected_manifest_sha256="<recorded receipt>", protected_roots=("/absolute/protected/root",)))'
```

Independent Pi evidence review follows separately.

## 7. Signals

- A first Ctrl-C or SIGTERM handled during the pre-engine awaits prevents the
  run from being invoked; the process cleans up and exits 130 or 143.
- A signal that arrives while the synchronous start line is being written may
  only be handled after the run has been invoked. The existing cleanup then
  applies, and the summary truthfully reports `run_invoked=true` with the child
  `unknown`.
- A SIGTERM that arrives before the cold runner has installed its handlers keeps
  the default disposition: the process ends with no summary. An early Ctrl-C at
  that point usually exits 130 with a closed summary recording `signal=none`,
  but if Python's own interrupt handling cancels the task while the runner is
  starting and that cancellation is absorbed, it can exit 1 with `signal=none`.
  Exit 130 is not guaranteed for every early timing.
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
