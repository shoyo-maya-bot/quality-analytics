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
- 指摘表の id は `table:<取り込み元ルートからの相対パス（拡張子なし）>:<番号の列の値>` です（例: `table:samples/指摘一覧:1`、
  `table:2026上期/指摘一覧:1`）。ルートは `--root`（既定はカレントフォルダ）で、いつも同じフォルダから（または同じ `--root` で）
  取り込んでください。番号が空の行は、行の位置ではなく内容（成果物・指摘者・指摘日・本文）のハッシュで id を付けます
- 同じ表を取り込み直したとき、表から無くなった行は `data/findings.csv` から **削除** します（件数を表示）。削除の範囲は
  その表のパスなので、別のフォルダの同じ名前の表（`2026上期/指摘一覧.xlsx` と `2026下期/指摘一覧.xlsx`）の行は消えません
- 以前の版（id がファイル名だけ: `table:指摘一覧:1`）で貯めた行は、取り込み直したときに、同じ番号・同じ本文の行をパスの id に
  置き換えます（分類の結果は引き継ぎます）。置き換えられない行が残ったときは件数を表示します
- 中身の違う指摘に同じ id が付いたとき（番号の重複、別のフォルダの同じ名前のファイルなど）は、片方を捨てずにエラーで止めます
- GitHub の日付は、UTC の時刻を `--timezone`（既定は環境変数 `QA_TIMEZONE`、無ければ `Asia/Tokyo`）の日付にします。
  `--since` もその日付で比べ、それより古い PR が出たところで取得をやめます。通信は 429・5xx・接続エラーを再試行します
- `--since` は `YYYY-MM-DD` です（`2026/04/01` や存在しない日付は引数の誤りとして終了コード 2）。`--file` と併用したときも、
  保存した JSON の PR の `updated_at` で同じように絞ります（`updated_at` の無い古い JSON の PR は絞れないので取り込み、件数を表示します）
- GitHub の取得は PR 200 件まで、1 つの一覧（PR・コメント・レビュー）は 50 ページ（5000 件）までです。
  そこで打ち切ったときは黙って捨てず、「注意: … 打ち切りました」と件数を表示します（`--since` で期間を絞ってください）
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
- id も伏せます。伏せた部分には元の値のハッシュ（8 桁）を付けるので、`指摘一覧_A社` と `指摘一覧_B社` の指摘が同じ id になって
  消えることはありません（例: `table:指摘一覧_＊＊＊~3f2a9c1b:1`）
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

通信は devtools-common の `devtools_common.llm`（`LLMClient`）を使います。

- 何度も送る「分類の定義」はシステムプロンプトにしてプロンプトキャッシュを使います（2 回目以降が安くなる。
  ただしモデルごとの最小の長さより短いとキャッシュされません）。指摘の本文だけを毎回送ります
- 回答はツール（JSON Schema）で受け取り、type・topic（その工程の観点）・root_cause は定義の値だけを受け付けます。
  合わない回答は 1 回だけ再試行し、それでも合わなければ取り込まずに `NG` として表示します
- 出力が `max_output_tokens` で切れたら、その回の結果は使わずに `NG` を表示します（`batch_size` を減らすか `max_output_tokens` を増やす）
- 混雑（429・529 など）や接続エラーは `retry-after` に従って再試行します（`max_retries`。既定 3 回）。
  届いて処理された後に接続が切れた場合は、再送した分も課金されることがあります
- 実行後に、呼び出し回数・トークン数（キャッシュの書き込み・読み込みを含む）・実際の費用を表示します。
  `--log-level INFO` で呼び出しごとのログ（トークン数・所要時間・request-id・費用）も出ます

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

### office-review との形式の約束（テストで確かめています）

office-review が書くレビュー結果（`reviews/*_result.md` の表: No / check_id / 重要度 / ロール / 箇所 / 指摘 / 根拠 /
無効となるケースの確認）と判定（`data/history.jsonl`）は、devtools-common の共通部品 `devtools_common.review_results`
（office-review と同じ部品）で読みます。形式は **コントラクトテスト**（`tests/test_office_review_contract.py`）で確かめています。

