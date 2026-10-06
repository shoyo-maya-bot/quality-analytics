---
target: inputs/db/stock-db.md
type: db
reviewed_at: 2026-10-03
reviewer: "Copilot（確認: <担当者>）"
---

# レビュー結果: stock-db

> office-review の .github/prompts/review.prompt.md の形式から作った、境界の確認用の例です（架空）。

| No | check_id | 重要度 | ロール | 箇所 | 指摘 | 根拠 | 無効となるケースの確認 |
|---|---|---|---|---|---|---|---|
| 1 | DB-001 | MUST | dba | 2. テーブル定義 | 区分の値 `A\|B\|C` 以外が入ったときの扱いが無い | 2. テーブル定義「区分（A\|B\|C）」 | 値の一覧が仕様に書かれているため該当しない |
| 2 | DB-002 | NITS | dba | 3. 配置 | 出力先 C:\work\stock の区切り文字が他の設計書と違う | 3. 配置「C:\work\stock」 | 表記の揺れのため該当しない |
| 3 | NEW | Q | ops | 全体 | 夜間バッチと同時に更新されたときの排他は？ | 記載なし | 既存の観点に無い |
