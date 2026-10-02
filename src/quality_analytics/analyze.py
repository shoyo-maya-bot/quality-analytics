"""指摘の集計（pandas）. 表（DataFrame）と、そこから読み取れる所見（文章）を返す."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .model import SEVERITIES
from .taxonomy import Taxonomy


@dataclass
class Analysis:
    total: int
    classified: int
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)
    insights: list[str] = field(default_factory=list)


def _pct(n: float, d: float) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def run(df: pd.DataFrame, tax: Taxonomy, deliverables: Path | None = None, *, noise_threshold: float = 30.0) -> Analysis:
    df = df.copy()
    df["group"] = df["type"].map(tax.group_of).fillna("")
    a = Analysis(total=len(df), classified=int((df["type"] != "").sum()))
    if df.empty:
        return a

    # 1. 全体: 種類 × 重要度
    t = pd.crosstab(df["type"].replace("", "（未分類）"), df["severity"].replace("", "（なし）"), margins=True, margins_name="計")
    t.index = [tax.type_label(i) if i in tax.types else i for i in t.index]
    a.tables["種類×重要度"] = t

    # 2. 工程 × 区分
    a.tables["工程×区分"] = pd.crosstab(
        df["phase"].replace("", "（不明）"), df["group"].replace("", "（未分類）"), margins=True, margins_name="計"
    )

    # 3. レビュアー別
    rows = []
    for name, g in df.groupby("reviewer"):
        decided = g[g["decision"] != ""]
        rows.append(
            {
                "レビュアー": name or "（不明）",
                "件数": len(g),
                **{s: int((g["severity"] == s).sum()) for s in SEVERITIES},
                "欠陥(A)率%": _pct((g["group"] == "A").sum(), len(g)),
                "ノイズ(B)率%": _pct((g["group"] == "B").sum(), len(g)),
                "採用率%": _pct((decided["decision"] == "adopted").sum(), len(decided)) if len(decided) else None,
            }
        )
    rev = pd.DataFrame(rows).sort_values("件数", ascending=False).reset_index(drop=True)
    a.tables["レビュアー別"] = rev

    # 4. 成果物別（ページ数があれば密度）
    dg = df.groupby("deliverable").agg(
        件数=("id", "count"), MUST=("severity", lambda s: int((s == "MUST").sum())), A=("group", lambda s: int((s == "A").sum()))
    )
    if deliverables and deliverables.is_file():
        pages = pd.read_csv(deliverables, dtype={"deliverable": str}, encoding="utf-8-sig").set_index("deliverable")["pages"]
        dg = dg.join(pages, how="left")
        dg["件/ページ"] = (dg["件数"] / dg["pages"]).round(2)
        dg["A/ページ"] = (dg["A"] / dg["pages"]).round(2)
        dg = dg.rename(columns={"pages": "ページ数"})
    a.tables["成果物別"] = dg.sort_values("件数", ascending=False)

    # 5. 欠陥（A）: 観点 × 真因
    A = df[df["group"] == "A"]
    if not A.empty:
        x = pd.crosstab(A["topic"].replace("", "（観点なし）"), A["root_cause"].replace("", "（真因なし）"))
        x.columns = [tax.cause_label(c) for c in x.columns]
        x["計"] = x.sum(axis=1)
        a.tables["欠陥 観点×真因"] = x.sort_values("計", ascending=False)

    # 6. ノイズ（B）: 種類 × レビュアー
    B = df[df["group"] == "B"]
    if not B.empty:
        nb = pd.crosstab(B["reviewer"].replace("", "（不明）"), B["type"].map(tax.type_label))
        nb["計"] = nb.sum(axis=1)
        a.tables["ノイズ 種類×レビュアー"] = nb.sort_values("計", ascending=False)

    a.insights = _insights(df, a, tax, noise_threshold)
    return a


def _insights(df: pd.DataFrame, a: Analysis, tax: Taxonomy, noise_threshold: float) -> list[str]:
    out = []
    unclassified = a.total - a.classified
    if unclassified:
        out.append(f"未分類の指摘が {unclassified} 件あります（classify を実行してください）。")
    A = df[df["group"] == "A"]
    if not A.empty:
        causes = A[A["root_cause"] != ""]["root_cause"].value_counts()
        if not causes.empty:
            top = causes.index[0]
            out.append(
                f"欠陥の真因で最も多いのは「{tax.cause_label(top)}」（{causes.iloc[0]} 件 / 真因あり {causes.sum()} 件）。"
                "作成時のチェックリストや事前の確認会で手当てする候補です。"
            )
        topics = A[A["topic"] != ""]["topic"].value_counts()
        if not topics.empty:
            out.append(f"欠陥が多い観点: {'、'.join(f'{k}（{v}）' for k, v in topics.head(3).items())}。")
    rev = a.tables.get("レビュアー別")
    if rev is not None and not rev.empty:
        noisy = rev[(rev["ノイズ(B)率%"] >= noise_threshold) & (rev["件数"] >= 3)]
        for _, r in noisy.iterrows():
            out.append(
                f"{r['レビュアー']} の指摘はノイズ（B）が {r['ノイズ(B)率%']}% です。表記・好みの指摘はまとめて伝えると、作成者の負担が減ります。"
            )
    dg = a.tables.get("成果物別")
    if dg is not None and "A/ページ" in dg.columns and dg["A/ページ"].notna().sum() >= 2:
        mean, sd = dg["A/ページ"].mean(), dg["A/ページ"].std()
        for name, r in dg[dg["A/ページ"] > mean + sd].iterrows():
            out.append(
                f"「{name}」は欠陥の密度が高い（{r['A/ページ']} 件/ページ、平均 {mean:.2f}）。再レビューや作成者の支援を検討してください。"
            )
    total_b = (df["group"] == "B").sum()
    if a.classified and _pct(total_b, a.classified) >= noise_threshold:
        out.append(
            f"全体のノイズ（B）率が {_pct(total_b, a.classified)}% です。表記ゆれなどは機械チェック（office-review の事前チェック）に任せる候補です。"
        )
    return out
