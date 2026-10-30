"""第 8 回: office-review の取り込みの範囲（--project）、--history・--file・llm.yaml の誤りの終了コード、ドキュメントとの一致."""

from __future__ import annotations

import contextlib
import io
import json
import re
from pathlib import Path

import pytest
from devtools_common.cli import run_main

from quality_analytics import cli, model
from quality_analytics.cli import main
from quality_analytics.sources import office_review

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
README = (ROOT / "README.md").read_text(encoding="utf-8")
INTERNALS = (ROOT / "docs" / "INTERNALS.md").read_text(encoding="utf-8")
CONTRACT_REVIEWS = ROOT / "tests/contract/office-review/reviews"


def run(tmp_path: Path, *args: str, config_dir: Path = CONFIG) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = run_main(
            main,
            ["--data", str(tmp_path / "data/findings.csv"), "--config-dir", str(config_dir), "--prompt-dir", str(ROOT / "prompts"), *args],
        )
    return rc, out.getvalue() + err.getvalue()


def _reviews(dst: Path) -> Path:
    dst.mkdir(parents=True)
    for p in CONTRACT_REVIEWS.glob("*.md"):
        (dst / p.name).write_bytes(p.read_bytes())
    return dst


def _ids(tmp_path: Path) -> set[str]:
    return set(model.load(tmp_path / "data/findings.csv")["id"])


# --- office-review: プロジェクトごとの範囲 ---------------------------------------------------------


def test_project_keeps_findings_of_other_projects(tmp_path: Path):
    """同じファイル名のレビュー結果を別のプロジェクトとして取り込んでも、id が重ならず、前のプロジェクトの指摘を消さない."""
    a = _reviews(tmp_path / "a/reviews")
    b = _reviews(tmp_path / "b/reviews")
    rc, out = run(tmp_path, "collect", "office-review", "--reviews", str(a), "--project", "order")
    assert rc == 0, out
    n = len(_ids(tmp_path))
    assert n > 0 and all(i.startswith("office-review:order:") for i in _ids(tmp_path))
    rc, out = run(tmp_path, "collect", "office-review", "--reviews", str(b), "--project", "stock")
    assert rc == 0 and "削除 0 件" in out, out
    ids = _ids(tmp_path)
    assert len(ids) == 2 * n
    df = model.load(tmp_path / "data/findings.csv")
    assert set(df["project"]) == {"order", "stock"}


def test_project_reimport_removes_only_its_own_deleted_results(tmp_path: Path):
    a = _reviews(tmp_path / "a/reviews")
    b = _reviews(tmp_path / "b/reviews")
    assert run(tmp_path, "collect", "office-review", "--reviews", str(a), "--project", "order")[0] == 0
    assert run(tmp_path, "collect", "office-review", "--reviews", str(b))[0] == 0  # --project なし
    (a / "2026-10-03_stock-db_result.md").unlink()
    rc, out = run(tmp_path, "collect", "office-review", "--reviews", str(a), "--project", "order")
    assert rc == 0 and "削除 3 件" in out, out
    ids = _ids(tmp_path)
    assert not any(i.startswith("office-review:order:2026-10-03_stock-db") for i in ids)
    assert any(i == "office-review:2026-10-03_stock-db_result.md#1" for i in ids)  # --project なしの指摘は残る


def test_without_project_does_not_delete_project_findings(tmp_path: Path):
    """--project なしの取り込み直しは、--project なしで取り込んだ指摘だけを範囲にする（以前と同じ id・同じ削除）."""
    a = _reviews(tmp_path / "a/reviews")
    b = _reviews(tmp_path / "b/reviews")
    assert run(tmp_path, "collect", "office-review", "--reviews", str(a), "--project", "order")[0] == 0
    assert run(tmp_path, "collect", "office-review", "--reviews", str(b))[0] == 0
    before = _ids(tmp_path)
    (b / "2026-10-03_stock-db_result.md").unlink()
    rc, out = run(tmp_path, "collect", "office-review", "--reviews", str(b))
    assert rc == 0 and "削除 3 件" in out, out
    after = _ids(tmp_path)
    assert {i for i in before if i.startswith("office-review:order:")} <= after
    assert "office-review:2026-10-02_order-api_result.md#1" in after  # id の形は以前のまま


@pytest.mark.parametrize("name", ["", " ", "a:b", "a#b"])
def test_invalid_project_name_is_usage_error(tmp_path: Path, name: str):
    a = _reviews(tmp_path / "a/reviews")
    rc, out = run(tmp_path, "collect", "office-review", "--reviews", str(a), "--project", name)
    assert rc == 2, out


