#!/bin/bash
# Deploy Lead Automation to DigitalOcean server
# Usage: ./scripts/deploy-automation.sh [--sync-config]

set -e

SERVER="root@143.110.169.251"
IMAGE="zelusottomayor/lead-automation:latest"

echo "=== Building Docker image (amd64) ==="
docker buildx build --platform linux/amd64 -t "$IMAGE" --load .

echo ""
echo "=== Pushing to Docker Hub ==="
docker push "$IMAGE"

echo ""
echo "=== Pulling latest image on server ==="
ssh "$SERVER" "docker pull $IMAGE"

if [[ "$1" == "--sync-config" ]]; then
  echo ""
  echo "=== Syncing config files ==="
  scp config/settings.yaml "$SERVER:/root/lead-automation/config/"
  scp config/email_templates.yaml "$SERVER:/root/lead-automation/config/"
  echo "Config files synced (NOT .env - that stays on server)"
fi

echo ""
echo "=== Deploy complete ==="
echo "Run manually on server:  ssh $SERVER /root/lead-automation/run.sh"
echo "Cron runs daily at 8 AM UTC"
