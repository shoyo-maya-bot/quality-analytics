"""表計算ソフトで開くファイル（CSV・Excel）の数式注入の無害化.

指摘の本文や LLM の reason は、PR にコメントできる人なら誰でも書ける。`=HYPERLINK(...)` のような値が
Excel で数式として動かないようにする（devtools-common に同じ部品ができたら、そちらに置き換える）。

- CSV: `= + - @`・タブ・CR で始まる値と、`'` で始まる値の先頭に `'` を付ける。読み込むとき（unquote）に 1 つ外すので、
  data/findings.csv に保存して読み戻しても値は変わらない
- Excel（openpyxl）: 数式とみなされた文字列のセルを文字列型にする（値はそのまま）
"""

from __future__ import annotations

from typing import Any

DANGEROUS = ("=", "+", "-", "@", "\t", "\r")
QUOTE = "'"


def quote(v: Any) -> Any:
    """CSV に書く値. 数式の始まりの文字（と引用符）で始まる文字列の先頭に ' を付ける."""
    if isinstance(v, str) and v.startswith((*DANGEROUS, QUOTE)):
        return QUOTE + v
    return v


def unquote(v: Any) -> Any:
    """quote の逆. CSV から読んだ値の先頭の ' を 1 つ外す."""
    if isinstance(v, str) and v.startswith(QUOTE):
        return v[1:]
    return v


def excel_strings(workbook: Any) -> int:
    """openpyxl のブックの、数式とみなされた文字列のセルを文字列型にする. → 直したセルの数."""
    fixed = 0
    for ws in workbook.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if cell.data_type == "f" or (isinstance(cell.value, str) and cell.value.startswith(DANGEROUS)):
                    cell.data_type = "s"
                    fixed += 1
    return fixed
