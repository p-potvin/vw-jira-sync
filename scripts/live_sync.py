#!/usr/bin/env python3
"""Live one-way GitHub -> Jira sync. Runs inside GitHub Actions.

Reads the webhook event from $GITHUB_EVENT_PATH (file) and $GITHUB_EVENT_NAME
(env). Each repo has its own dedicated Jira project (repo_project_keys in
config.yaml). Tasks are created directly in the project — no Epic wrapper.

Event coverage:
  pull_request               -> upsert Task, transition status
  pull_request_review        -> add Jira comment (review summary)
  pull_request_review_comment-> add Jira comment (inline review comment)
  issue_comment (on a PR)    -> add Jira comment
  push (default branch)      -> for non-merge commits not associated with a
                                PR, add a comment on the repo's Direct Commits task.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jira_sync import (  # noqa: E402
    gh_api,
    gh_pr_commits,
    gh_repo_info,
    jira_add_comment,
    jira_create_issue,
    jira_find_by_label,
    jira_session,
    jira_transition,
    jira_update_issue,
    load_token,
)
from backfill import (  # noqa: E402
    build_commits_issue_payload,
    build_direct_commit_comment,
    build_pr_comment,
    build_review_summary,
    build_task_payload,
    pr_state,
    repo_project_key,
)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"


def load_config() -> Dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_event() -> Dict[str, Any]:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path or not Path(path).exists():
        raise RuntimeError("GITHUB_EVENT_PATH not set or missing")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_owner_repo(event: Dict[str, Any]) -> tuple[str, str]:
    repo_full = event["repository"]["full_name"]
    owner, name = repo_full.split("/", 1)
    return owner, name


def normalize_repo(cfg: Dict[str, Any], repo: str) -> str:
    """Map renamed repos back to their backfill label name."""
    return cfg.get("repo_aliases", {}).get(repo, repo)


def label_pr_task(owner: str, repo: str, pr_num: int) -> str:
    return f"gh-pr-{owner}-{repo}-{pr_num}"


def label_commits_task(owner: str, repo: str) -> str:
    return f"gh-repo-{owner}-{repo}"


def find_task_for_pr(
    session, cfg: Dict[str, Any], owner: str, repo: str, pr_num: int
) -> Optional[str]:
    proj = repo_project_key(cfg, repo)
    return jira_find_by_label(session, proj, label_pr_task(owner, repo, pr_num))


def ensure_commits_task(session, cfg: Dict[str, Any], owner: str, repo: str) -> str:
    """Find or create the 'Direct Commits' task for this repo's project."""
    proj = repo_project_key(cfg, repo)
    label = label_commits_task(owner, repo)
    key = jira_find_by_label(session, proj, label)
    if key:
        return key
    info = gh_repo_info(owner, repo)
    result = jira_create_issue(session, build_commits_issue_payload(cfg, repo, info))
    return result["key"]


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

def handle_pull_request(session, cfg, event):
    pr = event["pull_request"]
    owner, gh_repo = get_owner_repo(event)
    repo = normalize_repo(cfg, gh_repo)
    num = pr["number"]
    action = event.get("action", "")
    print(f"pull_request[{action}] {owner}/{repo}#{num}")

    task_key = find_task_for_pr(session, cfg, owner, repo, num)
    commits = gh_pr_commits(owner, repo, num)
    payload = build_task_payload(cfg, repo, pr, commits)
    fields = payload["fields"]

    if task_key:
        update_fields = {
            k: v for k, v in fields.items() if k not in ("parent", "project", "issuetype")
        }
        jira_update_issue(session, task_key, update_fields)
        print(f"  updated {task_key}")
    else:
        result = jira_create_issue(session, payload)
        task_key = result["key"]
        print(f"  created {task_key}")

    state = pr_state(pr)
    target = cfg["status_map"].get(state)
    if target:
        jira_transition(session, task_key, target)
        print(f"  -> {target}")


