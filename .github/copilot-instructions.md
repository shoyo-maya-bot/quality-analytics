# Copilot への指示（quality-analytics）

レビュー指摘の分類・分析ツールです。説明は日本語で行ってください。

- 分類の正は `config/taxonomy.yaml`。分類を変えたら version を上げ、`prompts/` と `config/rules.yaml` も合わせる
- 指摘の分類を頼まれたら `/classify-findings` の手順に従い、定義に無い type・topic・root_cause を作らない
- `data/` と `output/` は実データ。コミットしない・本文を他の場所に書き写さない
- LLM API（有料）を呼ぶ変更では、送信前の見積もり・上限・`--yes` の確認・伏せ字（redact）を外さない
- 分析結果は個人の評価ではなくプロセスの改善に使う。レビュアーや作成者を責める書き方をしない
- コードを変更したら `ruff check . && ruff format --check . && pytest` を実行して結果を報告する
