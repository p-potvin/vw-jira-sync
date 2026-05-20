#!/usr/bin/env python3
"""Create one Jira project per tracked GitHub repo.

Each project is a team-managed (simplified) Kanban project.
Already-existing projects are skipped idempotently.

Usage:
    JIRA_TOKEN_FILE=C:/Users/Administrator/Desktop/jira-token.txt \\
        python scripts/create_projects.py

    # dry-run (print payloads, don't create)
    JIRA_TOKEN_FILE=... python scripts/create_projects.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jira_sync import jira_request, jira_session, load_token
from backfill import repo_owner

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"


def load_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_account_id(session) -> str:
    r = jira_request(session, "GET", "/rest/api/3/myself")
    if r.status_code >= 300:
        raise RuntimeError(f"Cannot fetch account ID: {r.status_code} {r.text}")
    return r.json()["accountId"]


def project_exists(session, key: str) -> bool:
    r = jira_request(session, "GET", f"/rest/api/3/project/{key}")
    return r.status_code == 200


def create_project(session, key: str, repo: str, owner: str, lead_id: str) -> dict:
    payload = {
        "key": key,
        "name": repo,
        "projectTypeKey": "software",
        # team-managed simplified kanban
        "projectTemplateKey": "com.pyxis.greenhopper.jira:gh-simplified-kanban-classic",
        "description": f"GitHub sync: {owner}/{repo}",
        "leadAccountId": lead_id,
        "assigneeType": "UNASSIGNED",
    }
    r = jira_request(session, "POST", "/rest/api/3/project", json=payload)
    if r.status_code >= 300:
        raise RuntimeError(f"Create {key} failed: {r.status_code} {r.text}")
    return r.json()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--repo", action="append", help="Only this repo (repeatable)")
    args = ap.parse_args()

    cfg = load_config()
    owner = cfg["github"]["owner"]
    repo_keys: dict = cfg.get("repo_project_keys", {})
    repos = args.repo or cfg["repos"]

    token = load_token()
    session = jira_session(cfg["jira"]["base_url"], cfg["jira"]["email"], token)

    lead_id = get_account_id(session)
    print(f"Lead account: {lead_id}\n")

    created, skipped, errors = [], [], []

    for repo in repos:
        key = repo_keys.get(repo)
        if not key:
            print(f"  WARN: no project key for '{repo}', skipping")
            continue

        if project_exists(session, key):
            print(f"  [{key}] {repo} — already exists, skip")
            skipped.append(key)
            continue

        if args.dry_run:
            print(f"  [DRY-RUN] would create project {key} for {repo}")
            continue

        try:
            result = create_project(session, key, repo, repo_owner(cfg, repo), lead_id)
            print(f"  [{key}] {repo} — created (id={result.get('id')})")
            created.append(key)
            time.sleep(0.5)
        except Exception as e:  # noqa: BLE001
            print(f"  [{key}] {repo} — ERROR: {e}", file=sys.stderr)
            errors.append((key, str(e)))

    print(f"\nDone. created={len(created)} skipped={len(skipped)} errors={len(errors)}")
    if errors:
        for k, e in errors:
            print(f"  ERROR {k}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
