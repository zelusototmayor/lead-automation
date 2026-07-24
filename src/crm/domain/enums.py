from __future__ import annotations

from enum import Enum


class CRMStage(str, Enum):
    """Canonical CRM stages persisted and exchanged by domain code."""

    NEW = "new"
    CONTACTED = "contacted"
    QUALIFIED = "qualified"
    MEETING_BOOKED = "meeting_booked"
    MEETING_HELD = "meeting_held"
    PROPOSAL_REQUESTED = "proposal_requested"
    PROPOSAL_SENT = "proposal_sent"
    NEGOTIATION = "negotiation"
    WON = "won"
    LOST = "lost"
    NOT_A_FIT = "not_a_fit"
