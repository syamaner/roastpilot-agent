"""Tests for the PR-scoped Claude approval workflow helper."""

from __future__ import annotations

import json
from collections.abc import Mapping
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError

import claude_review_approval as approval
import pytest


class FakeAPI:
    """Deterministic GitHub API double."""

    def __init__(self, responses: Mapping[str, approval.JsonValue]) -> None:
        """Initialize canned GET responses."""

        self.responses = dict(responses)
        self.posts: list[tuple[str, Mapping[str, approval.JsonValue] | None]] = []
        self.puts: list[tuple[str, Mapping[str, approval.JsonValue]]] = []

    def get(self, path: str) -> approval.JsonValue:
        """Return one canned GET response."""

        return self.responses[path]

    def post(
        self,
        path: str,
        payload: Mapping[str, approval.JsonValue] | None = None,
    ) -> approval.JsonValue:
        """Record one POST."""

        self.posts.append((path, payload))
        return {}

    def put(
        self,
        path: str,
        payload: Mapping[str, approval.JsonValue],
    ) -> approval.JsonValue:
        """Record one PUT."""

        self.puts.append((path, payload))
        return {}


def _pr_payload(
    number: int = 10,
    sha: str = "abc123",
    ref: str = "feature/test",
    author: str = "syamaner",
) -> approval.JsonObject:
    return {
        "number": number,
        "head": {"sha": sha, "ref": ref, "repo": {"id": 1}},
        "base": {"sha": "base123", "ref": "main", "repo": {"id": 2}},
        "user": {"login": author},
    }


def _identity_marker(
    *,
    number: int = 10,
    head_sha: str = "abc123",
    head_ref: str = "feature/test",
    base_ref: str = "main",
) -> str:
    return (
        f"[claude-review-identity-v1] pr={number} "
        f"head=1:{head_ref.replace('/', '%2F')}:{head_sha} "
        f"base=2:{base_ref.replace('/', '%2F')}"
    )


def _approval_body(text: str, *, exempt: bool = False) -> str:
    marker = "[claude-review-exempt]" if exempt else "[claude-review-approval]"
    return f"{marker} {text} {_identity_marker()}"


def _run_payload(
    *,
    run_id: int = 100,
    run_number: int = 20,
    attempt: int = 1,
    conclusion: str = "success",
    number: int = 10,
    sha: str = "abc123",
    ref: str = "feature/test",
) -> approval.JsonObject:
    return {
        "id": run_id,
        "run_number": run_number,
        "run_attempt": attempt,
        "workflow_id": 7,
        "path": ".github/workflows/claude-code-review.yml",
        "head_sha": sha,
        "head_branch": ref,
        "status": "completed",
        "conclusion": conclusion,
        "created_at": "2026-07-27T12:00:00Z",
        "pull_requests": [
            {
                "number": number,
                "head": {"sha": sha, "ref": ref, "repo": {"id": 1}},
                "base": {"sha": "base123", "ref": "main", "repo": {"id": 2}},
            }
        ],
    }


def _workflow_api(
    run: approval.JsonObject,
    *,
    pr: approval.JsonObject | None = None,
    runs: list[approval.JsonValue] | None = None,
    reviews: list[approval.JsonValue] | None = None,
) -> FakeAPI:
    pull_requests = run["pull_requests"]
    assert isinstance(pull_requests, list)
    summary = pull_requests[0]
    assert isinstance(summary, dict)
    number = summary["number"]
    sha = run["head_sha"]
    assert isinstance(number, int)
    assert isinstance(sha, str)
    return FakeAPI(
        {
            f"/pulls/{number}": pr or _pr_payload(number=number, sha=sha),
            f"/pulls/{number}/files?per_page=100": [],
            f"/actions/workflows/7/runs?head_sha={sha}&per_page=100": {
                "workflow_runs": runs or [run]
            },
            f"/pulls/{number}/reviews?per_page=100&page=1": reviews or [],
        }
    )


def test_successful_exact_head_review_approves_pr() -> None:
    """A successful newest run produces one exact-commit approval."""

    run = _run_payload()
    api = _workflow_api(run)

    result = approval.process_event({"workflow_run": run}, api)

    assert result == "approved PR #10 at abc123"
    assert api.posts == [
        (
            "/pulls/10/reviews",
            {
                "body": (
                    "[claude-review-approval] Claude Code Review run 100 attempt 1 "
                    f"completed successfully for `abc123`. {_identity_marker()} "
                    "reviewed-base-sha=base123 "
                    "Inline findings remain gated "
                    "by required conversation resolution."
                ),
                "commit_id": "abc123",
                "event": "APPROVE",
            },
        )
    ]


