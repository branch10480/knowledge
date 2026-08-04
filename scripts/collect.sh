#!/usr/bin/env bash
# Knowledge v2 収集パイプライン（Hermes cron 用の実行本体）。
# set -Eeuo pipefail、固定 PATH、umask 077、mkdir ベースの process lock（stale 検知付き）、
# run ごとの mktemp -d、clean branch 確認、trap を使用。
# macOS には flock(1) が無いため、排他は mkdir で実現する。
set -Eeuo pipefail
umask 077
cd "$(dirname "$0")/.."
REPO=$(pwd)

# ---- 固定 PATH（外部ツールを信用しない）----
export PATH="/etc/profiles/per-user/${USER:-unknown}/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

PY="$REPO/.venv/bin/python"
export PYTHONPATH="$REPO/src"
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(date -u +%s)}"

# ---- clean branch 確認 ----
branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)
if [ "$branch" != "main" ]; then
  echo "branch is not main: ${branch:-unknown}" >&2
  exit 1
fi
if ! git diff --quiet -- data/entries.json data/checkpoint.json; then
  echo "tracked data has uncommitted changes" >&2
  exit 1
fi
if ! git diff --cached --quiet -- data/entries.json data/checkpoint.json; then
  echo "tracked data has staged uncommitted changes" >&2
  exit 1
fi

# ---- process lock（mkdir ベース、stale 検知）----
mkdir -p .work
LOCK=.work/lock
if [ -d "$LOCK" ]; then
  pid=$(cat "$LOCK/pid" 2>/dev/null || true)
  if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
    rm -rf "$LOCK"
  fi
fi
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "another run in progress" >&2
  exit 1
fi
echo $$ > "$LOCK/pid"

# run ごとの作業領域（mktemp -d）。終了時に削除。
WORK_DIR=$(mktemp -d "$REPO/.work.run.XXXXXX")
trap 'rm -rf "$WORK_DIR" "$LOCK" 2>/dev/null || true' EXIT

# T0 を UTC で一度だけ採取
T0=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

echo "== [1/6] collect =="
"$PY" -m knowledge.cli collect --run-started-at "$T0" --output "$WORK_DIR/candidates.json"

echo "== [2/6] summarize =="
"$PY" -m knowledge.cli summarize --candidates "$WORK_DIR/candidates.json" --output "$WORK_DIR/summaries.json"

# merge を dry-run で実行し、merged を一時ファイルへ。commit は build・QA・secret scan の後に行う
echo "== [3/6] merge (dry-run) =="
"$PY" -m knowledge.cli merge --candidates "$WORK_DIR/candidates.json" --summaries "$WORK_DIR/summaries.json" --output-merged "$WORK_DIR/merged.json"

echo "== [4/6] build (temp) =="
"$PY" -m knowledge.cli build --entries "$WORK_DIR/merged.json" --output "$WORK_DIR/dist"

echo "== [5/6] QA =="
"$PY" -m knowledge.cli validate-atom "$WORK_DIR/dist/feed.xml"
"$PY" -m knowledge.cli check-build --entries "$WORK_DIR/merged.json" --dist "$WORK_DIR/dist"
"$PY" -m pytest
git diff --check

echo "== [6/6] secret scan =="
./scripts/scan-secrets.sh --paths data "$WORK_DIR/dist"

# 全 stage 成功後、entries と checkpoint を同一 commit で commit/push
echo "== commit + push =="
"$PY" -m knowledge.cli merge --candidates "$WORK_DIR/candidates.json" --summaries "$WORK_DIR/summaries.json" --commit

echo "RUN_OK commit=$(git rev-parse HEAD)"
