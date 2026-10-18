"""GitHub の PR レビューコメントを取り込む.

本文の先頭の [MUST] / [WANT] / [NITS] / [Q] を重要度として読む。返信（in_reply_to_id があるもの）は指摘ではなく、
元の指摘の「対応内容」としてつなぐ。Bot のコメントは除く。

取得は REST API（GH_TOKEN）。通信は devtools_common の HttpClient（GET は 429・5xx・接続エラーを再試行、
Retry-After に従う。1 秒あたりの回数も抑える）。GitHub が 403 で返すレート制限（secondary rate limit・残り 0）も、
待ち時間が RATE_LIMIT_MAX_WAIT 秒以内なら 429 と同じく待って再試行する。
取得の途中で失敗したら、それまでに取り終えた PR は捨てずに返し（"error" に理由）、呼び出し側が取り込む。
PR は更新の新しい順に 1 ページずつ取り、--since より古い PR が出たら止める。
日付（created_at・--since との比較）は UTC の時刻を設定のタイムゾーン（既定 Asia/Tokyo）の日付にして使う。
--since は YYYY-MM-DD（誤りは引数の誤り）。--file でも PR の updated_at で同じように絞る（updated_at の無い PR は絞れないので残し、件数を警告）。
オフラインで試すときは、同じ形の JSON（--file）を読む:
    {"repo": "owner/name", "pulls": [{"number": 1, "title": "...", "user": {"login": "..."}, "updated_at": "2026-10-04T23:30:00Z",
      "comments": [ ...pulls/{n}/comments の要素... ], "reviews": [ ...pulls/{n}/reviews の要素... ]}]}
"""

from __future__ import annotations

import datetime as dt
import email.message
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from devtools_common.httpclient import HttpClient, HttpError, safe_urlopen

from ..mask import pseudonym
from ..model import normalize, split_tag

log = logging.getLogger(__name__)

API = "https://api.github.com"
PER_PAGE = 100
MAX_PAGES = 50
DEFAULT_TZ = "Asia/Tokyo"
Fetch = Callable[[str], Any]


RATE_LIMIT_MAX_WAIT = 120.0  # 403 のレート制限で待つ上限（秒）. これより長く待つ必要があるときは再試行せずに失敗にする
RATE_LIMIT_DEFAULT_WAIT = 60.0  # Retry-After も x-ratelimit-reset も無い secondary rate limit で待つ秒数（GitHub の推奨は 1 分以上）


def rate_limit_wait(status: int, headers: Any, body: str, now: Callable[[], float] = time.time) -> float | None:
    """GitHub の 403 がレート制限なら待つ秒数、そうでなければ None（権限の不足など. 再試行しない）."""
    if status != 403:
        return None
    get = headers.get if headers is not None else (lambda _k, _d=None: None)
    retry_after = get("Retry-After")
    remaining = get("x-ratelimit-remaining")
    if not retry_after and remaining != "0" and "rate limit" not in body.lower():
        return None
    if retry_after:
        try:
            return max(float(retry_after), 0.0)
        except ValueError:
            pass
    reset = get("x-ratelimit-reset")
    if remaining == "0" and reset:
        try:
            return max(float(reset) - now(), 1.0)
        except ValueError:
            pass
    return RATE_LIMIT_DEFAULT_WAIT


