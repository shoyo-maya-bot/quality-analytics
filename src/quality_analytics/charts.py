"""グラフ（matplotlib）. 日本語フォントは入っているものを順に探す（追加のライブラリは使わない）."""

from __future__ import annotations

import warnings
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib import font_manager  # noqa: E402

JP_FONTS = [
    "Yu Gothic",
    "Meiryo",
    "MS Gothic",
    "Hiragino Sans",
    "Noto Sans CJK JP",
    "Noto Sans JP",
    "IPAexGothic",
    "IPAGothic",
    "TakaoGothic",
]


def setup_font() -> str | None:
    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in JP_FONTS:
        if name in available:
            plt.rcParams["font.family"] = name
            return name
    return None


def _save(fig, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # フォントが無い環境での字形の警告
        fig.tight_layout()
        fig.savefig(path, dpi=110)
    plt.close(fig)
    return path


def bar_stacked(df: pd.DataFrame, title: str, path: Path) -> Path:
    fig, ax = plt.subplots(figsize=(8, max(3, 0.4 * len(df) + 1.5)))
    df.plot.barh(stacked=True, ax=ax)
    ax.invert_yaxis()
    ax.set_title(title)
    ax.set_xlabel("件数")
    ax.legend(loc="lower right", fontsize=8)
    return _save(fig, path)


def heatmap(df: pd.DataFrame, title: str, path: Path) -> Path:
    fig, ax = plt.subplots(figsize=(max(6, 1.1 * len(df.columns) + 2), max(3, 0.5 * len(df) + 1.5)))
    im = ax.imshow(df.values, cmap="Reds", aspect="auto")
    ax.set_xticks(range(len(df.columns)), df.columns, rotation=30, ha="right", fontsize=8)
    ax.set_yticks(range(len(df.index)), df.index, fontsize=8)
    for i in range(len(df.index)):
        for j in range(len(df.columns)):
            v = df.values[i, j]
            if v:
                ax.text(j, i, int(v), ha="center", va="center", fontsize=8)
    ax.set_title(title)
    fig.colorbar(im, ax=ax)
    return _save(fig, path)


def draw_all(tables: dict[str, pd.DataFrame], out_dir: Path) -> dict[str, Path]:
    setup_font()
    charts: dict[str, Path] = {}
    rev = tables.get("レビュアー別")
    if rev is not None and not rev.empty:
        d = rev.set_index("レビュアー")[["MUST", "WANT", "NITS", "Q"]]
        charts["レビュアー別の重要度"] = bar_stacked(d, "レビュアー別の重要度", out_dir / "reviewer_severity.png")
    pg = tables.get("工程×区分")
    if pg is not None and not pg.empty:
        d = pg.drop(index="計", columns="計", errors="ignore")
        charts["工程別の区分"] = bar_stacked(d, "工程別の区分（A 欠陥 / B ノイズ / C 確認）", out_dir / "phase_group.png")
    x = tables.get("欠陥 観点×真因")
    if x is not None and not x.empty:
        charts["欠陥の観点×真因"] = heatmap(x.drop(columns="計"), "欠陥（A）の観点 × 真因", out_dir / "defect_topic_cause.png")
    return charts
