"""指摘の共通形式と、その保存（CSV）.

どの取り込み元も、この列にそろえて data/findings.csv に書く。分類の結果も同じファイルの列に入る。
CSV は Excel で開けるように UTF-8（BOM 付き）で書く。
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

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
    return normalize(pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig"))


def save(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalize(df).to_csv(path, index=False, encoding="utf-8-sig", lineterminator="\n")


def merge(existing: pd.DataFrame, new: pd.DataFrame) -> tuple[pd.DataFrame, int, int]:
    """id で追加・更新する. 分類の結果は、本文が変わっていなければ残す. → (結果, 追加件数, 更新件数)."""
    existing, new = normalize(existing), normalize(new)
    if existing.empty:
        return new.drop_duplicates("id", keep="last").reset_index(drop=True), len(new.drop_duplicates("id")), 0
    old = existing.set_index("id")
    added = updated = 0
    rows = []
    for _, r in new.drop_duplicates("id", keep="last").iterrows():
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
    new_ids = {r["id"] for r in rows}
    keep = existing[~existing["id"].isin(new_ids)]
    return normalize(pd.concat([keep, pd.DataFrame(rows)], ignore_index=True)), added, updated
