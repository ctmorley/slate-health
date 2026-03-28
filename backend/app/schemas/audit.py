"""Pydantic schemas for audit log API responses."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AuditLogItem(BaseModel):
    """Single audit log entry."""
    id: str
    timestamp: str | None = None
    actor_id: str | None = None
    actor_type: str | None = None
    action: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    phi_accessed: bool | None = None
    ip_address: str | None = None
    details: dict[str, Any] | None = None


class AuditLogListResponse(BaseModel):
    """Paginated list of audit log entries."""
    items: list[AuditLogItem]
    total: int
    limit: int
    offset: int


class AuditFilterOptionsResponse(BaseModel):
    """Available filter options for audit log queries."""
    actions: list[str]
    resource_types: list[str]


class PHIAccessItem(BaseModel):
    """Single PHI access log entry."""
    id: str
    timestamp: str | None = None
    user_id: str
    patient_id: str | None = None
    access_type: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    reason: str | None = None
    phi_fields_accessed: list[str] | None = None


class PHIAccessListResponse(BaseModel):
    """Paginated list of PHI access log entries."""
    items: list[PHIAccessItem]
    total: int
    limit: int
    offset: int
