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
- **The MCP package: three different things.** Keep them apart.
  1. *Published distribution metadata.* The `[pi]` extra and the development
     group pin `coffee-roaster-mcp==0.2.2`; the published 0.2.2 wheel digest is
     `25f4164027afc9d336e8726465ad3c702c39dc40a905c9f83a2c36f3473d4de3`.
     Published 0.2.2 lacks the temperature projection, so every cold run
     against it **fails closed at its first tick** (`NOT_CONFORMANT`).
  2. *Reviewed candidate bytes*, as recorded by the #997 review: reviewed source
     revision `0fc5e6d2e2a677c392b0706267e154f5780b96b0`; merged MCP main
     `96f8916407ec8d31bbed9637e824c3cd2b0cf24a` (tree
     `02bfa739a4cc3975295540c7df98ac844b5abbb7`); a retained candidate wheel
     of 169948 bytes with SHA-256
     `4f52048efa4c368e785c30acface04356dd943583a251e02a9b635905dec476a`. Its
     self-reported version is also 0.2.2, so only the digest, byte length and
     reviewed revision distinguish it from the published wheel.
  3. *Installed bytes*, which this software never attests.

  Installing the candidate is **a separately authorised gate** and is not
  authorised by this runbook. No authenticity, rebuild or installation claim is
  made, and no relation between the reviewed and merged trees is claimed beyond
  the recorded values.
- The terminal session survives a disconnect (for example `tmux` or `screen`).
  SIGHUP is not handled.
- **Complete appliance (D194, E11-S3).** A Raspberry Pi 5 runs the installed
  appliance stack, with the Hottop serial link connected and its telemetry
  read-only. Exactly one primary microphone delivers one mono 16 kHz / 16-bit
  stream; multi-microphone capture is deferred. Real ONNX-int8 first-crack
  inference runs on that stream. The advisory-only advisor runs with a frozen
  production provider, model and prompt, and returns typed `RoastDecision` data
  only.
- **Frozen identity, recorded before the run (D183).** Record the Pi 5 board
  and RAM; the 64-bit OS, kernel and firmware; the PSU and cooling; the storage;
  Python; the candidate MCP wheel SHA-256 and dependency inventory; the
  ONNX-int8 model and config hashes; the ALSA primary-device identity; and the
  Hottop serial identity. Any material identity change restarts
  characterisation.
- These are operator preconditions only. The software's existing admission and
  qualification checks still apply, but the software does not independently
  attest the physical board, the microphone format or the actual loaded model
  and its quantisation. Self-reported identities and software evidence are not
  physical proof.

### 2.1 Select artifacts before installation

Preparation starts by selecting two exact artifacts and recording their identities
outside version-only reasoning:

1. Record one Agent artifact's kind, source revision, clean-tree assertion where
   applicable, and byte length and SHA-256 where applicable.
2. Record the reviewed MCP candidate identified above: source revision, merged-tree
   record, candidate wheel byte length and SHA-256. Published MCP 0.2.2 lacks the
   D209 temperature projection; the reviewed, unreleased candidate reports the same
   0.2.2 version and includes the typed projection.
3. Treat the selected Agent artifact, reviewed MCP candidate, published MCP
   distribution and resulting installed bytes as separate facts. A version, wheel
   name, declared digest, `--artefact-*` value or `--mcp-candidate-*` value is an
   assertion or selected-artifact fact; none attests the bytes imported by the
   intended interpreter.
4. Keep installation and verification that the resulting installed distributions
   correspond to both selected artifacts as a **separately authorised installed-byte
   gate**. That gate remains unexecuted here.

An unknown, missing or mismatched artifact identity refuses preparation: the
physical gate remains unexecuted and the run must not be described as prepared.
This section supplies no installation transaction or installed-files attestor.

### 2.2 Prepare and check the cold configuration

Use a dedicated saved configuration. Replace every angle-bracketed placeholder
below with the frozen non-secret value selected for the supervised run. The serial
device, MCP entry point and both YAML paths must be absolute. The primary microphone
entry is deliberately repeated. `model_slug_by_phase`, `recording_enabled`,
`recording_autocapture`, `fc_confidence_threshold`, `auto_t0_detection_enabled`,
`auto_t0_drop_threshold_c`, `ambient_mode`, `ambient_device` and
`ambient_poll_interval_seconds` must remain absent. This leaves the base model as
the sole frozen advisor model and leaves the inference/device values not expressly
managed here under the operator's MCP YAML.

