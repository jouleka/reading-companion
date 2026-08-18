"""Session-derived hosted tenant API. Client-supplied owner identity is never accepted."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from app.ask import (
    ASK_SYSTEM,
    AskDraft,
    AskRequest,
    AskSafetyError,
    ask_prompt,
    cited_sources,
    draft_text,
    source_facts,
    validate_ask_draft,
)
from app.cost import CostCeilingExceeded, pricing_known
from app.eval.spoiler_gate.judge import JUDGE_SYSTEM, JudgeVerdict, judge_prompt
from app.hosted.auth.api import AuthenticatedSession, authenticated_session, csrf_valid
from app.hosted.credentials import normalize_provider
from app.hosted.credentials import CredentialUnavailableError
from app.hosted.limits import LimitExceededError
from app.hosted.provider_settings import (
    ProviderSettingPolicyError,
    ValidationResult,
    default_settings_payload,
    validate_setting_policy,
)
from app.hosted.provider_runtime import (
    budgeted_hosted_completion,
    close_completion_client,
    completion_client,
)
from app.hosted.runtime import CacheNamespace, ResourceKind, TenantCacheKey, TenantResourceKey
from app.hosted.storage import (
    EPUB_MEDIA_TYPE,
    ObjectConflictError,
    ObjectIntegrityError,
    ObjectNotFoundError,
    ObjectPolicyError,
    ObjectStorageUnavailableError,
    SourceObjectRef,
    new_source_ref,
)
from app.hosted.tenant.models import (
    FuturePositionVersionError,
    InvalidPositionError,
    MissingTenantResourceError,
    OwnerId,
    StalePositionEpochError,
)
from app.ingest.book_type import detect_book_type
from app.ingest.extraction.chapter_text import segment_for_ingest
from app.ingest.segmentation.epub_segmenter import EpubDrmError
from app.reading_assist import (
    CLOSEOUT_SYSTEM,
    SELECTION_SYSTEM,
    ChapterCloseoutRequest,
    SelectionActionRequest,
    SelectionDraft,
    chapter_closeout_prompt,
    chapter_passages,
    selection_prompt,
)

router = APIRouter(prefix="/api", tags=["hosted-library"])
_LOG = logging.getLogger(__name__)


class PositionResetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    position_epoch: StrictInt = Field(ge=0, le=2**63 - 1)


class PositionUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cfi: str = Field(min_length=1, max_length=4096)
    offset: StrictInt = Field(ge=0, le=2**63 - 1)
    completed_chapter: StrictInt = Field(ge=0, le=2**31 - 1)
    position_epoch: StrictInt = Field(ge=0, le=2**63 - 1)
    base_version: StrictInt = Field(ge=0, le=2**63 - 1)
    client_id: uuid.UUID
    client_sequence: StrictInt = Field(ge=1, le=2**63 - 1)


class ReaderPreferencesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    font_size: Literal["small", "book", "large", "x-large"]
    line_height: Literal["compact", "comfortable", "relaxed"]
    measure: Literal["narrow", "balanced", "wide"]
    theme: Literal["paper", "sepia", "night", "system"]
    margins: Literal["compact", "balanced", "generous"]
    typeface: Literal["publisher", "serif", "sans"]


class TextQuoteAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exact: str = Field(min_length=1, max_length=2000)
    prefix: str = Field(default="", max_length=200)
    suffix: str = Field(default="", max_length=200)


class ReaderMarkAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cfi: str = Field(min_length=1, max_length=4096, pattern=r"^epubcfi\(.+\)$")
    atom: StrictInt = Field(ge=1, le=2**31 - 1)
    quote: TextQuoteAnchor | None = None


class HighlightIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    anchor: ReaderMarkAnchor
    color: Literal["yellow", "green", "blue", "pink"] = "yellow"
    selected_text: str = Field(min_length=1, max_length=2000)

    @field_validator("selected_text")
    @classmethod
    def selected_text_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("selected text is blank")
        return value


class AnnotationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    anchor: ReaderMarkAnchor
    body: str = Field(min_length=1, max_length=10000)
    highlight_id: uuid.UUID | None = None

    @field_validator("body")
    @classmethod
    def body_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("annotation is blank")
        return value


class BookmarkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    anchor: ReaderMarkAnchor
    label: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("label")
    @classmethod
    def label_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("bookmark label is blank")
        return value


class HighlightUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    color: Literal["yellow", "green", "blue", "pink"]


class AnnotationUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=10000)

    @field_validator("body")
    @classmethod
    def body_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("annotation is blank")
        return value


class BookmarkUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=500)

    @field_validator("label")
    @classmethod
    def label_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("bookmark label is blank")
        return value


class HostedEntityCorrectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_entity_id: uuid.UUID
    canonical_name: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1000)
    bookmark: StrictInt = Field(ge=1, le=2**31 - 1)

    @field_validator("canonical_name", "reason")
    @classmethod
    def correction_text_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value


class ProviderSettingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["openai-compatible", "anthropic", "offline"]
    model: str = Field(min_length=1, max_length=128)
    credential_id: uuid.UUID | None = None
    base_url: str | None = Field(default=None, max_length=512)


async def _session(request: Request) -> AuthenticatedSession:
    value = await authenticated_session(request)
    if value is None:
        raise HTTPException(401, "Authentication required")
    return value


async def _owner_input_guard(request: Request) -> None:
    forbidden = {
        "owner",
        "ownerid",
        "userid",
        "credentialid",
        "objectid",
        "objectkey",
        "sourceobjectid",
        "storagekey",
    }
    for key in request.query_params:
        normalized = key.casefold().replace("_", "").replace("-", "")
        if normalized in forbidden:
            raise HTTPException(422, "storage and owner identity are server-derived")


async def _owner(
    request: Request,
    session: AuthenticatedSession = Depends(_session),
    _guard: None = Depends(_owner_input_guard),
) -> OwnerId:
    owner_id = OwnerId(session.principal.owner_id)
    try:
        await request.app.state.tenant_repository.consume_request_limit(owner_id)
    except LimitExceededError as exc:
        raise _limit_failure(exc) from exc
    return owner_id


async def _csrf_owner(
    request: Request,
    session: AuthenticatedSession = Depends(_session),
    _guard: None = Depends(_owner_input_guard),
) -> OwnerId:
    if not csrf_valid(request, session):
        raise HTTPException(403, "CSRF validation failed")
    owner_id = OwnerId(session.principal.owner_id)
    try:
        await request.app.state.tenant_repository.consume_request_limit(owner_id)
    except LimitExceededError as exc:
        raise _limit_failure(exc) from exc
    return owner_id


def _private(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"


def _resource(owner_id: OwnerId, kind: ResourceKind, resource_id: uuid.UUID) -> TenantResourceKey:
    return TenantResourceKey(owner_id, kind, resource_id)


def _storage_failure(exc: Exception) -> HTTPException:
    if isinstance(exc, ObjectPolicyError):
        return HTTPException(422, str(exc))
    if isinstance(exc, ObjectNotFoundError):
        return HTTPException(404, "source EPUB not found")
    return HTTPException(503, "source object storage is unavailable")


def _limit_failure(exc: LimitExceededError) -> HTTPException:
    status = 429 if exc.retry_after_seconds is not None else (
        413 if exc.code == "upload_size_exceeded" else 409
    )
    headers = (
        {"Retry-After": str(exc.retry_after_seconds)}
        if exc.retry_after_seconds is not None
        else None
    )
    return HTTPException(
        status,
        {
            "code": exc.code,
            "limit": exc.limit,
            "retry_after_seconds": exc.retry_after_seconds,
            "action": exc.action,
        },
        headers=headers,
    )


async def _credential_input(request: Request, *, include_provider: bool) -> tuple[str | None, str]:
    """Parse secret-bearing JSON without ever reflecting invalid input through validation details."""
    media_type = request.headers.get("content-type", "").partition(";")[0].strip().casefold()
    if media_type != "application/json":
        raise HTTPException(415, "credential request must use application/json")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    try:
        payload = json.loads(await request.body(), object_pairs_hook=unique_object)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(422, "invalid credential request") from exc
    expected = {"provider", "secret"} if include_provider else {"secret"}
    if not isinstance(payload, dict) or set(payload) != expected:
        raise HTTPException(422, "invalid credential request")
    secret = payload.get("secret")
    provider = payload.get("provider") if include_provider else None
    if not isinstance(secret, str) or (include_provider and not isinstance(provider, str)):
        raise HTTPException(422, "invalid credential request")
    return provider, secret


@router.post("/credentials", status_code=201)
async def create_credential(
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    _private(response)
    provider, secret = await _credential_input(request, include_provider=True)
    assert provider is not None
    credential_id = uuid.uuid4()
    try:
        envelope = request.app.state.credential_cipher.encrypt(
            owner_id, credential_id, normalize_provider(provider), secret
        )
    except ValueError as exc:
        raise HTTPException(422, "invalid credential request") from exc
    key = _resource(owner_id, ResourceKind.CREDENTIAL, credential_id)
    async with request.app.state.hosted_runtime.serialized(key):
        return await request.app.state.tenant_repository.create_credential(owner_id, envelope)


@router.get("/credentials")
async def list_credentials(
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_owner),
):
    _private(response)
    key = _resource(owner_id, ResourceKind.LIBRARY, owner_id.value)
    async with request.app.state.hosted_runtime.serialized(key):
        return await request.app.state.tenant_repository.list_credentials(owner_id)


@router.put("/credentials/{credential_id}")
async def replace_credential(
    credential_id: uuid.UUID,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    _private(response)
    _provider, secret = await _credential_input(request, include_provider=False)
    key = _resource(owner_id, ResourceKind.CREDENTIAL, credential_id)
    async with request.app.state.hosted_runtime.serialized(key):
        current = await request.app.state.tenant_repository.get_credential(
            owner_id, credential_id
        )
        if current is None:
            raise HTTPException(404, "unknown credential")
        try:
            envelope = request.app.state.credential_cipher.encrypt(
                owner_id, credential_id, current["provider"], secret
            )
        except ValueError as exc:
            raise HTTPException(422, "invalid credential request") from exc
        result = await request.app.state.tenant_repository.replace_credential(owner_id, envelope)
        if result is None:
            raise HTTPException(404, "unknown credential")
        return result


@router.delete("/credentials/{credential_id}", status_code=204)
async def delete_credential(
    credential_id: uuid.UUID,
    request: Request,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    key = _resource(owner_id, ResourceKind.CREDENTIAL, credential_id)
    async with request.app.state.hosted_runtime.serialized(key):
        if not await request.app.state.tenant_repository.delete_credential(
            owner_id, credential_id
        ):
            raise HTTPException(404, "unknown credential")


@router.get("/provider-settings")
async def list_provider_settings(
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_owner),
):
    _private(response)
    key = _resource(owner_id, ResourceKind.PROVIDER_SETTINGS, owner_id.value)
    async with request.app.state.hosted_runtime.serialized(key):
        policy = default_settings_payload()
        policy["items"] = await request.app.state.tenant_repository.list_provider_settings(
            owner_id
        )
        return policy


@router.put("/provider-settings/{capability}")
async def put_provider_setting(
    capability: str,
    payload: ProviderSettingIn,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    _private(response)
    try:
        capability, provider, model, base_url = validate_setting_policy(
            capability=capability,
            provider=payload.provider,
            model=payload.model,
            credential_id=payload.credential_id,
            base_url=payload.base_url,
            origins=request.app.state.provider_origins,
        )
    except ProviderSettingPolicyError as exc:
        raise HTTPException(422, str(exc)) from exc
    key = _resource(owner_id, ResourceKind.PROVIDER_SETTINGS, owner_id.value)
    async with request.app.state.hosted_runtime.serialized(key):
        setting = await request.app.state.tenant_repository.upsert_provider_setting(
            owner_id,
            capability=capability,
            provider=provider,
            credential_id=payload.credential_id,
            model=model,
            base_url=base_url,
        )
        if setting is None:
            raise HTTPException(422, "selected credential is unavailable")
        return setting


@router.post("/provider-settings/{capability}/validate")
async def validate_provider_setting(
    capability: str,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    _private(response)
    key = _resource(owner_id, ResourceKind.PROVIDER_SETTINGS, owner_id.value)
    async with request.app.state.hosted_runtime.serialized(key):
        setting = await request.app.state.tenant_repository.get_provider_setting(
            owner_id, capability
        )
        if setting is None:
            raise HTTPException(404, "provider setting is not configured")
        if setting["provider"] == "offline":
            result = ValidationResult("offline", "offline")
        else:
            credential_id = uuid.UUID(setting["credential_id"])
            try:
                resolved = await request.app.state.tenant_repository.resolve_credential(
                    owner_id, credential_id, request.app.state.credential_cipher
                )
            except CredentialUnavailableError:
                result = ValidationResult("invalid", "invalid_credentials")
            else:
                with resolved:
                    result = await request.app.state.provider_validator.validate(
                        setting, resolved.get_secret_value()
                    )
        recorded = await request.app.state.tenant_repository.record_provider_validation(
            owner_id,
            setting_id=uuid.UUID(setting["id"]),
            expected_updated_at=setting["updated_at"],
            result=result,
        )
        if recorded is None:
            raise HTTPException(409, "provider setting changed during validation")
        return {"status": result.status, "code": result.code, "setting": recorded}


@router.post("/books", status_code=201)
async def upload_book(
    file: UploadFile,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    _private(response)
    form = await request.form()
    if set(form) != {"file"} or len(form.getlist("file")) != 1:
        raise HTTPException(422, "upload accepts exactly one server-addressed EPUB file")
    if file.content_type != EPUB_MEDIA_TYPE:
        raise HTTPException(415, "EPUB upload must use application/epub+zip")
    limit = request.app.state.settings.epub_max_upload_bytes
    blob = await file.read(limit + 1)
    await file.close()
    if len(blob) > limit:
        raise HTTPException(413, "EPUB upload exceeds the configured size limit")
    if not blob:
        raise HTTPException(422, "empty upload")
    book_id = uuid.uuid4()
    incarnation = uuid.uuid4()
    ref = new_source_ref(owner_id)
    try:
        result, chapters = await asyncio.to_thread(segment_for_ingest, blob, str(book_id))
    except EpubDrmError as exc:
        raise HTTPException(
            422, "DRM-protected EPUBs are not supported; import a DRM-free EPUB"
        ) from exc
    except Exception as exc:
        raise HTTPException(422, f"not a readable EPUB: {type(exc).__name__}") from exc
    if result.mode == "none" or not chapters:
        raise HTTPException(422, "no body chapters detected in this EPUB")

    profile = detect_book_type(result, chapters)
    title = result.title or (file.filename or "book.epub").rsplit(".", 1)[0]
    author = result.author
    checksum = hashlib.sha256(blob).hexdigest()
    char_start = 0
    search_documents = []
    for _atom, chapter in zip(result.atoms, chapters, strict=True):
        char_end = char_start + max(1, len(chapter["text"]))
        search_documents.append(
            {
                "ordinal": chapter["ordinal"],
                "href": chapter["href"],
                "title": chapter["title"],
                "part_label": chapter["part_label"],
                "content": chapter["text"],
                "char_start": char_start,
                "char_end": char_end,
            }
        )
        char_start = char_end
    library_key = _resource(owner_id, ResourceKind.LIBRARY, owner_id.value)
    async with request.app.state.hosted_runtime.serialized(library_key):
        try:
            receipt = await asyncio.to_thread(
                request.app.state.object_storage.put,
                ref,
                blob,
                media_type=EPUB_MEDIA_TYPE,
                expected_sha256=checksum,
            )
        except (
            ObjectConflictError,
            ObjectPolicyError,
            ObjectIntegrityError,
            ObjectStorageUnavailableError,
        ) as exc:
            raise _storage_failure(exc) from exc
        try:
            book = await request.app.state.tenant_repository.create_uploaded_book(
                owner_id,
                book_id=book_id,
                incarnation=incarnation,
                title=title,
                author=author,
                file_hash=checksum,
                content_language=result.content_language,
                book_type=profile.book_type,
                object_id=ref.object_id,
                storage_provider=receipt.provider,
                media_type=receipt.media_type,
                byte_size=receipt.byte_size,
                encryption=receipt.encryption,
                chapter_count=len(chapters),
                search_documents=search_documents,
            )
        except BaseException as exc:
            try:
                await asyncio.shield(
                    asyncio.to_thread(request.app.state.object_storage.delete, ref)
                )
            except Exception:
                _LOG.error("source object compensation failed after metadata transaction error")
            if isinstance(exc, LimitExceededError):
                raise _limit_failure(exc) from exc
            raise
    return {**book, "mode": result.mode, "atoms": len(chapters)}


@router.get("/limits")
async def get_limits(
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_owner),
):
    _private(response)
    return await request.app.state.tenant_repository.limit_status(owner_id)


@router.get("/books")
async def list_books(request: Request, response: Response, owner_id: OwnerId = Depends(_owner)):
    _private(response)
    key = _resource(owner_id, ResourceKind.LIBRARY, owner_id.value)
    async with request.app.state.hosted_runtime.serialized(key):
        return await request.app.state.tenant_repository.list_books(owner_id)


@router.get("/jobs")
async def list_jobs(request: Request, response: Response, owner_id: OwnerId = Depends(_owner)):
    _private(response)
    key = _resource(owner_id, ResourceKind.LIBRARY, owner_id.value)
    async with request.app.state.hosted_runtime.serialized(key):
        return await request.app.state.tenant_repository.list_jobs(owner_id)


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: uuid.UUID,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_owner),
):
    _private(response)
    key = _resource(owner_id, ResourceKind.JOB, job_id)
    async with request.app.state.hosted_runtime.serialized(key):
        job = await request.app.state.tenant_repository.get_job(owner_id, job_id)
        if job is None:
            raise HTTPException(404, "unknown job")
        return job


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: uuid.UUID,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    _private(response)
    key = _resource(owner_id, ResourceKind.JOB, job_id)
    async with request.app.state.hosted_runtime.serialized(key):
        job = await request.app.state.tenant_repository.cancel_job(owner_id, job_id)
        if job is None:
            raise HTTPException(404, "unknown job")
        if job["state"] in {"succeeded", "failed"}:
            raise HTTPException(409, "completed job cannot be cancelled")
        return job


@router.get("/books/{book_id}")
async def get_book(
    book_id: uuid.UUID,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_owner),
):
    _private(response)
    resource = _resource(owner_id, ResourceKind.BOOK, book_id)
    cache_key = TenantCacheKey(resource, CacheNamespace.BOOK_METADATA, "v1")
    async with request.app.state.hosted_runtime.serialized(resource):
        cached = await request.app.state.hosted_runtime.cache_get(cache_key)
        if cached is not None:
            return cached
        book = await request.app.state.tenant_repository.get_book(owner_id, book_id)
        if book is None:
            raise HTTPException(404, "unknown book")
        await request.app.state.hosted_runtime.cache_set(cache_key, book)
        return book


@router.get("/books/{book_id}/manifest")
async def get_book_manifest(
    book_id: uuid.UUID,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_owner),
):
    _private(response)
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(key):
        manifest = await request.app.state.tenant_repository.get_book_manifest(
            owner_id, book_id
        )
        if manifest is None:
            raise HTTPException(404, "unknown book")
        return manifest


@router.get("/books/{book_id}/epub")
async def stream_epub(
    book_id: uuid.UUID,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_owner),
):
    _private(response)
    resource = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(resource):
        record = await request.app.state.tenant_repository.source_object(owner_id, book_id)
        if record is None:
            raise HTTPException(404, "unknown book")
        if record.provider != request.app.state.object_storage.provider:
            raise HTTPException(503, "source object storage is unavailable")
        ref = SourceObjectRef(owner_id, record.object_id)
        try:
            stored = await asyncio.to_thread(request.app.state.object_storage.get, ref)
        except (ObjectNotFoundError, ObjectIntegrityError, ObjectStorageUnavailableError) as exc:
            raise _storage_failure(exc) from exc
        if (
            stored.media_type != record.media_type
            or stored.byte_size != record.byte_size
            or stored.sha256 != record.sha256
        ):
            raise HTTPException(503, "source object metadata does not match stored bytes")

    def chunks():
        for offset in range(0, len(stored.data), 64 * 1024):
            yield stored.data[offset : offset + 64 * 1024]

    return StreamingResponse(
        chunks(),
        media_type=EPUB_MEDIA_TYPE,
        headers={
            "Content-Length": str(stored.byte_size),
            "Content-Disposition": f'inline; filename="{book_id}.epub"',
            "Cache-Control": "private, no-store",
            "Pragma": "no-cache",
        },
    )


@router.delete("/books/{book_id}", status_code=204)
async def delete_book(
    book_id: uuid.UUID,
    request: Request,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    resource = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(resource):
        record = await request.app.state.tenant_repository.source_object(owner_id, book_id)
        if record is None:
            raise HTTPException(404, "unknown book")
        if record.provider != request.app.state.object_storage.provider:
            raise HTTPException(503, "source object storage is unavailable")
        try:
            await asyncio.to_thread(
                request.app.state.object_storage.delete,
                SourceObjectRef(owner_id, record.object_id),
            )
        except ObjectStorageUnavailableError as exc:
            raise _storage_failure(exc) from exc
        if not await request.app.state.tenant_repository.soft_delete_book(owner_id, book_id):
            raise HTTPException(404, "unknown book")
        await request.app.state.hosted_runtime.cache_invalidate_resource(resource)
    return Response(status_code=204)


@router.get("/books/{book_id}/position")
async def get_position(
    book_id: uuid.UUID,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_owner),
):
    _private(response)
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(key):
        position = await request.app.state.tenant_repository.get_position(owner_id, book_id)
        if position is None:
            raise HTTPException(404, "unknown book")
        return position


@router.get("/books/{book_id}/preferences")
async def get_reader_preferences(
    book_id: uuid.UUID,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_owner),
):
    _private(response)
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(key):
        preferences = await request.app.state.tenant_repository.get_reader_preferences(
            owner_id, book_id
        )
        if preferences is None:
            raise HTTPException(404, "unknown book")
        return preferences


@router.get("/books/{book_id}/search")
async def search_book(
    book_id: uuid.UUID,
    request: Request,
    response: Response,
    q: str = Query(min_length=2, max_length=200),
    limit: int = Query(default=20, ge=1, le=50),
    owner_id: OwnerId = Depends(_owner),
):
    _private(response)
    query = q.strip()
    if len(query) < 2:
        raise HTTPException(422, "search query is too short")
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(key):
        results = await request.app.state.tenant_repository.search_book(
            owner_id, book_id, query, limit=limit
        )
        if results is None:
            raise HTTPException(404, "unknown book")
        return results


def _ask_cost(calls: list[dict]) -> dict:
    return {
        "currency": "USD",
        "usd": f"{sum(call['usd'] for call in calls):.10f}",
        "input_tokens": sum(call["input_tokens"] for call in calls),
        "output_tokens": sum(call["output_tokens"] for call in calls),
        "pricing_known": all(call["pricing_known"] for call in calls),
        "calls": [
            {
                "provider": call["provider"],
                "model": call["model"],
                "usd": f"{call['usd']:.10f}",
            }
            for call in calls
        ],
        "payer": "your configured provider account",
    }


def _public_ask_citation(source: dict) -> dict:
    return {
        "id": source["id"],
        "ordinal": source["ordinal"],
        "chapter_key": source["chapter_key"],
        "href": source["href"],
        "title": source["title"],
        "excerpt": source["text"],
    }


def _ready_ai_setting(settings: dict, capability: str) -> dict:
    setting = settings.get(capability)
    if setting is None or not setting.get("enabled"):
        raise HTTPException(503, {
            "code": "ai_not_configured",
            "message": f"Configure a {capability} provider before asking the book.",
        })
    if setting.get("provider") == "offline" or setting.get("validation_status") != "ready":
        raise HTTPException(503, {
            "code": "provider_not_ready",
            "message": f"The configured {capability} provider is not ready.",
        })
    if not setting.get("credential_id"):
        raise HTTPException(503, {
            "code": "provider_not_ready",
            "message": f"The configured {capability} credential is unavailable.",
        })
    return setting


async def _run_owner_completion(
    request: Request,
    owner_id: OwnerId,
    book_id: uuid.UUID,
    setting: dict,
    *,
    phase: str,
    system: str,
    user: str,
    schema,
    max_output_tokens: int,
):
    try:
        resolved = await request.app.state.tenant_repository.resolve_credential(
            owner_id,
            uuid.UUID(setting["credential_id"]),
            request.app.state.credential_cipher,
        )
    except CredentialUnavailableError as exc:
        raise HTTPException(503, {
            "code": "provider_not_ready",
            "message": "The configured provider credential is unavailable.",
        }) from exc
    with resolved:
        client = completion_client(setting, resolved.get_secret_value())
        try:
            try:
                return await budgeted_hosted_completion(
                    request.app.state.tenant_repository,
                    owner_id,
                    book_id,
                    request.app.state.settings,
                    setting,
                    client,
                    phase=phase,
                    system=system,
                    user=user,
                    schema=schema,
                    max_output_tokens=max_output_tokens,
                )
            except CredentialUnavailableError as exc:
                raise HTTPException(409, {
                    "code": "provider_configuration_changed",
                    "message": "The provider configuration changed; submit the question again.",
                }) from exc
        finally:
            close_completion_client(client)


@router.post("/books/{book_id}/ask")
async def ask_the_book(
    book_id: uuid.UUID,
    body: AskRequest,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    _private(response)
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    calls: list[dict] = []

    def remember(result, setting):
        calls.append({
            "provider": setting["provider"],
            "model": setting["model"],
            "input_tokens": int(result.usage.get("in", 0) or 0),
            "output_tokens": int(result.usage.get("out", 0) or 0),
            "usd": float(result.usd),
            "pricing_known": pricing_known(setting["model"]),
        })

    async with request.app.state.hosted_runtime.serialized(key):
        context = await request.app.state.tenant_repository.ask_context(
            owner_id,
            book_id,
            body.question,
            requested_bookmark=body.bookmark,
        )
        if context is None:
            raise HTTPException(404, "unknown book")
        if not context["sources"]:
            return {
                "as_of_chapter": context["as_of_chapter"],
                "insufficient_evidence": True,
                "claims": [],
                "citations": [],
                "cost": _ask_cost(calls),
            }
        synthesis = _ready_ai_setting(context["settings"], "synthesis")
        judge = _ready_ai_setting(context["settings"], "judge")
        prompt = ask_prompt(body.question, context["sources"])
        for _attempt in range(2):
            try:
                generated = await _run_owner_completion(
                    request,
                    owner_id,
                    book_id,
                    synthesis,
                    phase="synthesis",
                    system=ASK_SYSTEM,
                    user=prompt,
                    schema=AskDraft,
                    max_output_tokens=1200,
                )
            except LimitExceededError as exc:
                raise _limit_failure(exc) from exc
            except CostCeilingExceeded as exc:
                raise HTTPException(429, "AI request exceeds the configured token ceiling") from exc
            remember(generated, synthesis)
            try:
                draft = validate_ask_draft(generated.value, context["sources"])
            except (AskSafetyError, ValueError):
                continue
            if draft.insufficient_evidence:
                return {
                    "as_of_chapter": context["as_of_chapter"],
                    "insufficient_evidence": True,
                    "claims": [],
                    "citations": [],
                    "cost": _ask_cost(calls),
                }
            used = cited_sources(draft, context["sources"])
            answer = draft_text(draft)
            try:
                reviewed = await _run_owner_completion(
                    request,
                    owner_id,
                    book_id,
                    judge,
                    phase="judge",
                    system=JUDGE_SYSTEM,
                    user=judge_prompt(answer, source_facts(used)),
                    schema=JudgeVerdict,
                    max_output_tokens=500,
                )
            except LimitExceededError as exc:
                raise _limit_failure(exc) from exc
            except CostCeilingExceeded as exc:
                raise HTTPException(429, "AI request exceeds the configured token ceiling") from exc
            remember(reviewed, judge)
            verdict = JudgeVerdict.model_validate(reviewed.value)
            if verdict.references_future or verdict.unsupported_claims:
                continue
            return {
                "as_of_chapter": context["as_of_chapter"],
                "insufficient_evidence": False,
                "claims": [claim.model_dump(mode="json") for claim in draft.claims],
                "citations": [_public_ask_citation(source) for source in used],
                "cost": _ask_cost(calls),
            }
    raise HTTPException(502, "answer could not be cleared against the pages you have read")


@router.post("/books/{book_id}/selection-action")
async def selection_action(
    book_id: uuid.UUID,
    body: SelectionActionRequest,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    _private(response)
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    calls: list[dict] = []

    def remember(result, setting):
        calls.append({
            "provider": setting["provider"],
            "model": setting["model"],
            "input_tokens": int(result.usage.get("in", 0) or 0),
            "output_tokens": int(result.usage.get("out", 0) or 0),
            "usd": float(result.usd),
            "pricing_known": pricing_known(setting["model"]),
        })

    async with request.app.state.hosted_runtime.serialized(key):
        context = await request.app.state.tenant_repository.selection_action_context(
            owner_id, book_id, body.atom
        )
        if context is None:
            raise HTTPException(404, "unknown book")
        if context["source"] is None:
            raise HTTPException(422, "selection is outside the current reading position")
        synthesis = _ready_ai_setting(context["settings"], "synthesis")
        judge = _ready_ai_setting(context["settings"], "judge")
        source = {
            "id": 1,
            "ordinal": body.atom,
            "chapter_key": str(body.atom),
            "href": context["source"]["href"],
            "title": context["source"]["title"] or f"Chapter {body.atom}",
            "text": body.text,
        }
        prompt = selection_prompt(body)
        for _attempt in range(2):
            try:
                generated = await _run_owner_completion(
                    request,
                    owner_id,
                    book_id,
                    synthesis,
                    phase="synthesis",
                    system=SELECTION_SYSTEM,
                    user=prompt,
                    schema=SelectionDraft,
                    max_output_tokens=800,
                )
            except LimitExceededError as exc:
                raise _limit_failure(exc) from exc
            except CostCeilingExceeded as exc:
                raise HTTPException(429, "AI request exceeds the configured token ceiling") from exc
            remember(generated, synthesis)
            try:
                draft = SelectionDraft.model_validate(generated.value)
            except ValueError:
                continue
            if draft.insufficient_evidence:
                return {
                    "action": body.action,
                    "as_of_chapter": context["as_of_chapter"],
                    "insufficient_evidence": True,
                    "text": None,
                    "citation": None,
                    "cost": _ask_cost(calls),
                }
            try:
                reviewed = await _run_owner_completion(
                    request,
                    owner_id,
                    book_id,
                    judge,
                    phase="judge",
                    system=JUDGE_SYSTEM,
                    user=judge_prompt(draft.text or "", source_facts([source])),
                    schema=JudgeVerdict,
                    max_output_tokens=500,
                )
            except LimitExceededError as exc:
                raise _limit_failure(exc) from exc
            except CostCeilingExceeded as exc:
                raise HTTPException(429, "AI request exceeds the configured token ceiling") from exc
            remember(reviewed, judge)
            verdict = JudgeVerdict.model_validate(reviewed.value)
            if verdict.references_future or verdict.unsupported_claims:
                continue
            citation = _public_ask_citation(source)
            citation["cfi"] = body.cfi
            return {
                "action": body.action,
                "as_of_chapter": context["as_of_chapter"],
                "insufficient_evidence": False,
                "text": draft.text,
                "citation": citation,
                "cost": _ask_cost(calls),
            }
    raise HTTPException(502, "selection help could not be cleared against the selected passage")


@router.post("/books/{book_id}/chapter-closeout")
async def chapter_closeout(
    book_id: uuid.UUID,
    body: ChapterCloseoutRequest,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    _private(response)
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    calls: list[dict] = []

    def remember(result, setting):
        calls.append({
            "provider": setting["provider"],
            "model": setting["model"],
            "input_tokens": int(result.usage.get("in", 0) or 0),
            "output_tokens": int(result.usage.get("out", 0) or 0),
            "usd": float(result.usd),
            "pricing_known": pricing_known(setting["model"]),
        })

    async with request.app.state.hosted_runtime.serialized(key):
        context = await request.app.state.tenant_repository.chapter_closeout_context(
            owner_id, book_id, body.chapter
        )
        if context is None:
            raise HTTPException(404, "unknown book")
        if body.chapter > context["as_of_chapter"] or not context["documents"]:
            raise HTTPException(409, "chapter is not completed or unavailable")
        synthesis = _ready_ai_setting(context["settings"], "synthesis")
        judge = _ready_ai_setting(context["settings"], "judge")
        first = context["documents"][0]
        raw = "\n\n".join(document["content"] for document in context["documents"])
        sources = chapter_passages(
            raw,
            ordinal=body.chapter,
            chapter_key=str(body.chapter),
            href=first["href"],
            title=first["title"] or f"Chapter {body.chapter}",
        )
        prompt = chapter_closeout_prompt(body.chapter, sources)
        for _attempt in range(2):
            try:
                generated = await _run_owner_completion(
                    request,
                    owner_id,
                    book_id,
                    synthesis,
                    phase="synthesis",
                    system=CLOSEOUT_SYSTEM,
                    user=prompt,
                    schema=AskDraft,
                    max_output_tokens=1200,
                )
            except LimitExceededError as exc:
                raise _limit_failure(exc) from exc
            except CostCeilingExceeded as exc:
                raise HTTPException(429, "AI request exceeds the configured token ceiling") from exc
            remember(generated, synthesis)
            try:
                draft = validate_ask_draft(generated.value, sources)
            except (AskSafetyError, ValueError):
                continue
            if draft.insufficient_evidence:
                return {
                    "chapter": body.chapter,
                    "as_of_chapter": context["as_of_chapter"],
                    "insufficient_evidence": True,
                    "claims": [],
                    "citations": [],
                    "cost": _ask_cost(calls),
                }
            used = cited_sources(draft, sources)
            try:
                reviewed = await _run_owner_completion(
                    request,
                    owner_id,
                    book_id,
                    judge,
                    phase="judge",
                    system=JUDGE_SYSTEM,
                    user=judge_prompt(draft_text(draft), source_facts(used)),
                    schema=JudgeVerdict,
                    max_output_tokens=500,
                )
            except LimitExceededError as exc:
                raise _limit_failure(exc) from exc
            except CostCeilingExceeded as exc:
                raise HTTPException(429, "AI request exceeds the configured token ceiling") from exc
            remember(reviewed, judge)
            verdict = JudgeVerdict.model_validate(reviewed.value)
            if verdict.references_future or verdict.unsupported_claims:
                continue
            return {
                "chapter": body.chapter,
                "as_of_chapter": context["as_of_chapter"],
                "insufficient_evidence": False,
                "claims": [claim.model_dump(mode="json") for claim in draft.claims],
                "citations": [_public_ask_citation(source) for source in used],
                "cost": _ask_cost(calls),
            }
    raise HTTPException(502, "chapter closeout could not be cleared against the completed chapter")


@router.get("/books/{book_id}/marks")
async def list_reader_marks(
    book_id: uuid.UUID,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_owner),
):
    _private(response)
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(key):
        marks = await request.app.state.tenant_repository.list_reader_marks(owner_id, book_id)
        if marks is None:
            raise HTTPException(404, "unknown book")
        return marks


@router.get("/books/{book_id}/marks/export")
async def export_reader_marks(
    book_id: uuid.UUID,
    request: Request,
    owner_id: OwnerId = Depends(_owner),
):
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(key):
        marks = await request.app.state.tenant_repository.list_reader_marks(owner_id, book_id)
        if marks is None:
            raise HTTPException(404, "unknown book")
    payload = {
        "format": "litlet-reader-marks",
        "version": 1,
        "book_id": str(book_id),
        **marks,
    }
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={
            "Cache-Control": "private, no-store",
            "Pragma": "no-cache",
            "Content-Disposition": f'attachment; filename="litlet-marks-{book_id}.json"',
        },
    )


async def _create_reader_mark(
    *,
    kind: str,
    body: HighlightIn | AnnotationIn | BookmarkIn,
    book_id: uuid.UUID,
    request: Request,
    response: Response,
    owner_id: OwnerId,
):
    _private(response)
    values = body.model_dump()
    anchor = values.pop("anchor")
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(key):
        try:
            return await request.app.state.tenant_repository.create_reader_mark(
                owner_id, book_id, kind=kind, anchor=anchor, **values
            )
        except MissingTenantResourceError as exc:
            raise HTTPException(404, "unknown book or linked highlight") from exc
        except InvalidPositionError as exc:
            raise HTTPException(409, str(exc)) from exc


@router.post("/books/{book_id}/highlights", status_code=201)
async def create_highlight(
    book_id: uuid.UUID,
    body: HighlightIn,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    return await _create_reader_mark(
        kind="highlight", body=body, book_id=book_id, request=request,
        response=response, owner_id=owner_id,
    )


@router.post("/books/{book_id}/annotations", status_code=201)
async def create_annotation(
    book_id: uuid.UUID,
    body: AnnotationIn,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    return await _create_reader_mark(
        kind="annotation", body=body, book_id=book_id, request=request,
        response=response, owner_id=owner_id,
    )


@router.post("/books/{book_id}/bookmarks", status_code=201)
async def create_bookmark(
    book_id: uuid.UUID,
    body: BookmarkIn,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    return await _create_reader_mark(
        kind="bookmark", body=body, book_id=book_id, request=request,
        response=response, owner_id=owner_id,
    )


async def _update_reader_mark(
    *,
    kind: str,
    value: str,
    book_id: uuid.UUID,
    mark_id: uuid.UUID,
    request: Request,
    response: Response,
    owner_id: OwnerId,
):
    _private(response)
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(key):
        mark = await request.app.state.tenant_repository.update_reader_mark(
            owner_id, book_id, mark_id, kind=kind, value=value
        )
        if mark is None:
            raise HTTPException(404, "unknown mark")
        return mark


@router.patch("/books/{book_id}/highlights/{mark_id}")
async def update_highlight(
    book_id: uuid.UUID,
    mark_id: uuid.UUID,
    body: HighlightUpdateIn,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    return await _update_reader_mark(
        kind="highlight", value=body.color, book_id=book_id, mark_id=mark_id,
        request=request, response=response, owner_id=owner_id,
    )


@router.patch("/books/{book_id}/annotations/{mark_id}")
async def update_annotation(
    book_id: uuid.UUID,
    mark_id: uuid.UUID,
    body: AnnotationUpdateIn,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    return await _update_reader_mark(
        kind="annotation", value=body.body, book_id=book_id, mark_id=mark_id,
        request=request, response=response, owner_id=owner_id,
    )


@router.patch("/books/{book_id}/bookmarks/{mark_id}")
async def update_bookmark(
    book_id: uuid.UUID,
    mark_id: uuid.UUID,
    body: BookmarkUpdateIn,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    return await _update_reader_mark(
        kind="bookmark", value=body.label, book_id=book_id, mark_id=mark_id,
        request=request, response=response, owner_id=owner_id,
    )


async def _delete_reader_mark(
    *,
    kind: str,
    book_id: uuid.UUID,
    mark_id: uuid.UUID,
    request: Request,
    owner_id: OwnerId,
):
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(key):
        deleted = await request.app.state.tenant_repository.delete_reader_mark(
            owner_id, book_id, mark_id, kind=kind
        )
        if not deleted:
            raise HTTPException(404, "unknown mark")
    return Response(status_code=204)


@router.delete("/books/{book_id}/highlights/{mark_id}", status_code=204)
async def delete_highlight(
    book_id: uuid.UUID,
    mark_id: uuid.UUID,
    request: Request,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    return await _delete_reader_mark(
        kind="highlight", book_id=book_id, mark_id=mark_id,
        request=request, owner_id=owner_id,
    )


@router.delete("/books/{book_id}/annotations/{mark_id}", status_code=204)
async def delete_annotation(
    book_id: uuid.UUID,
    mark_id: uuid.UUID,
    request: Request,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    return await _delete_reader_mark(
        kind="annotation", book_id=book_id, mark_id=mark_id,
        request=request, owner_id=owner_id,
    )


@router.delete("/books/{book_id}/bookmarks/{mark_id}", status_code=204)
async def delete_bookmark(
    book_id: uuid.UUID,
    mark_id: uuid.UUID,
    request: Request,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    return await _delete_reader_mark(
        kind="bookmark", book_id=book_id, mark_id=mark_id,
        request=request, owner_id=owner_id,
    )


@router.put("/books/{book_id}/preferences")
async def put_reader_preferences(
    book_id: uuid.UUID,
    preferences: ReaderPreferencesIn,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    _private(response)
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(key):
        try:
            return await request.app.state.tenant_repository.upsert_reader_preferences(
                owner_id,
                book_id,
                **preferences.model_dump(),
            )
        except MissingTenantResourceError as exc:
            raise HTTPException(404, "unknown book") from exc


@router.put("/books/{book_id}/position")
async def update_position(
    book_id: uuid.UUID,
    update: PositionUpdateIn,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    _private(response)
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(key):
        try:
            return await request.app.state.tenant_repository.update_position(
                owner_id,
                book_id,
                cfi=update.cfi,
                offset=update.offset,
                completed_chapter=update.completed_chapter,
                expected_epoch=update.position_epoch,
                base_version=update.base_version,
                client_id=update.client_id,
                client_sequence=update.client_sequence,
            )
        except MissingTenantResourceError as exc:
            raise HTTPException(404, "unknown book") from exc
        except StalePositionEpochError as exc:
            raise HTTPException(
                409, "reading position was reset in another session; reload before continuing"
            ) from exc
        except FuturePositionVersionError as exc:
            raise HTTPException(
                409, "reading position version is ahead of the server; reload before continuing"
            ) from exc
        except InvalidPositionError as exc:
            raise HTTPException(422, str(exc)) from exc


@router.post("/books/{book_id}/position/reset")
async def reset_position(
    book_id: uuid.UUID,
    reset: PositionResetIn,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    _private(response)
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(key):
        try:
            return await request.app.state.tenant_repository.reset_position(
                owner_id, book_id, reset.position_epoch
            )
        except MissingTenantResourceError as exc:
            raise HTTPException(404, "unknown book") from exc
        except StalePositionEpochError as exc:
            raise HTTPException(
                409, "reading position was reset in another session; reload before continuing"
            ) from exc


@router.get("/books/{book_id}/memory")
async def memory_snapshot(
    book_id: uuid.UUID,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_owner),
):
    _private(response)
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(key):
        snapshot = await request.app.state.tenant_repository.memory_snapshot(owner_id, book_id)
        if snapshot is None:
            raise HTTPException(404, "unknown book")
        return snapshot


@router.get("/books/{book_id}/memory-corrections")
async def memory_corrections(
    book_id: uuid.UUID,
    request: Request,
    response: Response,
    bookmark: int | None = Query(default=None, ge=0),
    owner_id: OwnerId = Depends(_owner),
):
    _private(response)
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(key):
        result = await request.app.state.tenant_repository.memory_corrections(
            owner_id, book_id, bookmark
        )
        if result is None:
            raise HTTPException(404, "unknown book")
        return result


@router.post("/books/{book_id}/memory-corrections")
async def correct_memory(
    book_id: uuid.UUID,
    body: HostedEntityCorrectionIn,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    _private(response)
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(key):
        try:
            result = await request.app.state.tenant_repository.replace_memory_entity(
                owner_id,
                book_id,
                source_entity_id=body.source_entity_id,
                canonical_name=body.canonical_name,
                reason=body.reason,
                bookmark=body.bookmark,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if result is None:
            raise HTTPException(404, "unknown book")
        return result


@router.get("/costs")
async def list_costs(
    request: Request,
    response: Response,
    book_id: uuid.UUID | None = None,
    owner_id: OwnerId = Depends(_owner),
):
    _private(response)
    kind = ResourceKind.BOOK if book_id is not None else ResourceKind.COSTS
    key = _resource(owner_id, kind, book_id or owner_id.value)
    async with request.app.state.hosted_runtime.serialized(key):
        costs = await request.app.state.tenant_repository.list_costs(owner_id, book_id)
        if costs is None:
            raise HTTPException(404, "unknown book")
        return costs
