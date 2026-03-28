"""Payer and payer rules API routes — CRUD for payers and their rules."""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.jwt import TokenPayload
from app.core.auth.middleware import require_role
from app.dependencies import get_db
from app.models.payer import Payer, PayerRule
from app.schemas.payer import (
    PayerCreate,
    PayerResponse,
    PayerRuleCreate,
    PayerRuleResponse,
    PayerRuleUpdate,
)

router = APIRouter(prefix="/payers", tags=["payers"])


def _payer_to_response(p: Payer) -> PayerResponse:
    from sqlalchemy import inspect as sa_inspect

    d = sa_inspect(p).dict
    return PayerResponse(
        id=d.get("id", p.id),
        name=d.get("name", ""),
        payer_id_code=d.get("payer_id_code", ""),
        payer_type=d.get("payer_type"),
        address=d.get("address"),
        phone=d.get("phone"),
        electronic_payer_id=d.get("electronic_payer_id"),
        is_active=d.get("is_active", True),
        created_at=d.get("created_at"),
        updated_at=d.get("updated_at"),
    )


def _rule_to_response(r: PayerRule) -> PayerRuleResponse:
    from sqlalchemy import inspect as sa_inspect

    d = sa_inspect(r).dict
    return PayerRuleResponse(
        id=d.get("id", r.id),
        payer_id=d.get("payer_id"),
        agent_type=d.get("agent_type", ""),
        rule_type=d.get("rule_type", ""),
        description=d.get("description"),
        conditions=d.get("conditions", {}),
        actions=d.get("actions"),
        effective_date=d.get("effective_date"),
        termination_date=d.get("termination_date"),
        version=d.get("version", 1),
        is_active=d.get("is_active", True),
        created_at=d.get("created_at"),
        updated_at=d.get("updated_at"),
    )


@router.get("", response_model=list[PayerResponse])
async def list_payers(
    current_user: TokenPayload = Depends(require_role("viewer")),
    session: AsyncSession = Depends(get_db),
) -> list[PayerResponse]:
    """List all payers."""
    result = await session.execute(
        select(Payer).where(Payer.is_active.is_(True)).order_by(Payer.name)
    )
    payers = list(result.scalars().all())
    return [_payer_to_response(p) for p in payers]


@router.post("", response_model=PayerResponse, status_code=status.HTTP_201_CREATED)
async def create_payer(
    body: PayerCreate,
    current_user: TokenPayload = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_db),
) -> PayerResponse:
    """Create a new payer."""
    payer = Payer(
        name=body.name,
        payer_id_code=body.payer_id_code,
        payer_type=body.payer_type,
        address=body.address,
        phone=body.phone,
        electronic_payer_id=body.electronic_payer_id,
    )
    session.add(payer)
    await session.flush()
    return _payer_to_response(payer)


@router.get("/{payer_id}", response_model=PayerResponse)
async def get_payer(
    payer_id: uuid.UUID,
    current_user: TokenPayload = Depends(require_role("viewer")),
    session: AsyncSession = Depends(get_db),
) -> PayerResponse:
    """Get a single payer by ID."""
    result = await session.execute(
        select(Payer).where(Payer.id == payer_id)
    )
    payer = result.scalar_one_or_none()
    if payer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Payer '{payer_id}' not found",
        )
    return _payer_to_response(payer)


@router.put("/{payer_id}", response_model=PayerResponse)
async def update_payer(
    payer_id: uuid.UUID,
    body: PayerCreate,
    current_user: TokenPayload = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_db),
) -> PayerResponse:
    """Update an existing payer."""
    result = await session.execute(
        select(Payer).where(Payer.id == payer_id)
    )
    payer = result.scalar_one_or_none()
    if payer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Payer '{payer_id}' not found",
        )
    payer.name = body.name
    payer.payer_id_code = body.payer_id_code
    if body.payer_type is not None:
        payer.payer_type = body.payer_type
    if body.address is not None:
        payer.address = body.address
    if body.phone is not None:
        payer.phone = body.phone
    if body.electronic_payer_id is not None:
        payer.electronic_payer_id = body.electronic_payer_id
    await session.flush()
    return _payer_to_response(payer)


