"""Mutation endpoint authentication tests."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.auth import require_mutation_token
from app.config import Settings


def test_mutation_auth_is_opt_in_for_local_demo() -> None:
    assert require_mutation_token(None, Settings(protect_mutation_endpoints=False)) is None


def test_public_mutations_fail_closed_without_configured_token() -> None:
    with pytest.raises(HTTPException) as caught:
        require_mutation_token(None, Settings(protect_mutation_endpoints=True))
    assert caught.value.status_code == 503


def test_public_mutations_require_exact_admin_token() -> None:
    settings = Settings(protect_mutation_endpoints=True, mutation_api_token="correct-secret")
    with pytest.raises(HTTPException) as caught:
        require_mutation_token("wrong-secret", settings)
    assert caught.value.status_code == 401
    assert require_mutation_token("correct-secret", settings) is None
