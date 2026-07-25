"""Execute the review-gate suspension stamp from its workflow source."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import cast

import yaml

_REPO_ROOT = Path(__file__).parent.parent
_WORKFLOW = _REPO_ROOT / ".github/workflows/review-gate.yml"


def _workflow_config() -> dict[str, object]:
    return cast(dict[str, object], yaml.safe_load(_WORKFLOW.read_text()))


def _workflow_script(job_name: str) -> str:
    loaded = _workflow_config()
    jobs = cast(dict[str, object], loaded["jobs"])
    job = cast(dict[str, object], jobs[job_name])
    steps = cast(list[dict[str, object]], job["steps"])
    with_config = cast(dict[str, object], steps[0]["with"])
    script = with_config["script"]
    assert isinstance(script, str)
    return script


def _stamp_script() -> str:
    return _workflow_script("stamp-on-pr")


def _flip_script() -> str:
    return _workflow_script("flip-on-review")


def test_concurrency_serializes_every_pr_status_transition_without_cancellation() -> None:
    """PR, dispatch, and review completion must share one non-cancelling lane."""
    concurrency = cast(dict[str, object], _workflow_config()["concurrency"])
    assert concurrency == {
        "group": (
            "review-gate-status-${{ "
            "toJSON(github.event.pull_request.number || "
            "github.event.client_payload.pr_number || "
            "github.event.workflow_run.pull_requests[0].number || github.run_id) }}"
        ),
        "cancel-in-progress": False,
        "queue": "max",
    }


def _execute_cases(cases: list[dict[str, object]]) -> list[dict[str, object]]:
    node = shutil.which("node")
    assert node is not None
    wrapper = f"""
const AsyncFunction = Object.getPrototypeOf(async function () {{}}).constructor;
const execute = new AsyncFunction("context", "github", "core", {json.dumps(_stamp_script())});
const cases = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
(async () => {{
const results = [];
for (const testCase of cases) {{
  if (Object.hasOwn(testCase, "env")) {{
    process.env.CLAUDE_REVIEW_SUSPENDED = testCase.env;
  }} else {{
    delete process.env.CLAUDE_REVIEW_SUSPENDED;
  }}
  const statuses = [];
  const reruns = [];
  const logs = [];
  const pr = {{
    number: 17,
    state: testCase.prState ?? "open",
    head: {{ sha: "abc123" }},
    user: {{ login: testCase.login ?? "engineer" }},
  }};
  const eventName = testCase.eventName ?? "pull_request";
  const context = {{
    eventName,
    actor: testCase.actor ?? "engineer",
    payload: eventName === "repository_dispatch"
      ? {{ client_payload: {{ pr_number: testCase.prNumber ?? 17 }} }}
      : {{ pull_request: pr }},
    repo: {{ owner: "owner", repo: "repo" }},
  }};
  const listFiles = async () => {{}};
  const listWorkflowRuns = async () => {{}};
  const defaultRun = {{
    id: 91,
    head_sha: "abc123",
    event: "pull_request",
    status: "completed",
    run_number: 4,
    run_attempt: 1,
    created_at: "2026-07-25T17:00:00Z",
    pull_requests: Object.hasOwn(testCase, "pullRequests")
      ? testCase.pullRequests
      : [{{ number: 17 }}],
  }};
  const github = {{
    paginate: async (endpoint) => {{
      if (endpoint === listFiles) {{
        if (testCase.listFilesError) throw new Error(testCase.listFilesError);
        return (testCase.files ?? []).map((filename) => ({{ filename }}));
      }}
      if (endpoint === listWorkflowRuns) {{
        if (testCase.listRunsError) throw new Error(testCase.listRunsError);
        return testCase.runs ?? [defaultRun];
      }}
      throw new Error("unexpected paginate endpoint");
    }},
    rest: {{
      actions: {{
        listWorkflowRuns,
        reRunWorkflow: async (request) => {{
          if (testCase.rerunError) throw new Error(testCase.rerunError);
          reruns.push(request);
        }},
      }},
      pulls: {{
        get: async () => ({{ data: pr }}),
        listFiles,
      }},
      repos: {{
        createCommitStatus: async (status) => statuses.push(status),
      }},
    }},
  }};
  let failure = null;
  const core = {{
    info: (message) => logs.push(message),
    setFailed: (message) => {{ failure = message; }},
  }};
  let thrown = null;
  try {{
    await execute(context, github, core);
  }} catch (error) {{
    thrown = String(error);
  }}
  results.push({{
    name: testCase.name,
    status: statuses.at(-1) ?? null,
    statuses,
    reruns,
    logs,
    failure,
    thrown,
  }});
}}
process.stdout.write(JSON.stringify(results));
}})().catch((error) => {{
  console.error(error);
  process.exitCode = 1;
}});
"""
    completed = subprocess.run(  # noqa: S603
        [node, "-e", wrapper],
        input=json.dumps(cases),
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return cast(list[dict[str, object]], json.loads(completed.stdout))


def _execute_flip_cases(
    cases: list[dict[str, object]],
) -> list[dict[str, object]]:
    node = shutil.which("node")
    assert node is not None
    wrapper = f"""
