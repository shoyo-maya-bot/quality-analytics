"""Copilot Chat（追加費用なし）で分類するための、プロンプトの書き出しと回答の取り込み.

1. `classify --mode export` で output/prompts/*.md を作る（工程ごと・batch_size 件ずつ）
2. 各ファイルの中身を Copilot Chat に貼り、回答の JSON を output/labels/*.json に保存する
3. `classify --mode import --labels output/labels` で取り込む（定義に合わない分類は取り込まず一覧にする）
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from ..taxonomy import Taxonomy
from . import llm, prompt


def export(df: pd.DataFrame, tax: Taxonomy, conf: dict[str, Any], out_dir: Path, prompt_dir: Path | None = None) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for n, (phase, g) in enumerate(llm.batches(df, int(conf.get("batch_size", 20))), start=1):
        p = out_dir / f"classify_{n:03d}_{phase or 'NA'}.md"
        p.write_text(prompt.build(phase, g, tax, redact_patterns=conf.get("redact"), prompt_dir=prompt_dir), encoding="utf-8", newline="\n")
        written.append(p)
    return written


def read_labels(path: Path) -> list[dict[str, Any]]:
    files = sorted([*path.glob("*.json"), *path.glob("*.jsonl")]) if path.is_dir() else [path]
    labels: list[dict[str, Any]] = []
    for f in files:
        text = f.read_text(encoding="utf-8-sig")
        if f.suffix == ".jsonl":
            labels += [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            labels += prompt.parse(text)
    return labels
