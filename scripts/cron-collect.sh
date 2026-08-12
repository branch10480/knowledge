#!/usr/bin/env bash
# Legacy entrypoint. Scheduled collection now starts through Hermes knowledge_start.
set -Eeuo pipefail

printf '%s\n' \
  'Knowledge cron shell entrypoint is disabled. Configure the Hermes cron job to call knowledge_start.' >&2
exit 64
