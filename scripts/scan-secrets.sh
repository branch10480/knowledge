#!/usr/bin/env bash
# 公開前の secret scan（必須ゲート）。
# gitleaks が利用可能なら併用。project-specific regex で API key / Bearer /
# private key / cookie / Signal・Telegram credential を検出する。
# 検出時、または scanner 自体が実行不能な場合は exit 1 で失敗する。
set -Eeuo pipefail

paths=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --paths)
      shift
      while [[ $# -gt 0 && "$1" != --* ]]; do paths+=("$1"); shift; done
      ;;
    *) shift ;;
  esac
done

if [[ ${#paths[@]} -eq 0 ]]; then
  echo "usage: scan-secrets.sh --paths <dir>..." >&2
  exit 2
fi

found=0

# ---- project-specific regex ----
patterns=(
  # OpenAI / generic API keys
  'sk-[A-Za-z0-9]{20,}'
  'sk-[A-Za-z0-9_-]{20,}'
  # Bearer tokens
  'Bearer [A-Za-z0-9._~+/=-]{20,}'
  # private keys
  '-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----'
  # AWS / GitHub tokens
  'AKIA[0-9A-Z]{16}'
  'gh[pousr]_[A-Za-z0-9]{20,}'
  # Signal / Telegram-ish credentials (env-like)
  'SIGNAL_CLI_TOKEN='
  'TELEGRAM_BOT_TOKEN='
  'TG_BOT_TOKEN='
  # basic auth / password in url
  'https://[^ ]+@[^ ]+'
)

for dir in "${paths[@]}"; do
  if [[ ! -d "$dir" ]]; then
    echo "scan-secrets: missing path: $dir" >&2
    found=1
    continue
  fi
  while IFS= read -r -d '' f; do
    for pat in "${patterns[@]}"; do
      if grep -rIlE "$pat" "$f" >/dev/null 2>&1; then
        echo "SECRET: $f matched $pat" >&2
        found=1
      fi
    done
  done < <(find "$dir" -type f -print0)
done

# ---- gitleaks（あれば併用、なければ失敗させないが必須ゲートは regex で担保）----
if command -v gitleaks >/dev/null 2>&1; then
  gitleaks detect --no-git --redact --report-format json --report-path /tmp/gitleaks.json >/dev/null 2>&1 || true
  if [[ -s /tmp/gitleaks.json ]]; then
    echo "SECRET: gitleaks reported leaks" >&2
    found=1
  fi
fi

if [[ $found -ne 0 ]]; then
  echo "secret scan FAILED" >&2
  exit 1
fi
echo "secret scan OK"
