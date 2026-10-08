"""LLM API（Anthropic Messages API 互換）による分類. 任意・有料.

通信・再試行・構造化出力・費用の計算は devtools_common.llm（LLMClient）に任せる。

- 実行前に必ず費用を見積もり、上限（max_cost_usd）を超えるなら実行しない. 見積もりには再試行の分の余裕
  （retry_margin. 既定 30%）を含める
- 実行中も実際にかかった費用（API が返す usage）を積算し、次の呼び出しで上限を超えそうなら残りの分類を止める
- 接続先は環境変数 ANTHROPIC_BASE_URL（未設定なら https://api.anthropic.com）。API キーは ANTHROPIC_API_KEY
  （ゲートウェイ向けに ANTHROPIC_AUTH_TOKEN も使える）
- 何度も送る長い「分類の定義」（prompts/classify.md と config/taxonomy.yaml・工程の目安）はシステムプロンプトにして
  プロンプトキャッシュを使う（cache_system）。指摘の本文だけを user に入れる
- 回答はツール 1 つ（JSON Schema）で受け取る（complete_json）。type・topic・root_cause は定義の値だけを許す（enum）
- 出力が max_tokens で切れたら、その回の結果は使わずに問題として報告する
- 送る本文は取り込み時に伏せたもの（mask.py）. 送る直前にもメールアドレス・電話番号と redact のパターンを伏せ直す
- 結果はキャッシュ（data/llm-cache.jsonl）に残し、同じ本文・同じプロンプト（テンプレートと定義）なら再送しない。
  プロンプトや定義を直すとキャッシュのキーが変わり、分類し直す
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from devtools_common.llm import Completion, LLMClient, LLMError, MaxTokensError, Price, SchemaValidationError, price_for

from ..taxonomy import Taxonomy
from . import prompt

log = logging.getLogger(__name__)

OUTPUT_TOKENS_PER_ITEM = 80  # {"id","type","topic","root_cause","reason"} 1 件の目安（ツールの入力として返る分を含む）
TOOL_OVERHEAD_TOKENS = 400  # ツールを使わせるときに API が足すシステムプロンプトとスキーマの目安（1 回ごと）
TOOL_NAME = "record_labels"
SCHEMA_VERSION = 1  # 回答のスキーマを変えたら上げる（結果のキャッシュのキーに入る）
DEFAULT_RETRY_MARGIN = 0.3  # 見積もりに足す再試行（スキーマ不一致の再送・通信の再試行）の余裕（割合）
COMPARE_MODELS = ["claude-haiku-4-5", "claude-sonnet-5-5", "claude-opus-5-5"]  # 見積もりの表に並べるモデル


def estimate_tokens(text: str) -> int:
    """トークン数の見積もり（安全側）. 日本語は 1 文字 ≒ 1 トークン、英数字は 4 文字 ≒ 1 トークンとして数える."""
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    return math.ceil((len(text) - ascii_chars) + ascii_chars / 4)


# ---------------------------------------------------------------- 料金


def price(model: str, conf: dict[str, Any]) -> Price | None:
    """モデルの料金. 共通の料金表（devtools_common.llm.PRICES）を使う.

    config/llm.yaml に pricing（旧形式）が残っていれば上書きとして使い、警告を出す。
    """
    override = (conf.get("pricing") or {}).get(model)
    if override:
        log.warning(
            "config/llm.yaml の pricing は非推奨です（料金は devtools_common.llm.PRICES の 1 か所で管理します）。"
            "%s は pricing の値で見積もります",
            model,
        )
        inp = float(override["input"])
        return Price(
            inp,
            float(override["output"]),
            float(override.get("cache_write", inp * 1.25)),
            float(override.get("cache_read", inp * 0.1)),
        )
    return price_for(model)


@dataclass
class Estimate:
    calls: int
    items: int
    input_tokens: int  # キャッシュを使わない入力（指摘・ツール・2 回目以降の定義も、キャッシュが効かない前提で数える）
    output_tokens: int
    cache_write_tokens: int = 0  # 工程ごとの最初の 1 回の定義（キャッシュの書き込み単価）

    def cost(self, p: Price) -> float:
        """安全側の見積もり（USD）. 2 回目以降の定義もキャッシュが効かない前提で、通常の入力単価で数える."""
        return (self.input_tokens * p.input + self.output_tokens * p.output + self.cache_write_tokens * p.cache_write) / 1_000_000

    def cost_with_margin(self, p: Price, conf: dict[str, Any]) -> float:
        """再試行の分の余裕（config/llm.yaml の retry_margin. 既定 30%）を足した見積もり. 上限と比べるのはこの値."""
        return self.cost(p) * (1 + retry_margin(conf))


def retry_margin(conf: dict[str, Any]) -> float:
    v = conf.get("retry_margin")
    return DEFAULT_RETRY_MARGIN if v is None else max(0.0, float(v))


def batches(df: pd.DataFrame, size: int) -> list[tuple[str, pd.DataFrame]]:
    out = []
    for phase, g in df.groupby("phase", sort=True):
        for start in range(0, len(g), size):
            out.append((str(phase), g.iloc[start : start + size]))
    return out


def estimate(df: pd.DataFrame, tax: Taxonomy, conf: dict[str, Any], prompt_dir: Path | None = None) -> Estimate:
    calls = inp = write = 0
    seen: set[str] = set()
    for phase, g in batches(df, int(conf.get("batch_size", 20))):
        system, user = prompt.build_parts(phase, g, tax, redact_patterns=conf.get("redact"), prompt_dir=prompt_dir)
        sys_tokens = estimate_tokens(system)
        if phase in seen:
            inp += sys_tokens
        else:
            write += sys_tokens
            seen.add(phase)
        inp += estimate_tokens(user) + estimate_tokens(json.dumps(schema(tax, phase, g["id"]), ensure_ascii=False)) + TOOL_OVERHEAD_TOKENS
        calls += 1
    return Estimate(calls, len(df), inp, len(df) * OUTPUT_TOKENS_PER_ITEM, write)


def cost_table(est: Estimate, conf: dict[str, Any]) -> str:
    lines = [
        f"見積もり: 指摘 {est.items} 件 / 呼び出し {est.calls} 回 / 入力 約 {est.input_tokens + est.cache_write_tokens:,} トークン"
        f"（うちキャッシュに書く定義 {est.cache_write_tokens:,}）/ 出力 約 {est.output_tokens:,} トークン",
        "",
        "| モデル | 費用の見積もり（USD） |",
        "|---|---|",
    ]
    models = [conf["model"], *(m for m in COMPARE_MODELS if not conf["model"].startswith(m))]
    for model in models:
        p = price(model, conf) if model == conf["model"] else price_for(model)
        mark = "（設定中）" if model == conf["model"] else ""
        lines.append(f"| {model}{mark} | {'料金表にありません' if p is None else f'${est.cost_with_margin(p, conf):.3f}'} |")
    lines += [
        "",
        f"見積もりには再試行の分の余裕（{retry_margin(conf):.0%}. config/llm.yaml の retry_margin）を含みます。"
        "実行中も実際の費用を数え、上限を超えそうなら残りを止めます。",
        "価格は devtools_common.llm.PRICES（確認日をコメントに記載）。使う前に公式の料金表で確かめてください。",
        "定義はプロンプトキャッシュを使うので、2 回目以降の呼び出しは実際にはこれより安くなります。",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- 回答のスキーマ


def schema(tax: Taxonomy, phase: str, ids: Any) -> dict[str, Any]:
    """回答の JSON Schema. type・topic・root_cause は定義の値だけ、id は送った指摘の id だけを許す."""
    label = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "enum": [str(i) for i in ids]},
            "type": {"type": "string", "enum": list(tax.types)},
            "topic": {"type": "string", "enum": ["", *tax.topics_for(phase)]},
            "root_cause": {"type": "string", "enum": ["", *tax.root_causes]},
            "reason": {"type": "string"},
        },
        "required": ["id", "type", "topic", "root_cause", "reason"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"labels": {"type": "array", "items": label}},
        "required": ["labels"],
        "additionalProperties": False,
    }


def prompt_hash(text: str) -> str:
    """プロンプト（テンプレート + 分類の定義 + 工程の目安）のハッシュ. 結果のキャッシュのキーに入れる."""
    return hashlib.sha256(f"schema-v{SCHEMA_VERSION}\n{text}".encode()).hexdigest()[:16]


# ---------------------------------------------------------------- クライアント


def make_client(conf: dict[str, Any]) -> LLMClient:
    """config/llm.yaml から LLMClient を作る. 接続先は ANTHROPIC_BASE_URL（LLMClient が読む）."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        from devtools_common.cli import UsageError

        raise UsageError("環境変数 ANTHROPIC_API_KEY（または ANTHROPIC_AUTH_TOKEN）が設定されていません（.env に書く）")
    return LLMClient(
        conf["model"],
        max_tokens=int(conf.get("max_output_tokens", 4000)),
        temperature=conf.get("temperature"),
        cache_system=True,
        max_retries=int(conf.get("max_retries", 3)),
    )


