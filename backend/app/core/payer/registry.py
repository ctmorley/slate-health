"""Payer registry — CRUD operations for payer configurations with caching.

Provides a data access layer for payer records and clearinghouse configs
with an in-memory cache to avoid repeated database queries for frequently
accessed payer data.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payer import ClearinghouseConfig, Payer, PayerRule

logger = logging.getLogger(__name__)


class PayerNotFoundError(Exception):
    """Raised when a payer is not found."""
    pass


class PayerRegistry:
    """CRUD operations for payer configurations with caching.

    The cache is a simple dict that persists for the lifetime of the
    registry instance. In production, instantiate per-request or use
    a shared instance with TTL-based invalidation.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._cache: dict[str, Payer] = {}

    def _invalidate_cache(self, payer_id: str | None = None) -> None:
        """Invalidate cache for a specific payer or all payers."""
        if payer_id:
            self._cache.pop(payer_id, None)
        else:
            self._cache.clear()

    # ── Payer CRUD ──────────────────────────────────────────────────

    async def get_payer(self, payer_id: str) -> Payer:
        """Get a payer by ID, using cache if available."""
        if payer_id in self._cache:
            return self._cache[payer_id]

        stmt = select(Payer).where(Payer.id == payer_id)
        result = await self._session.execute(stmt)
        payer = result.scalar_one_or_none()

        if payer is None:
            raise PayerNotFoundError(f"Payer '{payer_id}' not found")

        self._cache[payer_id] = payer
        return payer

    async def get_payer_by_code(self, payer_id_code: str) -> Payer:
        """Get a payer by its external payer_id_code."""
        stmt = select(Payer).where(Payer.payer_id_code == payer_id_code)
        result = await self._session.execute(stmt)
        payer = result.scalar_one_or_none()

        if payer is None:
            raise PayerNotFoundError(
                f"Payer with code '{payer_id_code}' not found"
            )

        self._cache[str(payer.id)] = payer
        return payer

    async def list_payers(
        self,
        *,
        active_only: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Payer]:
        """List payers with optional active filter."""
        stmt = select(Payer).order_by(Payer.name)

        if active_only:
            stmt = stmt.where(Payer.is_active == True)  # noqa: E712

        stmt = stmt.offset(offset).limit(limit)
        result = await self._session.execute(stmt)
        payers = list(result.scalars().all())

        # Populate cache
        for p in payers:
            self._cache[str(p.id)] = p

        return payers

    async def create_payer(
        self,
        *,
        name: str,
        payer_id_code: str,
        payer_type: str | None = None,
        address: str | None = None,
        phone: str | None = None,
        electronic_payer_id: str | None = None,
    ) -> Payer:
        """Create a new payer record."""
        payer = Payer(
            name=name,
            payer_id_code=payer_id_code,
            payer_type=payer_type,
            address=address,
            phone=phone,
            electronic_payer_id=electronic_payer_id,
            is_active=True,
        )
        self._session.add(payer)
        await self._session.flush()
        self._cache[str(payer.id)] = payer
        logger.info("Created payer: %s (%s)", name, payer_id_code)
        return payer

    async def update_payer(
        self, payer_id: str, **updates: Any
    ) -> Payer:
        """Update a payer's fields."""
        payer = await self.get_payer(payer_id)

        allowed_fields = {
            "name", "payer_type", "address", "phone",
            "electronic_payer_id", "is_active",
        }
        for field_name, value in updates.items():
            if field_name in allowed_fields:
                setattr(payer, field_name, value)

        await self._session.flush()
        self._invalidate_cache(payer_id)
        return payer

    async def deactivate_payer(self, payer_id: str) -> Payer:
        """Deactivate a payer (soft delete).

        Sets is_active=False on the payer record. The payer remains in
        the database for historical reference but is excluded from
        active queries by default.
        """
        payer = await self.get_payer(payer_id)
        payer.is_active = False
        await self._session.flush()
        self._invalidate_cache(payer_id)
        logger.info("Deactivated payer: %s (%s)", payer.name, payer_id)
        return payer

    async def delete_payer(self, payer_id: str) -> None:
        """Hard-delete a payer record.

        Permanently removes the payer from the database. Use
        deactivate_payer() for soft-delete semantics in production.

        Raises:
            PayerNotFoundError: If the payer does not exist.
        """
        payer = await self.get_payer(payer_id)
        await self._session.delete(payer)
        await self._session.flush()
        self._invalidate_cache(payer_id)
        logger.info("Deleted payer: %s (%s)", payer.name, payer_id)

    # ── Rule CRUD ───────────────────────────────────────────────────

    async def get_rules_for_payer(
        self,
        payer_id: str,
        *,
        agent_type: str | None = None,
        active_only: bool = True,
    ) -> list[PayerRule]:
        """Get rules for a payer, optionally filtered by agent type."""
        stmt = select(PayerRule).where(PayerRule.payer_id == payer_id)

        if agent_type:
            stmt = stmt.where(PayerRule.agent_type == agent_type)
        if active_only:
            stmt = stmt.where(PayerRule.is_active == True)  # noqa: E712

        stmt = stmt.order_by(PayerRule.rule_type, PayerRule.version.desc())
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def create_rule(
        self,
        *,
        payer_id: str,
        agent_type: str,
        rule_type: str,
        conditions: dict[str, Any],
        actions: dict[str, Any] | None = None,
        description: str | None = None,
        effective_date: Any,
        termination_date: Any | None = None,
        version: int = 1,
    ) -> PayerRule:
        """Create a new payer rule."""
        rule = PayerRule(
            payer_id=payer_id,
            agent_type=agent_type,
            rule_type=rule_type,
            conditions=conditions,
            actions=actions,
            description=description,
            effective_date=effective_date,
            termination_date=termination_date,
            version=version,
            is_active=True,
        )
        self._session.add(rule)
        await self._session.flush()
        logger.info(
            "Created rule %s for payer %s (agent=%s)",
            rule_type,
            payer_id,
            agent_type,
        )
        return rule

    # ── Clearinghouse Config ────────────────────────────────────────

    async def get_clearinghouse_config(
        self,
        organization_id: str,
    ) -> ClearinghouseConfig | None:
        """Get the active clearinghouse config for an organization."""
        stmt = (
            select(ClearinghouseConfig)
            .where(
                and_(
                    ClearinghouseConfig.organization_id == organization_id,
                    ClearinghouseConfig.is_active == True,  # noqa: E712
                )
            )
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()
