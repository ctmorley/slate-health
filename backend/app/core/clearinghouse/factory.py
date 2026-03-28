"""Clearinghouse factory — select implementation based on organization config.

Provides a factory function and registry for instantiating the correct
clearinghouse client based on an organization's clearinghouse_config.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.clearinghouse.availity import AvailityClient
from app.core.clearinghouse.base import BaseClearinghouse, ClearinghouseError
from app.core.clearinghouse.claim_md import ClaimMDClient
from app.core.clearinghouse.mock import MockClearinghouse

logger = logging.getLogger(__name__)

# Registry of known clearinghouse implementations
_CLEARINGHOUSE_REGISTRY: dict[str, type[BaseClearinghouse]] = {
    "availity": AvailityClient,
    "claim_md": ClaimMDClient,
    "claimmd": ClaimMDClient,
    "mock": MockClearinghouse,
}


def register_clearinghouse(name: str, cls: type[BaseClearinghouse]) -> None:
    """Register a new clearinghouse implementation.

    Args:
        name: The clearinghouse name (lowercase, used in config).
        cls: The clearinghouse class to register.
    """
    _CLEARINGHOUSE_REGISTRY[name.lower()] = cls


def get_clearinghouse(
    *,
    clearinghouse_name: str,
    api_endpoint: str,
    credentials: dict[str, Any] | None = None,
    timeout: float = 30.0,
    max_retries: int = 3,
) -> BaseClearinghouse:
    """Create a clearinghouse client based on configuration.

    Args:
        clearinghouse_name: Name of the clearinghouse (e.g. 'availity', 'claim_md').
        api_endpoint: The API base URL for the clearinghouse.
        credentials: Authentication credentials (API keys, etc.).
        timeout: Request timeout in seconds.
        max_retries: Maximum retry attempts for connection failures.

    Returns:
        An instance of the appropriate clearinghouse client.

    Raises:
        ClearinghouseError: If the clearinghouse name is not recognized.
    """
    name = clearinghouse_name.lower().strip()
    cls = _CLEARINGHOUSE_REGISTRY.get(name)

    if cls is None:
        available = sorted(_CLEARINGHOUSE_REGISTRY.keys())
        raise ClearinghouseError(
            f"Unknown clearinghouse '{clearinghouse_name}'. "
            f"Available: {available}"
        )

    return cls(
        api_endpoint=api_endpoint,
        credentials=credentials,
        timeout=timeout,
        max_retries=max_retries,
    )


def get_clearinghouse_from_config(config: dict[str, Any]) -> BaseClearinghouse:
    """Create a clearinghouse client from a ClearinghouseConfig-style dict.

    Expects keys matching the ClearinghouseConfig model:
    - clearinghouse_name
    - api_endpoint
    - credentials (optional)

    Args:
        config: Dictionary with clearinghouse configuration.

    Returns:
        An instance of the appropriate clearinghouse client.
    """
    return get_clearinghouse(
        clearinghouse_name=config["clearinghouse_name"],
        api_endpoint=config["api_endpoint"],
        credentials=config.get("credentials"),
    )


def list_clearinghouses() -> list[str]:
    """Return sorted list of registered clearinghouse names."""
    return sorted(_CLEARINGHOUSE_REGISTRY.keys())
