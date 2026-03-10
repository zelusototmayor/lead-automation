#!/bin/bash
# EU B2B Outreach Pipeline Runner (PT/UK/ES)
# Pulls latest image and runs the EU outreach sourcing pipeline once

set -e

WORKDIR="/root/lead-automation"
IMAGE="zelusottomayor/lead-automation:latest"
LOGFILE="$WORKDIR/logs/eu-outreach.log"

echo "========================================" >> "$LOGFILE"
echo "[$(date)] Starting EU B2B outreach pipeline..." >> "$LOGFILE"
echo "========================================" >> "$LOGFILE"

# Pull latest image
docker pull "$IMAGE" >> "$LOGFILE" 2>&1

# Run EU outreach pipeline (one-shot, auto-cleanup)
docker run --rm \
  --env-file "$WORKDIR/.env" \
  -v "$WORKDIR/config:/app/config:ro" \
  -v "$WORKDIR/logs:/app/logs" \
  "$IMAGE" \
  python src/eu_outreach.py \
  >> "$LOGFILE" 2>&1

EXIT_CODE=$?

echo "[$(date)] EU outreach pipeline finished with exit code $EXIT_CODE" >> "$LOGFILE"
echo "" >> "$LOGFILE"

exit $EXIT_CODE
