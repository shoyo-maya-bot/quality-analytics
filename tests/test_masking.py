"""個人情報の伏せ字のテスト. 外に出るすべての経路（data/findings.csv・LLM に送るプロンプト・Copilot 用の書き出し・
レポート・画面への表示）に、氏名・メールアドレス・電話番号が残らないことを確かめる. 名前やアドレスはすべて架空."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
from pathlib import Path

import pandas as pd
import pytest
from devtools_common.cli import run_main
from fakes import EchoLLM

from quality_analytics import mask, model, taxonomy
from quality_analytics.classify import llm, prompt
from quality_analytics.cli import main

ROOT = Path(__file__).resolve().parent.parent
TAX = taxonomy.load(ROOT / "config/taxonomy.yaml")

SECRETS = [
    "試験太郎",
    "例題花子",
    "架空商事",
    "taro@example.com",
    "ｔａｒｏ＠ｅｘａｍｐｌｅ．ｃｏｍ",
    "hanako@example.org",
    "０９０－１２３４－５６７８",
    "09012345678",
    "090 1234 5678",
    "03(1234)5678",
]


@pytest.mark.parametrize(
    "value",
    [
        "taro@example.com",
        "taro.yamada+qa@mail.example.co.jp",
        "ｔａｒｏ＠ｅｘａｍｐｌｅ．ｃｏｍ",
        "taro＠example.com",
        "090-1234-5678",
        "０９０－１２３４－５６７８",
        "090ー1234ー5678",
        "09012345678",
        "０９０１２３４５６７８",
        "090 1234 5678",
        "03(1234)5678",
        "03-1234-5678",
        "0120-123-456",
        "+81 90-1234-5678",
        "＋８１－９０－１２３４－５６７８",
    ],
)
def test_builtin_patterns_mask(value: str):
    out = mask.Masker().text(f"連絡先は{value}です")
    assert out == f"連絡先は{mask.MASK}です"


@pytest.mark.parametrize(
    "value",
    ["2026-10-05", "応答時間が 3 秒と 5 秒で異なる", "APPE + 4 桁", "注文番号 0001", "氏名の桁が 50 と 40", "#12/c1001", "v1.2.3"],
)
def test_builtin_patterns_keep_ordinary_text(value: str):
    assert mask.Masker().text(value) == value


def _leaky_df() -> pd.DataFrame:
    return model.normalize(
        pd.DataFrame(
            [
                {
                    "id": "table:list:1",
                    "phase": "BD",
                    "reviewer": "試験太郎",
                    "author": "例題花子",
                    "deliverable": "設計書_hanako@example.org",
                    "severity": "MUST",
                    "text": "試験太郎より: 連絡先 ｔａｒｏ＠ｅｘａｍｐｌｅ．ｃｏｍ と ０９０－１２３４－５６７８ が架空商事の資料と違う",
                    "response": "例題花子: 09012345678 と 090 1234 5678 と 03(1234)5678 に確認済み",
                }
            ]
        )
    )


def test_frame_masks_all_free_text_columns_and_names():
    m = mask.Masker(words=["架空商事"])
    out = m.frame(_leaky_df())
    flat = " ".join(out.iloc[0].tolist())
    for s in SECRETS:
        assert s not in flat, s
    r = out.iloc[0]
    assert r["reviewer"].startswith("U-") and r["author"].startswith("U-")
    assert r["reviewer"] in r["text"]  # 本文中の氏名は同じ仮名に置き換わる
    assert (r["id"], r["phase"], r["severity"]) == ("table:list:1", "BD", "MUST")  # 区分の列はそのまま
    assert m.frame(out).equals(out)  # 何度かけても変わらない


def test_frame_masks_unknown_columns_by_default():
    df = pd.DataFrame([{"id": "1", "text": "x", "memo": "taro@example.com"}])
    assert mask.Masker().frame(df).iloc[0]["memo"] == mask.MASK


def test_short_ascii_login_is_replaced_only_as_a_word():
    df = model.normalize(
        pd.DataFrame(
            [{"id": "1", "reviewer": "api", "author": "例題花子", "text": "api さんの指摘: rapid な取込で api_key と apiv2 は別。(api) も対象"}]
        )
    )
    r = mask.Masker().frame(df).iloc[0]
    alias = mask.pseudonym("api")
    assert r["reviewer"] == alias
    assert r["text"] == f"{alias} さんの指摘: rapid な取込で api_key と apiv2 は別。({alias}) も対象"


def test_japanese_and_long_names_are_replaced_inside_text():
    m = mask.Masker()
    names = m.names(["例題花子", "taro.yamada"])
    assert m.text("例題花子さん / taro.yamada2 の件", names) == (
        f"{mask.pseudonym('例題花子')}さん / {mask.pseudonym('taro.yamada')}2 の件"
    )


def test_frame_skips_ai_reviewers_and_can_keep_names():
    df = model.normalize(pd.DataFrame([{"id": "1", "reviewer": "AI:dba", "author": "例題花子", "text": "x"}]))
    assert mask.Masker().frame(df).iloc[0]["reviewer"] == "AI:dba"
    assert mask.Masker(pseudonymize=[]).frame(df).iloc[0]["author"] == "例題花子"


def test_prompt_build_masks_without_config():
    text = prompt.build("BD", _leaky_df(), TAX, prompt_dir=ROOT / "prompts")
    payload = text[text.index("## 指摘") :]
    for s in ["hanako@example.org", "ｔａｒｏ＠ｅｘａｍｐｌｅ．ｃｏｍ", "０９０－１２３４－５６７８"]:
        assert s not in payload, s


# --- 外に出るすべての経路 ---------------------------------------------------------------------


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    d = tmp_path / "config"
    shutil.copytree(ROOT / "config", d)
    text = (d / "mask.yaml").read_text(encoding="utf-8").replace("words: []", "words: [架空商事]")
    (d / "mask.yaml").write_text(text, encoding="utf-8")
    return d


def _run(tmp_path: Path, config_dir: Path, *args: str) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = run_main(
            main,
            ["--data", str(tmp_path / "data/findings.csv"), "--config-dir", str(config_dir), "--prompt-dir", str(ROOT / "prompts"), *args],
        )
    return rc, out.getvalue() + err.getvalue()


def _assert_clean(label: str, text: str) -> None:
    for s in SECRETS:
        assert s not in text, f"{label} に {s} が残っている"


def _write_table(path: Path) -> None:
    rows = [
        "No,成果物,工程,指摘者,作成者,重要度,指摘内容,対応内容,判定,指摘日",
        "1,設計書_hanako@example.org,基本設計,試験太郎,例題花子,必須,"
        "試験太郎より: 連絡先 ｔａｒｏ＠ｅｘａｍｐｌｅ．ｃｏｍ と ０９０－１２３４－５６７８ が架空商事の資料と違う,"
        "09012345678 と 090 1234 5678 と 03(1234)5678 に確認済み,対応する,2026-09-01",
        "2,設計書_hanako@example.org,基本設計,試験太郎,例題花子,軽微,誤字,,対応する,2026-09-01",
        "3,設計書_hanako@example.org,基本設計,試験太郎,例題花子,軽微,表記ゆれ,,対応する,2026-09-01",
    ]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def test_every_outbound_path_is_masked(tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch):
    src = tmp_path / "list.csv"
    _write_table(src)
    rc, out = _run(tmp_path, config_dir, "collect", "table", str(src))
    assert rc == 0
    _assert_clean("data/findings.csv", (tmp_path / "data/findings.csv").read_text(encoding="utf-8-sig"))

    # Copilot 用の書き出し
    assert _run(tmp_path, config_dir, "classify", "--mode", "export", "--out", str(tmp_path / "prompts"))[0] == 0
    for f in (tmp_path / "prompts").glob("*.md"):
        _assert_clean(f.name, f.read_text(encoding="utf-8"))

    # LLM API に送る本文（送信はしない. 送ろうとしたリクエストを記録するだけ）
    clients: list[EchoLLM] = []

    def make_client(conf):
        clients.append(EchoLLM(lambda item: {"type": "B1", "topic": "", "root_cause": "", "reason": ""}, model=conf["model"]))
        return clients[-1]

    monkeypatch.setattr(llm, "make_client", make_client)
    rc, out = _run(tmp_path, config_dir, "classify", "--mode", "api", "--yes")
    sent = [json.dumps(r, ensure_ascii=False) for c in clients for r in c.requests]
    assert rc == 0 and sent
    _assert_clean("LLM のプロンプト", "\n".join(sent))

    # レポートと画面への表示
    rc, out = _run(tmp_path, config_dir, "analyze", "--out", str(tmp_path / "out"), "--no-charts")
    assert rc == 0
    _assert_clean("analyze の表示", out)
    for name in ("report.md", "dashboard.html"):
        _assert_clean(name, (tmp_path / "out" / name).read_text(encoding="utf-8"))
    sheets = pd.read_excel(tmp_path / "out/summary.xlsx", sheet_name=None, dtype=str)
    _assert_clean("summary.xlsx", "\n".join(df.fillna("").to_csv() for df in sheets.values()))


def test_existing_unmasked_data_is_masked_before_output(tmp_path: Path, config_dir: Path):
    """伏せ字の前のバージョンで貯めた data/findings.csv でも、レポートには残らない."""
    df = _leaky_df()
    df["type"] = "B1"
    model.save(df, tmp_path / "data/findings.csv")
    rc, out = _run(tmp_path, config_dir, "analyze", "--out", str(tmp_path / "out"), "--no-charts")
    assert rc == 0
    _assert_clean("analyze の表示", out)
    _assert_clean("dashboard.html", (tmp_path / "out/dashboard.html").read_text(encoding="utf-8"))
    rc, out = _run(tmp_path, config_dir, "classify", "--mode", "export", "--out", str(tmp_path / "prompts"), "--overwrite")
    for f in (tmp_path / "prompts").glob("*.md"):
        _assert_clean(f.name, f.read_text(encoding="utf-8"))
