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
    settings = _settings(
        ENVIRONMENT="production",
        JWT_SECRET="j" * 40,
        SECRET_KEY="s" * 40,
    )
    assert settings.ENVIRONMENT == "production"


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
