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

MASK = "＊＊＊"
# 決まった値だけが入る列. ここに無い列（本文・成果物名・未知の列など）はすべて伏せる対象
KEEP = frozenset({"source", "phase", "severity", "decision", "created_at", "type", "topic", "root_cause", "classified_by"})
PEOPLE = ("reviewer", "author")
_PSEUDO = re.compile(r"^U-[0-9a-f]{6}$")

_D = "[0-9０-９]"
_SEP = r"[\s\-‐‑–—―−－ー.．()（）]"
_ALNUM = "A-Za-z0-9Ａ-Ｚａ-ｚ０-９"
EMAIL = re.compile(rf"[{_ALNUM}._%+\-．＿％＋－]+\s*[@＠]\s*[{_ALNUM}\-－]+(?:\s*[.．]\s*[{_ALNUM}\-－]+)+")
_PHONE = re.compile(rf"(?<![0-9０-９])(?:[+＋]\s*[8８][1１]|[0０])(?:{_SEP}{{0,2}}{_D}){{8,11}}(?![0-9０-９])")


def _phone(m: re.Match[str]) -> str:
    s = m.group(0)
    digits = len(re.findall(_D, s))
    if s[0] in "+＋":
        return MASK if 11 <= digits <= 12 else s  # 81 + 9〜10 桁
    return MASK if 10 <= digits <= 11 else s


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
        self._res = [re.compile(p) for p in self.patterns if p]
        self._words = sorted({w for w in self.words if w}, key=len, reverse=True)

    def text(self, s: str, names: dict[str, str] | None = None) -> str:
        if not s:
            return s
        for name, alias in (names or {}).items():
            s = s.replace(name, alias)
        for w in self._words:
            s = s.replace(w, MASK)
        s = EMAIL.sub(MASK, s)
        s = _PHONE.sub(_phone, s)
        for r in self._res:
            s = r.sub(MASK, s)
        return s

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
