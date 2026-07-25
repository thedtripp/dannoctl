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
