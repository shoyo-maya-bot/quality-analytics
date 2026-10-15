"""利用者向けの README と、変更履歴（CHANGELOG.md）・内部の仕様（docs/）の分担を確かめる."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")


def test_readme_has_no_change_history_or_internal_contract():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "## 変更点" not in readme
    assert "形式の約束（テストで確かめています）" not in readme
    assert "docs/INTERNALS.md" in readme and "CHANGELOG.md" in readme


def test_internal_spec_and_history_live_in_their_own_files():
    internals = (ROOT / "docs/INTERNALS.md").read_text(encoding="utf-8")
    assert "## office-review との形式の約束" in internals and "## LLM API の通信" in internals
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "利用者の動きが変わるもの" in changelog


def test_relative_links_in_docs_resolve():
    for md in [ROOT / "README.md", ROOT / "CHANGELOG.md", *sorted((ROOT / "docs").glob("*.md"))]:
        for target in LINK.findall(md.read_text(encoding="utf-8")):
            if "://" in target:
                continue
            assert (md.parent / target).exists(), f"{md.name}: {target}"
