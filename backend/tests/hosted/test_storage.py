"""Owner-scoped filesystem/S3 source-object storage contract (LIT-43)."""

from __future__ import annotations

import base64
import hashlib
import io
import uuid

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.stub import Stubber

from app.hosted.storage import (
    EPUB_MEDIA_TYPE,
    EncryptedFilesystemStorage,
    ObjectIntegrityError,
    ObjectNotFoundError,
    ObjectPolicyError,
    S3ObjectStorage,
    SourceObjectRef,
    StorageConfigurationError,
    build_object_storage,
    new_source_ref,
)
from app.config import Settings
from app.hosted.tenant.models import OwnerId


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_encrypted_filesystem_contract_is_owner_scoped_bounded_and_authenticated(tmp_path) -> None:
    owner_a = OwnerId(uuid.uuid4())
    owner_b = OwnerId(uuid.uuid4())
    ref = new_source_ref(owner_a)
    payload = b"PK\x03\x04private epub bytes"
    storage = EncryptedFilesystemStorage(
        root=tmp_path / "objects",
        encryption_key=b"k" * 32,
        max_object_bytes=len(payload),
    )

    receipt = storage.put(
        ref,
        payload,
        media_type=EPUB_MEDIA_TYPE,
        expected_sha256=_sha256(payload),
    )
    assert receipt.ref == ref
    assert receipt.provider == "filesystem"
    assert receipt.byte_size == len(payload)
    assert receipt.sha256 == _sha256(payload)
    assert receipt.encryption == "AES-256-GCM"
    assert storage.exists(ref)

    stored_files = [path for path in (tmp_path / "objects").rglob("*") if path.is_file()]
    assert len(stored_files) == 1
    assert payload not in stored_files[0].read_bytes()
    assert stored_files[0].stat().st_mode & 0o077 == 0

    downloaded = storage.get(ref)
    assert downloaded.data == payload
    assert downloaded.media_type == EPUB_MEDIA_TYPE
    assert downloaded.sha256 == _sha256(payload)

    stolen = SourceObjectRef(owner_b, ref.object_id)
    assert not storage.exists(stolen)
    with pytest.raises(ObjectNotFoundError):
        storage.get(stolen)
    storage.delete(stolen)
    assert storage.exists(ref)
    assert storage.get(ref).data == payload
    with pytest.raises(ObjectPolicyError):
        storage.put(
            new_source_ref(owner_a),
            payload + b"x",
            media_type=EPUB_MEDIA_TYPE,
            expected_sha256=_sha256(payload + b"x"),
        )
    with pytest.raises(ObjectPolicyError):
        storage.put(
            new_source_ref(owner_a),
            payload,
            media_type="application/octet-stream",
            expected_sha256=_sha256(payload),
        )
    with pytest.raises(ObjectIntegrityError):
        storage.put(
            new_source_ref(owner_a),
            payload,
            media_type=EPUB_MEDIA_TYPE,
            expected_sha256="0" * 64,
        )

    raw = bytearray(stored_files[0].read_bytes())
    raw[-1] ^= 1
    stored_files[0].write_bytes(raw)
    with pytest.raises(ObjectIntegrityError):
        storage.get(ref)

    storage.delete(ref)
    assert not storage.exists(ref)
    storage.delete(ref)  # deletion is idempotent for retry-safe lifecycle cleanup


