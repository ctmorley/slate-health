"""Tests for configuration management."""

import os

import pytest


def test_default_settings():
    """Settings have sensible defaults."""
    from app.config import Settings

    s = Settings(
        _env_file=None,  # Don't load .env in tests
    )
    assert s.app_name == "Slate Health"
    assert s.debug is False
    assert s.port == 8000
    assert s.jwt_algorithm == "HS256"


def test_env_override(monkeypatch):
    """Environment variables override defaults."""
    monkeypatch.setenv("SLATE_DEBUG", "true")
    monkeypatch.setenv("SLATE_PORT", "9000")
    monkeypatch.setenv("SLATE_APP_NAME", "Test App")

    from app.config import Settings

    s = Settings(_env_file=None)
    assert s.debug is True
    assert s.port == 9000
    assert s.app_name == "Test App"


def test_short_secret_key_rejected():
    """Secret key shorter than 32 characters is rejected at startup."""
    from app.config import Settings

    with pytest.raises(Exception, match="SLATE_SECRET_KEY must be at least 32 characters"):
        Settings(_env_file=None, secret_key="too-short")


def test_minimum_length_secret_key_accepted():
    """Secret key of exactly 32 characters is accepted."""
    from app.config import Settings

    s = Settings(_env_file=None, secret_key="a" * 32)
    assert len(s.secret_key) == 32
