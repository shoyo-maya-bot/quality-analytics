"""第 6 回: 本文の氏名の伏せ（姓だけ・全角空白）・仮名のソルト・GitHub の取得の途中失敗と 403 のレート制限."""

from __future__ import annotations

import contextlib
import email.message
import io
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd
import pytest
from devtools_common.cli import run_main
from devtools_common.httpclient import HttpClient, HttpError

from quality_analytics import mask, model
from quality_analytics.cli import main
from quality_analytics.sources import github

ROOT = Path(__file__).resolve().parent.parent

# --- 本文の氏名 --------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_salt(monkeypatch: pytest.MonkeyPatch):
    # setenv で元の状態（無し）を覚えさせてから消す. テスト中に .env から読んだ値も、終わったら元に戻る
    monkeypatch.setenv(mask.SALT_ENV, "")
    monkeypatch.delenv(mask.SALT_ENV)


def _frame(reviewer: str, text: str) -> pd.Series:
    df = model.normalize(pd.DataFrame([{"id": "1", "reviewer": reviewer, "author": "", "text": text}]))
    return mask.Masker().frame(df).iloc[0]


@pytest.mark.parametrize(
    "text",
    ["例題 花子さんの指摘", "例題　花子さんの指摘", "例題花子さんの指摘", "例題  花子さんの指摘"],
)
def test_name_with_any_spacing_is_replaced(text: str):
    r = _frame("例題 花子", text)
    assert r["text"] == f"{mask.pseudonym('例題 花子')}さんの指摘"


def test_unspaced_name_matches_spaced_text():
    r = _frame("例題花子", "担当: 例題　花子")
    assert r["text"] == f"担当: {mask.pseudonym('例題花子')}"


def test_surname_or_given_name_alone_is_replaced():
    r = _frame("例題 花子", "例題さんに確認。花子さんも同意")
    alias = mask.pseudonym("例題 花子")
    assert r["text"] == f"{alias}さんに確認。{alias}さんも同意"


def test_shared_surname_is_masked_not_attributed():
    df = model.normalize(
        pd.DataFrame(
            [
                {"id": "1", "reviewer": "例題 花子", "author": "例題 次郎", "text": "例題さんの件"},
            ]
        )
    )
    r = mask.Masker().frame(df).iloc[0]
    assert r["text"] == f"{mask.MASK}さんの件"  # どちらの例題さんか決められない


def test_ascii_name_with_spaces_and_parts():
    r = _frame("Taro Sample", "Taro  Sample said; ask Sample. Taros is another word")
    alias = mask.pseudonym("Taro Sample")
    assert r["text"] == f"{alias} said; ask {alias}. Taros is another word"


def test_one_char_parts_are_not_replaced():
    r = _frame("例 花子", "例のとおり。花子さん")
    alias = mask.pseudonym("例 花子")
    assert r["text"] == f"例のとおり。{alias}さん"


# --- 仮名のソルト ------------------------------------------------------------------------------


def test_pseudonym_without_salt_is_unchanged():
    import hashlib

    assert mask.pseudonym("例題花子") == "U-" + hashlib.sha256("例題花子".encode()).hexdigest()[:6]
    assert not mask.salted()


def test_pseudonym_with_salt_differs_and_is_stable(monkeypatch: pytest.MonkeyPatch):
    plain = mask.pseudonym("例題花子")
    monkeypatch.setenv(mask.SALT_ENV, "s3cret-salt")
    salted = mask.pseudonym("例題花子")
    assert salted != plain and salted == mask.pseudonym("例題花子")
    assert mask.pseudonym("例題花子", salt="other") != salted
    assert mask.salted()
    r = _frame("例題花子", "例題花子さん")
    assert r["reviewer"] == salted and r["text"] == f"{salted}さん"


