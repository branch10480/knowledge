#!/usr/bin/env bash
# 公開前の secret scan（必須ゲート）。
# gitleaks を必須で併用。project-specific regex で API key / Bearer /
# private key / cookie / Signal・Telegram credential を検出する。
# 検出時、または scanner 自体が実行不能な場合は exit 1 で失敗する。
set -Eeuo pipefail

paths=()
staged_mode=0
json_mode=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --staged)
      staged_mode=1
      shift
      ;;
    --paths)
      shift
      while [[ $# -gt 0 && "$1" != --* ]]; do paths+=("$1"); shift; done
      ;;
    --json)
      json_mode=1
      shift
      ;;
    *)
      echo "scan-secrets: unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

repo_root=""
if [[ $staged_mode -eq 1 ]]; then
  repo_root=$(/usr/bin/git rev-parse --show-toplevel 2>/dev/null) || {
    echo "scan-secrets: --staged requires a Git repository" >&2
    exit 2
  }
fi

if [[ $staged_mode -eq 0 && ${#paths[@]} -eq 0 ]]; then
  echo "usage: scan-secrets.sh --staged | --paths <dir>..." >&2
  exit 2
fi

found=0
grep_bin="${GREP_BIN:-$(command -v grep || true)}"
gitleaks_bin="${GITLEAKS_BIN:-$(command -v gitleaks || true)}"
if [ ! -x "$grep_bin" ]; then
  echo "scan-secrets: required grep scanner is unavailable" >&2
  exit 2
fi
scan_root=$(mktemp -d "${TMPDIR:-/tmp}/knowledge-secret-snapshot.XXXXXX")
report=$(mktemp "${TMPDIR:-/tmp}/knowledge-gitleaks.XXXXXX")
trap 'rm -rf "$scan_root"; rm -f "$report"' EXIT

# Open every regular input once with NOFOLLOW, copy those exact bytes to an
# unguessable private snapshot, and record their digests. In --staged mode the
# bytes come from the index blobs, never from the mutable worktree.
if ! /usr/bin/python3 - "$scan_root" "$staged_mode" "$repo_root" "${paths[@]}" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

snapshot = Path(sys.argv[1])
staged = sys.argv[2] == "1"
repo_root = sys.argv[3]
requested = [Path(value) for value in sys.argv[4:]]
files_root = snapshot / "files"
files_root.mkdir(mode=0o700)
manifest = []

def save(label: str, payload: bytes) -> None:
    target = files_root / str(len(manifest) + 1)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    manifest.append({
        "label": label,
        "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
    })

def read_regular(path: Path) -> bytes:
    absolute = Path(os.path.abspath(path))
    before = os.lstat(absolute)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise RuntimeError(f"unsafe file metadata: {absolute}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(absolute, flags)
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise RuntimeError(f"file changed while opening: {absolute}")
        return handle.read()

def walk(root: Path):
    absolute = Path(os.path.abspath(root))
    metadata = os.lstat(absolute)
    if stat.S_ISLNK(metadata.st_mode):
        raise RuntimeError(f"symlinked scan input: {absolute}")
    if stat.S_ISREG(metadata.st_mode):
        yield absolute
        return
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(f"unsupported scan input: {absolute}")
    for current, directories, filenames in os.walk(absolute, followlinks=False):
        current_path = Path(current)
        for name in list(directories):
            child = current_path / name
            if child.is_symlink():
                raise RuntimeError(f"symlinked scan input: {child}")
        for name in sorted(filenames):
            child = current_path / name
            if child.is_symlink():
                raise RuntimeError(f"symlinked scan input: {child}")
            yield child

if staged:
    environment = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LC_ALL": "C",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
    }
    names = subprocess.run(
        ["/usr/bin/git", "-C", repo_root, "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"],
        env=environment, check=True, capture_output=True,
    ).stdout.split(b"\0")
    for raw_name in names:
        if not raw_name:
            continue
        name = raw_name.decode("utf-8")
        entry = subprocess.run(
            ["/usr/bin/git", "-C", repo_root, "ls-files", "-s", "-z", "--", name],
            env=environment, check=True, capture_output=True,
        ).stdout
        if not entry.startswith(b"100") or b"\t" not in entry:
            raise RuntimeError(f"unsupported staged file type: {name}")
        payload = subprocess.run(
            ["/usr/bin/git", "-C", repo_root, "show", f":{name}"],
            env=environment, check=True, capture_output=True,
        ).stdout
        save(name, payload)
else:
    for root in requested:
        for path in walk(root):
            save(str(path), read_regular(path))

(snapshot / "manifest.json").write_text(
    json.dumps({"schema_version": 1, "files": manifest}, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
PY
then
  echo "scan-secrets: could not create an exact input snapshot" >&2
  exit 1
fi

shopt -s nullglob
scan_files=("$scan_root"/files/*)
shopt -u nullglob
if [[ ${#scan_files[@]} -eq 0 ]]; then
  if [[ $json_mode -eq 1 ]]; then
    printf '%s\n' '{"files":[],"ok":true,"schema_version":1}'
  else
    echo "secret scan OK (no files)"
  fi
  exit 0
fi

# ---- project-specific regex ----
signal_key='SIGNAL_CLI_TOKEN'
telegram_key='TELEGRAM_BOT_TOKEN'
telegram_short_key='TG_BOT_TOKEN'
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
  "${signal_key}=[^[:space:]\"']{12,}"
  "${telegram_key}=[^[:space:]\"']{12,}"
  "${telegram_short_key}=[^[:space:]\"']{12,}"
  # basic auth / password in url
  'https://[^ ]+@[^ ]+'
)

for f in "${scan_files[@]}"; do
  for pat in "${patterns[@]}"; do
    if "$grep_bin" -IlE "$pat" "$f" >/dev/null 2>&1; then
      echo "SECRET: $f matched $pat" >&2
      found=1
    fi
  done
done

# ---- gitleaks（exact file set。欠落・異常終了もfail closed）----
if [ ! -x "$gitleaks_bin" ]; then
  echo "scan-secrets: required gitleaks scanner is unavailable" >&2
  found=1
else
  : > "$report"
  set +e
  "$gitleaks_bin" detect --no-git --redact --source "$scan_root/files" \
    --report-format json --report-path "$report" >/dev/null 2>&1
  gitleaks_status=$?
  set -e
  if [[ $gitleaks_status -ne 0 ]]; then
    if [[ -s "$report" ]]; then
      echo "SECRET: gitleaks reported leaks in the exact publication inputs" >&2
    else
      echo "scan-secrets: gitleaks execution failed" >&2
    fi
    found=1
  fi
fi

if [[ $found -ne 0 ]]; then
  echo "secret scan FAILED" >&2
  exit 1
fi
if ! /usr/bin/python3 - "$scan_root" "$json_mode" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
files = manifest.get("files")
if not isinstance(files, list):
    raise SystemExit(1)
for index, item in enumerate(files, 1):
    payload = (root / "files" / str(index)).read_bytes()
    if item.get("sha256") != "sha256:" + hashlib.sha256(payload).hexdigest():
        raise SystemExit(1)
manifest["ok"] = True
if sys.argv[2] == "1":
    print(json.dumps(manifest, sort_keys=True, separators=(",", ":")))
PY
then
  echo "scan-secrets: exact input snapshot changed during scanning" >&2
  exit 1
fi
if [[ $json_mode -eq 0 ]]; then
  echo "secret scan OK"
fi
