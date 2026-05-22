#!/usr/bin/env python3
"""Delete .github/workflows/jira-sync.yml from every repo in config.yaml.

The caller workflow is no longer needed — the webhook path on the VPS
handles all Jira sync events directly. These files only generate ghost
QUEUED jobs on every push.

Usage:
    python scripts/remove_caller_workflows.py [--dry-run]
"""

from __future__ import annotations

import json
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

WORKFLOW_PATH = ".github/workflows/jira-sync.yml"
COMMIT_MSG = "chore: remove Jira sync caller workflow (superseded by webhook)"

DRY_RUN = "--dry-run" in sys.argv


def owner_for(repo: str) -> str:
    return REPO_OWNERS.get(repo, OWNER)


def _gh_token() -> str:
    t = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if not t:
        raise RuntimeError("Set GH_TOKEN or GITHUB_TOKEN")
    return t


def gh(*args: str, input_data: str | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ}
    return subprocess.run(
        ["gh", *args], input=input_data, text=True, capture_output=True,
        check=True, env=env,
    )


def get_file_sha(owner: str, repo: str) -> str | None:
    try:
        out = gh("api", f"/repos/{owner}/{repo}/contents/{WORKFLOW_PATH}")
        return json.loads(out.stdout).get("sha")
    except subprocess.CalledProcessError:
        return None


def delete_file(owner: str, repo: str, sha: str) -> None:
    payload = json.dumps({"message": COMMIT_MSG, "sha": sha})
    gh(
        "api", "-X", "DELETE",
        f"/repos/{owner}/{repo}/contents/{WORKFLOW_PATH}",
        "--input", "-",
        input_data=payload,
    )


def main() -> int:
    _gh_token()  # validate early

    if DRY_RUN:
        print("--- DRY RUN ---")

    errors = 0
    skipped = 0
    removed = 0

    for repo in REPOS:
        own = owner_for(repo)
        try:
            sha = get_file_sha(own, repo)
            if sha is None:
                print(f"[{repo}] skip (not found)")
                skipped += 1
                continue
            if DRY_RUN:
                print(f"[{repo}] dry-run: would delete (sha={sha[:8]})")
                removed += 1
                continue
            delete_file(own, repo, sha)
            print(f"[{repo}] deleted")
            removed += 1
        except subprocess.CalledProcessError as e:
            err = (e.stderr or e.stdout or "").strip()[:120]
            print(f"[{repo}] ERROR: {err}", file=sys.stderr)
            errors += 1
        except Exception as e:  # noqa: BLE001
            print(f"[{repo}] ERROR: {e}", file=sys.stderr)
            errors += 1

    print(f"\nDone. {removed} removed, {skipped} already absent, {errors} errors.")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
