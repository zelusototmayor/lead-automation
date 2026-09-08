from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from src.crm.services import call_metrics as module
from src.crm.pt_logistics_sheet import DATE_FIELDS, FIELD_ALIASES


# Independent schema-derived matrix: new contact date fields in the real sheet
# must not silently fall outside the metrics reader's whitelist. Scheduling and
# UI-touch dates do not prove contact. Keep the older import aliases covered too.
NON_CONTACT_DATE_FIELDS = {'due', 'proposal_next_action_due', 'dashboard_touched'}
CONTACT_DATE_HEADERS = sorted(
    {FIELD_ALIASES[key] for key in DATE_FIELDS - NON_CONTACT_DATE_FIELDS}
    | {'Proposal Email Sent', 'Last Contact'}
)


@pytest.mark.parametrize('header', CONTACT_DATE_HEADERS)
@pytest.mark.parametrize('value, expected', [
    ('2026-09-07', 'prior'), ('07/09/2026', 'prior'), ('2024/02/03', 'prior'),
    ('2026-09-08', 'uncertain'), ('2026-09-09', 'none'),
    ('2026-09-08T08:00:00+01:00', 'prior'),
    ('2026-09-08T20:00:00+01:00', 'none'),
    ('2026-09-08T10:00:00+01:00', 'none'),  # Equal is not strictly prior.
    ('2026-09-08T08:00:00', 'uncertain'),
    ('yes', 'uncertain'), ('2026-02-30', 'uncertain'),
    (True, 'uncertain'), (123, 'uncertain'), ([], 'uncertain'), ({}, 'uncertain'),
    ('', 'none'), ('  ', 'none'), (None, 'none'),
])
def test_legacy_contact_dates_are_evaluated_strictly_before_call(header, value, expected):
    assert module.legacy_contact_state({header: value},
        datetime.fromisoformat('2026-09-08T10:00:00+01:00'), ZoneInfo('Europe/Lisbon')) == expected


@pytest.mark.parametrize('header', CONTACT_DATE_HEADERS)
def test_date_only_contact_uses_lisbon_day_not_utc(header):
    assert module.legacy_contact_state({header: '2026-09-07'},
        datetime.fromisoformat('2026-09-07T23:30:00+00:00'), ZoneInfo('Europe/Lisbon')) == 'prior'