def test_salt_from_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import json

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"{mask.SALT_ENV}=from-dotenv\n", encoding="utf-8")
    raw = {
        "repo": "o/r",
        "pulls": [{"number": 1, "user": {"login": "author01"}, "comments": [{"id": 5, "body": "[MUST] x", "user": {"login": "rev01"}}]}],
    }
    (tmp_path / "raw.json").write_text(json.dumps(raw), encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = run_main(
            main,
            ["--data", str(tmp_path / "f.csv"), "--config-dir", str(ROOT / "config"), "collect", "github", "--file", "raw.json"],
        )
    assert rc == 0, out.getvalue() + err.getvalue()
    text = (tmp_path / "f.csv").read_text(encoding="utf-8")
    assert mask.pseudonym("rev01", salt="from-dotenv") in text
    assert mask.pseudonym("rev01", salt="") not in text


# --- GitHub: 途中失敗 ---------------------------------------------------------------------------


def _fetch_failing_at(fail_pr: int):
    def fetch(url: str):
        if "/pulls?" in url:
            return [{"number": n, "updated_at": "2026-09-30T00:00:00Z"} for n in (1, 2, 3)] if url.endswith("page=1") else []
        if f"/pulls/{fail_pr}/" in url:
            raise HttpError(500, "Internal Server Error")
        if "/comments" in url:
            return [{"id": 10, "body": "[MUST] 直す", "user": {"login": "rev"}}]
        return []

    return fetch


def test_download_keeps_finished_pulls_on_error():
    data = github.download("o/r", _fetch_failing_at(3))
    assert [p["number"] for p in data["pulls"]] == [1, 2]  # 途中の PR 3 は入れない
    assert "取り終えた PR 2 件" in data["error"]


def test_download_raises_when_nothing_finished():
    with pytest.raises(HttpError):
        github.download("o/r", _fetch_failing_at(1))


def test_cli_stores_partial_and_exits_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GH_TOKEN", "dummy")
    monkeypatch.setattr(github, "http_fetch", lambda token: _fetch_failing_at(3))
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = run_main(
            main,
            ["--data", str(tmp_path / "f.csv"), "--config-dir", str(ROOT / "config"), "collect", "github", "--repo", "o/r"],
        )
    assert rc == 3
    assert "取り直してください" in err.getvalue()
    stored = model.load(tmp_path / "f.csv")
    assert len(stored) == 2  # PR 1・2 のコメントは取り込んだ（以前は全部失っていた）


# --- GitHub: 403 のレート制限 -----------------------------------------------------------------


def _http_error(code: int, headers: dict[str, str], body: bytes = b"") -> urllib.error.HTTPError:
    msg = email.message.Message()
    for k, v in headers.items():
        msg[k] = v
    return urllib.error.HTTPError("https://api.github.com/x", code, "Forbidden", msg, io.BytesIO(body))


@pytest.mark.parametrize(
    ("headers", "body", "expected"),
    [
        ({"Retry-After": "30"}, b"", 30.0),
        ({}, b'{"message": "You have exceeded a secondary rate limit"}', github.RATE_LIMIT_DEFAULT_WAIT),
        ({"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1060"}, b"", 60.0),
        ({}, b'{"message": "Resource not accessible by integration"}', None),
    ],
)
def test_rate_limit_wait(headers, body, expected):
    msg = email.message.Message()
    for k, v in headers.items():
        msg[k] = v
    assert github.rate_limit_wait(403, msg, body.decode(), now=lambda: 1000.0) == expected


class _Resp:
    status = 200
    headers: dict[str, str] = {}

    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _client(errors: list[urllib.error.HTTPError], sleeps: list[float]) -> HttpClient:
    def opener(req: urllib.request.Request, timeout: float):
        if errors:
            raise errors.pop(0)
        return _Resp(b"[]")

    return HttpClient(
        github.API,
        rate_per_sec=1000.0,
        backoff_max=github.RATE_LIMIT_MAX_WAIT,
        opener=github._rate_limit_as_429(opener),
        sleep=sleeps.append,
    )


def test_secondary_rate_limit_403_is_retried():
    sleeps: list[float] = []
    client = _client([_http_error(403, {}, b'{"message": "You have exceeded a secondary rate limit."}')], sleeps)
    assert github.http_fetch("t", client)(f"{github.API}/repos/o/r/pulls") == []
    assert sleeps and sleeps[0] >= 60  # 1 分以上待ってから再試行した


def test_permission_403_is_not_retried():
    sleeps: list[float] = []
    client = _client([_http_error(403, {}, b'{"message": "Resource not accessible"}')], sleeps)
    with pytest.raises(HttpError) as e:
        github.http_fetch("t", client)(f"{github.API}/repos/o/r/pulls")
    assert e.value.status == 403 and sleeps == []


def test_long_primary_rate_limit_is_not_waited():
    import time

    sleeps: list[float] = []
    reset = str(int(time.time()) + 3600)
    client = _client([_http_error(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset})], sleeps)
    with pytest.raises(HttpError):
        github.http_fetch("t", client)(f"{github.API}/repos/o/r/pulls")
    assert sleeps == []
