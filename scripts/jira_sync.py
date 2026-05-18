"""Shared Jira + GitHub helpers used by backfill.py and (later) live_sync.py.

Reads its Jira token from $JIRA_TOKEN, or from the file pointed to by
$JIRA_TOKEN_FILE. GitHub API calls go through the locally-authenticated
`gh` CLI, so no GitHub token handling is needed here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from requests.auth import HTTPBasicAuth


# ---------------------------------------------------------------------------
# Auth / session
# ---------------------------------------------------------------------------

def load_token() -> str:
    """Return the Jira API token from $JIRA_TOKEN or file at $JIRA_TOKEN_FILE."""
    inline = os.environ.get("JIRA_TOKEN")
    if inline:
        return inline.strip()
    path = os.environ.get("JIRA_TOKEN_FILE")
    if path and Path(path).exists():
        return Path(path).read_text(encoding="utf-8").strip()
    raise RuntimeError(
        "No Jira token. Set $JIRA_TOKEN, or $JIRA_TOKEN_FILE to a token file."
    )


def jira_session(base_url: str, email: str, token: str) -> requests.Session:
    s = requests.Session()
    s.auth = HTTPBasicAuth(email, token)
    s.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    s.base_url = base_url.rstrip("/")  # type: ignore[attr-defined]
    return s


def jira_request(
    session: requests.Session, method: str, path: str, **kwargs: Any
) -> requests.Response:
    """Issue a Jira REST call with retry on 429 and 5xx (exponential backoff)."""
    url = f"{session.base_url}{path}"  # type: ignore[attr-defined]
    backoff = 1.0
    last: Optional[requests.Response] = None
    for _ in range(6):
        r = session.request(method, url, timeout=30, **kwargs)
        last = r
        if r.status_code == 429:
            wait = float(r.headers.get("Retry-After", backoff))
            print(f"  [jira 429] sleep {wait}s", file=sys.stderr)
            time.sleep(wait)
            backoff *= 2
            continue
        if 500 <= r.status_code < 600:
            print(
                f"  [jira {r.status_code}] retry in {backoff}s", file=sys.stderr
            )
            time.sleep(backoff)
            backoff *= 2
            continue
        return r
    assert last is not None
    return last


# ---------------------------------------------------------------------------
# ADF (Atlassian Document Format) builders
# ---------------------------------------------------------------------------

def adf_text(text: str) -> Dict[str, Any]:
    return {"type": "text", "text": text if text else " "}


def adf_paragraph(text: str) -> Dict[str, Any]:
    return {"type": "paragraph", "content": [adf_text(text)]}


def adf_paragraphs(text: str) -> List[Dict[str, Any]]:
    """Split text on blank lines, one ADF paragraph per non-empty block."""
    if not text:
        return [adf_paragraph(" ")]
    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
    return [adf_paragraph(b) for b in blocks] or [adf_paragraph(" ")]


def adf_heading(text: str, level: int = 2) -> Dict[str, Any]:
    return {
        "type": "heading",
        "attrs": {"level": level},
        "content": [adf_text(text)],
    }


def adf_code_block(text: str, lang: str = "") -> Dict[str, Any]:
    node: Dict[str, Any] = {"type": "codeBlock", "content": [adf_text(text or " ")]}
    if lang:
        node["attrs"] = {"language": lang}
    return node


def adf_bullet_list(items: List[str]) -> Dict[str, Any]:
    return {
        "type": "bulletList",
        "content": [
            {"type": "listItem", "content": [adf_paragraph(i)]} for i in items
        ],
    }


def adf_doc(*nodes: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "doc", "version": 1, "content": list(nodes)}


def truncate_text(text: str, limit: int = 30000) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n... [truncated, original {len(text)} chars]"


# ---------------------------------------------------------------------------
# Jira high-level operations
# ---------------------------------------------------------------------------

def jira_create_issue(session: requests.Session, payload: Dict[str, Any]) -> Dict[str, Any]:
    r = jira_request(session, "POST", "/rest/api/3/issue", json=payload)
    if r.status_code >= 300:
        raise RuntimeError(f"Create issue failed: {r.status_code} {r.text}")
    return r.json()


def jira_add_comment(
    session: requests.Session, issue_key: str, body_adf: Dict[str, Any]
) -> Dict[str, Any]:
    r = jira_request(
        session,
        "POST",
        f"/rest/api/3/issue/{issue_key}/comment",
        json={"body": body_adf},
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Comment failed on {issue_key}: {r.status_code} {r.text}")
    return r.json()


def jira_find_by_label(
    session: requests.Session,
    project_key: str,
    label: str,
    issuetype: Optional[str] = None,
) -> Optional[str]:
    """Return the issue key of the first issue matching `label`, or None."""
    parts = [f'project = "{project_key}"', f'labels = "{label}"']
    if issuetype:
        parts.append(f'issuetype = "{issuetype}"')
    jql = " AND ".join(parts)
    r = jira_request(
        session,
        "POST",
        "/rest/api/3/search/jql",
        json={"jql": jql, "fields": ["summary"], "maxResults": 1},
    )
    if r.status_code >= 300:
        return None
    issues = r.json().get("issues", [])
    return issues[0]["key"] if issues else None


def jira_update_issue(
    session: requests.Session, issue_key: str, fields: Dict[str, Any]
) -> None:
    """Update fields on an existing Jira issue."""
    r = jira_request(
        session, "PUT", f"/rest/api/3/issue/{issue_key}", json={"fields": fields}
    )
    if r.status_code >= 300:
        raise RuntimeError(
            f"Update {issue_key} failed: {r.status_code} {r.text}"
        )


def jira_transition(
    session: requests.Session, issue_key: str, target_status: str
) -> None:
    r = jira_request(session, "GET", f"/rest/api/3/issue/{issue_key}/transitions")
    if r.status_code >= 300:
        print(
            f"  WARN: fetch transitions for {issue_key} failed: {r.status_code}",
            file=sys.stderr,
        )
        return
    transitions = r.json().get("transitions", [])
    tid = next(
        (
            t["id"]
            for t in transitions
            if t["to"]["name"].lower() == target_status.lower()
        ),
        None,
    )
    if not tid:
        print(
            f"  WARN: no transition to '{target_status}' on {issue_key}",
            file=sys.stderr,
        )
        return
    r2 = jira_request(
        session,
        "POST",
        f"/rest/api/3/issue/{issue_key}/transitions",
        json={"transition": {"id": tid}},
    )
    if r2.status_code >= 300:
        print(
            f"  WARN: transition {issue_key} -> {target_status} failed: "
            f"{r2.status_code} {r2.text}",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# GitHub helpers (via local `gh` CLI)
# ---------------------------------------------------------------------------

def gh_api(path: str, paginate: bool = False) -> Any:
    cmd = ["gh", "api"]
    if paginate:
        cmd += ["--paginate"]
    cmd.append(path)
    try:
        out = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"gh api {path} failed: {(e.stderr or '').strip()}")
    text = (out.stdout or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # `gh api --paginate` concatenates multiple JSON arrays. Decode each.
        decoder = json.JSONDecoder()
        merged: List[Any] = []
        idx = 0
        while idx < len(text):
            obj, end = decoder.raw_decode(text, idx)
            if isinstance(obj, list):
                merged.extend(obj)
            else:
                merged.append(obj)
            idx = end
            while idx < len(text) and text[idx].isspace():
                idx += 1
        return merged


def gh_repo_info(owner: str, repo: str) -> Dict[str, Any]:
    return gh_api(f"repos/{owner}/{repo}")


def gh_pr_list(owner: str, repo: str) -> List[Dict[str, Any]]:
    return gh_api(f"repos/{owner}/{repo}/pulls?state=all&per_page=100", paginate=True) or []


def gh_pr_commits(owner: str, repo: str, num: int) -> List[Dict[str, Any]]:
    return gh_api(f"repos/{owner}/{repo}/pulls/{num}/commits?per_page=100", paginate=True) or []


def gh_pr_issue_comments(owner: str, repo: str, num: int) -> List[Dict[str, Any]]:
    return gh_api(f"repos/{owner}/{repo}/issues/{num}/comments?per_page=100", paginate=True) or []


def gh_pr_review_comments(owner: str, repo: str, num: int) -> List[Dict[str, Any]]:
    return gh_api(f"repos/{owner}/{repo}/pulls/{num}/comments?per_page=100", paginate=True) or []


def gh_pr_reviews(owner: str, repo: str, num: int) -> List[Dict[str, Any]]:
    return gh_api(f"repos/{owner}/{repo}/pulls/{num}/reviews?per_page=100", paginate=True) or []


def gh_repo_commits(owner: str, repo: str, branch: str) -> List[Dict[str, Any]]:
    return (
        gh_api(
            f"repos/{owner}/{repo}/commits?sha={branch}&per_page=100",
            paginate=True,
        )
        or []
    )
