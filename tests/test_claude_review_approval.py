"""Tests for the PR-scoped Claude approval workflow helper."""

from __future__ import annotations

import json
from collections.abc import Mapping
from email.message import Message
from pathlib import Path
from typing import cast
from urllib.error import HTTPError, URLError

import claude_review_approval as approval
import pytest
import yaml


class FakeAPI:
    """Deterministic GitHub API double."""

    def __init__(
        self,
        responses: Mapping[str, approval.JsonValue],
        *,
        response_sequences: Mapping[str, list[approval.JsonValue]] | None = None,
    ) -> None:
        """Initialize canned GET responses."""

        self.responses = dict(responses)
        self.response_sequences = {
            path: list(values) for path, values in (response_sequences or {}).items()
        }
        self.posts: list[tuple[str, Mapping[str, approval.JsonValue] | None]] = []
        self.puts: list[tuple[str, Mapping[str, approval.JsonValue]]] = []
        self.deletes: list[str] = []
        self.review_bodies: dict[int, str] = {}

    def get(self, path: str) -> approval.JsonValue:
        """Return one canned GET response."""

        if path in self.response_sequences:
            return self.response_sequences[path].pop(0)
        if path not in self.responses and path.startswith("/pulls/") and path.count("/") == 2:
            return _pr_payload(number=int(path.rsplit("/", 1)[1]))
        if path == "/pulls/10/reviews/900":
            return {
                "state": "APPROVED",
                "commit_id": "abc123",
                "body": self.review_bodies[900],
            }
        return self.responses[path]

    def post(
        self,
        path: str,
        payload: Mapping[str, approval.JsonValue] | None = None,
    ) -> approval.JsonValue:
        """Record one POST."""

        self.posts.append((path, payload))
        if path.endswith("/reviews"):
            assert payload is not None
            body = payload.get("body")
            assert isinstance(body, str)
            self.review_bodies[900] = body
            return {"id": 900}
        return {}

    def delete(self, path: str) -> approval.JsonValue:
        """Record one DELETE."""

        self.deletes.append(path)
        if path.startswith("/pulls/10/reviews/"):
            review_id = int(path.rsplit("/", 1)[1])
            reviews_path = "/pulls/10/reviews?per_page=100&page=1"
            reviews = self.responses.get(reviews_path)
            if isinstance(reviews, list):
                self.responses[reviews_path] = [
                    value
                    for value in reviews
                    if not (isinstance(value, dict) and value.get("id") == review_id)
                ]
        return None

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
    base_sha: str = "base123",
    base_ref: str = "main",
) -> approval.JsonObject:
    return {
        "number": number,
        "head": {"sha": sha, "ref": ref, "repo": {"id": 1}},
        "base": {"sha": base_sha, "ref": base_ref, "repo": {"id": 2}},
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
        f"[claude-review-identity-v1 pr={number} "
        f"head=1:{head_ref.replace('/', '%2F')}:{head_sha} "
        f"base=2:{base_ref.replace('/', '%2F')}]"
    )


def _approval_body(
    text: str,
    *,
    exempt: bool = False,
    run_order: tuple[int, int, int] | None = None,
) -> str:
    marker = "[claude-review-exempt]" if exempt else "[claude-review-approval]"
    order = (
        ""
        if run_order is None
        else (
            f" [claude-review-run-v1 workflow={run_order[0]} "
            f"number={run_order[1]} attempt={run_order[2]}]"
        )
    )
    return f"{marker} {text}{order} {_identity_marker()}"


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


def _operator_event(
    *,
    number: str = "10",
    reason: str = "Workflow change independently reviewed and approved.",
    actor: str = "syamaner",
) -> approval.JsonObject:
    return {
        "action": "privileged_review_override",
        "client_payload": {"pull_request": number, "reason": reason},
        "sender": {"login": actor},
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
                    "[claude-review-run-v1 workflow=7 number=20 attempt=1] "
                    f"completed successfully for `abc123`. {_identity_marker()} "
                    "reviewed-base-sha=base123 "
                    "Inline findings remain gated "
                    "by required conversation resolution."
                ),
                "commit_id": "abc123",
            },
        ),
        ("/pulls/10/reviews/900/events", {"event": "APPROVE"}),
    ]


def test_null_body_ordinary_review_does_not_block_approval() -> None:
    """GitHub reviews without summary text are ignored by bridge scans."""

    run = _run_payload()
    api = _workflow_api(
        run,
        reviews=[
            {
                "id": 50,
                "user": {"login": "human-reviewer"},
                "state": "APPROVED",
                "commit_id": "abc123",
                "body": None,
            },
            {
                "id": 51,
                "user": {"login": "github-actions[bot]"},
                "state": "COMMENTED",
                "commit_id": "abc123",
                "body": None,
            },
        ],
    )

    result = approval.process_event({"workflow_run": run}, api)

    assert result == "approved PR #10 at abc123"
    assert api.posts[-1] == ("/pulls/10/reviews/900/events", {"event": "APPROVE"})


def test_non_string_review_body_fails_closed() -> None:
    """Malformed non-null review bodies remain strict input errors."""

    run = _run_payload()
    api = _workflow_api(
        run,
        reviews=[
            {
                "id": 52,
                "user": {"login": "human-reviewer"},
                "state": "COMMENTED",
                "commit_id": "abc123",
                "body": {"unexpected": "object"},
            }
        ],
    )

    with pytest.raises(ValueError, match="review.body must be a string"):
        approval.process_event({"workflow_run": run}, api)


def test_missing_review_body_fails_closed() -> None:
    """A missing body member is malformed, unlike an explicit JSON null."""

    run = _run_payload()
    api = _workflow_api(
        run,
        reviews=[
            {
                "id": 53,
                "user": {"login": "human-reviewer"},
                "state": "COMMENTED",
                "commit_id": "abc123",
            }
        ],
    )

    with pytest.raises(ValueError, match="review.body is required"):
        approval.process_event({"workflow_run": run}, api)


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


def test_head_change_before_approval_prevents_post() -> None:
    """A push before approval mutation cannot receive stale evidence."""

    run = _run_payload()
    original = _pr_payload()
    changed = _pr_payload(sha="new-sha")
    api = _workflow_api(run, pr=original)
    api.response_sequences["/pulls/10"] = [original, changed]

    result = approval.process_event({"workflow_run": run}, api)

    assert result == "PR #10 identity changed before approval; approval remains absent"
    assert api.posts == []
    assert api.puts == []


def test_head_change_after_approval_dismisses_created_review() -> None:
    """A push racing the approval POST causes immediate review dismissal."""

    run = _run_payload()
    original = _pr_payload()
    changed = _pr_payload(sha="new-sha")
    api = _workflow_api(run, pr=original)
    api.response_sequences["/pulls/10"] = [
        original,
        original,
        original,
        original,
        changed,
    ]

    result = approval.process_event({"workflow_run": run}, api)

    assert result == "PR #10 identity changed after approval; new review dismissed"
    assert api.posts[0][0] == "/pulls/10/reviews"
    assert api.puts == [
        (
            "/pulls/10/reviews/900/dismissals",
            {"message": "The pull request identity changed while approval was published."},
        )
    ]


def test_post_approval_malformed_pr_response_dismisses_created_review() -> None:
    """Malformed post-publication identity data cannot leave approval active."""

    run = _run_payload()
    original = _pr_payload()
    api = _workflow_api(run, pr=original)
    api.response_sequences["/pulls/10"] = [original, original, original, original, {}]

    with pytest.raises(ValueError, match="pull_request.user must be an object"):
        approval.process_event({"workflow_run": run}, api)

    assert api.posts[0][0] == "/pulls/10/reviews"
    assert api.puts == [
        (
            "/pulls/10/reviews/900/dismissals",
            {"message": "Post-publication pull request validation failed."},
        )
    ]


