#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
[[ -f ~/.openclaw/.env.local ]] && source ~/.openclaw/.env.local
exec python3 "$SCRIPT_DIR/process_os_inbox_prepare.py"
