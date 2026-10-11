"""Open/update/close a persistent failure-tracker issue for a completed
workflow run. Invoked by .github/workflows/failure_alerts.yml on
`workflow_run: completed` for daily_report.yml and e2e_nightly.yml.

Mirrors bruce-bot's common/failure_tracker.py pattern: one issue per
workflow, titled "⚠️ <workflow file> Failures". A failing scheduled run
creates it (or comments on it if it's already open), so a string of
failures is one thread, not N issues. The next successful run closes it.

Why this exists even though daily_report.yml already reports to a tracker
in bruce-bot: that tracker is written with PRIVATE_REPO_TOKEN, the owner's
own PAT, so every issue/comment on it is authored *by the owner* -- and
GitHub never notifies you about your own activity. bruce-bot#491 collected
8 failure comments over 2026-10-07..09 without a single notification. This
script runs with the built-in GITHUB_TOKEN, so the tracker is authored by
github-actions[bot] and the owner (@-mentioned in the issue body) gets a
normal email/mobile notification.

This repo is PUBLIC. The issue body deliberately carries only things that
are already public: the workflow file/name, the run link, the names of the
failed jobs (which appear in the public workflow YAML), the run's
conclusion, and timestamps. Never add step output, logs, or anything read
from bruce-bot here.

Pure stdlib -- no checkout of bruce-bot and no dependency on
PRIVATE_REPO_TOKEN, so this still alerts when the failure *is* that token
(expired/revoked) or bruce-bot's own scripts.

Usage:
    python3 failure_alert.py [--dry-run] [--event-path PATH]

Env:
    GITHUB_EVENT_PATH   workflow_run event payload (or --event-path)
    GITHUB_REPOSITORY   repo whose run completed (owner/name)
    ALERT_REPO          repo to file the tracker in (default: GITHUB_REPOSITORY)
    ALERT_TOKEN         token for ALERT_REPO (issues: write)
    ACTIONS_TOKEN       token for GITHUB_REPOSITORY (actions: read), used to
                        list failed jobs. Defaults to ALERT_TOKEN.
    ALERT_MENTION       login to @-mention in a newly opened tracker (optional)
    DRY_RUN=1           same as --dry-run: reads still happen if a token is
                        set, writes are only printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol

API_ROOT = "https://api.github.com"

# Conclusions that count as "the pipeline is broken". cancelled/skipped/
# neutral/action_required are deliberately ignored -- a cancelled run is a
# human decision, not a breakage, and shouldn't open or close anything.
FAILURE_CONCLUSIONS = {"failure", "timed_out", "startup_failure"}
SUCCESS_CONCLUSIONS = {"success"}
FAILED_JOB_CONCLUSIONS = {"failure", "timed_out", "cancelled", "startup_failure"}

# Only scheduled runs alert -- a failed manual dispatch has someone watching
# it already. Any successful run (scheduled or manual) proves the pipeline
# works again, so any success resolves.
ALERT_EVENTS = {"schedule"}

# Issue authors trusted as "our" tracker. This repo is public, so anyone can
# open an issue with the same title; never comment on or close theirs.
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
BOT_LOGIN = "github-actions[bot]"


class Api(Protocol):
    def request(self, method: str, path: str, body: dict | None = None) -> object: ...


class GitHubApi:
    def __init__(self, token: str):
        self.token = token

    def request(self, method: str, path: str, body: dict | None = None) -> object:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{API_ROOT}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "dannoctl-failure-alert",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            # Status only -- response bodies can echo request content.
            raise RuntimeError(f"GitHub API {method} {path} -> HTTP {exc.code}") from None
        return json.loads(raw) if raw else None


class DryRunApi:
    """Performs reads through *inner* (if any) and prints writes instead of
    sending them."""

    def __init__(self, inner: Api | None):
        self.inner = inner
        self.writes: list[tuple[str, str, dict | None]] = []

    def request(self, method: str, path: str, body: dict | None = None) -> object:
        if method == "GET":
            return self.inner.request(method, path, body) if self.inner else []
        self.writes.append((method, path, body))
        print(f"[dry run] {method} {path}")
        if body:
            for key, value in body.items():
                print(f"  {key}: {value}")
        return {"number": 0, "html_url": "(dry run)"}


def tracker_title(workflow_file: str) -> str:
    return f"⚠️ {workflow_file} Failures"


def classify(run: dict) -> str:
    """Returns "alert", "resolve", or "ignore" for a completed workflow_run."""
    conclusion = run.get("conclusion")
    if conclusion in SUCCESS_CONCLUSIONS:
        return "resolve"
    if conclusion in FAILURE_CONCLUSIONS and run.get("event") in ALERT_EVENTS:
        return "alert"
    return "ignore"


def failed_job_names(api: Api, repo: str, run_id: int) -> list[str]:
    jobs = api.request("GET", f"/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100")
    names = [
        job["name"]
        for job in (jobs or {}).get("jobs", [])
        if job.get("conclusion") in FAILED_JOB_CONCLUSIONS
    ]
    return names


def find_open_tracker(api: Api, repo: str, title: str) -> int | None:
    query = urllib.parse.urlencode({"state": "open", "per_page": 100, "sort": "created", "direction": "desc"})
    issues = api.request("GET", f"/repos/{repo}/issues?{query}") or []
    for issue in issues:
        if "pull_request" in issue or issue.get("title") != title:
            continue
        author = (issue.get("user") or {}).get("login")
        if author == BOT_LOGIN or issue.get("author_association") in TRUSTED_ASSOCIATIONS:
            return issue["number"]
    return None


def workflow_file(run: dict) -> str:
    # run["path"] is ".github/workflows/daily_report.yml" (possibly with an
    # "@ref" suffix for reusable workflows).
    return os.path.basename(run.get("path", "").split("@", 1)[0]) or run.get("name", "workflow")


def failure_body(run: dict, jobs: list[str]) -> str:
    jobs_text = ", ".join(f"`{name}`" for name in jobs) if jobs else "(none reported -- run may have failed to start)"
    return "\n".join(
        [
            f"**{workflow_file(run)}** ({run.get('name', '')}) -- scheduled run "
            f"#{run.get('run_number')} concluded `{run.get('conclusion')}`.",
            "",
            f"- Failed job(s): {jobs_text}",
            f"- Started: {run.get('run_started_at') or run.get('created_at')} (UTC)",
            f"- Finished: {run.get('updated_at')} (UTC)",
            f"- Run: {run.get('html_url')}",
            "",
            "_Run logs here are deleted after ~24h by cleanup_runs.yml -- open the link soon._",
        ]
    )


def handle(
    run: dict,
    *,
    alert_api: Api,
    actions_api: Api,
    alert_repo: str,
    run_repo: str,
    mention: str | None = None,
) -> str:
    """Applies the tracker update for *run*; returns what it did."""
    action = classify(run)
    if action == "ignore":
        print(f"Ignoring {workflow_file(run)} run: event={run.get('event')} conclusion={run.get('conclusion')}")
        return "ignored"

    title = tracker_title(workflow_file(run))
    tracker = find_open_tracker(alert_api, alert_repo, title)

    if action == "resolve":
        if tracker is None:
            print(f"No open tracker '{title}' -- nothing to resolve.")
            return "noop"
        alert_api.request(
            "POST",
            f"/repos/{alert_repo}/issues/{tracker}/comments",
            {"body": f"Resolved: run #{run.get('run_number')} passed at {run.get('updated_at')} (UTC).\n\n{run.get('html_url')}"},
        )
        alert_api.request(
            "PATCH",
            f"/repos/{alert_repo}/issues/{tracker}",
            {"state": "closed", "state_reason": "completed"},
        )
        print(f"Closed tracker #{tracker}.")
        return "closed"

    body = failure_body(run, failed_job_names(actions_api, run_repo, run["id"]))
    if tracker is not None:
        alert_api.request("POST", f"/repos/{alert_repo}/issues/{tracker}/comments", {"body": body})
        print(f"Commented on tracker #{tracker}.")
        return "commented"
    if mention:
        body = f"@{mention} a scheduled pipeline run failed.\n\n{body}"
    created = alert_api.request("POST", f"/repos/{alert_repo}/issues", {"title": title, "body": body})
    print(f"Opened tracker #{(created or {}).get('number')}.")
    return "opened"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--event-path", default=os.environ.get("GITHUB_EVENT_PATH"))
    args = parser.parse_args(argv)
    dry_run = args.dry_run or os.environ.get("DRY_RUN") == "1"

    if not args.event_path:
        print("No event payload (set GITHUB_EVENT_PATH or --event-path).", file=sys.stderr)
        return 2
    with open(args.event_path, encoding="utf-8") as fh:
        run = json.load(fh)["workflow_run"]

    run_repo = os.environ.get("GITHUB_REPOSITORY") or run["repository"]["full_name"]
    alert_repo = os.environ.get("ALERT_REPO") or run_repo
    alert_token = os.environ.get("ALERT_TOKEN")
    actions_token = os.environ.get("ACTIONS_TOKEN") or alert_token

    if not alert_token and not dry_run:
        print("ALERT_TOKEN is not set.", file=sys.stderr)
        return 2

    alert_api: Api = GitHubApi(alert_token) if alert_token else None  # type: ignore[assignment]
    actions_api: Api = GitHubApi(actions_token) if actions_token else None  # type: ignore[assignment]
    if dry_run:
        alert_api = DryRunApi(alert_api)
        actions_api = DryRunApi(actions_api)

    handle(
        run,
        alert_api=alert_api,
        actions_api=actions_api,
        alert_repo=alert_repo,
        run_repo=run_repo,
        mention=os.environ.get("ALERT_MENTION") or None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
