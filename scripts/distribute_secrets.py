#!/usr/bin/env python3
"""Push JIRA_BASE_URL, JIRA_EMAIL, JIRA_TOKEN as Actions secrets to every repo
listed in config.yaml.

Usage:
    JIRA_TOKEN_FILE=C:/Users/Administrator/Desktop/jira-token.txt \\
        python scripts/distribute_secrets.py

The token is passed to gh via stdin (never command-line) to keep it out of
process listings.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))

OWNER = CONFIG["github"]["owner"]
REPOS = CONFIG["repos"]
REPO_OWNERS: dict = CONFIG.get("repo_owners", {})

JIRA_BASE_URL = CONFIG["jira"]["base_url"]
JIRA_EMAIL = CONFIG["jira"]["email"]


def owner_for(repo: str) -> str:
    return REPO_OWNERS.get(repo, OWNER)


def load_token() -> str:
    inline = os.environ.get("JIRA_TOKEN")
    if inline:
        return inline.strip()
    p = os.environ.get("JIRA_TOKEN_FILE")
    if p and Path(p).exists():
        return Path(p).read_text(encoding="utf-8").strip()
    raise RuntimeError("Set $JIRA_TOKEN or $JIRA_TOKEN_FILE")


def set_secret(repo: str, name: str, value: str) -> None:
    cmd = ["gh", "secret", "set", name, "--repo", f"{owner_for(repo)}/{repo}", "--body", "-"]
    try:
        subprocess.run(cmd, input=value, text=True, check=True, capture_output=True)
        print(f"  {name} OK")
    except subprocess.CalledProcessError as e:
        print(f"  {name} FAILED: {(e.stderr or '').strip()}", file=sys.stderr)


def main() -> int:
    token = load_token()
    errors = 0
    for repo in REPOS:
        print(f"\n[{repo}]")
        try:
            set_secret(repo, "JIRA_BASE_URL", JIRA_BASE_URL)
            set_secret(repo, "JIRA_EMAIL", JIRA_EMAIL)
            set_secret(repo, "JIRA_TOKEN", token)
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR: {e}", file=sys.stderr)
            errors += 1
    print(f"\nDone. {len(REPOS) - errors}/{len(REPOS)} repos updated.")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
