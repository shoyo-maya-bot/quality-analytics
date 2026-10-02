"""quality-analytics のテスト. サンプル生成スクリプトで入力を一時ディレクトリに作って確かめる（外部 API は呼ばない）."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path

import pandas as pd
import pytest
import yaml
from devtools_common.cli import run_main

from quality_analytics import analyze, model, taxonomy
from quality_analytics.classify import exchange, llm, prompt
from quality_analytics.cli import main
from quality_analytics.sources import github, office_review, table

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
TAX = taxonomy.load(CONFIG / "taxonomy.yaml")

_spec = importlib.util.spec_from_file_location("make_samples", ROOT / "samples/make_samples.py")
assert _spec and _spec.loader
make_samples = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(make_samples)


@pytest.fixture()
def samples(tmp_path: Path) -> Path:
    make_samples.generate(tmp_path / "samples")
    return tmp_path / "samples"


def run(tmp_path: Path, *args: str) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = run_main(
            main,
            ["--data", str(tmp_path / "data/findings.csv"), "--config-dir", str(CONFIG), "--prompt-dir", str(ROOT / "prompts"), *args],
        )
    return rc, out.getvalue() + err.getvalue()


def collect_all(tmp_path: Path, samples: Path) -> None:
    assert run(tmp_path, "collect", "table", str(samples / "指摘一覧.xlsx"))[0] == 0
    assert run(tmp_path, "collect", "github", "--file", str(samples / "github_pulls.json"))[0] == 0
    rc, _ = run(
        tmp_path, "collect", "office-review", "--reviews", str(samples / "office-review/reviews"),
        "--history", str(samples / "office-review/history.jsonl"),
    )  # fmt: skip
    assert rc == 0


# --- 共通形式 ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[MUST] 直す", ("MUST", "直す")),
        ("【WANT】 直す", ("WANT", "直す")),
        ("nit: typo", ("NITS", "typo")),
        ("Q：理由は？", ("Q", "理由は？")),
        ("タグなし", ("", "タグなし")),
    ],
)
def test_split_tag(text, expected):
    assert model.split_tag(text) == expected


def test_merge_keeps_classification_when_text_is_same():
    base = model.normalize(pd.DataFrame([{"id": "a", "text": "x", "type": "A1", "classified_by": "human"}]))
    again = model.normalize(pd.DataFrame([{"id": "a", "text": "x", "decision": "adopted"}, {"id": "b", "text": "y"}]))
    merged, added, updated = model.merge(base, again)
    row = merged.set_index("id").loc["a"]
    assert (added, updated, row["type"], row["decision"]) == (1, 1, "A1", "adopted")
    changed = model.normalize(pd.DataFrame([{"id": "a", "text": "x を直した"}]))
    merged, _, _ = model.merge(base, changed)
    assert merged.set_index("id").loc["a", "type"] == ""  # 本文が変わったら分類し直す


# --- 取り込み ----------------------------------------------------------------------------------


def test_table_xlsx(samples: Path):
    conf = yaml.safe_load((CONFIG / "sources/table.yaml").read_text(encoding="utf-8"))
    df = table.read(samples / "指摘一覧.xlsx", conf)
    assert len(df) == len(make_samples.ROWS)
    r = df.iloc[0]
    assert (r["id"], r["phase"], r["severity"], r["decision"]) == ("table:指摘一覧:1", "RD", "MUST", "adopted")
    assert set(df["severity"]) == {"MUST", "WANT", "NITS", "Q"}


def test_table_csv_cp932_and_tag_in_text(tmp_path: Path):
    p = tmp_path / "list.csv"
    p.write_text("番号,内容,誰\n1,[NITS] 誤字,R1\n2,,R1\n", encoding="cp932")
    df = table.read(p, {"columns": {"id": "番号", "text": "内容", "reviewer": "誰"}})
    assert len(df) == 1  # 本文の無い行は読まない
    assert (df.iloc[0]["severity"], df.iloc[0]["text"]) == ("NITS", "誤字")


def test_table_missing_column_is_usage_error(tmp_path: Path, samples: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("columns: {text: 指摘本文}\n", encoding="utf-8")
    rc, out = run(tmp_path, "collect", "table", str(samples / "指摘一覧.xlsx"), "--mapping", str(bad))
    assert rc == 2 and "指摘本文" in out


def test_github(samples: Path):
    data = json.loads((samples / "github_pulls.json").read_text(encoding="utf-8"))
    df = github.to_findings(data)
    assert len(df) == 6  # Bot・返信・空のレビュー本文は除く
    first = df.set_index("id").loc["github:example-org/member-service#12/c1001"]
    assert (first["severity"], first["response"], first["author"]) == ("MUST", "NotFoundException に直しました", "dev-1")
    assert "MemberService.java" in first["text"]
    assert len(github.to_findings(data, only_tagged=True)) == 5
    anon = github.to_findings(data, anonymize=True)
    assert all(v.startswith("U-") for v in anon["reviewer"])


def test_github_download_pages():
    calls: list[str] = []

    def fetch(url: str):
        calls.append(url)
        if "/pulls?" in url:
            return [{"number": 1, "updated_at": "2026-09-01T00:00:00Z", "user": {"login": "a"}}] if "page=1" in url else []
        return []

    data = github.download("o/r", fetch, since="2026-08-01")
    assert [p["number"] for p in data["pulls"]] == [1]
    assert any("/pulls/1/comments" in c for c in calls)


def test_office_review(samples: Path):
    df = office_review.read(samples / "office-review/reviews", samples / "office-review/history.jsonl")
    assert len(df) == 6
    d = df.set_index("id")
    assert d.loc["office-review:2026-10-02_order-api_result.md#1", "decision"] == "adopted"
    assert d.loc["office-review:2026-10-02_order-api_result.md#4", "decision"] == "rejected"
    assert set(df["reviewer"]) == {"AI:spec_manager", "AI:infra", "AI:dba", "AI:ops"}


# --- 分類 --------------------------------------------------------------------------------------


def test_rules_classify_sample(tmp_path: Path, samples: Path):
    collect_all(tmp_path, samples)
    rc, out = run(tmp_path, "classify", "--mode", "rules")
    assert rc == 0 and "50 件" in out
    df = model.load(tmp_path / "data/findings.csv")
    by_text = df.set_index("text")["type"]
    assert by_text["「メールアドレス」と「メアド」の表記ゆれ"] == "B1"
    assert by_text["会員数はどの程度を想定していますか？"] == "C1"
    assert by_text["画面の項目は左右 2 列にした方が見やすい"] == "B2"
    assert by_text["退会した会員のデータをいつまで保持するかの記載が無い"] == "A1"
    assert by_text["ジョブの処理ステップの順序が処理フロー図と異なる"] == "A2"
    assert run(tmp_path, "check")[0] == 0


def test_taxonomy_check():
    assert TAX.check("A1", "API・IF", "R2", "BD") == []
    assert len(TAX.check("Z9", "", "", "BD")) == 1
    assert len(TAX.check("A1", "業務要件", "", "BD")) == 1  # 工程 BD の観点ではない
    assert len(TAX.check("B1", "", "R1", "BD")) == 1  # 真因は A だけ


def _sample_df() -> pd.DataFrame:
    return model.normalize(
        pd.DataFrame(
            [
                {"id": "1", "phase": "BD", "severity": "MUST", "text": "連絡先 taro@example.com の桁が書かれていない"},
                {"id": "2", "phase": "BD", "severity": "NITS", "text": "誤字"},
                {"id": "3", "phase": "RD", "severity": "Q", "text": "件数は？"},
            ]
        )
    )


def test_prompt_build_is_per_phase_and_redacted():
    df = _sample_df()
    text = prompt.build("BD", df[df["phase"] == "BD"], TAX, redact_patterns=[r"[\w.+-]+@[\w-]+\.[\w.]+"], prompt_dir=ROOT / "prompts")
    assert "API・IF" in text and "業務要件" not in text  # BD の観点だけ
    assert "taro@example.com" not in text and "＊＊＊" in text
    assert "基本設計では" in text  # 工程の目安（prompts/phase-BD.md）


def test_prompt_parse_and_apply():
    labels = prompt.parse('```json\n[{"id": "1", "type": "A1", "topic": "API・IF", "root_cause": "R2", "reason": "桁"},'
                          ' {"id": "2", "type": "B1", "topic": "", "root_cause": "R1"}]\n```')  # fmt: skip
    out, problems = prompt.apply(_sample_df(), labels, TAX, "copilot")
    assert out.set_index("id").loc["1", "type"] == "A1"
    assert out.set_index("id").loc["2", "type"] == ""  # B に真因は付けられない → 取り込まない
    assert len(problems) == 1
    with pytest.raises(ValueError):
        prompt.parse("分類できませんでした")


def test_llm_run_uses_cache_and_retries(tmp_path: Path):
    conf = yaml.safe_load((CONFIG / "llm.yaml").read_text(encoding="utf-8"))
    sent: list[str] = []
    answers = iter(
        [
            "ごめんなさい",  # 形式の誤り → 再試行
            '[{"id": "1", "type": "A1", "topic": "API・IF", "root_cause": "R2", "reason": "x"}, {"id": "2", "type": "B1", "topic": ""}]',
            '[{"id": "3", "type": "C1", "topic": "非機能要件"}]',
        ]
    )

    def send(model_name: str, text: str, max_tokens: int):
        sent.append(text)
        return next(answers), 1000, 100

    cache = tmp_path / "cache.jsonl"
    out, res = llm.run(_sample_df(), TAX, conf, send, cache, ROOT / "prompts")
    assert (res.classified, res.input_tokens, len(sent)) == (3, 3000, 3)
    assert list(out["type"]) == ["A1", "B1", "C1"]
    assert out.iloc[0]["classified_by"] == f"llm:{conf['model']}"
    # 2 回目はキャッシュだけで済む（送信しない）
    out2, res2 = llm.run(_sample_df(), TAX, conf, lambda *a: pytest.fail("送信してはいけない"), cache, ROOT / "prompts")
    assert (res2.cached, list(out2["type"])) == (3, ["A1", "B1", "C1"])


def test_estimate_tokens_and_cost():
    assert llm.estimate_tokens("あいう") == 3
    assert llm.estimate_tokens("abcdefgh") == 2
    est = llm.Estimate(calls=1, items=1, input_tokens=1_000_000, output_tokens=1_000_000)
    assert est.cost({"input": 1.0, "output": 5.0}) == 6.0


def test_api_mode_without_yes_does_not_send(tmp_path: Path, samples: Path, monkeypatch: pytest.MonkeyPatch):
    collect_all(tmp_path, samples)
    monkeypatch.setattr(llm, "http_send", lambda key: pytest.fail("送信してはいけない"))
    rc, out = run(tmp_path, "classify", "--mode", "api")
    assert rc == 0 and "見積もり" in out and "送信していません" in out
    rc, out = run(tmp_path, "classify", "--mode", "api", "--max-cost", "0.000001", "--yes")
    assert rc == 2 and "上限" in out


def test_api_mode_with_yes(tmp_path: Path, samples: Path, monkeypatch: pytest.MonkeyPatch):
    collect_all(tmp_path, samples)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def fake_send(key):
        def send(model_name, text, max_tokens):
            items = json.loads(text[text.index("## 指摘") + len("## 指摘") :])
            return json.dumps([{"id": i["id"], "type": "C1", "topic": ""} for i in items]), 500, 50

        return send

    monkeypatch.setattr(llm, "http_send", fake_send)
    rc, out = run(tmp_path, "classify", "--mode", "api", "--yes", "--limit", "5")
    assert rc == 0 and "分類 5 件" in out
    df = model.load(tmp_path / "data/findings.csv")
    assert (df["type"] == "C1").sum() == 5 and len(df) == 50


def test_export_and_import(tmp_path: Path, samples: Path):
    collect_all(tmp_path, samples)
    rc, out = run(tmp_path, "classify", "--mode", "export", "--out", str(tmp_path / "prompts"))
    files = sorted((tmp_path / "prompts").glob("*.md"))
    assert rc == 0 and len(files) == 4  # CD 6 件・BD 23 件（20+3）・RD 15 件
    df = model.load(tmp_path / "data/findings.csv")
    labels_dir = tmp_path / "labels"
    labels_dir.mkdir()
    labels = [{"id": i, "type": "C1", "topic": ""} for i in df["id"]]
    labels.append({"id": df["id"].iloc[0], "type": "A9"})  # 定義に無い分類は取り込まない
    (labels_dir / "answer.json").write_text(json.dumps(labels, ensure_ascii=False), encoding="utf-8")
    rc, out = run(tmp_path, "classify", "--mode", "import", "--labels", str(labels_dir))
    assert rc == 1 and "問題 1 件" in out
    assert (model.load(tmp_path / "data/findings.csv")["classified_by"] == "copilot").all()
    assert exchange.read_labels(labels_dir)[0]["type"] == "C1"


# --- 分析 --------------------------------------------------------------------------------------


def test_analyze_outputs(tmp_path: Path, samples: Path):
    collect_all(tmp_path, samples)
    run(tmp_path, "classify", "--mode", "rules")
    out_dir = tmp_path / "out"
    rc, out = run(tmp_path, "analyze", "--out", str(out_dir), "--deliverables", str(CONFIG / "deliverables.csv"))
    assert rc == 0
    assert {p.name for p in out_dir.iterdir()} >= {"report.md", "dashboard.html", "summary.xlsx", "charts"}
    assert len(list((out_dir / "charts").glob("*.png"))) == 3
    md = (out_dir / "report.md").read_text(encoding="utf-8")
    assert "## 欠陥 観点×真因" in md and "![" in md
    html = (out_dir / "dashboard.html").read_text(encoding="utf-8")
    assert "data:image/png;base64," in html and "<script>" in html
    sheets = pd.ExcelFile(out_dir / "summary.xlsx").sheet_names
    assert sheets[0] == "所見" and "指摘" in sheets


def test_analyze_tables_and_insights():
    df = model.normalize(
        pd.DataFrame(
            [{"id": str(i), "reviewer": "R1", "deliverable": "D1", "phase": "BD", "severity": "NITS", "type": "B1"} for i in range(4)]
            + [
                {
                    "id": "a",
                    "reviewer": "R2",
                    "deliverable": "D2",
                    "phase": "BD",
                    "severity": "MUST",
                    "type": "A1",
                    "topic": "バッチ",
                    "root_cause": "R2",
                }
            ]
            + [{"id": "b", "reviewer": "R2", "deliverable": "D2", "phase": "BD", "severity": "MUST", "type": ""}]
        )
    )
    a = analyze.run(df, TAX)
    rev = a.tables["レビュアー別"].set_index("レビュアー")
    assert rev.loc["R1", "ノイズ(B)率%"] == 100.0
    assert rev.loc["R2", "欠陥(A)率%"] == 50.0
    assert a.tables["欠陥 観点×真因"].loc["バッチ", "計"] == 1
    text = "\n".join(a.insights)
    assert "未分類の指摘が 1 件" in text and "R1 の指摘はノイズ" in text and "R2 前提・制約の確認漏れ" in text


def test_analyze_without_data_is_usage_error(tmp_path: Path):
    assert run(tmp_path, "analyze")[0] == 2
