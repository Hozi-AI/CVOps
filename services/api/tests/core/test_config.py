"""Tests for startup secret validation."""

import pytest

from cvops_api.config import Settings, validate_secrets


def _settings(**overrides) -> Settings:
    """Build a Settings object with safe defaults for testing."""
    base = {
        "JWT_SECRET": "a-real-secret-that-is-long-enough-yes",
        "WORKER_TOKEN": "real-worker-token",
        "S3_ACCESS_KEY": "GKrealaccesskey",
        "S3_SECRET_KEY": "realsecretkey",
        "ALLOW_INSECURE_DEFAULTS": False,
    }
    base.update(overrides)
    return Settings(**base)


def test_validate_secrets_passes_with_real_values():
    validate_secrets(_settings())  # must not raise


def test_validate_secrets_rejects_default_jwt_secret():
    s = _settings(JWT_SECRET="change-me-in-production-min-32-chars")
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        validate_secrets(s)


def test_validate_secrets_rejects_default_worker_token():
    s = _settings(WORKER_TOKEN="change-me-worker-token")
    with pytest.raises(RuntimeError, match="WORKER_TOKEN"):
        validate_secrets(s)


def test_validate_secrets_rejects_default_s3_keys():
    s = _settings(S3_ACCESS_KEY="GKchangeme", S3_SECRET_KEY="changeme")
    with pytest.raises(RuntimeError, match="S3_ACCESS_KEY"):
        validate_secrets(s)


def test_validate_secrets_rejects_change_prefix():
    s = _settings(JWT_SECRET="change_this_before_deploying")
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        validate_secrets(s)


def test_validate_secrets_allow_insecure_defaults_bypasses_check():
    s = _settings(
        JWT_SECRET="change-me-in-production-min-32-chars",
        WORKER_TOKEN="change-me-worker-token",
        ALLOW_INSECURE_DEFAULTS=True,
    )
    validate_secrets(s)  # must not raise
