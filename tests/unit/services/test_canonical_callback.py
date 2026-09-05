from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4
import pytest
from src.crm.services.callback_execution import (
    CanonicalCallbackCalendar,
    CalendarProjectionError,
)


class Response:
    def __init__(self, status, data=None):
        self.status_code = status
        self.data = data or {}

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("provider failed")


class Calendar(CanonicalCallbackCalendar):
    def __init__(self):
        super().__init__("unused", calendar_id="test@example.test")
        self.events = {}
        self.calls = []
        self.fail_once = False

    def _request(self, method, path, **kwargs):
        self.calls.append((method, path))
        event_id = path.rsplit("/", 1)[-1]
        if method == "GET":
            return (
                Response(200, self.events[event_id])
                if event_id in self.events
                else Response(404)
            )
        if method == "POST":
            value = kwargs["json"]
            event_id = value["id"]
            if event_id in self.events:
                return Response(409)
            self.events[event_id] = value
            if self.fail_once:
                self.fail_once = False
                raise TimeoutError("uncertain response")
            return Response(200, value)
        if method == "PATCH":
            self.events[event_id].update(kwargs["json"])
            return Response(200, self.events[event_id])
        if method == "DELETE":
            self.events.pop(event_id, None)
            return Response(204)


def task():
    return SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        title="Telefonar",
        due_at=datetime(2026, 10, 25, 10, 0, tzinfo=UTC),
        status="open",
    )


def test_uncertain_creation_retry_uses_one_owned_event_and_timezone():
    calendar = Calendar()
    t = task()
    calendar.fail_once = True
    with pytest.raises(TimeoutError):
        calendar.sync_task(t)
    result = calendar.sync_task(t)
    assert result["verified"] is True and len(calendar.events) == 1
    event = next(iter(calendar.events.values()))
    assert event["start"]["dateTime"] == "2026-10-25T10:00:00+00:00"
    assert sum(method == "POST" for method, _ in calendar.calls) == 1


def test_foreign_event_cannot_be_modified_or_deleted():
    calendar = Calendar()
    t = task()
    event_id = "crm" + t.id.hex
    calendar.events[event_id] = {"id": event_id, "summary": "Reunião externa"}
    with pytest.raises(CalendarProjectionError):
        calendar.sync_task(t)
    t.status = "cancelled"
    with pytest.raises(CalendarProjectionError):
        calendar.sync_task(t)
    assert all(method == "GET" for method, _ in calendar.calls)


def test_reschedule_and_cancel_reuse_owned_event():
    calendar = Calendar()
    t = task()
    calendar.sync_task(t)
    t.due_at = datetime(2026, 9, 20, 11, 0, tzinfo=UTC)
    calendar.sync_task(t)
    event = next(iter(calendar.events.values()))
    assert event["start"]["dateTime"] == "2026-09-20T12:00:00+01:00"
    t.status = "cancelled"
    result = calendar.sync_task(t)
    assert result["status"] == "deleted" and calendar.events == {}


def test_legacy_owned_callback_is_adopted_without_duplicate():
    calendar = Calendar()
    t = task()
    legacy_id = "previouscallback"
    calendar.events[legacy_id] = {
        "id": legacy_id,
        "summary": "Callback anterior",
        "extendedProperties": {"private": {"pt_logistics_callback": "1"}},
    }
    result = calendar.sync_task(t, legacy_event_id=legacy_id)
    assert result["event_id"] == legacy_id and len(calendar.events) == 1
    assert not any(method == "POST" for method, _ in calendar.calls)
    event = calendar.events[legacy_id]
    assert event["extendedProperties"]["private"]["crm_task_id"] == str(t.id)
    t.status = "cancelled"
    calendar.sync_task(t, legacy_event_id=legacy_id)
    assert calendar.events == {}


def test_legacy_external_or_foreign_crm_event_is_never_adopted():
    for private in ({}, {"pt_logistics_callback": "1", "crm_task_id": str(uuid4())}):
        calendar = Calendar()
        t = task()
        legacy_id = "foreigncalendar"
        calendar.events[legacy_id] = {
            "id": legacy_id,
            "extendedProperties": {"private": private},
        }
        with pytest.raises(CalendarProjectionError):
            calendar.sync_task(t, legacy_event_id=legacy_id)
        assert len(calendar.events) == 1 and all(
            method == "GET" for method, _ in calendar.calls
        )


def test_missing_legacy_id_falls_back_to_same_deterministic_event():
    calendar = Calendar()
    t = task()
    first = calendar.sync_task(t, legacy_event_id="deletedcallback")
    second = calendar.sync_task(t, legacy_event_id="deletedcallback")
    assert first["event_id"] == second["event_id"] == "crm" + t.id.hex
    assert len(calendar.events) == 1
