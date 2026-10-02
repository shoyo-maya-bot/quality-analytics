"""キーワードによる分類（無料・オフライン・決定的）. 精度より再現性を取る「たたき台」."""

from __future__ import annotations

from typing import Any

import pandas as pd

from ..taxonomy import Taxonomy


def classify_one(text: str, severity: str, phase: str, rules: dict[str, Any], tax: Taxonomy) -> tuple[str, str, str]:
    type_id = ""
    for rule in rules.get("types", []):
        if any(k in text for k in rule["any"]):
            type_id = rule["type"]
            break
    if not type_id:
        type_id = (rules.get("severity_defaults") or {}).get(severity, rules.get("default_type", "A1"))
    allowed = tax.topics_for(phase)
    topic = ""
    best = 0
    for name, words in (rules.get("topics") or {}).items():
        if name not in allowed:
            continue
        hits = sum(1 for w in words if w in text)
        if hits > best:
            topic, best = name, hits
    cause = ""
    if tax.group_of(type_id) == "A":
        for rid, words in (rules.get("root_causes") or {}).items():
            if any(w in text for w in words):
                cause = rid
                break
    return type_id, topic, cause


def classify(df: pd.DataFrame, rules: dict[str, Any], tax: Taxonomy, *, overwrite: bool = False) -> tuple[pd.DataFrame, int]:
    out = df.copy()
    n = 0
    for i, r in out.iterrows():
        if r["type"] and not overwrite:
            continue
        t, topic, cause = classify_one(r["text"], r["severity"], r["phase"], rules, tax)
        out.loc[i, ["type", "topic", "root_cause", "classified_by", "reason"]] = [t, topic, cause, "rules", ""]
        n += 1
    return out, n