<!-- story-1002-cold-config-template -->
```yaml
controller:
  advisory_timeout_seconds: <frozen advisory call bound seconds>
  post_fc_min_consult_interval_seconds: <frozen post-completion dwell seconds>

advisor:
  provider: <frozen provider>
  provider_base_url: <frozen provider endpoint>
  model_slug: <frozen model slug>
  prompt_version: <supported frozen prompt version>
  temperature: <frozen advisor temperature>

mcp:
  command: <frozen absolute MCP entry point>
  call_timeout_seconds: <frozen MCP call timeout seconds>
  startup_timeout_seconds: <frozen MCP startup timeout seconds>
  stop_timeout_seconds: <frozen MCP stop timeout seconds>
  env: {}

mcp_device:
  serial_port: <frozen absolute device path>
  roaster_driver: hottop_kn8828b_2k_plus
  audio_input_device: <frozen primary microphone identity>
  recording_devices:
    - <frozen primary microphone identity>
  fc_mode: audio
  mcp_yaml_source_path: <absolute path to frozen MCP YAML>
```

Set `ROASTPILOT_CONFIG_FILE` to the saved file's absolute path. The saved-config
loader does not admit `advisor.api_key_env` from YAML. The only currently admitted
effective credential-variable name is `OPENROUTER_API_KEY`. Do not set
`ROASTPILOT_ADVISOR__API_KEY_ENV` to another name; export the credential only under
`OPENROUTER_API_KEY`. The preflight checks only that its value is nonempty and never
prints it. The effective configuration uses environment-over-file-over-default
precedence. This recipe compares the effective provider, endpoint,
credential-variable name, base model, empty phase model map, supported prompt,
advisor temperature and reasoning effort; the controller's cold advisory call bound
and post-completion dwell; the MCP command, three lifecycle timeouts and empty MCP
environment; every current safety limit; and the device fields shown or expressly
required to remain unset above. Inspect `ROASTPILOT_...__...` overrides for those
fields as part of preparation because an environment override wins over the value
shown in the saved YAML. Export no environment name beginning `COFFEE_`, in any
mixture of case. Do not set a phase-specific advisor model override: a nonempty
effective `model_slug_by_phase` refuses preparation. The selected MCP entry point
must remain the exact absolute path recorded here; an alternate absolute path, a
relative path or the bare default is refused.

The baseline leaves `advisor.reasoning_effort` absent so the effective setting is
the runtime default `None`, recorded exactly in the recipe. With this loader, YAML
`null` becomes the string `"null"` and is refused; it is not a way to select `None`.
If the operator deliberately selects a concrete schema-admitted reasoning effort,
add that value to the saved YAML (or its environment override) and record the same
value in `EXPECTED_REASONING_EFFORT`. The numeric values shown are source-verified
preparation examples which the operator records and freezes; they do not introduce
new accepted safety limits or hardware policy. The saved-config loader deliberately
ignores a saved `safety:` section, so do not add one to this template. A deliberate
safety change must be supplied through an operator-set `ROASTPILOT_SAFETY__...`
environment value and must exactly match the ten recorded expectations in the
recipe. `advisor.timeout_seconds` and
`advisor.healthcheck_timeout_seconds` are outside this preflight's workload claim:
cold mode passes the two controller timing values directly to its sampler and does
not run the normal provider health check.

Run the following with the interpreter from the intended Agent environment after
replacing its expected placeholders with the same frozen non-secret values. It
reads the credential only for truthiness and emits exactly one closed line. A
refusal exits nonzero without printing configuration, environment, provider or
result objects, credential values, or exception details.

This local preparation recipe assumes an operator-controlled configuration file
that is kept stable for the duration of the check. It does not lock or atomically
snapshot the selected pathname, and it does not protect against concurrent
replacement. The result is a configuration preparation observation, not a
persisted-state or installed-byte attestation.

