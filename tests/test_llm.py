"""LLM API による分類（devtools_common.llm）のテスト. 通信はしない（FakeLLM で応答を再生する）."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pandas as pd
import pytest
import yaml
from devtools_common.llm import FakeLLM, LLMClient, validate
from fakes import tool_reply

from quality_analytics import model, taxonomy
from quality_analytics.classify import llm

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
TAX = taxonomy.load(CONFIG / "taxonomy.yaml")
CONF = yaml.safe_load((CONFIG / "llm.yaml").read_text(encoding="utf-8"))

GOOD = [
    {"id": "1", "type": "A1", "topic": "API・IF", "root_cause": "R2", "reason": "桁が無い"},
    {"id": "2", "type": "B1", "topic": "", "root_cause": "", "reason": "誤字"},
]


def _df() -> pd.DataFrame:
    return model.normalize(
        pd.DataFrame(
            [
                {"id": "1", "phase": "BD", "severity": "MUST", "text": "連絡先 taro@example.com の桁が書かれていない"},
                {"id": "2", "phase": "BD", "severity": "NITS", "text": "誤字"},
            ]
        )
    )


def _client(responses, **kw) -> FakeLLM:
    return FakeLLM(responses, model=CONF["model"], cache_system=True, **kw)


def test_schema_valid_response_is_applied_with_cached_definitions(tmp_path: Path):
    client = _client([tool_reply(GOOD, usage={"input_tokens": 300, "output_tokens": 80, "cache_creation_input_tokens": 2000})])
    out, res = llm.run(_df(), TAX, CONF, client, tmp_path / "cache.jsonl", ROOT / "prompts")
    assert list(out["type"]) == ["A1", "B1"] and res.problems == [] and res.classified == 2
    assert (res.calls, res.input_tokens, res.output_tokens, res.cache_creation_input_tokens) == (1, 300, 80, 2000)
    req = client.requests[0]
    # 長い定義はキャッシュするシステムプロンプトに、指摘は user に
    assert req["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "分類の定義" in req["system"][0]["text"] and "taro" not in req["system"][0]["text"]
    assert "## 指摘" in req["messages"][0]["content"] and "taro@example.com" not in req["messages"][0]["content"]
    # 構造化出力: ツール 1 つを強制し、type・topic・root_cause は定義の値だけ
    assert req["tool_choice"] == {"type": "tool", "name": llm.TOOL_NAME}
    item = req["tools"][0]["input_schema"]["properties"]["labels"]["items"]["properties"]
    assert item["type"]["enum"] == list(TAX.types)
    assert item["topic"]["enum"] == ["", *TAX.topics_for("BD")] and "業務要件" not in item["topic"]["enum"]
    assert item["root_cause"]["enum"] == ["", *TAX.root_causes]
    assert item["id"]["enum"] == ["1", "2"]
    # 実際の費用は共通の料金表で計算する
    assert res.cost(llm.price(CONF["model"], CONF)) > 0


@pytest.mark.parametrize(
    "bad",
    [
        {"id": "1", "type": "Z9", "topic": "", "root_cause": "", "reason": ""},  # 定義に無い type
        {"id": "1", "type": "A1", "topic": "業務要件", "root_cause": "R2", "reason": ""},  # BD の観点ではない
        {"id": "1", "type": "A1", "topic": "API・IF", "root_cause": "R99", "reason": ""},  # 定義に無い真因
        {"id": "9", "type": "A1", "topic": "API・IF", "root_cause": "R2", "reason": ""},  # 送っていない id
    ],
)
def test_invalid_enum_is_rejected(tmp_path: Path, bad: dict):
    with pytest.raises(Exception, match="does not match the schema"):
        validate({"labels": [bad]}, llm.schema(TAX, "BD", ["1", "2"]))
    # 2 回ともスキーマに合わない → 反映もキャッシュもしない
    cache = tmp_path / "cache.jsonl"
    client = _client([tool_reply([bad, GOOD[1]]), tool_reply([bad, GOOD[1]])])
    out, res = llm.run(_df(), TAX, CONF, client, cache, ROOT / "prompts")
    assert len(client.requests) == 2 and res.calls == 2
    assert list(out["type"]) == ["", ""] and res.classified == 0
    assert len(res.problems) == 2 and all("定義に合いません" in p for p in res.problems)
    assert not cache.read_text(encoding="utf-8").strip()


def test_invalid_answer_is_retried_once(tmp_path: Path):
    bad = {"id": "1", "type": "Z9", "topic": "", "root_cause": "", "reason": ""}
    client = _client([tool_reply([bad, GOOD[1]]), tool_reply(GOOD)])
    out, res = llm.run(_df(), TAX, CONF, client, tmp_path / "cache.jsonl", ROOT / "prompts")
    assert list(out["type"]) == ["A1", "B1"] and res.classified == 2 and len(res.problems) == 1


def test_max_tokens_is_detected(tmp_path: Path):
    cut = tool_reply(GOOD[:1], stop_reason="max_tokens", usage={"input_tokens": 300, "output_tokens": 4000})
    client = _client([cut])
    out, res = llm.run(_df(), TAX, CONF, client, tmp_path / "cache.jsonl", ROOT / "prompts")
    assert len(client.requests) == 1  # 切れた回答は再送しない（同じ結果になるため）
    assert list(out["type"]) == ["", ""] and res.classified == 0
    assert res.output_tokens == 4000  # 切れた回の費用も数える
    assert len(res.problems) == 1 and "max_tokens" in res.problems[0] and "batch_size" in res.problems[0]


def test_retry_on_529_honors_retry_after(tmp_path: Path):
    overloaded = {
        "status": 529,
        "headers": {"retry-after": "7"},
        "body": {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
    }
    slept: list[float] = []
    client = _client([overloaded, tool_reply(GOOD)], sleep=slept.append)
    out, res = llm.run(_df(), TAX, CONF, client, tmp_path / "cache.jsonl", ROOT / "prompts")
    assert list(out["type"]) == ["A1", "B1"] and res.problems == []
    assert len(client.requests) == 2 and len(slept) == 1 and 7 <= slept[0] < 8


def test_cache_key_changes_when_prompt_changes(tmp_path: Path):
    prompts = tmp_path / "prompts"
    shutil.copytree(ROOT / "prompts", prompts)
    cache = tmp_path / "cache.jsonl"
    llm.run(_df(), TAX, CONF, _client([tool_reply(GOOD)]), cache, prompts)
    # 同じプロンプト → キャッシュだけで済む（FakeLLM([]) は送ると例外）
    _, res = llm.run(_df(), TAX, CONF, _client([]), cache, prompts)
    assert res.cached == 2 and res.calls == 0
    # 工程の目安を直す → キーが変わり、送り直す
    guide = prompts / "phase-BD.md"
    guide.write_text(guide.read_text(encoding="utf-8") + "\n- 用語集と食い違うものは A2\n", encoding="utf-8")
    client = _client([tool_reply(GOOD)])
    _, res = llm.run(_df(), TAX, CONF, client, cache, prompts)
    assert res.cached == 0 and len(client.requests) == 1
    # テンプレートを直しても同じ
    tpl = prompts / "classify.md"
    tpl.write_text(tpl.read_text(encoding="utf-8").replace("品質管理の担当者", "品質保証の担当者"), encoding="utf-8")
    client = _client([tool_reply(GOOD)])
    _, res = llm.run(_df(), TAX, CONF, client, cache, prompts)
    assert res.cached == 0 and len(client.requests) == 1


def test_cache_key_changes_when_definitions_change():
    data = yaml.safe_load((CONFIG / "taxonomy.yaml").read_text(encoding="utf-8"))
    first = next(iter(data["types"]))
    data["types"][first]["description"] += "（改訂）"
    tax2 = taxonomy.Taxonomy(TAX.version, data["groups"], data["types"], data["topics"], data["root_causes"])
    h1 = llm._prompt_hashes(_df(), TAX, CONF, ROOT / "prompts")
    h2 = llm._prompt_hashes(_df(), tax2, CONF, ROOT / "prompts")
    assert h1["BD"] != h2["BD"]


def test_make_client_honors_base_url_and_config(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://llm-gateway.example.internal/")
    client = llm.make_client(CONF)
    assert isinstance(client, LLMClient)
    assert client.base_url == "https://llm-gateway.example.internal"
    assert (client.model, client.max_tokens, client.max_retries, client.cache_system) == (
        CONF["model"],
        CONF["max_output_tokens"],
        CONF["max_retries"],
        True,
    )


def test_price_uses_shared_table_and_warns_on_override(caplog: pytest.LogCaptureFixture):
    assert "pricing" not in CONF  # 料金は devtools_common.llm.PRICES の 1 か所で管理する
    shared = llm.price(CONF["model"], CONF)
    assert shared is not None and shared.input > 0
    with caplog.at_level(logging.WARNING):
        p = llm.price(CONF["model"], {**CONF, "pricing": {CONF["model"]: {"input": 9.0, "output": 99.0}}})
    assert p is not None and (p.input, p.output) == (9.0, 99.0)
    assert "非推奨" in caplog.text
    assert llm.price("unknown-model", {}) is None


def test_label_outside_definition_is_not_cached_when_id_contains_colons(tmp_path: Path):
    """id は「table:指摘一覧.xlsx:1」のように「:」を含む. 定義に合わない分類をキャッシュに残さない."""
    df = model.normalize(
        pd.DataFrame(
            [
                {"id": "table:指摘一覧.xlsx:1", "phase": "BD", "severity": "MUST", "text": "桁が書かれていない"},
                {"id": "table:指摘一覧.xlsx:2", "phase": "BD", "severity": "NITS", "text": "誤字"},
            ]
        )
    )
    labels = [
        {"id": "table:指摘一覧.xlsx:1", "type": "A1", "topic": "API・IF", "root_cause": "R2", "reason": "桁が無い"},
        # スキーマ（enum）には合うが定義に合わない: root_cause は A の指摘だけ
        {"id": "table:指摘一覧.xlsx:2", "type": "B1", "topic": "", "root_cause": "R2", "reason": "誤字"},
    ]
    cache = tmp_path / "cache.jsonl"
    out, res = llm.run(df, TAX, CONF, _client([tool_reply(labels)]), cache, ROOT / "prompts")
    assert res.problems == ["table:指摘一覧.xlsx:2: root_cause は A の指摘だけに付けます"]
    assert list(out["type"]) == ["A1", ""]
    assert res.classified == 1
    assert len(cache.read_text(encoding="utf-8").splitlines()) == 1
    # 次の実行では、定義に合わなかった指摘だけを送り直す
    fixed = [{**labels[1], "root_cause": ""}]
    client = _client([tool_reply(fixed)])
    out, res = llm.run(df, TAX, CONF, client, cache, ROOT / "prompts")
    assert res.cached == 1 and res.classified == 1 and list(out["type"]) == ["A1", "B1"]
    assert len(client.requests) == 1
