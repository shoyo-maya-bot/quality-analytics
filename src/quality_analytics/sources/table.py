"""指摘表（Excel / CSV / TSV）を、設定（config/sources/table.yaml）の列の対応で取り込む.

id は `<source>:<ファイル名>:<番号の列の値>`。番号が空の行（と番号の列が無い表）は、行の位置ではなく内容
（成果物・指摘者・指摘日・本文）のハッシュで `h` + 10 桁の id を付ける。行を削除・挿入して取り込み直しても id は変わらない。
同じ内容の行が複数あるときは、2 件目から `-2`、`-3` を付ける。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pandas as pd

from ..model import normalize, split_tag


def read(path: Path, conf: dict[str, Any]) -> pd.DataFrame:
    header = int(conf.get("header_row", 1)) - 1
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        raw = pd.read_excel(path, sheet_name=conf.get("sheet") or 0, header=header, dtype=str)
    elif suffix in (".csv", ".tsv", ".txt"):
        sep = "\t" if suffix in (".tsv", ".txt") else ","
        raw = _read_text(path, sep, header)
    else:
        raise ValueError(f"対応していない形式です（.xlsx / .csv / .tsv）: {path.name}")
    raw = raw.fillna("")
    raw.columns = [str(c).strip() for c in raw.columns]
    columns = {str(k): _col_name(v) for k, v in (conf.get("columns") or {}).items()}
    missing = [src for src in columns.values() if src not in raw.columns]
    if missing:
        raise ValueError(f"{path.name}: 列がありません: {', '.join(missing)}（config の columns を見直してください）")
    df = pd.DataFrame({dst: raw[src].astype(str) for dst, src in columns.items()})
    for k, v in (conf.get("fixed") or {}).items():
        if k not in df.columns or (df[k] == "").all():
            df[k] = str(v)
    df = df[df.get("text", pd.Series("", index=df.index)).str.strip() != ""].copy()
    if "id" not in df.columns:
        df["id"] = ""
    df["id"] = _fill_ids(df)
    df["id"] = scope(path, conf) + df["id"]
    for col, key in (("severity", "severity_map"), ("decision", "decision_map"), ("phase", "phase_map")):
        if col in df.columns:
            df[col] = df[col].str.strip().replace(conf.get(key) or {})
    # 本文の先頭に [MUST] などがあれば、重要度の列が空のときに使う
    tags = df["text"].map(split_tag)
    if "severity" not in df.columns:
        df["severity"] = ""
    df["severity"] = [s or t[0] for s, t in zip(df["severity"], tags, strict=True)]
    df["text"] = [t[1] for t in tags]
    if "created_at" in df.columns:
        df["created_at"] = df["created_at"].str.slice(0, 10).str.replace("/", "-")
    return normalize(df)


def scope(path: Path, conf: dict[str, Any]) -> str:
    """このファイルから取り込んだ指摘の id の先頭. 取り込み直したとき、ここで始まる古い行を削除の対象にする."""
    prefix = str((conf.get("fixed") or {}).get("source", "table"))
    return f"{prefix}:{path.stem}:"


_HASH_COLS = ("deliverable", "reviewer", "created_at", "text")


def _fill_ids(df: pd.DataFrame) -> list[str]:
    """番号の空の行に、内容のハッシュの id を付ける（番号のある行はそのまま）."""
    seen: dict[str, int] = {}
    out = []
    for _, r in df.iterrows():
        given = str(r["id"]).strip()
        if given:
            out.append(given)
            continue
        key = "\x1f".join(str(r[c]).strip() if c in df.columns else "" for c in _HASH_COLS)
        base = "h" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]
        seen[base] = seen.get(base, 0) + 1
        out.append(base if seen[base] == 1 else f"{base}-{seen[base]}")
    return out


def _col_name(v: object) -> str:
    """YAML の No / Yes が真偽値になった場合も列名に戻す."""
    if isinstance(v, bool):
        return "Yes" if v else "No"
    return str(v)


def _read_text(path: Path, sep: str, header: int) -> pd.DataFrame:
    for enc in ("utf-8-sig", "cp932"):
        try:
            return pd.read_csv(path, sep=sep, header=header, dtype=str, keep_default_na=False, encoding=enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"{path.name}: 文字コードを判別できません（UTF-8 か Shift_JIS で保存してください）")
