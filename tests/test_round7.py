"""第 7 回: --anonymize の本文のログイン名、分類 0 件の率、README と実際の動き（終了コード・版・既定値・料金）の一致."""

from __future__ import annotations

import inspect
import json
import re
import tomllib
from pathlib import Path

import pandas as pd
import yaml
from devtools_common import cli as common_cli
from devtools_common import llm as common_llm

from quality_analytics import analyze, cli, model, report, taxonomy
from quality_analytics.mask import pseudonym
from quality_analytics.sources import github

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
TAX = taxonomy.load(ROOT / "config" / "taxonomy.yaml")


# --- --anonymize: 本文・対応内容のログイン名 ---


def _pulls() -> dict:
    return {
        "repo": "example-org/svc",
        "pulls": [
            {
                "number": 7,
                "user": {"login": "dev-taro", "type": "User"},
                "comments": [
                    {
                        "id": 1,
                        "user": {"login": "rev-hanako"},
                        "body": "[MUST] @dev-taro さん、Dev-Taro の前回の修正と矛盾します",
                        "path": "a.py",
                    },
                    {
                        "id": 2,
                        "user": {"login": "dev-taro"},
                        "in_reply_to_id": 1,
                        "body": "rev-hanako さん、@outsider-jiro に確認して直しました",
                    },
                    {"id": 3, "user": {"login": "rev-hanako"}, "body": "[NITS] 連絡は dev@example.com へ。dev-taro2 は別の人"},
                ],
                "reviews": [{"id": 9, "user": {"login": "rev-hanako"}, "body": "[Q] dev-taro の方針は？"}],
            }
        ],
    }


def test_anonymize_replaces_logins_in_text_and_response():
    df = github.to_findings(_pulls(), anonymize=True)
    blob = "\n".join(df[c].astype(str).str.cat(sep="\n") for c in df.columns)
    for login in ("dev-taro", "Dev-Taro", "rev-hanako", "outsider-jiro"):
        assert not re.search(rf"(?<![0-9A-Za-z_-]){re.escape(login)}(?![0-9A-Za-z_-])", blob), login
    first = df.set_index("id").loc["github:example-org/svc#7/c1"]
    taro, hanako = pseudonym("dev-taro"), pseudonym("rev-hanako")
    assert first["author"] == taro and first["reviewer"] == hanako
    assert f"@{taro} さん、{taro} の前回" in first["text"]  # 大文字・小文字の違いも同じ人
    assert first["response"].startswith(f"{hanako} さん、@{pseudonym('outsider-jiro')} に確認")  # 参加していない人の @メンションも
    third = df.set_index("id").loc["github:example-org/svc#7/c3"]
    assert "dev@example.com" in third["text"]  # メールアドレスは @メンションではない（伏せるのは取り込み時の mask）
    assert "dev-taro2" in third["text"]  # 別のログイン名の一部は置き換えない


def test_without_anonymize_text_is_unchanged():
    df = github.to_findings(_pulls())
    assert "@dev-taro さん" in df.set_index("id").loc["github:example-org/svc#7/c1", "text"]


def test_anonymize_is_idempotent_on_pseudonyms():
    once = github.anonymize_text("@dev-taro と dev-taro", {"dev-taro": "dev-taro"})
    assert github.anonymize_text(once, {"dev-taro": "dev-taro"}) == once


def test_collect_github_anonymize_with_pseudonymize_off_leaves_no_login(tmp_path: Path):
    (tmp_path / "config").mkdir()
    for f in (ROOT / "config").glob("*.yaml"):
        (tmp_path / "config" / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "config" / "mask.yaml").write_text("pseudonymize: []\n", encoding="utf-8")
    raw = tmp_path / "pulls.json"
    raw.write_text(json.dumps(_pulls(), ensure_ascii=False), encoding="utf-8")
    data = tmp_path / "findings.csv"
    rc = cli.main(["--data", str(data), "--config-dir", str(tmp_path / "config"), "collect", "github", "--file", str(raw), "--anonymize"])
    assert rc == 0
    text = data.read_text(encoding="utf-8-sig")
    for login in ("dev-taro,", "dev-taro ", "rev-hanako", "outsider-jiro", "Dev-Taro"):
        assert login not in text, login


# --- 分類 0 件の率 ---


