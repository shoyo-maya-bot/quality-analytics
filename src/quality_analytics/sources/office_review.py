"""office-review のレビュー結果（reviews/*.md）と判定（data/history.jsonl）を取り込む.

レビュー結果の表の列: No / check_id / 重要度 / ロール / 箇所 / 指摘 / 根拠 / 無効となるケースの確認
判定の行: {"key": "<ファイル名>#<No>", "decision": "adopted" | "rejected", ...}
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import yaml

from ..model import normalize

DOC_TYPE_PHASE = {"requirements": "RD", "rd": "RD"}  # それ以外の設計書は BD とみなす


def _front_matter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    return (yaml.safe_load(text[3:end]) or {}) if end > 0 else {}


def _cells(line: str) -> list[str]:
    body = line.strip().strip("|")
    out, cur, esc = [], "", False
    for ch in body:
        if esc:
            cur += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == "|":
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    out.append(cur.strip())
    return out


def read(reviews_dir: Path, history: Path | None = None) -> pd.DataFrame:
    decisions: dict[str, str] = {}
    if history and history.is_file():
        for line in history.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                decisions[rec["key"]] = rec.get("decision", "")
    rows = []
    for md in sorted(reviews_dir.glob("*.md")):
        text = md.read_text(encoding="utf-8")
        meta = _front_matter(text)
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
            key = f"{md.name}#{r.get('No', '')}"
            target = Path(str(meta.get("target", md.stem))).stem
            doc_type = str(meta.get("type", ""))
            rows.append(
                {
                    "id": f"office-review:{key}",
                    "source": "office-review",
                    "project": "office-review",
                    "phase": DOC_TYPE_PHASE.get(doc_type.lower(), "BD"),
                    "deliverable": target,
                    "reviewer": f"AI:{r.get('ロール', '')}",
                    "severity": r.get("重要度", ""),
                    "text": r.get("指摘", ""),
                    "response": "",
                    "decision": decisions.get(key, ""),
                    "created_at": str(meta.get("reviewed_at", ""))[:10],
                }
            )
    return normalize(pd.DataFrame(rows))