def test_post_approval_api_failure_dismisses_created_review() -> None:
    """A failed post-publication reload dismisses the exact created review."""

    class FailingReloadAPI(FakeAPI):
        pull_reads = 0

        def get(self, path: str) -> approval.JsonValue:
            if path == "/pulls/10":
                self.pull_reads += 1
                if self.pull_reads == 5:
                    raise RuntimeError("temporary API failure")
            return super().get(path)

    run = _run_payload()
    base = _workflow_api(run)
    api = FailingReloadAPI(base.responses)

    with pytest.raises(RuntimeError, match="temporary API failure"):
        approval.process_event({"workflow_run": run}, api)

    assert api.puts == [
        (
            "/pulls/10/reviews/900/dismissals",
            {"message": "Post-publication pull request validation failed."},
        )
    ]


def test_post_approval_cleanup_failure_is_activation_blocker() -> None:
    """A validation plus dismissal failure remains loud for operator audit."""

    class FailingCleanupAPI(FakeAPI):
        pull_reads = 0

        def get(self, path: str) -> approval.JsonValue:
            if path == "/pulls/10":
                self.pull_reads += 1
                if self.pull_reads == 5:
                    raise RuntimeError("temporary API failure")
            return super().get(path)

        def put(
            self,
            path: str,
            payload: Mapping[str, approval.JsonValue],
        ) -> approval.JsonValue:
            raise RuntimeError("dismissal failed")

    run = _run_payload()
    base = _workflow_api(run)
    api = FailingCleanupAPI(base.responses)

    with pytest.raises(RuntimeError, match="operator audit required before activation"):
        approval.process_event({"workflow_run": run}, api)


def test_identity_change_cleanup_failure_is_activation_blocker() -> None:
    """A raced identity plus dismissal failure requires operator audit."""

    class FailingCleanupAPI(FakeAPI):
        def put(
            self,
            path: str,
            payload: Mapping[str, approval.JsonValue],
        ) -> approval.JsonValue:
            raise RuntimeError("dismissal failed")

    run = _run_payload()
    original = _pr_payload()
    changed = _pr_payload(sha="new-sha")
    base = _workflow_api(run, pr=original)
    api = FailingCleanupAPI(base.responses)
    api.response_sequences["/pulls/10"] = [
        original,
        original,
        original,
        original,
        changed,
    ]

    with pytest.raises(RuntimeError, match="operator audit required before activation"):
        approval.process_event({"workflow_run": run}, api)


def test_lost_pending_review_create_response_cannot_publish_approval() -> None:
    """An indeterminate initial create leaves only non-counting pending evidence."""

    class LostCreateResponseAPI(FakeAPI):
        lost_body: str | None = None

        def get(self, path: str) -> approval.JsonValue:
            if path == "/pulls/10/reviews?per_page=100&page=1" and self.lost_body is not None:
                return [
                    {
                        "id": 900,
                        "user": {"login": "github-actions[bot]"},
                        "state": "PENDING",
                        "commit_id": "abc123",
                        "body": self.lost_body,
                    }
                ]
            return super().get(path)

        def post(
            self,
            path: str,
            payload: Mapping[str, approval.JsonValue] | None = None,
        ) -> approval.JsonValue:
            if path.endswith("/reviews"):
                self.posts.append((path, payload))
                assert payload is not None
                raw_body = payload.get("body")
                assert isinstance(raw_body, str)
                self.lost_body = raw_body
                raise RuntimeError("response lost")
            return super().post(path, payload)

    run = _run_payload()
    base = _workflow_api(run)
    api = LostCreateResponseAPI(base.responses)

    result = approval.process_event({"workflow_run": run}, api)

    assert result == "approved PR #10 at abc123"
    assert api.posts[0][1] is not None
    assert "event" not in api.posts[0][1]
    assert api.puts == []
    assert api.deletes == []


def test_identity_change_before_submit_deletes_pending_review() -> None:
    """A pre-submit identity race cannot strand the bot's pending review."""

    run = _run_payload()
    original = _pr_payload()
    changed = _pr_payload(sha="new-sha")
    api = _workflow_api(run, pr=original)
    api.response_sequences["/pulls/10"] = [original, original, original, changed]

    result = approval.process_event({"workflow_run": run}, api)

    assert result == ("PR #10 identity changed before approval submission; approval remains absent")
    assert api.deletes == ["/pulls/10/reviews/900"]
    assert all(not path.endswith("/events") for path, _payload in api.posts)


def test_pending_review_delete_failure_is_activation_blocker() -> None:
    """Failure to remove a known pending review requires operator audit."""

    class FailingDeleteAPI(FakeAPI):
        def delete(self, path: str) -> approval.JsonValue:
            raise RuntimeError("delete failed")

    run = _run_payload()
    original = _pr_payload()
    changed = _pr_payload(sha="new-sha")
    base = _workflow_api(run, pr=original)
    api = FailingDeleteAPI(base.responses)
    api.response_sequences["/pulls/10"] = [original, original, original, changed]

    with pytest.raises(RuntimeError, match="operator audit required before activation"):
        approval.process_event({"workflow_run": run}, api)


def test_exact_pending_review_is_resumed_before_retry() -> None:
    """A duplicate exact-run handler resumes the existing pending review."""

    run = _run_payload()
    body = (
        "[claude-review-approval] Claude Code Review run 100 attempt 1 "
        "[claude-review-run-v1 workflow=7 number=20 attempt=1] "
        f"completed successfully for `abc123`. {_identity_marker()} "
        "reviewed-base-sha=base123 "
        "Inline findings remain gated by required conversation resolution."
    )
    api = _workflow_api(
        run,
        reviews=[
            {
                "id": 899,
                "user": {"login": "github-actions[bot]"},
                "state": "PENDING",
                "commit_id": "abc123",
                "body": body,
            }
        ],
    )

    result = approval.process_event({"workflow_run": run}, api)

    assert result == "approved PR #10 at abc123"
    assert api.deletes == []
    assert api.posts == [("/pulls/10/reviews/899/events", {"event": "APPROVE"})]


def test_different_run_pending_review_is_not_deleted() -> None:
    """Concurrent different-run work remains owned by its original handler."""

    run = _run_payload()
    api = _workflow_api(
        run,
        reviews=[
            {
                "id": 898,
                "user": {"login": "github-actions[bot]"},
                "state": "PENDING",
                "commit_id": "abc123",
                "body": _approval_body("newer handler", run_order=(7, 21, 1)),
            }
        ],
    )

    result = approval.process_event({"workflow_run": run}, api)

    assert result == "another bridge handler owns pending review evidence for PR #10; deferred"
    assert api.deletes == []
    assert api.posts == []


def test_old_head_pending_is_deleted_before_new_head_create() -> None:
    """A push cannot leave prior-head pending evidence blocking the new review."""

    run = _run_payload(sha="new-sha")
    api = _workflow_api(
        run,
        pr=_pr_payload(sha="new-sha"),
        reviews=[
            {
                "id": 897,
                "user": {"login": "github-actions[bot]"},
                "state": "PENDING",
                "commit_id": "abc123",
                "body": (
                    f"[claude-review-approval] old head {_identity_marker(head_sha='abc123')}"
                ),
            }
        ],
    )

    result = approval.process_event({"workflow_run": run}, api)

    assert result == "approved PR #10 at new-sha"
    assert api.deletes == ["/pulls/10/reviews/897"]
    assert api.posts[-1] == ("/pulls/10/reviews/900/events", {"event": "APPROVE"})


def test_old_base_pending_is_deleted_before_current_base_create() -> None:
    """A current handler removes stale-base pending evidence before creating."""

    run = _run_payload()
    api = _workflow_api(
        run,
        reviews=[
            {
                "id": 896,
                "user": {"login": "github-actions[bot]"},
                "state": "PENDING",
                "commit_id": "abc123",
                "body": (
                    f"[claude-review-approval] old base {_identity_marker(base_ref='release')}"
                ),
            }
        ],
    )

    result = approval.process_event({"workflow_run": run}, api)

    assert result == "approved PR #10 at abc123"
    assert api.deletes == ["/pulls/10/reviews/896"]
    assert api.posts[-1] == ("/pulls/10/reviews/900/events", {"event": "APPROVE"})