const AsyncFunction = Object.getPrototypeOf(async function () {{}}).constructor;
const execute = new AsyncFunction("context", "github", "core", {json.dumps(_flip_script())});
const cases = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
(async () => {{
const results = [];
for (const testCase of cases) {{
  if (Object.hasOwn(testCase, "env")) {{
    process.env.CLAUDE_REVIEW_SUSPENDED = testCase.env;
  }} else {{
    delete process.env.CLAUDE_REVIEW_SUSPENDED;
  }}
  const writes = [];
  const reruns = [];
  const logs = [];
  const run = {{
    id: testCase.runId ?? 91,
    workflow_id: 7,
    head_sha: "abc123",
    run_number: 4,
    run_attempt: Object.hasOwn(testCase, "runAttempt")
      ? testCase.runAttempt
      : 1,
    created_at: "2026-07-25T17:00:00Z",
    conclusion: testCase.conclusion ?? "failure",
    pull_requests: Object.hasOwn(testCase, "pullRequests")
      ? testCase.pullRequests
      : [{{ number: 17 }}],
  }};
  const currentStatus = {{
    context: "review-gate",
    state: testCase.statusState ?? "success",
    description: testCase.description,
    updated_at: testCase.updatedAt ?? "2026-07-25T17:00:01Z",
    creator: {{ login: testCase.creator ?? "github-actions[bot]" }},
  }};
  const context = {{
    payload: {{ workflow_run: run }},
    repo: {{ owner: "owner", repo: "repo" }},
  }};
  const github = {{
    paginate: async () => testCase.statuses ?? [currentStatus],
    rest: {{
      actions: {{
        listWorkflowRuns: async () => ({{
          data: {{ workflow_runs: testCase.runsForSha ?? [run] }},
        }}),
        reRunWorkflow: async (request) => {{
          if (testCase.rerunError) throw new Error(testCase.rerunError);
          reruns.push(request);
        }},
      }},
      pulls: {{
        get: async () => ({{ data: {{ head: {{ sha: "abc123" }} }} }}),
      }},
      repos: {{
        createCommitStatus: async (status) => writes.push(status),
        listCommitStatusesForRef: async () => {{}},
      }},
    }},
  }};
  const core = {{
    info: (message) => logs.push(message),
    notice: (message) => logs.push(message),
    warning: (message) => logs.push(message),
  }};
  await execute(context, github, core);
  results.push({{ name: testCase.name, writes, reruns, logs }});
}}
process.stdout.write(JSON.stringify(results));
}})().catch((error) => {{
  console.error(error);
  process.exitCode = 1;
}});
"""
    completed = subprocess.run(  # noqa: S603
        [node, "-e", wrapper],
        input=json.dumps(cases),
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return cast(list[dict[str, object]], json.loads(completed.stdout))


def test_review_gate_suspension_is_exact_reversible_and_precedence_safe() -> None:
    """Only exact suspension truth bypasses, and a later false event re-stamps."""
    cases: list[dict[str, object]] = [
        {"name": "suspended", "env": "true"},
        {"name": "absent"},
        {"name": "empty", "env": ""},
        {"name": "false", "env": "false"},
        {"name": "upper", "env": "TRUE"},
        {"name": "trailing", "env": "true "},
        {
            "name": "dispatch-off",
            "env": "false",
            "eventName": "repository_dispatch",
        },
        {
            "name": "dispatch-active",
            "env": "false",
            "eventName": "repository_dispatch",
            "runs": [
                {
                    "id": 92,
                    "head_sha": "abc123",
                    "event": "pull_request",
                    "status": "in_progress",
                    "run_number": 5,
                    "run_attempt": 1,
                    "created_at": "2026-07-25T17:01:00Z",
                    "pull_requests": [{"number": 17}],
                }
            ],
        },
        {
            "name": "dispatch-no-run",
            "env": "false",
            "eventName": "repository_dispatch",
            "runs": [],
        },
        {
            "name": "dispatch-wrong-pr",
            "env": "false",
            "eventName": "repository_dispatch",
            "runs": [
                {
                    "id": 93,
                    "head_sha": "abc123",
                    "event": "pull_request",
                    "status": "completed",
                    "run_number": 6,
                    "created_at": "2026-07-25T17:02:00Z",
                    "pull_requests": [{"number": 18}],
                }
            ],
        },
        {
            "name": "dispatch-wrong-first-pr",
            "env": "false",
            "eventName": "repository_dispatch",
            "runs": [
                {
                    "id": 96,
                    "head_sha": "abc123",
                    "event": "pull_request",
                    "status": "completed",
                    "run_number": 8,
                    "run_attempt": 1,
                    "created_at": "2026-07-25T17:04:00Z",
                    "pull_requests": [{"number": 18}, {"number": 17}],
                }
            ],
        },
        {
            "name": "dispatch-wrong-sha",
            "env": "false",
            "eventName": "repository_dispatch",
            "runs": [
                {
                    "id": 94,
                    "head_sha": "def456",
                    "event": "pull_request",
                    "status": "completed",
                    "run_number": 7,
                    "created_at": "2026-07-25T17:03:00Z",
                    "pull_requests": [{"number": 17}],
                }
            ],
        },
        {
            "name": "dispatch-rerun-fails",
            "env": "false",
            "eventName": "repository_dispatch",
            "rerunError": "API unavailable",
        },
        {
            "name": "dispatch-files-fail",
            "env": "false",
            "eventName": "repository_dispatch",
            "listFilesError": "files API unavailable",
        },
        {
            "name": "dispatch-runs-fail",
            "env": "false",
            "eventName": "repository_dispatch",
            "listRunsError": "runs API unavailable",
        },
        {
            "name": "dispatch-structural",
            "env": "false",
            "eventName": "repository_dispatch",
            "files": [".github/workflows/changed.yml"],
        },
        {
            "name": "dispatch-invalid",
            "env": "false",
            "eventName": "repository_dispatch",
            "prNumber": 0,
        },
        {
            "name": "dispatch-closed",
            "env": "false",
            "eventName": "repository_dispatch",
            "prState": "closed",
        },
        {
            "name": "dependabot",
            "env": "true",
            "login": "dependabot[bot]",
        },
        {
            "name": "workflow",
            "env": "true",
            "files": [".github/workflows/changed.yml"],
        },
    ]
    raw_results = {cast(str, result["name"]): result for result in _execute_cases(cases)}
    results = {
        name: cast(
            dict[str, object],
            result["status"],
        )
        for name, result in raw_results.items()
        if result["status"] is not None
    }

    suspended = results["suspended"]
    assert suspended["state"] == "success"
    suspended_description = cast(str, suspended["description"])
    assert suspended_description.startswith("[suspended] ")
    assert "operator-suspended" in suspended_description
    assert "Codex and independent triage remain required" in suspended_description
    assert len(suspended_description) <= 140

    for name in (
        "absent",
        "empty",
        "false",
        "upper",
        "trailing",
        "dispatch-no-run",
        "dispatch-wrong-pr",
        "dispatch-wrong-first-pr",
        "dispatch-wrong-sha",
    ):
        status = results[name]
        assert status["state"] == "pending"
        expected = (
            "[revocation] Reconciliation in progress; Claude review required after suspension."
            if name.startswith("dispatch-")
            else "Awaiting Claude Code Review to post its findings."
        )
        assert status["description"] == expected

    dependabot_description = cast(str, results["dependabot"]["description"])
    workflow_description = cast(str, results["workflow"]["description"])
    assert dependabot_description.startswith("[exempt] Dependabot PR")
    assert workflow_description.startswith("[exempt] PR edits .github/workflows/")

    assert raw_results["dispatch-invalid"]["status"] is None
    assert (
        raw_results["dispatch-invalid"]["failure"]
        == "client_payload.pr_number must be a positive JSON integer"
    )
    assert raw_results["dispatch-closed"]["status"] is None
    assert raw_results["dispatch-closed"]["failure"] == "PR #17 is not open"

    dispatch_reruns = cast(list[dict[str, object]], raw_results["dispatch-off"]["reruns"])
    assert dispatch_reruns == [{"owner": "owner", "repo": "repo", "run_id": 91}]
    assert raw_results["dispatch-off"]["failure"] is None
    dispatch_status = cast(dict[str, object], raw_results["dispatch-off"]["status"])
    assert dispatch_status["state"] == "pending"
    assert dispatch_status["description"] == (
        "[revocation] Claude review run 91 attempt 2 required after suspension."
    )
    assert raw_results["dispatch-active"]["reruns"] == []
    assert raw_results["dispatch-active"]["failure"] is None
    active_status = cast(dict[str, object], raw_results["dispatch-active"]["status"])
    assert active_status["state"] == "pending"
    assert active_status["description"] == (
        "[revocation] Claude review run 92 attempt 1 required after suspension."
    )
    active_logs = cast(list[str], raw_results["dispatch-active"]["logs"])
    assert any("is in_progress; no duplicate re-run requested" in log for log in active_logs)

    for name in (
        "dispatch-no-run",
        "dispatch-wrong-pr",
        "dispatch-wrong-first-pr",
        "dispatch-wrong-sha",
    ):
        assert raw_results[name]["reruns"] == []
        failure = cast(str, raw_results[name]["failure"])
        assert "no valid Claude Code Review run/attempt bound to PR #17 at abc123" in failure
        assert "send the suspension reconciliation dispatch again" in failure

    assert raw_results["dispatch-rerun-fails"]["reruns"] == []
    rerun_failure = cast(str, raw_results["dispatch-rerun-fails"]["failure"])
    assert "Could not re-run Claude Code Review 91" in rerun_failure
    assert "API unavailable" in rerun_failure
    assert "send the suspension reconciliation dispatch again" in rerun_failure
    rerun_failure_status = cast(dict[str, object], raw_results["dispatch-rerun-fails"]["status"])
    assert rerun_failure_status["description"] == (
        "[revocation] Claude review run 91 attempt 2 required after suspension."
    )

    for name, error in (
        ("dispatch-files-fail", "files API unavailable"),
        ("dispatch-runs-fail", "runs API unavailable"),
    ):
        writes = cast(list[dict[str, object]], raw_results[name]["statuses"])
        assert writes[0]["state"] == "pending"
        assert writes[0]["description"] == (
            "[revocation] Reconciliation in progress; Claude review required after suspension."
        )
        assert error in cast(str, raw_results[name]["thrown"])

    assert raw_results["dispatch-structural"]["reruns"] == []
    assert raw_results["dispatch-structural"]["failure"] is None
    structural_status = cast(dict[str, object], raw_results["dispatch-structural"]["status"])
    assert structural_status["state"] == "success"
    assert cast(str, structural_status["description"]).startswith("[exempt] ")

    # Cases run sequentially against the same SHA: the false-like stamp follows
    # suspended success and proves the next event writes PENDING, not sticky success.
    assert results["suspended"]["sha"] == results["absent"]["sha"] == "abc123"


def test_review_gate_flip_preserves_only_current_authenticated_exemptions() -> None:
    """A stale suspension is revoked while structural exemptions remain."""
    cases: list[dict[str, object]] = [
        {
            "name": "active-suspension",
            "env": "true",
            "description": "[suspended] incident",
        },
        {
            "name": "ended-suspension",
            "env": "false",
            "description": "[suspended] incident",
        },
        {
            "name": "absent-suspension",
            "description": "[suspended] incident",
        },
        {
            "name": "structural",
            "env": "false",
            "description": "[exempt] workflow edit",
        },
        {
            "name": "completed-review-during-revocation",
            "env": "false",
            "description": "[suspended] incident",
            "conclusion": "success",
        },
        {
            "name": "forged-structural",
            "env": "true",
            "description": "[exempt] forged",
            "creator": "attacker",
        },
        {
            "name": "forged-suspension",
            "env": "true",
            "description": "[suspended] forged",
            "creator": "attacker",
        },
        {
            "name": "newest-suspension-first",
            "env": "false",
            "description": "unused",
            "statuses": [
                {
                    "context": "review-gate",
                    "state": "success",
                    "description": "[suspended] newest",
                    "updated_at": "2026-07-25T17:00:02Z",
                    "creator": {"login": "github-actions[bot]"},
                },
                {
                    "context": "review-gate",
                    "state": "success",
                    "description": "[exempt] older",
                    "updated_at": "2026-07-25T17:00:01Z",
                    "creator": {"login": "github-actions[bot]"},
                },
            ],
        },
        {
            "name": "newest-suspension-last",
            "env": "false",
            "description": "unused",
            "statuses": [
                {
                    "context": "review-gate",
                    "state": "success",
                    "description": "[exempt] older",
                    "updated_at": "2026-07-25T17:00:01Z",
                    "creator": {"login": "github-actions[bot]"},
                },
                {
                    "context": "review-gate",
                    "state": "success",
                    "description": "[suspended] newest",
                    "updated_at": "2026-07-25T17:00:02Z",
                    "creator": {"login": "github-actions[bot]"},
                },
            ],
        },
    ]
    results = {cast(str, result["name"]): result for result in _execute_flip_cases(cases)}

    assert results["active-suspension"]["writes"] == []
    assert results["structural"]["writes"] == []
    for name in (
        "ended-suspension",
        "absent-suspension",
        "newest-suspension-first",
        "newest-suspension-last",
    ):
        writes = cast(list[dict[str, object]], results[name]["writes"])
        assert len(writes) == 1
        assert writes[0]["state"] == "pending"
        assert writes[0]["description"] == (
            "Suspension ended; awaiting Claude Code Review to post its findings."
        )

    completed_writes = cast(
        list[dict[str, object]],
        results["completed-review-during-revocation"]["writes"],
    )
    assert len(completed_writes) == 1
    assert completed_writes[0]["state"] == "pending"
    assert completed_writes[0]["description"] == (
        "Suspension ended; awaiting Claude Code Review to post its findings."
    )

    for name in ("forged-structural", "forged-suspension"):
        assert results[name]["writes"] == []
        logs = cast(list[str], results[name]["logs"])
        assert any("staying pending (fail-closed)" in log for log in logs)


def test_review_gate_flip_rejects_stale_rerun_attempt_events() -> None:
    """A delayed prior attempt cannot own the gate after the run is rerun."""
    cases: list[dict[str, object]] = [
        {
            "name": "stale-attempt-one",
            "conclusion": "success",
            "runAttempt": 1,
            "statusState": "pending",
            "description": (
                "[revocation] Claude review run 91 attempt 2 required after suspension."
            ),
        },
        {
            "name": "stale-attempt-one-after-registration",
            "conclusion": "success",
            "runAttempt": 1,
            "statusState": "pending",
            "description": (
                "[revocation] Claude review run 91 attempt 2 required after suspension."
            ),
            "runsForSha": [
                {
                    "id": 91,
                    "workflow_id": 7,
                    "head_sha": "abc123",
                    "run_number": 4,
                    "run_attempt": 2,
                    "created_at": "2026-07-25T17:00:00Z",
                }
            ],
        },
        {
            "name": "current-attempt-two",
            "conclusion": "success",
            "runAttempt": 2,
            "statusState": "pending",
            "description": (
                "[revocation] Claude review run 91 attempt 2 required after suspension."
            ),
        },
        {
            "name": "missing-attempt",
            "conclusion": "success",
            "runAttempt": None,
        },
        {
            "name": "missing-direct-pr-binding",
            "conclusion": "success",
            "pullRequests": [],
        },
        {
            "name": "new-run-cannot-use-old-marker",
            "conclusion": "success",
            "runId": 92,
            "runAttempt": 1,
            "statusState": "pending",
            "description": (
                "[revocation] Claude review run 91 attempt 2 required after suspension."
            ),
        },
        {
            "name": "cancelled-marked-attempt-advances",
            "conclusion": "cancelled",
            "runAttempt": 1,
            "statusState": "pending",
            "description": (
                "[revocation] Claude review run 91 attempt 1 required after suspension."
            ),
        },
    ]
    results = {cast(str, result["name"]): result for result in _execute_flip_cases(cases)}

    assert results["stale-attempt-one"]["writes"] == []
    stale_logs = cast(list[str], results["stale-attempt-one"]["logs"])
    assert any(
        "does not match the authenticated revocation requirement" in log for log in stale_logs
    )

    assert results["stale-attempt-one-after-registration"]["writes"] == []
    stale_registered_logs = cast(list[str], results["stale-attempt-one-after-registration"]["logs"])
    assert any(
        "attempt 1" in log and "newest is run 91 attempt 2" in log for log in stale_registered_logs
    )

    current_writes = cast(list[dict[str, object]], results["current-attempt-two"]["writes"])
    assert len(current_writes) == 1
    assert current_writes[0]["state"] == "success"

    assert results["missing-attempt"]["writes"] == []
    assert results["new-run-cannot-use-old-marker"]["writes"] == []
    assert results["missing-direct-pr-binding"]["writes"] == []
    unbound_logs = cast(list[str], results["missing-direct-pr-binding"]["logs"])
    assert any("cannot share the PR concurrency lane" in log for log in unbound_logs)

    cancelled = results["cancelled-marked-attempt-advances"]
    cancelled_writes = cast(list[dict[str, object]], cancelled["writes"])
    assert len(cancelled_writes) == 1
    assert cancelled_writes[0]["state"] == "pending"
    assert cancelled_writes[0]["description"] == (
        "[revocation] Claude review run 91 attempt 2 required after suspension."
    )
    assert cancelled["reruns"] == [{"owner": "owner", "repo": "repo", "run_id": 91}]
