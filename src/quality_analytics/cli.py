"""quality-analytics — レビュー指摘を集めて分類し、傾向を分析する.

  qa collect table 指摘一覧.xlsx                    # 指摘表（列の対応は config/sources/table.yaml）
  qa collect github --repo owner/name --since 2026-04-01   # PR のレビューコメント（GH_TOKEN）
  qa collect office-review --reviews ../office-review/reviews --history ../office-review/data/history.jsonl
  qa classify --mode rules                          # キーワードで分類（無料）
  qa classify --mode export → Copilot Chat → qa classify --mode import --labels output/labels
  qa classify --mode api                            # 費用の見積もりだけ表示（送信しない）
  qa classify --mode api --yes                      # LLM API で分類（有料。上限は config/llm.yaml）
  qa check                                          # 分類が定義に合っているか
  qa analyze                                        # output/ にレポート・ダッシュボード・Excel

取り込んだ指摘と分類の結果は data/findings.csv（既定）に貯まります。
終了コード: 0 正常 / 1 定義に合わない分類あり（check）/ 2 引数・設定・入力の誤り / 3 実行時エラー
（classify --mode api が実際の費用の上限で途中で止まったときも 3）
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from devtools_common import cli as common_cli
from devtools_common.config import load_dotenv, load_structured, require_env

from . import __version__, analyze, charts, mask, model, report, taxonomy
from .classify import exchange, llm, rules
from .sources import github, office_review, table

TOOL = "quality-analytics"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qa", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--data", type=Path, default=Path("data/findings.csv"), help="指摘の保存先（既定: data/findings.csv）")
    p.add_argument("--config-dir", type=Path, default=Path("config"))
    p.add_argument("--prompt-dir", type=Path, default=Path("prompts"))
    p.add_argument("--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("collect", help="指摘を取り込む").add_subparsers(dest="source", required=True)
    t = c.add_parser("table", help="指摘表（.xlsx / .csv / .tsv）")
    t.add_argument("files", type=Path, nargs="+")
    t.add_argument("--mapping", type=Path, help="列の対応（既定: config/sources/table.yaml）")
    g = c.add_parser("github", help="GitHub の PR レビューコメント")
    src = g.add_mutually_exclusive_group(required=True)
    src.add_argument("--repo", help="owner/name（GH_TOKEN が必要）")
    src.add_argument("--file", type=Path, help="取得済みの JSON（--save-raw で保存したもの）")
    g.add_argument("--since", default="", help="この日以降に更新された PR（YYYY-MM-DD）")
    g.add_argument("--phase", default="CD")
    g.add_argument("--only-tagged", action="store_true", help="[MUST] などのタグがあるコメントだけ")
    g.add_argument("--anonymize", action="store_true", help="ユーザー名を U-xxxxxx に置き換える")
    g.add_argument("--save-raw", type=Path, help="取得した JSON を保存する（再取得せずに試せる）")
    g.add_argument(
        "--timezone",
        default=os.environ.get("QA_TIMEZONE") or github.DEFAULT_TZ,
        help="日付にするタイムゾーン（既定: 環境変数 QA_TIMEZONE、無ければ Asia/Tokyo）",
    )
    o = c.add_parser("office-review", help="office-review のレビュー結果と判定")
    o.add_argument("--reviews", type=Path, required=True)
    o.add_argument("--history", type=Path)

    k = sub.add_parser("classify", help="指摘を分類する")
    k.add_argument("--mode", choices=["rules", "api", "export", "import"], required=True)
    k.add_argument("--overwrite", action="store_true", help="分類済みも分類し直す")
    k.add_argument("--labels", type=Path, help="import: 回答の JSON（ファイルかフォルダ）")
    k.add_argument("--out", type=Path, default=Path("output/prompts"), help="export: プロンプトの書き出し先")
    k.add_argument("--yes", action="store_true", help="api: 見積もりを確認したうえで送信する")
    k.add_argument("--max-cost", type=float, help="api: 費用の上限（USD）。既定は config/llm.yaml")
    k.add_argument("--limit", type=int, help="api / export: 先頭の N 件だけ（試行用）")

    sub.add_parser("check", help="分類が定義（config/taxonomy.yaml）に合っているか")

    a = sub.add_parser("analyze", help="集計してレポートを出す")
    a.add_argument("--out", type=Path, default=Path("output"))
    a.add_argument("--deliverables", type=Path, help="成果物のページ数（既定: config/deliverables.csv）")
    a.add_argument("--title", default="レビュー指摘の分析")
    a.add_argument("--noise-threshold", type=float, default=30.0, help="ノイズ（B）率の注意の閾値（%%）")
    a.add_argument("--no-charts", action="store_true")
    return p


def _yaml(path: Path) -> dict:
    if not path.is_file():
        raise common_cli.UsageError(f"設定ファイルが見つかりません: {path}")
    return load_structured(path) or {}


def _tax(args) -> taxonomy.Taxonomy:
    try:
        return taxonomy.load(args.config_dir / "taxonomy.yaml")
    except (OSError, ValueError) as e:
        raise common_cli.UsageError(str(e)) from None


def _prompt_dir(args) -> Path | None:
    return args.prompt_dir if (args.prompt_dir / "classify.md").is_file() else None


def _masker(args) -> mask.Masker:
    llm_conf = args.config_dir / "llm.yaml"
    extra = ((load_structured(llm_conf) or {}).get("redact") or []) if llm_conf.is_file() else []  # 互換: llm.yaml の redact
    try:
        return mask.load(args.config_dir, extra)
    except (OSError, ValueError) as e:
        raise common_cli.UsageError(f"config/mask.yaml: {e}") from None


def _load(args):
    """保存済みの指摘を読み、伏せ字をかける（伏せ字の前のバージョンで貯めたデータも、出力の前に伏せる）."""
    return model.normalize(_masker(args).frame(model.load(args.data)))


def _store(args, new, scopes: list[str] | None = None) -> int:
    """取り込んだ指摘を伏せ字にして保存する. scopes（id の先頭）で始まり、今回に無い既存の行は削除する."""
    m = _masker(args)
    new = model.normalize(new)
    names = m.names(v for c in m.pseudonymize if c in new.columns for v in new[c].astype(str))
    masked_scopes = [m.ident(s.rstrip(":"), names) + ":" for s in scopes or []]
    try:
        merged, added, updated, deleted = model.merge(m.frame(model.load(args.data)), m.frame(new), masked_scopes)
    except model.DuplicateIdError as e:
        raise common_cli.UsageError(str(e)) from None
    model.save(merged, args.data)
    print(f"{args.data}: 追加 {added} 件 / 更新 {updated} 件 / 削除 {deleted} 件（合計 {len(merged)} 件）")
    if deleted:
        print("削除: 取り込み元の表から無くなった行です（分類の結果も消えます）")
    return common_cli.ExitCode.OK


def cmd_collect(args) -> int:
    if args.source == "table":
        conf = _yaml(args.mapping or args.config_dir / "sources/table.yaml")
        frames, scopes = [], []
        for f in args.files:
            if not f.is_file():
                raise common_cli.UsageError(f"見つかりません: {f}")
            try:
                frames.append(table.read(f, conf))
            except ValueError as e:
                raise common_cli.UsageError(str(e)) from None
            scopes.append(table.scope(f, conf))
        import pandas as pd

        return _store(args, pd.concat(frames, ignore_index=True), scopes)
    if args.source == "github":
        try:
            ZoneInfo(args.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise common_cli.UsageError(f"タイムゾーンが分かりません: {args.timezone}（例: Asia/Tokyo、UTC）") from None
        if args.file:
            data = load_structured(args.file)
        else:
            load_dotenv()
            data = github.download(args.repo, github.http_fetch(require_env("GH_TOKEN")), since=args.since, tz=args.timezone)
            for note in data.get("truncated", []):
                print(f"注意: {note}", file=sys.stderr)
            print(f"{args.repo}: PR {len(data['pulls'])} 件を取得しました")
            if args.save_raw:
                import json

                args.save_raw.parent.mkdir(parents=True, exist_ok=True)
                args.save_raw.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return _store(
            args, github.to_findings(data, phase=args.phase, only_tagged=args.only_tagged, anonymize=args.anonymize, tz=args.timezone)
        )
    if not args.reviews.is_dir():
        raise common_cli.UsageError(f"フォルダが見つかりません: {args.reviews}")
    return _store(args, office_review.read(args.reviews, args.history))


def cmd_classify(args) -> int:
    tax = _tax(args)
    df = _load(args)
    if df.empty:
        raise common_cli.UsageError(f"指摘がありません（先に collect）: {args.data}")
    if args.mode == "rules":
        out, n = rules.classify(df, _yaml(args.config_dir / "rules.yaml"), tax, overwrite=args.overwrite)
        model.save(out, args.data)
        print(f"ルールで {n} 件を分類しました（classified_by=rules）")
        return common_cli.ExitCode.OK
    if args.mode == "import":
        if not args.labels or not args.labels.exists():
            raise common_cli.UsageError("--labels に回答の JSON（ファイルかフォルダ）を指定してください")
        from .classify import prompt

        out, problems = prompt.apply(df, exchange.read_labels(args.labels), tax, "copilot")
        model.save(out, args.data)
        for pr in problems:
            print(f"NG {pr}")
        print(f"取り込み: 問題 {len(problems)} 件（問題のあるものは取り込んでいません）")
        return common_cli.ExitCode.FINDINGS if problems else common_cli.ExitCode.OK

    conf = _yaml(args.config_dir / "llm.yaml")
    target = df if args.overwrite else df[df["type"] == ""]
    if args.limit:
        target = target.head(args.limit)
    if target.empty:
        print("分類が必要な指摘はありません（分類し直すときは --overwrite）")
        return common_cli.ExitCode.OK
    if args.mode == "export":
        files = exchange.export(target, tax, conf, args.out, _prompt_dir(args))
        print(
            f"{len(files)} 個のプロンプトを {args.out} に書き出しました。Copilot Chat に貼り、回答を JSON で保存して import してください。"
        )
        return common_cli.ExitCode.OK

    est = llm.estimate(target, tax, conf, _prompt_dir(args))
    print(llm.cost_table(est, conf))
    price = llm.price(conf["model"], conf)
    if price is None:
        raise common_cli.UsageError(
            f"{conf['model']} の料金が devtools_common.llm.PRICES にありません（モデル名を確かめるか、devtools-common を更新してください）"
        )
    limit = args.max_cost if args.max_cost is not None else float(conf.get("max_cost_usd", 5.0))
    if est.cost_with_margin(price, conf) > limit:
        raise common_cli.UsageError(
            f"見積もり ${est.cost_with_margin(price, conf):.3f}（再試行の余裕込み）が上限 ${limit:.2f} を超えています"
            "（--limit で件数を絞るか --max-cost）"
        )
    if not args.yes:
        print("\n送信していません。内容と費用を確認して、--yes を付けて実行してください。")
        return common_cli.ExitCode.OK
    load_dotenv()
    client = llm.make_client(conf)
    out, res = _run_subset(df, target, tax, conf, client, args, limit, price)
    model.save(out, args.data)
    print(
        f"分類 {res.classified} 件 / キャッシュ利用 {res.cached} 件 / 呼び出し {res.calls} 回 / 実際の費用 約 ${res.cost(price):.3f}"
        f"（入力 {res.input_tokens:,} / キャッシュ書き込み {res.cache_creation_input_tokens:,} / キャッシュ読み込み {res.cache_read_input_tokens:,}"
        f" / 出力 {res.output_tokens:,} トークン）"
    )
    for pr in res.problems:
        print(f"NG {pr}")
    if res.error:
        print(
            f"エラー: LLM API の呼び出しに失敗したため、残り {res.skipped} 件の分類を止めました（{res.error}）。"
            f"分類できた分は {args.data} に保存しました。時間をおいて、もう一度実行してください（分類済みは送りません）",
            file=sys.stderr,
        )
        return common_cli.ExitCode.RUNTIME
    if res.stopped:
        print(
            f"注意: 実際の費用が費用の上限 ${limit:.2f} に近づいたため、残り {res.skipped} 件の分類を止めました"
            "（分類できた分は保存しました。続けるときは --max-cost を見直して、もう一度実行してください）",
            file=sys.stderr,
        )
        return common_cli.ExitCode.RUNTIME
    return common_cli.ExitCode.FINDINGS if res.problems else common_cli.ExitCode.OK


def _run_subset(df, target, tax, conf, client, args, max_cost=None, price=None):
    cache = args.data.parent / "llm-cache.jsonl"
    sub, res = llm.run(target, tax, conf, client, cache, _prompt_dir(args), max_cost=max_cost, price=price)
    out = df.set_index("id")
    out.update(sub.set_index("id"))
    return out.reset_index()[model.COLUMNS], res


def cmd_check(args) -> int:
    tax = _tax(args)
    df = _load(args)
    problems = []
    for _, r in df[df["type"] != ""].iterrows():
        problems += [f"{r['id']}: {p}" for p in tax.check(r["type"], r["topic"], r["root_cause"], r["phase"])]
    for pr in problems:
        print(f"NG {pr}")
    print(f"分類済み {int((df['type'] != '').sum())} / {len(df)} 件、問題 {len(problems)} 件")
    return common_cli.ExitCode.FINDINGS if problems else common_cli.ExitCode.OK


def cmd_analyze(args) -> int:
    tax = _tax(args)
    df = _load(args)
    if df.empty:
        raise common_cli.UsageError(f"指摘がありません（先に collect）: {args.data}")
    deliverables = args.deliverables or args.config_dir / "deliverables.csv"
    a = analyze.run(df, tax, deliverables if deliverables.is_file() else None, noise_threshold=args.noise_threshold)
    args.out.mkdir(parents=True, exist_ok=True)
    drawn = {} if args.no_charts else charts.draw_all(a.tables, args.out / "charts")
    (args.out / "report.md").write_text(report.markdown(a, args.title, drawn, args.out), encoding="utf-8", newline="\n")
    (args.out / "dashboard.html").write_text(report.dashboard(a, args.title, df, drawn), encoding="utf-8", newline="\n")
    report.excel(a, df, args.out / "summary.xlsx")
    print(f"{args.out}: report.md / dashboard.html / summary.xlsx（グラフ {len(drawn)} 枚）")
    for s in a.insights:
        print(f"- {s}")
    return common_cli.ExitCode.OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    common_cli.configure_from_args(args)
    os.environ.setdefault("MPLBACKEND", "Agg")
    return {"collect": cmd_collect, "classify": cmd_classify, "check": cmd_check, "analyze": cmd_analyze}[args.command](args)


def entrypoint() -> None:
    sys.exit(common_cli.run_main(main))