def test_stale_handler_preserves_new_live_identity_pending() -> None:
    """An old handler classifies its review snapshot against reloaded live identity."""

    run = _run_payload()
    old = _pr_payload()
    new = _pr_payload(sha="new-sha")
    api = _workflow_api(
        run,
        pr=old,
        reviews=[
            {
                "id": 894,
                "user": {"login": "github-actions[bot]"},
                "state": "PENDING",
                "commit_id": "abc123",
                "body": (
                    f"[claude-review-approval] old head {_identity_marker(head_sha='abc123')}"
                ),
            },
            {
                "id": 895,
                "user": {"login": "github-actions[bot]"},
                "state": "PENDING",
                "commit_id": "new-sha",
                "body": (
                    f"[claude-review-approval] new head {_identity_marker(head_sha='new-sha')}"
                ),
            },
        ],
    )
    api.response_sequences["/pulls/10"] = [old, old, new]

    result = approval.process_event({"workflow_run": run}, api)

    assert result == "PR #10 identity changed during pending cleanup; approval remains absent"
    assert api.deletes == ["/pulls/10/reviews/894"]
    assert api.posts == []


def test_lost_submit_response_dismisses_known_review() -> None:
    """An indeterminate submit is cleaned up using the pending review ID."""

    class LostSubmitResponseAPI(FakeAPI):
        def post(
            self,
            path: str,
            payload: Mapping[str, approval.JsonValue] | None = None,
        ) -> approval.JsonValue:
            result = super().post(path, payload)
            if path.endswith("/events"):
                raise RuntimeError("response lost")
            return result

    run = _run_payload()
    base = _workflow_api(run)
    api = LostSubmitResponseAPI(base.responses)

    result = approval.process_event({"workflow_run": run}, api)

    assert result == "approved PR #10 at abc123 after response reconciliation"
    assert api.posts[-1] == ("/pulls/10/reviews/900/events", {"event": "APPROVE"})
    assert api.puts == []


def test_lost_submit_while_pending_retains_review_for_retry() -> None:
    """An unaccepted submit retains exact pending evidence for explicit retry."""

    class PendingLostSubmitAPI(FakeAPI):
        def get(self, path: str) -> approval.JsonValue:
            if path == "/pulls/10/reviews/900":
                return {
                    "state": "PENDING",
                    "commit_id": "abc123",
                    "body": self.review_bodies[900],
                }
            return super().get(path)

        def post(
            self,
            path: str,
            payload: Mapping[str, approval.JsonValue] | None = None,
        ) -> approval.JsonValue:
            result = super().post(path, payload)
            if path.endswith("/events"):
                raise RuntimeError("response lost")
            return result

    run = _run_payload()
    base = _workflow_api(run)
    api = PendingLostSubmitAPI(base.responses)

    with pytest.raises(RuntimeError, match="exact pending review retained for retry"):
        approval.process_event({"workflow_run": run}, api)

    assert api.deletes == []
    assert api.puts == []


def test_lost_submit_unknown_state_is_activation_blocker() -> None:
    """An indeterminate submit with an unknown state requires operator audit."""

    class PendingLostSubmitCleanupAPI(FakeAPI):
        def get(self, path: str) -> approval.JsonValue:
            if path == "/pulls/10/reviews/900":
                return {
                    "state": "UNKNOWN",
                    "commit_id": "abc123",
                    "body": self.review_bodies[900],
                }
            return super().get(path)

        def post(
            self,
            path: str,
            payload: Mapping[str, approval.JsonValue] | None = None,
        ) -> approval.JsonValue:
            result = super().post(path, payload)
            if path.endswith("/events"):
                raise RuntimeError("response lost")
            return result

    run = _run_payload()
    base = _workflow_api(run)
    api = PendingLostSubmitCleanupAPI(base.responses)

    with pytest.raises(RuntimeError, match="operator audit required before activation"):
        approval.process_event({"workflow_run": run}, api)


def test_lost_submit_state_lookup_failure_is_activation_blocker() -> None:
    """An indeterminate submit plus failed state lookup requires operator audit."""

    class LostSubmitAndCleanupAPI(FakeAPI):
        def post(
            self,
            path: str,
            payload: Mapping[str, approval.JsonValue] | None = None,
        ) -> approval.JsonValue:
            result = super().post(path, payload)
            if path.endswith("/events"):
                raise RuntimeError("response lost")
            return result

        def get(self, path: str) -> approval.JsonValue:
            if path == "/pulls/10/reviews/900":
                raise RuntimeError("state lookup failed")
            return super().get(path)

    run = _run_payload()
    base = _workflow_api(run)
    api = LostSubmitAndCleanupAPI(base.responses)

    with pytest.raises(RuntimeError, match="operator audit required before activation"):
        approval.process_event({"workflow_run": run}, api)


def test_post_publication_dismissal_epoch_removes_old_approval() -> None:
    """A dismissal racing publication prevents an older handler from restoring evidence."""

    run = _run_payload()
    dismissed: approval.JsonObject = {
        "id": 901,
        "user": {"login": "github-actions[bot]"},
        "state": "DISMISSED",
        "commit_id": "abc123",
        "body": _approval_body("replacement started", run_order=(7, 20, 1)),
    }
    api = _workflow_api(run)
    review_sequence: list[approval.JsonValue] = [
        [],
        [],
        [],
        [],
        [],
        [],
        [dismissed],
    ]
    api.response_sequences["/pulls/10/reviews?per_page=100&page=1"] = review_sequence

    result = approval.process_event({"workflow_run": run}, api)

    assert result == (
        "review run is not newer than the post-publication dismissal epoch for PR "
        "#10; new review dismissed"
    )
    assert api.puts == [
        (
            "/pulls/10/reviews/900/dismissals",
            {"message": "A newer dismissal epoch appeared while approval was published."},
        )
    ]


def test_post_publication_epoch_api_failure_dismisses_approval() -> None:
    """A failed final epoch lookup cannot leave the submitted approval active."""

    class FailingFinalEpochAPI(FakeAPI):
        review_reads = 0

        def get(self, path: str) -> approval.JsonValue:
            if path == "/pulls/10/reviews?per_page=100&page=1":
                self.review_reads += 1
                if self.review_reads == 7:
                    raise RuntimeError("epoch lookup failed")
            return super().get(path)

    run = _run_payload()
    base = _workflow_api(run)
    api = FailingFinalEpochAPI(base.responses)

    with pytest.raises(RuntimeError, match="epoch lookup failed"):
        approval.process_event({"workflow_run": run}, api)

    assert api.puts == [
        (
            "/pulls/10/reviews/900/dismissals",
            {"message": "Post-publication dismissal-epoch validation failed."},
        )
    ]


def test_post_publication_epoch_cleanup_failure_is_activation_blocker() -> None:
    """A failed final epoch lookup plus failed dismissal requires operator audit."""

    class FailingFinalEpochCleanupAPI(FakeAPI):
        review_reads = 0

        def get(self, path: str) -> approval.JsonValue:
            if path == "/pulls/10/reviews?per_page=100&page=1":
                self.review_reads += 1
                if self.review_reads == 7:
                    raise RuntimeError("epoch lookup failed")
            return super().get(path)

        def put(
            self,
            path: str,
            payload: Mapping[str, approval.JsonValue],
        ) -> approval.JsonValue:
            raise RuntimeError("dismissal failed")

    run = _run_payload()
    base = _workflow_api(run)
    api = FailingFinalEpochCleanupAPI(base.responses)

    with pytest.raises(RuntimeError, match="operator audit required before activation"):
        approval.process_event({"workflow_run": run}, api)


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