# ---------------------------------------------------------------- 実行


def _cache_key(tax: Taxonomy, model: str, phase: str, text: str, phash: str) -> str:
    return hashlib.sha256(f"{tax.version}\n{model}\n{phase}\n{phash}\n{text}".encode()).hexdigest()[:24]


def _load_cache(path: Path) -> dict[str, dict]:
    """結果のキャッシュを読む. 壊れた行（途中で止まって書きかけの最終行など）は読み飛ばして警告する.

    読み飛ばした行の指摘は分類し直す（キャッシュに無い扱い）。壊れた行が 1 行あるだけで以後の実行が
    すべて失敗しないようにする。
    """
    if not path.is_file():
        return {}
    out: dict[str, dict] = {}
    bad: list[int] = []
    for no, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            key = rec["key"]
            if not isinstance(key, str) or not isinstance(rec.get("label"), dict):
                raise TypeError("key / label の形が違います")
        except (ValueError, KeyError, TypeError):
            bad.append(no)
            continue
        out[key] = rec
    if bad:
        shown = ", ".join(str(n) for n in bad[:10]) + (" ほか" if len(bad) > 10 else "")
        log.warning("%s: 壊れた行 %d 行を読み飛ばしました（行 %s）。その指摘は分類し直します", path, len(bad), shown)
    return out


