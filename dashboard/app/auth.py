"""HTTP Basic Authentication for Dashboard"""

import os
import secrets
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

security = HTTPBasic()


def get_credentials():
    """Get dashboard credentials from environment variables."""
    return {
        "username": os.getenv("DASHBOARD_USER", "admin"),
        "password": os.getenv("DASHBOARD_PASSWORD", "changeme"),
    }


def authenticate(credentials: HTTPBasicCredentials = Depends(security)):
    """Verify HTTP Basic Auth credentials."""
    correct = get_credentials()

    username_correct = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        correct["username"].encode("utf-8")
    )
    password_correct = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        correct["password"].encode("utf-8")
    )

    if not (username_correct and password_correct):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    return credentials.username
