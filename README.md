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

必要なもの: Python 3.12 以上（`pyproject.toml` の `requires-python`。CI は 3.12 で確かめています）

1. clone する
2. **Windows**: `setup.cmd` をダブルクリック ／ **macOS・Linux**: `./setup.sh`
   （`constraints.txt` があれば `-c constraints.txt` で依存の版を CI と同じにして入れます）
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
- 指摘表の id（`table:<相対パス>:<番号>`）と取り込み直したときの扱い（表から無くなった行の削除、同じ id の重複はエラー）は
  [docs/INTERNALS.md](docs/INTERNALS.md)
- GitHub の日付は、UTC の時刻を `--timezone`（既定は環境変数 `QA_TIMEZONE`、無ければ `Asia/Tokyo`。`QA_TIMEZONE` は `.env` では効かないので、実際の環境変数にします）の日付にします。
  `--since` もその日付で比べ、それより古い PR が出たところで取得をやめます。通信は 429・5xx・接続エラーを再試行します。
  GitHub が 403 で返すレート制限（secondary rate limit・残り 0）も、待ち時間が 120 秒以内なら待って再試行します（権限の不足の 403 は再試行しません）
- GitHub の取得が途中で失敗したときは、取り終えた PR の指摘だけを取り込み、「error: 取得の途中で失敗しました…」を出して終了コード 3 にします。
  GitHub の取り込みは今回に無い行を削除しないので、時間をおいて同じコマンドで取り直せば残りが足されます（1 件も取れていなければ何も取り込みません）
- `--since` は `YYYY-MM-DD` です（`2026/04/01` や存在しない日付は引数の誤りとして終了コード 2）。`--file` と併用したときも、
  保存した JSON の PR の `updated_at` で同じように絞ります（`updated_at` の無い古い JSON の PR は絞れないので取り込み、件数を表示します）
- GitHub の取得は PR 200 件までなどの上限があり、打ち切ったときは「注意: … 打ち切りました」と表示します（詳しくは docs/INTERNALS.md）
- GitHub: 返信は元の指摘の「対応内容」になり、Bot のコメントは除きます。`--save-raw` で取得した JSON を保存できます
  （以後は `--file` で再取得なしに試せます）
- `--anonymize` は、指摘者・作成者のログイン名を取り込む時点で `U-xxxxxx` にし、本文・対応内容の中の同じログイン名
  （大文字・小文字の違いも同じ人。`@` の有無を問わず、語として現れたところ）と `@メンション`（PR に参加していない人も）も同じ仮名にします。
  `config/mask.yaml` の `pseudonymize` を `[]` にしていてもログイン名を伏せたいときに使います
  （既定の `pseudonymize` でも指摘者・作成者は仮名になりますが、参加していない人への `@メンション` は残ります）
- `data/` と `output/` は `.gitignore` 済みです。実データをコミットしないでください
- 取り込むときに個人情報を伏せます（下の「個人情報の伏せ字」）

### 個人情報の伏せ字

取り込むときに 1 回だけ伏せ、`data/findings.csv` には伏せた値だけを残します。
LLM API に送る本文、Copilot 用の書き出し、レポート（`report.md`・`dashboard.html`・`summary.xlsx`）はすべてこの値を使います。

| 対象 | 伏せ方 |
|---|---|
| 指摘者・作成者の氏名 | `U-xxxxxx`（仮名）に置き換える。本文中の同じ氏名も同じ仮名にする（5 文字以下の英数字の氏名・ログイン名は単語として現れたところだけ。`api` は `rapid` の中では置き換えない）。本文では氏名の途中の空白（半角・全角）の有無・数の違いも同じ氏名とみなす（`例題 花子`・`例題　花子`・`例題花子`）。空白で区切った氏名は、姓・名だけ（2 文字以上）の出現も置き換える（同じ姓・名の人が複数いれば `＊＊＊`） |
| メールアドレス・電話番号 | `＊＊＊` にする。全角・区切りなし・`+81` も対象。設定では外せない |
| `config/mask.yaml` の `words`・`patterns` | `＊＊＊` にする。社名・顧客名・システム名などを足す |

