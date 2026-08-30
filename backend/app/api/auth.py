"""Authentication boundary for destructive or resource-intensive demo mutations."""
from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from app.config import Settings, get_settings


def require_mutation_token(
    supplied_token: Annotated[str | None, Header(alias="X-Waggle-Admin-Token")] = None,
    settings: Settings = Depends(get_settings),
) -> None:
    """Require an admin token when public mutation protection is enabled."""
    if not settings.protect_mutation_endpoints:
        return
    configured = settings.mutation_api_token
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Mutation endpoints are locked until MUTATION_API_TOKEN is configured",
        )
    if supplied_token is None or not hmac.compare_digest(supplied_token, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid X-Waggle-Admin-Token header is required",
        )
