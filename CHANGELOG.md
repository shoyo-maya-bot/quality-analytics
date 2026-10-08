# 変更履歴

## 未リリース

### 変更（office-review の判定の結び付け: 指紋 v2）

- `qa collect office-review` の判定（history.jsonl）の結び付けを、office-review と同じ **指紋 v2**（対象・箇所・本文・ロール・観点）に
  しました（devtools-common の `resolve_history_v2`）。これまでは旧指紋（対象・箇所・本文）で引いていたため、同じ箇所・同じ本文で
  ロールや観点が違う 2 件の指摘に、片方の判定が両方に付いていました。`fingerprint_v2` の無い行（office-review 0.2.0 まで）は
  旧指紋で結び付け、同じ旧指紋の指摘が複数あればロールと観点が一致するものに結び付けます
- **指摘表の id と取り込み直しの範囲を、ファイル名ではなく取り込み元ルート（`--root`、既定はカレントフォルダ）からの相対パスに**
  しました（`table:<相対パス>:<番号>`）。これまでは別のフォルダの同じ名前の表を取り込み直すと、もう片方の表の行を
  「表から消えた行」として削除していました。以前の id（ファイル名だけ）の行は、取り込み直したときに同じ番号・同じ本文の行を
  新しい id に置き換えます（分類は引き継ぐ）
- devtools-common の固定を `cbe97d5` → `6a263b8`（指紋 v2 の共通部品、history.jsonl の型の誤った行・識別子の無い行を飛ばす）に更新

### 変更（office-review の取り込み）

`qa collect office-review` の読み込みを、devtools-common の共通部品 `devtools_common.review_results`（office-review と同じ部品）に
置き換えました。office-review と同じ規則で読むため、次の点で取り込みの結果が変わります。

- **BOM 付きのファイルを読める**: BOM 付き UTF-8 のレビュー結果は、これまで Front Matter を読めず、成果物名がファイル名・
  作成日が空になっていました。今後は Front Matter（`target`・`type`・`reviewed_at`）を読みます。BOM 付きの history.jsonl は、
  これまで JSON の誤りで取り込み全体が止まっていましたが、今後は読めます
- **末尾が `\\|` のセル**: `\\|` は「文字の `\`」の後ろの区切りとして扱います（これまでは `\|`（セルの中の `|`）と見なし、
  セルがつながって列がずれていました）。例: `| 2. 入力 D:\in\\| 入力フォルダの… |` は、箇所 `2. 入力 D:\in\\`・指摘 `入力フォルダの…`。
  セルの文字は `\|` だけを `|` に戻し、ほかのエスケープはそのまま残します（office-review と同じ。指紋が変わらないように）
- **壊れた行を飛ばす**: 表の列数が違う行・No が数字でない行は、これまで空の列や `#x` のような id のまま取り込んでいましたが、
  今後は「警告: <ファイル名>:<行番号> を飛ばしました（理由）」を標準エラーに出して飛ばします。history.jsonl の壊れた行
  （JSON でない・オブジェクトでない・`decision`（adopted / rejected）か `check_id` が無い）は、これまで取り込み全体が止まるか
  そのまま使っていましたが、今後は行番号付きで警告して飛ばします（office-review と同じ。`check_id` の無い判定の行は使いません）
- 指摘の表は、列名が office-review の 8 列（No / check_id / 重要度 / ロール / 箇所 / 指摘 / 根拠 / 無効となるケースの確認）と
  この順で一致する最初の表だけを読みます（office-review と同じ）

### 修正

- `qa collect office-review` で取り込み直したとき、消したレビュー結果（ファイル・行）の指摘が `data/findings.csv` に残り、
  二重に数えていた不具合を直しました。前回取り込んだ office-review の指摘のうち今回に無いものは削除します
  （指摘表の取り込み直しと同じ扱い。ほかの取り込み元の指摘は消しません）

### 依存

- devtools-common の固定を `2e25f8a` → `cbe97d5`（`devtools_common.review_results` を含む版）に更新
- サンプル（`samples/office-review/history.jsonl`）を office-review の data/history.jsonl と同じ形式（`check_id` 付き）にしました