class _FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict] = {}
        self.last_put: dict | None = None

    @staticmethod
    def _missing(operation: str) -> ClientError:
        return ClientError({"Error": {"Code": "NoSuchKey", "Message": "missing"}}, operation)

    def put_object(self, **kwargs):
        self.last_put = kwargs
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = dict(kwargs)
        response = {
            "ServerSideEncryption": kwargs["ServerSideEncryption"],
            "ChecksumSHA256": kwargs["ChecksumSHA256"],
        }
        if "SSEKMSKeyId" in kwargs:
            response["SSEKMSKeyId"] = kwargs["SSEKMSKeyId"]
        return response

    def get_object(self, **kwargs):
        item = self.objects.get((kwargs["Bucket"], kwargs["Key"]))
        if item is None:
            raise self._missing("GetObject")
        body = item["Body"]
        return {
            "Body": io.BytesIO(body),
            "ContentLength": len(body),
            "ContentType": item["ContentType"],
            "Metadata": item["Metadata"],
            "ChecksumSHA256": item["ChecksumSHA256"],
            "ServerSideEncryption": item["ServerSideEncryption"],
            **({"SSEKMSKeyId": item["SSEKMSKeyId"]} if "SSEKMSKeyId" in item else {}),
        }

    def head_object(self, **kwargs):
        item = self.objects.get((kwargs["Bucket"], kwargs["Key"]))
        if item is None:
            raise self._missing("HeadObject")
        return {
            "ContentLength": len(item["Body"]),
            "ContentType": item["ContentType"],
            "Metadata": item["Metadata"],
            "ServerSideEncryption": item["ServerSideEncryption"],
            **({"SSEKMSKeyId": item["SSEKMSKeyId"]} if "SSEKMSKeyId" in item else {}),
        }

    def delete_object(self, **kwargs):
        self.objects.pop((kwargs["Bucket"], kwargs["Key"]), None)
        return {}

    def close(self) -> None:
        return None


def test_s3_contract_derives_owner_path_and_enforces_checksum_content_type_and_sse() -> None:
    client = _FakeS3Client()
    storage = S3ObjectStorage(
        client=client,
        bucket="private-books",
        max_object_bytes=1024,
        sse_algorithm="AES256",
    )
    owner_a = OwnerId(uuid.uuid4())
    owner_b = OwnerId(uuid.uuid4())
    ref = new_source_ref(owner_a)
    payload = b"PK\x03\x04s3 private epub"

    receipt = storage.put(
        ref,
        payload,
        media_type=EPUB_MEDIA_TYPE,
        expected_sha256=_sha256(payload),
    )
    assert receipt.provider == "s3"
    assert receipt.encryption == "AES256"
    assert client.last_put is not None
    assert client.last_put["Key"] == (
        f"owners/{owner_a.value.hex}/epubs/{ref.object_id.hex}.epub"
    )
    assert client.last_put["ContentType"] == EPUB_MEDIA_TYPE
    assert client.last_put["ServerSideEncryption"] == "AES256"
    assert client.last_put["IfNoneMatch"] == "*"
    assert client.last_put["ChecksumSHA256"] == base64.b64encode(
        hashlib.sha256(payload).digest()
    ).decode("ascii")
    assert client.last_put["Metadata"] == {"sha256": _sha256(payload)}

    assert storage.exists(ref)
    assert storage.get(ref).data == payload
    stolen = SourceObjectRef(owner_b, ref.object_id)
    assert not storage.exists(stolen)
    with pytest.raises(ObjectNotFoundError):
        storage.get(stolen)
    storage.delete(stolen)
    assert storage.exists(ref)
    assert storage.get(ref).data == payload

    key = ("private-books", client.last_put["Key"])
    client.objects[key]["Body"] = payload + b"tampered"
    with pytest.raises(ObjectIntegrityError):
        storage.get(ref)

    storage.delete(ref)
    assert not storage.exists(ref)


def test_s3_kms_mode_requires_and_verifies_the_exact_configured_key() -> None:
    client = _FakeS3Client()
    storage = S3ObjectStorage(
        client=client,
        bucket="private-books",
        max_object_bytes=1024,
        sse_algorithm="aws:kms",
        sse_kms_key_id="alias/litlet-source-objects",
    )
    ref = new_source_ref(OwnerId(uuid.uuid4()))
    payload = b"PK\x03\x04kms encrypted epub"
    storage.put(
        ref,
        payload,
        media_type=EPUB_MEDIA_TYPE,
        expected_sha256=_sha256(payload),
    )
    assert client.last_put["SSEKMSKeyId"] == "alias/litlet-source-objects"
    key = ("private-books", client.last_put["Key"])
    client.objects[key]["SSEKMSKeyId"] = "alias/unexpected-key"
    with pytest.raises(ObjectIntegrityError, match="different KMS key"):
        storage.get(ref)