- レビュー結果は UTF-8（BOM 付きでもよい）。表の中の `\|` はセルの中の `|`、`\\|` は「文字の `\`」の後ろの区切りです
- 列数が違う行・No が数字でない行、判定の壊れた行（JSON でない・`decision`（adopted / rejected）や `check_id` が無い・
  それらや指紋が文字列でない・`fingerprint_v2` も `fingerprint` も `key` も無い）は、
  「警告: <ファイル名>:<行番号> を飛ばしました（理由）」を標準エラーに出して飛ばします（ほかの行は取り込みます）
- 判定は office-review と同じく **指紋 v2**（`fingerprint_v2`: 対象・箇所・指摘の本文・ロール・観点（check_id）のハッシュ）で
  指摘に結び付けます（devtools-common の `resolve_history_v2`）。同じ箇所に同じ文の指摘を別のロール・観点が出しても、
  それぞれの判定を引きます
  - `fingerprint_v2` の無い行（office-review 0.2.0 までに記録した行。`fingerprint` は旧指紋＝対象・箇所・本文）は、
    旧指紋が同じ指摘に結び付けます。旧指紋が同じ指摘が複数あるときは、行の `role` と `check_id` が一致するものに結び付け、
    決められなければ結び付けません
  - 指紋の無い古い行（`key` = ファイル名#No だけ）は、その No にいまある指摘が同じとき（本文と check_id が一致）だけ使います
- history.jsonl の 1 行（office-review の `harness triage` / ダッシュボードが書く形）:
  `{"key": "<ファイル名>#<No>", "fingerprint": "fp:…（旧指紋）", "fingerprint_v2": "fp:…", "decision": "adopted", "check_id": "…", "role": "…", …}`
- `tests/contract/office-review/` に、office-review が書く形式そのもののファイルを置いています（office-review のサンプルを写したもの、
  テンプレートから作った境界の例（表の中の `\|`、BOM、末尾が `\\|` のセル、壊れた行、ロールと観点だけが違う同じ指摘）、
  office-review の判定を書く処理で書いた行（指紋なし・旧指紋だけ・今の形））
- `expected.json` は、同じファイルを office-review 自身の読み込み処理（main の `results.load_all` / `history.load_records` /
  `history.resolve`）で読んだ結果（警告を含む）です。取り込みが表のすべての列・両方の指紋・判定を同じように読めることを確かめます
- 取り込み直すと、前回取り込んだ office-review の指摘のうち今回のレビュー結果に無いもの（消したファイル・行）は削除します
  （指摘表の取り込み直しと同じ扱い。ほかの取り込み元の指摘は消しません）
- office-review の形式が変わったら、`tests/contract/office-review/regenerate.py` で `expected.json` を作り直し
  （作った版は `expected.json` の `generated_by`）、このテストが通ることを確かめます

## 開発

```bash
./setup.sh                    # Windows: setup.cmd（どちらも constraints.txt で依存の版を CI と同じにする）
# 手で入れるとき: pip install -e ".[dev]" -c constraints.txt（constraints.txt は GitHub Actions の deps-lock が作る）
ruff check . && ruff format --check . && pytest
```

CI は Linux と Windows でテストを流し、bandit（SAST）と pip-audit（依存の脆弱性）も行います。

構成: `sources/`（取り込み）→ `mask.py`（伏せ字）→ `model.py`（共通形式と保存）→ `classify/`（ルール・プロンプト・LLM・Copilot 連携）→ `analyze.py`（集計）→ `charts.py` / `report.py`（出力）

## 変更点

### 2026-10（fix/score-up-1007）

利用者の動きが変わるもの:

- id の伏せ字: `config/mask.yaml` の `words` などで伏せた部分を含む id は、`…＊＊＊~<ハッシュ>…` の形に変わります。
  以前の形で貯めた行は別の指摘として残るので、気になるときは `data/findings.csv` を消して取り込み直してください
- 指摘表の番号が空の行の id が、行の位置（`1`、`2`…）から内容のハッシュ（`h` + 10 桁）に変わります。
  取り込み直すと、古い位置の id の行は「表から無くなった行」として削除されます（分類はし直しになります）
- 指摘表を取り込み直したとき、表から無くなった行を削除します（以前は残り続け、件数が二重に数えられていました）
- 中身の違う同じ id があると、取り込みをエラー（終了コード 2）で止めます（以前は後の行だけを残していました）
- `data/findings.csv` で `= + - @` で始まる値の先頭に `'` を付けます（読み込むときに外します）。`'` で始まる値は `''` になります
- GitHub の日付が UTC から日本時間（`--timezone`）になります。日本時間の 0〜9 時のコメントの日付が 1 日後ろにずれます
- `classify --mode api` の見積もりに再試行の余裕（30%）が入り、上限に近い件数では実行しなくなることがあります。
  実際の費用で途中で止まったときは終了コード 3 です
- `model.merge` の戻り値に削除件数が増えました（`(結果, 追加, 更新, 削除)`）

## セキュリティ・ライセンス

- 脆弱性を見つけたら、公開の Issue ではなく [SECURITY.md](SECURITY.md) の方法で知らせてください
- ライセンスは [MIT](LICENSE) です
