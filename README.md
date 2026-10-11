# dannoctl

Public repo that runs bruce-bot's scheduled/heavy workflows on **free public-repo
GitHub Actions minutes**, instead of billed private-repo minutes on `bruce-bot`.

`bruce-bot` keeps thin dispatcher copies of these workflows (`workflow_dispatch`
only, plus the unavoidable `issues:closed` gate checks) as a manual fallback;
the real schedules and work live here. See the comment block at the top of
each workflow file in [.github/workflows/](.github/workflows/) for the
specifics of what moved and why.

## Checking GitHub Actions billing

### Web UI (quickest)

1. Go to [github.com/settings/billing/summary](https://github.com/settings/billing/summary) — shows
   current-cycle spend across Actions, Copilot, LFS, etc., with an "Actions" line item.
2. Go to [github.com/settings/billing/usage](https://github.com/settings/billing/usage) — the itemized
   minute-by-minute breakdown per repo. Filter by product (`Actions`) and date range to see which
   repo is actually burning minutes.
3. Public repos (like this one) are always free/unlimited on Actions minutes.
   Private repos (like `bruce-bot`) draw from your plan's monthly free minutes,
   then bill per minute after that. Watch the `netAmount` / `repositoryName`
   columns on the usage page to catch a private repo creeping back up.

### CLI

```bash
# Current-cycle usage, itemized by product/repo/day (JSON)
gh api /users/<your-username>/settings/billing/usage

# Same, scoped to a specific month
gh api "/users/<your-username>/settings/billing/usage?year=2026&month=7"

# Summarize Actions minutes by repo for a given month
gh api "/users/<your-username>/settings/billing/usage?year=2026&month=7" \
  | python3 -c '
import json, sys
from collections import defaultdict
items = json.load(sys.stdin)["usageItems"]
by_repo = defaultdict(lambda: [0.0, 0.0])
for i in items:
    if i["product"] == "actions" and i["unitType"] == "Minutes":
        by_repo[i["repositoryName"]][0] += i["quantity"]
        by_repo[i["repositoryName"]][1] += i["netAmount"]
for repo, (minutes, net) in sorted(by_repo.items(), key=lambda x: -x[1][1]):
    print(f"{repo:25s} {minutes:8.0f} min   net ${net:.2f}")
'
```

Requires `gh auth login` with a token that has access to your own account
billing (the default `gh auth` scopes cover this).

If a private repo shows nonzero `net` minutes for workflows that are supposed
to be inlined here instead, check that repo's workflow file still has its
`schedule:`/event trigger removed (thin dispatchers should only have
`workflow_dispatch` and, where unavoidable, a cheap gate-check job).

## Failure alerts

[`failure_alerts.yml`](.github/workflows/failure_alerts.yml) runs after every
`daily_report.yml` / `e2e_nightly.yml` run (`workflow_run: completed`). When a
**scheduled** run fails it opens one issue in this repo, `⚠️ <workflow>.yml Failures`,
that @-mentions the repo owner. Each later failure adds a comment to that issue, and the
next successful run (scheduled or manual) closes it. Cancelled runs are ignored.

- **Why this repo and not bruce-bot:** bruce-bot's tracker is written with
  `PRIVATE_REPO_TOKEN`, the owner's own PAT, and GitHub never notifies you about
  your own activity. The built-in `GITHUB_TOKEN` posts as `github-actions[bot]`, so
  its issues do send notifications, but that token can only write to this repo.
- **Public-safe:** the issue holds only the workflow name, run link, failed job names,
  conclusion, and timestamps, all of which are already public. Don't extend it with
  logs or anything read from bruce-bot.
- **No new secrets:** it doesn't use `PRIVATE_REPO_TOKEN` or check out bruce-bot,
  so it still alerts when that token expires.
- Logic lives in [`.github/scripts/failure_alert.py`](.github/scripts/failure_alert.py)
  (stdlib only). To test it:

  ```bash
  python3 -m unittest discover -s .github/scripts -v
  # dry run against a saved workflow_run payload (prints writes instead of sending them):
  python3 .github/scripts/failure_alert.py --dry-run --event-path event.json
  ```
