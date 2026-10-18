"""個人情報の伏せ字. 取り込むとき（と読み込むとき）に 1 回かけ、以後の出力はすべて伏せた値だけを使う.

- 氏名の列（既定: reviewer・author）は U-xxxxxx の仮名にする。本文中の同じ氏名も同じ仮名にする.
  短い英数字の氏名（ログイン名など. SHORT_ASCII_NAME 文字以下）は、単語の一部（「api」に対する「rapid」など）は
  置き換えず、前後が英数字・「_」でないところだけを置き換える
  本文では、氏名の途中の空白（半角・全角）の有無・数の違いも同じ氏名として置き換える（「例題 花子」「例題　花子」「例題花子」）.
  空白で区切った氏名（「例題 花子」）は、姓・名だけ（2 文字以上）の出現も置き換える（同じ姓・名の人が複数いれば ＊＊＊）
- 仮名は、環境変数 QA_PSEUDONYM_SALT（.env 可）があればそれを鍵にした HMAC-SHA256 から作る（氏名の候補から割り出せない）.
  無ければ以前と同じソルト無しの SHA-256（同じ人は以前と同じ仮名. 氏名の候補が分かれば割り出せる）
- メールアドレス・電話番号は、全角・区切りなし・+81 も含めて常に伏せる（設定で外せない）
- config/mask.yaml の words（社名・システム名など）と patterns（正規表現）も伏せる
- 区分の列（KEEP）以外は、未知の列も含めてすべて伏せる対象にする（既定で伏せる）
- id は伏せ字で別の指摘と同じ値にならないように、「:」で区切った部分ごとに伏せ、伏せた部分には元の値の
  ハッシュ（8 桁）を付ける（例: table:指摘一覧_A社:1 → table:指摘一覧_＊＊＊~3f2a9c1b:1）. 何度かけても同じ値になる
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import os
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
SHORT_ASCII_NAME = 5  # この文字数以下の英数字だけの氏名は、本文では単語として現れたところだけを置き換える
_WORD = "0-9A-Za-z_"

# メールアドレス・電話番号の検出は devtools_common.llm と共通（全角・区切りなし・+81 を含む）
EMAIL = llm.EMAIL_RE
_PHONE = llm.PHONE_RE


SALT_ENV = "QA_PSEUDONYM_SALT"


def pseudonym(name: str, salt: str | None = None) -> str:
    """氏名 → U-xxxxxx. salt（省略時は環境変数 QA_PSEUDONYM_SALT）があれば HMAC-SHA256、無ければ以前と同じ SHA-256."""
    key = os.environ.get(SALT_ENV, "") if salt is None else salt
    if key:
        return "U-" + hmac.new(key.encode(), name.encode(), hashlib.sha256).hexdigest()[:6]
    return "U-" + hashlib.sha256(name.encode()).hexdigest()[:6]


def salted() -> bool:
    """仮名にソルト（QA_PSEUDONYM_SALT）を使っているか."""
    return bool(os.environ.get(SALT_ENV, ""))


def _digest(s: str, n: int = 8) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:n]


_SPACE = "[ \t\u3000]*"  # 氏名の途中の空白（半角・全角. 無くてもよい）
_SPLIT = re.compile(r"[ \t\u3000]+")


@functools.lru_cache(maxsize=1024)
def _name_pattern(name: str) -> re.Pattern[str]:
    """本文で氏名を探す正規表現.

    - 短い英数字の氏名（SHORT_ASCII_NAME 文字以下）は単語として現れたところだけ
    - 英数字以外を含む氏名（日本語など）は、文字の間の空白（半角・全角）の有無・数を問わない（「例題 花子」も「例題　花子」も当たる）
    - 空白を含む英数字の氏名（Taro Yamada）は、語の間の空白の数・全角を問わない
    """
    parts = _SPLIT.split(name.strip())
    if name.isascii():
        body = _SPACE.replace("*", "+").join(re.escape(p) for p in parts)
        if len(name) <= SHORT_ASCII_NAME or len(parts) > 1:
            return re.compile(rf"(?<![{_WORD}]){body}(?![{_WORD}])")
        return re.compile(body)
    return re.compile(_SPACE.join(re.escape(ch) for ch in "".join(parts)))


@functools.lru_cache(maxsize=1024)
def _part_pattern(part: str) -> re.Pattern[str]:
    """姓・名だけの出現を探す正規表現（英数字は単語として）."""
    if part.isascii():
        return re.compile(rf"(?<![{_WORD}]){re.escape(part)}(?![{_WORD}])")
    return re.compile(re.escape(part))


def _replace_name(s: str, name: str, alias: str) -> str:
    return _name_pattern(name).sub(alias, s)


def name_parts(names: dict[str, str]) -> dict[str, str]:
    """空白で区切った氏名の姓・名（2 文字以上）→ 仮名. 複数の人に共通する姓・名は ＊＊＊（どの人か決められない）.

    氏名そのもの（区切りの無い氏名を含む）と同じ文字列の部分は除く（氏名として置き換える）.
    """
    owners: dict[str, set[str]] = {}
    for name, alias in names.items():
        parts = _SPLIT.split(name.strip())
        if len(parts) < 2:
            continue
        for p in parts:
            if len(p) >= 2:
                owners.setdefault(p, set()).add(alias)
    for name, alias in names.items():  # 区切りの無い氏名と同じ部分は、その人の氏名として扱う
        if name in owners:
            owners[name].add(alias)
    out = {p: (next(iter(a)) if len(a) == 1 else MASK) for p, a in owners.items() if p not in names}
    return dict(sorted(out.items(), key=lambda kv: len(kv[0]), reverse=True))


@functools.lru_cache(maxsize=64)
def _parts_of(items: tuple[tuple[str, str], ...]) -> dict[str, str]:
    return name_parts(dict(items))


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
        names = names or {}
        for name, alias in names.items():  # 氏名 → 仮名（このツール固有）
            s = _replace_name(s, name, alias)
        for part, alias in _parts_of(tuple(names.items())).items():  # 姓・名だけの出現
            s = _part_pattern(part).sub(alias, s)
        return self._generic.text(s)

    def ident(self, s: str, names: dict[str, str] | None = None) -> str:
        """id の伏せ字. 伏せた部分に元の値のハッシュを付けて、別の id と同じ値にならないようにする（何度かけても同じ）."""
        if not s:
            return s
        parts = []
        for part in s.split(":"):
            masked = self.text(part, names)
            parts.append(part if masked == part else f"{masked}~{_digest(part)}")
        out = ":".join(parts)
        if self.text(out, names) != out:  # 「:」をまたぐ語など. 部分ごとでは伏せきれないときは全体をハッシュにする
            return f"masked~{_digest(s, 16)}"
        return out

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
            if c == "id":
                out[c] = [self.ident(str(v), names) if isinstance(v, str) else v for v in out[c]]
            elif c in cols:
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
