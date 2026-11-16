#!/usr/bin/env bash
set -euo pipefail

environment_file="${OLX_BOT_ENV_FILE:-/etc/olx-business-bot.env}"
project_dir="${OLX_BOT_DIR:-/opt/olx-business-bot}"

set -a
source "$environment_file"
set +a

cd "$project_dir"
exec .venv/bin/python scripts/backup_once.py

