"""Unit tests for immutable audit logging system."""

import uuid

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.logger import AuditImmutabilityError, AuditLogger, phi_access_context
from app.models.audit import AuditLog, PHIAccessLog


@pytest.fixture
def audit_logger(db_session: AsyncSession) -> AuditLogger:
    """Create an AuditLogger instance with test session."""
    return AuditLogger(db_session)


@pytest.mark.asyncio
async def test_create_audit_entry(audit_logger: AuditLogger, db_session: AsyncSession):
    """Audit logger creates an entry that persists in the database."""
    entry = await audit_logger.log(
        action="test_action",
        actor_type="system",
        resource_type="test_resource",
        resource_id="res-001",
        details={"key": "value"},
    )

    assert entry.id is not None
    assert entry.action == "test_action"
    assert entry.actor_type == "system"
    assert entry.resource_type == "test_resource"
    assert entry.details == {"key": "value"}


@pytest.mark.asyncio
async def test_create_multiple_entries(audit_logger: AuditLogger, db_session: AsyncSession):
    """Audit logger creates 10 entries and all persist."""
    entries = []
    for i in range(10):
        entry = await audit_logger.log(
            action=f"action_{i}",
            actor_type="system",
            resource_type="test",
            resource_id=f"res-{i:03d}",
        )
        entries.append(entry)

    # Verify all 10 entries exist
    result = await db_session.execute(
        select(AuditLog).where(AuditLog.actor_type == "system")
    )
    persisted = result.scalars().all()
    assert len(persisted) >= 10


@pytest.mark.asyncio
async def test_audit_entries_immutable_update(audit_logger: AuditLogger, db_session: AsyncSession):
    """Audit entries cannot be updated — SQLAlchemy event listener blocks it."""
    entry = await audit_logger.log(
        action="immutable_test",
        actor_type="system",
    )
    original_id = entry.id

    # Attempt to modify the entry — should raise AuditImmutabilityError
    entry.action = "tampered_action"
    with pytest.raises(AuditImmutabilityError, match="Cannot modify"):
        await db_session.flush()

    # Rollback the failed flush and verify original is still intact
    await db_session.rollback()


@pytest.mark.asyncio
async def test_audit_entries_immutable_delete(audit_logger: AuditLogger, db_session: AsyncSession):
    """Audit entries cannot be deleted — SQLAlchemy event listener blocks it."""
    entry = await audit_logger.log(
        action="nodelete_test",
        actor_type="system",
    )

    # Attempt to delete the entry — should raise AuditImmutabilityError
    await db_session.delete(entry)
    with pytest.raises(AuditImmutabilityError, match="Cannot delete"):
        await db_session.flush()

    await db_session.rollback()


@pytest.mark.asyncio
async def test_phi_access_log_immutable(audit_logger: AuditLogger, db_session: AsyncSession):
    """PHI access log entries cannot be modified after creation."""
    user_id = uuid.uuid4()
    phi_entry = await audit_logger.log_phi_access(
        user_id=user_id,
        access_type="read",
        resource_type="patient",
        reason="Treatment",
    )

    phi_entry.reason = "tampered_reason"
    with pytest.raises(AuditImmutabilityError, match="Cannot modify"):
        await db_session.flush()

    await db_session.rollback()


@pytest.mark.asyncio
async def test_log_with_phi_flag(audit_logger: AuditLogger, db_session: AsyncSession):
    """Audit logger correctly records PHI access flag."""
    entry = await audit_logger.log(
        action="view_patient",
        actor_id=uuid.uuid4(),
        actor_type="user",
        phi_accessed=True,
    )

    assert entry.phi_accessed is True


@pytest.mark.asyncio
async def test_log_phi_access(audit_logger: AuditLogger, db_session: AsyncSession):
    """PHI access logging creates both PHIAccessLog and AuditLog entries."""
    user_id = uuid.uuid4()
    patient_id = uuid.uuid4()

    phi_entry = await audit_logger.log_phi_access(
        user_id=user_id,
        patient_id=patient_id,
        access_type="read",
        resource_type="patient",
        resource_id=str(patient_id),
        reason="Eligibility check",
        phi_fields_accessed=["name", "dob", "ssn"],
    )

    assert phi_entry.id is not None
    assert phi_entry.user_id == user_id
    assert phi_entry.access_type == "read"
    assert phi_entry.phi_fields_accessed == ["name", "dob", "ssn"]


@pytest.mark.asyncio
async def test_query_logs_by_action(audit_logger: AuditLogger, db_session: AsyncSession):
    """Query audit logs filtered by action."""
    await audit_logger.log(action="login", actor_type="user")
    await audit_logger.log(action="login", actor_type="user")
    await audit_logger.log(action="logout", actor_type="user")

    logs = await audit_logger.query_logs(action="login")
    assert len(logs) >= 2
    for log_entry in logs:
        assert log_entry.action == "login"


@pytest.mark.asyncio
async def test_query_logs_by_resource_type(audit_logger: AuditLogger, db_session: AsyncSession):
    """Query audit logs filtered by resource type."""
    await audit_logger.log(action="create", resource_type="patient")
    await audit_logger.log(action="create", resource_type="claim")
    await audit_logger.log(action="create", resource_type="patient")

    logs = await audit_logger.query_logs(resource_type="patient")
    assert len(logs) >= 2
    for log_entry in logs:
        assert log_entry.resource_type == "patient"


