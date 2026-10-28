# 内部の仕様

利用者向けの使い方は [README](../README.md)、変更履歴は [CHANGELOG](../CHANGELOG.md) です。このページは開発・保守のための説明です。

## 構成

`sources/`（取り込み）→ `mask.py`（伏せ字）→ `model.py`（共通形式と保存）→ `classify/`（ルール・プロンプト・LLM・Copilot 連携）→ `analyze.py`（集計）→ `charts.py` / `report.py`（出力）

## 指摘表の id と取り込み直し

- 指摘表の id は `table:<取り込み元ルートからの相対パス（拡張子なし）>:<番号の列の値>` です（例: `table:samples/指摘一覧:1`、
  `table:2026上期/指摘一覧:1`）。ルートは `--root`（既定はカレントフォルダ）で、いつも同じフォルダから（または同じ `--root` で）
  取り込んでください。番号が空の行は、行の位置ではなく内容（成果物・指摘者・指摘日・本文）のハッシュで id を付けます
- 同じ表を取り込み直したとき、表から無くなった行は `data/findings.csv` から **削除** します（件数を表示）。削除の範囲は
  その表のパスなので、別のフォルダの同じ名前の表（`2026上期/指摘一覧.xlsx` と `2026下期/指摘一覧.xlsx`）の行は消えません
- 以前の版（id がファイル名だけ: `table:指摘一覧:1`）で貯めた行は、取り込み直したときに、同じ番号・同じ本文の行をパスの id に
  置き換えます（分類の結果は引き継ぎます）。置き換えられない行が残ったときは件数を表示します
- 中身の違う指摘に同じ id が付いたとき（番号の重複、別のフォルダの同じ名前のファイルなど）は、片方を捨てずにエラーで止めます

## GitHub の取得の上限

- GitHub の取得は PR 200 件まで、1 つの一覧（PR・コメント・レビュー）は 50 ページ（5000 件）までです。
  そこで打ち切ったときは黙って捨てず、「注意: … 打ち切りました」と件数を表示します（`--since` で期間を絞ってください）

## LLM API の通信

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
- 回答の検証で定義に合わなかった指摘は、id（`table:指摘一覧:1` のように `:` を含む）ごとに判別し、結果のキャッシュには残しません（次の実行で送り直す）

## office-review との形式の約束（テストで確かめています）

office-review が書くレビュー結果（`reviews/*_result.md` の表。取り込みは `--reviews` のフォルダの `*.md` をすべてファイル名順に読み、表の無いファイルは 0 件: No / check_id / 重要度 / ロール / 箇所 / 指摘 / 根拠 /
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
- id は `office-review:<ファイル名>#<No>`、`--project <名前>` を付けたときは `office-review:<名前>:<ファイル名>#<No>` です
- 取り込み直すと、前回取り込んだ office-review の指摘のうち今回のレビュー結果に無いもの（消したファイル・行）は削除します
  （指摘表の取り込み直しと同じ扱い。ほかの取り込み元の指摘は消しません）。削除の範囲は、`--project` を付けたときはその名前の id
  （`office-review:<名前>:`）、付けないときはプロジェクト名の無い id（`office-review:` の後ろに `:` が無いもの。`model.merge` の
  `replace_flat_scopes`）だけで、ほかのプロジェクトの指摘は消しません
- office-review の形式が変わったら、`tests/contract/office-review/regenerate.py` で `expected.json` を作り直し
  （作った版は `expected.json` の `generated_by`）、このテストが通ることを確かめます
