"""GitHub の PR レビューコメントを取り込む.

本文の先頭の [MUST] / [WANT] / [NITS] / [Q] を重要度として読む。返信（in_reply_to_id があるもの）は指摘ではなく、
元の指摘の「対応内容」としてつなぐ。Bot のコメントは除く。

取得は REST API（GH_TOKEN）。オフラインで試すときは、同じ形の JSON（--file）を読む:
    {"repo": "owner/name", "pulls": [{"number": 1, "title": "...", "user": {"login": "..."},
      "comments": [ ...pulls/{n}/comments の要素... ], "reviews": [ ...pulls/{n}/reviews の要素... ]}]}
"""

from __future__ import annotations

import hashlib
import json
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

import pandas as pd

from ..model import normalize, split_tag

API = "https://api.github.com"
Fetch = Callable[[str], Any]


def http_fetch(token: str) -> Fetch:
    def fetch(url: str) -> Any:
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        )
        with urllib.request.urlopen(req, timeout=30) as res:  # noqa: S310 — https の固定 URL のみ
            return json.loads(res.read().decode("utf-8"))

    return fetch


def _pages(fetch: Fetch, url: str, limit: int = 50) -> list[dict]:
    out: list[dict] = []
    sep = "&" if "?" in url else "?"
    for page in range(1, limit + 1):
        items = fetch(f"{url}{sep}per_page=100&page={page}")
        if not items:
            break
        out += items
        if len(items) < 100:
            break
    return out


def download(repo: str, fetch: Fetch, since: str = "", max_pulls: int = 200) -> dict:
    """PR とそのレビューコメント・レビュー本文を集める（since は YYYY-MM-DD。更新日で絞る）."""
    owner_repo = urllib.parse.quote(repo, safe="/")
    pulls = []
    for pr in _pages(fetch, f"{API}/repos/{owner_repo}/pulls?state=all&sort=updated&direction=desc"):
        if since and pr.get("updated_at", "")[:10] < since:
            break
        n = pr["number"]
        pulls.append(
            {
                "number": n,
                "title": pr.get("title", ""),
                "user": pr.get("user") or {},
                "comments": _pages(fetch, f"{API}/repos/{owner_repo}/pulls/{n}/comments"),
                "reviews": _pages(fetch, f"{API}/repos/{owner_repo}/pulls/{n}/reviews"),
            }
        )
        if len(pulls) >= max_pulls:
            break
    return {"repo": repo, "pulls": pulls}


def _who(user: dict | None, anonymize: bool) -> str:
    login = (user or {}).get("login", "")
    if not anonymize or not login:
        return login
    return "U-" + hashlib.sha256(login.encode()).hexdigest()[:6]


def to_findings(data: dict, *, phase: str = "CD", only_tagged: bool = False, anonymize: bool = False) -> pd.DataFrame:
    repo = data.get("repo", "")
    rows: list[dict[str, str]] = []
    for pr in data.get("pulls", []):
        deliverable = f"{repo}#{pr['number']}"
        author = _who(pr.get("user"), anonymize)
        comments = [c for c in pr.get("comments", []) if (c.get("user") or {}).get("type") != "Bot"]
        replies: dict[int, list[str]] = {}
        for c in comments:
            if c.get("in_reply_to_id"):
                replies.setdefault(c["in_reply_to_id"], []).append(c.get("body", "").strip())
        items = [("c", c, c.get("path", "")) for c in comments if not c.get("in_reply_to_id")]
        items += [
            ("r", r, "") for r in pr.get("reviews", []) if (r.get("body") or "").strip() and (r.get("user") or {}).get("type") != "Bot"
        ]
        for kind, c, path in items:
            sev, text = split_tag(c.get("body", ""))
            if only_tagged and not sev:
                continue
            rows.append(
                {
                    "id": f"github:{deliverable}/{kind}{c['id']}",
                    "source": "github",
                    "project": repo,
                    "phase": phase,
                    "deliverable": deliverable,
                    "reviewer": _who(c.get("user"), anonymize),
                    "author": author,
                    "severity": sev,
                    "text": f"{text}（{path}）" if path else text,
                    "response": " / ".join(replies.get(c["id"], [])),
                    "created_at": (c.get("created_at") or c.get("submitted_at") or "")[:10],
                }
            )
    return normalize(pd.DataFrame(rows))
