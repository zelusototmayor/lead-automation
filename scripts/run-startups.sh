#!/bin/bash
# B2B Startups Pipeline Runner
# Pulls latest image and runs the startups sourcing pipeline once

set -e

WORKDIR="/root/lead-automation"
IMAGE="zelusottomayor/lead-automation:latest"
LOGFILE="$WORKDIR/logs/startups.log"

echo "========================================" >> "$LOGFILE"
echo "[$(date)] Starting B2B startups pipeline..." >> "$LOGFILE"
echo "========================================" >> "$LOGFILE"

# Pull latest image
docker pull "$IMAGE" >> "$LOGFILE" 2>&1

# Run startups pipeline (one-shot, auto-cleanup)
docker run --rm \
  --env-file "$WORKDIR/.env" \
  -v "$WORKDIR/config:/app/config:ro" \
  -v "$WORKDIR/logs:/app/logs" \
  "$IMAGE" \
  python src/startups.py \
  >> "$LOGFILE" 2>&1

EXIT_CODE=$?

echo "[$(date)] Startups pipeline finished with exit code $EXIT_CODE" >> "$LOGFILE"
echo "" >> "$LOGFILE"

exit $EXIT_CODE
