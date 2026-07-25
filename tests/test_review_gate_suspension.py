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


def test_concurrency_group_preserves_dispatch_payload_type() -> None:
    """Invalid string payloads must not cancel the matching numeric PR stamp."""
    concurrency = cast(dict[str, object], _workflow_config()["concurrency"])
    assert concurrency == {
        "group": (
            "review-gate-status-${{ "
            "toJSON(github.event.pull_request.number || "
            "github.event.client_payload.pr_number || github.run_id) }}"
        ),
        "cancel-in-progress": True,
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
  const github = {{
    paginate: async () => (testCase.files ?? []).map((filename) => ({{ filename }})),
    rest: {{
      pulls: {{
        get: async () => ({{ data: pr }}),
        listFiles: async () => {{}},
      }},
      repos: {{
        createCommitStatus: async (status) => statuses.push(status),
      }},
    }},
  }};
  let failure = null;
  const core = {{
    info: () => {{}},
    setFailed: (message) => {{ failure = message; }},
  }};
  await execute(context, github, core);
  results.push({{
    name: testCase.name,
    status: statuses[0] ?? null,
    failure,
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
  const logs = [];
  const run = {{
    id: 91,
    workflow_id: 7,
    head_sha: "abc123",
    run_number: 4,
    created_at: "2026-07-25T17:00:00Z",
    conclusion: testCase.conclusion ?? "failure",
    pull_requests: [{{ number: 17 }}],
  }};
  const currentStatus = {{
    context: "review-gate",
    state: "success",
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
        listWorkflowRuns: async () => ({{ data: {{ workflow_runs: [run] }} }}),
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
  results.push({{ name: testCase.name, writes, logs }});
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
        "dispatch-off",
    ):
        status = results[name]
        assert status["state"] == "pending"
        assert status["description"] == "Awaiting Claude Code Review to post its findings."

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
            "name": "completed-review",
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
        results["completed-review"]["writes"],
    )
    assert len(completed_writes) == 1
    assert completed_writes[0]["state"] == "success"
    assert completed_writes[0]["description"] == (
        "Claude Code Review completed; findings gated by conversation resolution."
    )

    for name in ("forged-structural", "forged-suspension"):
        assert results[name]["writes"] == []
        logs = cast(list[str], results[name]["logs"])
        assert any("staying pending (fail-closed)" in log for log in logs)