def test_rates_are_blank_not_zero_when_nothing_classified():
    rows = [{"id": f"u{i}", "reviewer": "R1", "phase": "BD", "severity": "NITS", "type": ""} for i in range(4)]
    rows += [{"id": f"b{i}", "reviewer": "R2", "phase": "BD", "severity": "NITS", "type": "B1"} for i in range(3)]
    a = analyze.run(model.normalize(pd.DataFrame(rows)), TAX)
    rev = a.tables["レビュアー別"].set_index("レビュアー")
    assert pd.isna(rev.loc["R1", "欠陥(A)率%"]) and pd.isna(rev.loc["R1", "ノイズ(B)率%"])
    assert rev.loc["R2", "ノイズ(B)率%"] == 100.0 and rev.loc["R2", "欠陥(A)率%"] == 0.0  # 分類済みがあれば 0% は 0%
    assert pd.isna(rev.loc["R1", "採用率%"])  # 判定が無いときも空欄
    assert any("R2 の指摘はノイズ" in s for s in a.insights) and not any("R1 の指摘" in s for s in a.insights)
    md = report.markdown(a, "t")
    r1 = next(line for line in md.splitlines() if line.startswith("| R1 |"))
    assert "0.0" not in r1 and "None" not in r1 and "nan" not in r1


def test_rates_all_blank_does_not_break_insights():
    rows = [{"id": f"u{i}", "reviewer": "R1", "phase": "BD", "severity": "NITS", "type": ""} for i in range(3)]
    a = analyze.run(model.normalize(pd.DataFrame(rows)), TAX)
    assert pd.isna(a.tables["レビュアー別"].iloc[0]["ノイズ(B)率%"])
    assert a.insights == ["未分類の指摘が 3 件あります（classify を実行してください）。"]


# --- README と実際の動きのずれを機械で見つける ---


def _section(title: str) -> str:
    m = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## )", README, re.S | re.M)
    assert m, title
    return m.group(1)


def test_readme_exit_codes_match_constants_and_help():
    table = dict(re.findall(r"^\| (\d) \| (.+?) \|$", _section("終了コード"), re.M))
    assert {int(k) for k in table} == {int(c) for c in common_cli.ExitCode}
    help_text = cli.build_parser().format_help()
    for code in table:
        assert re.search(rf"\b{code} ", help_text.split("終了コード:")[1]), code
    # 使っているのは表にある値だけ
    used = set(re.findall(r"ExitCode\.(\w+)", inspect.getsource(cli)))
    assert {int(common_cli.ExitCode[u]) for u in used} <= {int(k) for k in table}
    assert "見積もり" in table["2"] and "途中" in table["3"]


def test_readme_python_version_matches_pyproject_and_ci():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    need = re.fullmatch(r">=(\d+\.\d+)", meta["project"]["requires-python"]).group(1)
    assert f"Python {need} 以上" in README
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert set(re.findall(r'python-version: "([\d.]+)"', ci)) == {need}
    assert f"CI は {need} で" in README
    assert meta["tool"]["ruff"]["target-version"] == "py" + need.replace(".", "")


def test_readme_defaults_match_code_and_config():
    conf = yaml.safe_load((ROOT / "config/llm.yaml").read_text(encoding="utf-8"))
    assert f"`max_cost_usd`（既定 ${conf['max_cost_usd']:g}）" in README
    assert f"`retry_margin`。既定 {conf['retry_margin']:.0%}" in README
    assert f"{conf['batch_size']} 件ずつ" in README
    noise = next(a for a in cli.build_parser()._subparsers._group_actions[0].choices["analyze"]._actions if a.dest == "noise_threshold")
    assert f"`--noise-threshold`（既定 {noise.default:g}%）" in README
    internals = (ROOT / "docs/INTERNALS.md").read_text(encoding="utf-8")
    max_pulls = inspect.signature(github.download).parameters["max_pulls"].default
    assert f"PR {max_pulls} 件まで" in README and f"PR {max_pulls} 件まで" in internals
    assert f"{github.MAX_PAGES} ページ（{github.MAX_PAGES * github.PER_PAGE} 件）" in internals
    assert f"待ち時間が {github.RATE_LIMIT_MAX_WAIT:g} 秒以内" in README
    assert f"`max_retries`。既定 {conf['max_retries']} 回" in internals
    assert "haiku" in conf["model"] and "Haiku 4.5（既定）" in README


def test_readme_price_table_matches_common_prices():
    rows = re.findall(r"^\| Claude (\w+) ([\d.]+)(?:（既定）)? \| \$([\d.]+) / \$([\d.]+) \| 約 \$([\d.]+) \|$", README, re.M)
    assert len(rows) == 3
    m = re.search(r"入力 約 ([\d.]+) 万・出力 約 ([\d.]+) 万トークン", README)
    tokens_in, tokens_out = float(m.group(1)) * 1e4, float(m.group(2)) * 1e4
    for family, ver, pin, pout, cost in rows:
        price = common_llm.PRICES[f"claude-{family.lower()}-{ver.replace('.', '-')}"]
        assert (price.input, price.output) == (float(pin), float(pout)), family
        assert round((tokens_in * price.input + tokens_out * price.output) / 1e6, 2) == float(cost), family


def test_env_example_lists_every_env_var_in_readme():
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in sorted(set(re.findall(r"`((?:ANTHROPIC|GH|QA)_[A-Z_]+)", README))):
        assert name in env, name
