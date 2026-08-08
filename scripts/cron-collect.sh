#!/usr/bin/env bash
# Knowledge cron entrypoint with bounded retry for temporary inference contention.

set -Eeuo pipefail
umask 077

script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
repo="$(CDPATH='' cd -- "$script_dir/.." && pwd)"
expected_origin="${KNOWLEDGE_CRON_EXPECTED_ORIGIN:-https://github.com/branch10480/knowledge.git}"
max_attempts="${KNOWLEDGE_CRON_MAX_ATTEMPTS:-4}"
retry_delay_seconds="${KNOWLEDGE_CRON_RETRY_DELAY_SECONDS:-600}"

case "$max_attempts" in
  ''|*[!0-9]*)
    printf '%s\n' 'Knowledge v2 aborted: retry attempt count must be an integer' >&2
    exit 2
    ;;
esac
case "$retry_delay_seconds" in
  ''|*[!0-9]*)
    printf '%s\n' 'Knowledge v2 aborted: retry delay must be an integer' >&2
    exit 2
    ;;
esac
if [ "$max_attempts" -lt 1 ] || [ "$max_attempts" -gt 12 ]; then
  printf '%s\n' 'Knowledge v2 aborted: retry attempt count must be between 1 and 12' >&2
  exit 2
fi
if [ "$retry_delay_seconds" -gt 3600 ]; then
  printf '%s\n' 'Knowledge v2 aborted: retry delay must be at most 3600 seconds' >&2
  exit 2
fi

cd "$repo"

if [ "$(git symbolic-ref --short HEAD 2>/dev/null || true)" != main ]; then
  printf '%s\n' 'Knowledge v2 aborted: branch is not main' >&2
  exit 1
fi
if ! git diff --quiet --; then
  printf '%s\n' 'Knowledge v2 aborted: tracked unstaged changes exist' >&2
  exit 1
fi
if ! git diff --cached --quiet --; then
  printf '%s\n' 'Knowledge v2 aborted: tracked staged changes exist' >&2
  exit 1
fi
if [ "$(git remote get-url origin 2>/dev/null || true)" != "$expected_origin" ]; then
  printf '%s\n' 'Knowledge v2 aborted: unexpected origin' >&2
  exit 1
fi

git fetch --prune origin
if ! git merge-base --is-ancestor HEAD origin/main; then
  printf '%s\n' 'Knowledge v2 aborted: local main is ahead of or diverged from origin/main' >&2
  exit 1
fi
git merge --ff-only origin/main

attempt=1
while [ "$attempt" -le "$max_attempts" ]; do
  set +e
  /bin/bash "$repo/scripts/collect.sh"
  status=$?
  set -e

  if [ "$status" -eq 0 ]; then
    exit 0
  fi
  if [ "$status" -ne 75 ]; then
    exit "$status"
  fi
  if [ "$attempt" -ge "$max_attempts" ]; then
    printf '{"ok":false,"deferred":true,"reason":"inference_busy","attempt":%d,"max_attempts":%d,"retry_scheduled":false}\n' \
      "$attempt" "$max_attempts"
    exit 75
  fi

  printf '{"ok":false,"deferred":true,"reason":"inference_busy","attempt":%d,"max_attempts":%d,"retry_scheduled":true,"retry_after_seconds":%d}\n' \
    "$attempt" "$max_attempts" "$retry_delay_seconds"
  sleep "$retry_delay_seconds"
  attempt=$((attempt + 1))
done

exit 75
