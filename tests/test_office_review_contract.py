"""office-review との形式の約束（コントラクトテスト）.

tests/contract/office-review/ は office-review が書く形式そのもの:
- reviews/2026-10-02_order-api_result.md と history.jsonl の先頭 3 行: office-review のリポジトリのサンプルをそのまま写したもの
- reviews/2026-10-03_stock-db_result.md: office-review のテンプレート（.github/prompts/review.prompt.md）から作った境界の例
  （表の中の \\|、\\ を含むパス）。office-review の harness lint で error 0 を確かめてある
- history.jsonl の残りの行: office-review の history.append（判定を書く処理）で書いた新しい形式（fingerprint 付き）
- expected.json: 同じファイルを office-review の results.parse / history.load で読んだ結果（作り方は expected.json の generated_by）

office-review の形式が変わったら、ここのファイルを作り直し、このテストが通るように取り込み（sources/office_review.py）を直す。
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


def _parsed() -> dict[str, tuple[dict, dict[str, str]]]:
    out = {}
    for md in sorted((CONTRACT / "reviews").glob("*_result.md")):
        meta, rows = office_review.parse_file(md)
        for r in rows:
            out[f"{md.name}#{r['No']}"] = (meta, r)
    return out


def test_columns_match_office_review():
    assert set(FIELD_OF_COLUMN) == set(office_review.COLUMNS)
    assert list(FIELD_OF_COLUMN) == office_review.COLUMNS  # 列の順も office-review の COLUMNS と同じ


def test_parser_reads_every_column():
    parsed = _parsed()
    assert sorted(parsed) == sorted(e["key"] for e in EXPECTED["items"])
    for exp in EXPECTED["items"]:
        meta, row = parsed[exp["key"]]
        for col, field in FIELD_OF_COLUMN.items():
            assert row[col] == str(exp[field]), f"{exp['key']} の {col}"
        assert str(meta["target"]) == exp["target"] and str(meta["type"]) == exp["doc_type"]
        assert str(meta["reviewed_at"]) == exp["reviewed_at"]
        assert office_review.fingerprint(exp["target"] or exp["file"], row["箇所"], row["指摘"]) == exp["fingerprint"]


def test_escaped_pipes_and_backslashes():
    _, row = _parsed()["2026-10-03_stock-db_result.md#1"]
    assert row["指摘"] == "区分の値 `A|B|C` 以外が入ったときの扱いが無い"  # \| は | に戻す
    _, row = _parsed()["2026-10-03_stock-db_result.md#2"]
    assert "C:\\work\\stock" in row["指摘"]  # ほかの \ は残す


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
    rec = json.loads((CONTRACT / "history.jsonl").read_text(encoding="utf-8").splitlines()[0])
    hist.write_text(json.dumps({**rec, "finding": "別の指摘"}, ensure_ascii=False) + "\n", encoding="utf-8")
    df = office_review.read(CONTRACT / "reviews", hist).set_index("id")
    assert df.loc[f"office-review:{rec['key']}", "decision"] == ""
