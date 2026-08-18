# vw-jira-sync Tasks

## Completed Work (2026-05-30)

### [x] Webhook Dependabot Alerts Integration
<!-- COMPLETED: 2026-05-30 10:48 -->
<!-- ACTOR: Claude (claude-opus-4-6) -->
Updated all 41 GitHub repo webhooks to subscribe to `dependabot_alert` events using `deploy_webhooks.py` in `--events-only` mode. Added `--events-only` flag and delay control to the script. Pushed to branch `vw-codex-dependabot-alerts`.

**Key Files:**
- `scripts/deploy_webhooks.py` — added `--events-only` flag and delay control
- `live_sync.py` — updated to handle `dependabot_alert` events
- PR: `p-potvin/vw-jira-sync#3` — pending review

## Planned Work

### [ ] Review and Merge Dependabot Alerts PR
<!-- ESTIMATE: 15min -->
Review PR `p-potvin/vw-jira-sync#3` and merge webhook updates to main branch.

### [ ] Test Dependabot Alert → Jira Sync
<!-- ESTIMATE: 30min -->
Verify end-to-end flow: GitHub Dependabot alert → webhook event → Jira issue creation.
