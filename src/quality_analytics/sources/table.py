"""指摘表（Excel / CSV / TSV）を、設定（config/sources/table.yaml）の列の対応で取り込む."""

from __future__ import annotations

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
    if "id" not in df.columns or (df["id"] == "").any():
        df["id"] = [str(i + 1) for i in range(len(df))]
    prefix = str((conf.get("fixed") or {}).get("source", "table"))
    df["id"] = f"{prefix}:{path.stem}:" + df["id"].str.strip()
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