@router.delete("/{payer_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_payer(
    payer_id: uuid.UUID,
    current_user: TokenPayload = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_db),
) -> None:
    """Soft-delete a payer (set is_active=False)."""
    result = await session.execute(
        select(Payer).where(Payer.id == payer_id)
    )
    payer = result.scalar_one_or_none()
    if payer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Payer '{payer_id}' not found",
        )
    payer.is_active = False
    await session.flush()


@router.get("/{payer_id}/rules", response_model=list[PayerRuleResponse])
async def list_payer_rules(
    payer_id: uuid.UUID,
    agent_type: str | None = None,
    current_user: TokenPayload = Depends(require_role("viewer")),
    session: AsyncSession = Depends(get_db),
) -> list[PayerRuleResponse]:
    """List rules for a specific payer."""
    # Verify payer exists
    payer_result = await session.execute(
        select(Payer).where(Payer.id == payer_id)
    )
    if payer_result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Payer '{payer_id}' not found",
        )

    query = select(PayerRule).where(
        PayerRule.payer_id == payer_id,
        PayerRule.is_active.is_(True),
    )
    if agent_type:
        query = query.where(PayerRule.agent_type == agent_type)

    query = query.order_by(PayerRule.effective_date.desc())
    result = await session.execute(query)
    rules = list(result.scalars().all())
    return [_rule_to_response(r) for r in rules]


@router.post(
    "/{payer_id}/rules",
    response_model=PayerRuleResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_payer_rule(
    payer_id: uuid.UUID,
    body: PayerRuleCreate,
    current_user: TokenPayload = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_db),
) -> PayerRuleResponse:
    """Create a new rule for a payer."""
    # Verify payer exists
    payer_result = await session.execute(
        select(Payer).where(Payer.id == payer_id)
    )
    if payer_result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Payer '{payer_id}' not found",
        )

    rule = PayerRule(
        payer_id=payer_id,
        agent_type=body.agent_type,
        rule_type=body.rule_type,
        description=body.description,
        conditions=body.conditions,
        actions=body.actions,
        effective_date=body.effective_date,
        termination_date=body.termination_date,
        version=body.version,
    )
    session.add(rule)
    await session.flush()
    return _rule_to_response(rule)


@router.put("/{payer_id}/rules/{rule_id}", response_model=PayerRuleResponse)
async def update_payer_rule(
    payer_id: uuid.UUID,
    rule_id: uuid.UUID,
    body: PayerRuleUpdate,
    current_user: TokenPayload = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_db),
) -> PayerRuleResponse:
    """Update an existing payer rule."""
    result = await session.execute(
        select(PayerRule).where(
            PayerRule.id == rule_id,
            PayerRule.payer_id == payer_id,
        )
    )
    rule = result.scalar_one_or_none()
    if rule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Rule '{rule_id}' not found for payer '{payer_id}'",
        )

    if body.conditions is not None:
        rule.conditions = body.conditions
    if body.actions is not None:
        rule.actions = body.actions
    if body.description is not None:
        rule.description = body.description
    if body.termination_date is not None:
        rule.termination_date = body.termination_date
    if body.is_active is not None:
        rule.is_active = body.is_active

    await session.flush()
    return _rule_to_response(rule)


@router.delete(
    "/{payer_id}/rules/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_payer_rule(
    payer_id: uuid.UUID,
    rule_id: uuid.UUID,
    current_user: TokenPayload = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_db),
) -> None:
    """Soft-delete a payer rule (set is_active=False)."""
    result = await session.execute(
        select(PayerRule).where(
            PayerRule.id == rule_id,
            PayerRule.payer_id == payer_id,
        )
    )
    rule = result.scalar_one_or_none()
    if rule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Rule '{rule_id}' not found for payer '{payer_id}'",
        )
    rule.is_active = False
    await session.flush()
