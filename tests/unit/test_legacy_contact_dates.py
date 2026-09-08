from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from src.crm.services import call_metrics as module

@pytest.mark.parametrize('value, expected', [
    ('2026-09-07','prior'),('07/09/2026','prior'),('2024/02/03','prior'),
    ('2026-09-08','uncertain'),('2026-09-09','none'),
    ('2026-09-08T08:00:00+01:00','prior'),
    ('2026-09-08T20:00:00+01:00','none'),
    ('2026-09-08T08:00:00','uncertain'),
    ('yes','uncertain'),('', 'none'),(None,'none')])
def test_legacy_contact_dates_are_evaluated_strictly_before_call(value, expected):
    assert module.legacy_contact_state({'Initial Email Sent':value},
        datetime.fromisoformat('2026-09-08T10:00:00+01:00'),ZoneInfo('Europe/Lisbon'))==expected