- 本文・対応内容・成果物名など、区分（工程・重要度・判定・分類）以外の列はすべて伏せる対象です
- id も伏せます。伏せた部分には元の値のハッシュ（8 桁）を付けるので、`指摘一覧_A社` と `指摘一覧_B社` の指摘が同じ id になって
  消えることはありません（例: `table:指摘一覧_＊＊＊~3f2a9c1b:1`）
- 伏せ字の前のバージョンで貯めた `data/findings.csv` も、分類・分析の前に伏せます
- 氏名のまま集計したいときは `config/mask.yaml` の `pseudonymize` を `[]` にします。その場合も `data/` と `output/` は社外に出さないでください
- 仮名は氏名から決まるので、同じ人は毎回同じ仮名になります。鍵（ソルト）を設定しないと、氏名の候補が分かれば仮名から割り出せます。
  `.env`（または環境変数）に `QA_PSEUDONYM_SALT=<長いランダムな文字列>` を書くと、その鍵の HMAC-SHA256 から仮名を作るので、鍵を知らなければ割り出せません（推奨）。
  鍵を設定すると、以前に鍵なしで貯めた `data/findings.csv` の仮名とはつながりません（同じ人が別の仮名になる）。鍵は一度決めたら変えず、社外に出さないでください。
  仮名のレポートも社内だけで使ってください
- 区切りの無い氏名（`例題花子`）は、姓だけ・名だけの出現を判別できません。姓だけで書かれることが多いときは、`config/mask.yaml` の `words` に足してください

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

- API キーは `.env` の `ANTHROPIC_API_KEY`（Anthropic の Console で発行。従量課金）。社内ゲートウェイ等を使うときは
  `ANTHROPIC_BASE_URL`（と必要なら `ANTHROPIC_AUTH_TOKEN`）を設定します
- 見積もり（再試行の分の余裕 `retry_margin`。既定 30% を含む）が `config/llm.yaml` の `max_cost_usd`（既定 $5）を超えると
  実行しません（`--max-cost` で変更）
- 実行中も **実際にかかった費用**（API が返すトークン数）を数え、次の呼び出しで上限を超えそうなら残りの分類を止めます。
  分類できた分は保存し、止めた件数を表示して終了コード 3 で終わります
- 一度分類した指摘は `data/llm-cache.jsonl` に残り、再実行しても再送しません（費用がかからない）。
  途中で止まって壊れた行があっても読み飛ばして警告し、その指摘だけ分類し直します。
- 通信の失敗（再試行しても失敗）で止まったときも、それまでに分類できた分は `data/findings.csv` に保存してから終了します（終了コード 3）。
  キャッシュのキーには **プロンプトのハッシュ**（`prompts/classify.md`・工程の目安・`config/taxonomy.yaml` の定義）が入るので、
  プロンプトや定義を直すと、その工程の指摘は分類し直します
- 送る本文は取り込み時に伏せたものです（上の「個人情報の伏せ字」）。送る直前にもメールアドレス・電話番号を伏せ直します。
  **社外の API に指摘の本文を送ってよいか、所属組織のルールを先に確認してください**
- 定義に合わない回答や `max_output_tokens` で切れた回答は取り込まずに `NG` として表示します。通信・キャッシュ・再試行の仕組みは [docs/INTERNALS.md](docs/INTERNALS.md)

指摘 1,000 件の見積もり（このツールの `--mode api` の表示。サンプルと同程度の長さ、20 件ずつ 51 回の呼び出し、入力 約 13.5 万・出力 約 8 万トークン。
キャッシュが効かない前提の安全側の値）:

| モデル | 料金（USD / 100 万トークン、入力 / 出力） | 1,000 件の費用 |
|---|---|---|
| Claude Haiku 4.5（既定） | $1 / $5 | 約 $0.54 |
| Claude Sonnet 5.5 | $2 / $10 | 約 $1.07 |
| Claude Opus 5.5 | $4 / $20 | 約 $2.14 |

