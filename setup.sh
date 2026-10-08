#!/usr/bin/env bash
# セットアップ（macOS / Linux / Git Bash）: .venv を作って qa コマンドを入れる
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip || true
# constraints.txt（GitHub Actions の deps-lock が作る）があれば、依存の版を CI と同じにする
# （macOS の bash 3.2 は set -u で空の配列を展開できないので、配列ではなく文字列にする）
c=""; [ -f constraints.txt ] && c="-c constraints.txt"
# shellcheck disable=SC2086
.venv/bin/python -m pip install -q $c -e ".[dev]"
[ -f .env ] || cp .env.example .env
echo "完了: source .venv/bin/activate のあと qa --help"
