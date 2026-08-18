"""Owner-scoped source-object storage adapters for hosted mode (LIT-43)."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError
from botocore.config import Config
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.hosted.tenant.models import OwnerId

EPUB_MEDIA_TYPE = "application/epub+zip"
_FILESYSTEM_MAGIC = b"LITLET-EPUB-AESGCM-1\n"
_MAX_HEADER_BYTES = 2048


class ObjectStorageError(RuntimeError):
    """Base class whose message is safe to surface without provider/path details."""


class ObjectPolicyError(ObjectStorageError):
    pass


class ObjectIntegrityError(ObjectStorageError):
    pass


class ObjectNotFoundError(ObjectStorageError):
    pass


class ObjectStorageUnavailableError(ObjectStorageError):
    pass


class ObjectConflictError(ObjectStorageError):
    pass


class StorageConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SourceObjectRef:
    """Server-only object identity; physical paths always derive owner scope from this value."""

    owner_id: OwnerId
    object_id: uuid.UUID

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, OwnerId):
            raise TypeError("owner_id must be an OwnerId")
        if not isinstance(self.owner_id.value, uuid.UUID):
            raise TypeError("owner_id value must be a UUID")
        if not isinstance(self.object_id, uuid.UUID):
            raise TypeError("object_id must be a UUID")


def new_source_ref(owner_id: OwnerId) -> SourceObjectRef:
    if not isinstance(owner_id, OwnerId):
        raise TypeError("owner_id must be an OwnerId")
    return SourceObjectRef(owner_id, uuid.uuid4())


@dataclass(frozen=True, slots=True)
class StorageReceipt:
    ref: SourceObjectRef
    provider: str
    media_type: str
    byte_size: int
    sha256: str
    encryption: str


@dataclass(frozen=True, slots=True)
class StoredObject:
    ref: SourceObjectRef
    media_type: str
    byte_size: int
    sha256: str
    data: bytes


def _validate_upload(
    data: bytes, *, media_type: str, expected_sha256: str, max_object_bytes: int
) -> str:
    if media_type != EPUB_MEDIA_TYPE:
        raise ObjectPolicyError("source object must use the EPUB media type")
    if not data:
        raise ObjectPolicyError("source object cannot be empty")
    if len(data) > max_object_bytes:
        raise ObjectPolicyError("source object exceeds the configured size limit")
    if (
        not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(char not in "0123456789abcdef" for char in expected_sha256)
    ):
        raise ObjectIntegrityError("source object checksum is invalid")
    actual = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(actual, expected_sha256):
        raise ObjectIntegrityError("source object checksum does not match its bytes")
    return actual


class EncryptedFilesystemStorage:
    """Filesystem adapter with owner-separated paths and AES-256-GCM encryption at rest."""

    provider = "filesystem"
    encryption = "AES-256-GCM"

    def __init__(self, *, root: str | Path, encryption_key: bytes, max_object_bytes: int) -> None:
        if len(encryption_key) != 32:
            raise ValueError("filesystem object encryption key must be exactly 32 bytes")
        if max_object_bytes < 1:
            raise ValueError("object size limit must be positive")
        self._root = Path(root).resolve()
        self._key = bytes(encryption_key)
        self._max_object_bytes = max_object_bytes
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._root, 0o700)

    def _path(self, ref: SourceObjectRef) -> Path:
        if not isinstance(ref, SourceObjectRef):
            raise TypeError("object reference must be a SourceObjectRef")
        owner_dir = self._root / ref.owner_id.value.hex
        return owner_dir / f"{ref.object_id.hex}.epub.enc"

    def put(
        self,
        ref: SourceObjectRef,
        data: bytes,
        *,
        media_type: str,
        expected_sha256: str,
    ) -> StorageReceipt:
        if not isinstance(ref, SourceObjectRef):
            raise TypeError("object reference must be a SourceObjectRef")
        checksum = _validate_upload(
            data,
            media_type=media_type,
            expected_sha256=expected_sha256,
            max_object_bytes=self._max_object_bytes,
        )
        nonce = os.urandom(12)
        header = json.dumps(
            {
                "media_type": media_type,
                "byte_size": len(data),
                "sha256": checksum,
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "encryption": self.encryption,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        ciphertext = AESGCM(self._key).encrypt(nonce, data, header)
        payload = _FILESYSTEM_MAGIC + len(header).to_bytes(4, "big") + header + ciphertext

        target = self._path(ref)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(target.parent, 0o700)
        if target.exists():
            raise ObjectConflictError("source object already exists")
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError as exc:
                raise ObjectConflictError("source object already exists") from exc
        except OSError as exc:
            raise ObjectStorageUnavailableError("filesystem object write failed") from exc
        finally:
            temporary.unlink(missing_ok=True)
        return StorageReceipt(
            ref=ref,
            provider=self.provider,
            media_type=media_type,
            byte_size=len(data),
            sha256=checksum,
            encryption=self.encryption,
        )

    def get(self, ref: SourceObjectRef) -> StoredObject:
        path = self._path(ref)
        try:
            envelope_size = path.stat().st_size
            maximum_envelope_size = (
                len(_FILESYSTEM_MAGIC) + 4 + _MAX_HEADER_BYTES + self._max_object_bytes + 16
            )
            if envelope_size < len(_FILESYSTEM_MAGIC) + 4 + 16:
                raise ObjectIntegrityError("source object envelope is truncated")
            if envelope_size > maximum_envelope_size:
                raise ObjectIntegrityError("source object envelope exceeds the configured size limit")
            payload = path.read_bytes()
        except FileNotFoundError as exc:
            raise ObjectNotFoundError("source object does not exist") from exc
        except ObjectIntegrityError:
            raise
        except OSError as exc:
            raise ObjectStorageUnavailableError("filesystem object read failed") from exc
        try:
            if not payload.startswith(_FILESYSTEM_MAGIC):
                raise ObjectIntegrityError("source object envelope is invalid")
            offset = len(_FILESYSTEM_MAGIC)
            header_size = int.from_bytes(payload[offset : offset + 4], "big")
            if header_size < 1 or header_size > _MAX_HEADER_BYTES:
                raise ObjectIntegrityError("source object envelope is invalid")
            header_start = offset + 4
            header_end = header_start + header_size
            header = payload[header_start:header_end]
            metadata = json.loads(header.decode("ascii"))
            nonce = base64.b64decode(metadata["nonce"], validate=True)
            if len(nonce) != 12 or metadata.get("encryption") != self.encryption:
                raise ObjectIntegrityError("source object encryption metadata is invalid")
            data = AESGCM(self._key).decrypt(nonce, payload[header_end:], header)
        except ObjectIntegrityError:
            raise
        except (
            InvalidTag,
            AttributeError,
            KeyError,
            ValueError,
            UnicodeError,
            json.JSONDecodeError,
        ) as exc:
            raise ObjectIntegrityError("source object authentication failed") from exc

        media_type = metadata.get("media_type")
        byte_size = metadata.get("byte_size")
        checksum = metadata.get("sha256")
        if media_type != EPUB_MEDIA_TYPE or byte_size != len(data):
            raise ObjectIntegrityError("source object metadata does not match its bytes")
        if not isinstance(byte_size, int) or byte_size < 1 or byte_size > self._max_object_bytes:
            raise ObjectIntegrityError("source object size is outside policy")
        actual = hashlib.sha256(data).hexdigest()
        if not isinstance(checksum, str) or not hmac.compare_digest(actual, checksum):
            raise ObjectIntegrityError("source object checksum does not match its bytes")
        return StoredObject(ref, media_type, byte_size, checksum, data)

    def exists(self, ref: SourceObjectRef) -> bool:
        return self._path(ref).is_file()

    def delete(self, ref: SourceObjectRef) -> None:
        path = self._path(ref)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise ObjectStorageUnavailableError("filesystem object deletion failed") from exc

    def close(self) -> None:
        return None


class S3ObjectStorage:
    """S3-compatible adapter requiring owner-derived keys, SHA-256, and server-side encryption."""

    provider = "s3"

    def __init__(
        self,
        *,
        client: Any,
        bucket: str,
        max_object_bytes: int,
        sse_algorithm: str,
        sse_kms_key_id: str | None = None,
        expected_bucket_owner: str | None = None,
    ) -> None:
        if not bucket:
            raise ValueError("S3 bucket is required")
        if max_object_bytes < 1:
            raise ValueError("object size limit must be positive")
        if sse_algorithm not in {"AES256", "aws:kms"}:
            raise ValueError("S3 server-side encryption must be AES256 or aws:kms")
        if sse_algorithm == "aws:kms" and not sse_kms_key_id:
            raise ValueError("S3 KMS encryption requires a key id")
        if sse_algorithm == "AES256" and sse_kms_key_id:
            raise ValueError("S3 AES256 encryption cannot use a KMS key id")
        self._client = client
        self._bucket = bucket
        self._max_object_bytes = max_object_bytes
        self._sse_algorithm = sse_algorithm
        self._sse_kms_key_id = sse_kms_key_id
        self._expected_bucket_owner = expected_bucket_owner

    @property
    def encryption(self) -> str:
        return self._sse_algorithm

    @staticmethod
    def _key(ref: SourceObjectRef) -> str:
        if not isinstance(ref, SourceObjectRef):
            raise TypeError("object reference must be a SourceObjectRef")
        return f"owners/{ref.owner_id.value.hex}/epubs/{ref.object_id.hex}.epub"

    def _base(self, ref: SourceObjectRef) -> dict[str, Any]:
        values: dict[str, Any] = {"Bucket": self._bucket, "Key": self._key(ref)}
        if self._expected_bucket_owner:
            values["ExpectedBucketOwner"] = self._expected_bucket_owner
        return values

    @staticmethod
    def _missing(exc: ClientError) -> bool:
        return str(exc.response.get("Error", {}).get("Code", "")) in {
            "404",
            "NoSuchKey",
            "NotFound",
        }

    def _validate_response_metadata(self, response: dict[str, Any]) -> tuple[int, str]:
        if response.get("ContentType") != EPUB_MEDIA_TYPE:
            raise ObjectIntegrityError("stored source object has an invalid media type")
        if response.get("ServerSideEncryption") != self._sse_algorithm:
            raise ObjectIntegrityError("stored source object is not encrypted as configured")
        if self._sse_kms_key_id and response.get("SSEKMSKeyId") != self._sse_kms_key_id:
            raise ObjectIntegrityError("stored source object uses a different KMS key")
        byte_size = response.get("ContentLength")
        if not isinstance(byte_size, int) or byte_size < 1 or byte_size > self._max_object_bytes:
            raise ObjectIntegrityError("stored source object size is outside policy")
        checksum = response.get("Metadata", {}).get("sha256")
        if (
            not isinstance(checksum, str)
            or len(checksum) != 64
            or any(char not in "0123456789abcdef" for char in checksum)
        ):
            raise ObjectIntegrityError("stored source object checksum metadata is invalid")
        return byte_size, checksum

    def put(
        self,
        ref: SourceObjectRef,
        data: bytes,
        *,
        media_type: str,
        expected_sha256: str,
    ) -> StorageReceipt:
        checksum = _validate_upload(
            data,
            media_type=media_type,
            expected_sha256=expected_sha256,
            max_object_bytes=self._max_object_bytes,
        )
        digest = hashlib.sha256(data).digest()
        values = {
            **self._base(ref),
            "Body": data,
            "ContentLength": len(data),
            "ContentType": media_type,
            "ContentMD5": base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode(
                "ascii"
            ),
            "ChecksumSHA256": base64.b64encode(digest).decode("ascii"),
            "Metadata": {"sha256": checksum},
            "ServerSideEncryption": self._sse_algorithm,
            "IfNoneMatch": "*",
        }
        if self._sse_kms_key_id:
            values["SSEKMSKeyId"] = self._sse_kms_key_id
        try:
            response = self._client.put_object(**values)
            if response.get("ServerSideEncryption") != self._sse_algorithm or (
                self._sse_kms_key_id
                and response.get("SSEKMSKeyId") != self._sse_kms_key_id
            ):
                self._client.delete_object(**self._base(ref))
                raise ObjectIntegrityError("object store did not confirm server-side encryption")
            returned_checksum = response.get("ChecksumSHA256")
            if returned_checksum and returned_checksum != values["ChecksumSHA256"]:
                self._client.delete_object(**self._base(ref))
                raise ObjectIntegrityError("object store returned a different checksum")
        except ObjectIntegrityError:
            raise
        except ClientError as exc:
            raise ObjectStorageUnavailableError("S3 object write failed") from exc
        return StorageReceipt(
            ref=ref,
            provider=self.provider,
            media_type=media_type,
            byte_size=len(data),
            sha256=checksum,
            encryption=self.encryption,
        )

    def get(self, ref: SourceObjectRef) -> StoredObject:
        values = {**self._base(ref), "ChecksumMode": "ENABLED"}
        try:
            response = self._client.get_object(**values)
        except ClientError as exc:
            if self._missing(exc):
                raise ObjectNotFoundError("source object does not exist") from exc
            raise ObjectStorageUnavailableError("S3 object read failed") from exc
        body = response.get("Body")
        try:
            data = body.read(self._max_object_bytes + 1)
        except Exception as exc:
            raise ObjectStorageUnavailableError("S3 object body read failed") from exc
        finally:
            if body is not None:
                body.close()
        byte_size, checksum = self._validate_response_metadata(response)
        if len(data) != byte_size:
            raise ObjectIntegrityError("stored source object length does not match metadata")
        digest = hashlib.sha256(data).digest()
        actual = digest.hex()
        if not hmac.compare_digest(actual, checksum):
            raise ObjectIntegrityError("stored source object checksum does not match its bytes")
        encoded = response.get("ChecksumSHA256")
        if encoded and not hmac.compare_digest(encoded, base64.b64encode(digest).decode("ascii")):
            raise ObjectIntegrityError("object-store checksum does not match source bytes")
        return StoredObject(ref, EPUB_MEDIA_TYPE, byte_size, checksum, data)

    def exists(self, ref: SourceObjectRef) -> bool:
        try:
            response = self._client.head_object(**self._base(ref))
        except ClientError as exc:
            if self._missing(exc):
                return False
            raise ObjectStorageUnavailableError("S3 object existence check failed") from exc
        self._validate_response_metadata(response)
        return True

    def delete(self, ref: SourceObjectRef) -> None:
        try:
            self._client.delete_object(**self._base(ref))
        except ClientError as exc:
            raise ObjectStorageUnavailableError("S3 object deletion failed") from exc

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            close()


def build_object_storage(settings) -> EncryptedFilesystemStorage | S3ObjectStorage:
    """Build hosted storage from validated settings without logging secret values."""
    if settings.hosted_storage_backend == "filesystem":
        try:
            key = base64.b64decode(
                settings.hosted_storage_filesystem_key.get_secret_value(), validate=True
            )
        except (binascii.Error, ValueError, AttributeError) as exc:
            raise StorageConfigurationError(
                "HOSTED_STORAGE_FILESYSTEM_KEY must be base64-encoded"
            ) from exc
        if len(key) != 32:
            raise StorageConfigurationError(
                "HOSTED_STORAGE_FILESYSTEM_KEY must decode to exactly 32 bytes"
            )
        return EncryptedFilesystemStorage(
            root=settings.hosted_storage_filesystem_root,
            encryption_key=key,
            max_object_bytes=settings.epub_max_upload_bytes,
        )
    if settings.hosted_storage_backend == "s3":
        client_kwargs: dict[str, Any] = {
            "region_name": settings.hosted_s3_region,
            "config": Config(
                signature_version="s3v4",
                s3={"addressing_style": "path" if settings.hosted_s3_endpoint_url else "auto"},
            ),
        }
        if settings.hosted_s3_endpoint_url:
            client_kwargs["endpoint_url"] = settings.hosted_s3_endpoint_url
        if settings.hosted_s3_access_key_id is not None:
            client_kwargs["aws_access_key_id"] = (
                settings.hosted_s3_access_key_id.get_secret_value()
            )
            client_kwargs["aws_secret_access_key"] = (
                settings.hosted_s3_secret_access_key.get_secret_value()
            )
        client = boto3.client("s3", **client_kwargs)
        return S3ObjectStorage(
            client=client,
            bucket=settings.hosted_s3_bucket,
            max_object_bytes=settings.epub_max_upload_bytes,
            sse_algorithm=settings.hosted_s3_sse_algorithm,
            sse_kms_key_id=settings.hosted_s3_kms_key_id,
            expected_bucket_owner=settings.hosted_s3_expected_bucket_owner,
        )
    raise StorageConfigurationError(
        "hosted mode requires HOSTED_STORAGE_BACKEND=filesystem or s3"
    )
