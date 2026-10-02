"""分類の定義（config/taxonomy.yaml）."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Taxonomy:
    version: int
    groups: dict[str, dict[str, str]]
    types: dict[str, dict[str, str]]
    topics: dict[str, list[str]]
    root_causes: dict[str, dict[str, str]]

    def group_of(self, type_id: str) -> str:
        return self.types.get(type_id, {}).get("group", "")

    def type_label(self, type_id: str) -> str:
        t = self.types.get(type_id)
        return f"{type_id} {t['name']}" if t else type_id

    def cause_label(self, rid: str) -> str:
        r = self.root_causes.get(rid)
        return f"{rid} {r['name']}" if r else rid

    def topics_for(self, phase: str) -> list[str]:
        if phase in self.topics:
            return list(self.topics[phase])
        return sorted({t for ts in self.topics.values() for t in ts})

    def check(self, type_id: str, topic: str, root_cause: str, phase: str) -> list[str]:
        """分類の結果が定義に合っているか. → 問題の一覧（空なら OK）."""
        problems = []
        if type_id not in self.types:
            problems.append(f"type {type_id!r} は定義にありません")
        if topic and topic not in self.topics_for(phase):
            problems.append(f"topic {topic!r} は工程 {phase or '（不明）'} の観点にありません")
        if root_cause and root_cause not in self.root_causes:
            problems.append(f"root_cause {root_cause!r} は定義にありません")
        if root_cause and self.group_of(type_id) != "A":
            problems.append("root_cause は A の指摘だけに付けます")
        return problems


def load(path: Path) -> Taxonomy:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    for key in ("groups", "types", "topics", "root_causes"):
        if not isinstance(data.get(key), dict) or not data[key]:
            raise ValueError(f"{path.name}: {key} が必要です")
    for tid, t in data["types"].items():
        if t.get("group") not in data["groups"]:
            raise ValueError(f"{path.name}: types.{tid}.group が groups にありません")
    return Taxonomy(int(data.get("version", 1)), data["groups"], data["types"], data["topics"], data["root_causes"])
