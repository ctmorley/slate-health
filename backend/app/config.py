"""Application configuration via pydantic-settings with env var support."""

import logging

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Minimum secret key length in bytes for HS256 (RFC 7518 §3.2 recommends key >= hash output)
MIN_SECRET_KEY_LENGTH = 32


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="SLATE_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Application
    app_name: str = "Slate Health"
    debug: bool = False

    # Database
    database_url: str = "postgresql+asyncpg://slate:slate@localhost:5432/slate_health"

    # Test database (used only in test mode)
    test_database_url: str = "sqlite+aiosqlite:///./test.db"

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Security
    secret_key: str = "change-me-in-production-use-at-least-32-bytes!"
    jwt_algorithm: str = "HS256"
    jwt_expiry_minutes: int = 30

    # Temporal
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "slate-health-agents"

    # FHIR
    fhir_base_url: str = ""

    # OIG LEIE exclusion list API base URL.  When set (non-empty), the
    # credentialing agent will use the HTTP-based OIG provider to query
    # the real OIG LEIE download/API and SAM.gov.  When empty, falls
    # back to MockOIGExclusionProvider (dev/test only).
    oig_api_base_url: str = ""

    # When True, allow mock/fallback data when external services (FHIR, NPPES,
    # OIG) are unreachable.  Defaults to False so that production deployments
    # surface real failures instead of silently generating reports from
    # synthetic data.  Set to True explicitly in dev/test environments.
    allow_mock_fallback: bool = False

    # Frontend URL for SSO callback redirects
    frontend_url: str = "http://localhost:3000/login"

    # SSO — SAML 2.0
    saml_sp_entity_id: str = "https://slate-health.example.com/saml/metadata"
    saml_sp_acs_url: str = "http://localhost:8000/api/v1/auth/callback/saml"
    saml_idp_entity_id: str = ""
    saml_idp_sso_url: str = ""
    saml_idp_x509_cert: str = ""
    saml_idp_metadata_url: str = ""

    # SSO — OIDC
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_discovery_url: str = ""
    oidc_redirect_uri: str = "http://localhost:8000/api/v1/auth/callback/oidc"
    oidc_scopes: str = "openid email profile"

    # Database connection pool
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: int = 30
    db_pool_recycle: int = 1800

    # Rate limiting
    rate_limit_max_requests: int = 100
    rate_limit_window_seconds: int = 60
    # Redis URL for distributed rate limiting (required for multi-replica prod).
    # When unset, falls back to in-memory per-instance limiting.
    rate_limit_redis_url: str = ""

    # Logging
    log_format: str = "json"  # "json" or "text"
    log_level: str = "INFO"

    # SSO — Role mapping
    # Comma-separated IdP attribute values that map to each role.
    sso_role_attribute: str = "role"
    sso_admin_values: str = "admin,administrator"
    sso_reviewer_values: str = "reviewer,staff,clinician"
    # Anything not matching admin or reviewer maps to "viewer".

    @model_validator(mode="after")
    def _validate_secret_key_length(self) -> "Settings":
        """Enforce minimum secret key length for cryptographic security."""
        if len(self.secret_key) < MIN_SECRET_KEY_LENGTH:
            raise ValueError(
                f"SLATE_SECRET_KEY must be at least {MIN_SECRET_KEY_LENGTH} characters. "
                f"Got {len(self.secret_key)} characters. "
                "Set SLATE_SECRET_KEY environment variable to a secure random string."
            )
        return self


settings = Settings()
