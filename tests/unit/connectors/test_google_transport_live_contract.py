import base64
from datetime import UTC, datetime
import pytest
from src.crm.connectors.google_transport import (
    GmailTransport,
    GoogleTransportError,
    extract_amounts,
)


class Response:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data

    def raise_for_status(self):
        pass


class Session:
    def __init__(self, mailbox="me@example.test"):
        self.calls = []
        self.mailbox = mailbox

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        if url.endswith("/profile"):
            return Response({"emailAddress": self.mailbox})
        if url.endswith("/messages"):
            return Response({"messages": [{"id": "m1"}], "nextPageToken": "next"})
        return Response(
            {
                "id": "m1",
                "threadId": "t1",
                "internalDate": "1788600000000",
                "labelIds": ["SENT"],
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [
                        {"name": "To", "value": "Ana <ana@example.test>"},
                        {"name": "Subject", "value": "Proposta"},
                    ],
                    "body": {
                        "data": base64.urlsafe_b64encode(b"Total: 1200 EUR").decode()
                    },
                },
            }
        )


def test_real_api_paths_sent_archived_and_explicit_paging():
    session = Session()
    transport = GmailTransport(
        credentials_file="unused",
        mailbox_email="me@example.test",
        since=datetime(2026, 9, 1, tzinfo=UTC),
        session=session,
        limit=5,
    )
    page = transport.fetch("mailbox:me@example.test", "previous")
    assert page["next_cursor"] == "next"
    assert page["observations"][0]["direction"] == "outbound"
    assert page["observations"][0]["contact_emails"] == ["ana@example.test"]
    query = session.calls[1][1]
    assert (
        query["pageToken"] == "previous"
        and "in:inbox" not in query["q"]
        and "is:unread" not in query["q"]
    )
    assert all(timeout == 20 for _, _, timeout in session.calls)


def test_wrong_authenticated_mailbox_is_rejected_before_list():
    session = Session("someone@example.test")
    with pytest.raises(GoogleTransportError):
        GmailTransport(
            credentials_file="unused", mailbox_email="me@example.test", session=session
        ).fetch("mailbox:me@example.test")
    assert len(session.calls) == 1


def test_only_explicit_amounts_become_candidates():
    assert extract_amounts("Preço a combinar")["one_off_amount"] is None
    assert (
        extract_amounts("Total: 1.200,00 EUR; Mensal: 100 EUR")["one_off_amount"]
        == "1200.00"
    )
    ambiguous = extract_amounts("Total: 1200 EUR ou Total: 2000 EUR")
    assert ambiguous["value_ambiguous"] is True and ambiguous["one_off_amount"] is None
