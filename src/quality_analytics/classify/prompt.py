"""分類のプロンプトを組み立て、LLM の回答（JSON）を検証する. API 呼び出しと Copilot 用の書き出しで共通."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pandas as pd

from ..taxonomy import Taxonomy

PROMPTS = Path(__file__).resolve().parents[3] / "prompts"


def _prompt_dir(prompt_dir: Path | None) -> Path:
    return prompt_dir or PROMPTS


def redact(text: str, patterns: list[str]) -> str:
    for p in patterns:
        text = re.sub(p, "＊＊＊", text)
    return text


def build(
    phase: str, items: pd.DataFrame, tax: Taxonomy, *, redact_patterns: list[str] | None = None, prompt_dir: Path | None = None
) -> str:
    d = _prompt_dir(prompt_dir)
    template = (d / "classify.md").read_text(encoding="utf-8")
    guide_file = d / f"phase-{phase}.md"
    guide = guide_file.read_text(encoding="utf-8").strip() if guide_file.is_file() else "- 工程に固有の目安はありません"
    types = "\n".join(f"- {k}（{tax.groups[v['group']]['name']}）{v['name']}: {v['description']}" for k, v in tax.types.items())
    topics = "\n".join(f"- {t}" for t in tax.topics_for(phase))
    causes = "\n".join(f"- {k} {v['name']}: {v['description']}" for k, v in tax.root_causes.items())
    payload = [
        {"id": r["id"], "severity": r["severity"], "deliverable": r["deliverable"], "text": redact(r["text"], redact_patterns or [])}
        for _, r in items.iterrows()
    ]
    return template.format(
        types=types,
        topics=topics,
        root_causes=causes,
        phase=phase or "（不明）",
        phase_guide=guide,
        items=json.dumps(payload, ensure_ascii=False, indent=1),
    )


def parse(answer: str) -> list[dict[str, Any]]:
    """回答から JSON の配列を取り出す（コードブロックで囲まれていてもよい）."""
    m = re.search(r"\[.*\]", answer, re.S)
    if not m:
        raise ValueError("回答に JSON の配列がありません")
    data = json.loads(m.group(0))
    if not isinstance(data, list):
        raise ValueError("回答が配列ではありません")
    return [d for d in data if isinstance(d, dict)]


def apply(df: pd.DataFrame, labels: list[dict[str, Any]], tax: Taxonomy, by: str) -> tuple[pd.DataFrame, list[str]]:
    """分類の結果を反映する. 定義に合わないものは反映せず、問題として返す."""
    out = df.copy()
    index = {v: i for i, v in out["id"].items()}
    problems: list[str] = []
    for lab in labels:
        i = index.get(str(lab.get("id", "")))
        if i is None:
            problems.append(f"{lab.get('id')}: 対象の指摘がありません")
            continue
        t, topic, cause = str(lab.get("type", "")), str(lab.get("topic", "") or ""), str(lab.get("root_cause", "") or "")
        bad = tax.check(t, topic, cause, out.loc[i, "phase"])
        if bad:
            problems.append(f"{lab.get('id')}: {'; '.join(bad)}")
            continue
        out.loc[i, ["type", "topic", "root_cause", "classified_by", "reason"]] = [t, topic, cause, by, str(lab.get("reason", ""))]
    return out, problems