def test_shared_sha_runs_approve_only_their_associated_pr() -> None:
    """Two PRs sharing a SHA receive separate review objects."""

    first = _run_payload(number=10, ref="feature/first")
    second = _run_payload(run_id=101, run_number=21, number=11, ref="feature/second")
    first_api = _workflow_api(
        first,
        pr=_pr_payload(number=10, ref="feature/first"),
        runs=[first, second],
    )
    second_api = _workflow_api(
        second,
        pr=_pr_payload(number=11, ref="feature/second"),
        runs=[first, second],
    )

    approval.process_event({"workflow_run": first}, first_api)
    approval.process_event({"workflow_run": second}, second_api)

    assert first_api.posts[0][0] == "/pulls/10/reviews"
    assert second_api.posts[0][0] == "/pulls/11/reviews"


def test_stale_head_run_fails_closed() -> None:
    """A run whose PR moved to another head cannot approve it."""

    run = _run_payload()
    api = _workflow_api(run, pr=_pr_payload(sha="new-sha"))

    result = approval.process_event({"workflow_run": run}, api)

    assert result == "no exact open pull request is associated with the review run; fail closed"
    assert api.posts == []


@pytest.mark.parametrize(
    ("side", "field", "value"),
    [
        ("base", "ref", "release"),
        ("base", "repo", {"id": 3}),
        ("head", "repo", {"id": 3}),
    ],
)
def test_changed_pr_identity_fails_closed(
    side: str,
    field: str,
    value: approval.JsonValue,
) -> None:
    """A retarget or repository mismatch invalidates the run."""

    run = _run_payload()
    pr = _pr_payload()
    ref = pr[side]
    assert isinstance(ref, dict)
    ref[field] = value
    api = _workflow_api(run, pr=pr)

    result = approval.process_event({"workflow_run": run}, api)

    assert result == "no exact open pull request is associated with the review run; fail closed"
    assert api.posts == []


def test_base_tip_advance_preserves_unchanged_head_review() -> None:
    """A moving base tip cannot strand an otherwise exact PR/head review."""

    run = _run_payload()
    pr = _pr_payload()
    base = pr["base"]
    assert isinstance(base, dict)
    base["sha"] = "new-base"
    api = _workflow_api(run, pr=pr)

    result = approval.process_event({"workflow_run": run}, api)

    assert result == "approved PR #10 at abc123"
    assert _identity_marker() in str(api.posts[0][1])
    assert "reviewed-base-sha=base123" in str(api.posts[0][1])


def test_unexpected_workflow_path_fails_closed() -> None:
    """A same-name workflow at another path is never trusted."""

    run = _run_payload()
    run["path"] = ".github/workflows/not-claude.yml"

    result = approval.process_event({"workflow_run": run}, FakeAPI({}))

    assert result == "review run has an unexpected workflow path; fail closed"


def test_ambiguous_pr_association_fails_closed() -> None:
    """A run associated with two exact PR candidates is never guessed."""

    run = _run_payload()
    other = _run_payload(number=11)["pull_requests"]
    assert isinstance(other, list)
    current = run["pull_requests"]
    assert isinstance(current, list)
    run["pull_requests"] = [current[0], other[0]]
    api = FakeAPI(
        {
            "/pulls/10": _pr_payload(number=10),
            "/pulls/11": _pr_payload(number=11),
        }
    )

    result = approval.process_event({"workflow_run": run}, api)

    assert result == "no exact open pull request is associated with the review run; fail closed"


def test_delayed_older_run_defers_to_newest_pr_run() -> None:
    """A late completion cannot revoke valid identical-byte evidence."""

    old = _run_payload()
    new = _run_payload(run_id=101, run_number=21)
    new["conclusion"] = None
    api = _workflow_api(
        old,
        runs=[old, new],
        reviews=[
            {
                "id": 54,
                "user": {"login": "github-actions[bot]"},
                "state": "APPROVED",
                "commit_id": "abc123",
                "body": _approval_body("old run"),
            }
        ],
    )

    result = approval.process_event({"workflow_run": old}, api)

    assert result == "review run is not the newest run for this PR/head; deferred"
    assert api.posts == []
    assert api.puts == []


