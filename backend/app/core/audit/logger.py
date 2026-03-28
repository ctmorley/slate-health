"""Immutable audit logging to audit_logs and phi_access_log tables.

Provides an append-only audit trail with context manager for PHI access tracking.
Audit entries are write-once — updates and deletes are not supported.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

from sqlalchemy import event, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session as SyncSession

from app.models.audit import AuditLog, PHIAccessLog

logger = logging.getLogger(__name__)


class AuditImmutabilityError(Exception):
    """Raised when attempting to modify an existing audit entry."""
    pass


# ── Database-Level Immutability Enforcement ──────────────────────────


def _block_audit_update(mapper, connection, target):
    """SQLAlchemy event listener that prevents updates to audit log entries."""
    raise AuditImmutabilityError(
        f"Cannot modify audit log entry {target.id}: audit records are immutable"
    )


def _block_audit_delete(mapper, connection, target):
    """SQLAlchemy event listener that prevents deletion of audit log entries."""
    raise AuditImmutabilityError(
        f"Cannot delete audit log entry {target.id}: audit records are immutable"
    )


# Register event listeners for immutability enforcement on both audit tables
event.listen(AuditLog, "before_update", _block_audit_update)
event.listen(AuditLog, "before_delete", _block_audit_delete)
event.listen(PHIAccessLog, "before_update", _block_audit_update)
event.listen(PHIAccessLog, "before_delete", _block_audit_delete)


class AuditLogger:
    """Append-only audit logger backed by the audit_logs and phi_access_log tables.

    All write operations create new records; modification of existing records is
    explicitly forbidden to maintain HIPAA compliance and audit trail integrity.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def log(
        self,
        *,
        action: str,
        actor_id: uuid.UUID | None = None,
        actor_type: str = "system",
        resource_type: str | None = None,
        resource_id: str | None = None,
        details: dict[str, Any] | None = None,
        phi_accessed: bool = False,
        ip_address: str | None = None,
    ) -> AuditLog:
        """Create an immutable audit log entry.

        Args:
            action: Description of the action performed.
            actor_id: UUID of the user or system performing the action.
            actor_type: Type of actor ('user', 'system', 'agent').
            resource_type: Type of resource affected.
            resource_id: ID of the resource affected.
            details: Additional context as JSON.
            phi_accessed: Whether PHI was accessed in this action.
            ip_address: IP address of the requester.

        Returns:
            The created AuditLog entry.
        """
        entry = AuditLog(
            actor_id=actor_id,
            actor_type=actor_type,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details,
            phi_accessed=phi_accessed,
            ip_address=ip_address,
        )
        self._session.add(entry)
        await self._session.flush()
        logger.info(
            "Audit: %s by %s/%s on %s/%s (phi=%s)",
            action, actor_type, actor_id, resource_type, resource_id, phi_accessed,
        )
        return entry

    async def log_phi_access(
        self,
        *,
        user_id: uuid.UUID,
        patient_id: uuid.UUID | None = None,
        access_type: str,
        resource_type: str,
        resource_id: str | None = None,
        reason: str,
        phi_fields_accessed: list[str] | None = None,
    ) -> PHIAccessLog:
        """Log a specific PHI access event for HIPAA compliance.

        Args:
            user_id: UUID of the user accessing PHI.
            patient_id: UUID of the patient whose PHI is accessed.
            access_type: Type of access ('read', 'write', 'export').
            resource_type: Type of resource containing PHI.
            resource_id: ID of the specific resource.
            reason: Business justification for accessing PHI.
            phi_fields_accessed: List of specific PHI field names accessed.

        Returns:
            The created PHIAccessLog entry.
        """
        entry = PHIAccessLog(
            user_id=user_id,
            patient_id=patient_id,
            access_type=access_type,
            resource_type=resource_type,
            resource_id=resource_id,
            reason=reason,
            phi_fields_accessed=phi_fields_accessed,
        )
        self._session.add(entry)
        await self._session.flush()

        # Also create a corresponding audit log entry
        await self.log(
            action=f"phi_access:{access_type}",
            actor_id=user_id,
            actor_type="user",
            resource_type=resource_type,
            resource_id=resource_id,
            details={"phi_fields": phi_fields_accessed, "reason": reason},
            phi_accessed=True,
        )

        return entry

    async def query_logs(
        self,
        *,
        actor_id: uuid.UUID | None = None,
        action: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        phi_accessed: bool | None = None,
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditLog]:
        """Query audit logs with filters.

        Args:
            search: Free-text search across action, resource_type, and resource_id.

        Returns:
            List of matching AuditLog entries, ordered by timestamp descending.
        """
        stmt = select(AuditLog).order_by(AuditLog.timestamp.desc())

        if actor_id is not None:
            stmt = stmt.where(AuditLog.actor_id == actor_id)
        if action is not None:
            stmt = stmt.where(AuditLog.action == action)
        if resource_type is not None:
            stmt = stmt.where(AuditLog.resource_type == resource_type)
        if resource_id is not None:
            stmt = stmt.where(AuditLog.resource_id == resource_id)
        if start_time is not None:
            stmt = stmt.where(AuditLog.timestamp >= start_time)
        if end_time is not None:
            stmt = stmt.where(AuditLog.timestamp <= end_time)
        if phi_accessed is not None:
            stmt = stmt.where(AuditLog.phi_accessed == phi_accessed)
        if search:
            like_pattern = f"%{search}%"
            stmt = stmt.where(
                or_(
                    AuditLog.action.ilike(like_pattern),
                    AuditLog.resource_type.ilike(like_pattern),
                    AuditLog.resource_id.ilike(like_pattern),
                )
            )

        stmt = stmt.offset(offset).limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def query_phi_access(
        self,
        *,
        user_id: uuid.UUID | None = None,
        patient_id: uuid.UUID | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[PHIAccessLog]:
        """Query PHI access logs with filters."""
        stmt = select(PHIAccessLog).order_by(PHIAccessLog.timestamp.desc())

        if user_id is not None:
            stmt = stmt.where(PHIAccessLog.user_id == user_id)
        if patient_id is not None:
            stmt = stmt.where(PHIAccessLog.patient_id == patient_id)
        if start_time is not None:
            stmt = stmt.where(PHIAccessLog.timestamp >= start_time)
        if end_time is not None:
            stmt = stmt.where(PHIAccessLog.timestamp <= end_time)

        stmt = stmt.offset(offset).limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())


@asynccontextmanager
async def phi_access_context(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    patient_id: uuid.UUID | None = None,
    access_type: str,
    resource_type: str,
    resource_id: str | None = None,
    reason: str,
    phi_fields: list[str] | None = None,
) -> AsyncGenerator[AuditLogger, None]:
    """Context manager that automatically logs PHI access on entry.

    Usage:
        async with phi_access_context(session, user_id=uid, ...) as audit:
            # Access PHI here
            patient = await get_patient(patient_id)
            # Additional audit logging as needed
            await audit.log(action="viewed_patient_details", ...)
    """
    audit_logger = AuditLogger(session)
    await audit_logger.log_phi_access(
        user_id=user_id,
        patient_id=patient_id,
        access_type=access_type,
        resource_type=resource_type,
        resource_id=resource_id,
        reason=reason,
        phi_fields_accessed=phi_fields,
    )
    yield audit_logger