def test_s3_requests_match_the_real_botocore_service_model() -> None:
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="test-access-id",
        aws_secret_access_key="test-secret-key",
    )
    storage = S3ObjectStorage(
        client=client,
        bucket="private-books",
        max_object_bytes=1024,
        sse_algorithm="AES256",
    )
    ref = new_source_ref(OwnerId(uuid.uuid4()))
    payload = b"PK\x03\x04botocore contract epub"
    checksum = _sha256(payload)
    checksum_b64 = base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")
    content_md5 = base64.b64encode(
        hashlib.md5(payload, usedforsecurity=False).digest()
    ).decode("ascii")
    key = f"owners/{ref.owner_id.value.hex}/epubs/{ref.object_id.hex}.epub"

    with Stubber(client) as stubber:
        stubber.add_response(
            "put_object",
            {"ServerSideEncryption": "AES256", "ChecksumSHA256": checksum_b64},
            {
                "Bucket": "private-books",
                "Key": key,
                "Body": payload,
                "ContentLength": len(payload),
                "ContentType": EPUB_MEDIA_TYPE,
                "ContentMD5": content_md5,
                "ChecksumSHA256": checksum_b64,
                "Metadata": {"sha256": checksum},
                "ServerSideEncryption": "AES256",
                "IfNoneMatch": "*",
            },
        )
        storage.put(
            ref,
            payload,
            media_type=EPUB_MEDIA_TYPE,
            expected_sha256=checksum,
        )
        stubber.add_response(
            "get_object",
            {
                "Body": StreamingBody(io.BytesIO(payload), len(payload)),
                "ContentLength": len(payload),
                "ContentType": EPUB_MEDIA_TYPE,
                "Metadata": {"sha256": checksum},
                "ChecksumSHA256": checksum_b64,
                "ServerSideEncryption": "AES256",
            },
            {"Bucket": "private-books", "Key": key, "ChecksumMode": "ENABLED"},
        )
        assert storage.get(ref).data == payload


def test_source_object_reference_rejects_untyped_or_malformed_identity() -> None:
    owner = OwnerId(uuid.uuid4())
    with pytest.raises(TypeError):
        SourceObjectRef(owner.value, uuid.uuid4())
    with pytest.raises(TypeError):
        SourceObjectRef(owner, "client-key")


def _hosted_settings(**overrides) -> Settings:
    values = {
        "deployment_mode": "hosted",
        "hosted_auth_dsn": "postgresql://unused.invalid/litlet",
        "hosted_tenant_dsn": "postgresql://unused.invalid/litlet",
        "oidc_issuer": "https://idp.example",
        "oidc_client_id": "litlet",
        "oidc_client_secret": "not-a-real-secret",
        "oidc_redirect_uri": "https://reader.example/api/auth/callback",
        "hosted_credential_master_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "hosted_credential_key_version": "test-v1",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_storage_configuration_is_fail_loud_and_keeps_keys_secret(tmp_path) -> None:
    with pytest.raises(StorageConfigurationError, match="requires HOSTED_STORAGE_BACKEND"):
        build_object_storage(_hosted_settings())
    invalid = _hosted_settings(
        hosted_storage_backend="filesystem",
        hosted_storage_filesystem_root=str(tmp_path),
        hosted_storage_filesystem_key="not-base64",
    )
    with pytest.raises(StorageConfigurationError, match="base64"):
        build_object_storage(invalid)

    encoded_key = base64.b64encode(b"z" * 32).decode("ascii")
    settings = _hosted_settings(
        hosted_storage_backend="filesystem",
        hosted_storage_filesystem_root=str(tmp_path),
        hosted_storage_filesystem_key=encoded_key,
    )
    storage = build_object_storage(settings)
    assert isinstance(storage, EncryptedFilesystemStorage)
    assert encoded_key not in repr(settings)

    with pytest.raises(ValueError, match="allowed origin"):
        _hosted_settings(
            hosted_storage_backend="s3",
            hosted_s3_bucket="private-books",
            hosted_s3_endpoint_url="http://object-store.example",
        )