def _end_with_newline(path: Path) -> None:
    """書きかけの最終行（改行で終わらない）があれば改行を足す. 次の追記が壊れた行につながらないようにする."""
    if path.is_file() and path.stat().st_size:
        with path.open("rb") as fh:
            fh.seek(-1, os.SEEK_END)
            last = fh.read(1)
        if last != b"\n":
            with path.open("ab") as fh:
                fh.write(b"\n")


@dataclass
class RunResult:
    classified: int
    cached: int
    input_tokens: int
    output_tokens: int
    problems: list[str]
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    calls: int = 0
    stopped: bool = False  # 費用の上限で途中で止めた
    skipped: int = 0  # 止めたために分類しなかった件数
    error: str = ""  # 通信の失敗（再試行しても失敗した）で途中で止めたときの理由. 止めるまでの結果は out に入っている

    def cost(self, p: Price) -> float:
        return (
            self.input_tokens * p.input
            + self.output_tokens * p.output
            + self.cache_creation_input_tokens * p.cache_write
            + self.cache_read_input_tokens * p.cache_read
        ) / 1_000_000

    def add(self, usage: Any) -> None:
        self.calls += 1
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_creation_input_tokens += usage.cache_creation_input_tokens
        self.cache_read_input_tokens += usage.cache_read_input_tokens


def _prompt_hashes(df: pd.DataFrame, tax: Taxonomy, conf: dict[str, Any], prompt_dir: Path | None) -> dict[str, str]:
    """工程 → プロンプトのハッシュ. 指摘を 0 件にして組み立てた全文（テンプレート + 定義 + 工程の目安）から作る."""
    return {
        str(phase): prompt_hash(prompt.build(str(phase), df.iloc[0:0], tax, redact_patterns=conf.get("redact"), prompt_dir=prompt_dir))
        for phase in sorted(set(df["phase"]))
    }


def _would_exceed(res: RunResult, max_cost: float | None, p: Price | None) -> bool:
    """次の呼び出しで上限を超えそうか. 1 回あたりの実際の費用の平均を、次の 1 回の費用とみなす."""
    if max_cost is None or p is None or res.calls == 0:
        return False  # 最初の 1 回は、実行前の見積もり（余裕込み）で上限と比べてある
    spent = res.cost(p)
    return spent + spent / res.calls > max_cost