def test_older_attempt_of_same_run_defers() -> None:
    """A delayed attempt-one event defers once attempt two exists."""

    old = _run_payload(attempt=1)
    rerun = _run_payload(attempt=2)
    api = _workflow_api(old, runs=[rerun])

    result = approval.process_event({"workflow_run": old}, api)

    assert result == "review run is not the newest run for this PR/head; deferred"


def test_cancelled_first_attempt_reruns_once() -> None:
    """A first cancellation receives one bounded automatic retry."""

    run = _run_payload(conclusion="cancelled")
    api = _workflow_api(run)

    result = approval.process_event({"workflow_run": run}, api)

    assert result == "cancelled review run 100 re-run once; approval remains absent"
    assert api.posts == [("/actions/runs/100/rerun", None)]


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "action_required"])
def test_non_successful_review_never_approves(conclusion: str) -> None:
    """Non-success conclusions remain fail-closed."""

    run = _run_payload(conclusion=conclusion)
    api = _workflow_api(run)

    result = approval.process_event({"workflow_run": run}, api)

    assert result == f"review conclusion {conclusion!r} is not success; approval remains absent"
    assert api.posts == []


def test_cancelled_second_attempt_does_not_loop() -> None:
    """A cancelled retry is not rerun indefinitely."""

    run = _run_payload(attempt=2, conclusion="cancelled")
    api = _workflow_api(run)

    result = approval.process_event({"workflow_run": run}, api)

    assert result == "review conclusion 'cancelled' is not success; approval remains absent"
    assert api.posts == []


def test_new_attempt_preserves_same_head_approval_before_completion() -> None:
    """A same-head rerun cannot revoke an earlier successful approval."""

    run = _run_payload(attempt=2, conclusion="success")
    run["conclusion"] = None
    api = _workflow_api(
        run,
        reviews=[
            {
                "id": 55,
                "user": {"login": "github-actions[bot]"},
                "state": "APPROVED",
                "commit_id": "abc123",
                "body": _approval_body("attempt one"),
            }
        ],
    )

    result = approval.process_event(
        {"action": "in_progress", "workflow_run": run},
        api,
    )

    assert result == "newest review run is in_progress; exact-head approval preserved"
    assert api.puts == []
    assert api.posts == []


@pytest.mark.parametrize(
    ("action", "conclusion"),
    [("in_progress", None), ("completed", "failure"), ("completed", "success")],
)
def test_new_attempt_preserves_when_run_inventory_is_stale(
    action: str,
    conclusion: str | None,
) -> None:
    """A newer event outranks an eventually-consistent old success inventory."""

    incoming = _run_payload(attempt=2)
    incoming["conclusion"] = conclusion
    old = _run_payload(attempt=1)
    api = _workflow_api(
        incoming,
        runs=[old],
        reviews=[
            {
                "id": 58,
                "user": {"login": "github-actions[bot]"},
                "state": "APPROVED",
                "commit_id": "abc123",
                "body": _approval_body("attempt one"),
            }
        ],
    )

    result = approval.process_event({"action": action, "workflow_run": incoming}, api)

    assert api.puts == []
    assert api.posts == []
    assert result == "review run is not the newest run for this PR/head; deferred"


def test_delayed_start_after_success_does_not_revoke_current_approval() -> None:
    """An out-of-order start event cannot undo terminal success evidence."""

    incoming = _run_payload()
    incoming["conclusion"] = None
    completed = _run_payload(conclusion="success")
    api = _workflow_api(
        incoming,
        runs=[completed],
        reviews=[
            {
                "id": 59,
                "user": {"login": "github-actions[bot]"},
                "state": "APPROVED",
                "commit_id": "abc123",
                "body": _approval_body("completed"),
            }
        ],
    )

    result = approval.process_event(
        {"action": "in_progress", "workflow_run": incoming},
        api,
    )

    assert result == "stale start event arrived after successful completion; ignored"
    assert api.puts == []


def test_failed_rerun_preserves_approval_if_start_event_was_delayed() -> None:
    """A failure completion cannot revoke valid identical-byte evidence."""

    run = _run_payload(attempt=2, conclusion="failure")
    api = _workflow_api(
        run,
        reviews=[
            {
                "id": 56,
                "user": {"login": "github-actions[bot]"},
                "state": "APPROVED",
                "commit_id": "abc123",
                "body": _approval_body("attempt one"),
            }
        ],
    )

    result = approval.process_event(
        {"action": "completed", "workflow_run": run},
        api,
    )

    assert result == "review conclusion 'failure' is not success; exact-head approval preserved"
    assert api.puts == []


