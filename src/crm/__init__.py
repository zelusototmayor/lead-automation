"""CRM modules."""

from .sheets import GoogleSheetsCRM, CRM_HEADERS, COL
from .local_services_sheet import LocalServicesCRM, LOCAL_SERVICES_HEADERS, COL_LS

__all__ = [
    "GoogleSheetsCRM", "CRM_HEADERS", "COL",
    "LocalServicesCRM", "LOCAL_SERVICES_HEADERS", "COL_LS",
]