def run(
    df: pd.DataFrame,
    tax: Taxonomy,
    conf: dict[str, Any],
    client: LLMClient,
    cache_path: Path,
    prompt_dir: Path | None = None,
    *,
    max_cost: float | None = None,
    price: Price | None = None,
) -> tuple[pd.DataFrame, RunResult]:
    """分類する. max_cost と price を渡すと、実際の費用で上限を超えそうになったら残りを止める（res.stopped）."""
    model = conf["model"]
    cache = _load_cache(cache_path)
    hashes = _prompt_hashes(df, tax, conf, prompt_dir)
    out = df.copy()
    res = RunResult(0, 0, 0, 0, [])
    # キャッシュにあるものは送らない
    todo_idx = []
    for i, r in out.iterrows():
        rec = cache.get(_cache_key(tax, model, r["phase"], r["text"], hashes[str(r["phase"])]))
        if rec:
            out, probs = prompt.apply(out, [{**rec["label"], "id": r["id"]}], tax, f"llm:{model}")
            res.cached += 1 if not probs else 0
            if not probs:
                continue
        todo_idx.append(i)
    todo = out.loc[todo_idx]
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    _end_with_newline(cache_path)
    with cache_path.open("a", encoding="utf-8") as fh:
        for phase, g in batches(todo, int(conf.get("batch_size", 20))):
            if res.error:
                res.skipped += len(g)
                continue
            if res.stopped or _would_exceed(res, max_cost, price):
                res.stopped = True
                res.skipped += len(g)
                continue
            system, user = prompt.build_parts(phase, g, tax, redact_patterns=conf.get("redact"), prompt_dir=prompt_dir)
            user += f"\n\n結果は {TOOL_NAME} ツールの labels に、上のすべての指摘について 1 件ずつ入れて返してください。"
            sch = schema(tax, phase, g["id"])
            labels: list[dict] = []
            for attempt in range(2):  # スキーマに合わない回答は 1 回だけ再試行
                if attempt and _would_exceed(res, max_cost, price):
                    res.stopped = True
                    res.skipped += len(g)
                    break
                try:
                    with _track_usage(client, res):
                        data = client.complete_json(user, schema=sch, name=TOOL_NAME, system=system)
                except SchemaValidationError as e:
                    res.problems.append(f"{phase} の {len(g)} 件: 回答が定義に合いません（{e}）")
                    continue
                except MaxTokensError:
                    res.problems.append(
                        f"{phase} の {len(g)} 件: 出力が max_tokens で切れました。"
                        "config/llm.yaml の batch_size を減らすか max_output_tokens を増やしてください"
                    )
                    break
                except (LLMError, OSError, ValueError) as e:
                    # 通信の失敗（API のエラー・接続できない・応答が読めない）. 残りは送らずに止め、
                    # ここまでの結果を呼び出し側で保存させる（例外のまま抜けると分類できた分まで保存されない）
                    res.error = f"{type(e).__name__}: {e}"
                    res.skipped += len(g)
                    log.warning("LLM API の呼び出しに失敗したため、残りの分類を止めます（%s）", res.error)
                    break
                labels = list(data["labels"])
                break
            if res.error:
                continue
            out, probs = prompt.apply(out, labels, tax, f"llm:{model}")
            res.problems += probs
            bad_ids = {p.split(":", 1)[0] for p in probs}
            by_id = {str(lab.get("id")): lab for lab in labels}
            for _, r in g.iterrows():
                lab = by_id.get(r["id"])
                if lab and r["id"] not in bad_ids:
                    rec = {
                        "key": _cache_key(tax, model, phase, r["text"], hashes[phase]),
                        "label": {k: lab.get(k, "") for k in ("type", "topic", "root_cause", "reason")},
                    }
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    res.classified += 1
    log.info(
        "llm usage: calls=%d in=%d out=%d cache_write=%d cache_read=%d",
        res.calls,
        res.input_tokens,
        res.output_tokens,
        res.cache_creation_input_tokens,
        res.cache_read_input_tokens,
    )
    return out, res


@contextlib.contextmanager
def _track_usage(client: LLMClient, res: RunResult) -> Iterator[None]:
    """complete_json は結果だけを返すので、中で呼ぶ complete() の Completion からトークン数を足す."""
    orig = client.complete

    def complete(*a: Any, **kw: Any) -> Completion:
        try:
            comp = orig(*a, **kw)
        except MaxTokensError as e:
            res.add(e.completion.usage)
            raise
        res.add(comp.usage)
        return comp

    client.complete = complete  # type: ignore[method-assign]
    try:
        yield
    finally:
        del client.complete
