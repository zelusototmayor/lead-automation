"""Keep undated imports and bookkeeping out of observed interaction metrics."""

from sqlalchemy import func
from src.crm.persistence.models import Activity


def operational_activity_filter():
    # Historical rows without actor metadata remain valid. Imported context has
    # a recording date, not evidence that the original interaction happened then.
    return func.coalesce(Activity.actor_type, "").not_in(
        ("migration", "import", "system")
    )
