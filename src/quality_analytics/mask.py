"""個人情報の伏せ字. 取り込むとき（と読み込むとき）に 1 回かけ、以後の出力はすべて伏せた値だけを使う.

- 氏名の列（既定: reviewer・author）は U-xxxxxx の仮名にする。本文中の同じ氏名も同じ仮名にする
- メールアドレス・電話番号は、全角・区切りなし・+81 も含めて常に伏せる（設定で外せない）
- config/mask.yaml の words（社名・システム名など）と patterns（正規表現）も伏せる
- 区分の列（KEEP）以外は、未知の列も含めてすべて伏せる対象にする（既定で伏せる）
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
from devtools_common import llm

MASK = llm.MASK  # ＊＊＊
# 決まった値だけが入る列. ここに無い列（本文・成果物名・未知の列など）はすべて伏せる対象
KEEP = frozenset({"source", "phase", "severity", "decision", "created_at", "type", "topic", "root_cause", "classified_by"})
PEOPLE = ("reviewer", "author")
_PSEUDO = re.compile(r"^U-[0-9a-f]{6}$")

# メールアドレス・電話番号の検出は devtools_common.llm と共通（全角・区切りなし・+81 を含む）
EMAIL = llm.EMAIL_RE
_PHONE = llm.PHONE_RE


def pseudonym(name: str) -> str:
    return "U-" + hashlib.sha256(name.encode()).hexdigest()[:6]


def _is_kept_name(v: str) -> bool:
    return not v or bool(_PSEUDO.match(v)) or v.startswith("AI:")


@dataclass
class Masker:
    words: list[str] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)
    pseudonymize: list[str] = field(default_factory=lambda: list(PEOPLE))

    def __post_init__(self) -> None:
        # 語（words）→ メールアドレス・電話番号 → patterns の順に伏せる部分は devtools_common.llm.Masker に任せる
        self._generic = llm.Masker(patterns=self.patterns, words=self.words, replacement=MASK)

    def text(self, s: str, names: dict[str, str] | None = None) -> str:
        if not s:
            return s
        for name, alias in (names or {}).items():  # 氏名 → 仮名（このツール固有）
            s = s.replace(name, alias)
        return self._generic.text(s)

    def names(self, values: Iterable[str]) -> dict[str, str]:
        """氏名 → 仮名. 長い氏名から置き換える（1 文字の氏名は本文では置き換えない）."""
        found = {v.strip() for v in values if not _is_kept_name(v.strip())}
        return {n: pseudonym(n) for n in sorted(found, key=len, reverse=True) if len(n) >= 2}

    def frame(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        cols = [c for c in self.pseudonymize if c in out.columns]
        names = self.names(v for c in cols for v in out[c].astype(str))
        for c in out.columns:
            if c in KEEP:
                continue
            if c in cols:
                out[c] = [v if _is_kept_name(str(v).strip()) else pseudonym(str(v).strip()) for v in out[c].astype(str)]
            else:
                out[c] = [self.text(str(v), names) if isinstance(v, str) else v for v in out[c]]
        return out


def from_config(conf: dict[str, Any] | None, extra_patterns: Iterable[str] = ()) -> Masker:
    conf = conf or {}
    pseudo = conf.get("pseudonymize", list(PEOPLE))
    return Masker(
        words=[str(w) for w in conf.get("words") or []],
        patterns=[*(str(p) for p in conf.get("patterns") or []), *(str(p) for p in extra_patterns or [])],
        pseudonymize=[str(c) for c in pseudo or []],
    )


def load(config_dir: Path, extra_patterns: Iterable[str] = ()) -> Masker:
    import yaml

    p = config_dir / "mask.yaml"
    conf = yaml.safe_load(p.read_text(encoding="utf-8")) if p.is_file() else {}
    return from_config(conf, extra_patterns)
