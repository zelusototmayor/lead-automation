"""
Setup Logistics ES — Quote Agent campaign
==========================================
One-shot script for the Spanish logistics campaign launch (2026-04-27).

What it does:
  1. Creates a NEW PAUSED campaign "Logistics ES — Quote Agent".
  2. Schedule: Tue/Wed/Thu 09:00–10:00 Europe/Madrid (skip weekends).
  3. 4-step sequence with delays 0/4/5/5 (= Day 0, 4, 9, 14).
  4. Assigns inbox jose@zelusotto.com (cold-dedicated).
  5. Daily limit: 10 sends/day.
  6. Open + link tracking OFF (cold rules: no clickable URLs).
  7. Imports the 124 leads from data/logistics_es/logistics_es_leads.json
     with first_name / last_name / company_name / email + custom var "city".
  8. Leaves the campaign PAUSED. You activate it manually in the Instantly UI.

Run from the lead-automation root (where .env lives):
    python scripts/setup_logistics_es_campaign.py [--dry-run]

The --dry-run flag prints what would be sent without hitting the API.
"""

import os
import sys
import json
import time
import argparse
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load_dotenv_simple():
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def load_instantly_client():
    """Load InstantlyClient bypassing the outreach __init__ (avoids anthropic import)."""
    spec = importlib.util.spec_from_file_location(
        "instantly_client", ROOT / "src" / "outreach" / "instantly_client.py"
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.InstantlyClient


# ---------------------------------------------------------------------------
# Campaign config
# ---------------------------------------------------------------------------

CAMPAIGN_NAME = "Logistics ES — Quote Agent"
SENDER_INBOX = "jose@zelusotto.com"
DAILY_LIMIT = 10
# Instantly's allowed timezone list excludes Europe/Madrid. Atlantic/Canary is
# the closest accepted zone and runs 1h behind Madrid year-round, so we shift
# the send window by -1h to land on 09:00–10:00 mainland Spain local time.
TIMEZONE = "Atlantic/Canary"
SEND_FROM = "08:00"
SEND_TO = "09:00"

# Tue=2, Wed=3, Thu=4 only
SCHEDULE_DAYS = {
    "0": False, "1": False, "2": True, "3": True, "4": True, "5": False, "6": False,
}

LEADS_JSON = ROOT / "data" / "logistics_es" / "logistics_es_leads.json"


# ---------------------------------------------------------------------------
# Email sequence — verbatim copy approved 2026-04-26
# ---------------------------------------------------------------------------

EMAIL_1_SUBJECT = "cotizaciones en {{company}}"
EMAIL_1_BODY = """Hola {{first_name}},

Estuve investigando empresas de transporte por carretera en {{city}} y {{company}} apareció varias veces. Le escribo por algo concreto.

En empresas como la suya, las cotizaciones que no salen el mismo día se pierden — el cliente llama al siguiente transportista. Lo que normalmente significa que usted, o quien esté cotizando, acaba respondiendo a horas en las que debería estar haciendo otra cosa.

He construido un agente de IA que lee esas solicitudes y prepara la cotización con sus tarifas y rutas. La revisan antes de enviar — el criterio sigue siendo suyo. Lo que desaparece son los minutos repetitivos.

Reenvíe una solicitud real a max@zelusottomayor.com y le devuelvo lo que prepararía el agente. Para que la compare con la suya antes de cualquier conversación.

Un saludo,
Zelu"""

EMAIL_2_SUBJECT = "re: cotizaciones en {{company}}"
EMAIL_2_BODY = """{{first_name}},

Imagino que la semana ha sido intensa. Le dejo el detalle que suele importar más a directores de operaciones:

Nada se envía automáticamente. El agente prepara el borrador con sus tarifas y su forma de redactar — pero la decisión final siempre la toma una persona del equipo. Lo que cambia es el tiempo: en vez de empezar cada cotización desde cero, su equipo revisa una propuesta que ya está casi lista.

Si quiere probarlo, reenvíe una solicitud real a max@zelusottomayor.com.

Un saludo,
Zelu"""

EMAIL_3_SUBJECT = "una idea distinta"
EMAIL_3_BODY = """{{first_name}},

Cambio el enfoque, porque el anterior puede no ser el adecuado.

Si el volumen de cotizaciones en {{company}} no es alto, esto probablemente no le aporta nada. Pero si su equipo está respondiendo a más de 30 o 40 solicitudes a la semana — y especialmente si algunas se quedan sin respuesta porque no hay tiempo material — el agente cambia esa dinámica. No por sustituir al equipo, sino por dejarles atender lo que de verdad necesita criterio humano.

No le pido una llamada. Si tiene curiosidad, reenvíe una solicitud a max@zelusottomayor.com y vea lo que devuelve. Si no encaja, lo entiendo perfectamente.

Un saludo,
Zelu"""

EMAIL_4_SUBJECT = "cierro este hilo"
EMAIL_4_BODY = """{{first_name}},

No quiero llenarle el buzón. Cierro aquí.

Si en algún momento quiere ver cómo respondería el agente a una cotización real de {{company}}, max@zelusottomayor.com está abierto. Sin agenda comercial, sólo el ejercicio.

Buena ruta,
Zelu"""


def to_html(body: str) -> str:
    """Wrap each line in <div> using Instantly's Quill-editor format.
    Empty lines become <div><br /></div>. Mirrors the AEC campaign body."""
    return "".join(
        "<div><br /></div>" if line == "" else f"<div>{line}</div>"
        for line in body.split("\n")
    )


def build_sequence():
    """Day 0 / Day 4 / Day 9 / Day 14 → delays 0/4/5/5 (each = gap from previous)."""
    steps = [
        {"step_number": 1, "type": "email", "delay": 0,
         "variants": [{"subject": EMAIL_1_SUBJECT, "body": to_html(EMAIL_1_BODY)}]},
        {"step_number": 2, "type": "email", "delay": 4,
         "variants": [{"subject": EMAIL_2_SUBJECT, "body": to_html(EMAIL_2_BODY)}]},
        {"step_number": 3, "type": "email", "delay": 5,
         "variants": [{"subject": EMAIL_3_SUBJECT, "body": to_html(EMAIL_3_BODY)}]},
        {"step_number": 4, "type": "email", "delay": 5,
         "variants": [{"subject": EMAIL_4_SUBJECT, "body": to_html(EMAIL_4_BODY)}]},
    ]
    return [{"steps": steps}]


def build_schedule_payload():
    return {
        "schedules": [{
            "name": "ES Tue-Thu morning",
            "timing": {"from": SEND_FROM, "to": SEND_TO},
            "days": SCHEDULE_DAYS,
            "timezone": TIMEZONE,
        }]
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Print plan without API calls")
    ap.add_argument("--reuse-existing", action="store_true",
                    help="If campaign with same name exists, use it instead of erroring")
    args = ap.parse_args()

    load_dotenv_simple()
    api_key = os.environ.get("INSTANTLY_API_KEY")
    if not api_key:
        print("ERROR: INSTANTLY_API_KEY not set in .env", file=sys.stderr)
        sys.exit(1)

    if not LEADS_JSON.exists():
        print(f"ERROR: leads file not found at {LEADS_JSON}", file=sys.stderr)
        sys.exit(1)

    leads = json.loads(LEADS_JSON.read_text(encoding="utf-8"))
    print(f"Loaded {len(leads)} leads from {LEADS_JSON.name}")

    sequences = build_sequence()
    schedule = build_schedule_payload()

    print(f"\nCampaign: {CAMPAIGN_NAME}")
    print(f"Sender:   {SENDER_INBOX}")
    print(f"Schedule: {SEND_FROM}–{SEND_TO} {TIMEZONE}, "
          f"days {[d for d,v in SCHEDULE_DAYS.items() if v]}")
    print(f"Sequence: {len(sequences[0]['steps'])} steps with delays "
          f"{[s['delay'] for s in sequences[0]['steps']]}")
    print(f"Daily limit: {DAILY_LIMIT}")
    print(f"Tracking:    open=OFF link=OFF")

    if args.dry_run:
        print("\n--dry-run: stopping before any API call.")
        return

    InstantlyClient = load_instantly_client()
    client = InstantlyClient(api_key)

    # 1) Check for collision
    existing = next((c for c in client.list_campaigns()
                     if c.get("name") == CAMPAIGN_NAME), None)
    if existing:
        if not args.reuse_existing:
            print(f"\nERROR: campaign '{CAMPAIGN_NAME}' already exists "
                  f"(id={existing['id']}). Re-run with --reuse-existing to update it.",
                  file=sys.stderr)
            sys.exit(1)
        cid = existing["id"]
        print(f"\nReusing existing campaign: {cid}")
    else:
        print(f"\nCreating campaign ...", end=" ", flush=True)
        result = client._make_request("POST", "campaigns", data={
            "name": CAMPAIGN_NAME,
            "campaign_schedule": schedule,
        })
        if not result:
            print("FAILED"); sys.exit(1)
        cid = result["id"]
        print(f"OK → {cid}")
        time.sleep(1)

    # 2) Inbox + tracking flags + daily limit (single PATCH)
    print(f"Patching inbox/tracking/daily_limit ...", end=" ", flush=True)
    patch = client._make_request("PATCH", f"campaigns/{cid}", data={
        "email_list": [SENDER_INBOX],
        "open_tracking": False,
        "link_tracking": False,
        "daily_limit": DAILY_LIMIT,
    })
    print("OK" if patch is not None else "PARTIAL")
    time.sleep(1)

    # 3) Sequence
    print(f"Setting sequence ...", end=" ", flush=True)
    r = client.set_campaign_sequences(cid, sequences)
    print("OK" if r is not None else "FAILED")
    time.sleep(1)

    # 4) Force pause
    print(f"Ensuring PAUSED ...", end=" ", flush=True)
    detail = client.get_campaign(cid)
    status = (detail or {}).get("status", "?")
    if status not in ("paused", "draft"):
        client.pause_campaign(cid)
        print(f"paused (was {status})")
    else:
        print(f"already {status}")
    time.sleep(1)

    # 5) Import leads
    print(f"\nImporting {len(leads)} leads...")
    payload = []
    for L in leads:
        payload.append({
            "email": L["email"],
            "first_name": L["first_name"],
            "last_name": L["last_name"],
            "company_name": L["company"],
            "website": L.get("website", ""),
            "phone": L.get("phone", ""),
            "city": L.get("city", ""),  # picked up as custom_variables.city
        })
    results = client.add_leads_to_campaign(cid, payload)
    n_ok = len(results) if results else 0
    print(f"  imported: {n_ok}/{len(leads)}")

    print(f"\n{'='*60}")
    print(f"Done. Campaign id: {cid}  status: paused")
    print(f"{'='*60}")
    print("Next steps (manual in Instantly UI):")
    print("  1. Open the campaign → verify sequence renders")
    print("  2. Send yourself a test email per step (Preview > Test)")
    print("  3. Confirm {{first_name}} / {{company}} / {{city}} all merge")
    print(f"  4. When ready: activate the campaign — first sends fire on")
    print(f"     the next Tue/Wed/Thu at 09:00 {TIMEZONE} (10/day cap)")


if __name__ == "__main__":
    main()