def _rate_limit_as_429(opener: Callable[..., Any] = safe_urlopen) -> Callable[..., Any]:
    """403 のレート制限を、Retry-After 付きの 429 に読み替える opener（HttpClient が待って再試行する）."""

    def open_(req: urllib.request.Request, timeout: float) -> Any:
        try:
            return opener(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code != 403:
                raise
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 — 本文が読めなくてもヘッダーで判断する
                body = ""
            wait = rate_limit_wait(e.code, e.headers, body)
            if wait is None or wait > RATE_LIMIT_MAX_WAIT:
                raise urllib.error.HTTPError(e.url, e.code, e.reason or "Forbidden", e.headers, None) from None
            headers = email.message.Message()
            headers["Retry-After"] = str(int(wait + 0.999))
            log.warning("GitHub のレート制限（403）: %d 秒待って再試行します", int(wait + 0.999))
            raise urllib.error.HTTPError(e.url, 429, "rate limited (403)", headers, None) from None

    return open_


def http_fetch(token: str, client: HttpClient | None = None) -> Fetch:
    """GitHub の REST API を GET する関数. 再試行・レート制限は HttpClient に任せる（api.github.com 以外には送らない）.

    403 のレート制限（secondary rate limit など）も待って再試行する（RATE_LIMIT_MAX_WAIT 秒まで）.
    """
    client = client or HttpClient(
        API,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        rate_per_sec=5.0,
        backoff_max=RATE_LIMIT_MAX_WAIT,
        opener=_rate_limit_as_429(),
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


def parse_since(value: str) -> str:
    """--since の値（YYYY-MM-DD）を確かめて返す. 空はそのまま（絞らない）. 形式・日付が誤りなら ValueError."""
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise ValueError(f"--since は YYYY-MM-DD で指定してください（例: 2026-04-01）: {value}")
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"--since の日付が正しくありません（YYYY-MM-DD）: {value}") from None
    return value


def filter_since(data: dict, since: str, tz: str = DEFAULT_TZ) -> tuple[dict, int, int]:
    """取得済みの JSON（--file）の PR を、更新日（updated_at を tz の日付にしたもの）が since 以降のものに絞る.

    → (絞った JSON, 除いた PR の件数, updated_at が無く絞れなかった PR の件数（残す）)
    """
    if not since:
        return data, 0, 0
    kept, dropped, unknown = [], 0, 0
    for pr in data.get("pulls", []):
        updated = pr.get("updated_at") or ""
        if not updated:
            unknown += 1
            kept.append(pr)
        elif local_date(updated, tz) >= since:
            kept.append(pr)
        else:
            dropped += 1
    return {**data, "pulls": kept}, dropped, unknown


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
    取得の途中で失敗（HttpError）したときは、取り終えた PR を返し、"error" に理由を入れる（1 件も無ければ HttpError をそのまま送出）。
    """
    owner_repo = urllib.parse.quote(repo, safe="/")
    pulls: list[dict] = []
    cut: list[str] = []
    notes: list[str] = []
    more_pulls = False
    error = ""
    try:
        for pr in _pages(fetch, f"{API}/repos/{owner_repo}/pulls?state=all&sort=updated&direction=desc", truncated=cut):
            if since and local_date(pr.get("updated_at", ""), tz) < since:
                break  # 一覧の途中で止めるので、一覧の打ち切り（cut）には入らない
            if len(pulls) >= max_pulls:
                more_pulls = True  # 上限を超える PR がまだある
                break
            n = pr["number"]
            # コメント・レビューを両方取り終えた PR だけを足す（途中で失敗した PR は入れない）
            pulls.append(
                {
                    "number": n,
                    "title": pr.get("title", ""),
                    "user": pr.get("user") or {},
                    "updated_at": pr.get("updated_at", ""),  # --file で読み直すときも --since で絞れるように
                    "comments": list(_pages(fetch, f"{API}/repos/{owner_repo}/pulls/{n}/comments", truncated=cut)),
                    "reviews": list(_pages(fetch, f"{API}/repos/{owner_repo}/pulls/{n}/reviews", truncated=cut)),
                }
            )
    except HttpError as e:
        if not pulls:
            raise  # 1 件も取れていなければ以前どおり失敗にする
        error = f"取得の途中で失敗しました（{e}）。取り終えた PR {len(pulls)} 件だけを返します"
        log.warning("%s: %s", repo, error)
    if more_pulls:
        notes.append(f"PR は {max_pulls} 件で打ち切りました（それより古い PR は取り込んでいません。--since で期間を絞ってください）")
        log.warning("%s: PR を %d 件で打ち切りました", repo, max_pulls)
    for url in cut:
        what = "PR の一覧" if "/pulls?" in url else url.removeprefix(f"{API}/repos/{owner_repo}/")
        notes.append(f"{what} は {MAX_PAGES} ページ（{MAX_PAGES * PER_PAGE} 件）で打ち切りました")
    out: dict[str, Any] = {"repo": repo, "pulls": pulls}
    if notes:
        out["truncated"] = notes
    if error:
        out["error"] = error
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
