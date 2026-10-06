"""テスト用の LLM（通信しない）. devtools_common.llm.FakeLLM を使う."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from devtools_common.llm import FakeLLM

from quality_analytics.classify import llm


def tool_reply(labels: list[dict[str, Any]], *, stop_reason: str = "tool_use", usage: dict[str, int] | None = None) -> dict[str, Any]:
    """Messages API の応答（record_labels ツールの呼び出し）."""
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5-20251001",
        "content": [{"type": "tool_use", "id": "toolu_test", "name": llm.TOOL_NAME, "input": {"labels": labels}}],
        "stop_reason": stop_reason,
        "usage": usage or {"input_tokens": 100, "output_tokens": 50, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    }


def sent_items(request: dict[str, Any]) -> list[dict[str, Any]]:
    """送ったリクエストの user の本文から、指摘の JSON を取り出す."""
    text = request["messages"][0]["content"]
    start = text.index("## 指摘") + len("## 指摘")
    return json.loads(text[start : text.rindex("\n\n結果は")])


class EchoLLM(FakeLLM):
    """送られた指摘すべてに label(item) の分類を返す（件数の決まらない CLI のテスト用）."""

    def __init__(self, label: Callable[[dict[str, Any]], dict[str, Any]], **kw: Any):
        super().__init__([], **kw)
        self._label = label

    def _open(self, req: Any, timeout: float) -> Any:
        body = json.loads(req.data)
        self._responses = [tool_reply([{"id": i["id"], **self._label(i)} for i in sent_items(body)])]
        return super()._open(req, timeout)