<!-- story-1002-cold-config-preflight -->
```python
import math
import os
from pathlib import Path

from roastpilot_agent.advisor import instructions_for
from roastpilot_agent.cold_characterisation.advisory_sampler import (
    MIN_POST_COMPLETION_DWELL_SECONDS,
)
from roastpilot_agent.cold_characterisation.identity import (
    _ALLOWED_CREDENTIAL_ENV_NAMES,
)
from roastpilot_agent.cold_composition import (
    ColdCompositionRefusal,
    _admit_device_config,
    _admit_environment,
)
from roastpilot_agent.config import SafetyLimits
from roastpilot_agent.config_store import load_app_config

EXPECTED_PROVIDER = "<frozen provider>"
EXPECTED_PROVIDER_ENDPOINT = "<frozen provider endpoint>"
EXPECTED_CREDENTIAL_NAME = "OPENROUTER_API_KEY"
EXPECTED_MODEL = "<frozen model slug>"
EXPECTED_PROMPT = "<supported frozen prompt version>"
EXPECTED_TEMPERATURE = <frozen advisor temperature>
EXPECTED_REASONING_EFFORT = None
EXPECTED_CALL_BOUND_SECONDS = <frozen advisory call bound seconds>
EXPECTED_DWELL_SECONDS = <frozen post-completion dwell seconds>
EXPECTED_MCP_COMMAND = Path("<frozen absolute MCP entry point>")
EXPECTED_MCP_CALL_TIMEOUT_SECONDS = <frozen MCP call timeout seconds>
EXPECTED_MCP_STARTUP_TIMEOUT_SECONDS = <frozen MCP startup timeout seconds>
EXPECTED_MCP_STOP_TIMEOUT_SECONDS = <frozen MCP stop timeout seconds>
EXPECTED_SERIAL = "<frozen absolute device path>"
EXPECTED_DRIVER = "hottop_kn8828b_2k_plus"
EXPECTED_AUDIO = "<frozen primary microphone identity>"
EXPECTED_MCP_YAML = Path("<absolute path to frozen MCP YAML>")
EXPECTED_SAFETY = SafetyLimits(
    max_bean_temp_c=230.0,
    max_env_temp_c=240.0,
    pre_t0_max_bean_temp_c=200.0,
    overrun_safe_fan_percent=100,
    pre_t0_overrun_severity="recovery",
    min_seconds_between_commands=2.0,
    max_consecutive_mcp_failures=3,
    max_consecutive_advisor_failures=3,
    bitter_ceiling_temp_c=196.0,
    emergency_drop_temp_c=198.0,
)


def main() -> int:
    try:
        selected = os.environ.get("ROASTPILOT_CONFIG_FILE")
        if selected is None:
            raise ValueError
        selected_path = Path(selected)
        if not selected_path.is_absolute() or not selected_path.is_file():
            raise ValueError
        with selected_path.open("rb") as selected_file:
            selected_file.read(1)

        config, _ = load_app_config()
        device = config.mcp_device
        if (
            config.advisor.provider != EXPECTED_PROVIDER
            or config.advisor.provider_base_url != EXPECTED_PROVIDER_ENDPOINT
            or config.advisor.api_key_env != EXPECTED_CREDENTIAL_NAME
            or config.advisor.api_key_env not in _ALLOWED_CREDENTIAL_ENV_NAMES
            or config.advisor.model_slug != EXPECTED_MODEL
            or config.advisor.model_slug_by_phase != {}
            or config.advisor.prompt_version != EXPECTED_PROMPT
            or config.advisor.temperature != EXPECTED_TEMPERATURE
            or config.advisor.reasoning_effort != EXPECTED_REASONING_EFFORT
            or config.controller.advisory_timeout_seconds != EXPECTED_CALL_BOUND_SECONDS
            or not math.isfinite(config.controller.advisory_timeout_seconds)
            or config.controller.advisory_timeout_seconds <= 0.0
            or config.controller.post_fc_min_consult_interval_seconds
            != EXPECTED_DWELL_SECONDS
            or config.controller.post_fc_min_consult_interval_seconds
            < MIN_POST_COMPLETION_DWELL_SECONDS
            or config.mcp.command != str(EXPECTED_MCP_COMMAND)
            or not EXPECTED_MCP_COMMAND.is_absolute()
            or config.mcp.call_timeout_seconds != EXPECTED_MCP_CALL_TIMEOUT_SECONDS
            or config.mcp.startup_timeout_seconds != EXPECTED_MCP_STARTUP_TIMEOUT_SECONDS
            or config.mcp.stop_timeout_seconds != EXPECTED_MCP_STOP_TIMEOUT_SECONDS
            or config.mcp.env != {}
            or config.safety != EXPECTED_SAFETY
            or device.serial_port != EXPECTED_SERIAL
            or device.serial_port is None
            or not Path(device.serial_port).is_absolute()
            or device.roaster_driver != EXPECTED_DRIVER
            or device.audio_input_device != EXPECTED_AUDIO
            or device.recording_devices != (EXPECTED_AUDIO,)
            or device.fc_mode != "audio"
            or device.fc_confidence_threshold is not None
            or device.auto_t0_detection_enabled is not None
            or device.auto_t0_drop_threshold_c is not None
            or device.recording_enabled is not None
            or device.recording_autocapture is not None
            or device.mcp_yaml_source_path != EXPECTED_MCP_YAML
            or not EXPECTED_MCP_YAML.is_absolute()
            or device.ambient_mode is not None
            or device.ambient_device is not None
            or device.ambient_poll_interval_seconds is not None
        ):
            raise ValueError

        instructions_for(config.advisor.prompt_version)

        environment = _admit_environment(config, os.name)
        if (
            isinstance(environment, ColdCompositionRefusal)
            or environment.credential_present is not True
        ):
            raise ValueError
        phases = _admit_device_config(device)
        if isinstance(phases, ColdCompositionRefusal):
            raise ValueError
    except Exception:
        print("cold configuration preflight: REFUSED")
        return 1
    print("cold configuration preflight: ADMITTED")
    return 0


raise SystemExit(main())
```

