#!/bin/bash
# Pipeline Monitor — runs after all pipelines to check health & send alerts
# Cron: 0 11 * * * /root/lead-automation/scripts/run-monitor.sh

set -e

WORKDIR="/root/lead-automation"
IMAGE="zelusottomayor/lead-automation:latest"
LOGFILE="$WORKDIR/logs/monitor.log"

echo "========================================" >> "$LOGFILE"
echo "[$(date)] Running pipeline monitor..." >> "$LOGFILE"
echo "========================================" >> "$LOGFILE"

# Run monitor (reuses the already-pulled image from earlier pipeline runs)
docker run --rm \
  --env-file "$WORKDIR/.env" \
  -v "$WORKDIR/config:/app/config:ro" \
  -v "$WORKDIR/logs:/app/logs" \
  "$IMAGE" \
  python src/monitor.py \
  >> "$LOGFILE" 2>&1

EXIT_CODE=$?

echo "[$(date)] Monitor finished with exit code $EXIT_CODE" >> "$LOGFILE"
echo "" >> "$LOGFILE"

exit $EXIT_CODE
