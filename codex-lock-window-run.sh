#!/usr/bin/env bash
set -euo pipefail

mkdir -p "/home/arnab/.local/state"

{
  printf '[%s] Sending auto-lock message: %s\n' "$(date '+%F %T')" Hi
  codex exec --skip-git-repo-check --cd "/home/arnab" --ephemeral Hi
  sleep 10
  printf '[%s] Refreshing next auto-lock timer\n' "$(date '+%F %T')"
  /home/arnab/Projects/scripts/codex-lock-window Hi
} >> /home/arnab/.local/state/codex-lock-window.log 2>&1
