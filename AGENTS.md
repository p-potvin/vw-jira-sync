# VaultWares — pre-instructions (repo stub)
This file is intentionally short. It routes work to the company protocol TOC.
Always start at: `C:\Users\Administrator\Desktop\Github Repos\vaultwares-docs\instructions\ROUTER.md`
Execute the ROUTER routine first (always): scan all protocol categories end-to-end, select relevant categories, then open only the selected summaries in category order.
Execute other routines only when relevant (tools/routines). Ledger is always the last step before replying.
Read full notes only when explicitly prompted: `read full notes`
Mandatory ledger (last step before replying): use `C:\Users\Administrator\Desktop\Github Repos\agent-ledger\scripts\record-agent-change.ps1`

---

## vw-jira-sync — key facts for agents

One-way GitHub → Jira sync. Every tracked GitHub repo maps to a dedicated Jira project.
Config lives in `config.yaml`. Scripts are in `scripts/`.

### Critical: GitHub repo renames
Renaming a GitHub repo without updating this repo **will create duplicate Jira issues**.
Always follow the RENAMING protocol before or immediately after any GitHub repo rename.
Full procedure: `vaultwares-docs/docs-content/operations/jira-sync.mdx`
Quick steps: update `config.yaml` (repo_project_keys + repos) → rename `mapping/{old}.json` → `python scripts/backfill.py --repo new-name` → push.

### Adding a new repo
1. Add to `repo_project_keys` and `repos` in `config.yaml`.
2. `python scripts/deploy_caller_workflows.py --repo <name> --strategy main`
3. `python scripts/backfill.py --repo <name>`

### Secrets
Three secrets required per repo: `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_TOKEN`.
Distribute with: `python scripts/distribute_secrets.py`

### Mapping files
`mapping/{repo}.json` is the idempotency checkpoint. Format: `{"prs": {}, "commits": {}}`.
Never delete these — they prevent duplicate Jira issues on re-run.