def test_cancelled_first_rerun_preserves_existing_approval() -> None:
    """A bounded retry is additive when identical bytes are already approved."""

    run = _run_payload(conclusion="cancelled")
    api = _workflow_api(
        run,
        reviews=[
            {
                "id": 60,
                "user": {"login": "github-actions[bot]"},
                "state": "APPROVED",
                "commit_id": "abc123",
                "body": _approval_body("earlier success"),
            }
        ],
    )

    result = approval.process_event({"workflow_run": run}, api)

    assert result == "cancelled review run 100 re-run once; exact-head approval preserved"
    assert api.posts == [("/actions/runs/100/rerun", None)]
    assert api.puts == []


def test_existing_exact_bot_approval_is_idempotent() -> None:
    """Repeated workflow events do not spam duplicate approval reviews."""

    run = _run_payload()
    api = _workflow_api(
        run,
        reviews=[
            {
                "user": {"login": "github-actions[bot]"},
                "state": "APPROVED",
                "commit_id": "abc123",
                "body": _approval_body("already done"),
            }
        ],
    )

    result = approval.process_event({"workflow_run": run}, api)

    assert result == "PR #10 already has a bot approval for abc123"
    assert api.posts == []


def test_old_base_identity_cannot_suppress_fresh_workflow_approval() -> None:
    """Same-commit evidence for an old base is not current-identity approval."""

    run = _run_payload()
    api = _workflow_api(
        run,
        reviews=[
            {
                "id": 61,
                "user": {"login": "github-actions[bot]"},
                "state": "APPROVED",
                "commit_id": "abc123",
                "body": (
                    f"[claude-review-approval] old base {_identity_marker(base_ref='release')}"
                ),
            }
        ],
    )

    result = approval.process_event({"workflow_run": run}, api)

    assert result == "approved PR #10 at abc123"
    assert len(api.posts) == 1
    assert _identity_marker() in str(api.posts[0][1])
    assert api.puts == []


def test_unrelated_approval_does_not_satisfy_idempotency() -> None:
    """Another identity or stale commit cannot suppress the exact approval."""

    run = _run_payload()
    api = _workflow_api(
        run,
        reviews=[
            {
                "user": {"login": "someone"},
                "state": "APPROVED",
                "commit_id": "abc123",
                "body": "[claude-review-approval] lookalike",
            },
            {
                "user": {"login": "github-actions[bot]"},
                "state": "APPROVED",
                "commit_id": "old",
                "body": "[claude-review-approval] stale",
            },
        ],
    )

    approval.process_event({"workflow_run": run}, api)

    assert api.posts[0][0] == "/pulls/10/reviews"


def test_review_lookup_paginates() -> None:
    """A current approval cannot hide after a full first review page."""

    run = _run_payload()
    api = _workflow_api(
        run,
        reviews=[{"user": {"login": "someone"}} for _ in range(100)],
    )
    api.responses["/pulls/10/reviews?per_page=100&page=2"] = [
        {
            "user": {"login": "github-actions[bot]"},
            "state": "APPROVED",
            "commit_id": "abc123",
            "body": _approval_body("current"),
        }
    ]

    result = approval.process_event({"workflow_run": run}, api)

    assert result == "PR #10 already has a bot approval for abc123"
    assert api.posts == []


def test_dependabot_receives_explicit_exemption_approval() -> None:
    """Dependabot is approved through the trusted exemption path."""

    pr = _pr_payload(author="dependabot[bot]")
    api = FakeAPI(
        {
            "/pulls/10/files?per_page=100": [],
            "/pulls/10/reviews?per_page=100&page=1": [],
        }
    )

    result = approval.process_event(
        {"action": "opened", "pull_request": pr},
        api,
    )

    assert result == "approved PR #10 at abc123"
    assert "[claude-review-exempt]" in str(api.posts[0][1])
    assert "Dependabot cannot receive repository secrets" in str(api.posts[0][1])


def test_dependabot_privileged_edit_requires_maintainer() -> None:
    """Dependency automation cannot alter the bridge and self-exempt."""

    api = FakeAPI(
        {"/pulls/10/files?per_page=100": [{"filename": "scripts/claude_review_approval.py"}]}
    )

    result = approval.process_event(
        {
            "action": "opened",
            "pull_request": _pr_payload(author="dependabot[bot]"),
        },
        api,
    )

    assert result == "privileged-code-editing PR requires an explicit maintainer approval"
    assert api.posts == []


