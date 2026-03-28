"""Pydantic schemas for payer and rule request/response models."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field


class PayerResponse(BaseModel):
    """Response for a payer record."""

    id: uuid.UUID
    name: str
    payer_id_code: str
    payer_type: str | None = None
    address: str | None = None
    phone: str | None = None
    electronic_payer_id: str | None = None
    is_active: bool = True
    created_at: datetime
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class PayerCreate(BaseModel):
    """Request to create a new payer."""

    name: str
    payer_id_code: str
    payer_type: str | None = None
    address: str | None = None
    phone: str | None = None
    electronic_payer_id: str | None = None


class PayerRuleResponse(BaseModel):
    """Response for a payer rule."""

    id: uuid.UUID
    payer_id: uuid.UUID
    agent_type: str
    rule_type: str
    description: str | None = None
    conditions: dict[str, Any]
    actions: dict[str, Any] | None = None
    effective_date: date
    termination_date: date | None = None
    version: int = 1
    is_active: bool = True
    created_at: datetime
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class PayerRuleCreate(BaseModel):
    """Request to create a new payer rule."""

    agent_type: str
    rule_type: str
    description: str | None = None
    conditions: dict[str, Any]
    actions: dict[str, Any] | None = None
    effective_date: date
    termination_date: date | None = None
    version: int = 1


class PayerRuleUpdate(BaseModel):
    """Request to update a payer rule."""

    conditions: dict[str, Any] | None = None
    actions: dict[str, Any] | None = None
    description: str | None = None
    termination_date: date | None = None
    is_active: bool | None = None


class PayerRuleEvaluationRequest(BaseModel):
    """Request to evaluate rules against a context."""

    context: dict[str, Any] = Field(description="Data to evaluate rules against")
    rule_type: str | None = None
