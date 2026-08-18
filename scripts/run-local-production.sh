#!/usr/bin/env bash
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
credentials=${READING_COMPANION_ENV_FILE:-"$repo/.env"}
data_dir=${READING_COMPANION_DATA_DIR:-"$repo/data"}
state_root=${XDG_STATE_HOME:-"$repo/.state"}
state_dir=${READING_COMPANION_STATE_DIR:-"$state_root/reading-companion"}
backup_dir="$state_dir/backups"
log_file="$state_dir/service.log"
pid_file="$state_dir/service.pid"
lock_file="$state_dir/service.lock"

mkdir -p "$state_dir" "$backup_dir"
exec 9>"$lock_file"
flock -n 9 || exit 0

if curl -fsS --max-time 1 http://127.0.0.1:5173/api/health/live >/dev/null 2>&1; then
  exit 0
fi

test -r "$credentials"
test -d "$data_dir"
test -f "$repo/frontend/dist/index.html"

if [[ -f "$log_file" ]] && (( $(wc -c <"$log_file") > 5242880 )); then
  mv -f "$log_file" "$log_file.1"
fi
exec >>"$log_file" 2>&1

set -a
source "$credentials"
set +a
export DATA_DIR="$data_dir"
export FRONTEND_DIST_DIR="$repo/frontend/dist"
export EXPOSE_API_DOCS=false
export PYTHONUNBUFFERED=1
export PYTHONPATH="$repo/backend${PYTHONPATH:+:$PYTHONPATH}"

cd "$repo"
run_backup() {
  if ! backend/.venv/bin/python -m app.lifecycle backup-all --data-dir "$data_dir" \
      --output-dir "$backup_dir" --keep 7 --min-age-hours 24; then
    echo "WARNING: automatic backup failed; continuing with the existing data unchanged" >&2
  fi
}
run_backup

backup_worker=""
backup_loop() {
  while true; do
    sleep 86400
    run_backup
  done
}
backup_loop &
backup_worker=$!

child=""
stopping=0
cleanup() {
  stopping=1
  if [[ -n "$child" ]] && kill -0 "$child" 2>/dev/null; then
    kill "$child"
    wait "$child" 2>/dev/null || true
  fi
  if [[ -n "$backup_worker" ]] && kill -0 "$backup_worker" 2>/dev/null; then
    kill "$backup_worker"
    wait "$backup_worker" 2>/dev/null || true
  fi
  rm -f "$pid_file"
  exit 0
}
trap cleanup INT TERM
printf '%s\n' "$$" >"$pid_file"

while (( stopping == 0 )); do
  backend/.venv/bin/python -m uvicorn app.main:create_app --factory --app-dir backend \
    --host 127.0.0.1 --port 5173 --no-proxy-headers --no-server-header --no-access-log &
  child=$!
  status=0
  wait "$child" || status=$?
  child=""
  if (( stopping != 0 )); then
    break
  fi
  echo "Service exited with status $status; restarting in 2 seconds" >&2
  sleep 2
done

rm -f "$pid_file"
