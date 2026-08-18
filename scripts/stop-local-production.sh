#!/usr/bin/env bash
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
state_root=${XDG_STATE_HOME:-"$repo/.state"}
state_dir=${READING_COMPANION_STATE_DIR:-"$state_root/reading-companion"}
pid_file="$state_dir/service.pid"

[[ -r "$pid_file" ]] || exit 0
pid=$(<"$pid_file")
[[ "$pid" =~ ^[0-9]+$ ]] || exit 1

if [[ -r "/proc/$pid/cmdline" ]] && tr '\0' ' ' <"/proc/$pid/cmdline" | grep -Fq 'run-local-production.sh'; then
  kill "$pid"
  for _ in {1..50}; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.1
  done
fi
