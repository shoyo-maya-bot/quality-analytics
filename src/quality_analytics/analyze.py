"""指摘の集計（pandas）. 表（DataFrame）と、そこから読み取れる所見（文章）を返す."""

from __future__ import annotations

from collections.abc import Callable
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


class DeliverablesError(ValueError):
    """成果物のページ数のファイル（deliverables.csv）の誤り（設定の誤り）."""


def read_deliverables(path: Path, mask_name: Callable[[str], str] | None = None) -> pd.Series:
    """deliverables.csv（deliverable, pages）→ 成果物名 → ページ数.

    成果物名には指摘と同じ伏せ字（mask_name）をかけてから返す（指摘の成果物名は取り込むときに伏せてあるので、
    伏せた名前同士で結合する）。deliverable・pages の列が無い、ページ数が数でないときは DeliverablesError。
    伏せた後に同じ名前になった成果物は、ページ数を合計する。
    """
    try:
        raw = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    except (OSError, ValueError, UnicodeDecodeError) as e:
        raise DeliverablesError(f"{path}: 読めません（{e}）") from None
    raw.columns = [str(c).strip() for c in raw.columns]
    missing = [c for c in ("deliverable", "pages") if c not in raw.columns]
    if missing:
        raise DeliverablesError(f"{path}: 列がありません: {', '.join(missing)}（見出しは deliverable,pages。例: 基本設計書_会員API,18）")
    pages = pd.to_numeric(raw["pages"].str.strip(), errors="coerce")
    bad = raw.loc[pages.isna() | (pages <= 0), "deliverable"]
    if not bad.empty:
        raise DeliverablesError(f"{path}: pages が正の数ではない行があります（{', '.join(map(str, bad.head(5)))}）")
    names = raw["deliverable"].str.strip()
    if mask_name is not None:
        names = names.map(mask_name)
    return pd.Series(pages.to_numpy(), index=names.to_numpy(), name="pages").groupby(level=0).sum()


def _pct(n: float, d: float) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def _rate(n: float, d: float) -> float | None:
    """率（%）. 分母が 0（分類済みが 0 件など）のときは 0% ではなく None（表では空欄）にする."""
    return round(100.0 * n / d, 1) if d else None


def run(
    df: pd.DataFrame,
    tax: Taxonomy,
    deliverables: Path | None = None,
    *,
    noise_threshold: float = 30.0,
    mask_name: Callable[[str], str] | None = None,
) -> Analysis:
    """集計する. deliverables（成果物のページ数）の成果物名には mask_name（指摘と同じ伏せ字）をかけてから結合する."""
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
        classified = int((g["type"] != "").sum())  # 率の分母は全体の率と同じく分類済みの件数
        rows.append(
            {
                "レビュアー": name or "（不明）",
                "件数": len(g),
                "分類済み": classified,
                **{s: int((g["severity"] == s).sum()) for s in SEVERITIES},
                # 分類済みが 0 件のレビュアーは率を出せない（0% と書くと「欠陥もノイズも無い」と読めてしまう）ので空欄
                "欠陥(A)率%": _rate((g["group"] == "A").sum(), classified),
                "ノイズ(B)率%": _rate((g["group"] == "B").sum(), classified),
                "採用率%": _rate((decided["decision"] == "adopted").sum(), len(decided)),
            }
        )
    rev = pd.DataFrame(rows).sort_values("件数", ascending=False).reset_index(drop=True)
    a.tables["レビュアー別"] = rev

    # 4. 成果物別（ページ数があれば密度）
    dg = df.groupby("deliverable").agg(
        件数=("id", "count"), MUST=("severity", lambda s: int((s == "MUST").sum())), A=("group", lambda s: int((s == "A").sum()))
    )
    if deliverables and deliverables.is_file():
        pages = read_deliverables(deliverables, mask_name)
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
        noise = pd.to_numeric(rev["ノイズ(B)率%"], errors="coerce")  # 空欄（None）は比べない
        noisy = rev[(noise >= noise_threshold) & (rev["分類済み"] >= 3)]
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