def handle_pull_request_review(session, cfg, event):
    review = event["review"]
    pr = event["pull_request"]
    owner, gh_repo = get_owner_repo(event)
    repo = normalize_repo(cfg, gh_repo)
    num = pr["number"]
    print(f"pull_request_review {owner}/{repo}#{num} state={review.get('state')}")

    task_key = find_task_for_pr(session, cfg, owner, repo, num)
    if not task_key:
        result = jira_create_issue(
            session, build_task_payload(cfg, repo, pr, gh_pr_commits(owner, repo, num))
        )
        task_key = result["key"]
        print(f"  created Task {task_key} on demand")

    jira_add_comment(session, task_key, build_review_summary(review))
    print(f"  posted review summary to {task_key}")


def handle_pull_request_review_comment(session, cfg, event):
    comment = event["comment"]
    pr = event["pull_request"]
    owner, gh_repo = get_owner_repo(event)
    repo = normalize_repo(cfg, gh_repo)
    num = pr["number"]
    print(f"pull_request_review_comment {owner}/{repo}#{num}")

    task_key = find_task_for_pr(session, cfg, owner, repo, num)
    if not task_key:
        result = jira_create_issue(
            session, build_task_payload(cfg, repo, pr, gh_pr_commits(owner, repo, num))
        )
        task_key = result["key"]
        print(f"  created Task {task_key} on demand")

    jira_add_comment(session, task_key, build_pr_comment(comment, "review_inline"))
    print(f"  posted inline comment to {task_key}")


def handle_issue_comment(session, cfg, event):
    issue = event["issue"]
    if "pull_request" not in issue:
        print("issue_comment on non-PR -- ignored")
        return
    comment = event["comment"]
    owner, gh_repo = get_owner_repo(event)
    repo = normalize_repo(cfg, gh_repo)
    num = issue["number"]
    print(f"issue_comment on PR {owner}/{repo}#{num}")

    task_key = find_task_for_pr(session, cfg, owner, repo, num)
    if not task_key:
        pr = gh_api(f"repos/{owner}/{repo}/pulls/{num}")
        result = jira_create_issue(
            session, build_task_payload(cfg, repo, pr, gh_pr_commits(owner, repo, num))
        )
        task_key = result["key"]
        print(f"  created Task {task_key} on demand")

    jira_add_comment(session, task_key, build_pr_comment(comment, "issue_comment"))
    print(f"  posted comment to {task_key}")


def handle_push(session, cfg, event):
    owner, gh_repo = get_owner_repo(event)
    repo = normalize_repo(cfg, gh_repo)
    ref = event.get("ref", "")
    default_branch = event["repository"].get("default_branch", "main")
    if not ref.endswith(f"/{default_branch}"):
        print(f"push on {ref} -- not default branch, ignored")
        return

    commits = event.get("commits", [])
    print(f"push {owner}/{repo} {len(commits)} commit(s) on {default_branch}")
    if not commits:
        return

    commits_task_key = ensure_commits_task(session, cfg, owner, repo)

    for c in commits:
        sha = c["id"]
        full = gh_api(f"repos/{owner}/{repo}/commits/{sha}")
        if len(full.get("parents") or []) > 1:
            print(f"  skip merge {sha[:7]}")
            continue
        pulls = gh_api(f"repos/{owner}/{repo}/commits/{sha}/pulls") or []
        target_key = commits_task_key
        if pulls:
            pr_num = pulls[0]["number"]
            t = find_task_for_pr(session, cfg, owner, repo, pr_num)
            if t:
                target_key = t
        jira_add_comment(session, target_key, build_direct_commit_comment(full))
        print(f"  commit {sha[:7]} -> {target_key}")


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

HANDLERS = {
    "pull_request": handle_pull_request,
    "pull_request_review": handle_pull_request_review,
    "pull_request_review_comment": handle_pull_request_review_comment,
    "issue_comment": handle_issue_comment,
    "push": handle_push,
}


def main() -> int:
    name = os.environ.get("GITHUB_EVENT_NAME", "")
    if name not in HANDLERS:
        print(f"event '{name}' has no handler -- exiting cleanly")
        return 0

    cfg = load_config()
    event = load_event()
    token = load_token()
    session = jira_session(cfg["jira"]["base_url"], cfg["jira"]["email"], token)

    try:
        HANDLERS[name](session, cfg, event)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR handling {name}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
