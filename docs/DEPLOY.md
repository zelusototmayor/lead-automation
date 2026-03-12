# Deployment & Operations

## Infrastructure

- **Server:** DigitalOcean droplet at `143.110.169.251` (SSH as `root`)
- **Docker image:** `zelusottomayor/lead-automation:latest` (Docker Hub)
- **Dashboard:** Runs via Kamal (`leads-dashboard-web` container) — deployed separately
- **Automation:** Docker containers run and exit (no persistent container)

## Server File Layout

```
/root/lead-automation/
├── .env                  # API keys — DO NOT OVERWRITE (server has its own keys)
├── run.sh                # Cron entry: AEC daily pipeline
├── run-startups.sh       # Cron entry: Startups daily pipeline
├── config/               # Synced from local config/
│   ├── settings.yaml
│   ├── email_templates.yaml
│   └── google_credentials.json
└── logs/                 # Mounted into container
```

## Cron Schedule

```cron
0 8 * * *  /root/lead-automation/run.sh          # AEC pipeline (8 AM UTC)
0 9 * * *  /root/lead-automation/run-startups.sh # Startups pipeline (9 AM UTC)
```

Local Services is not scheduled — run manually via dashboard or CLI.

## Deploy Workflow

```bash
# Standard deploy (code changes only)
./scripts/deploy-automation.sh

# Deploy + sync config files (settings.yaml, email_templates.yaml, google_credentials.json)
./scripts/deploy-automation.sh --sync-config
```

What the script does:
1. Build Docker image for `linux/amd64` (server arch)
2. Push to `zelusottomayor/lead-automation:latest` on Docker Hub
3. SSH into server → `docker pull` latest image

## Manual Pipeline Runs (on server)

```bash
ssh root@143.110.169.251

# AEC pipeline
docker run --rm --env-file /root/lead-automation/.env \
  -v /root/lead-automation/config:/app/config:ro \
  -v /root/lead-automation/logs:/app/logs \
  zelusottomayor/lead-automation:latest python src/main.py

# B2B Startups
docker run --rm --env-file /root/lead-automation/.env \
  -v /root/lead-automation/config:/app/config:ro \
  -v /root/lead-automation/logs:/app/logs \
  zelusottomayor/lead-automation:latest python src/startups.py

# Local Services
docker run --rm --env-file /root/lead-automation/.env \
  -v /root/lead-automation/config:/app/config:ro \
  -v /root/lead-automation/logs:/app/logs \
  zelusottomayor/lead-automation:latest python src/local_services.py
```

## Logs

```bash
# On server, latest log
ls -lt /root/lead-automation/logs/
tail -f /root/lead-automation/logs/<latest>.log
```

## Known Issues

- **Apollo 401 errors:** Server API key may be expired — update `APOLLO_API_KEY` in `/root/lead-automation/.env`
- **Ofelia not used:** Job-exec approach failed (needs running container). Automation uses system cron instead.

## Adding a New Campaign Cron

1. Add a `run-<campaign>.sh` script in `scripts/`
2. SCP it to server: `scp scripts/run-<campaign>.sh root@143.110.169.251:/root/lead-automation/`
3. SSH → `chmod +x /root/lead-automation/run-<campaign>.sh`
4. Add cron entry: `crontab -e`

## Environment Variables (`.env`)

```
GOOGLE_MAPS_API_KEY=
APOLLO_API_KEY=
ANTHROPIC_API_KEY=
INSTANTLY_API_KEY=
SERPAPI_API_KEY=
DASHBOARD_USERNAME=
DASHBOARD_PASSWORD=
```
