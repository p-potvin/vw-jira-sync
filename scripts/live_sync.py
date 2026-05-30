#!/usr/bin/env python3
"""Live one-way GitHub -> Jira sync.

Reads the webhook event from $GITHUB_EVENT_PATH (file) and $GITHUB_EVENT_NAME
(env). This can run:
  - inside GitHub Actions (event file provided by Actions), or
  - on a server-side GitHub webhook receiver (event body written to a file).

Each repo has its own dedicated Jira project (repo_project_keys in config.yaml).
Tasks are created directly in the project — no Epic wrapper.

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
    adf_bullet_list,
    adf_code_block,
    adf_doc,
    adf_heading,
    adf_paragraphs,
    gh_api,
    gh_pr_commits,
    jira_add_comment,
    jira_create_issue,
    jira_find_by_label,
    jira_session,
    jira_transition,
    jira_update_issue,
    load_token,
    truncate_text,
)
from backfill import (  # noqa: E402
    build_commit_task_payload,
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
    """No-op: repo_aliases is deprecated. Labels now always use the current
    GitHub repo name. Kept as a pass-through so callers don't need changing."""
    return repo


def label_pr_task(owner: str, repo: str, pr_num: int) -> str:
    return f"gh-pr-{owner}-{repo}-{pr_num}"


def label_dependabot_alert(owner: str, repo: str, alert_id: str | int) -> str:
    return f"gh-dependabot-alert-{owner}-{repo}-{alert_id}"



def find_task_for_pr(
    session, cfg: Dict[str, Any], owner: str, repo: str, pr_num: int
) -> Optional[str]:
    proj = repo_project_key(cfg, repo)
    return jira_find_by_label(session, proj, label_pr_task(owner, repo, pr_num))


def find_commit_task(session, cfg: Dict[str, Any], owner: str, repo: str, sha: str) -> Optional[str]:
    """Return existing Task for this commit SHA, or None."""
    proj = repo_project_key(cfg, repo)
    return jira_find_by_label(session, proj, f"gh-commit-{owner}-{repo}-{sha[:12]}")


def find_dependabot_alert_task(
    session, cfg: Dict[str, Any], owner: str, repo: str, alert_id: str | int
) -> Optional[str]:
    proj = repo_project_key(cfg, repo)
    return jira_find_by_label(session, proj, label_dependabot_alert(owner, repo, alert_id))


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

    for c in commits:
        sha = c["id"]
        full = gh_api(f"repos/{owner}/{repo}/commits/{sha}")
        if len(full.get("parents") or []) > 1:
            print(f"  skip merge {sha[:7]}")
            continue
        # If commit belongs to a PR, add a comment on that Task instead
        pulls = gh_api(f"repos/{owner}/{repo}/commits/{sha}/pulls") or []
        if pulls:
            pr_num = pulls[0]["number"]
            t = find_task_for_pr(session, cfg, owner, repo, pr_num)
            if t:
                from backfill import build_direct_commit_comment  # noqa: PLC0415
                jira_add_comment(session, t, build_direct_commit_comment(full))
                print(f"  commit {sha[:7]} -> comment on {t}")
                continue
        # Direct commit — create its own Task (idempotent)
        existing = find_commit_task(session, cfg, owner, repo, sha)
        if existing:
            print(f"  commit {sha[:7]} already has Task {existing}, skip")
            continue
        result = jira_create_issue(session, build_commit_task_payload(cfg, repo, full))
        print(f"  commit {sha[:7]} -> Task {result['key']}")


def _first_nonempty(*vals: Any) -> Any:
    for v in vals:
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        return v
    return None


def _dependabot_alert_id(alert: Dict[str, Any]) -> str | int | None:
    # GitHub payloads have used both `number` and `id` across APIs.
    return _first_nonempty(alert.get("number"), alert.get("id"))


