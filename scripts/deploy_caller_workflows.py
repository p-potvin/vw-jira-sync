#!/usr/bin/env python3
"""Add `.github/workflows/jira-sync.yml` to every repo in config.yaml.

Two modes:
  --strategy=main : push directly to main (fast)
  --strategy=pr   : push to a `chore/jira-sync` branch and open a PR (default)

Uses the GitHub Contents API via `gh api` -- no local cloning required.
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
OWNER = CONFIG["github"]["owner"]
REPOS = CONFIG["repos"]
REPO_OWNERS: dict = CONFIG.get("repo_owners", {})


def owner_for(repo: str) -> str:
    return REPO_OWNERS.get(repo, OWNER)

WORKFLOW_SRC = ROOT / ".github" / "workflow-templates" / "jira-sync.yml"
WORKFLOW_DEST = ".github/workflows/jira-sync.yml"

COMMIT_MSG = "chore: add Jira sync workflow"
PR_BRANCH = "chore/jira-sync"
PR_TITLE = "Add Jira sync workflow"
PR_BODY = """Adds the one-way GitHub -> Jira sync workflow.

Fires on:
- `pull_request` (opened/edited/closed/reopened/ready_for_review/converted_to_draft)
- `pull_request_review` (submitted)
- `pull_request_review_comment` (created)
- `issue_comment` (created, on PRs)
- `push` to `main` / `master`

Delegates to the reusable workflow at `p-potvin/vw-jira-sync@main`.

Requires three repo-level secrets (already distributed by
`scripts/distribute_secrets.py`):
  - `JIRA_BASE_URL`
  - `JIRA_EMAIL`
  - `JIRA_TOKEN`
"""


def gh(*args: str, input_data: str | None = None) -> subprocess.CompletedProcess:
    cmd = ["gh", *args]
    return subprocess.run(
        cmd, input=input_data, text=True, capture_output=True, check=True
    )


def file_sha(owner: str, repo: str, path: str, ref: str = "main") -> str | None:
    """Return the file's blob SHA on `ref`, or None if it doesn't exist."""
    try:
        out = gh("api", f"/repos/{owner}/{repo}/contents/{path}?ref={ref}")
        return json.loads(out.stdout).get("sha")
    except subprocess.CalledProcessError:
        return None


def put_file(
    owner: str,
    repo: str,
    path: str,
    content: str,
    branch: str,
    message: str,
    sha: str | None = None,
) -> None:
    payload: dict = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha
    gh(
        "api",
        "-X",
        "PUT",
        f"/repos/{owner}/{repo}/contents/{path}",
        "--input",
        "-",
        input_data=json.dumps(payload),
    )


def ensure_branch(owner: str, repo: str, branch: str, base: str = "main") -> None:
    """Create branch from base if it doesn't exist (no-op if it does)."""
    try:
        gh("api", f"/repos/{owner}/{repo}/git/ref/heads/{branch}")
        return  # exists
    except subprocess.CalledProcessError:
        pass
    base_ref = json.loads(
        gh("api", f"/repos/{owner}/{repo}/git/ref/heads/{base}").stdout
    )
    base_sha = base_ref["object"]["sha"]
    gh(
        "api",
        "-X",
        "POST",
        f"/repos/{owner}/{repo}/git/refs",
        "-f",
        f"ref=refs/heads/{branch}",
        "-f",
        f"sha={base_sha}",
    )


def deploy_to_main(owner: str, repo: str, content: str) -> None:
    sha = file_sha(owner, repo, WORKFLOW_DEST, ref="main")
    put_file(owner, repo, WORKFLOW_DEST, content, "main", COMMIT_MSG, sha=sha)
    print("  pushed to main")


def deploy_via_pr(owner: str, repo: str, content: str) -> None:
    ensure_branch(owner, repo, PR_BRANCH, base="main")
    sha = file_sha(owner, repo, WORKFLOW_DEST, ref=PR_BRANCH)
    put_file(owner, repo, WORKFLOW_DEST, content, PR_BRANCH, COMMIT_MSG, sha=sha)
    # Open PR if not already open
    try:
        existing = gh(
            "pr",
            "list",
            "--repo",
            f"{owner}/{repo}",
            "--head",
            PR_BRANCH,
            "--state",
            "open",
            "--json",
            "number",
        )
        prs = json.loads(existing.stdout)
        if prs:
            print(f"  PR #{prs[0]['number']} already open, file updated")
            return
    except subprocess.CalledProcessError:
        pass
    out = gh(
        "pr",
        "create",
        "--repo",
        f"{owner}/{repo}",
        "--base",
        "main",
        "--head",
        PR_BRANCH,
        "--title",
        PR_TITLE,
        "--body",
        PR_BODY,
    )
    print(f"  opened PR: {out.stdout.strip()}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--strategy",
        choices=("main", "pr"),
        default="pr",
        help="main = push directly to main; pr = create branch + open PR (default)",
    )
    ap.add_argument(
        "--repo", action="append", help="Only this repo (repeatable, default all)"
    )
    args = ap.parse_args()

    if not WORKFLOW_SRC.exists():
        print(f"ERROR: template not found at {WORKFLOW_SRC}", file=sys.stderr)
        return 2

    content = WORKFLOW_SRC.read_text(encoding="utf-8")
    repos = args.repo or REPOS
    errors = 0

    for repo in repos:
        owner = owner_for(repo)
        print(f"\n[{owner}/{repo}]")
        try:
            if args.strategy == "main":
                deploy_to_main(owner, repo, content)
            else:
                deploy_via_pr(owner, repo, content)
        except subprocess.CalledProcessError as e:
            err = (e.stderr or e.stdout or "").strip()
            print(f"  FAILED: {err}", file=sys.stderr)
            errors += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR: {e}", file=sys.stderr)
            errors += 1

    print(f"\nDone. {len(repos) - errors}/{len(repos)} succeeded.")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
