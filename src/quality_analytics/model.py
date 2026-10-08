"""指摘の共通形式と、その保存（CSV）.

どの取り込み元も、この列にそろえて data/findings.csv に書く。分類の結果も同じファイルの列に入る。
CSV は Excel で開けるように UTF-8（BOM 付き）で書く。数式の始まりの文字（= + - @ など）で始まる値は、
Excel で数式として動かないように先頭に ' を付けて書き、読むときに外す（safe.py）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

import pandas as pd

from . import safe

COLUMNS = [
    "id",  # 取り込み元の中で一意（例: table:12, github:owner/repo#3/c123, office-review:file.md#2）
    "source",  # table / github / office-review
    "project",
    "phase",  # RD / BD / DD / CD
    "deliverable",  # 成果物（設計書名、PR など）
    "reviewer",
    "author",
    "severity",  # MUST / WANT / NITS / Q（不明は空）
    "text",  # 指摘の本文
    "response",  # 対応内容・返信
    "decision",  # adopted / rejected / 空
    "created_at",  # YYYY-MM-DD
    # 分類の結果
    "type",  # A1 / A2 / A3 / B1 / B2 / B3 / C1
    "topic",
    "root_cause",  # R1〜R6（A のときだけ）
    "classified_by",  # rules / llm:<model> / copilot / human
    "reason",  # 分類の理由（LLM・Copilot）
]
SEVERITIES = ["MUST", "WANT", "NITS", "Q"]
_TAG = re.compile(r"^\s*[\[【(（]?\s*(MUST|WANT|NITS|NIT|Q)\s*[\]】)）:：]\s*", re.I)


def empty() -> pd.DataFrame:
    return pd.DataFrame(columns=COLUMNS, dtype="string")


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """列をそろえ、すべて文字列（欠損は空文字）にする."""
    out = df.copy()
    for c in COLUMNS:
        if c not in out.columns:
            out[c] = ""
    out = out[COLUMNS].astype("string").fillna("")
    for c in COLUMNS:
        out[c] = out[c].str.strip()
    out["severity"] = out["severity"].str.upper().replace({"NIT": "NITS"})
    return out


def split_tag(text: str) -> tuple[str, str]:
    """先頭の [MUST] などの重要度タグを取り出す. → (重要度, 残りの本文)."""
    m = _TAG.match(text or "")
    if not m:
        return "", (text or "").strip()
    sev = m.group(1).upper()
    return ("NITS" if sev == "NIT" else sev), text[m.end() :].strip()


def load(path: Path) -> pd.DataFrame:
    if not path.is_file():
        return empty()
    df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    return normalize(df.map(safe.unquote))


def save(df: pd.DataFrame, path: Path) -> None:
    """一時ファイルに書いてから置き換える（途中で落ちても元のファイルは壊れない）."""
    path.parent.mkdir(parents=True, exist_ok=True)
    out = normalize(df).astype(object).map(safe.quote)
    tmp = path.with_name(path.name + ".tmp")
    out.to_csv(tmp, index=False, encoding="utf-8-sig", lineterminator="\n")
    tmp.replace(path)


class DuplicateIdError(ValueError):
    """取り込んだ指摘に、中身の違う同じ id がある（片方を黙って捨てないように止める）."""


def _check_duplicates(new: pd.DataFrame) -> pd.DataFrame:
    """中身まで同じ行は 1 件にまとめる. 中身の違う同じ id があれば DuplicateIdError."""
    new = new.drop_duplicates()
    dup = new[new["id"].duplicated(keep=False)]
    if not dup.empty:
        ids = ", ".join(sorted(set(dup["id"]))[:5])
        raise DuplicateIdError(
            f"id が重複しています（{len(set(dup['id']))} 件. 例: {ids}）。"
            "指摘表の番号の列・ファイル名（同じ名前のファイルを別のフォルダから取り込んでいないか）を確かめてください"
        )
    return new


def merge(existing: pd.DataFrame, new: pd.DataFrame, replace_scopes: Iterable[str] = ()) -> tuple[pd.DataFrame, int, int, int]:
    """id で追加・更新する. 分類の結果は、本文が変わっていなければ残す. → (結果, 追加件数, 更新件数, 削除件数).

    replace_scopes: 取り込み元の id の先頭（例: "table:指摘一覧:"）. 既存の指摘のうち、この先頭で始まり new に無いものは、
    取り込み元から消えた行として削除する（同じ表を取り込み直したとき、古い行が残って二重に数えないように）。
    """
    existing, new = normalize(existing), _check_duplicates(normalize(new))
    scopes = tuple(s for s in replace_scopes if s)
    new_ids = set(new["id"])
    gone = existing["id"].str.startswith(scopes) & ~existing["id"].isin(new_ids) if scopes else pd.Series(False, index=existing.index)
    deleted = int(gone.sum())
    existing = existing[~gone]
    if existing.empty:
        return new.reset_index(drop=True), len(new), 0, deleted
    old = existing.drop_duplicates("id", keep="last").set_index("id")
    added = updated = 0
    rows = []
    for _, r in new.iterrows():
        if r["id"] in old.index:
            prev = old.loc[r["id"]]
            if prev["text"] == r["text"]:
                for c in ("type", "topic", "root_cause", "classified_by", "reason"):
                    if not r[c]:
                        r[c] = prev[c]
            if any(r[c] != prev[c] for c in COLUMNS if c != "id"):
                updated += 1
        else:
            added += 1
        rows.append(r)
    keep = existing[~existing["id"].isin(new_ids)]
    return normalize(pd.concat([keep, pd.DataFrame(rows)], ignore_index=True)), added, updated, deleted
