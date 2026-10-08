"""tests/contract/office-review/expected.json を、office-review 自身の読み込み処理で作り直す.

office-review の main を取り出し、その版が固定している devtools-common を入れた環境で実行する:

    python tests/contract/office-review/regenerate.py <office-review の src> tests/contract/office-review <office-review の SHA>

--append: history.jsonl の末尾に、2026-10-05_member-api_result.md の判定（0.2.0 形式の行 3 つ + history.append の行）を足す
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from review_harness import history, results  # noqa: E402

c = Path(sys.argv[2])
items = results.load_all(c / "reviews")
h = c / "history.jsonl"
if "--append" in sys.argv:
    by = {i.key: i for i in items}
    r1, r2, r3 = (by[f"2026-10-05_member-api_result.md#{n}"] for n in (1, 2, 3))
    assert r1.fingerprint_v1 == r2.fingerprint_v1 and r1.fingerprint != r2.fingerprint

    def v020(it, decision, role=None, check_id=None):  # office-review 0.2.0 の history.append が書いた形（fingerprint_v2 なし）
        return {
            "key": it.key,
            "fingerprint": it.fingerprint_v1,
            "decision": decision,
            "check_id": check_id or it.check_id,
            "severity": it.severity,
            "role": role or it.role,
            "doc_type": it.doc_type,
            "location": it.location,
            "finding": it.finding,
            "triaged_at": "2026-10-05",
        }

    lines = [
        v020(r2, "rejected"),  # 旧指紋が同じ指摘が 2 つ → ロールと観点が一致する No 2 に結び付く
        v020(r1, "adopted", role="ops", check_id="NEW"),  # ロールと観点が一致する指摘が無い → 決められない（結び付けない）
        v020(r3, "adopted"),  # 旧指紋が同じ指摘は 1 つ → No 3 に結び付く
    ]
    with h.open("a", encoding="utf-8", newline="\n") as f:
        for rec in lines:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    history.append(h, {r1.fingerprint: "adopted"}, items, date="2026-10-06")  # 今の形式（fingerprint と fingerprint_v2）
warned = []
res = history.resolve(history.load_records(h, warned.append), items)
out = {
    "generated_by": f"office-review {sys.argv[3]}: review_harness.results.load_all / history.append / history.load_records / history.resolve"
    "（fingerprint は旧指紋 = ReviewItem.fingerprint_v1、fingerprint_v2 は ReviewItem.fingerprint）",
    "items": [dict(vars(i), key=i.key, fingerprint=i.fingerprint_v1, fingerprint_v2=i.fingerprint) for i in items],
    "decisions": dict(sorted({i.key: res.decisions[i.fingerprint]["decision"] for i in items if i.fingerprint in res.decisions}.items())),
    "orphans": res.orphans,
    "warnings": warned,
}
(c / "expected.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
print(json.dumps(out["decisions"], ensure_ascii=False, indent=0), out["warnings"], len(out["items"]))
