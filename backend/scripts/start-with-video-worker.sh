#! /usr/bin/env bash

set -euo pipefail

FASTAPI_WORKERS="${FASTAPI_WORKERS:-4}"
EMBED_VIDEO_WORKER="${EMBED_VIDEO_WORKER:-1}"

pids=()

cleanup() {
  for pid in "${pids[@]:-}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  wait || true
}

trap cleanup INT TERM EXIT

if [ "$EMBED_VIDEO_WORKER" = "1" ]; then
  python -m app.workers.video_worker &
  pids+=("$!")
fi

fastapi run --workers "$FASTAPI_WORKERS" app/main.py &
pids+=("$!")

wait -n "${pids[@]}"
status=$?
cleanup
exit "$status"
