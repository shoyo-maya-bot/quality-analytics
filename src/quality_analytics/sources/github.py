"""GitHub の PR レビューコメントを取り込む.

本文の先頭の [MUST] / [WANT] / [NITS] / [Q] を重要度として読む。返信（in_reply_to_id があるもの）は指摘ではなく、
元の指摘の「対応内容」としてつなぐ。Bot のコメントは除く。

取得は REST API（GH_TOKEN）。通信は devtools_common の HttpClient（GET は 429・5xx・接続エラーを再試行、
Retry-After に従う。1 秒あたりの回数も抑える）。PR は更新の新しい順に 1 ページずつ取り、--since より古い PR が出たら止める。
日付（created_at・--since との比較）は UTC の時刻を設定のタイムゾーン（既定 Asia/Tokyo）の日付にして使う。
オフラインで試すときは、同じ形の JSON（--file）を読む:
    {"repo": "owner/name", "pulls": [{"number": 1, "title": "...", "user": {"login": "..."},
      "comments": [ ...pulls/{n}/comments の要素... ], "reviews": [ ...pulls/{n}/reviews の要素... ]}]}
"""

from __future__ import annotations

import datetime as dt
import logging
import urllib.parse
from collections.abc import Callable, Iterator
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from devtools_common.httpclient import HttpClient

from ..mask import pseudonym
from ..model import normalize, split_tag

log = logging.getLogger(__name__)

API = "https://api.github.com"
PER_PAGE = 100
MAX_PAGES = 50
DEFAULT_TZ = "Asia/Tokyo"
Fetch = Callable[[str], Any]


def http_fetch(token: str, client: HttpClient | None = None) -> Fetch:
    """GitHub の REST API を GET する関数. 再試行・レート制限は HttpClient に任せる（api.github.com 以外には送らない）."""
    client = client or HttpClient(
        API,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        rate_per_sec=5.0,
    )

    def fetch(url: str) -> Any:
        return client.get(url).json()

    return fetch


def local_date(timestamp: str, tz: str = DEFAULT_TZ) -> str:
    """API の時刻（UTC. 例: 2026-10-04T23:30:00Z）→ そのタイムゾーンの日付（YYYY-MM-DD）. 読めない値は先頭 10 文字."""
    if not timestamp:
        return ""
    try:
        t = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return timestamp[:10]
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.UTC)
    return t.astimezone(ZoneInfo(tz)).date().isoformat()


def _pages(fetch: Fetch, url: str, limit: int = MAX_PAGES, truncated: list[str] | None = None) -> Iterator[dict]:
    """1 ページずつ取る（呼び出し側が途中で止めれば、残りのページは取らない）.

    limit ページ取ってもまだ続きがありそうなら（最後のページが満杯）、打ち切ったことを警告し、truncated に url を足す。
    """
    sep = "&" if "?" in url else "?"
    for page in range(1, limit + 1):
        items = fetch(f"{url}{sep}per_page={PER_PAGE}&page={page}")
        if not items:
            return
        yield from items
        if len(items) < PER_PAGE:
            return
    log.warning("%s: %d ページ（%d 件）で取得を打ち切りました。続きは取り込んでいません", url, limit, limit * PER_PAGE)
    if truncated is not None:
        truncated.append(url)


def download(repo: str, fetch: Fetch, since: str = "", max_pulls: int = 200, tz: str = DEFAULT_TZ) -> dict:
    """PR とそのレビューコメント・レビュー本文を集める（since は YYYY-MM-DD。tz の日付での更新日で絞る）.

    max_pulls 件や 1 つの一覧の上限ページ数（MAX_PAGES）で打ち切ったときは、黙って捨てずに警告を出し、
    戻り値の "truncated"（打ち切りの説明のリスト）にも残す（呼び出し側が件数とともに表示する）。
    """
    owner_repo = urllib.parse.quote(repo, safe="/")
    pulls: list[dict] = []
    cut: list[str] = []
    notes: list[str] = []
    more_pulls = False
    for pr in _pages(fetch, f"{API}/repos/{owner_repo}/pulls?state=all&sort=updated&direction=desc", truncated=cut):
        if since and local_date(pr.get("updated_at", ""), tz) < since:
            break  # 一覧の途中で止めるので、一覧の打ち切り（cut）には入らない
        if len(pulls) >= max_pulls:
            more_pulls = True  # 上限を超える PR がまだある
            break
        n = pr["number"]
        pulls.append(
            {
                "number": n,
                "title": pr.get("title", ""),
                "user": pr.get("user") or {},
                "comments": list(_pages(fetch, f"{API}/repos/{owner_repo}/pulls/{n}/comments", truncated=cut)),
                "reviews": list(_pages(fetch, f"{API}/repos/{owner_repo}/pulls/{n}/reviews", truncated=cut)),
            }
        )
    if more_pulls:
        notes.append(f"PR は {max_pulls} 件で打ち切りました（それより古い PR は取り込んでいません。--since で期間を絞ってください）")
        log.warning("%s: PR を %d 件で打ち切りました", repo, max_pulls)
    for url in cut:
        what = "PR の一覧" if "/pulls?" in url else url.removeprefix(f"{API}/repos/{owner_repo}/")
        notes.append(f"{what} は {MAX_PAGES} ページ（{MAX_PAGES * PER_PAGE} 件）で打ち切りました")
    out: dict[str, Any] = {"repo": repo, "pulls": pulls}
    if notes:
        out["truncated"] = notes
    return out


def _who(user: dict | None, anonymize: bool) -> str:
    login = (user or {}).get("login", "")
    if not anonymize or not login:
        return login
    return pseudonym(login)


def to_findings(data: dict, *, phase: str = "CD", only_tagged: bool = False, anonymize: bool = False, tz: str = DEFAULT_TZ) -> pd.DataFrame:
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
                    "created_at": local_date(c.get("created_at") or c.get("submitted_at") or "", tz),
                }
            )
    return normalize(pd.DataFrame(rows))