def test_empty_run_association_refreshes_from_trusted_run_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A temporarily empty event association recovers from the exact run."""

    run = _run_payload()
    empty = dict(run)
    empty["pull_requests"] = []
    api = _workflow_api(run)
    api.responses["/actions/runs/100"] = run
    monkeypatch.setattr(approval, "_ASSOCIATION_REFRESH_DELAYS_SECONDS", (0.0,))

    result = approval.process_event({"workflow_run": empty}, api)

    assert result == "approved PR #10 at abc123"
    assert api.posts[0][0] == "/pulls/10/reviews"


def test_persistently_empty_association_reruns_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing trusted association gets one bounded automatic recovery run."""

    run = _run_payload()
    run["pull_requests"] = []
    api = FakeAPI({"/actions/runs/100": run})
    monkeypatch.setattr(approval, "_ASSOCIATION_REFRESH_DELAYS_SECONDS", (0.0,))

    result = approval.process_event({"action": "completed", "workflow_run": run}, api)

    assert result == (
        "review run 100 had no recoverable PR association; re-run once; approval remains absent"
    )
    assert api.posts == [("/actions/runs/100/rerun", None)]


def test_persistently_empty_retry_association_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second association failure cannot loop reruns indefinitely."""

    run = _run_payload(attempt=2)
    run["pull_requests"] = []
    api = FakeAPI({"/actions/runs/100": run})
    monkeypatch.setattr(approval, "_ASSOCIATION_REFRESH_DELAYS_SECONDS", (0.0,))

    result = approval.process_event({"action": "completed", "workflow_run": run}, api)

    assert result == "review run has no recoverable PR association; fail closed"
    assert api.posts == []


def test_empty_association_rejects_changed_refreshed_run_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Association recovery cannot swap in metadata from another run."""

    run = _run_payload()
    run["pull_requests"] = []
    changed = dict(run)
    changed["head_sha"] = "different"
    api = FakeAPI({"/actions/runs/100": changed})
    monkeypatch.setattr(approval, "_ASSOCIATION_REFRESH_DELAYS_SECONDS", (0.0,))

    with pytest.raises(ValueError, match="refreshed workflow run identity changed"):
        approval.process_event({"action": "completed", "workflow_run": run}, api)


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
    ("action", "conclusion", "expected"),
    [
        (
            "in_progress",
            None,
            "newest review run is in_progress; exact-head approval preserved",
        ),
        (
            "completed",
            "failure",
            "review conclusion 'failure' is not success; exact-head approval preserved",
        ),
        (
            "completed",
            "success",
            "PR #10 already has a bot approval for abc123",
        ),
    ],
)
def test_new_attempt_preserves_when_run_inventory_is_stale(
    action: str,
    conclusion: str | None,
    expected: str,
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
    assert result == expected


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


def test_dismissed_same_run_cannot_recreate_replaced_approval() -> None:
    """A delayed completion before the dismissal epoch cannot reapprove."""

    run = _run_payload()
    api = _workflow_api(
        run,
        reviews=[
            {
                "id": 91,
                "user": {"login": "github-actions[bot]"},
                "state": "DISMISSED",
                "commit_id": "abc123",
                "body": _approval_body("replaced", run_order=(7, 20, 1)),
            }
        ],
    )

    result = approval.process_event({"workflow_run": run}, api)

    assert result == (
        "review run is not newer than the dismissed evidence epoch for PR #10; "
        "approval remains absent"
    )
    assert api.posts == []


@pytest.mark.parametrize(
    ("run_number", "attempt"),
    [(20, 2), (21, 1)],
)
def test_newer_replacement_run_can_approve_after_dismissal(
    run_number: int,
    attempt: int,
) -> None:
    """A strictly newer attempt or run may cross the dismissal epoch."""

    run = _run_payload(run_id=101, run_number=run_number, attempt=attempt)
    api = _workflow_api(
        run,
        reviews=[
            {
                "id": 91,
                "user": {"login": "github-actions[bot]"},
                "state": "DISMISSED",
                "commit_id": "abc123",
                "body": _approval_body("replaced", run_order=(7, 20, 1)),
            }
        ],
    )

    result = approval.process_event({"workflow_run": run}, api)

    assert result == "approved PR #10 at abc123"
    assert len(api.posts) == 2


def test_unrelated_dismissal_does_not_create_epoch() -> None:
    """A stranger or another PR identity cannot suppress trusted evidence."""

    run = _run_payload()
    api = _workflow_api(
        run,
        reviews=[
            {
                "id": 91,
                "user": {"login": "someone"},
                "state": "DISMISSED",
                "commit_id": "abc123",
                "body": _approval_body("lookalike", run_order=(7, 99, 1)),
            },
            {
                "id": 92,
                "user": {"login": "github-actions[bot]"},
                "state": "DISMISSED",
                "commit_id": "abc123",
                "body": (
                    "[claude-review-approval] old base "
                    "[claude-review-run-v1 workflow=7 number=99 attempt=1] "
                    f"{_identity_marker(base_ref='release')}"
                ),
            },
        ],
    )

    result = approval.process_event({"workflow_run": run}, api)

    assert result == "approved PR #10 at abc123"


def test_malformed_current_identity_tombstone_fails_closed() -> None:
    """An unparseable current-identity dismissal cannot be bypassed."""

    run = _run_payload()
    api = _workflow_api(
        run,
        reviews=[
            {
                "id": 91,
                "user": {"login": "github-actions[bot]"},
                "state": "DISMISSED",
                "commit_id": "abc123",
                "body": _approval_body("legacy evidence"),
            }
        ],
    )

    with pytest.raises(ValueError, match="lacks a run-order marker"):
        approval.process_event({"workflow_run": run}, api)

    assert api.posts == []


def test_dismissed_epoch_lookup_paginates_and_uses_latest_order() -> None:
    """The newest tombstone is enforced across every review page."""

    run = _run_payload(run_number=21)
    filler: list[approval.JsonValue] = [
        {"user": {"login": "someone"}, "body": None} for _ in range(100)
    ]
    api = _workflow_api(run, reviews=filler)
    api.responses["/pulls/10/reviews?per_page=100&page=2"] = [
        {
            "id": 91,
            "user": {"login": "github-actions[bot]"},
            "state": "DISMISSED",
            "commit_id": "abc123",
            "body": _approval_body("older", run_order=(7, 19, 1)),
        },
        {
            "id": 92,
            "user": {"login": "github-actions[bot]"},
            "state": "DISMISSED",
            "commit_id": "abc123",
            "body": _approval_body("latest", run_order=(7, 21, 1)),
        },
    ]

    result = approval.process_event({"workflow_run": run}, api)

    assert result == (
        "review run is not newer than the dismissed evidence epoch for PR #10; "
        "approval remains absent"
    )
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
    assert len(api.posts) == 2
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
        reviews=[{"user": {"login": "someone"}, "body": None} for _ in range(100)],
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


def _operator_api(
    *,
    permission: str = "admin",
    role_name: str | None = None,
    state: str = "open",
    files: list[approval.JsonValue] | None = None,
    reviews: list[approval.JsonValue] | None = None,
) -> FakeAPI:
    pr = _pr_payload()
    pr["state"] = state
    permission_response: approval.JsonObject = {"permission": permission}
    if role_name is not None:
        permission_response["role_name"] = role_name
    return FakeAPI(
        {
            "/collaborators/syamaner/permission": permission_response,
            "/pulls/10": pr,
            "/pulls/10/files?per_page=100": (
                [{"filename": ".github/workflows/example.yml"}] if files is None else files
            ),
            "/pulls/10/reviews?per_page=100&page=1": [] if reviews is None else reviews,
        }
    )


@pytest.mark.parametrize(
    ("permission", "role_name"),
    [("admin", "admin"), ("write", "maintain")],
)
def test_operator_override_approves_only_current_privileged_identity(
    permission: str,
    role_name: str,
) -> None:
    """A current maintainer can explicitly approve a privileged PR with audit evidence."""

    api = _operator_api(permission=permission, role_name=role_name)

    result = approval.process_event(_operator_event(reason='Reviewed "workflow" change.'), api)

    assert result == "approved PR #10 at abc123"
    assert api.posts[0][0] == "/pulls/10/reviews"
    payload = api.posts[0][1]
    assert payload is not None
    body = payload["body"]
    assert isinstance(body, str)
    assert "[claude-review-operator-override]" in body
    assert "Maintainer @syamaner" in body
    assert "reason-urlencoded=Reviewed%20%22workflow%22%20change." in body
    assert _identity_marker() in body
    assert api.posts[1] == ("/pulls/10/reviews/900/events", {"event": "APPROVE"})


@pytest.mark.parametrize(
    ("permission", "role_name"),
    [("write", "write"), ("write", None), ("read", "read"), ("none", "none")],
)
def test_operator_override_rejects_actor_without_maintain(
    permission: str,
    role_name: str | None,
) -> None:
    """The event sender must still hold repository maintainer authority."""

    api = _operator_api(permission=permission, role_name=role_name)

    with pytest.raises(ValueError, match="current repository maintain permission"):
        approval.process_event(_operator_event(), api)

    assert api.posts == []


@pytest.mark.parametrize("number", ["0", "-1", "01", "not-a-number"])
def test_operator_override_rejects_invalid_pr_number(number: str) -> None:
    """The workflow input must identify one canonical positive PR number."""

    api = _operator_api()

    with pytest.raises(ValueError, match="positive integer"):
        approval.process_event(_operator_event(number=number), api)

    assert api.posts == []


@pytest.mark.parametrize("reason", ["", "   ", "x" * 501])
def test_operator_override_rejects_invalid_reason(reason: str) -> None:
    """Every override carries a bounded non-empty audit reason."""

    api = _operator_api()

    with pytest.raises(ValueError, match="client_payload.reason"):
        approval.process_event(_operator_event(reason=reason), api)

    assert api.posts == []


def test_operator_override_rejects_closed_pr() -> None:
    """A dispatch cannot publish evidence onto a closed pull request."""

    api = _operator_api(state="closed")

    with pytest.raises(ValueError, match="open pull request"):
        approval.process_event(_operator_event(), api)

    assert api.posts == []


def test_operator_override_rejects_ordinary_pr() -> None:
    """The manual path cannot bypass normal Claude review requirements."""

    api = _operator_api(files=[{"filename": "docs/example.md"}])

    with pytest.raises(ValueError, match="restricted to privileged-code-editing PRs"):
        approval.process_event(_operator_event(), api)

    assert api.posts == []


def test_operator_override_finds_privileged_rename_on_later_file_page() -> None:
    """The override eligibility scan covers every available PR-files page."""

    api = _operator_api(files=[{"filename": f"docs/{index}.md"} for index in range(100)])
    api.responses["/pulls/10/files?per_page=100&page=2"] = [
        {
            "filename": "docs/moved.yml",
            "previous_filename": ".github/workflows/ci.yml",
        }
    ]

    result = approval.process_event(_operator_event(), api)

    assert result == "approved PR #10 at abc123"
    assert api.posts[0][0] == "/pulls/10/reviews"


def test_operator_override_rejects_large_ordinary_pr_after_pagination() -> None:
    """One full ordinary page is not itself treated as override eligibility."""

    api = _operator_api(files=[{"filename": f"docs/{index}.md"} for index in range(100)])
    api.responses["/pulls/10/files?per_page=100&page=2"] = []

    with pytest.raises(ValueError, match="restricted to privileged-code-editing PRs"):
        approval.process_event(_operator_event(), api)

    assert api.posts == []


def test_operator_override_rejects_indeterminate_maximum_file_inventory() -> None:
    """The 3,000-file ceiling blocks even with an early privileged match."""

    first_page: list[approval.JsonValue] = [{"filename": ".github/workflows/ci.yml"}]
    first_page.extend({"filename": f"docs/1-{index}.md"} for index in range(99))
    api = _operator_api(files=first_page)
    for page in range(2, 31):
        api.responses[f"/pulls/10/files?per_page=100&page={page}"] = [
            {"filename": f"docs/{page}-{index}.md"} for index in range(100)
        ]

    with pytest.raises(ValueError, match="cannot prove privileged scope"):
        approval.process_event(_operator_event(), api)

    assert api.posts == []


def test_operator_override_is_idempotent_for_exact_identity() -> None:
    """A repeated dispatch does not accumulate counting approvals."""

    body = f"[claude-review-operator-override] recorded {_identity_marker()}"
    api = _operator_api(
        reviews=[
            {
                "id": 61,
                "user": {"login": "github-actions[bot]"},
                "state": "APPROVED",
                "commit_id": "abc123",
                "body": body,
            }
        ]
    )

    result = approval.process_event(_operator_event(), api)

    assert result == "PR #10 already has a recorded operator override for abc123"
    assert api.posts == []


def test_operator_override_dismisses_false_automated_evidence_first() -> None:
    """Privileged override replaces any normal or Dependabot-labelled bot evidence."""

    reviews: list[approval.JsonValue] = [
        {
            "id": 61,
            "user": {"login": "github-actions[bot]"},
            "state": "APPROVED",
            "commit_id": "abc123",
            "body": _approval_body("normal"),
        },
        {
            "id": 62,
            "user": {"login": "github-actions[bot]"},
            "state": "APPROVED",
            "commit_id": "abc123",
            "body": _approval_body("exemption", exempt=True),
        },
    ]
    api = _operator_api(reviews=reviews)

    approval.process_event(_operator_event(), api)

    assert [path for path, _payload in api.puts] == [
        "/pulls/10/reviews/61/dismissals",
        "/pulls/10/reviews/62/dismissals",
    ]
    assert api.posts[0][0] == "/pulls/10/reviews"


def test_operator_reason_cannot_forge_future_base_identity() -> None:
    """Encoded audit text cannot preserve stale approval after a same-head retarget."""

    forged_identity = _identity_marker(base_ref="release")
    publish_api = _operator_api()
    approval.process_event(_operator_event(reason=forged_identity), publish_api)
    payload = publish_api.posts[0][1]
    assert payload is not None
    body = payload["body"]
    assert isinstance(body, str)
    assert forged_identity not in body
    assert "%5Bclaude-review-identity-v1%20" in body

    retarget_api = FakeAPI(
        {
            "/pulls/10": _pr_payload(base_sha="release-tip", base_ref="release"),
            "/pulls/10/reviews?per_page=100&page=1": [
                {
                    "id": 63,
                    "user": {"login": "github-actions[bot]"},
                    "state": "APPROVED",
                    "commit_id": "abc123",
                    "body": body,
                }
            ],
        }
    )

    result = approval.process_event(
        {
            "action": "edited",
            "changes": {"base": {"ref": {"from": "main"}}},
            "pull_request": _pr_payload(base_ref="main"),
        },
        retarget_api,
    )

    assert (
        result
        == "base branch changed and dismissed the prior approval; fresh Claude review required"
    )
    assert retarget_api.puts == [
        (
            "/pulls/10/reviews/63/dismissals",
            {"message": "A newer Claude review attempt must succeed before merge."},
        )
    ]


def test_dependabot_receives_explicit_exemption_approval() -> None:
    """Dependabot is approved through the trusted exemption path."""

    pr = _pr_payload(author="dependabot[bot]")
    api = FakeAPI(
        {
            "/pulls/10": _pr_payload(author="dependabot[bot]"),
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


def test_dependabot_workflow_run_routes_only_to_exemption() -> None:
    """A skipped review workflow cannot become false Claude evidence."""

    run = _run_payload()
    api = _workflow_api(run, pr=_pr_payload(author="dependabot[bot]"))

    result = approval.process_event({"workflow_run": run}, api)

    assert result == "approved PR #10 at abc123"
    assert len(api.posts) == 2
    assert "[claude-review-exempt]" in str(api.posts[0][1])
    assert "[claude-review-approval]" not in str(api.posts[0][1])


def test_dependabot_workflow_run_replaces_false_normal_approval() -> None:
    """Author routing dismisses false normal evidence before exemption."""

    run = _run_payload()
    api = _workflow_api(
        run,
        pr=_pr_payload(author="dependabot[bot]"),
        reviews=[
            {
                "id": 93,
                "user": {"login": "github-actions[bot]"},
                "state": "APPROVED",
                "commit_id": "abc123",
                "body": _approval_body("false normal evidence"),
            }
        ],
    )

    result = approval.process_event({"workflow_run": run}, api)

    assert result == "approved PR #10 at abc123"
    assert api.puts == [
        (
            "/pulls/10/reviews/93/dismissals",
            {"message": "A newer Claude review attempt must succeed before merge."},
        )
    ]
    assert "[claude-review-exempt]" in str(api.posts[0][1])


def test_dependabot_workflow_run_preserves_existing_exemption() -> None:
    """Either event ordering converges on one explicit exemption."""

    run = _run_payload()
    api = _workflow_api(
        run,
        pr=_pr_payload(author="dependabot[bot]"),
        reviews=[
            {
                "id": 94,
                "user": {"login": "github-actions[bot]"},
                "state": "APPROVED",
                "commit_id": "abc123",
                "body": _approval_body("safe exemption", exempt=True),
            }
        ],
    )

    result = approval.process_event({"workflow_run": run}, api)

    assert result == "PR #10 already has a bot exemption for abc123"
    assert api.posts == []
    assert api.puts == []


def test_dependabot_workflow_run_cleans_false_normal_beside_exemption() -> None:
    """Mixed evidence converges to only the labelled exemption."""

    run = _run_payload()
    api = _workflow_api(
        run,
        pr=_pr_payload(author="dependabot[bot]"),
        reviews=[
            {
                "id": 97,
                "user": {"login": "github-actions[bot]"},
                "state": "APPROVED",
                "commit_id": "abc123",
                "body": _approval_body("false normal evidence"),
            },
            {
                "id": 98,
                "user": {"login": "github-actions[bot]"},
                "state": "APPROVED",
                "commit_id": "abc123",
                "body": _approval_body("safe exemption", exempt=True),
            },
        ],
    )

    result = approval.process_event({"workflow_run": run}, api)

    assert result == "PR #10 already has a bot exemption for abc123"
    assert api.puts == [
        (
            "/pulls/10/reviews/97/dismissals",
            {"message": "A newer Claude review attempt must succeed before merge."},
        )
    ]
    assert api.posts == []


def test_privileged_dependabot_workflow_run_preserves_operator_override() -> None:
    """Privileged automation removes false evidence but preserves maintainer authority."""

    run = _run_payload()
    api = _workflow_api(
        run,
        pr=_pr_payload(author="dependabot[bot]"),
        reviews=[
            {
                "id": 95,
                "user": {"login": "github-actions[bot]"},
                "state": "APPROVED",
                "commit_id": "abc123",
                "body": _approval_body("false normal evidence"),
            },
            {
                "id": 96,
                "user": {"login": "github-actions[bot]"},
                "state": "APPROVED",
                "commit_id": "abc123",
                "body": _approval_body("unsafe exemption", exempt=True),
            },
            {
                "id": 97,
                "user": {"login": "github-actions[bot]"},
                "state": "APPROVED",
                "commit_id": "abc123",
                "body": (f"[claude-review-operator-override] authorized {_identity_marker()}"),
            },
        ],
    )
    api.responses["/pulls/10/files?per_page=100"] = [{"filename": ".github/workflows/ci.yml"}]

    result = approval.process_event({"workflow_run": run}, api)

    assert result == "privileged-code-editing PR requires an explicit maintainer approval"
    assert [path for path, _payload in api.puts] == [
        "/pulls/10/reviews/95/dismissals",
        "/pulls/10/reviews/96/dismissals",
    ]
    assert api.posts == []


def test_dependabot_privileged_edit_requires_maintainer() -> None:
    """Dependency automation cannot alter the bridge and self-exempt."""

    api = FakeAPI(
        {
            "/pulls/10/files?per_page=100": [{"filename": "scripts/claude_review_approval.py"}],
            "/pulls/10/reviews?per_page=100&page=1": [
                {
                    "id": 99,
                    "user": {"login": "github-actions[bot]"},
                    "state": "APPROVED",
                    "commit_id": "abc123",
                    "body": _approval_body("unsafe exemption", exempt=True),
                },
                {
                    "id": 100,
                    "user": {"login": "github-actions[bot]"},
                    "state": "APPROVED",
                    "commit_id": "abc123",
                    "body": (f"[claude-review-operator-override] authorized {_identity_marker()}"),
                },
            ],
        }
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
    assert api.puts == [
        (
            "/pulls/10/reviews/99/dismissals",
            {"message": "A newer Claude review attempt must succeed before merge."},
        )
    ]


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


def test_maximum_file_inventory_fails_closed_as_possible_workflow_edit() -> None:
    """GitHub's 3,000-file listing ceiling cannot hide a workflow edit."""

    run = _run_payload()
    api = _workflow_api(run)
    api.responses["/pulls/10/files?per_page=100"] = [
        {"filename": f"src/file-{index}.py"} for index in range(100)
    ]
    for page in range(2, 31):
        api.responses[f"/pulls/10/files?per_page=100&page={page}"] = [
            {"filename": f"src/page-{page}-file-{index}.py"} for index in range(100)
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


def test_closed_lifecycle_event_does_not_touch_approval() -> None:
    """Closing a PR does not mutate review evidence."""

    api = FakeAPI({})

    result = approval.process_event(
        {"action": "closed", "pull_request": _pr_payload()},
        api,
    )

    assert result == "pull_request_target action 'closed' does not require approval work"


@pytest.mark.parametrize("action", ["reopened", "ready_for_review"])
def test_recovery_lifecycle_preserves_existing_exact_head_approval(action: str) -> None:
    """Recovery transitions preserve valid evidence while starting additive review."""

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
        {"action": action, "pull_request": _pr_payload()},
        api,
    )

    assert result == f"{action} PR #10 retains its exact-head approval"
    assert api.posts == []
    assert api.puts == []


@pytest.mark.parametrize("action", ["reopened", "ready_for_review"])
def test_unapproved_recovery_lifecycle_waits_for_automatic_review(action: str) -> None:
    """Lifecycle recovery needs no prior run history or bridge-side dispatch."""

    api = FakeAPI(
        {
            "/pulls/10/reviews?per_page=100&page=1": [],
            "/pulls/10/files?per_page=100": [],
        }
    )

    result = approval.process_event(
        {"action": action, "pull_request": _pr_payload()},
        api,
    )

    assert result == f"{action} PR #10 waits for its automatically started Claude review"
    assert api.posts == []


@pytest.mark.parametrize("action", ["reopened", "ready_for_review"])
def test_privileged_recovery_lifecycle_never_trusts_review(action: str) -> None:
    """Workflow or bridge edits still require the recorded maintainer path."""

    api = FakeAPI(
        {
            "/pulls/10/reviews?per_page=100&page=1": [],
            "/pulls/10/files?per_page=100": [{"filename": "scripts/claude_review_approval.py"}],
        }
    )

    result = approval.process_event(
        {"action": action, "pull_request": _pr_payload()},
        api,
    )

    assert result == "privileged-code-editing PR requires an explicit maintainer approval"
    assert api.posts == []


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
                    "body": (
                        f"[claude-review-approval] old base {_identity_marker(base_ref='release')}"
                    ),
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


def test_base_retarget_rejects_prefix_matching_stale_identity() -> None:
    """Base refs with a current-ref prefix cannot satisfy exact identity."""

    api = FakeAPI(
        {
            "/pulls/10/reviews?per_page=100&page=1": [
                {
                    "id": 80,
                    "user": {"login": "github-actions[bot]"},
                    "state": "APPROVED",
                    "commit_id": "abc123",
                    "body": (
                        "[claude-review-approval] stale prefixed base "
                        f"{_identity_marker(base_ref='main-old')}"
                    ),
                }
            ]
        }
    )

    result = approval.process_event(
        {
            "action": "edited",
            "changes": {"base": {"ref": {"from": "main-old"}}},
            "pull_request": _pr_payload(),
        },
        api,
    )

    assert result == (
        "base branch changed and dismissed the prior approval; fresh Claude review required"
    )
    assert api.puts == [
        (
            "/pulls/10/reviews/80/dismissals",
            {"message": "A newer Claude review attempt must succeed before merge."},
        )
    ]
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
                    "body": (
                        f"[claude-review-approval] old base {_identity_marker(base_ref='release')}"
                    ),
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


def test_delayed_base_retarget_dismisses_only_departed_identity() -> None:
    """A delayed A-to-B event cannot remove approval already published for C."""

    api = FakeAPI(
        {
            "/pulls/10": _pr_payload(base_sha="base-c", base_ref="target-c"),
            "/pulls/10/reviews?per_page=100&page=1": [
                {
                    "id": 81,
                    "user": {"login": "github-actions[bot]"},
                    "state": "APPROVED",
                    "commit_id": "abc123",
                    "body": (
                        f"[claude-review-approval] target A {_identity_marker(base_ref='target-a')}"
                    ),
                },
                {
                    "id": 82,
                    "user": {"login": "github-actions[bot]"},
                    "state": "APPROVED",
                    "commit_id": "abc123",
                    "body": (
                        f"[claude-review-approval] target C {_identity_marker(base_ref='target-c')}"
                    ),
                },
                {
                    "id": 88,
                    "user": {"login": "github-actions[bot]"},
                    "state": "APPROVED",
                    "commit_id": "abc123",
                    "body": (
                        f"[claude-review-approval] target B {_identity_marker(base_ref='target-b')}"
                    ),
                },
            ],
        }
    )

    result = approval.process_event(
        {
            "action": "edited",
            "changes": {"base": {"ref": {"from": "target-a"}}},
            "pull_request": _pr_payload(base_sha="base-b", base_ref="target-b"),
        },
        api,
    )

    assert result == (
        "base branch changed and dismissed the prior approval; "
        "fresh current-base approval preserved"
    )
    assert api.puts == [
        (
            "/pulls/10/reviews/81/dismissals",
            {"message": "A newer Claude review attempt must succeed before merge."},
        ),
        (
            "/pulls/10/reviews/88/dismissals",
            {"message": "A newer Claude review attempt must succeed before merge."},
        ),
    ]
    assert api.posts == []


def test_delayed_retarget_after_base_cycle_preserves_current_approval() -> None:
    """A delayed A-to-B handler cannot remove fresh evidence after B-to-A."""

    api = FakeAPI(
        {
            "/pulls/10": _pr_payload(base_sha="base-a-new", base_ref="target-a"),
            "/pulls/10/reviews?per_page=100&page=1": [
                {
                    "id": 87,
                    "user": {"login": "github-actions[bot]"},
                    "state": "APPROVED",
                    "commit_id": "abc123",
                    "body": (
                        "[claude-review-approval] fresh target A "
                        f"{_identity_marker(base_ref='target-a')}"
                    ),
                },
                {
                    "id": 89,
                    "user": {"login": "github-actions[bot]"},
                    "state": "APPROVED",
                    "commit_id": "abc123",
                    "body": (
                        "[claude-review-approval] stale target B "
                        f"{_identity_marker(base_ref='target-b')}"
                    ),
                },
                {
                    "id": 91,
                    "user": {"login": "human-reviewer"},
                    "state": "APPROVED",
                    "commit_id": "abc123",
                    "body": None,
                },
            ],
        }
    )

    result = approval.process_event(
        {
            "action": "edited",
            "changes": {"base": {"ref": {"from": "target-a"}}},
            "pull_request": _pr_payload(base_sha="base-b", base_ref="target-b"),
        },
        api,
    )

    assert result == (
        "base branch changed and dismissed the prior approval; "
        "fresh current-base approval preserved"
    )
    assert api.puts == [
        (
            "/pulls/10/reviews/89/dismissals",
            {"message": "A newer Claude review attempt must succeed before merge."},
        )
    ]
    assert api.deletes == []
    assert api.posts == []


def test_base_retarget_deletes_only_departed_pending_review() -> None:
    """A delayed A-to-B event deletes A pending evidence but preserves C."""

    api = FakeAPI(
        {
            "/pulls/10": _pr_payload(base_sha="base-c", base_ref="target-c"),
            "/pulls/10/reviews?per_page=100&page=1": [
                {
                    "id": 83,
                    "user": {"login": "github-actions[bot]"},
                    "state": "PENDING",
                    "commit_id": "abc123",
                    "body": (
                        f"[claude-review-approval] target A {_identity_marker(base_ref='target-a')}"
                    ),
                },
                {
                    "id": 84,
                    "user": {"login": "github-actions[bot]"},
                    "state": "PENDING",
                    "commit_id": "abc123",
                    "body": (
                        f"[claude-review-approval] target C {_identity_marker(base_ref='target-c')}"
                    ),
                },
                {
                    "id": 90,
                    "user": {"login": "github-actions[bot]"},
                    "state": "PENDING",
                    "commit_id": "abc123",
                    "body": (
                        f"[claude-review-approval] target B {_identity_marker(base_ref='target-b')}"
                    ),
                },
            ],
        }
    )

    result = approval.process_event(
        {
            "action": "edited",
            "changes": {"base": {"ref": {"from": "target-a"}}},
            "pull_request": _pr_payload(base_sha="base-b", base_ref="target-b"),
        },
        api,
    )

    assert result == (
        "base branch changed and dismissed the prior approval; fresh Claude review required"
    )
    assert api.deletes == ["/pulls/10/reviews/83", "/pulls/10/reviews/90"]
    assert api.puts == []


def test_dependabot_retarget_preserves_current_pending_exemption() -> None:
    """A delayed retarget deletes departed exemption pending, not current work."""

    api = FakeAPI(
        {
            "/pulls/10": _pr_payload(
                author="dependabot[bot]",
                base_sha="base-c",
                base_ref="target-c",
            ),
            "/pulls/10/files?per_page=100": [],
            "/pulls/10/reviews?per_page=100&page=1": [
                {
                    "id": 85,
                    "user": {"login": "github-actions[bot]"},
                    "state": "PENDING",
                    "commit_id": "abc123",
                    "body": (
                        f"[claude-review-exempt] target A {_identity_marker(base_ref='target-a')}"
                    ),
                },
                {
                    "id": 86,
                    "user": {"login": "github-actions[bot]"},
                    "state": "PENDING",
                    "commit_id": "abc123",
                    "body": (
                        f"[claude-review-exempt] target C {_identity_marker(base_ref='target-c')}"
                    ),
                },
            ],
        }
    )

    result = approval.process_event(
        {
            "action": "edited",
            "changes": {"base": {"ref": {"from": "target-a"}}},
            "pull_request": _pr_payload(
                author="dependabot[bot]",
                base_sha="base-b",
                base_ref="target-b",
            ),
        },
        api,
    )

    assert result == (
        "base branch changed and dismissed the prior approval; "
        "another bridge handler owns pending review evidence for PR #10; deferred"
    )
    assert api.deletes == ["/pulls/10/reviews/85"]
    assert api.posts == []


def test_dependabot_base_retarget_replaces_exemption_after_recheck() -> None:
    """A safe retarget receives fresh exemption evidence without deadlock."""

    api = FakeAPI(
        {
            "/pulls/10": _pr_payload(author="dependabot[bot]"),
            "/pulls/10/files?per_page=100": [],
            "/pulls/10/reviews?per_page=100&page=1": [
                {
                    "id": 72,
                    "user": {"login": "github-actions[bot]"},
                    "state": "APPROVED",
                    "commit_id": "abc123",
                    "body": (
                        f"[claude-review-exempt] Dependabot {_identity_marker(base_ref='release')}"
                    ),
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
            },
        ),
        ("/pulls/10/reviews/900/events", {"event": "APPROVE"}),
    ]


def test_dependabot_base_retarget_preserves_fresh_current_exemption() -> None:
    """A delayed retarget handler cannot replace current exemption evidence."""

    api = FakeAPI(
        {
            "/pulls/10": _pr_payload(author="dependabot[bot]"),
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

    assert result == "base branch changed; PR #10 already has a bot exemption for abc123"
    assert api.puts == []
    assert api.posts == []


def test_dependabot_base_retarget_dismisses_only_stale_exemption() -> None:
    """Fresh exemption is preserved when stale and current evidence coexist."""

    api = FakeAPI(
        {
            "/pulls/10": _pr_payload(author="dependabot[bot]"),
            "/pulls/10/files?per_page=100": [],
            "/pulls/10/reviews?per_page=100&page=1": [
                {
                    "id": 78,
                    "user": {"login": "github-actions[bot]"},
                    "state": "APPROVED",
                    "commit_id": "abc123",
                    "body": (
                        f"[claude-review-exempt] old base {_identity_marker(base_ref='release')}"
                    ),
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
        "PR #10 already has a bot exemption for abc123"
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
            "/pulls/10": _pr_payload(author="dependabot[bot]"),
            "/pulls/10/files?per_page=100": [
                {"filename": ".github/workflows/claude-code-review.yml"}
            ],
            "/pulls/10/reviews?per_page=100&page=1": [
                {
                    "id": 73,
                    "user": {"login": "github-actions[bot]"},
                    "state": "APPROVED",
                    "commit_id": "abc123",
                    "body": (
                        f"[claude-review-exempt] Dependabot {_identity_marker(base_ref='release')}"
                    ),
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


def test_workflows_preserve_all_bridge_events_and_metadata_reviews() -> None:
    """Workflow triggers preserve every reconciliation and review event."""

    root = Path(__file__).resolve().parents[1]
    bridge = (root / ".github/workflows/claude-review-approval.yml").read_text(encoding="utf-8")
    reviewer = (root / ".github/workflows/claude-code-review.yml").read_text(encoding="utf-8")

    assert "types: [opened, synchronize, ready_for_review, reopened, edited]" in bridge
    assert "github.event.action == 'ready_for_review' ||" in bridge
    assert "github.event.action == 'reopened' ||" in bridge
    assert "concurrency:" not in bridge
    assert "run: python3 -I scripts/claude_review_approval.py" in bridge
    assert "types: [opened, synchronize, ready_for_review, reopened, edited]" in reviewer
    assert "github.event.pull_request.user.login != 'dependabot[bot]'" in reviewer
    assert "github.event.action != 'edited'" not in reviewer
    assert "github.event.changes.base.ref.from" not in reviewer


_TRACK_PROGRESS_ALLOW_LIST = (
    "${{ github.event.action == 'opened' ||\n"
    "    github.event.action == 'synchronize' ||\n"
    "    github.event.action == 'ready_for_review' ||\n"
    "    github.event.action == 'reopened' }}"
)


def _mapping(value: object) -> dict[str, object]:
    """Narrow an untyped parsed YAML mapping."""
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _reviewer_workflow() -> dict[str, object]:
    """Load the reviewer workflow, normalizing PyYAML's bare `on:` key.

    PyYAML's YAML-1.1 resolver reads a bare `on:` key as the boolean `True`,
    not the string `"on"`; normalize before narrowing to a mapping.
    """
    root = Path(__file__).resolve().parents[1]
    reviewer_path = root / ".github/workflows/claude-code-review.yml"
    loaded = yaml.safe_load(reviewer_path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    raw_workflow = cast(dict[object, object], loaded)
    return _mapping(
        {"on" if key is True else cast(str, key): value for key, value in raw_workflow.items()}
    )


def _claude_review_action_step(job: dict[str, object]) -> dict[str, object]:
    """Return the claude-code-action step from the reviewer job."""
    steps = job["steps"]
    assert isinstance(steps, list)
    for raw_step in cast(list[object], steps):
        step = _mapping(raw_step)
        if step.get("uses") == "anthropics/claude-code-action@v1":
            return step
    raise AssertionError("claude-code-action step not found")


def test_track_progress_disabled_only_for_unsupported_pull_request_actions() -> None:
    """#735: `track_progress` must fail closed for `edited` and unknown actions.

    `edited` stays a fully reviewed trigger (#735 acceptance criterion 1) —
    the reviewer must still run, only the unsupported progress-comment
    tracking is disabled for it. This is a structural regression test: it
    fails if tracking is re-enabled unconditionally, `edited` is dropped
    from the trigger list, the action step becomes conditionally skipped, the
    concurrency policy changes, or job permissions widen.
    """
    workflow = _reviewer_workflow()
    jobs = _mapping(workflow["jobs"])
    job = _mapping(jobs["claude-review"])

    # Trigger list: `edited` present, executing a real review (not skipped).
    on = _mapping(workflow["on"])
    pull_request = _mapping(on["pull_request"])
    assert pull_request["types"] == [
        "opened",
        "synchronize",
        "ready_for_review",
        "reopened",
        "edited",
    ]

    # track_progress: exact fail-closed allow-list, edited absent from it.
    step = _claude_review_action_step(job)
    with_block = _mapping(step["with"])
    track_progress = with_block["track_progress"]
    assert isinstance(track_progress, str)
    assert track_progress == _TRACK_PROGRESS_ALLOW_LIST
    assert "edited" not in track_progress

    # The action step itself is never conditionally skipped.
    assert "if" not in step

    # The job `if` remains only the Dependabot-author guard.
    assert job["if"] == "${{ github.event.pull_request.user.login != 'dependabot[bot]' }}"

    # Concurrency group and cancellation policy are unchanged.
    concurrency = _mapping(workflow["concurrency"])
    assert concurrency["group"] == "claude-review-${{ github.event.pull_request.number }}"
    assert concurrency["cancel-in-progress"] is True

    # Job permissions remain exactly this read-only + id-token set.
    permissions = _mapping(job["permissions"])
    assert permissions == {
        "contents": "read",
        "pull-requests": "read",
        "issues": "read",
        "id-token": "write",
    }


def test_missing_matching_inventory_uses_authoritative_incoming_event() -> None:
    """A lagging inventory cannot permanently defer exact incoming success."""

    run = _run_payload()
    api = _workflow_api(run, runs=[_run_payload(number=11)])

    result = approval.process_event({"workflow_run": run}, api)

    assert result == "approved PR #10 at abc123"


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
    requests: list[object] = []

    def fake_urlopen(*args: object, **_kwargs: object) -> _Response:
        requests.append(args[0])
        return _Response(next(bodies))

    monkeypatch.setattr(approval, "urlopen", fake_urlopen)
    client = approval.RESTClient("owner/repo", "token")

    assert client.get("/value") == {"ok": True}
    assert client.post("/empty", {"value": 1}) is None
    assert client.put("/review", {"message": "stale"}) == {"dismissed": True}
    get_request, post_request, put_request = requests
    assert isinstance(get_request, approval.Request)
    assert isinstance(post_request, approval.Request)
    assert isinstance(put_request, approval.Request)
    assert get_request.get_header("Content-type") is None
    assert post_request.get_header("Content-type") == "application/json"
    assert post_request.data == b'{"value": 1}'
    assert put_request.get_header("Content-type") == "application/json"
    assert put_request.data == b'{"message": "stale"}'


def test_rest_client_redacts_http_error_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API failures expose only method, path, and status."""

    def fail(*_args: object, **_kwargs: object) -> None:
        raise HTTPError("url", 403, "forbidden", Message(), None)

    monkeypatch.setattr(approval, "urlopen", fail)

    with pytest.raises(RuntimeError, match=r"GET /value failed with HTTP 403"):
        approval.RESTClient("owner/repo", "secret").get("/value")


@pytest.mark.parametrize("error", [URLError("dns failed"), TimeoutError("timed out")])
def test_rest_client_normalizes_transport_errors(
    monkeypatch: pytest.MonkeyPatch,
    error: OSError,
) -> None:
    """Connection failures become cleanup-aware runtime failures."""

    def fail(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(approval, "urlopen", fail)

    with pytest.raises(RuntimeError, match=r"GET /value transport failed"):
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
        json.dumps({"action": "closed", "pull_request": _pr_payload()}),
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