This is a hardware-free and provider-free configuration preparation check. It
does not build or call the advisor, spawn MCP, open the MCP YAML, or access a
device. It does not verify the files behind configured paths, installed Agent or
MCP distributions, the loaded ONNX model, provider reachability, physical
identity, hardware state, calibration, readiness, accuracy or physical safety.
The selected MCP command path is compared as configuration text only: the recipe
does not resolve, open or execute it, or attest its existence, installed bytes,
code or provenance.
`ADMITTED` means only that the locally effective configuration matches the
operator-recorded preparation values and the locally callable closed admissions.
It does not attest provider or model existence, dependencies, response latency,
cancellation delivery, spend, executable or YAML existence/content, installed
Agent or MCP bytes, the ONNX model actually loaded, runtime identity, complete
phase execution, evidence sealing, qualification, calibration, readiness,
accuracy, physical devices, roaster state or physical safety.

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
  --mcp-candidate-version <installed self-reported MCP version> \
  --mcp-candidate-wheel-sha256 <64 lowercase hex> \
  --mcp-candidate-wheel-bytes <positive byte length> \
  --mcp-candidate-reviewed-revision <40 lowercase hex> \
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
- The four `--mcp-candidate-*` options are required. Copy them from the
  reviewed-candidate record of the artefact that the separately authorised gate
  actually installed. `--mcp-candidate-version` must equal the installed
  self-reported MCP version; otherwise the CLI refuses with
  `input_not_admitted`, exit 2. These values are an **operator assertion** and
  the software does not attest the bytes that are installed. Each phase's
  evidence records the asserted candidate before its first tick.

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
- A tick is published only after both its tick record and its paired
  tick-temperature record are retained, so publication follows one extra
  durable write. If the temperature record cannot be written, that tick may be
  retained on disk but is never displayed, and the run fails unsealed.
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
summary. An interrupt arriving while the usage line or help text is being
written follows the interrupt rule instead: exit 130 with one
`cancelled_before_run` summary, and the usage or help text may be partial.
Every other ordinary path that reaches the reporter attempts one closed
summary of exactly 16 `key=value` lines (delivery may be incomplete, as below):
`mode`, `run_invoked`, `result`, `cli_refusal`, `composition_refusal`,
`outcome`, `start_refusal`, `termination_reason`, `child_ownership`,
`advisory_path`, `provider_check`, `conformance_outcome`, `manifest_sha256`,
`signal`, `http_server` and `exit_code`. Values are closed tokens; no host,
port, path, profile, note, credential or exception text is printed.

`conformance_outcome` reports temperature conformance policy 3
(`temperature_screened_conformant` or `not_conformant`), which composes the
advisory policy. Exit 0 (`ADVISORY_CONFORMANT`) requires
`temperature_screened_conformant`. `composition_refusal` may be
`mcp_candidate_not_admitted` (exit 4) when the run consumer re-admits the
candidate and refuses it.

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

- **D209 temperature screen.** From the 60-second startup boundary, bean and
  environment temperatures must each lie within 5 to 40 °C inclusive, and
  every observation must show accepted-packet progress since the previous one,
  a valid last packet in Celsius, raw/typed agreement and no newly counted
  ignored-temperature packet, serial-read error or command-loop error. Any
  screen reason aborts the phase with a retained abort and no finalisation.
  The range is engineering screening, not calibration, and not a
  physical-safety statement.
- Packet progress means at least one accepted packet arrived between two
  observations. It is not a watchdog and not a sample-age bound.
- Raw/typed agreement is consistency, not independent sensor corroboration, and
  an `observed` projection does not imply liveness.
- A non-Celsius reported unit fails the screen.
- Bad-checksum frames that the device driver skips without counting them are
  invisible to every counter: a disclosed residual.
- The tick and its tick-temperature record are two durable writes, not a
  transaction; a process kill between them leaves an unsealed tree with no
  receipt.
- The acceptance interpretation of the inner run does not include the
  temperature screen; the screen is judged by temperature conformance policy 3.
- Clock progress is a port contract, not a watchdog.
- Per-tick software observation checks commanded heat, main fan, roast fan and
  cooling. Drum and solenoid/drop appear only in eligible D195 six-dimension
  finalisation evidence, never after a retained abort. These are commanded
  software values, not physical sensing or proof of physical response.
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
