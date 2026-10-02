"""レポートの出力（Markdown / HTML ダッシュボード / Excel）."""

from __future__ import annotations

import base64
import html
from pathlib import Path

import pandas as pd

from .analyze import Analysis


def _md_table(df: pd.DataFrame) -> str:
    d = df.reset_index() if df.index.name or not isinstance(df.index, pd.RangeIndex) else df
    d = d.fillna("")
    cols = [str(c) for c in d.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, r in d.iterrows():
        lines.append("| " + " | ".join(str(v).replace("|", "\\|").replace("\n", " ") for v in r.values) + " |")
    return "\n".join(lines)


def markdown(a: Analysis, title: str, charts: dict[str, Path] | None = None, base: Path | None = None) -> str:
    out = [f"# {title}", "", f"- 指摘: {a.total} 件（分類済み {a.classified} 件）", ""]
    if a.insights:
        out += ["## 所見", ""] + [f"- {s}" for s in a.insights] + [""]
    for name, p in (charts or {}).items():
        rel = p.relative_to(base) if base else p
        out += [f"![{name}]({rel.as_posix()})", ""]
    for name, df in a.tables.items():
        out += [f"## {name}", "", _md_table(df), ""]
    out += ["> 分類は AI・ルールによる推定を含みます。個人の評価には使わず、プロセスの改善に使ってください。", ""]
    return "\n".join(out)


_CSS = """
:root{--bg:#fff;--fg:#1f2328;--muted:#59636e;--line:#d1d9e0;--head:#f6f8fa;--accent:#0969da}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#9198a1;--line:#3d444d;--head:#151b23;--accent:#4493f8}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 system-ui,"Yu Gothic UI","Meiryo",sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}
h1{font-size:22px}h2{font-size:17px;border-bottom:1px solid var(--line);padding-bottom:4px;margin-top:28px}
.wrap{overflow-x:auto}table{border-collapse:collapse;font-size:13px}th,td{border:1px solid var(--line);padding:4px 8px;text-align:left}
th{background:var(--head)}img{max-width:100%;background:#fff;border:1px solid var(--line)}
.note{color:var(--muted)}input{padding:6px;width:100%;max-width:360px;box-sizing:border-box}
"""


def _img(p: Path) -> str:
    return f'<img alt="" src="data:image/png;base64,{base64.b64encode(p.read_bytes()).decode()}">'


def dashboard(a: Analysis, title: str, findings: pd.DataFrame, charts: dict[str, Path]) -> str:
    e = html.escape
    parts = [
        "<!doctype html><html lang='ja'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>{e(title)}</title><style>{_CSS}</style></head><body><main>",
        f"<h1>{e(title)}</h1><p>指摘 {a.total} 件（分類済み {a.classified} 件）</p>",
    ]
    if a.insights:
        parts.append("<h2>所見</h2><ul>" + "".join(f"<li>{e(s)}</li>" for s in a.insights) + "</ul>")
    for name, p in charts.items():
        parts.append(f"<h2>{e(name)}</h2>{_img(p)}")
    for name, df in a.tables.items():
        parts.append(f"<h2>{e(name)}</h2><div class='wrap'>{df.fillna('').to_html(border=0)}</div>")
    cols = ["id", "phase", "deliverable", "reviewer", "severity", "type", "topic", "root_cause", "text"]
    body = "".join("<tr>" + "".join(f"<td>{e(str(r[c]))}</td>" for c in cols) + "</tr>" for _, r in findings.iterrows())
    parts.append(
        "<h2>指摘の一覧</h2><input id='q' placeholder='絞り込み（例: A1、MUST、成果物名）' aria-label='絞り込み'>"
        f"<div class='wrap'><table id='t'><thead><tr>{''.join(f'<th>{c}</th>' for c in cols)}</tr></thead><tbody>{body}</tbody></table></div>"
        "<p class='note'>分類は AI・ルールによる推定を含みます。個人の評価には使わず、プロセスの改善に使ってください。</p>"
        "<script>document.getElementById('q').addEventListener('input',e=>{const q=e.target.value.toLowerCase();"
        "for(const tr of document.querySelectorAll('#t tbody tr'))tr.style.display=tr.textContent.toLowerCase().includes(q)?'':'none'})</script>"
        "</main></body></html>"
    )
    return "".join(parts)


def excel(a: Analysis, findings: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        pd.DataFrame({"所見": a.insights or ["（なし）"]}).to_excel(xw, sheet_name="所見", index=False)
        for name, df in a.tables.items():
            df.to_excel(xw, sheet_name=name[:31].replace("×", "x"))
        findings.to_excel(xw, sheet_name="指摘", index=False)
