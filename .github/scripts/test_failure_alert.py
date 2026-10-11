"""Unit tests for failure_alert.py. No network, no tokens.

Run: python3 -m unittest discover -s .github/scripts -v
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import failure_alert as fa  # noqa: E402

REPO = "thedtripp/dannoctl"
WORKFLOWS_DIR = Path(__file__).resolve().parents[1] / "workflows"
TITLE = "⚠️ daily_report.yml Failures"


class FakeApi:
    def __init__(self, issues=None, jobs=None):
        self.issues = issues or []
        self.jobs = jobs or []
        self.calls: list[tuple[str, str, dict | None]] = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and "/actions/runs/" in path:
            return {"jobs": self.jobs}
        if method == "GET" and "/issues?" in path:
            return self.issues
        if method == "POST" and path.endswith("/issues"):
            return {"number": 99}
        return {}

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


def make_run(conclusion="failure", event="schedule", **extra):
    run = {
        "id": 123,
        "name": "Daily dannoctl Pipeline (bruce-bot, inlined)",
        "path": ".github/workflows/daily_report.yml",
        "event": event,
        "conclusion": conclusion,
        "run_number": 297,
        "html_url": f"https://github.com/{REPO}/actions/runs/123",
        "run_started_at": "2026-10-09T13:11:22Z",
        "created_at": "2026-10-09T13:11:22Z",
        "updated_at": "2026-10-09T13:16:19Z",
        "repository": {"full_name": REPO},
    }
    run.update(extra)
    return run


def bot_issue(number=7, title=TITLE):
    return {"number": number, "title": title, "user": {"login": "github-actions[bot]"}, "author_association": "NONE"}


def run_handle(run, api, mention=None):
    with redirect_stdout(io.StringIO()):
        return fa.handle(run, alert_api=api, actions_api=api, alert_repo=REPO, run_repo=REPO, mention=mention)


class ClassifyTests(unittest.TestCase):
    def test_scheduled_failures_alert(self):
        for conclusion in ("failure", "timed_out", "startup_failure"):
            self.assertEqual(fa.classify(make_run(conclusion)), "alert", conclusion)

    def test_manual_failure_ignored(self):
        self.assertEqual(fa.classify(make_run("failure", event="workflow_dispatch")), "ignore")

    def test_any_success_resolves(self):
        self.assertEqual(fa.classify(make_run("success")), "resolve")
        self.assertEqual(fa.classify(make_run("success", event="workflow_dispatch")), "resolve")

    def test_cancelled_and_skipped_ignored(self):
        for conclusion in ("cancelled", "skipped", "neutral", "action_required", None):
            self.assertEqual(fa.classify(make_run(conclusion)), "ignore", conclusion)


class HandleTests(unittest.TestCase):
    def test_first_failure_opens_tracker_with_mention(self):
        api = FakeApi(jobs=[{"name": "fetch", "conclusion": "failure"}, {"name": "report", "conclusion": "skipped"}])
        self.assertEqual(run_handle(make_run(), api, mention="thedtripp"), "opened")
        [(method, path, body)] = api.writes()
        self.assertEqual((method, path), ("POST", f"/repos/{REPO}/issues"))
        self.assertEqual(body["title"], TITLE)
        self.assertTrue(body["body"].startswith("@thedtripp "))
        self.assertIn("`fetch`", body["body"])
        self.assertNotIn("`report`", body["body"])
        self.assertIn("https://github.com/thedtripp/dannoctl/actions/runs/123", body["body"])
        self.assertIn("2026-10-09T13:16:19Z", body["body"])

    def test_repeat_failure_comments_instead_of_opening(self):
        api = FakeApi(issues=[bot_issue(7)], jobs=[{"name": "fetch", "conclusion": "failure"}])
        self.assertEqual(run_handle(make_run(), api, mention="thedtripp"), "commented")
        [(method, path, body)] = api.writes()
        self.assertEqual((method, path), ("POST", f"/repos/{REPO}/issues/7/comments"))
        self.assertNotIn("@thedtripp", body["body"])  # already subscribed via the opening mention

    def test_success_closes_open_tracker(self):
        api = FakeApi(issues=[bot_issue(7)])
        self.assertEqual(run_handle(make_run("success"), api), "closed")
        writes = api.writes()
        self.assertEqual([w[:2] for w in writes], [("POST", f"/repos/{REPO}/issues/7/comments"), ("PATCH", f"/repos/{REPO}/issues/7")])
        self.assertEqual(writes[1][2]["state"], "closed")

    def test_success_without_tracker_writes_nothing(self):
        api = FakeApi()
        self.assertEqual(run_handle(make_run("success"), api), "noop")
        self.assertEqual(api.writes(), [])

    def test_ignored_run_makes_no_calls(self):
        api = FakeApi(issues=[bot_issue(7)])
        self.assertEqual(run_handle(make_run("cancelled"), api), "ignored")
        self.assertEqual(api.calls, [])

    def test_untrusted_same_title_issue_is_not_reused_or_closed(self):
        stranger = {"number": 5, "title": TITLE, "user": {"login": "rando"}, "author_association": "NONE"}
        api = FakeApi(issues=[stranger])
        self.assertEqual(run_handle(make_run("success"), api), "noop")
        self.assertEqual(run_handle(make_run(), api), "opened")

    def test_owner_authored_tracker_is_reused(self):
        owner = {"number": 3, "title": TITLE, "user": {"login": "thedtripp"}, "author_association": "OWNER"}
        api = FakeApi(issues=[owner])
        self.assertEqual(run_handle(make_run(), api), "commented")

    def test_pull_requests_and_other_titles_ignored(self):
        pr = dict(bot_issue(4), pull_request={})
        other = bot_issue(6, title="⚠️ e2e_nightly.yml Failures")
        api = FakeApi(issues=[pr, other])
        self.assertEqual(run_handle(make_run(), api), "opened")

    def test_trackers_are_per_workflow(self):
        api = FakeApi(issues=[bot_issue(7)])
        run = make_run(path=".github/workflows/e2e_nightly.yml", name="Nightly E2E Report Test (bruce-bot, inlined)")
        self.assertEqual(run_handle(run, api), "opened")
        self.assertEqual(api.writes()[0][2]["title"], "⚠️ e2e_nightly.yml Failures")

    def test_startup_failure_with_no_jobs(self):
        api = FakeApi(jobs=[])
        run_handle(make_run("startup_failure"), api)
        self.assertIn("none reported", api.writes()[0][2]["body"])

    def test_body_contains_only_public_fields(self):
        # Extra fields on the payload (commit message etc.) must never leak.
        run = make_run(head_commit={"message": "SECRET commit subject"}, display_title="SECRET title")
        api = FakeApi(jobs=[{"name": "report", "conclusion": "failure"}])
        run_handle(run, api)
        body = api.writes()[0][2]["body"]
        self.assertNotIn("SECRET", body)


class MainTests(unittest.TestCase):
    def _event_file(self, run):
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"workflow_run": run}, fh)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        return fh.name

    def test_dry_run_without_token_prints_and_sends_nothing(self):
        path = self._event_file(make_run())
        env = {k: v for k, v in os.environ.items() if k not in ("ALERT_TOKEN", "ACTIONS_TOKEN", "ALERT_REPO")}
        env["GITHUB_REPOSITORY"] = REPO
        out = io.StringIO()
        old = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(env)
            with redirect_stdout(out):
                rc = fa.main(["--dry-run", "--event-path", path])
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assertEqual(rc, 0)
        self.assertIn(f"[dry run] POST /repos/{REPO}/issues", out.getvalue())

    def test_missing_token_without_dry_run_fails(self):
        path = self._event_file(make_run())
        old = os.environ.copy()
        try:
            os.environ.pop("ALERT_TOKEN", None)
            os.environ.pop("DRY_RUN", None)
            with redirect_stdout(io.StringIO()), unittest.mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(fa.main(["--event-path", path]), 2)
        finally:
            os.environ.clear()
            os.environ.update(old)


class WorkflowWiringTests(unittest.TestCase):
    """failure_alerts.yml's `workflows:` list matches by display name, so a
    rename of a watched workflow would silently disable alerting."""

    def test_watched_names_match_workflow_files(self):
        alert_yml = (WORKFLOWS_DIR / "failure_alerts.yml").read_text(encoding="utf-8")
        watched = re.findall(r'^\s+- "(.+)"$', alert_yml.split("types:")[0], re.M)
        actual = {}
        for name in ("daily_report.yml", "e2e_nightly.yml"):
            match = re.search(r"^name: (.+)$", (WORKFLOWS_DIR / name).read_text(encoding="utf-8"), re.M)
            actual[name] = match.group(1).strip()
        self.assertEqual(sorted(watched), sorted(actual.values()))

    def test_job_filter_matches_script_conclusions(self):
        alert_yml = (WORKFLOWS_DIR / "failure_alerts.yml").read_text(encoding="utf-8")
        listed = set(json.loads(re.search(r"fromJSON\('(\[.*?\])'\)", alert_yml).group(1)))
        self.assertEqual(listed, fa.FAILURE_CONCLUSIONS)


if __name__ == "__main__":
    unittest.main()
