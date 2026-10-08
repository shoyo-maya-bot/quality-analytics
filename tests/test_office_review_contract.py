"""office-review との形式の約束（コントラクトテスト）.

tests/contract/office-review/ は office-review が書く形式そのもの:
- reviews/2026-10-02_order-api_result.md と history.jsonl の先頭 3 行: office-review のリポジトリのサンプルをそのまま写したもの
- reviews/2026-10-03_stock-db_result.md: office-review のテンプレート（.github/prompts/review.prompt.md）から作った境界の例
  （表の中の \\|、\\ を含むパス）。office-review の harness lint で error 0 を確かめてある
- reviews/2026-10-04_edge_result.md: 読み込みの境界の例（BOM 付き、末尾が \\\\| のセル、No が数字でない行・列数違いの行）
- reviews/2026-10-05_member-api_result.md: 指紋 v2 の境界の例。No 1 と 2 は箇所・本文が同じでロールと観点が違う
  （旧指紋は同じ、指紋 v2 は別）
- history.jsonl: 先頭に BOM。壊れた行（JSON でない・オブジェクトでない・decision が不正）を含む。ほかの行は
  office-review の history.append（判定を書く処理）で書いた形式: 指紋なしの古い行、fingerprint（旧指紋）だけの行
  （0.2.0 の形。旧指紋が同じ指摘が 2 つあり、ロールと観点で決まる行・決まらない行を含む）、今の形（fingerprint と fingerprint_v2）
- expected.json: 同じファイルを office-review の main（generated_by の SHA）の results.load_all / history.load_records /
  history.resolve で読んだ結果（警告の文言を含む）。items の fingerprint は旧指紋、fingerprint_v2 は office-review が
  判定の結び付けに使う指紋

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
        i.key: dict(vars(i), key=i.key, fingerprint=i.fingerprint_v1, fingerprint_v2=i.fingerprint_v2)
        for i in office_review.load_items(CONTRACT / "reviews", lambda _m: None)
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
        target = exp["target"] or exp["file"]
        assert office_review.fingerprint_v2(target, exp["location"], exp["finding"], exp["role"], exp["check_id"]) == exp["fingerprint_v2"]


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


def test_decisions_are_linked_by_fingerprint_v2_like_office_review():
    """同じ箇所・同じ本文でロールと観点が違う 2 件は、別の指摘として判定を引く（office-review の history.resolve と同じ）.

    旧指紋（対象・箇所・本文）で結び付けると、No 1 の判定（adopted）が No 2 にも付いてしまう。
    """
    df = office_review.read(CONTRACT / "reviews", CONTRACT / "history.jsonl").set_index("id")
    one, two = (df.loc[f"office-review:2026-10-05_member-api_result.md#{n}"] for n in (1, 2))
    assert (one["reviewer"], two["reviewer"]) == ("AI:spec_manager", "AI:dba")
    assert one["decision"] == "adopted"  # fingerprint_v2 の行
    assert two["decision"] == "rejected"  # 旧指紋だけの行（ロール dba・API-007 が一致する No 2 に結び付く）
    assert df.loc["office-review:2026-10-05_member-api_result.md#3", "decision"] == "adopted"  # 旧指紋が同じ指摘が 1 つ


def test_old_fingerprint_row_that_cannot_be_decided_is_not_linked(tmp_path: Path):
    """旧指紋だけの行で、同じ旧指紋の指摘が複数あり、ロールと観点でも決められないときは、どちらにも結び付けない."""
    lines = (CONTRACT / "history.jsonl").read_text(encoding="utf-8-sig").splitlines()
    recs = [json.loads(ln) for ln in lines if '"member-api_result.md#1"' in ln.replace("2026-10-05_", "")]
    ambiguous = [r for r in recs if "fingerprint_v2" not in r]
    assert len(ambiguous) == 1 and (ambiguous[0]["role"], ambiguous[0]["check_id"]) == ("ops", "NEW")
    hist = tmp_path / "history.jsonl"
    hist.write_text(json.dumps(ambiguous[0], ensure_ascii=False) + "\n", encoding="utf-8")
    df = office_review.read(CONTRACT / "reviews", hist).set_index("id")
    assert df.loc["office-review:2026-10-05_member-api_result.md#1", "decision"] == ""
    assert df.loc["office-review:2026-10-05_member-api_result.md#2", "decision"] == ""