def build_dependabot_alert_task_payload(
    cfg: Dict[str, Any], owner: str, repo: str, alert: Dict[str, Any]
) -> Dict[str, Any]:
    project_key = repo_project_key(cfg, repo)
    alert_id = _dependabot_alert_id(alert)

    dep = alert.get("dependency") or {}
    pkg = dep.get("package") or {}
    advisory = alert.get("security_advisory") or {}
    vuln = alert.get("security_vulnerability") or {}

    severity = _first_nonempty(vuln.get("severity"), advisory.get("severity"), "unknown")
    package_name = _first_nonempty(pkg.get("name"), dep.get("package_name"), "(unknown package)")
    manifest = _first_nonempty(dep.get("manifest_path"), dep.get("manifest"), "")
    scope = _first_nonempty(dep.get("scope"), "")
    summary_text = _first_nonempty(advisory.get("summary"), advisory.get("description"), "Dependabot vulnerability alert")

    summary = f"[Dependabot] {severity}: {package_name}"
    if manifest:
        summary += f" ({manifest})"
    summary = summary[:255]

    url = _first_nonempty(alert.get("html_url"), advisory.get("ghsa_id"), "")
    if isinstance(url, str) and url and not url.startswith("http"):
        url = ""

    bullets = [
        f"Repo: {owner}/{repo}",
        f"Alert: {alert_id}" if alert_id is not None else "Alert: (unknown id)",
        f"Severity: {severity}",
        f"Package: {package_name}",
    ]
    if scope:
        bullets.append(f"Scope: {scope}")
    if manifest:
        bullets.append(f"Manifest: {manifest}")
    if url:
        bullets.append(f"URL: {url}")

    nodes: list[dict[str, Any]] = [adf_heading("Dependabot vulnerability alert", 2)]
    for p in adf_paragraphs(truncate_text(str(summary_text), 20000)):
        nodes.append(p)
    nodes.append(adf_heading("Metadata", 3))
    nodes.append(adf_bullet_list(bullets))

    # Keep a compact JSON excerpt for forensics without bloating Jira.
    nodes.append(adf_heading("Raw payload (excerpt)", 3))
    excerpt = {
        "actionable": {
            "dependency": dep,
            "security_vulnerability": vuln,
            "security_advisory": {
                k: advisory.get(k)
                for k in ("summary", "severity", "cve_id", "ghsa_id", "references", "identifiers")
                if advisory.get(k) is not None
            },
        }
    }
    nodes.append(adf_code_block(truncate_text(json.dumps(excerpt, indent=2)[:20000]), "json"))

    payload = {
        "fields": {
            "project": {"key": project_key},
            "issuetype": {"name": cfg["jira"].get("task_issuetype_name", "Task")},
            "summary": summary,
            "description": adf_doc(*nodes),
            "labels": [label_dependabot_alert(owner, repo, alert_id or "unknown")],
        }
    }
    return payload


def handle_dependabot_alert(session, cfg, event):
    owner, gh_repo = get_owner_repo(event)
    repo = normalize_repo(cfg, gh_repo)
    action = (event.get("action") or "").strip()
    alert = event.get("alert") or {}
    alert_id = _dependabot_alert_id(alert)

    if alert_id is None:
        print(f"dependabot_alert[{action}] {owner}/{repo} missing alert id -- ignored")
        return

    print(f"dependabot_alert[{action}] {owner}/{repo} alert={alert_id}")
    task_key = find_dependabot_alert_task(session, cfg, owner, repo, alert_id)

    # Create (idempotent) on first-seen states; other actions can be layered later.
    create_actions = {"created", "reintroduced", "reopened"}
    if action not in create_actions:
        print(f"  action '{action}' ignored (create_actions={sorted(create_actions)})")
        return

    if task_key:
        print(f"  already has Task {task_key}, skip")
        return

    payload = build_dependabot_alert_task_payload(cfg, owner, repo, alert)
    result = jira_create_issue(session, payload)
    print(f"  created {result['key']}")


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

HANDLERS = {
    "pull_request": handle_pull_request,
    "pull_request_review": handle_pull_request_review,
    "pull_request_review_comment": handle_pull_request_review_comment,
    "issue_comment": handle_issue_comment,
    "push": handle_push,
    "dependabot_alert": handle_dependabot_alert,
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
