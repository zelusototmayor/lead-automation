"""Read-only source rank for the explicitly requested transport import cohort.

Membership comes only from verified creation receipts, never names, dates,
priority, or an EBITDA claim. Eligibility remains the pipeline's responsibility.
"""
import json
from pathlib import Path
from uuid import UUID

from sqlalchemy import case

_COHORT = json.loads(Path(__file__).with_name('transportes_20260907.json').read_text())
_RANKS = {UUID(row['lead_id']): row['source_row'] for row in _COHORT['records']}
if len(_RANKS) != 379 or len(set(_RANKS.values())) != 379:
    raise ValueError('Invalid transport import rank manifest')


def untouched_source_rank(lead_id):
    """Sort exact imported IDs by source row, followed by every other ID."""
    return case(_RANKS, value=lead_id, else_=max(_RANKS.values()) + 1).asc()