料金は devtools-common の共通の料金表（`devtools_common.llm.PRICES`。確認日はそのコメント）を使います。
変わることがあるので、使う前に [公式の料金表](https://platform.claude.com/docs/en/about-claude/pricing) で確かめてください。
料金表が古いときは devtools-common を更新します（`config/llm.yaml` に `pricing` を書くと上書きできますが、警告が出ます。非推奨）。
分類は軽い作業なので既定は Haiku です。精度が足りないと感じたら、まず 50 件ほどを `--limit 50` で Sonnet と比べてください。

## 3. 分析する（analyze）

指摘の本文や LLM の理由が `=` などで始まっても、`summary.xlsx` では数式にせず文字列のセルにします（数式注入の対策）。
`data/findings.csv` でも `= + - @`・タブ・CR で始まる値の先頭に `'` を付けて保存し、読むときに外します。

`qa analyze` で `output/` に次を出します。

| ファイル | 内容 |
|---|---|
| `report.md` | 所見と集計表（レビューの振り返りにそのまま貼れる） |
| `dashboard.html` | グラフと表、指摘の一覧（絞り込み付き）。1 ファイルで完結 |
| `summary.xlsx` | 集計表をシートごとに。指摘の一覧も含む |
| `charts/*.png` | レビュアー別の重要度、工程別の区分、欠陥の観点×真因（ヒートマップ） |

集計: 種類×重要度 / 工程×区分 / レビュアー別（件数・分類済み件数・重要度・欠陥率・ノイズ率・採用率。欠陥率・ノイズ率の分母は全体の率と同じく分類済みの件数。
分類済みが 0 件のレビュアーの欠陥率・ノイズ率と、判定が 0 件のレビュアーの採用率は 0% ではなく空欄）/
成果物別（`config/deliverables.csv` のページ数があれば 件/ページ）/ 欠陥の観点×真因 / ノイズの種類×レビュアー

`config/deliverables.csv`（`--deliverables` で変更）は見出しが `deliverable,pages` の CSV です。成果物名には指摘と同じ伏せ字
（`config/mask.yaml`）をかけてから結合するので、伏せる語を含む成果物名もそのまま書けます。`deliverable`・`pages` の列が無い、
`pages` が正の数でない、`--deliverables` のファイルが無いときは、設定の誤りとして終了コード 2 で止めます。

所見は自動で書きます（例: 真因の偏り、ノイズ率が `--noise-threshold`（既定 30%）以上のレビュアー、欠陥の密度が平均＋1σ を超える成果物）。

> 分析結果は **プロセスの改善** に使ってください。個人の評価に使うと、指摘が出にくくなり、かえって品質が下がります。

グラフの日本語は、入っているフォント（游ゴシック・メイリオ・Noto Sans CJK など）を自動で使います。文字化けする場合は
Noto Sans CJK JP などの日本語フォントを入れてください（Linux: `fonts-noto-cjk`）。

## 終了コード

| コード | 意味 |
|---|---|
| 0 | 正常 |
| 1 | 定義に合わない分類がある（`qa check`、`qa classify --mode import`・`--mode api` の `NG`） |
| 2 | 引数・設定・入力の誤り（`--since` の形式、`config/` の誤り、`classify --mode api` の見積もりが上限を超えたときなど） |
| 3 | 実行時エラー（`collect github` の取得が途中で失敗した、`classify --mode api` が通信の失敗や実際の費用の上限で途中で止まったとき） |

## 他のリポジトリとのつながり

- **office-review**: レビュー結果と採否を取り込み、観点（`config/checks.yaml`）を増やす根拠にする
- **ai-coding**: PR の指摘（MUST / WANT / NITS / Q）を取り込み、AI に任せる範囲やガイドラインの見直しに使う
- 重要度の区分（MUST / WANT / NITS / Q）は 3 つのリポジトリで共通です
- office-review の形式との約束（読み方・判定の結び付け・コントラクトテスト）は [docs/INTERNALS.md](docs/INTERNALS.md)

## 開発

```bash
./setup.sh                    # Windows: setup.cmd（どちらも constraints.txt で依存の版を CI と同じにする）
# 手で入れるとき: pip install -e ".[dev]" -c constraints.txt（constraints.txt は GitHub Actions の deps-lock が作る）
ruff check . && ruff format --check . && pytest
```

CI は Linux と Windows でテストを流し、bandit（SAST）と pip-audit（依存の脆弱性）も行います。

内部の仕様（構成・id・office-review との形式の約束・LLM の通信）は [docs/INTERNALS.md](docs/INTERNALS.md)、変更履歴は [CHANGELOG.md](CHANGELOG.md) です。

## セキュリティ・ライセンス

- 脆弱性を見つけたら、公開の Issue ではなく [SECURITY.md](SECURITY.md) の方法で知らせてください
- ライセンスは [MIT](LICENSE) です
