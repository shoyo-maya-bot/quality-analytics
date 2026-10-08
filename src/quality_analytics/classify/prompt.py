"""分類のプロンプトを組み立て、LLM の回答（JSON）を検証する. API 呼び出しと Copilot 用の書き出しで共通."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pandas as pd

from ..mask import Masker
from ..taxonomy import Taxonomy

PROMPTS = Path(__file__).resolve().parents[3] / "prompts"


def _prompt_dir(prompt_dir: Path | None) -> Path:
    return prompt_dir or PROMPTS


def redact(text: str, patterns: list[str]) -> str:
    """互換用. メールアドレス・電話番号（全角を含む）と patterns を伏せる."""
    return Masker(patterns=patterns).text(text)


def build(
    phase: str,
    items: pd.DataFrame,
    tax: Taxonomy,
    *,
    redact_patterns: list[str] | None = None,
    prompt_dir: Path | None = None,
    masker: Masker | None = None,
) -> str:
    """プロンプトを組み立てる. 指摘は読み込み時に伏せてあるが、送る直前にも成果物名と本文を伏せ直す（二重の守り）."""
    m = masker or Masker(patterns=redact_patterns or [])
    d = _prompt_dir(prompt_dir)
    template = (d / "classify.md").read_text(encoding="utf-8")
    guide_file = d / f"phase-{phase}.md"
    guide = guide_file.read_text(encoding="utf-8").strip() if guide_file.is_file() else "- 工程に固有の目安はありません"
    types = "\n".join(f"- {k}（{tax.groups[v['group']]['name']}）{v['name']}: {v['description']}" for k, v in tax.types.items())
    topics = "\n".join(f"- {t}" for t in tax.topics_for(phase))
    causes = "\n".join(f"- {k} {v['name']}: {v['description']}" for k, v in tax.root_causes.items())
    payload = [
        {"id": r["id"], "severity": r["severity"], "deliverable": m.text(r["deliverable"]), "text": m.text(r["text"])}
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


ITEMS_HEADING = "\n## 指摘\n"


def build_parts(
    phase: str,
    items: pd.DataFrame,
    tax: Taxonomy,
    *,
    redact_patterns: list[str] | None = None,
    prompt_dir: Path | None = None,
    masker: Masker | None = None,
) -> tuple[str, str]:
    """API 用に (分類の定義, 指摘) に分ける. 定義は工程ごとに同じなので、システムプロンプトにしてキャッシュする.

    build() の結果を最後の「## 指摘」の見出しで分ける（見出しから後ろが指摘）。見出しが無いテンプレートは全体を指摘として送る。
    """
    text = build(phase, items, tax, redact_patterns=redact_patterns, prompt_dir=prompt_dir, masker=masker)
    at = text.rfind(ITEMS_HEADING)
    if at < 0:
        return "", text
    return text[:at].rstrip(), text[at + 1 :]


def parse(answer: str) -> list[dict[str, Any]]:
    """回答から JSON の配列を取り出す（コードブロックで囲まれていてもよい）."""
    m = re.search(r"\[.*\]", answer, re.S)
    if not m:
        raise ValueError("回答に JSON の配列がありません")
    data = json.loads(m.group(0))
    if not isinstance(data, list):
        raise ValueError("回答が配列ではありません")
    return [d for d in data if isinstance(d, dict)]


def apply_detailed(df: pd.DataFrame, labels: list[dict[str, Any]], tax: Taxonomy, by: str) -> tuple[pd.DataFrame, list[tuple[str, str]]]:
    """分類の結果を反映する. 定義に合わないものは反映せず、(id, 理由) の組として返す.

    id は「table:指摘一覧:1」のように「:」を含むので、問題の文字列から id を取り出さずにこの組を使う。
    """
    out = df.copy()
    index = {v: i for i, v in out["id"].items()}
    problems: list[tuple[str, str]] = []
    for lab in labels:
        lid = str(lab.get("id", ""))
        i = index.get(lid)
        if i is None:
            problems.append((lid, "対象の指摘がありません"))
            continue
        t, topic, cause = str(lab.get("type", "")), str(lab.get("topic", "") or ""), str(lab.get("root_cause", "") or "")
        bad = tax.check(t, topic, cause, out.loc[i, "phase"])
        if bad:
            problems.append((lid, "; ".join(bad)))
            continue
        out.loc[i, ["type", "topic", "root_cause", "classified_by", "reason"]] = [t, topic, cause, by, str(lab.get("reason", ""))]
    return out, problems


def apply(df: pd.DataFrame, labels: list[dict[str, Any]], tax: Taxonomy, by: str) -> tuple[pd.DataFrame, list[str]]:
    """分類の結果を反映する. 定義に合わないものは反映せず、問題（「id: 理由」の文字列）として返す."""
    out, problems = apply_detailed(df, labels, tax, by)
    return out, [f"{i}: {msg}" for i, msg in problems]
