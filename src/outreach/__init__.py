"""Outreach modules (email personalization and campaign management)."""

from .personalize import EmailPersonalizer, calculate_lead_score
from .instantly_client import InstantlyClient, setup_campaign
from .sync_instantly import InstantlySyncer, sync_from_instantly

__all__ = [
    "EmailPersonalizer",
    "calculate_lead_score",
    "InstantlyClient",
    "setup_campaign",
    "InstantlySyncer",
    "sync_from_instantly",
]
