#!/bin/bash
# Pod-side hard cap: sleep MAX_HOURS, then stop or terminate THIS pod via the
# Runpod REST API — no matter what the driving session is doing.
#
# Why: a cost cap enforced only by an operator (or an agent) polling from
# outside is not enforced. Missed wakeups, dropped ssh, a crashed laptop, and
# the pod keeps billing. This runs ON the pod and needs nothing from outside.
#
# Usage (on the pod, key passed explicitly — pods do NOT have RUNPOD_API_KEY
# in their env unless the template puts it there):
#
#   RUNPOD_API_KEY=... setsid nohup bash /workspace/killswitch.sh <POD_ID> <MAX_HOURS> [stop|terminate] \
#       > /workspace/killswitch.log 2>&1 &
#
# From your machine, with rpt:
#
#   rpt run --pod <ID> --raw -- "cat > /workspace/killswitch.sh" < examples/killswitch.sh
#   rpt run --pod <ID> --background killswitch -- \
#       "RUNPOD_API_KEY=$RUNPOD_API_KEY bash /workspace/killswitch.sh <ID> 8 terminate"
#
# Keep it under /workspace: stop/resume recreates the container disk and
# anything under /root vanishes. Re-arm after every resume. Before launching a
# NEW run on the same pod, kill the old killswitch (pgrep -f '[k]illswitch')
# or it will fire on the new run's timeline.
set -u
POD_ID="${1:?pod id required}"
MAX_H="${2:?max hours required}"
ACTION="${3:-stop}"
: "${RUNPOD_API_KEY:?RUNPOD_API_KEY must be set (pass it explicitly; pods do not have it by default)}"

echo "[killswitch] armed: pod=$POD_ID cap=${MAX_H}h action=$ACTION start=$(date -u +%FT%TZ)"
sleep "$(python3 -c "print(int(float('$MAX_H') * 3600))")"
echo "[killswitch] CAP REACHED $(date -u +%FT%TZ) -> $ACTION $POD_ID"

if [ "$ACTION" = "terminate" ]; then
    curl -sS -X DELETE "https://rest.runpod.io/v1/pods/$POD_ID" \
        -H "Authorization: Bearer $RUNPOD_API_KEY"
else
    curl -sS -X POST "https://rest.runpod.io/v1/pods/$POD_ID/stop" \
        -H "Authorization: Bearer $RUNPOD_API_KEY"
fi
echo
echo "[killswitch] done"
