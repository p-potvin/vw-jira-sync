# vw-jira-sync

One-way mirror of GitHub activity (`p-potvin/*`) into the **Jira VW** project on
`vaultwares.atlassian.net`.

## Pieces

| File | Role |
|---|---|
| `config.yaml` | Tracked repos, project IDs, status mapping |
| `scripts/jira_sync.py` | Shared library: Jira REST + ADF + gh API |
| `scripts/backfill.py` | One-shot historical import (Epics + Tasks + comments) |
| `mapping/<repo>.json` | Per-repo idempotency mapping: `gh# -> JIRA-KEY` |
| `.github/workflows/sync.yml` | Reusable workflow called from each tracked repo (TBD) |

## Backfill

```powershell
$env:JIRA_TOKEN_FILE = "C:\Users\Administrator\Desktop\jira-token.txt"

# preview one repo without writing to Jira
python scripts\backfill.py --dry-run --repo nemo-playground

# full run, all repos in config.yaml
python scripts\backfill.py
```

The script is resumable. If it crashes mid-run, re-run the same command — items
already in `mapping/<repo>.json` are skipped.

## Status mapping

| GitHub PR state | Jira status |
|---|---|
| open (not draft) | PR Created |
| draft | To Do |
| merged | Done |
| closed (not merged) | Done (+ label `pr-closed`) |

## Live sync (post-backfill)

After backfill completes, install Atlassian's "GitHub for Jira" Marketplace app
for live PR/commit/branch panels via smart commits (`VW-123` in commit/PR
title). Then deploy the reusable workflow in `.github/workflows/sync.yml` and
add a 10-line caller workflow to each tracked repo for issue-level mirroring.