def test_scope_helper():
    assert office_review.scope() == "office-review:"
    assert office_review.scope("order") == "office-review:order:"


# --- --history のパス ---------------------------------------------------------------------------


def test_missing_history_is_usage_error(tmp_path: Path):
    a = _reviews(tmp_path / "a/reviews")
    rc, out = run(tmp_path, "collect", "office-review", "--reviews", str(a), "--history", str(tmp_path / "no-such.jsonl"))
    assert rc == 2 and "--history" in out, out
    assert not (tmp_path / "data/findings.csv").exists()  # 何も取り込まない


# --- --file の形式 ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        "[1, 2, 3]",
        '{"repo": "x"}',
        '{"pulls": {"number": 1}}',
        '{"pulls": [{"title": "number が無い"}]}',
        '{"pulls": [1]}',
        "{壊れた JSON",
    ],
)
def test_github_file_not_in_saved_format_is_usage_error(tmp_path: Path, content: str):
    f = tmp_path / "raw.json"
    f.write_text(content, encoding="utf-8")
    rc, out = run(tmp_path, "collect", "github", "--file", str(f))
    assert rc == 2, out


def test_github_file_missing_is_usage_error(tmp_path: Path):
    rc, out = run(tmp_path, "collect", "github", "--file", str(tmp_path / "none.json"))
    assert rc == 2, out


def test_github_saved_file_still_works(tmp_path: Path):
    f = tmp_path / "raw.json"
    f.write_text(json.dumps({"repo": "example-org/svc", "pulls": []}), encoding="utf-8")
    assert run(tmp_path, "collect", "github", "--file", str(f))[0] == 0


# --- llm.yaml の model ---------------------------------------------------------------------------


def _config_with_llm(tmp_path: Path, llm_yaml: str) -> Path:
    conf = tmp_path / "config"
    for p in CONFIG.rglob("*"):
        if p.is_file():
            dst = conf / p.relative_to(CONFIG)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(p.read_bytes())
    (conf / "llm.yaml").write_text(llm_yaml, encoding="utf-8")
    return conf


@pytest.mark.parametrize("llm_yaml", ["max_cost_usd: 5.0\n", "model: ''\n", "model: [a]\n"])
def test_llm_yaml_without_model_is_usage_error(tmp_path: Path, llm_yaml: str):
    conf = _config_with_llm(tmp_path, llm_yaml)
    a = _reviews(tmp_path / "a/reviews")
    assert run(tmp_path, "collect", "office-review", "--reviews", str(a), config_dir=conf)[0] == 0
    rc, out = run(tmp_path, "classify", "--mode", "api", config_dir=conf)
    assert rc == 2 and "model" in out, out


def test_llm_yaml_not_mapping_is_usage_error(tmp_path: Path):
    conf = _config_with_llm(tmp_path, "- model\n")
    a = _reviews(tmp_path / "a/reviews")
    rc, out = run(tmp_path, "collect", "office-review", "--reviews", str(a), config_dir=conf)  # 伏せ字の設定（redact）も読む
    assert rc == 2 and "mapping" in out, out


def test_export_does_not_need_model(tmp_path: Path):
    """export（Copilot 用の書き出し）は model を使わないので、無くても動く（以前と同じ）."""
    conf = _config_with_llm(tmp_path, "batch_size: 20\n")
    a = _reviews(tmp_path / "a/reviews")
    assert run(tmp_path, "collect", "office-review", "--reviews", str(a), config_dir=conf)[0] == 0
    rc, out = run(tmp_path, "classify", "--mode", "export", "--out", str(tmp_path / "prompts"), config_dir=conf)
    assert rc == 0, out


# --- ドキュメントとの一致 ----------------------------------------------------------------------


def test_docstrings_do_not_point_to_moved_readme_sections():
    """README から docs/INTERNALS.md に移した節を、コードの説明が README の節として参照していない."""
    moved = re.findall(r"^## (.+?)(?:（.*）)?$", INTERNALS, re.M)
    for py in (ROOT / "src").rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for title in moved:
            assert f"README の「{title}」" not in text, f"{py.name}: README の「{title}」は docs/INTERNALS.md にあります"


def test_readme_documents_office_review_options():
    help_text = (
        cli.build_parser()
        ._subparsers._group_actions[0]
        .choices["collect"]
        ._subparsers._group_actions[0]
        .choices["office-review"]
        .format_help()
    )
    for opt in ("--reviews", "--history", "--project"):
        assert opt in help_text
        assert opt in README
