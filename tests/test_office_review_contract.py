"""office-review との形式の約束（コントラクトテスト）.

tests/contract/office-review/ は office-review が書く形式そのもの:
- reviews/2026-10-02_order-api_result.md と history.jsonl の先頭 3 行: office-review のリポジトリのサンプルをそのまま写したもの
- reviews/2026-10-03_stock-db_result.md: office-review のテンプレート（.github/prompts/review.prompt.md）から作った境界の例
  （表の中の \\|、\\ を含むパス）。office-review の harness lint で error 0 を確かめてある
- reviews/2026-10-04_edge_result.md: 読み込みの境界の例（BOM 付き、末尾が \\\\| のセル、No が数字でない行・列数違いの行）
- history.jsonl: 先頭に BOM。壊れた行（JSON でない・オブジェクトでない・decision が不正）を含み、
  残りの行は office-review の history.append（判定を書く処理）で書いた新しい形式（fingerprint 付き）
- expected.json: 同じファイルを office-review の results.load_all / history.load_records / history.resolve で読んだ結果
  （警告の文言を含む。作り方は expected.json の generated_by）

読み込みは devtools-common の共通部品（devtools_common.review_results）。office-review の形式が変わったら、
ここのファイルと expected.json を作り直し、このテストが通ることを確かめる。
"""

from __future__ import annotations

import json
from pathlib import Path

from quality_analytics.sources import office_review

CONTRACT = Path(__file__).resolve().parent / "contract" / "office-review"
EXPECTED = json.loads((CONTRACT / "expected.json").read_text(encoding="utf-8"))
# office-review の ReviewItem の項目 ↔ 表の列
FIELD_OF_COLUMN = {
    "No": "no",
    "check_id": "check_id",
    "重要度": "severity",
    "ロール": "role",
    "箇所": "location",
    "指摘": "finding",
    "根拠": "basis",
    "無効となるケースの確認": "invalid_check",
}


def _parsed() -> dict[str, dict]:
    return {
        i.key: dict(vars(i), key=i.key, fingerprint=i.fingerprint) for i in office_review.load_items(CONTRACT / "reviews", lambda _m: None)
    }


def test_columns_match_office_review():
    assert set(FIELD_OF_COLUMN) == set(office_review.COLUMNS)
    assert list(FIELD_OF_COLUMN) == office_review.COLUMNS  # 列の順も office-review の COLUMNS と同じ


def test_parser_reads_every_column():
    parsed = _parsed()
    assert list(parsed) == [e["key"] for e in EXPECTED["items"]]  # 順も office-review の load_all と同じ
    for exp in EXPECTED["items"]:
        assert parsed[exp["key"]] == exp, exp["key"]  # 表のすべての列・Front Matter・指紋
        assert office_review.fingerprint(exp["target"] or exp["file"], exp["location"], exp["finding"]) == exp["fingerprint"]


def test_escaped_pipes_and_backslashes():
    parsed = _parsed()
    assert parsed["2026-10-03_stock-db_result.md#1"]["finding"] == "区分の値 `A|B|C` 以外が入ったときの扱いが無い"  # \| は | に戻す
    assert "C:\\work\\stock" in parsed["2026-10-03_stock-db_result.md#2"]["finding"]  # ほかの \ は残す
    # \\| は「文字の \（エスケープのまま \\）」の後ろの区切り（セルの中の | ではない）
    assert parsed["2026-10-04_edge_result.md#1"]["location"] == "2. 入力 D:\\in\\\\"
    assert parsed["2026-10-04_edge_result.md#1"]["finding"] == "入力フォルダの末尾の区切りの扱いが無い"
    assert parsed["2026-10-04_edge_result.md#2"]["invalid_check"] == "確認\\\\"  # 行末の \\| も区切り
    assert parsed["2026-10-04_edge_result.md#5"]["finding"] == "再実行時の `a|b` の扱い"


def test_bom_and_broken_rows_are_read_like_office_review():
    """BOM 付きのレビュー結果を読み、壊れた行（No が数字でない・列数違い）は行番号付きで警告して飛ばす."""
    assert (CONTRACT / "reviews/2026-10-04_edge_result.md").read_bytes().startswith(b"\xef\xbb\xbf")
    warned: list[str] = []
    items = office_review.load_items(CONTRACT / "reviews", warned.append)
    edge = [i for i in items if i.file == "2026-10-04_edge_result.md"]
    assert [i.no for i in edge] == [1, 2, 5]
    assert (edge[0].target, edge[0].doc_type, edge[0].reviewed_at) == ("inputs/batch/nightly-batch.md", "batch", "2026-10-04")
    assert warned == [
        "警告: 2026-10-04_edge_result.md:16 を飛ばしました（No が数字ではありません）",
        "警告: 2026-10-04_edge_result.md:17 を飛ばしました（列数が 5 です）",
    ]


def test_broken_history_lines_are_skipped_with_line_numbers():
    """history.jsonl（BOM 付き）の壊れた行は、office-review と同じ文言・行番号で警告して飛ばし、ほかの判定は読む."""
    assert (CONTRACT / "history.jsonl").read_bytes().startswith(b"\xef\xbb\xbf")
    warned: list[str] = []
    df = office_review.read(CONTRACT / "reviews", CONTRACT / "history.jsonl", warned.append)
    assert [w for w in warned if "history.jsonl" in w] == EXPECTED["warnings"]
    assert df.set_index("id").loc["office-review:2026-10-04_edge_result.md#1", "decision"] == "adopted"


def test_read_maps_columns_and_decisions():
    df = office_review.read(CONTRACT / "reviews", CONTRACT / "history.jsonl").set_index("id")
    assert len(df) == len(EXPECTED["items"])
    for exp in EXPECTED["items"]:
        r = df.loc[f"office-review:{exp['key']}"]
        assert r["text"] == exp["finding"]
        assert r["severity"] == exp["severity"]
        assert r["reviewer"] == f"AI:{exp['role']}"
        assert r["deliverable"] == Path(exp["target"]).stem
        assert r["created_at"] == exp["reviewed_at"]
        assert r["phase"] == "BD"
        # 判定は office-review の history.load と同じ結果（新旧の形式・同じ指摘は後の行が有効）
        assert r["decision"] == EXPECTED["decisions"].get(exp["key"], ""), exp["key"]
    assert df.loc["office-review:2026-10-03_stock-db_result.md#1", "decision"] == "adopted"  # rejected → adopted


def test_legacy_decision_is_not_moved_to_another_finding(tmp_path: Path):
    """旧形式の判定は、その No にいまある指摘が別のものなら結び付けない（office-review と同じ）."""
    hist = tmp_path / "history.jsonl"
    rec = json.loads((CONTRACT / "history.jsonl").read_text(encoding="utf-8-sig").splitlines()[0])
    hist.write_text(json.dumps({**rec, "finding": "別の指摘"}, ensure_ascii=False) + "\n", encoding="utf-8")
    df = office_review.read(CONTRACT / "reviews", hist).set_index("id")
    assert df.loc[f"office-review:{rec['key']}", "decision"] == ""