@pytest.mark.parametrize(
    "file",
    [
        {"filename": ".github/workflows/claude-code-review.yml"},
        {
            "filename": "docs/retired.yml",
            "previous_filename": ".github/workflows/claude-code-review.yml",
        },
        {"filename": "scripts/claude_review_approval.py"},
        {
            "filename": "scripts/replacement.py",
            "previous_filename": "scripts/claude_review_approval.py",
        },
    ],
)
def test_successful_untrusted_privileged_code_edit_cannot_approve(
    file: approval.JsonObject,
) -> None:
    """A PR cannot replace or rename privileged bridge code to self-approve."""

    run = _run_payload()
    api = _workflow_api(run)
    api.responses["/pulls/10/files?per_page=100"] = [{"filename": "src/app.py"}, file]

    result = approval.process_event({"workflow_run": run}, api)

    assert result == "privileged-code-editing PR requires an explicit maintainer approval"
    assert api.posts == []


def test_full_file_page_fails_closed_as_possible_workflow_edit() -> None:
    """An unpaginated 100-file inventory cannot hide a workflow edit."""

    run = _run_payload()
    api = _workflow_api(run)
    api.responses["/pulls/10/files?per_page=100"] = [
        {"filename": f"src/file-{index}.py"} for index in range(100)
    ]

    result = approval.process_event({"workflow_run": run}, api)

    assert result == "privileged-code-editing PR requires an explicit maintainer approval"
    assert api.posts == []


def test_skipped_claude_run_preserves_dependabot_exemption() -> None:
    """A skipped review run cannot revoke the Dependabot exemption."""

    run = _run_payload(conclusion="skipped")
    api = _workflow_api(
        run,
        reviews=[
            {
                "id": 57,
                "user": {"login": "github-actions[bot]"},
                "state": "APPROVED",
                "commit_id": "abc123",
                "body": _approval_body("Dependabot", exempt=True),
            }
        ],
    )

    approval.process_event({"workflow_run": run}, api)

    assert api.puts == []


def test_normal_pr_waits_for_claude() -> None:
    """A normal PR is not approved by the exemption event."""

    api = FakeAPI({})

    result = approval.process_event(
        {"action": "opened", "pull_request": _pr_payload()},
        api,
    )

    assert result == "normal PR waits for its PR-scoped Claude review"
    assert api.posts == []


@pytest.mark.parametrize("action", ["ready_for_review", "closed"])
def test_lifecycle_toggle_does_not_reapprove_unchanged_head(action: str) -> None:
    """Exact-head approval persists across non-code lifecycle toggles."""

    api = FakeAPI({})

    result = approval.process_event(
        {"action": action, "pull_request": _pr_payload()},
        api,
    )

    assert result == f"pull_request_target action {action!r} does not require approval work"


def test_reopened_pr_preserves_existing_exact_head_approval() -> None:
    """Reopening reviewed bytes does not invalidate their exact-head approval."""

    api = FakeAPI(
        {
            "/pulls/10/reviews?per_page=100&page=1": [
                {
                    "id": 70,
                    "user": {"login": "github-actions[bot]"},
                    "state": "APPROVED",
                    "commit_id": "abc123",
                    "body": _approval_body("prior run"),
                }
            ]
        }
    )

    result = approval.process_event(
        {"action": "reopened", "pull_request": _pr_payload()},
        api,
    )

    assert result == "reopened PR #10 retains its exact-head approval"
    assert api.posts == []
    assert api.puts == []


def test_reopened_unapproved_pr_reruns_latest_exact_review() -> None:
    """A reopened PR without approval automatically restarts its exact review."""

    run = _run_payload(run_id=101, run_number=21)
    api = FakeAPI(
        {
            "/pulls/10/reviews?per_page=100&page=1": [],
            "/pulls/10/files?per_page=100": [],
            ("/actions/workflows/claude-code-review.yml/runs?head_sha=abc123&per_page=100"): {
                "workflow_runs": [run]
            },
        }
    )

    result = approval.process_event(
        {"action": "reopened", "pull_request": _pr_payload()},
        api,
    )

    assert result == "reopened PR #10 re-ran Claude review run 101"
    assert api.posts == [("/actions/runs/101/rerun", None)]


