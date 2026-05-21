#!/usr/bin/env python3
"""Create or update GitHub webhooks pointing to hooks.vaultwares.ca/github
for every repo tracked in config.yaml.

Usage:
    VW_GITHUB_WEBHOOK_SECRET=<secret> python scripts/deploy_webhooks.py [--dry-run]

The script reads GH_TOKEN (or GITHUB_TOKEN) for GitHub API auth.
Existing webhooks for the same URL are updated in-place (PATCH) to ensure
events match; repos with no existing hook get a fresh one (POST).
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

WEBHOOK_URL = "https://hooks.vaultwares.ca/github"
WEBHOOK_EVENTS = [
    "push",
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
    "issue_comment",
]

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


def _list_hooks(owner: str, repo: str, token: str) -> list[dict]:
    r = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/hooks",
        headers=_headers(token),
        timeout=30,
    )
    if r.status_code == 404:
        return []
    r.raise_for_status()
    return r.json()


def _create_hook(owner: str, repo: str, token: str, secret: str) -> dict:
    r = requests.post(
        f"https://api.github.com/repos/{owner}/{repo}/hooks",
        headers=_headers(token),
        json={
            "name": "web",
            "active": True,
            "events": WEBHOOK_EVENTS,
            "config": {
                "url": WEBHOOK_URL,
                "content_type": "json",
                "secret": secret,
                "insecure_ssl": "0",
            },
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _update_hook(owner: str, repo: str, hook_id: int, token: str, secret: str) -> dict:
    r = requests.patch(
        f"https://api.github.com/repos/{owner}/{repo}/hooks/{hook_id}",
        headers=_headers(token),
        json={
            "active": True,
            "events": WEBHOOK_EVENTS,
            "config": {
                "url": WEBHOOK_URL,
                "content_type": "json",
                "secret": secret,
                "insecure_ssl": "0",
            },
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def deploy(repo: str, token: str, secret: str) -> str:
    own = owner_for(repo)
    hooks = _list_hooks(own, repo, token)
    existing = next((h for h in hooks if h.get("config", {}).get("url") == WEBHOOK_URL), None)

    if existing:
        current_events = set(existing.get("events") or [])
        needs_update = current_events != set(WEBHOOK_EVENTS)
        if not needs_update:
            return "skip (up-to-date)"
        if DRY_RUN:
            return f"dry-run: would update hook #{existing['id']} events={sorted(WEBHOOK_EVENTS)}"
        _update_hook(own, repo, existing["id"], token, secret)
        return f"updated hook #{existing['id']}"
    else:
        if DRY_RUN:
            return "dry-run: would create hook"
        _create_hook(own, repo, token, secret)
        return "created"


def main() -> int:
    token = _gh_token()
    secret = os.environ.get("VW_GITHUB_WEBHOOK_SECRET", "").strip()
    if not secret:
        print("ERROR: VW_GITHUB_WEBHOOK_SECRET not set", file=sys.stderr)
        return 1

    if DRY_RUN:
        print("--- DRY RUN ---")

    errors = 0
    for repo in REPOS:
        try:
            result = deploy(repo, token, secret)
            print(f"[{repo}] {result}")
        except Exception as e:  # noqa: BLE001
            print(f"[{repo}] ERROR: {e}", file=sys.stderr)
            errors += 1

    print(f"\nDone. {len(REPOS) - errors}/{len(REPOS)} repos OK.")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
