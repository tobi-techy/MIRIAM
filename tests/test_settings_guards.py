"""Tests that production refuses to boot with placeholder signing secrets.

Compose ships ``ENVIRONMENT=production``; the previous defaults
(``change-me-in-production``) meant forged JWTs would validate. The guard is
scoped to production so development and tests keep the convenient defaults.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

import pytest  # noqa: E402


def _settings(**kwargs):
    from miriam_agent.config.settings import Settings

    # Ignore any local .env so the assertion depends only on the passed values.
    return Settings(_env_file=None, **kwargs)


def test_production_refuses_default_secrets():
    with pytest.raises(Exception):
        _settings(
            ENVIRONMENT="production",
            JWT_SECRET="change-me-in-production",
            SECRET_KEY="change-me-in-production",
        )


def test_production_refuses_short_secrets():
    with pytest.raises(Exception):
        _settings(ENVIRONMENT="production", JWT_SECRET="short", SECRET_KEY="short")


def test_production_accepts_strong_secrets():
    assert _strong().ENVIRONMENT == "production"


def _strong(**overrides):
    base = dict(
        ENVIRONMENT="production",
        JWT_SECRET="j" * 40,
        SECRET_KEY="s" * 40,
        ENCRYPTION_KEY="e" * 40,
        JWT_AUDIENCE="miriam-api",
        JWT_ISSUER="rail-backend",
        ALLOWED_ORIGINS="https://app.example.com",
        DATABASE_URL="postgresql+asyncpg://miriam:strong-prod-pw-1234567890@localhost:5432/miriam",
    )
    base.update(overrides)
    return _settings(**base)


def test_production_rejects_short_encryption_key():
    with pytest.raises(Exception):
        _strong(ENCRYPTION_KEY="short")


def test_production_rejects_missing_audience_issuer():
    with pytest.raises(Exception):
        _strong(JWT_AUDIENCE="")
    with pytest.raises(Exception):
        _strong(JWT_ISSUER="")


def test_production_rejects_wildcard_origins():
    with pytest.raises(Exception):
        _strong(ALLOWED_ORIGINS="*")
    with pytest.raises(Exception):
        _strong(ALLOWED_ORIGINS="https://app.example.com,*")
    with pytest.raises(Exception):
        _strong(ALLOWED_ORIGINS="https://app.example.com, *, https://other.example.com")


def test_production_rejects_dev_example_secrets():
    """The public .env.example values must never boot in production."""
    with pytest.raises(Exception):
        _strong(JWT_SECRET="dev-jwt-secret-change-in-production-must-be-32-chars")
    with pytest.raises(Exception):
        _strong(SECRET_KEY="dev-secret-key-change-in-production")
    with pytest.raises(Exception):
        _strong(
            ENCRYPTION_KEY="dev-encryption-key-change-in-production-must-be-32-chars"
        )


def test_production_rejects_default_db_password():
    with pytest.raises(Exception):
        _strong(
            DATABASE_URL="postgresql+asyncpg://miriam:miriam_password@localhost:5432/miriam"
        )


def test_blank_typed_env_falls_back_to_defaults(monkeypatch):
    """A host that injects KEY= for unset vars must not crash startup."""
    monkeypatch.setenv("DEBUG", "")
    monkeypatch.setenv("GO_REQUEST_TIMEOUT", "")
    monkeypatch.setenv("OPENAI_MAX_TOKENS", "")
    settings = _settings(ENVIRONMENT="development")
    assert settings.DEBUG is False
    assert settings.GO_REQUEST_TIMEOUT == 15.0
    assert settings.OPENAI_MAX_TOKENS == 4096


def test_libpq_database_url_uses_asyncpg():
    settings = _settings(
        ENVIRONMENT="development",
        DATABASE_URL="postgresql://miriam:pw@db:5432/miriam",
    )
    assert settings.DATABASE_URL.startswith("postgresql+asyncpg://")
    postgres = _settings(
        ENVIRONMENT="development",
        DATABASE_URL="postgres://miriam:pw@db:5432/miriam",
    )
    assert postgres.DATABASE_URL.startswith("postgresql+asyncpg://")


def test_explicit_database_driver_is_kept():
    url = "postgresql+psycopg://miriam:pw@db:5432/miriam"
    settings = _settings(ENVIRONMENT="development", DATABASE_URL=url)
    assert settings.DATABASE_URL == url


def test_development_allows_defaults(monkeypatch):
    """Development keeps the convenient defaults.

    The ambient environment is cleared first: pydantic-settings reads real env
    vars in preference to the defaults, so on a machine that exports
    JWT_SECRET/SECRET_KEY (a developer's shell sourcing .env) this asserted
    against the developer's own secret rather than the shipped default.
    """
    for var in ("JWT_SECRET", "SECRET_KEY", "ENCRYPTION_KEY"):
        monkeypatch.delenv(var, raising=False)
    settings = _settings(ENVIRONMENT="development")
    assert settings.JWT_SECRET == "change-me-in-production"
