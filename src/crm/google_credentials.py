"""Load Google credentials without weakening the configured identity boundary."""

from __future__ import annotations

import json
from collections.abc import Sequence

from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials


def load_google_credentials(credentials_file: str, scopes: Sequence[str]):
    """Load an explicitly supported Google credential file.

    Production historically used a service account. Authorized-user credentials
    are also supported for operational recovery and retain the OAuth scopes that
    were granted to that user. Unknown credential types fail closed.
    """
    with open(credentials_file, encoding="utf-8") as handle:
        credential_type = (json.load(handle).get("type") or "").strip()

    if credential_type == "service_account":
        return ServiceAccountCredentials.from_service_account_file(
            credentials_file,
            scopes=scopes,
        )
    if credential_type == "authorized_user":
        # Preserve the scopes attached to the existing user grant. Passing
        # requested scopes during refresh can attempt to renegotiate consent
        # and causes older refresh tokens to fail with invalid_scope.
        return UserCredentials.from_authorized_user_file(credentials_file)
    raise ValueError(f"Unsupported Google credential type: {credential_type or 'missing'}")
