#!/usr/bin/env python3
"""
新しい知識エントリを entries.json に追記し、build.py を実行して
index.html を再生成するスクリプト。

使い方:
  cat entries.json | python3 add_entries.py
  または
  echo '[{"date":"...","title":"...",...}]' | python3 add_entries.py

JSON 形式（標準入力）:
  [
    {
      "date": "2026-08-02",
      "title": "エントリのタイトル",
      "tags": ["iOS", "Apple"],
      "content": "マークダウン形式の本文",
      "source": "https://..."
    },
    ...
  ]

既存の entries.json とマージし、日付降順でソートした上で
build.py を呼び出して index.html を再生成する。
"""
import json
import sys
import os
import re

ALLOWED_URL_SCHEMES = ("http://", "https://")


def validate_entry(entry: dict) -> None:
    d = entry.get("date", "")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        raise ValueError(f"Invalid date format: {d!r}")
    if not entry.get("title", "").strip():
        raise ValueError("title is required")
    tags = entry.get("tags", [])
    if not isinstance(tags, list):
        raise ValueError("tags must be a list")
    for t in tags:
        if not isinstance(t, str):
            raise ValueError(f"tag is not a string: {t!r}")
    if not entry.get("content", "").strip():
        raise ValueError("content is required")
    src = entry.get("source", "")
    if src and not src.startswith(ALLOWED_URL_SCHEMES):
        raise ValueError(f"Invalid source URL scheme: {src!r}")


def main():
    data = json.load(sys.stdin)
    if not data:
        print("No entries to add", file=sys.stderr)
        return

    for entry in data:
        validate_entry(entry)

    entries_path = "entries.json"
    repo_dir = os.path.dirname(os.path.abspath(__file__))

    # 既存の entries.json を読み込む
    existing = []
    entries_file = os.path.join(repo_dir, entries_path)
    if os.path.exists(entries_file):
        with open(entries_file, "r") as f:
            existing = json.load(f)

    # マージ
    all_entries = existing + data

    # 日付降順でソート（同じ日付は既存が先）
    def sort_key(e: dict) -> str:
        return e.get("date", "0000-00-00")
    all_entries.sort(key=sort_key, reverse=True)

    # 書き戻し
    with open(entries_file, "w") as f:
        json.dump(all_entries, f, ensure_ascii=False, indent=2)

    added = len(data)
    total = len(all_entries)
    print(f"Updated {entries_path}: {added} new entries added, {total} total")

    # build.py を呼び出す
    build_py = os.path.join(repo_dir, "build.py")
    if os.path.exists(build_py):
        import subprocess
        result = subprocess.run(
            ["python3", build_py],
            cwd=repo_dir,
            capture_output=True,
            text=True
        )
        print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
    else:
        print(f"Warning: {build_py} not found, index.html not regenerated",
              file=sys.stderr)


if __name__ == "__main__":
    main()
