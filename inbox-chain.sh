#!/usr/bin/env bash
# inbox-chain.sh — 顺序执行 flomo sync → inbox prepare → inbox AI → send report
# 替代原来 4 条独立 cron 条目，确保上游成功后才运行下游
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
[[ -f ~/.openclaw/.env.local ]] && source ~/.openclaw/.env.local

echo "[inbox-chain] start: $(date '+%Y-%m-%d %H:%M:%S')"

# ---- Step 1: flomo sync (失败不阻断，只记录) ----
echo "[inbox-chain] step 1: flomo sync"
if "$SCRIPT_DIR/flomo_sync_to_inbox.sh"; then
  echo "[inbox-chain] step 1: ok"
else
  echo "[inbox-chain] step 1: flomo sync failed (exit $?), continuing without new flomo items"
fi

# ---- Step 1.5: flomo KB incremental sync (失败不阻断) ----
echo "[inbox-chain] step 1.5: flomo KB sync"
if python3 "$SCRIPT_DIR/flomo_kb_incremental_sync.py" --hours 48; then
  echo "[inbox-chain] step 1.5: ok"
else
  echo "[inbox-chain] step 1.5: flomo KB sync failed (exit $?), continuing"
fi

# ---- Step 2: inbox prepare ----
echo "[inbox-chain] step 2: inbox prepare"
"$SCRIPT_DIR/process_os_inbox_prepare.sh"
PREPARE_RC=$?

if [[ $PREPARE_RC -eq 2 ]]; then
  echo "[inbox-chain] step 2: no items today (exit 2)"
elif [[ $PREPARE_RC -ne 0 ]]; then
  echo "[inbox-chain] step 2: prepare failed (exit $PREPARE_RC), aborting chain"
  exit "$PREPARE_RC"
else
  echo "[inbox-chain] step 2: ok"
fi

# ---- Step 3: inbox AI (失败后自动重试，最多 3 次，间隔 5 分钟) ----
echo "[inbox-chain] step 3: inbox AI"
AI_OK=false
for AI_ATTEMPT in 1 2 3; do
  echo "[inbox-chain] step 3: attempt $AI_ATTEMPT/3"
  if "$SCRIPT_DIR/process_os_inbox_ai.sh"; then
    echo "[inbox-chain] step 3: ok (attempt $AI_ATTEMPT)"
    AI_OK=true
    break
  else
    echo "[inbox-chain] step 3: attempt $AI_ATTEMPT failed (exit $?)"
    if [[ $AI_ATTEMPT -lt 3 ]]; then
      echo "[inbox-chain] step 3: waiting 5 minutes before retry..."
      sleep 300
    fi
  fi
done

if [[ "$AI_OK" != "true" ]]; then
  echo "[inbox-chain] step 3: all 3 attempts failed, skipping send"
  exit 1
fi

# ---- Step 4: send report ----
echo "[inbox-chain] step 4: send report"
if "$SCRIPT_DIR/send-inbox-write-report.sh"; then
  echo "[inbox-chain] step 4: ok"
else
  echo "[inbox-chain] step 4: send failed (exit $?)"
  exit 1
fi

# ---- Step 5: daily summary ----
echo "[inbox-chain] step 5: daily summary"
if python3 "$SCRIPT_DIR/generate_inbox_daily_summary.py"; then
  echo "[inbox-chain] step 5: ok"
else
  echo "[inbox-chain] step 5: daily summary failed (exit $?), non-critical"
fi

echo "[inbox-chain] done: $(date '+%Y-%m-%d %H:%M:%S')"
