---
target: inputs/api/member-api.md
type: api
reviewed_at: 2026-10-05
reviewer: "Copilot（確認: <担当者>）"
---

# レビュー結果: member-api

> office-review の .github/prompts/review.prompt.md の形式から作った、指紋 v2 の確認用の例です（架空）。
> No 1 と 2 は箇所・指摘の本文が同じで、ロールと観点（check_id）が違う（旧指紋は同じ、指紋 v2 は別）。

| No | check_id | 重要度 | ロール | 箇所 | 指摘 | 根拠 | 無効となるケースの確認 |
|---|---|---|---|---|---|---|---|
| 1 | API-002 | MUST | spec_manager | 3. 入力 | 検索条件の必須・任意が書かれていない | 3. 入力「検索条件」 | 入力ありのため該当しない |
| 2 | API-007 | WANT | dba | 3. 入力 | 検索条件の必須・任意が書かれていない | 3. 入力「検索条件」 | DB を使う API のため該当しない |
| 3 | ALL-003 | MUST | spec_manager | 4. 処理 | 「必要に応じて」の条件が書かれていない | 4. 処理「必要に応じて再送する」 | 例示ではなく仕様の記載のため該当しない |
