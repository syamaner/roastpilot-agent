"""Publish PR-scoped Claude approvals from trusted default-branch code."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject = dict[str, JsonValue]

_APPROVAL_MARKER = "[claude-review-approval]"
_EXEMPTION_MARKER = "[claude-review-exempt]"
_BOT_LOGIN = "github-actions[bot]"
_WORKFLOW_PATH = ".github/workflows/claude-code-review.yml"


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
        request = Request(
            f"{self._base_url}{path}",
            data=data,
            headers=self._headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=30) as response:  # noqa: S310
                body = response.read()
        except HTTPError as exc:
            raise RuntimeError(f"GitHub API {method} {path} failed with HTTP {exc.code}") from exc
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
            api,
        )
    raise ValueError("unsupported event: expected workflow_run or pull_request")


def _process_workflow_run(run: JsonObject, action: str, api: _GitHubAPI) -> str:
    if _string(run.get("path"), "workflow_run.path") != _WORKFLOW_PATH:
        return "review run has an unexpected workflow path; fail closed"
    pr = _pull_request_for_run(run, api)
    if pr is None:
        return "no exact open pull request is associated with the review run; fail closed"
    if _pull_request_edits_workflow(pr.number, api):
        return "workflow-editing PR requires an explicit maintainer approval"
    newest = _newest_run_for_pull_request(run, pr, api)
    run_id = _integer(run.get("id"), "workflow_run.id")
    attempt = _integer(run.get("run_attempt", 1), "workflow_run.run_attempt")
    incoming_order, newest_order = _run_order(run), _run_order(newest)
    if incoming_order != newest_order:
        if incoming_order > newest_order or newest.get("conclusion") != "success":
            _dismiss_approval(pr, api)
        return "review run is not the newest run for this PR/head; deferred"
    if action != "completed":
        if newest.get("conclusion") == "success":
            return "stale start event arrived after successful completion; ignored"
        dismissed = _dismiss_approval(pr, api)
        suffix = " and dismissed the prior approval" if dismissed else ""
        return f"newest review run is {action}{suffix}; approval remains absent"

    conclusion = _string(run.get("conclusion"), "workflow_run.conclusion")
    if conclusion == "success":
        body = (
            f"{_APPROVAL_MARKER} Claude Code Review run {run_id} attempt {attempt} "
            f"completed successfully for `{pr.head[0]}`. Inline findings remain gated "
            "by required conversation resolution."
        )
        return _approve(pr, body, api)
    _dismiss_approval(pr, api)
    if conclusion == "cancelled" and attempt == 1:
        api.post(f"/actions/runs/{run_id}/rerun")
        return f"cancelled review run {run_id} re-run once; approval remains absent"
    return f"review conclusion {conclusion!r} is not success; approval remains absent"


def _process_pull_request_target(
    pr_payload: JsonObject,
    action: str,
    api: _GitHubAPI,
) -> str:
    if action not in {"opened", "synchronize"}:
        return f"pull_request_target action {action!r} does not require approval work"
    pr = _pull_request_from_payload(pr_payload)
    if pr.author != "dependabot[bot]":
        return "normal PR waits for its PR-scoped Claude review"
    body = (
        f"{_EXEMPTION_MARKER} `{pr.head[0]}` is explicitly exempt: "
        "Dependabot cannot receive repository secrets. "
        "CI, codecov, exact-head Codex, conversation resolution, and independent "
        "triage remain required."
    )
    return _approve(pr, body, api)


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
    if not matches:
        raise ValueError("no workflow run matched the exact pull request and head")
    return max(matches, key=_run_order)


def _run_order(run: JsonObject) -> tuple[int, int, str]:
    return (
        _integer(run.get("run_number"), "run_number"),
        _integer(run.get("run_attempt", 1), "run_attempt"),
        _string(run.get("created_at"), "created_at"),
    )


def _summary_matches_pr(summary: JsonObject, pr: _PullRequest) -> bool:
    return (
        _integer(summary.get("number"), "summary.number") == pr.number
        and _ref_identity(summary.get("head"), "summary.head") == pr.head
        and _ref_identity(summary.get("base"), "summary.base") == pr.base
    )


def _pull_request_edits_workflow(number: int, api: _GitHubAPI) -> bool:
    values = _array(api.get(f"/pulls/{number}/files?per_page=100"), "files")
    for value in values:
        file = _object(value, "files[]")
        paths = (file.get("filename"), file.get("previous_filename"))
        if any(isinstance(path, str) and path.startswith(".github/workflows/") for path in paths):
            return True
    return len(values) == 100


def _approve(pr: _PullRequest, body: str, api: _GitHubAPI) -> str:
    if _approval_review(pr, api) is not None:
        return f"PR #{pr.number} already has a bot approval for {pr.head[0]}"
    api.post(
        f"/pulls/{pr.number}/reviews",
        {"body": body, "commit_id": pr.head[0], "event": "APPROVE"},
    )
    return f"approved PR #{pr.number} at {pr.head[0]}"


def _dismiss_approval(pr: _PullRequest, api: _GitHubAPI) -> bool:
    review = _approval_review(pr, api, approval_only=True)
    if review is None:
        return False
    review_id = _integer(review.get("id"), "review.id")
    api.put(
        f"/pulls/{pr.number}/reviews/{review_id}/dismissals",
        {"message": "A newer Claude review attempt must succeed before merge."},
    )
    return True


def _approval_review(
    pr: _PullRequest, api: _GitHubAPI, *, approval_only: bool = False
) -> JsonObject | None:
    for value in _all_reviews(pr.number, api):
        review = _object(value, "reviews[]")
        user = _object(review.get("user"), "review.user")
        if (
            _string(user.get("login"), "review.user.login") == _BOT_LOGIN
            and _string(review.get("state"), "review.state") == "APPROVED"
            and _string(review.get("commit_id"), "review.commit_id") == pr.head[0]
            and (
                (marker := _string(review.get("body", ""), "review.body").split(" ", 1)[0])
                == _APPROVAL_MARKER
                or (not approval_only and marker == _EXEMPTION_MARKER)
            )
        ):
            return review
    return None


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
