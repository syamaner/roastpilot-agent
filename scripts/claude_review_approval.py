"""Publish PR-scoped Claude approvals from trusted default-branch code."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject = dict[str, JsonValue]

_APPROVAL_MARKER = "[claude-review-approval]"
_EXEMPTION_MARKER = "[claude-review-exempt]"
_RUN_ORDER_MARKER = "[claude-review-run-v1"
_BOT_LOGIN = "github-actions[bot]"
_WORKFLOW_PATH = ".github/workflows/claude-code-review.yml"
_ASSOCIATION_REFRESH_DELAYS_SECONDS = (0.0, 1.0, 2.0)


class _GitHubAPI(Protocol):
    def get(self, path: str) -> JsonValue: ...

    def post(self, path: str, payload: Mapping[str, JsonValue] | None = None) -> JsonValue: ...

    def put(self, path: str, payload: Mapping[str, JsonValue]) -> JsonValue: ...


@dataclass(frozen=True)
class _PullRequest:
    number: int
    head: tuple[str, str, int]
    base: tuple[str, str, int]
    author: str


class RESTClient:
    """Authenticated repository-scoped GitHub REST client."""

    def __init__(self, repository: str, token: str) -> None:
        """Initialize the client for one repository."""
        self._base_url = f"https://api.github.com/repos/{repository}"
        self._headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "roastpilot-claude-review-approval",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def get(self, path: str) -> JsonValue:
        """Return decoded JSON from a GET request."""
        return self._request("GET", path, None)

    def post(self, path: str, payload: Mapping[str, JsonValue] | None = None) -> JsonValue:
        """Return decoded JSON from a POST request."""
        return self._request("POST", path, payload)

    def put(self, path: str, payload: Mapping[str, JsonValue]) -> JsonValue:
        """Return decoded JSON from a PUT request."""
        return self._request("PUT", path, payload)

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, JsonValue] | None,
    ) -> JsonValue:
        data = None if payload is None else json.dumps(payload).encode()
        headers = dict(self._headers)
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self._base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=30) as response:  # noqa: S310
                body = response.read()
        except HTTPError as exc:
            raise RuntimeError(f"GitHub API {method} {path} failed with HTTP {exc.code}") from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"GitHub API {method} {path} transport failed") from exc
        if not body:
            return None
        return cast(JsonValue, json.loads(body))


def process_event(event: JsonObject, api: _GitHubAPI) -> str:
    """Process one workflow event.

    Args:
        event: GitHub event payload.
        api: GitHub API client.

    Returns:
        Human-readable action summary.

    """

    if "workflow_run" in event:
        return _process_workflow_run(
            _object(event["workflow_run"], "workflow_run"),
            _string(event.get("action", "completed"), "action"),
            api,
        )
    if "pull_request" in event:
        return _process_pull_request_target(
            _object(event["pull_request"], "pull_request"),
            _string(event.get("action"), "action"),
            _object(event.get("changes", {}), "changes"),
            api,
        )
    raise ValueError("unsupported event: expected workflow_run or pull_request")


def _process_workflow_run(run: JsonObject, action: str, api: _GitHubAPI) -> str:
    if _string(run.get("path"), "workflow_run.path") != _WORKFLOW_PATH:
        return "review run has an unexpected workflow path; fail closed"
    associated_run = _run_with_association(run, api)
    if associated_run is None:
        attempt = _integer(run.get("run_attempt", 1), "workflow_run.run_attempt")
        run_id = _integer(run.get("id"), "workflow_run.id")
        if action == "completed" and attempt == 1:
            api.post(f"/actions/runs/{run_id}/rerun")
            return (
                f"review run {run_id} had no recoverable PR association; "
                "re-run once; approval remains absent"
            )
        return "review run has no recoverable PR association; fail closed"
    run = associated_run
    pr = _pull_request_for_run(run, api)
    if pr is None:
        return "no exact open pull request is associated with the review run; fail closed"
    if pr.author == "dependabot[bot]":
        if _pull_request_edits_privileged_code(pr.number, api):
            _dismiss_approval(pr, api, include_exemption=True)
            return "privileged-code-editing PR requires an explicit maintainer approval"
        return _approve_dependabot(pr, api)
    if _pull_request_edits_privileged_code(pr.number, api):
        return "privileged-code-editing PR requires an explicit maintainer approval"
    existing_approval = _approval_review(pr, api) is not None
    newest = _newest_run_for_pull_request(run, pr, api)
    run_id = _integer(run.get("id"), "workflow_run.id")
    attempt = _integer(run.get("run_attempt", 1), "workflow_run.run_attempt")
    incoming_order, newest_order = _run_order(run), _run_order(newest)
    if incoming_order < newest_order:
        return "review run is not the newest run for this PR/head; deferred"
    if action != "completed":
        if incoming_order == newest_order and newest.get("conclusion") == "success":
            return "stale start event arrived after successful completion; ignored"
        state = "exact-head approval preserved" if existing_approval else "approval remains absent"
        return f"newest review run is {action}; {state}"

    conclusion = _string(run.get("conclusion"), "workflow_run.conclusion")
    if conclusion == "success":
        body = (
            f"{_APPROVAL_MARKER} Claude Code Review run {run_id} attempt {attempt} "
            f"{_run_order_marker(run)} "
            f"completed successfully for `{pr.head[0]}`. {_identity_marker(pr)} "
            f"reviewed-base-sha={_reviewed_base_sha(run, pr)} "
            "Inline findings remain gated by required conversation resolution."
        )
        return _approve(pr, body, api, run_order=_review_run_order(run))
    if conclusion == "cancelled" and attempt == 1:
        api.post(f"/actions/runs/{run_id}/rerun")
        state = "exact-head approval preserved" if existing_approval else "approval remains absent"
        return f"cancelled review run {run_id} re-run once; {state}"
    state = "exact-head approval preserved" if existing_approval else "approval remains absent"
    return f"review conclusion {conclusion!r} is not success; {state}"


def _process_pull_request_target(
    pr_payload: JsonObject,
    action: str,
    changes: JsonObject,
    api: _GitHubAPI,
) -> str:
    if action == "edited":
        if "base" not in changes:
            return "non-base pull request edit does not require approval work"
        pr = _pull_request_from_payload(pr_payload)
        if pr.author == "dependabot[bot]" and _pull_request_edits_privileged_code(pr.number, api):
            dismissed = _dismiss_approval(pr, api, include_exemption=True)
            suffix = " and dismissed the prior approval" if dismissed else ""
            return (
                f"base branch changed{suffix}; privileged-code-editing PR "
                "requires an explicit maintainer approval"
            )
        dismissed = _dismiss_approval(
            pr,
            api,
            include_exemption=True,
            stale_identity_only=True,
        )
        suffix = " and dismissed the prior approval" if dismissed else ""
        if pr.author == "dependabot[bot]":
            approved = _approve_dependabot(pr, api)
            return f"base branch changed{suffix}; {approved}"
        if _approval_review(pr, api) is not None:
            return f"base branch changed{suffix}; fresh current-base approval preserved"
        return f"base branch changed{suffix}; fresh Claude review required"
    if action not in {"opened", "synchronize", "ready_for_review", "reopened"}:
        return f"pull_request_target action {action!r} does not require approval work"
    pr = _pull_request_from_payload(pr_payload)
    if pr.author == "dependabot[bot]":
        if _pull_request_edits_privileged_code(pr.number, api):
            _dismiss_approval(pr, api, include_exemption=True)
            return "privileged-code-editing PR requires an explicit maintainer approval"
        return _approve_dependabot(pr, api)
    if action in {"reopened", "ready_for_review"}:
        if _approval_review(pr, api) is not None:
            return f"{action} PR #{pr.number} retains its exact-head approval"
        if _pull_request_edits_privileged_code(pr.number, api):
            return "privileged-code-editing PR requires an explicit maintainer approval"
        return f"{action} PR #{pr.number} waits for its automatically started Claude review"
    return "normal PR waits for its PR-scoped Claude review"


def _run_with_association(run: JsonObject, api: _GitHubAPI) -> JsonObject | None:
    if _array(run.get("pull_requests"), "workflow_run.pull_requests"):
        return run
    run_id = _integer(run.get("id"), "workflow_run.id")
    for delay in _ASSOCIATION_REFRESH_DELAYS_SECONDS:
        if delay:
            time.sleep(delay)
        refreshed = _object(api.get(f"/actions/runs/{run_id}"), f"workflow run {run_id}")
        if not _same_run_identity(run, refreshed):
            raise ValueError("refreshed workflow run identity changed")
        if _array(refreshed.get("pull_requests"), "workflow_run.pull_requests"):
            return refreshed
    return None


def _same_run_identity(left: JsonObject, right: JsonObject) -> bool:
    return (
        _integer(left.get("id"), "id") == _integer(right.get("id"), "id")
        and _integer(left.get("workflow_id"), "workflow_id")
        == _integer(right.get("workflow_id"), "workflow_id")
        and _string(left.get("path"), "path") == _string(right.get("path"), "path")
        and _string(left.get("head_sha"), "head_sha") == _string(right.get("head_sha"), "head_sha")
        and _string(left.get("head_branch"), "head_branch")
        == _string(right.get("head_branch"), "head_branch")
        and _integer(left.get("run_number"), "run_number")
        == _integer(right.get("run_number"), "run_number")
        and _integer(left.get("run_attempt", 1), "run_attempt")
        == _integer(right.get("run_attempt", 1), "run_attempt")
    )


def _pull_request_for_run(run: JsonObject, api: _GitHubAPI) -> _PullRequest | None:
    head_sha = _string(run.get("head_sha"), "workflow_run.head_sha")
    head_ref = _string(run.get("head_branch"), "workflow_run.head_branch")
    candidates: list[_PullRequest] = []
    for value in _array(run.get("pull_requests"), "workflow_run.pull_requests"):
        summary = _object(value, "workflow_run.pull_requests[]")
        number = _integer(summary.get("number"), "pull_request.number")
        pr = _load_pull_request(number, api)
        if pr.head[0] == head_sha and pr.head[1] == head_ref and _summary_matches_pr(summary, pr):
            candidates.append(pr)
    if len(candidates) != 1:
        return None
    return candidates[0]


def _newest_run_for_pull_request(
    run: JsonObject,
    pr: _PullRequest,
    api: _GitHubAPI,
) -> JsonObject:
    workflow_id = _integer(run.get("workflow_id"), "workflow_run.workflow_id")
    encoded_sha = quote(pr.head[0], safe="")
    response = _object(
        api.get(f"/actions/workflows/{workflow_id}/runs?head_sha={encoded_sha}&per_page=100"),
        "workflow runs response",
    )
    matches = _matching_runs_for_pull_request(response, pr)
    if not matches:
        return run
    return max(matches, key=_run_order)


def _run_order(run: JsonObject) -> tuple[int, int, str]:
    return (
        _integer(run.get("run_number"), "run_number"),
        _integer(run.get("run_attempt", 1), "run_attempt"),
        _string(run.get("created_at"), "created_at"),
    )


def _summary_matches_pr(summary: JsonObject, pr: _PullRequest) -> bool:
    summary_base = _ref_identity(summary.get("base"), "summary.base")
    return (
        _integer(summary.get("number"), "summary.number") == pr.number
        and _ref_identity(summary.get("head"), "summary.head") == pr.head
        and summary_base[1:] == pr.base[1:]
    )


def _reviewed_base_sha(run: JsonObject, pr: _PullRequest) -> str:
    for value in _array(run.get("pull_requests"), "workflow_run.pull_requests"):
        summary = _object(value, "workflow_run.pull_requests[]")
        if _summary_matches_pr(summary, pr):
            return _ref_identity(summary.get("base"), "summary.base")[0]
    raise ValueError("workflow run has no matching reviewed base identity")


def _pull_request_edits_privileged_code(number: int, api: _GitHubAPI) -> bool:
    values = _array(api.get(f"/pulls/{number}/files?per_page=100"), "files")
    for value in values:
        file = _object(value, "files[]")
        paths = (file.get("filename"), file.get("previous_filename"))
        if any(
            isinstance(path, str)
            and (
                path.startswith(".github/workflows/") or path == "scripts/claude_review_approval.py"
            )
            for path in paths
        ):
            return True
    return len(values) == 100


def _matching_runs_for_pull_request(
    response: JsonObject,
    pr: _PullRequest,
) -> list[JsonObject]:
    matches: list[JsonObject] = []
    for value in _array(response.get("workflow_runs"), "workflow_runs"):
        candidate = _object(value, "workflow_runs[]")
        if _string(candidate.get("head_branch"), "run.head_branch") != pr.head[1]:
            continue
        if any(
            _summary_matches_pr(_object(item, "run.pull_requests[]"), pr)
            for item in _array(candidate.get("pull_requests"), "run.pull_requests")
        ):
            matches.append(candidate)
    return matches


def _approve_dependabot(pr: _PullRequest, api: _GitHubAPI) -> str:
    _dismiss_approval(pr, api)
    if _exemption_review(pr, api) is not None:
        return f"PR #{pr.number} already has a bot exemption for {pr.head[0]}"
    body = (
        f"{_EXEMPTION_MARKER} `{pr.head[0]}` is explicitly exempt: "
        f"Dependabot cannot receive repository secrets. {_identity_marker(pr)} "
        f"reviewed-base-sha={pr.base[0]} "
        "CI, codecov, exact-head Codex, conversation resolution, and independent "
        "triage remain required."
    )
    return _approve(pr, body, api)


def _approve(
    pr: _PullRequest,
    body: str,
    api: _GitHubAPI,
    *,
    run_order: tuple[int, int, int] | None = None,
) -> str:
    current = _load_pull_request(pr.number, api)
    if not _same_approval_identity(pr, current):
        return f"PR #{pr.number} identity changed before approval; approval remains absent"
    marker = body.split(" ", 1)[0]
    existing = (
        _exemption_review(pr, api)
        if marker == _EXEMPTION_MARKER
        else _approval_review(pr, api, approval_only=True)
    )
    if existing is not None:
        return f"PR #{pr.number} already has a bot approval for {pr.head[0]}"
    if run_order is not None:
        dismissed_order = _latest_dismissed_run_order(pr, api)
        if dismissed_order is not None:
            if run_order[0] != dismissed_order[0]:
                raise ValueError("dismissed approval belongs to another review workflow")
            if run_order <= dismissed_order:
                return (
                    f"review run is not newer than the dismissed evidence epoch for PR "
                    f"#{pr.number}; approval remains absent"
                )
    created = _object(
        api.post(
            f"/pulls/{pr.number}/reviews",
            {"body": body, "commit_id": pr.head[0], "event": "APPROVE"},
        ),
        "created review",
    )
    review_id = _integer(created.get("id"), "created review.id")
    try:
        current = _load_pull_request(pr.number, api)
    except (RuntimeError, ValueError):
        _dismiss_created_review_or_block(
            pr.number,
            review_id,
            api,
            "Post-publication pull request validation failed.",
        )
        raise
    if not _same_approval_identity(pr, current):
        _dismiss_created_review_or_block(
            pr.number,
            review_id,
            api,
            "The pull request identity changed while approval was published.",
        )
        return f"PR #{pr.number} identity changed after approval; new review dismissed"
    return f"approved PR #{pr.number} at {pr.head[0]}"


def _dismiss_created_review_or_block(
    number: int,
    review_id: int,
    api: _GitHubAPI,
    message: str,
) -> None:
    try:
        _dismiss_created_review(number, review_id, api, message)
    except (RuntimeError, ValueError) as cleanup_error:
        raise RuntimeError(
            "created-review cleanup failed; operator audit required before activation"
        ) from cleanup_error


def _dismiss_created_review(
    number: int,
    review_id: int,
    api: _GitHubAPI,
    message: str,
) -> None:
    api.put(
        f"/pulls/{number}/reviews/{review_id}/dismissals",
        {"message": message},
    )


def _review_run_order(run: JsonObject) -> tuple[int, int, int]:
    return (
        _integer(run.get("workflow_id"), "workflow_id"),
        _integer(run.get("run_number"), "run_number"),
        _integer(run.get("run_attempt", 1), "run_attempt"),
    )


def _run_order_marker(run: JsonObject) -> str:
    workflow_id, run_number, run_attempt = _review_run_order(run)
    return f"{_RUN_ORDER_MARKER} workflow={workflow_id} number={run_number} attempt={run_attempt}]"


def _latest_dismissed_run_order(
    pr: _PullRequest,
    api: _GitHubAPI,
) -> tuple[int, int, int] | None:
    orders: list[tuple[int, int, int]] = []
    identity = _identity_marker(pr)
    for value in _all_reviews(pr.number, api):
        review = _object(value, "reviews[]")
        user = _object(review.get("user"), "review.user")
        body = _string(review.get("body", ""), "review.body")
        if not (
            _string(user.get("login"), "review.user.login") == _BOT_LOGIN
            and _string(review.get("state"), "review.state") == "DISMISSED"
            and _string(review.get("commit_id"), "review.commit_id") == pr.head[0]
            and body.split(" ", 1)[0] == _APPROVAL_MARKER
            and identity in body
        ):
            continue
        orders.append(_parse_run_order_marker(body))
    return max(orders) if orders else None


def _parse_run_order_marker(body: str) -> tuple[int, int, int]:
    start = body.find(f"{_RUN_ORDER_MARKER} ")
    if start < 0:
        raise ValueError("dismissed current-identity approval lacks a run-order marker")
    end = body.find("]", start)
    if end < 0:
        raise ValueError("dismissed current-identity approval has a malformed run-order marker")
    fields: dict[str, int] = {}
    for item in body[start + len(_RUN_ORDER_MARKER) + 1 : end].split():
        name, separator, raw_value = item.partition("=")
        if not separator or name not in {"workflow", "number", "attempt"}:
            raise ValueError("dismissed current-identity approval has a malformed run-order marker")
        try:
            fields[name] = int(raw_value)
        except ValueError as exc:
            raise ValueError(
                "dismissed current-identity approval has a malformed run-order marker"
            ) from exc
    if set(fields) != {"workflow", "number", "attempt"}:
        raise ValueError("dismissed current-identity approval has a malformed run-order marker")
    return fields["workflow"], fields["number"], fields["attempt"]


def _same_approval_identity(left: _PullRequest, right: _PullRequest) -> bool:
    return (
        left.number == right.number and left.head == right.head and left.base[1:] == right.base[1:]
    )


def _dismiss_approval(
    pr: _PullRequest,
    api: _GitHubAPI,
    *,
    include_exemption: bool = False,
    stale_identity_only: bool = False,
) -> bool:
    dismissed = False
    identity = _identity_marker(pr)
    for value in _all_reviews(pr.number, api):
        review = _object(value, "reviews[]")
        if not _is_bridge_approval(review, pr, approval_only=not include_exemption):
            continue
        body = _string(review.get("body", ""), "review.body")
        if stale_identity_only and identity in body:
            continue
        review_id = _integer(review.get("id"), "review.id")
        api.put(
            f"/pulls/{pr.number}/reviews/{review_id}/dismissals",
            {"message": "A newer Claude review attempt must succeed before merge."},
        )
        dismissed = True
    return dismissed


def _approval_review(
    pr: _PullRequest, api: _GitHubAPI, *, approval_only: bool = False
) -> JsonObject | None:
    for value in _all_reviews(pr.number, api):
        review = _object(value, "reviews[]")
        if _is_bridge_approval(
            review,
            pr,
            approval_only=approval_only,
        ) and _identity_marker(pr) in _string(review.get("body", ""), "review.body"):
            return review
    return None


def _exemption_review(pr: _PullRequest, api: _GitHubAPI) -> JsonObject | None:
    for value in _all_reviews(pr.number, api):
        review = _object(value, "reviews[]")
        body = _string(review.get("body", ""), "review.body")
        if (
            _is_bridge_approval(review, pr, approval_only=False)
            and body.split(" ", 1)[0] == _EXEMPTION_MARKER
            and _identity_marker(pr) in body
        ):
            return review
    return None


def _is_bridge_approval(
    review: JsonObject,
    pr: _PullRequest,
    *,
    approval_only: bool,
) -> bool:
    user = _object(review.get("user"), "review.user")
    marker = _string(review.get("body", ""), "review.body").split(" ", 1)[0]
    return (
        _string(user.get("login"), "review.user.login") == _BOT_LOGIN
        and _string(review.get("state"), "review.state") == "APPROVED"
        and _string(review.get("commit_id"), "review.commit_id") == pr.head[0]
        and (marker == _APPROVAL_MARKER or (not approval_only and marker == _EXEMPTION_MARKER))
    )


def _identity_marker(pr: _PullRequest) -> str:
    head_ref = quote(pr.head[1], safe="")
    base_ref = quote(pr.base[1], safe="")
    return (
        f"[claude-review-identity-v1 pr={pr.number} "
        f"head={pr.head[2]}:{head_ref}:{pr.head[0]} "
        f"base={pr.base[2]}:{base_ref}]"
    )


def _all_reviews(number: int, api: _GitHubAPI) -> list[JsonValue]:
    reviews: list[JsonValue] = []
    page = 1
    while True:
        values = _array(api.get(f"/pulls/{number}/reviews?per_page=100&page={page}"), "reviews")
        reviews.extend(values)
        if len(values) < 100:
            return reviews
        page += 1


def _load_pull_request(number: int, api: _GitHubAPI) -> _PullRequest:
    return _pull_request_from_payload(
        _object(api.get(f"/pulls/{number}"), f"pull request #{number}")
    )


def _pull_request_from_payload(payload: JsonObject) -> _PullRequest:
    user = _object(payload.get("user"), "pull_request.user")
    return _PullRequest(
        number=_integer(payload.get("number"), "pull_request.number"),
        head=_ref_identity(payload.get("head"), "pull_request.head"),
        base=_ref_identity(payload.get("base"), "pull_request.base"),
        author=_string(user.get("login"), "pull_request.user.login"),
    )


def _ref_identity(value: JsonValue, name: str) -> tuple[str, str, int]:
    ref = _object(value, name)
    repo = _object(ref.get("repo"), f"{name}.repo")
    return (
        _string(ref.get("sha"), f"{name}.sha"),
        _string(ref.get("ref"), f"{name}.ref"),
        _integer(repo.get("id"), f"{name}.repo.id"),
    )


def _object(value: JsonValue, name: str) -> JsonObject:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _array(value: JsonValue, name: str) -> list[JsonValue]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    return value


def _string(value: JsonValue, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _integer(value: JsonValue, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    return value


def main() -> int:
    """Run the approval handler and return its process exit code."""

    event_path = os.environ.get("GITHUB_EVENT_PATH")
    repository = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if not event_path or not repository or not token:
        print(
            "GITHUB_EVENT_PATH, GITHUB_REPOSITORY, and GITHUB_TOKEN are required",
            file=sys.stderr,
        )
        return 2
    with open(event_path, encoding="utf-8") as handle:
        event = cast(JsonObject, json.load(handle))
    try:
        summary = process_event(event, RESTClient(repository, token))
    except (RuntimeError, ValueError) as exc:
        print(f"Claude approval gate failed closed: {exc}", file=sys.stderr)
        return 1
    print(summary)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
