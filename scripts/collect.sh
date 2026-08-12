#!/usr/bin/env bash
# Legacy entrypoint. Publication now requires an opaque Hermes-core capability.
set -Eeuo pipefail

printf '%s\n' \
  'Knowledge direct collection is disabled. Use the Hermes knowledge_start tool.' >&2
exit 64
