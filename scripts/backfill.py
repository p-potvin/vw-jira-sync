#!/usr/bin/env python3
"""One-shot backfill of p-potvin GitHub history into Jira project VW.

Per repo:
  * Creates 1 Epic.
  * Creates 1 Task per PR (any state), linked to the Epic as parent.
  * Adds Jira comments for PR issue comments, review comments, and review bodies.
  * Adds Jira comments on the Epic for each default-branch commit not in any PR.

Resumable: each repo writes `mapping/<repo>.json` after every issue/comment.
Re-running skips items already present in the mapping file.

Usage:
    # dry-run preview against a single repo:
    JIRA_TOKEN_FILE=C:/Users/Administrator/Desktop/jira-token.txt \\
        python scripts/backfill.py --dry-run --repo nemo-playground

    # full execution against all repos in config.yaml:
    JIRA_TOKEN_FILE=C:/Users/Administrator/Desktop/jira-token.txt \\
        python scripts/backfill.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jira_sync import (  # noqa: E402
    adf_bullet_list,
    adf_code_block,
    adf_doc,
    adf_heading,
    adf_paragraph,
    adf_paragraphs,
    gh_pr_commits,
    gh_pr_issue_comments,
    gh_pr_list,
    gh_pr_review_comments,
    gh_pr_reviews,
    gh_repo_commits,
    gh_repo_info,
    jira_add_comment,
    jira_create_issue,
    jira_session,
    jira_transition,
    load_token,
    truncate_text,
)

ROOT = Path(__file__).resolve().parent.parent
MAPPING_DIR = ROOT / "mapping"
CONFIG_PATH = ROOT / "config.yaml"


# ---------------------------------------------------------------------------
# config + mapping
# ---------------------------------------------------------------------------

def load_config() -> Dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_mapping(repo: str) -> Dict[str, Any]:
    p = MAPPING_DIR / f"{repo}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"epic": None, "prs": {}, "commits": {}}


def save_mapping(repo: str, mapping: Dict[str, Any]) -> None:
    MAPPING_DIR.mkdir(parents=True, exist_ok=True)
    p = MAPPING_DIR / f"{repo}.json"
    p.write_text(
        json.dumps(mapping, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------

def pr_state(pr: Dict[str, Any]) -> str:
    if pr.get("merged_at"):
        return "merged"
    if pr.get("draft"):
        return "draft"
    return pr.get("state", "closed")


def build_epic_payload(cfg: Dict[str, Any], repo: str, info: Dict[str, Any]) -> Dict[str, Any]:
    desc = adf_doc(
        adf_heading("GitHub repository", 2),
        adf_paragraph(info.get("description") or "(no description)"),
        adf_heading("Metadata", 3),
        adf_bullet_list(
            [
                f"URL: {info['html_url']}",
                f"Default branch: {info.get('default_branch', 'main')}",
                f"Visibility: {info.get('visibility', 'public')}",
                f"Created: {info.get('created_at', '')}",
                f"Last push: {info.get('pushed_at', '')}",
            ]
        ),
    )
    return {
        "fields": {
            "project": {"key": cfg["jira"]["project_key"]},
            "issuetype": {"id": cfg["jira"]["epic_issuetype_id"]},
            "summary": repo,
            "description": desc,
            "labels": [f"gh-repo-{cfg['github']['owner']}-{repo}"],
        }
    }


def build_task_payload(
    cfg: Dict[str, Any],
    repo: str,
    pr: Dict[str, Any],
    epic_key: str,
    commits: List[Dict[str, Any]],
) -> Dict[str, Any]:
    num = pr["number"]
    state = pr_state(pr)

    nodes: List[Dict[str, Any]] = [adf_heading("Pull request description", 2)]
    for p in adf_paragraphs(truncate_text(pr.get("body") or "(no description)", 20000)):
        nodes.append(p)

    nodes.append(adf_heading("Metadata", 3))
    nodes.append(
        adf_bullet_list(
            [
                f"Author: @{pr['user']['login']}",
                f"State: {state}",
                f"URL: {pr['html_url']}",
                f"Base: {pr['base']['ref']} <- Head: {pr['head']['ref']}",
                f"Created: {pr['created_at']}",
                f"Updated: {pr['updated_at']}",
                f"Merged: {pr.get('merged_at') or '(not merged)'}",
                f"Closed: {pr.get('closed_at') or '(open)'}",
            ]
        )
    )

    if commits:
        nodes.append(adf_heading("Commits", 3))
        lines = [
            f"{c['sha'][:7]}  {c['commit']['message'].splitlines()[0][:140]}"
            for c in commits
        ]
        nodes.append(adf_code_block(truncate_text("\n".join(lines), 8000)))

    summary = f"[{repo}#{num}] {pr['title']}"[:255]
    return {
        "fields": {
            "project": {"key": cfg["jira"]["project_key"]},
            "issuetype": {"id": cfg["jira"]["task_issuetype_id"]},
            "summary": summary,
            "description": adf_doc(*nodes),
            "parent": {"key": epic_key},
            "labels": [
                f"gh-pr-{cfg['github']['owner']}-{repo}-{num}",
                f"gh-repo-{repo}",
                f"pr-{state}",
            ],
        }
    }


def build_pr_comment(c: Dict[str, Any], kind: str) -> Dict[str, Any]:
    user = (c.get("user") or {}).get("login", "?")
    created = c.get("created_at", "")
    body = c.get("body") or "(empty)"
    header = f"@{user} ({created}) — GitHub {kind}"
    if kind == "review_inline" and c.get("path"):
        line = c.get("line") or c.get("original_line") or "?"
        header += f" on {c['path']}:{line}"
    nodes: List[Dict[str, Any]] = [adf_paragraph(header)]
    for p in adf_paragraphs(truncate_text(body, 15000)):
        nodes.append(p)
    return adf_doc(*nodes)


def build_review_summary(r: Dict[str, Any]) -> Dict[str, Any]:
    user = (r.get("user") or {}).get("login", "?")
    submitted = r.get("submitted_at", "")
    state = r.get("state", "")
    body = r.get("body") or ""
    header = f"@{user} ({submitted}) — GitHub review: {state}"
    nodes: List[Dict[str, Any]] = [adf_paragraph(header)]
    if body:
        for p in adf_paragraphs(truncate_text(body, 15000)):
            nodes.append(p)
    return adf_doc(*nodes)


def build_direct_commit_comment(c: Dict[str, Any]) -> Dict[str, Any]:
    sha = c["sha"]
    msg = c["commit"]["message"]
    author = (c["commit"]["author"] or {}).get("name", "?")
    date = (c["commit"]["author"] or {}).get("date", "")
    url = c.get("html_url", "")
    return adf_doc(
        adf_paragraph(f"Direct commit {sha[:10]} by {author} ({date})"),
        adf_paragraph(url),
        adf_code_block(truncate_text(msg, 5000)),
    )


# ---------------------------------------------------------------------------
# Backfill orchestration
# ---------------------------------------------------------------------------

def backfill_repo(session, cfg: Dict[str, Any], repo: str, dry_run: bool) -> Dict[str, Any]:
    print(f"\n=== {repo} ===")
    mapping = load_mapping(repo)
    owner = cfg["github"]["owner"]
    delay = float(cfg.get("write_delay", 0.15))

    # -- Epic --
    if mapping["epic"]:
        print(f"  epic: {mapping['epic']} (already created, skip)")
    else:
        info = gh_repo_info(owner, repo)
        payload = build_epic_payload(cfg, repo, info)
        if dry_run:
            print("  [DRY-RUN] EPIC payload (truncated to 2000 chars):")
            print(json.dumps(payload, indent=2)[:2000])
            mapping["epic"] = "VW-DRYEPIC"
        else:
            result = jira_create_issue(session, payload)
            mapping["epic"] = result["key"]
            print(f"  created Epic {result['key']}")
            time.sleep(delay)
        if not dry_run:
            save_mapping(repo, mapping)

    epic_key = mapping["epic"]

    # -- PRs -> Tasks --
    prs = gh_pr_list(owner, repo)
    print(f"  {len(prs)} PR(s)")

    for pr in prs:
        num = pr["number"]
        if str(num) in mapping["prs"]:
            print(f"  PR #{num} -> {mapping['prs'][str(num)]} (skip)")
            continue

        commits = gh_pr_commits(owner, repo, num)
        payload = build_task_payload(cfg, repo, pr, epic_key, commits)
        state = pr_state(pr)

        if dry_run:
            print(f"\n  [DRY-RUN] PR #{num} ({state}) -- TASK payload (truncated to 3000):")
            print(json.dumps(payload, indent=2)[:3000])
            task_key = f"VW-DRYPR{num}"
        else:
            result = jira_create_issue(session, payload)
            task_key = result["key"]
            print(f"  PR #{num} ({state}) -> Task {task_key}")
            time.sleep(delay)
        mapping["prs"][str(num)] = task_key

        issue_comments = gh_pr_issue_comments(owner, repo, num)
        review_comments = gh_pr_review_comments(owner, repo, num)
        reviews = gh_pr_reviews(owner, repo, num)
        total = (
            len(issue_comments)
            + len(review_comments)
            + sum(1 for r in reviews if r.get("body"))
        )

        if dry_run:
            print(f"    [DRY-RUN] would post {total} Jira comment(s) to {task_key}")
            if issue_comments:
                print("    [DRY-RUN] sample issue_comment ADF:")
                print(
                    json.dumps(build_pr_comment(issue_comments[0], "issue_comment"), indent=2)[
                        :1500
                    ]
                )
            elif review_comments:
                print("    [DRY-RUN] sample review_inline ADF:")
                print(
                    json.dumps(
                        build_pr_comment(review_comments[0], "review_inline"), indent=2
                    )[:1500]
                )
        else:
            for c in issue_comments:
                jira_add_comment(session, task_key, build_pr_comment(c, "issue_comment"))
                time.sleep(delay)
            for c in review_comments:
                jira_add_comment(session, task_key, build_pr_comment(c, "review_inline"))
                time.sleep(delay)
            for r in reviews:
                if r.get("body") or r.get("state") in ("APPROVED", "CHANGES_REQUESTED"):
                    jira_add_comment(session, task_key, build_review_summary(r))
                    time.sleep(delay)
            if total:
                print(f"    posted {total} comment(s)")

        # status transition based on PR state
        target = cfg["status_map"].get(state)
        if target:
            if dry_run:
                print(f"    [DRY-RUN] would transition {task_key} -> '{target}'")
            else:
                jira_transition(session, task_key, target)
                time.sleep(delay)

        if not dry_run:
            save_mapping(repo, mapping)

    # -- Direct commits (default branch, not in any PR) -> Epic comments --
    pr_shas = set()
    for num_str in mapping["prs"]:
        try:
            for c in gh_pr_commits(owner, repo, int(num_str)):
                pr_shas.add(c["sha"])
        except Exception as e:  # noqa: BLE001
            print(f"  WARN: pr_commits for #{num_str} failed: {e}", file=sys.stderr)

    info = gh_repo_info(owner, repo)
    default_branch = info.get("default_branch", "main")
    all_commits = gh_repo_commits(owner, repo, default_branch)
    # Skip merge commits (parents > 1) -- they are PR-merge artifacts, not
    # true direct-to-main commits.
    direct = [
        c
        for c in all_commits
        if c["sha"] not in pr_shas and len(c.get("parents") or []) <= 1
    ]
    skipped_merge = len([c for c in all_commits if c["sha"] not in pr_shas]) - len(direct)
    print(
        f"  {len(direct)} direct commit(s) (not in any PR); "
        f"skipped {skipped_merge} PR-merge commit(s)"
    )

    if dry_run and direct:
        print("  [DRY-RUN] sample direct-commit Epic comment ADF:")
        print(json.dumps(build_direct_commit_comment(direct[0]), indent=2)[:1500])
        for c in direct:
            mapping["commits"][c["sha"]] = epic_key
    else:
        for c in direct:
            sha = c["sha"]
            if sha in mapping["commits"]:
                continue
            jira_add_comment(session, epic_key, build_direct_commit_comment(c))
            mapping["commits"][sha] = epic_key
            time.sleep(delay)
        if direct:
            print(f"  posted {len(direct)} direct-commit comment(s) to {epic_key}")

    if not dry_run:
        save_mapping(repo, mapping)

    return {
        "repo": repo,
        "epic": mapping["epic"],
        "prs": len(mapping["prs"]),
        "direct_commits": len(mapping["commits"]),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Print payloads, don't write to Jira")
    ap.add_argument("--repo", action="append", help="Only this repo (repeatable)")
    args = ap.parse_args()

    cfg = load_config()
    repos = args.repo or cfg["repos"]

    session = None
    if not args.dry_run:
        token = load_token()
        session = jira_session(cfg["jira"]["base_url"], cfg["jira"]["email"], token)

    summary = []
    for repo in repos:
        try:
            summary.append(backfill_repo(session, cfg, repo, dry_run=args.dry_run))
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR on {repo}: {e}", file=sys.stderr)
            summary.append({"repo": repo, "error": str(e)})

    print("\n\n=== SUMMARY ===")
    for s in summary:
        print(json.dumps(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
