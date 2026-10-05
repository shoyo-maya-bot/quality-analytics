"""LLM API（Anthropic Messages API）による分類. 任意・有料.

- 実行前に必ず費用を見積もり、上限（max_cost_usd）を超えるなら実行しない
- 送る本文は取り込み時に伏せたもの（mask.py）. 送る直前にもメールアドレス・電話番号と redact のパターンを伏せ直す
- 結果はキャッシュ（data/llm-cache.jsonl）に残し、同じ本文・同じ分類定義なら再送しない（再実行で費用がかからない）
"""

from __future__ import annotations

import hashlib
import json
import math
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from ..taxonomy import Taxonomy
from . import prompt

API_URL = "https://api.anthropic.com/v1/messages"
OUTPUT_TOKENS_PER_ITEM = 60  # {"id","type","topic","root_cause","reason"} 1 件の目安
Send = Callable[[str, str, int], tuple[str, int, int]]  # (model, prompt, max_tokens) → (回答, 入力トークン, 出力トークン)


def estimate_tokens(text: str) -> int:
    """トークン数の見積もり（安全側）. 日本語は 1 文字 ≒ 1 トークン、英数字は 4 文字 ≒ 1 トークンとして数える."""
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    return math.ceil((len(text) - ascii_chars) + ascii_chars / 4)


@dataclass
class Estimate:
    calls: int
    items: int
    input_tokens: int
    output_tokens: int

    def cost(self, price: dict[str, float]) -> float:
        return (self.input_tokens * price["input"] + self.output_tokens * price["output"]) / 1_000_000


def batches(df: pd.DataFrame, size: int) -> list[tuple[str, pd.DataFrame]]:
    out = []
    for phase, g in df.groupby("phase", sort=True):
        for start in range(0, len(g), size):
            out.append((str(phase), g.iloc[start : start + size]))
    return out


def estimate(df: pd.DataFrame, tax: Taxonomy, conf: dict[str, Any], prompt_dir: Path | None = None) -> Estimate:
    calls = inp = 0
    for phase, g in batches(df, int(conf.get("batch_size", 20))):
        inp += estimate_tokens(prompt.build(phase, g, tax, redact_patterns=conf.get("redact"), prompt_dir=prompt_dir))
        calls += 1
    return Estimate(calls, len(df), inp, len(df) * OUTPUT_TOKENS_PER_ITEM)


def cost_table(est: Estimate, conf: dict[str, Any]) -> str:
    lines = [
        f"見積もり: 指摘 {est.items} 件 / 呼び出し {est.calls} 回 / 入力 約 {est.input_tokens:,} トークン / 出力 約 {est.output_tokens:,} トークン",
        "",
        "| モデル | 費用の見積もり（USD） | Batches API 利用時 |",
        "|---|---|---|",
    ]
    disc = float(conf.get("batch_discount", 0.5))
    for model, price in (conf.get("pricing") or {}).items():
        c = est.cost(price)
        mark = "（設定中）" if model == conf.get("model") else ""
        lines.append(f"| {model}{mark} | ${c:.3f} | ${c * disc:.3f} |")
    lines += ["", "価格は config/llm.yaml の pricing（確認日をコメントに記載）。使う前に公式の料金表で確かめてください。"]
    return "\n".join(lines)


def http_send(api_key: str) -> Send:
    def send(model: str, text: str, max_tokens: int) -> tuple[str, int, int]:
        body = json.dumps({"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": text}]}).encode()
        req = urllib.request.Request(
            API_URL,
            data=body,
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as res:  # noqa: S310 — 固定の https URL
            data = json.loads(res.read().decode("utf-8"))
        answer = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        usage = data.get("usage") or {}
        return answer, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))

    return send


def _cache_key(tax: Taxonomy, model: str, phase: str, text: str) -> str:
    return hashlib.sha256(f"{tax.version}\n{model}\n{phase}\n{text}".encode()).hexdigest()[:24]


def _load_cache(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            out[rec["key"]] = rec
    return out


@dataclass
class RunResult:
    classified: int
    cached: int
    input_tokens: int
    output_tokens: int
    problems: list[str]


def run(
    df: pd.DataFrame, tax: Taxonomy, conf: dict[str, Any], send: Send, cache_path: Path, prompt_dir: Path | None = None
) -> tuple[pd.DataFrame, RunResult]:
    model = conf["model"]
    cache = _load_cache(cache_path)
    out = df.copy()
    res = RunResult(0, 0, 0, 0, [])
    # キャッシュにあるものは送らない
    todo_idx = []
    for i, r in out.iterrows():
        rec = cache.get(_cache_key(tax, model, r["phase"], r["text"]))
        if rec:
            out, probs = prompt.apply(out, [{**rec["label"], "id": r["id"]}], tax, f"llm:{model}")
            res.cached += 1 if not probs else 0
            if not probs:
                continue
        todo_idx.append(i)
    todo = out.loc[todo_idx]
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("a", encoding="utf-8") as fh:
        for phase, g in batches(todo, int(conf.get("batch_size", 20))):
            text = prompt.build(phase, g, tax, redact_patterns=conf.get("redact"), prompt_dir=prompt_dir)
            labels: list[dict] = []
            for _attempt in range(2):  # 形式の誤りは 1 回だけ再試行
                answer, it, ot = send(model, text, int(conf.get("max_output_tokens", 4000)))
                res.input_tokens += it
                res.output_tokens += ot
                try:
                    labels = prompt.parse(answer)
                    break
                except ValueError as e:
                    res.problems.append(f"{phase} の {len(g)} 件: {e}")
            out, probs = prompt.apply(out, labels, tax, f"llm:{model}")
            res.problems += probs
            bad_ids = {p.split(":", 1)[0] for p in probs}
            by_id = {str(lab.get("id")): lab for lab in labels}
            for _, r in g.iterrows():
                lab = by_id.get(r["id"])
                if lab and r["id"] not in bad_ids:
                    rec = {
                        "key": _cache_key(tax, model, phase, r["text"]),
                        "label": {k: lab.get(k, "") for k in ("type", "topic", "root_cause", "reason")},
                    }
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    res.classified += 1
    return out, res
