#!/usr/bin/env bash
# Cron wrapper for the daily memory reflection.
# Sources .env manually if present — cron strips the environment.
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_DIR"
if [ -f "$PROJECT_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$PROJECT_DIR/.env"
  set +a
fi
exec "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/.claude/scripts/memory_reflect.py" "$@"
