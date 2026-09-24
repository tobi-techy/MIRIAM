"""Regression tests for PR #10 review blockers.

B1: pre-HKDF ciphertext must still decrypt after the KDF migration.
B2: /metrics in production requires a valid JWT, not any Bearer string.
S1: rate-limit falls back to a capped local window when Redis is down.
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")


def _legacy_fernet_for(secret: str):
    from cryptography.fernet import Fernet

    digest = hashlib.sha256(secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest).decode())


def test_decrypt_value_reads_pre_hkdf_rows(monkeypatch):
    secret = "e" * 40
    monkeypatch.setenv("ENCRYPTION_KEY", secret)
    monkeypatch.setenv("SECRET_KEY", "s" * 40)
    from miriam_agent.config.settings import get_settings

    get_settings.cache_clear()
    try:
        from miriam_agent.core import security

        legacy_ct = _legacy_fernet_for(secret).encrypt(b"grandma-photo").decode()
        assert security.decrypt_value(legacy_ct) == "grandma-photo"
        # New writes still round-trip.
        assert security.decrypt_value(security.encrypt_value("new-note")) == "new-note"
    finally:
        get_settings.cache_clear()


def test_validator_decrypts_pre_hkdf_rows(monkeypatch):
    import asyncio

    secret = "v" * 40
    monkeypatch.setenv("ENCRYPTION_KEY", secret)
    monkeypatch.setenv("SECRET_KEY", secret)
    from miriam_agent.safety.validator import InputValidator

    v = InputValidator()
    legacy_ct = _legacy_fernet_for(secret).encrypt(b"old-secret").decode()
    assert asyncio.run(v.decrypt_sensitive_data(legacy_ct)) == "old-secret"


def test_metrics_rejects_fake_bearer_in_production(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("JWT_SECRET", "j" * 40)
    monkeypatch.setenv("SECRET_KEY", "s" * 40)
    monkeypatch.setenv("ENCRYPTION_KEY", "e" * 40)
    monkeypatch.setenv("JWT_AUDIENCE", "miriam-api")
    monkeypatch.setenv("JWT_ISSUER", "rail-backend")
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://app.example.com")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://miriam:strong-prod-pw-1234567890@localhost:5432/miriam",
    )
    monkeypatch.setenv("RAIL_SERVICE_KEY", "r" * 40)
    from miriam_agent.config.settings import get_settings

    get_settings.cache_clear()
    try:
        from miriam_agent.api.main import app

        client = TestClient(app, raise_server_exceptions=False)
        assert client.get("/metrics").status_code == 404
        assert (
            client.get(
                "/metrics", headers={"Authorization": "Bearer garbage"}
            ).status_code
            == 404
        )
        from miriam_agent.auth.jwt import create_token

        # A plain user token is no longer enough: metrics are infrastructure
        # facts, so the token must carry an admin/metrics role.
        user_token = create_token("u1")
        resp = client.get("/metrics", headers={"Authorization": f"Bearer {user_token}"})
        assert resp.status_code == 404
        metrics_token = create_token("u1", claims={"role": "metrics"})
        resp = client.get(
            "/metrics", headers={"Authorization": f"Bearer {metrics_token}"}
        )
        assert resp.status_code == 200
        # The rail service key is the scraper-friendly alternative.
        resp = client.get("/metrics", headers={"X-Rail-Service-Key": "r" * 40})
        assert resp.status_code == 200
        resp = client.get("/metrics", headers={"X-Rail-Service-Key": "wrong"})
        assert resp.status_code == 404
    finally:
        get_settings.cache_clear()


def test_rate_limit_falls_back_locally_when_redis_down(monkeypatch):
    import asyncio

    monkeypatch.setenv("ENCRYPTION_KEY", "e" * 40)
    from miriam_agent.safety.validator import InputValidator

    v = InputValidator()
    v._local_buckets.clear()

    def _boom():
        raise ConnectionError("redis down")

    v._get_redis = _boom  # type: ignore[method-assign]
    allowed = [
        asyncio.run(v.validate_rate_limit("u-fallback", "transaction"))
        for _ in range(11)
    ]
    assert allowed[:10] == [True] * 10
    assert allowed[10] is False


def test_local_bucket_cap_evicts_on_new_keys(monkeypatch):
    import time

    monkeypatch.setenv("ENCRYPTION_KEY", "e" * 40)
    from miriam_agent.safety.validator import InputValidator

    v = InputValidator()
    v._local_buckets.clear()
    v._MAX_LOCAL_BUCKETS = 3
    now = time.time()
    for i in range(5):
        v._prune_local_bucket(f"ratelimit:u{i}:transaction", now, 60)
    assert len(v._local_buckets) <= 3
    v._MAX_LOCAL_BUCKETS = 10_000
    v._local_buckets.clear()
