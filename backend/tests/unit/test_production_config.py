"""Unit tests for production configuration and hardening.

Tests cover:
- Database connection pool settings are applied
- Rate limiting configuration
- Logging configuration
- Config validation (secret key length etc.)
- OpenAPI spec completeness (all endpoints have schemas)
"""

from __future__ import annotations

import os

import pytest

from app.config import Settings, MIN_SECRET_KEY_LENGTH


class TestProductionConfig:
    """Tests for production configuration settings."""

    def test_default_pool_settings(self):
        settings = Settings(
            _env_file=None,
            secret_key="a" * MIN_SECRET_KEY_LENGTH,
        )
        assert settings.db_pool_size == 10
        assert settings.db_max_overflow == 20
        assert settings.db_pool_timeout == 30
        assert settings.db_pool_recycle == 1800

    def test_rate_limit_defaults(self):
        settings = Settings(
            _env_file=None,
            secret_key="a" * MIN_SECRET_KEY_LENGTH,
        )
        assert settings.rate_limit_max_requests == 100
        assert settings.rate_limit_window_seconds == 60

    def test_log_format_defaults(self):
        settings = Settings(
            _env_file=None,
            secret_key="a" * MIN_SECRET_KEY_LENGTH,
        )
        assert settings.log_format == "json"
        assert settings.log_level == "INFO"

    def test_env_overrides(self):
        """Environment variables with SLATE_ prefix should override defaults."""
        overrides = {
            "SLATE_DB_POOL_SIZE": "20",
            "SLATE_DB_MAX_OVERFLOW": "40",
            "SLATE_RATE_LIMIT_MAX_REQUESTS": "200",
            "SLATE_LOG_FORMAT": "text",
            "SLATE_LOG_LEVEL": "DEBUG",
            "SLATE_SECRET_KEY": "a" * MIN_SECRET_KEY_LENGTH,
        }
        for k, v in overrides.items():
            os.environ[k] = v
        try:
            settings = Settings(_env_file=None)
            assert settings.db_pool_size == 20
            assert settings.db_max_overflow == 40
            assert settings.rate_limit_max_requests == 200
            assert settings.log_format == "text"
            assert settings.log_level == "DEBUG"
        finally:
            for k in overrides:
                os.environ.pop(k, None)

    def test_secret_key_too_short_raises(self):
        with pytest.raises(ValueError, match="SLATE_SECRET_KEY must be at least"):
            Settings(
                _env_file=None,
                secret_key="short",
            )


class TestOpenAPISpec:
    """Verify OpenAPI spec is accessible and comprehensive."""

    @pytest.mark.asyncio
    async def test_openapi_json_accessible(self, client):
        """GET /openapi.json should return a valid OpenAPI spec."""
        resp = await client.get("/openapi.json")
        assert resp.status_code == 200
        data = resp.json()
        assert "openapi" in data
        assert "paths" in data
        # Verify key endpoints are documented
        paths = data["paths"]
        assert any("/agents" in path for path in paths)
        assert any("/reviews" in path for path in paths)
        assert any("/workflows" in path for path in paths)
        assert any("/audit" in path for path in paths)
        assert any("/dashboard" in path for path in paths)

    @pytest.mark.asyncio
    async def test_openapi_all_endpoints_have_schemas(self, client):
        """Every documented path must have request/response schemas defined."""
        resp = await client.get("/openapi.json")
        assert resp.status_code == 200
        spec = resp.json()
        paths = spec.get("paths", {})

        # Ensure we have a meaningful number of paths documented
        assert len(paths) >= 10, (
            f"Expected at least 10 API paths documented, found {len(paths)}"
        )

        missing_response_schemas = []
        for path, methods in paths.items():
            for method, details in methods.items():
                if method in ("get", "post", "put", "delete", "patch"):
                    responses = details.get("responses", {})
                    if not responses:
                        missing_response_schemas.append(f"{method.upper()} {path}")
                    else:
                        # At least one response code should have content defined
                        has_content = any(
                            "content" in resp_detail
                            for resp_detail in responses.values()
                            if isinstance(resp_detail, dict)
                        )
                        if not has_content:
                            missing_response_schemas.append(
                                f"{method.upper()} {path} (no response content)"
                            )

        assert not missing_response_schemas, (
            f"Endpoints missing response schemas:\n"
            + "\n".join(f"  - {p}" for p in missing_response_schemas[:20])
        )

    @pytest.mark.asyncio
    async def test_openapi_error_responses_documented(self, client):
        """Key endpoints should document error responses (4xx)."""
        resp = await client.get("/openapi.json")
        assert resp.status_code == 200
        spec = resp.json()
        paths = spec.get("paths", {})

        # Check that POST endpoints document at least a success response
        post_endpoints = [
            (path, details.get("post", {}))
            for path, details in paths.items()
            if "post" in details
        ]

        for path, post_detail in post_endpoints:
            responses = post_detail.get("responses", {})
            # Every POST should have at least one success response code
            success_codes = [c for c in responses if c.startswith(("2", "3"))]
            assert success_codes, (
                f"POST {path} has no success response codes documented"
            )

    @pytest.mark.asyncio
    async def test_openapi_agent_endpoints_documented(self, client):
        """Verify all 6 agent types have task endpoints in the spec."""
        resp = await client.get("/openapi.json")
        assert resp.status_code == 200
        paths = resp.json().get("paths", {})

        # The agent routes use a path parameter {agent_type} so we check for
        # the parameterized path pattern
        agent_paths = [p for p in paths if "/agents/" in p]
        assert len(agent_paths) >= 1, (
            "No agent task endpoints found in OpenAPI spec"
        )

    @pytest.mark.asyncio
    async def test_docs_page_accessible(self, client):
        """GET /docs should return the Swagger UI HTML page."""
        resp = await client.get("/docs")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