def test_reopened_pr_with_active_review_does_not_duplicate_it() -> None:
    """A queued or running exact review is recovery already in progress."""

    run = _run_payload(run_id=102, run_number=22)
    run["status"] = "in_progress"
    run["conclusion"] = None
    api = FakeAPI(
        {
            "/pulls/10/reviews?per_page=100&page=1": [],
            "/pulls/10/files?per_page=100": [],
            ("/actions/workflows/claude-code-review.yml/runs?head_sha=abc123&per_page=100"): {
                "workflow_runs": [run]
            },
        }
    )

    result = approval.process_event(
        {"action": "reopened", "pull_request": _pr_payload()},
        api,
    )

    assert result == "reopened PR #10 already has review run 102 in_progress"
    assert api.posts == []


def test_reopened_privileged_edit_never_reruns_untrusted_review() -> None:
    """Workflow or bridge edits still require the recorded maintainer path."""

    api = FakeAPI(
        {
            "/pulls/10/reviews?per_page=100&page=1": [],
            "/pulls/10/files?per_page=100": [{"filename": "scripts/claude_review_approval.py"}],
        }
    )

    result = approval.process_event(
        {"action": "reopened", "pull_request": _pr_payload()},
        api,
    )

    assert result == "privileged-code-editing PR requires an explicit maintainer approval"
    assert api.posts == []


def test_reopened_pr_without_prior_run_fails_closed() -> None:
    """Missing review history is visible failure, never silent approval."""

    api = FakeAPI(
        {
            "/pulls/10/reviews?per_page=100&page=1": [],
            "/pulls/10/files?per_page=100": [],
            ("/actions/workflows/claude-code-review.yml/runs?head_sha=abc123&per_page=100"): {
                "workflow_runs": []
            },
        }
    )

    with pytest.raises(ValueError, match="no prior Claude review run matched"):
        approval.process_event(
            {"action": "reopened", "pull_request": _pr_payload()},
            api,
        )


def test_base_retarget_dismisses_approval_for_fresh_review() -> None:
    """Changing the base invalidates same-head approval before re-review."""

    api = FakeAPI(
        {
            "/pulls/10/reviews?per_page=100&page=1": [
                {
                    "id": 71,
                    "user": {"login": "github-actions[bot]"},
                    "state": "APPROVED",
                    "commit_id": "abc123",
                    "body": "[claude-review-approval] old base",
                }
            ]
        }
    )

    result = approval.process_event(
        {
            "action": "edited",
            "changes": {"base": {"ref": {"from": "release"}}},
            "pull_request": _pr_payload(),
        },
        api,
    )

    assert result == (
        "base branch changed and dismissed the prior approval; fresh Claude review required"
    )
    assert api.puts == [
        (
            "/pulls/10/reviews/71/dismissals",
            {"message": "A newer Claude review attempt must succeed before merge."},
        )
    ]


def test_base_retarget_preserves_fresh_current_identity_approval() -> None:
    """A delayed retarget handler cannot dismiss a fresh current-base approval."""

    api = FakeAPI(
        {
            "/pulls/10/reviews?per_page=100&page=1": [
                {
                    "id": 74,
                    "user": {"login": "github-actions[bot]"},
                    "state": "APPROVED",
                    "commit_id": "abc123",
                    "body": _approval_body("fresh current base"),
                }
            ]
        }
    )

    result = approval.process_event(
        {
            "action": "edited",
            "changes": {"base": {"ref": {"from": "release"}}},
            "pull_request": _pr_payload(),
        },
        api,
    )

    assert result == "base branch changed; fresh current-base approval preserved"
    assert api.puts == []
    assert api.posts == []


def test_base_retarget_dismisses_only_stale_identity_when_fresh_exists() -> None:
    """Both retarget event orderings converge on current-base evidence."""

    api = FakeAPI(
        {
            "/pulls/10/reviews?per_page=100&page=1": [
                {
                    "id": 75,
                    "user": {"login": "github-actions[bot]"},
                    "state": "APPROVED",
                    "commit_id": "abc123",
                    "body": "[claude-review-approval] old base",
                },
                {
                    "id": 76,
                    "user": {"login": "github-actions[bot]"},
                    "state": "APPROVED",
                    "commit_id": "abc123",
                    "body": _approval_body("fresh current base"),
                },
            ]
        }
    )

    result = approval.process_event(
        {
            "action": "edited",
            "changes": {"base": {"ref": {"from": "release"}}},
            "pull_request": _pr_payload(),
        },
        api,
    )

    assert result == (
        "base branch changed and dismissed the prior approval; "
        "fresh current-base approval preserved"
    )
    assert api.puts == [
        (
            "/pulls/10/reviews/75/dismissals",
            {"message": "A newer Claude review attempt must succeed before merge."},
        )
    ]
    assert api.posts == []


