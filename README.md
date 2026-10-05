# quality-analytics

レビュー指摘を **集めて → 分類して → 傾向を見る** ツールです。
「どの真因の欠陥が多いか」「どの成果物に欠陥が集中しているか」「ノイズ（表記・好み）の指摘がどれだけ時間を使っているか」を数字で示し、
作成時のチェックリストやレビューの観点の改善につなげます。

```text
指摘表（Excel/CSV） ─┐
GitHub の PR コメント ─┼─ qa collect → data/findings.csv → qa classify → qa analyze → output/
office-review の結果 ─┘                                   （ルール / Copilot / LLM API）   report.md・dashboard.html・summary.xlsx
```

## すぐ使う

1. clone する
2. **Windows**: `setup.cmd` をダブルクリック ／ **macOS・Linux**: `./setup.sh`
3. `.venv` を有効にしてサンプルで試す（API は呼びません）

```bash
qa collect table samples/指摘一覧.xlsx
qa collect github --file samples/github_pulls.json
qa collect office-review --reviews samples/office-review/reviews --history samples/office-review/history.jsonl
qa classify --mode rules
qa analyze                      # output/dashboard.html をブラウザで開く
```

サンプルは架空の会員・注文システムの指摘です（`python samples/make_samples.py` で作り直せます）。

## 1. 集める（collect）

| 取り込み元 | コマンド | 補足 |
|---|---|---|
| 指摘表（.xlsx / .csv / .tsv） | `qa collect table 指摘一覧.xlsx` | 列の対応は `config/sources/table.yaml`。CSV は UTF-8 / Shift_JIS のどちらでも可 |
| GitHub の PR レビューコメント | `qa collect github --repo owner/name --since 2026-04-01` | `.env` の `GH_TOKEN`（読み取り権限）。本文の先頭の `[MUST]` `[WANT]` `[NITS]` `[Q]` を重要度として読む |
| office-review | `qa collect office-review --reviews DIR --history FILE` | レビュー結果の表と採否の判定 |

- 取り込んだ指摘は `data/findings.csv`（Excel で開ける UTF-8）に **id で追加・更新** されます。本文が変わらなければ分類は残ります
- GitHub: 返信は元の指摘の「対応内容」になり、Bot のコメントは除きます。`--anonymize` でユーザー名を伏せ、
  `--save-raw` で取得した JSON を保存できます（以後は `--file` で再取得なしに試せます）
- `data/` と `output/` は `.gitignore` 済みです。実データをコミットしないでください
- 取り込むときに個人情報を伏せます（下の「個人情報の伏せ字」）

### 個人情報の伏せ字

取り込むときに 1 回だけ伏せ、`data/findings.csv` には伏せた値だけを残します。
LLM API に送る本文、Copilot 用の書き出し、レポート（`report.md`・`dashboard.html`・`summary.xlsx`）はすべてこの値を使います。

| 対象 | 伏せ方 |
|---|---|
| 指摘者・作成者の氏名 | `U-xxxxxx`（仮名）に置き換える。本文中の同じ氏名も同じ仮名にする |
| メールアドレス・電話番号 | `＊＊＊` にする。全角・区切りなし・`+81` も対象。設定では外せない |
| `config/mask.yaml` の `words`・`patterns` | `＊＊＊` にする。社名・顧客名・システム名などを足す |

- 本文・対応内容・成果物名など、区分（工程・重要度・判定・分類）以外の列はすべて伏せる対象です
- 伏せ字の前のバージョンで貯めた `data/findings.csv` も、分類・分析の前に伏せます
- 氏名のまま集計したいときは `config/mask.yaml` の `pseudonymize` を `[]` にします。その場合も `data/` と `output/` は社外に出さないでください
- 仮名は氏名から決まるので、同じ人は毎回同じ仮名になります。ただし氏名の候補が分かれば仮名から割り出せます。仮名のレポートも社内だけで使ってください

## 2. 分類する（classify）

指摘ごとに次の 3 つを付けます（定義は `config/taxonomy.yaml`、詳しくは [docs/CLASSIFICATION.md](docs/CLASSIFICATION.md)）。

| 項目 | 値 |
|---|---|
| type（種類） | **A 欠陥**: A1 記載漏れ / A2 誤り・矛盾 / A3 曖昧　**B ノイズ**: B1 表記・体裁 / B2 好み・代案 / B3 対象外・誤指摘　**C 確認**: C1 質問・確認 |
| topic（観点） | 工程（RD 要件定義 / BD 基本設計 / DD 詳細設計 / CD 製造）ごとの一覧から 1 つ |
| root_cause（真因） | A のときだけ。R1 業務・要件の理解不足 / R2 前提・制約の確認漏れ / R3 他成果物との整合確認漏れ / R4 標準・規約の理解不足 / R5 セルフチェック不足 / R6 記述の技術 |

