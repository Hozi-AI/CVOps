"""
Tests for cvops_api.core.storage.S3Backend using moto to mock S3.

S3Backend wraps synchronous boto3 calls inside async methods.
Since asyncio_mode = "auto" in pyproject.toml, async test functions are
collected and executed automatically without explicit @pytest.mark.anyio
or asyncio.run() wrappers.

Each test activates `with mock_aws():` and patches S3_ENDPOINT to None
so that boto3 targets the default AWS S3 endpoint — the one moto intercepts.
A non-None endpoint_url (e.g. localhost:3900) routes around moto's interception.
"""

from __future__ import annotations

import hashlib

from moto import mock_aws
from unittest.mock import patch

from cvops_api.config import settings
from cvops_api.core.storage import S3Backend, StorageBackend


def _mocked_backend() -> S3Backend:
    """Instantiate S3Backend with moto-compatible settings.

    S3_ENDPOINT must be None so boto3 uses the default AWS S3 endpoint
    that moto's mock_aws intercepts. Any custom endpoint_url (localhost:*) is
    not intercepted by moto and would route to a real server.

    moto autocreates buckets on first use, so the head_bucket check in
    `_verify_bucket()` passes after we explicitly create the bucket below.
    """
    import boto3

    boto3.client("s3").create_bucket(Bucket=settings.S3_BUCKET)
    return S3Backend()


