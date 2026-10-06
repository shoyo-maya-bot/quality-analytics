"""office-review のレビュー結果（reviews/*.md）と判定（data/history.jsonl）を取り込む.

形式は office-review が書くもの（review_harness/results.py と .github/prompts/review.prompt.md）に合わせる。
この約束は tests/contract/office-review/ の実物の形式で確かめている（README の「office-review との形式の約束」）。

レビュー結果の表の列: No / check_id / 重要度 / ロール / 箇所 / 指摘 / 根拠 / 無効となるケースの確認（表の中の | は \\|）
判定の行（data/history.jsonl）:
  新しい形式 {"key": "<ファイル名>#<No>", "fingerprint": "fp:…", "decision": "adopted" | "rejected", ...}
  旧形式     {"key": "<ファイル名>#<No>", "decision": ..., "check_id": ..., "finding": ...}（fingerprint なし）
  office-review と同じく、判定は指摘の指紋（対象・箇所・指摘の本文のハッシュ）で結び付け、同じ指紋は後の行が有効。
  旧形式の行は、その No にいまある指摘が同じとき（記録にある本文と check_id が一致するとき）だけ結び付ける
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import unicodedata
from pathlib import Path

import pandas as pd
import yaml

from ..model import normalize

DOC_TYPE_PHASE = {"requirements": "RD", "rd": "RD"}  # それ以外の設計書は BD とみなす
COLUMNS = ["No", "check_id", "重要度", "ロール", "箇所", "指摘", "根拠", "無効となるケースの確認"]


def _front_matter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    return (yaml.safe_load(text[3:end]) or {}) if end > 0 else {}


def _cells(line: str) -> list[str]:
    """表の 1 行をセルに分ける. office-review と同じく、\\| だけを | に戻す（ほかの \\ はそのまま）."""
    body = line.strip()
    body = body[1:] if body.startswith("|") else body
    body = body[:-1] if body.endswith("|") and not body.endswith("\\|") else body
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", body)]


def parse_file(md: Path) -> tuple[dict, list[dict[str, str]]]:
    """レビュー結果 1 ファイル → (Front Matter, 行の list). 行は表のすべての列（COLUMNS）を持つ dict."""
    text = md.read_text(encoding="utf-8")
    meta = _front_matter(text)
    rows: list[dict[str, str]] = []
    header: list[str] | None = None
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            header = None if line.strip() else header
            continue
        cells = _cells(line)
        if header is None:
            if "指摘" in cells and "No" in cells:
                header = cells
            continue
        if set(line.replace("|", "").strip()) <= set("-: "):
            continue
        r = dict(zip(header, cells, strict=False))
        no = r.get("No", "")
        r["No"] = str(int(no)) if no.strip().isdigit() else no  # office-review は No を数字として読む（key は「#1」）
        rows.append({c: r.get(c, "") for c in [*COLUMNS, *(h for h in header if h not in COLUMNS)]})
    return meta, rows


def normalize_text(text: str) -> str:
    """指紋用の正規化（office-review と同じ）: NFKC、空白の連続を 1 つに、前後の空白を除く、英字を小文字に."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip().lower()


def fingerprint(target: str, location: str, finding: str) -> str:
    """指摘の指紋（office-review の ReviewItem.fingerprint と同じ）. target が空ならファイル名を渡す."""
    parts = [normalize_text(target), normalize_text(location), normalize_text(finding)]
    return "fp:" + hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def _resolve(records: list[dict], items: dict[str, tuple[str, dict[str, str]]]) -> dict[str, str]:
    """判定を指摘（key = ファイル名#No）に結び付ける. items: key → (指紋, 行). → key → 判定.

    office-review の history.resolve と同じ規則: 指紋がある行はその指紋に、旧形式の行は同じ指摘のときだけ。後の行が勝つ。
    """
    by_fp: dict[str, str] = {}
    for rec in records:
        fp = rec.get("fingerprint")
        if not fp:
            item = items.get(str(rec.get("key", "")))
            if item is None:
                continue
            fp, row = item
            if "finding" in rec and normalize_text(str(rec["finding"])) != normalize_text(row["指摘"]):
                continue  # 再レビューで No がずれた等: 別の指摘の判定
            if "check_id" in rec and rec["check_id"] != row["check_id"]:
                continue
        by_fp[str(fp)] = str(rec.get("decision", ""))
    return {key: by_fp[fp] for key, (fp, _) in items.items() if fp in by_fp}


def read(reviews_dir: Path, history: Path | None = None) -> pd.DataFrame:
    records: list[dict] = []
    if history and history.is_file():
        records = [json.loads(line) for line in history.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = []
    items: dict[str, tuple[str, dict[str, str]]] = {}
    for md in sorted(reviews_dir.glob("*.md")):
        meta, found = parse_file(md)
        target = Path(str(meta.get("target", md.stem))).stem
        doc_type = str(meta.get("type", ""))
        reviewed = meta.get("reviewed_at", "")
        reviewed = reviewed.isoformat() if isinstance(reviewed, dt.date) else str(reviewed)
        for r in found:
            key = f"{md.name}#{r['No']}"
            items[key] = (fingerprint(str(meta.get("target", "")) or md.name, r["箇所"], r["指摘"]), r)
            rows.append(
                {
                    "id": f"office-review:{key}",
                    "source": "office-review",
                    "project": "office-review",
                    "phase": DOC_TYPE_PHASE.get(doc_type.lower(), "BD"),
                    "deliverable": target,
                    "reviewer": f"AI:{r['ロール']}",
                    "severity": r["重要度"],
                    "text": r["指摘"],
                    "response": "",
                    "decision": "",
                    "created_at": reviewed[:10],
                }
            )
    decisions = _resolve(records, items)
    for row in rows:
        row["decision"] = decisions.get(row["id"].removeprefix("office-review:"), "")
    return normalize(pd.DataFrame(rows))
