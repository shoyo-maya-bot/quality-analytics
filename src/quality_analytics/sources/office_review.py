"""office-review のレビュー結果（reviews/*.md）と判定（data/history.jsonl）を取り込む.

読み込みは devtools-common の共通部品（devtools_common.review_results）に任せ、office-review と同じ規則で読む:
- レビュー結果: UTF-8（BOM 可）、Front Matter（target・type・reviewed_at）、表の列は COLUMNS の 8 列（この順）。
  セルの区切りはエスケープされていない `|`（`\\|` はセルの中の `|`、`\\\\|` は「文字の `\\`」の後ろの区切り）。
  列数が違う行・No が数字でない行は、行番号付きで警告して飛ばす
- 判定（history.jsonl）: 1 行 1 判定、後の行が有効。壊れた行（JSON でない・decision（adopted / rejected）や
  check_id が無い）は、行番号付きで警告して飛ばす。判定は指摘の指紋（対象・箇所・指摘の本文のハッシュ）で結び付け、
  旧形式の行（fingerprint なし）は、その No にいまある指摘が同じとき（本文と check_id が一致）だけ結び付ける

この約束は tests/contract/office-review/ の実物の形式で確かめている（README の「office-review との形式の約束」）。
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd
from devtools_common import review_results as rr

from ..model import normalize

DOC_TYPE_PHASE = {"requirements": "RD", "rd": "RD"}  # それ以外の設計書は BD とみなす
COLUMNS = rr.COLUMNS
fingerprint = rr.fingerprint
SCOPE = "office-review:"  # 取り込んだ指摘の id の先頭（取り込み直したとき、今回に無い行を削除する範囲）


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr)


def load_items(reviews_dir: Path, warn: Callable[[str], Any] | None = None) -> list[rr.ReviewItem]:
    """フォルダのレビュー結果（*.md、ファイル名順）の指摘. 読めない行は行番号付きで warn に渡して飛ばす."""
    warn = warn or _stderr
    items: list[rr.ReviewItem] = []
    for md in sorted(Path(reviews_dir).glob("*.md")):
        got, problems = rr.parse_review_file(md)
        for p in problems:
            if p.rule_id == "lint.table" and ":" in p.location:  # 行の誤り（表が無いファイルは指摘 0 件として黙って飛ばす）
                warn(f"警告: {p.location} を飛ばしました（{p.message}）")
        items += got
    return items


def read(reviews_dir: Path, history: Path | None = None, warn: Callable[[str], Any] | None = None) -> pd.DataFrame:
    items = load_items(reviews_dir, warn)
    records = rr.load_history(history, warn or _stderr) if history else []
    decisions = rr.resolve_history(records, items).decisions
    rows = []
    for it in items:
        target = Path(it.target).stem if it.target else Path(it.file).stem
        rows.append(
            {
                "id": f"{SCOPE}{it.key}",
                "source": "office-review",
                "project": "office-review",
                "phase": DOC_TYPE_PHASE.get(it.doc_type.lower(), "BD"),
                "deliverable": target,
                "reviewer": f"AI:{it.role}",
                "severity": it.severity,
                "text": it.finding,
                "response": "",
                "decision": str(decisions.get(it.fingerprint, {}).get("decision", "")),
                "created_at": it.reviewed_at[:10],
            }
        )
    return normalize(pd.DataFrame(rows))
