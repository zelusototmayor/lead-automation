# Lead Automation System

Automated lead sourcing, enrichment, and cold email outreach for agencies.

## Features

- **Lead Sourcing**: Finds agencies via Google Maps Places API
- **Enrichment**: Enriches leads with Apollo.io (contacts, company info)
- **CRM**: Stores everything in Google Sheets
- **Personalization**: Uses Claude AI to write personalized emails
- **Outreach**: Sends via Instantly.ai with automatic sequences

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure API Keys

Copy `.env.example` to `.env` and fill in your API keys:

```bash
cp .env.example .env
```

Required keys:
- `GOOGLE_MAPS_API_KEY` - Google Cloud Places API
- `APOLLO_API_KEY` - Apollo.io API
- `ANTHROPIC_API_KEY` - Claude API
- `INSTANTLY_API_KEY` - Instantly.ai API

### 3. Set Up Google Sheets

1. Place your Google service account JSON in `config/google_credentials.json`
2. Share your Google Sheet with the service account email
3. Update `config/settings.yaml` with your spreadsheet ID

### 4. Run

```bash
python src/main.py
```

## Docker Deployment

### Build and Run

```bash
docker-compose up --build
```

### Run on Schedule

The docker-compose includes a scheduler that runs daily at 8 AM.

### Manual Run

```bash
docker-compose run --rm lead-automation python src/main.py
```

## Configuration

### `config/settings.yaml`

- Target cities and countries
- Search queries for agencies
- Email settings
- Daily lead targets

### `config/email_templates.yaml`

- Email sequence templates
- Personalization instructions

## Project Structure

```
lead-automation/
├── config/
│   ├── settings.yaml          # Main configuration
│   ├── email_templates.yaml   # Email templates
│   └── google_credentials.json # Google service account (not in git)
├── src/
│   ├── main.py               # Main orchestrator
│   ├── lead_sourcing/        # Google Maps + Apollo
│   ├── crm/                  # Google Sheets
│   └── email/                # Personalization + Instantly
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## DigitalOcean Deployment

1. Create a droplet (Basic $6/month is sufficient)
2. SSH into the droplet
3. Install Docker:
   ```bash
   curl -fsSL https://get.docker.com | sh
   ```
4. Clone this repo
5. Copy your `.env` and `config/google_credentials.json`
6. Run:
   ```bash
   docker-compose up -d
   ```

## Monitoring

Check logs:
```bash
docker-compose logs -f lead-automation
```

Check CRM stats:
```bash
docker-compose run --rm lead-automation python -c "
from src.crm import GoogleSheetsCRM
crm = GoogleSheetsCRM('config/google_credentials.json', 'YOUR_SPREADSHEET_ID')
print(crm.get_stats())
"
```
