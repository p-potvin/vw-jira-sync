#!/usr/bin/env python3
"""Delete JIRA_BASE_URL, JIRA_EMAIL, JIRA_TOKEN from GitHub Actions secrets
on every repo in config.yaml.

These secrets are no longer needed — the webhook path reads the Jira token
directly from vw-secretsd on the VPS. Keeping them on GitHub infrastructure
is unnecessary exposure.

Usage:
    python scripts/purge_actions_secrets.py [--dry-run]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))

OWNER = CONFIG["github"]["owner"]
REPOS = CONFIG["repos"]
REPO_OWNERS: dict = CONFIG.get("repo_owners", {})

SECRETS_TO_DELETE = ["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_TOKEN"]

DRY_RUN = "--dry-run" in sys.argv


def owner_for(repo: str) -> str:
    return REPO_OWNERS.get(repo, OWNER)


def _gh_token() -> str:
    t = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if not t:
        raise RuntimeError("Set GH_TOKEN or GITHUB_TOKEN")
    return t


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def delete_secret(owner: str, repo: str, name: str, token: str) -> str:
    if DRY_RUN:
        return f"dry-run: would delete {name}"
    r = requests.delete(
        f"https://api.github.com/repos/{owner}/{repo}/actions/secrets/{name}",
        headers=_headers(token),
        timeout=30,
    )
    if r.status_code == 204:
        return f"deleted {name}"
    if r.status_code == 404:
        return f"{name} not found (already gone)"
    r.raise_for_status()
    return f"{name} unknown ({r.status_code})"


def main() -> int:
    token = _gh_token()

    if DRY_RUN:
        print("--- DRY RUN ---")

    errors = 0
    for repo in REPOS:
        own = owner_for(repo)
        results = []
        try:
            for secret in SECRETS_TO_DELETE:
                results.append(delete_secret(own, repo, secret, token))
        except Exception as e:  # noqa: BLE001
            print(f"[{repo}] ERROR: {e}", file=sys.stderr)
            errors += 1
            continue
        print(f"[{repo}] {' | '.join(results)}")

    print(f"\nDone. {len(REPOS) - errors}/{len(REPOS)} repos processed.")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