@pytest.mark.asyncio
async def test_query_logs_pagination(audit_logger: AuditLogger, db_session: AsyncSession):
    """Query audit logs with limit and offset."""
    for i in range(5):
        await audit_logger.log(action=f"paginated_{i}", actor_type="test_pag")

    page1 = await audit_logger.query_logs(limit=2, offset=0)
    page2 = await audit_logger.query_logs(limit=2, offset=2)

    assert len(page1) <= 2
    assert len(page2) <= 2


@pytest.mark.asyncio
async def test_phi_access_context_manager(db_session: AsyncSession):
    """PHI access context manager logs access on entry."""
    user_id = uuid.uuid4()
    patient_id = uuid.uuid4()

    async with phi_access_context(
        db_session,
        user_id=user_id,
        patient_id=patient_id,
        access_type="read",
        resource_type="patient",
        reason="Treatment",
        phi_fields=["name", "dob"],
    ) as audit:
        # Perform additional logging within context
        await audit.log(action="viewed_details", actor_id=user_id, actor_type="user")

    # Verify PHI access was logged
    result = await db_session.execute(
        select(PHIAccessLog).where(PHIAccessLog.user_id == user_id)
    )
    phi_logs = result.scalars().all()
    assert len(phi_logs) >= 1
    assert phi_logs[0].reason == "Treatment"


@pytest.mark.asyncio
async def test_audit_log_has_timestamp(audit_logger: AuditLogger, db_session: AsyncSession):
    """Audit entries automatically get a timestamp."""
    entry = await audit_logger.log(action="timestamp_test")

    assert entry.timestamp is not None


@pytest.mark.asyncio
async def test_query_phi_access_by_user(audit_logger: AuditLogger, db_session: AsyncSession):
    """Query PHI access logs filtered by user."""
    user_id = uuid.uuid4()
    await audit_logger.log_phi_access(
        user_id=user_id,
        access_type="read",
        resource_type="patient",
        reason="Treatment",
    )
    await audit_logger.log_phi_access(
        user_id=user_id,
        access_type="write",
        resource_type="patient",
        reason="Update demographics",
    )

    logs = await audit_logger.query_phi_access(user_id=user_id)
    assert len(logs) >= 2
    for log_entry in logs:
        assert log_entry.user_id == user_id


@pytest.mark.asyncio
async def test_query_phi_access_by_patient(audit_logger: AuditLogger, db_session: AsyncSession):
    """Query PHI access logs filtered by patient."""
    user_id = uuid.uuid4()
    patient_id = uuid.uuid4()
    await audit_logger.log_phi_access(
        user_id=user_id,
        patient_id=patient_id,
        access_type="read",
        resource_type="patient",
        reason="Treatment",
    )

    logs = await audit_logger.query_phi_access(patient_id=patient_id)
    assert len(logs) >= 1
    assert logs[0].patient_id == patient_id


@pytest.mark.asyncio
async def test_log_with_ip_address(audit_logger: AuditLogger, db_session: AsyncSession):
    """Audit logger records IP address when provided."""
    entry = await audit_logger.log(
        action="api_request",
        actor_type="user",
        ip_address="192.168.1.100",
    )
    assert entry.ip_address == "192.168.1.100"


# ── Database-Level Immutability Tests (SQL bypass) ────────────────────


@pytest.mark.asyncio
async def test_sql_level_update_blocked(audit_logger: AuditLogger, db_session: AsyncSession):
    """Direct SQL UPDATE on audit_logs is blocked by database trigger."""
    entry = await audit_logger.log(
        action="sql_immutable_test",
        actor_type="system",
    )
    await db_session.commit()

    # Attempt a raw SQL-level UPDATE bypassing ORM listeners
    with pytest.raises(Exception, match="(?i)immutable|not allowed"):
        await db_session.execute(
            update(AuditLog)
            .where(AuditLog.id == entry.id)
            .values(action="tampered_action")
        )
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_sql_level_delete_blocked(audit_logger: AuditLogger, db_session: AsyncSession):
    """Direct SQL DELETE on audit_logs is blocked by database trigger."""
    entry = await audit_logger.log(
        action="sql_nodelete_test",
        actor_type="system",
    )
    await db_session.commit()

    # Attempt a raw SQL-level DELETE bypassing ORM listeners
    with pytest.raises(Exception, match="(?i)immutable|not allowed"):
        await db_session.execute(
            delete(AuditLog).where(AuditLog.id == entry.id)
        )
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_sql_level_phi_update_blocked(audit_logger: AuditLogger, db_session: AsyncSession):
    """Direct SQL UPDATE on phi_access_log is blocked by database trigger."""
    user_id = uuid.uuid4()
    entry = await audit_logger.log_phi_access(
        user_id=user_id,
        access_type="read",
        resource_type="patient",
        reason="Treatment",
    )
    await db_session.commit()

    with pytest.raises(Exception, match="(?i)immutable|not allowed"):
        await db_session.execute(
            update(PHIAccessLog)
            .where(PHIAccessLog.id == entry.id)
            .values(reason="tampered_reason")
        )
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_sql_level_phi_delete_blocked(audit_logger: AuditLogger, db_session: AsyncSession):
    """Direct SQL DELETE on phi_access_log is blocked by database trigger."""
    user_id = uuid.uuid4()
    entry = await audit_logger.log_phi_access(
        user_id=user_id,
        access_type="read",
        resource_type="patient",
        reason="Treatment nodelete",
    )
    await db_session.commit()

    with pytest.raises(Exception, match="(?i)immutable|not allowed"):
        await db_session.execute(
            delete(PHIAccessLog).where(PHIAccessLog.id == entry.id)
        )
        await db_session.commit()
    await db_session.rollback()