分類の方法は 3 つあります。どれも結果は同じ列に入り、`classified_by` に方法が残ります。

| 方法 | コマンド | 費用 | 精度・使いどころ |
|---|---|---|---|
| ルール | `qa classify --mode rules` | 無料 | キーワードによるたたき台。真因はほとんど付かない |
| Copilot Chat | `--mode export` → Chat に貼る → `--mode import --labels output/labels` | **追加費用なし**（Copilot のライセンス内） | 貼り付けの手間はかかる。精度は Copilot で選んだモデルによる。手順は `/classify-findings` |
| LLM API | `qa classify --mode api` → `--yes` | **有料**（下の表） | 件数が多いとき・定期的に回すとき |

`qa check` で、分類が定義に合っているか（観点が工程の一覧にあるか、真因が A だけに付いているか）を確かめられます。

### LLM API の費用

`qa classify --mode api` は **送信せずに費用の見積もりだけ** 表示します。確認して `--yes` を付けたときだけ送信します。

- API キーは `.env` の `ANTHROPIC_API_KEY`（Anthropic の Console で発行。従量課金）
- 見積もりが `config/llm.yaml` の `max_cost_usd`（既定 $5）を超えると実行しません（`--max-cost` で変更）
- 一度分類した指摘は `data/llm-cache.jsonl` に残り、再実行しても再送しません（費用がかからない）
- 送る本文は取り込み時に伏せたものです（上の「個人情報の伏せ字」）。送る直前にもメールアドレス・電話番号を伏せ直します。
  **社外の API に指摘の本文を送ってよいか、所属組織のルールを先に確認してください**

指摘 1,000 件の見積もり（このツールの `--mode api` の表示。サンプルと同程度の長さ、20 件ずつ 50 回の呼び出し、入力 約 11 万・出力 約 6 万トークン）:

| モデル | 料金（USD / 100 万トークン、入力 / 出力） | 1,000 件の費用 |
|---|---|---|
| Claude Haiku 4.5（既定） | $1 / $5 | 約 $0.41 |
| Claude Sonnet 5.5 | $2 / $10 | 約 $0.82 |
| Claude Opus 5.5 | $4 / $20 | 約 $1.64 |

料金は 2026-10-02 に [公式の料金表](https://platform.claude.com/docs/en/about-claude/pricing) で確認した値です。
変わることがあるので、使う前に確認して `config/llm.yaml` の `pricing` を更新してください。
分類は軽い作業なので既定は Haiku です。精度が足りないと感じたら、まず 50 件ほどを `--limit 50` で Sonnet と比べてください。

## 3. 分析する（analyze）

`qa analyze` で `output/` に次を出します。

| ファイル | 内容 |
|---|---|
| `report.md` | 所見と集計表（レビューの振り返りにそのまま貼れる） |
| `dashboard.html` | グラフと表、指摘の一覧（絞り込み付き）。1 ファイルで完結 |
| `summary.xlsx` | 集計表をシートごとに。指摘の一覧も含む |
| `charts/*.png` | レビュアー別の重要度、工程別の区分、欠陥の観点×真因（ヒートマップ） |

集計: 種類×重要度 / 工程×区分 / レビュアー別（件数・重要度・欠陥率・ノイズ率・採用率）/
成果物別（`config/deliverables.csv` のページ数があれば 件/ページ）/ 欠陥の観点×真因 / ノイズの種類×レビュアー

所見は自動で書きます（例: 真因の偏り、ノイズ率が `--noise-threshold`（既定 30%）以上のレビュアー、欠陥の密度が平均＋1σ を超える成果物）。

> 分析結果は **プロセスの改善** に使ってください。個人の評価に使うと、指摘が出にくくなり、かえって品質が下がります。

グラフの日本語は、入っているフォント（游ゴシック・メイリオ・Noto Sans CJK など）を自動で使います。文字化けする場合は
Noto Sans CJK JP などの日本語フォントを入れてください（Linux: `fonts-noto-cjk`）。

## 他のリポジトリとのつながり

- **office-review**: レビュー結果と採否を取り込み、観点（`config/checks.yaml`）を増やす根拠にする
- **ai-coding**: PR の指摘（MUST / WANT / NITS / Q）を取り込み、AI に任せる範囲やガイドラインの見直しに使う
- 重要度の区分（MUST / WANT / NITS / Q）は 3 つのリポジトリで共通です

## 開発

```bash
./setup.sh                    # Windows: setup.cmd
ruff check . && ruff format --check . && pytest
```

構成: `sources/`（取り込み）→ `mask.py`（伏せ字）→ `model.py`（共通形式と保存）→ `classify/`（ルール・プロンプト・LLM・Copilot 連携）→ `analyze.py`（集計）→ `charts.py` / `report.py`（出力）