def test_dependabot_base_retarget_replaces_exemption_after_recheck() -> None:
    """A safe retarget receives fresh exemption evidence without deadlock."""

    api = FakeAPI(
        {
            "/pulls/10/files?per_page=100": [],
            "/pulls/10/reviews?per_page=100&page=1": [
                {
                    "id": 72,
                    "user": {"login": "github-actions[bot]"},
                    "state": "APPROVED",
                    "commit_id": "abc123",
                    "body": "[claude-review-exempt] Dependabot",
                }
            ],
        }
    )

    result = approval.process_event(
        {
            "action": "edited",
            "changes": {"base": {"ref": {"from": "release"}}},
            "pull_request": _pr_payload(author="dependabot[bot]"),
        },
        api,
    )

    assert result == (
        "base branch changed and dismissed the prior approval; approved PR #10 at abc123"
    )
    assert api.puts == [
        (
            "/pulls/10/reviews/72/dismissals",
            {"message": "A newer Claude review attempt must succeed before merge."},
        )
    ]
    assert api.posts == [
        (
            "/pulls/10/reviews",
            {
                "body": (
                    "[claude-review-exempt] `abc123` is explicitly exempt: "
                    f"Dependabot cannot receive repository secrets. {_identity_marker()} "
                    "reviewed-base-sha=base123 "
                    "CI, codecov, "
                    "exact-head Codex, conversation resolution, and independent "
                    "triage remain required."
                ),
                "commit_id": "abc123",
                "event": "APPROVE",
            },
        )
    ]


def test_dependabot_base_retarget_preserves_fresh_current_exemption() -> None:
    """A delayed retarget handler cannot replace current exemption evidence."""

    api = FakeAPI(
        {
            "/pulls/10/files?per_page=100": [],
            "/pulls/10/reviews?per_page=100&page=1": [
                {
                    "id": 77,
                    "user": {"login": "github-actions[bot]"},
                    "state": "APPROVED",
                    "commit_id": "abc123",
                    "body": _approval_body("Dependabot", exempt=True),
                }
            ],
        }
    )

    result = approval.process_event(
        {
            "action": "edited",
            "changes": {"base": {"ref": {"from": "release"}}},
            "pull_request": _pr_payload(author="dependabot[bot]"),
        },
        api,
    )

    assert result == "base branch changed; PR #10 already has a bot approval for abc123"
    assert api.puts == []
    assert api.posts == []


def test_dependabot_base_retarget_dismisses_only_stale_exemption() -> None:
    """Fresh exemption is preserved when stale and current evidence coexist."""

    api = FakeAPI(
        {
            "/pulls/10/files?per_page=100": [],
            "/pulls/10/reviews?per_page=100&page=1": [
                {
                    "id": 78,
                    "user": {"login": "github-actions[bot]"},
                    "state": "APPROVED",
                    "commit_id": "abc123",
                    "body": "[claude-review-exempt] old base",
                },
                {
                    "id": 79,
                    "user": {"login": "github-actions[bot]"},
                    "state": "APPROVED",
                    "commit_id": "abc123",
                    "body": _approval_body("Dependabot", exempt=True),
                },
            ],
        }
    )

    result = approval.process_event(
        {
            "action": "edited",
            "changes": {"base": {"ref": {"from": "release"}}},
            "pull_request": _pr_payload(author="dependabot[bot]"),
        },
        api,
    )

    assert result == (
        "base branch changed and dismissed the prior approval; "
        "PR #10 already has a bot approval for abc123"
    )
    assert api.puts == [
        (
            "/pulls/10/reviews/78/dismissals",
            {"message": "A newer Claude review attempt must succeed before merge."},
        )
    ]
    assert api.posts == []


