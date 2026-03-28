"""Audit log API routes — query logs with filters."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime
from typing import Any, Union

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession

from app.core.audit.logger import AuditLogger
from app.core.auth.jwt import TokenPayload
from app.core.auth.middleware import require_role
from app.dependencies import get_db
from app.models.audit import AuditLog, PHIAccessLog
from app.schemas.audit import (
    AuditFilterOptionsResponse,
    AuditLogItem,
    AuditLogListResponse,
    PHIAccessItem,
    PHIAccessListResponse,
)

router = APIRouter(prefix="/audit", tags=["audit"])


def _log_to_dict(log: AuditLog) -> dict[str, Any]:
    """Convert an AuditLog ORM instance to a serialisable dict."""
    return {
        "id": str(log.id),
        "timestamp": log.timestamp.isoformat() if log.timestamp else None,
        "actor_id": str(log.actor_id) if log.actor_id else None,
        "actor_type": log.actor_type,
        "action": log.action,
        "resource_type": log.resource_type,
        "resource_id": log.resource_id,
        "details": log.details,
        "phi_accessed": log.phi_accessed,
        "ip_address": log.ip_address,
    }


async def _fetch_audit_logs(
    session: AsyncSession,
    action: str | None,
    resource_type: str | None,
    actor_id: str | None,
    start_time: datetime | None,
    end_time: datetime | None,
    limit: int,
    offset: int,
    phi_accessed: bool | None = None,
    resource_id: str | None = None,
    search: str | None = None,
) -> tuple[list[AuditLog], int]:
    """Shared helper: fetch filtered audit logs and total count."""
    audit_logger = AuditLogger(session)

    parsed_actor_id: uuid.UUID | None = None
    if actor_id:
        try:
            parsed_actor_id = uuid.UUID(actor_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid actor_id UUID: '{actor_id}'",
            )

    effective_limit = min(limit, 500)
    logs = await audit_logger.query_logs(
        actor_id=parsed_actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        start_time=start_time,
        end_time=end_time,
        phi_accessed=phi_accessed,
        search=search,
        limit=effective_limit,
        offset=offset,
    )

    # Count total matching records for pagination metadata
    count_stmt = select(func.count()).select_from(AuditLog)
    if parsed_actor_id is not None:
        count_stmt = count_stmt.where(AuditLog.actor_id == parsed_actor_id)
    if action is not None:
        count_stmt = count_stmt.where(AuditLog.action == action)
    if resource_type is not None:
        count_stmt = count_stmt.where(AuditLog.resource_type == resource_type)
    if resource_id is not None:
        count_stmt = count_stmt.where(AuditLog.resource_id == resource_id)
    if start_time is not None:
        count_stmt = count_stmt.where(AuditLog.timestamp >= start_time)
    if end_time is not None:
        count_stmt = count_stmt.where(AuditLog.timestamp <= end_time)
    if phi_accessed is not None:
        count_stmt = count_stmt.where(AuditLog.phi_accessed == phi_accessed)
    if search:
        like_pattern = f"%{search}%"
        count_stmt = count_stmt.where(
            or_(
                AuditLog.action.ilike(like_pattern),
                AuditLog.resource_type.ilike(like_pattern),
                AuditLog.resource_id.ilike(like_pattern),
            )
        )
    count_result = await session.execute(count_stmt)
    total = count_result.scalar() or 0

    return logs, total


@router.get("/filter-options", response_model=AuditFilterOptionsResponse)
async def get_filter_options(
    current_user: TokenPayload = Depends(require_role("viewer")),
    session: AsyncSession = Depends(get_db),
) -> AuditFilterOptionsResponse:
    """Return distinct action and resource_type values for populating filter dropdowns."""
    actions_result = await session.execute(
        select(AuditLog.action).distinct().order_by(AuditLog.action)
    )
    resource_types_result = await session.execute(
        select(AuditLog.resource_type).distinct().order_by(AuditLog.resource_type)
    )
    return AuditFilterOptionsResponse(
        actions=[r for (r,) in actions_result.all() if r],
        resource_types=[r for (r,) in resource_types_result.all() if r],
    )


@router.get(
    "/logs",
    response_model=AuditLogListResponse,
    responses={
        200: {
            "description": "Audit log entries (JSON or CSV)",
            "content": {
                "application/json": {"schema": AuditLogListResponse.model_json_schema()},
                "text/csv": {"schema": {"type": "string", "format": "binary"}},
            },
        },
    },
)
async def query_audit_logs(
    action: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    actor_id: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    phi_accessed: bool | None = None,
    search: str | None = None,
    format: str | None = None,
    limit: int = 100,
    offset: int = 0,
    current_user: TokenPayload = Depends(require_role("viewer")),
    session: AsyncSession = Depends(get_db),
) -> Union[AuditLogListResponse, StreamingResponse]:
    """Query audit logs with filters.  Pass ``format=csv`` to download as CSV."""
    logs, total = await _fetch_audit_logs(
        session, action, resource_type, actor_id, start_time, end_time, limit, offset,
        phi_accessed=phi_accessed, resource_id=resource_id, search=search,
    )

    items_raw = [_log_to_dict(log) for log in logs]

    if format == "csv":
        csv_columns = [
            "id", "timestamp", "actor_id", "actor_type", "action",
            "resource_type", "resource_id", "phi_accessed", "ip_address", "details",
        ]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=csv_columns, extrasaction="ignore")
        writer.writeheader()
        for item in items_raw:
            row = {**item, "details": str(item.get("details") or "")}
            writer.writerow(row)
        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=audit-logs.csv"},
        )

    return AuditLogListResponse(
        items=[AuditLogItem(**item) for item in items_raw],
        total=total,
        limit=min(limit, 500),
        offset=offset,
    )


@router.get("/phi-access", response_model=PHIAccessListResponse)
async def query_phi_access(
    user_id: str | None = None,
    patient_id: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
    current_user: TokenPayload = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_db),
) -> PHIAccessListResponse:
    """Query PHI access logs (admin only)."""
    audit_logger = AuditLogger(session)

    parsed_user_id: uuid.UUID | None = None
    parsed_patient_id: uuid.UUID | None = None
    if user_id:
        try:
            parsed_user_id = uuid.UUID(user_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid user_id UUID: '{user_id}'",
            )
    if patient_id:
        try:
            parsed_patient_id = uuid.UUID(patient_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid patient_id UUID: '{patient_id}'",
            )

    effective_limit = min(limit, 500)
    logs = await audit_logger.query_phi_access(
        user_id=parsed_user_id,
        patient_id=parsed_patient_id,
        start_time=start_time,
        end_time=end_time,
        limit=effective_limit,
        offset=offset,
    )

    # Count total matching records
    count_stmt = select(func.count()).select_from(PHIAccessLog)
    if parsed_user_id is not None:
        count_stmt = count_stmt.where(PHIAccessLog.user_id == parsed_user_id)
    if parsed_patient_id is not None:
        count_stmt = count_stmt.where(PHIAccessLog.patient_id == parsed_patient_id)
    if start_time is not None:
        count_stmt = count_stmt.where(PHIAccessLog.timestamp >= start_time)
    if end_time is not None:
        count_stmt = count_stmt.where(PHIAccessLog.timestamp <= end_time)
    count_result = await session.execute(count_stmt)
    total = count_result.scalar() or 0

    return PHIAccessListResponse(
        items=[
            PHIAccessItem(
                id=str(log.id),
                timestamp=log.timestamp.isoformat() if log.timestamp else None,
                user_id=str(log.user_id),
                patient_id=str(log.patient_id) if log.patient_id else None,
                access_type=log.access_type,
                resource_type=log.resource_type,
                resource_id=log.resource_id,
                reason=log.reason,
                phi_fields_accessed=log.phi_fields_accessed,
            )
            for log in logs
        ],
        total=total,
        limit=effective_limit,
        offset=offset,
    )
