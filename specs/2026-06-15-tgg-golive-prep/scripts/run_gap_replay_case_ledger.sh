#!/bin/bash
# Run the go-live gap through the canonical Christopher replay harness for the
# case-ledger maintenance chats only (the same four chats the validated v64
# harness used to produce the eval corpus).
set -uo pipefail
SPEC=~/pcl-biz/_agents/edna/specs/2026-06-09-christopher-wa-eval-disamb
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SANDBOX="$HERE/sandbox/gap-replay"
LOGDIR="$SANDBOX/logs"
mkdir -p "$LOGDIR"
declare -a RUNS=(
  "sk 120363403845802098@g.us"
  "amk 120363421424519051@g.us"
  "hg 120363422582425366@g.us"
  "pg 120363423568509280@g.us"
)
# Gap starts immediately after the eval corpus's latest case timestamp:
# 2026-06-13 00:57:54 SGT (exclusive at bridge-ingest; replay can use >= safely
# because the source_ref is absent before the inserted gap rows for these chats).
declare -a WINDOWS=(
  "2026-06-13 00:57:54|2026-06-14 00:00:00|0613"
  "2026-06-14 00:00:00|2026-06-15 00:00:00|0614"
  "2026-06-15 00:00:00|2026-06-16 00:00:00|0615"
)
for run in "${RUNS[@]}"; do
  read -r label jid <<< "$run"
  for window in "${WINDOWS[@]}"; do
    IFS='|' read -r since until tag <<< "$window"
    log="$LOGDIR/${label}-${tag}.log"
    if [ -f "$log.done" ]; then
      echo "=== ${label} ${tag} SKIP (done marker) ==="
      continue
    fi
    row_count=$(sqlite3 "$SANDBOX/tenants/tgg.db" "SELECT COUNT(*) FROM bridge_message_log WHERE chat_jid='$jid' AND sgt >= '$since' AND sgt < '$until';")
    if [ "$row_count" = "0" ]; then
      echo "=== ${label} ${tag} SKIP (0 bridge rows) ==="
      touch "$log.done"
      continue
    fi
    CONT=1; [ -d "$SANDBOX/hermes-home" ] || CONT=0
    echo "=== ${label} ${tag} start $(date +%H:%M:%S) (continue=$CONT) ==="
    CONTINUE=$CONT NIGHTLY_COMPACT=1 bash "$SPEC/scripts/run_replay.sh" "$SANDBOX" "$since" "$until" "gap-${label}-${tag}" "$jid" \
      > "$log" 2>&1
    rc=$?
    echo "=== ${label} ${tag} exit=$rc $(date +%H:%M:%S) ==="
    if [ $rc -eq 0 ]; then
      touch "$log.done"
    else
      tail -40 "$log"
      exit $rc
    fi
  done
done
echo "ALL GAP CASE-LEDGER REPLAY DONE $(date +%H:%M:%S)"