def test_dependabot_privileged_base_retarget_remains_unapproved() -> None:
    """Retargeting cannot exempt Dependabot after privileged code enters the diff."""

    api = FakeAPI(
        {
            "/pulls/10/files?per_page=100": [
                {"filename": ".github/workflows/claude-code-review.yml"}
            ],
            "/pulls/10/reviews?per_page=100&page=1": [
                {
                    "id": 73,
                    "user": {"login": "github-actions[bot]"},
                    "state": "APPROVED",
                    "commit_id": "abc123",
                    "body": "[claude-review-exempt] Dependabot",
                }
            ],
        }
    )

    result = approval.process_event(
        {
            "action": "edited",
            "changes": {"base": {"ref": {"from": "release"}}},
            "pull_request": _pr_payload(author="dependabot[bot]"),
        },
        api,
    )

    assert result == (
        "base branch changed and dismissed the prior approval; "
        "privileged-code-editing PR requires an explicit maintainer approval"
    )
    assert api.puts[0][0] == "/pulls/10/reviews/73/dismissals"
    assert api.posts == []


def test_non_base_edit_does_not_touch_approval() -> None:
    """Editing title or body does not churn exact-head review evidence."""

    api = FakeAPI({})

    result = approval.process_event(
        {
            "action": "edited",
            "changes": {"title": {"from": "old"}},
            "pull_request": _pr_payload(),
        },
        api,
    )

    assert result == "non-base pull request edit does not require approval work"
    assert api.posts == []
    assert api.puts == []


def test_workflows_serialize_bridge_and_review_only_base_edits() -> None:
    """Workflow triggers preserve race and retarget protections."""

    root = Path(__file__).resolve().parents[1]
    bridge = (root / ".github/workflows/claude-review-approval.yml").read_text(encoding="utf-8")
    reviewer = (root / ".github/workflows/claude-code-review.yml").read_text(encoding="utf-8")

    assert "types: [opened, synchronize, reopened, edited]" in bridge
    assert "claude-review-approval-${{" in bridge
    assert "github.event.workflow_run.head_sha ||" in bridge
    assert "github.event.pull_request.head.sha ||" in bridge
    assert "cancel-in-progress: false" in bridge
    assert "run: python3 -I scripts/claude_review_approval.py" in bridge
    assert "types: [opened, synchronize, edited]" in reviewer
    assert "github.event.changes.base.ref.from != ''" in reviewer


def test_missing_matching_runs_raises() -> None:
    """A malformed run inventory fails closed."""

    run = _run_payload()
    api = _workflow_api(run, runs=[_run_payload(number=11)])

    with pytest.raises(ValueError, match="no workflow run matched"):
        approval.process_event({"workflow_run": run}, api)


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"workflow_run": []},
        {"pull_request": {}},
        {
            "workflow_run": {
                **_run_payload(),
                "pull_requests": [{"number": True}],
            }
        },
        {"workflow_run": {**_run_payload(), "pull_requests": {}}},
    ],
)
def test_malformed_events_fail_closed(event: approval.JsonObject) -> None:
    """Malformed events cannot produce approvals."""

    with pytest.raises(ValueError):
        approval.process_event(event, FakeAPI({}))


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def test_rest_client_decodes_json_and_empty_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stdlib REST client handles JSON and no-content responses."""

    bodies = iter([b'{"ok": true}', b"", b'{"dismissed": true}'])

    def fake_urlopen(*_args: object, **_kwargs: object) -> _Response:
        return _Response(next(bodies))

    monkeypatch.setattr(approval, "urlopen", fake_urlopen)
    client = approval.RESTClient("owner/repo", "token")

    assert client.get("/value") == {"ok": True}
    assert client.post("/empty", {"value": 1}) is None
    assert client.put("/review", {"message": "stale"}) == {"dismissed": True}


def test_rest_client_redacts_http_error_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API failures expose only method, path, and status."""

    def fail(*_args: object, **_kwargs: object) -> None:
        raise HTTPError("url", 403, "forbidden", Message(), None)

    monkeypatch.setattr(approval, "urlopen", fail)

    with pytest.raises(RuntimeError, match=r"GET /value failed with HTTP 403"):
        approval.RESTClient("owner/repo", "secret").get("/value")


def test_main_requires_action_environment(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI fails closed when Actions context is absent."""

    for name in ("GITHUB_EVENT_PATH", "GITHUB_REPOSITORY", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    assert approval.main() == 2
    assert "are required" in capsys.readouterr().err


def test_main_processes_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI loads its event and reports the handler result."""

    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps({"action": "ready_for_review", "pull_request": _pr_payload()}),
        encoding="utf-8",
    )
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    assert approval.main() == 0
    assert "does not require approval work" in capsys.readouterr().out


def test_main_reports_fail_closed_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI converts validation failures into a nonzero exit."""

    event_path = tmp_path / "event.json"
    event_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    assert approval.main() == 1
    assert "failed closed" in capsys.readouterr().err
