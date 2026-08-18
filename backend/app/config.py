"""Service configuration (ADR 0007 D-A7): pydantic-settings over the git-ignored repo-root ``.env``,
loaded via an ABSOLUTE ``__file__``-derived path (never a CWD lookup), fail-loud by default.

The fail-loud predicate is delegated to ``LLMClient``: at startup ``deps.build_state`` constructs the
client with ``allow_stub=settings.allow_stub``; if no real LLM provider resolves and ``ALLOW_STUB`` is
not set, the client RAISES and the app refuses to start (default-deny — a silent stub deploy would
produce garbage extractions). The key never leaves the process; settings are never logged wholesale.
"""
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# app/config.py -> app -> backend -> repo root (D-A7: parents[2], verified against the layout)
ENV_FILE = str(Path(__file__).resolve().parents[2] / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    # LLM / embeddings (all optional — the client auto-detects; embed_* are independent, D19)
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    openai_base_url: str | None = None
    embed_provider: str | None = None
    embed_base_url: str | None = None
    embed_api_key: str | None = None
    embed_model: str | None = None
    llm_cheap_model: str | None = None
    llm_large_model: str | None = None

    # service
    deployment_mode: Literal["local", "hosted"] = "local"
    data_dir: str = str(Path(__file__).resolve().parents[2] / "data")
    frontend_dist_dir: str | None = None
    expose_api_docs: bool = False
    trusted_hosts: str = "localhost,127.0.0.1,testserver"
    allow_stub: bool = False                      # D-A7 default-deny
    vector_backend: Literal["vec0", "bruteforce"] = "vec0"
    store_max_handles: int = Field(default=16, ge=1)
    segmentation_cache_max_entries: int = Field(default=8, ge=1)
    recap_cache_max_entries: int = Field(default=128, ge=1)
    recap_failure_max_entries: int = Field(default=128, ge=1)
    recap_max_inflight: int = Field(default=8, ge=1)
    epub_max_upload_bytes: int = Field(default=128 * 1024 * 1024, ge=1, lt=2 ** 63)
    cost_max_input_tokens_per_call: int = Field(default=60_000, ge=1, lt=2 ** 63)
    cost_max_output_tokens_per_call: int = Field(default=4_096, ge=1, lt=2 ** 63)
    cost_max_input_tokens_per_book: int = Field(default=2_000_000, ge=1, lt=2 ** 63)
    cost_max_output_tokens_per_book: int = Field(default=500_000, ge=1, lt=2 ** 63)
    cost_max_usd_per_book: float = Field(default=5.0, gt=0, allow_inf_nan=False)

    # Hosted authentication (LIT-40). DSNs and client secrets use SecretStr so validation errors,
    # reprs, and diagnostics cannot accidentally print credentials.
    hosted_auth_dsn: SecretStr | None = None
    hosted_tenant_dsn: SecretStr | None = None
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: SecretStr | None = None
    oidc_redirect_uri: str | None = None
    oidc_scopes: str = "openid profile email"
    oidc_signing_algorithms: str = "RS256"
    oidc_request_timeout_seconds: float = Field(default=10.0, gt=0, le=30)
    oidc_clock_skew_seconds: int = Field(default=60, ge=0, le=300)
    oidc_attempt_ttl_seconds: int = Field(default=600, ge=60, le=1800)
    session_absolute_ttl_seconds: int = Field(default=8 * 60 * 60, ge=300, le=30 * 24 * 60 * 60)
    session_idle_ttl_seconds: int = Field(default=30 * 60, ge=60, le=24 * 60 * 60)
    hosted_runtime_cache_max_entries: int = Field(default=256, ge=1, le=100_000)
    hosted_runtime_lock_max_entries: int = Field(default=256, ge=1, le=100_000)
    hosted_runtime_shutdown_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    hosted_storage_backend: Literal["filesystem", "s3"] | None = None
    hosted_storage_filesystem_root: str | None = None
    hosted_storage_filesystem_key: SecretStr | None = None
    hosted_s3_bucket: str | None = None
    hosted_s3_region: str = "us-east-1"
    hosted_s3_endpoint_url: str | None = None
    hosted_s3_allow_insecure_http: bool = False
    hosted_s3_access_key_id: SecretStr | None = None
    hosted_s3_secret_access_key: SecretStr | None = None
    hosted_s3_sse_algorithm: Literal["AES256", "aws:kms"] = "AES256"
    hosted_s3_kms_key_id: str | None = None
    hosted_s3_expected_bucket_owner: str | None = None
    # LIT-45 envelope-encryption keyring. The active key encrypts only per-credential data keys;
    # previous version->base64 keys remain available solely while envelopes are being rewrapped.
    hosted_credential_master_key: SecretStr | None = None
    hosted_credential_key_version: str | None = None
    hosted_credential_previous_master_keys: SecretStr | None = None
    hosted_provider_allowed_origins: str = (
        "https://api.openai.com,https://api.anthropic.com"
    )
    hosted_provider_validation_timeout_seconds: float = Field(default=5.0, gt=0, le=15)

    @model_validator(mode="after")
    def validate_hosted_auth(self):
        if self.deployment_mode != "hosted":
            return self

        hosts = self.trusted_host_values()
        if not hosts or "*" in hosts:
            raise ValueError("hosted mode requires an explicit TRUSTED_HOSTS allowlist")

        required = {
            "HOSTED_AUTH_DSN": self.hosted_auth_dsn,
            "HOSTED_TENANT_DSN": self.hosted_tenant_dsn,
            "OIDC_ISSUER": self.oidc_issuer,
            "OIDC_CLIENT_ID": self.oidc_client_id,
            "OIDC_CLIENT_SECRET": self.oidc_client_secret,
            "OIDC_REDIRECT_URI": self.oidc_redirect_uri,
            "HOSTED_CREDENTIAL_MASTER_KEY": self.hosted_credential_master_key,
            "HOSTED_CREDENTIAL_KEY_VERSION": self.hosted_credential_key_version,
        }
        missing = [name for name, value in required.items() if value is None or value == ""]
        if missing:
            raise ValueError("hosted mode requires " + ", ".join(missing))

        assert self.oidc_issuer is not None
        assert self.oidc_redirect_uri is not None
        for label, value in (
            ("OIDC_ISSUER", self.oidc_issuer),
            ("OIDC_REDIRECT_URI", self.oidc_redirect_uri),
        ):
            parsed = urlsplit(value)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError(f"{label} must be an HTTPS URL without user information")
            if parsed.fragment or (label == "OIDC_ISSUER" and parsed.query):
                raise ValueError(f"{label} may not contain a query or fragment")
        if self.oidc_issuer.endswith("/"):
            raise ValueError("OIDC_ISSUER must use its exact issuer value without a trailing slash")
        if "openid" not in self.oidc_scopes.split():
            raise ValueError("OIDC_SCOPES must include openid")
        algorithms = self.oidc_signing_algorithm_values()
        if not algorithms or "none" in {item.lower() for item in algorithms}:
            raise ValueError("OIDC_SIGNING_ALGORITHMS must pin at least one signed algorithm")
        if self.session_idle_ttl_seconds > self.session_absolute_ttl_seconds:
            raise ValueError("SESSION_IDLE_TTL_SECONDS may not exceed absolute session lifetime")
        if self.hosted_storage_backend == "filesystem":
            if not self.hosted_storage_filesystem_root or self.hosted_storage_filesystem_key is None:
                raise ValueError(
                    "filesystem object storage requires HOSTED_STORAGE_FILESYSTEM_ROOT and "
                    "HOSTED_STORAGE_FILESYSTEM_KEY"
                )
        elif self.hosted_storage_backend == "s3":
            if not self.hosted_s3_bucket:
                raise ValueError("S3 object storage requires HOSTED_S3_BUCKET")
            if (self.hosted_s3_access_key_id is None) != (
                self.hosted_s3_secret_access_key is None
            ):
                raise ValueError("S3 static access key id and secret must be configured together")
            if self.hosted_s3_sse_algorithm == "aws:kms" and not self.hosted_s3_kms_key_id:
                raise ValueError("S3 aws:kms encryption requires HOSTED_S3_KMS_KEY_ID")
            if self.hosted_s3_sse_algorithm == "AES256" and self.hosted_s3_kms_key_id:
                raise ValueError("S3 AES256 encryption cannot use HOSTED_S3_KMS_KEY_ID")
            if self.hosted_s3_endpoint_url:
                endpoint = urlsplit(self.hosted_s3_endpoint_url)
                allowed_schemes = {"https"}
                if self.hosted_s3_allow_insecure_http:
                    allowed_schemes.add("http")
                if (
                    endpoint.scheme not in allowed_schemes
                    or not endpoint.hostname
                    or endpoint.username
                    or endpoint.password
                    or endpoint.query
                    or endpoint.fragment
                ):
                    raise ValueError("HOSTED_S3_ENDPOINT_URL must be an allowed origin URL")
        return self

    def oidc_signing_algorithm_values(self) -> tuple[str, ...]:
        return tuple(item.strip() for item in self.oidc_signing_algorithms.split(",") if item.strip())

    def trusted_host_values(self) -> tuple[str, ...]:
        return tuple(item.strip() for item in self.trusted_hosts.split(",") if item.strip())

    def llm_env(self) -> dict:
        """The env mapping handed to ``LLMClient(env=...)`` — settings values only (the process env is
        deliberately NOT consulted twice; one source of truth)."""
        pairs = {
            "OPENAI_API_KEY": self.openai_api_key,
            "ANTHROPIC_API_KEY": self.anthropic_api_key,
            "OPENAI_BASE_URL": self.openai_base_url,
            "EMBED_PROVIDER": self.embed_provider,
            "EMBED_BASE_URL": self.embed_base_url,
            "EMBED_API_KEY": self.embed_api_key,
            "EMBED_MODEL": self.embed_model,
            "LLM_CHEAP_MODEL": self.llm_cheap_model,
            "LLM_LARGE_MODEL": self.llm_large_model,
        }
        return {k: v for k, v in pairs.items() if v}