def _moto_settings() -> tuple:
    return (
        patch.object(settings, "S3_ENDPOINT", None),
        patch.object(settings, "S3_REGION", "us-east-1"),
        patch.object(settings, "S3_ACCESS_KEY", "testing"),
        patch.object(settings, "S3_SECRET_KEY", "testing"),
        patch.object(settings, "S3_BUCKET", "test-bucket"),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_save_bytes_returns_sha256_hash() -> None:
    """save_bytes should return a string prefixed with 'sha256:'."""
    with mock_aws():
        a, b, c, d, e = _moto_settings()
        with a, b, c, d, e:
            backend = _mocked_backend()
            blob_hash = await backend.save_bytes(b"hello", "application/octet-stream")

    assert isinstance(blob_hash, str)
    assert blob_hash.startswith("sha256:")


async def test_save_bytes_idempotent() -> None:
    """Saving the same bytes twice must return the identical hash without error."""
    with mock_aws():
        a, b, c, d, e = _moto_settings()
        with a, b, c, d, e:
            backend = _mocked_backend()
            data = b"idempotent content"
            hash1 = await backend.save_bytes(data, "application/octet-stream")
            hash2 = await backend.save_bytes(data, "application/octet-stream")

    assert hash1 == hash2


async def test_get_bytes_returns_uploaded_content() -> None:
    """get_bytes should return exactly the bytes that were previously saved."""
    with mock_aws():
        a, b, c, d, e = _moto_settings()
        with a, b, c, d, e:
            backend = _mocked_backend()
            original = b"round-trip content"
            blob_hash = await backend.save_bytes(original, "application/octet-stream")
            retrieved = await backend.get_bytes(blob_hash)

    assert retrieved == original


def test_sha256_utility() -> None:
    """StorageBackend._sha256 should return 'sha256:' + the hex digest of the input."""
    data = b"test"
    expected = "sha256:" + hashlib.sha256(data).hexdigest()

    assert StorageBackend._sha256(data) == expected


def test_bucket_key_format() -> None:
    """_bucket_key should split the hex part into 'blobs/{first2}/{rest}' segments."""
    hex_part = "ab" * 32  # 64 hex chars — a valid SHA-256 digest
    blob_hash = "sha256:" + hex_part

    key = StorageBackend._bucket_key(blob_hash)

    assert key.startswith("blobs/")
    parts = key.split("/")
    # Expect exactly: ["blobs", "<first2>", "<remaining62>"]
    assert len(parts) == 3
    assert parts[1] == hex_part[:2]
    assert parts[2] == hex_part[2:]


async def test_get_presigned_get_returns_string() -> None:
    """get_presigned_get should return a non-empty string URL for an existing blob."""
    with mock_aws():
        a, b, c, d, e = _moto_settings()
        with a, b, c, d, e:
            backend = _mocked_backend()
            blob_hash = await backend.save_bytes(b"presign me", "application/octet-stream")
            url = await backend.get_presigned_get(blob_hash)

    assert isinstance(url, str)
    assert len(url) > 0


async def test_get_presigned_put_for_upload_returns_string() -> None:
    """get_presigned_put_for_upload should return a non-empty string URL keyed on upload_id."""
    with mock_aws():
        a, b, c, d, e = _moto_settings()
        with a, b, c, d, e:
            backend = _mocked_backend()
            url = await backend.get_presigned_put_for_upload("some-upload-id")

    assert isinstance(url, str)
    assert "uploads/some-upload-id" in url


async def test_promote_upload_copies_to_blob_key() -> None:
    """promote_upload should server-side copy uploads/{id} → blobs/{hash} and
    return (size, media_type, storage_key) without reading bytes through us."""
    payload = b"a tiny fake video"
    blob_hash = StorageBackend._sha256(payload)
    expected_key = StorageBackend._bucket_key(blob_hash)

    with mock_aws():
        a, b, c, d, e = _moto_settings()
        with a, b, c, d, e:
            backend = _mocked_backend()
            # Simulate the client's direct PUT to the transient uploads key.
            backend._client.put_object(
                Bucket=settings.S3_BUCKET,
                Key="uploads/ds-1",
                Body=payload,
                ContentType="video/mp4",
            )

            size_bytes, media_type, storage_key = await backend.promote_upload("ds-1", blob_hash)

            assert size_bytes == len(payload)
            assert media_type == "video/mp4"
            assert storage_key == expected_key
            # Bytes now live at the content-addressed key.
            assert await backend.get_bytes(blob_hash) == payload


async def test_presigned_url_uses_public_endpoint() -> None:
    """Presigned URLs must be signed against S3_PUBLIC_ENDPOINT (browser-reachable),
    not the internal S3_ENDPOINT."""
    with mock_aws():
        a, b, c, d, e = _moto_settings()
        with a, b, c, d, e, patch.object(settings, "S3_PUBLIC_ENDPOINT", "http://public-host:3900"):
            backend = _mocked_backend()
            put_url = await backend.get_presigned_put_for_upload("ds-1")
            get_url = await backend.get_presigned_get("sha256:" + "a" * 64)

    assert "public-host:3900" in put_url
    assert "public-host:3900" in get_url


async def test_presigned_url_honors_per_request_endpoint() -> None:
    """A per-request endpoint (derived from the browser Host) overrides the
    default signing host, so dev-VM uploads target the right address."""
    with mock_aws():
        a, b, c, d, e = _moto_settings()
        with a, b, c, d, e:  # S3_PUBLIC_ENDPOINT stays default (empty)
            backend = _mocked_backend()
            url = await backend.get_presigned_put_for_upload(
                "ds-1", endpoint="http://dev-vm.example:3900"
            )

    assert "dev-vm.example:3900" in url


def test_public_s3_endpoint_helper() -> None:
    from cvops_api.core.storage import public_s3_endpoint

    with (
        patch.object(settings, "S3_PUBLIC_ENDPOINT", ""),
        patch.object(settings, "S3_PUBLIC_PORT", 3900),
    ):
        assert public_s3_endpoint("dev-vm") == "http://dev-vm:3900"
        assert public_s3_endpoint(None) is None
    with patch.object(settings, "S3_PUBLIC_ENDPOINT", "http://fixed:9000"):
        assert public_s3_endpoint("dev-vm") == "http://fixed:9000"  # override wins


async def test_ensure_cors_sets_bucket_rule() -> None:
    """Instantiating the backend should install a permissive CORS rule allowing
    cross-origin PUT (for direct browser uploads)."""
    with mock_aws():
        a, b, c, d, e = _moto_settings()
        with a, b, c, d, e:
            backend = _mocked_backend()
            cors = backend._client.get_bucket_cors(Bucket=settings.S3_BUCKET)

    rule = cors["CORSRules"][0]
    assert "PUT" in rule["AllowedMethods"]
    assert rule["AllowedOrigins"] == ["*"]


async def test_promote_upload_idempotent() -> None:
    """A second promote_upload for an already-registered blob must not error."""
    payload = b"dup content"
    blob_hash = StorageBackend._sha256(payload)

    with mock_aws():
        a, b, c, d, e = _moto_settings()
        with a, b, c, d, e:
            backend = _mocked_backend()
            backend._client.put_object(
                Bucket=settings.S3_BUCKET,
                Key="uploads/ds-2",
                Body=payload,
                ContentType="application/octet-stream",
            )
            first = await backend.promote_upload("ds-2", blob_hash)
            second = await backend.promote_upload("ds-2", blob_hash)

    assert first == second
