"""User provisioning and management service.

Handles auto-creation of user records on first SSO login, role mapping
from IdP attributes, and user lookup/update operations.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.user import User

logger = logging.getLogger(__name__)


def map_role_from_idp(idp_attributes: dict[str, Any]) -> str:
    """Map an IdP role attribute value to a Slate Health role.

    Checks the configured role attribute name in the IdP attributes dict.
    Values are matched against comma-separated admin/reviewer value lists
    in settings. Falls back to 'viewer' for unrecognized values.

    Args:
        idp_attributes: Dict of IdP attribute names → values.
            Values can be a string or a list of strings.

    Returns:
        Role string: 'admin', 'reviewer', or 'viewer'.
    """
    role_attr = settings.sso_role_attribute
    raw_value = idp_attributes.get(role_attr)
    if raw_value is None:
        # Also check common alternative attribute names
        for alt_key in ("Role", "roles", "Roles", "groups", "Groups",
                        "http://schemas.microsoft.com/ws/2008/06/identity/claims/role"):
            raw_value = idp_attributes.get(alt_key)
            if raw_value is not None:
                break

    if raw_value is None:
        return "viewer"

    # Normalize to a set of lowercase values
    if isinstance(raw_value, list):
        values = {v.lower().strip() for v in raw_value if isinstance(v, str)}
    elif isinstance(raw_value, str):
        values = {raw_value.lower().strip()}
    else:
        return "viewer"

    admin_values = {v.strip().lower() for v in settings.sso_admin_values.split(",") if v.strip()}
    reviewer_values = {v.strip().lower() for v in settings.sso_reviewer_values.split(",") if v.strip()}

    if values & admin_values:
        return "admin"
    if values & reviewer_values:
        return "reviewer"

    return "viewer"


async def provision_user(
    db: AsyncSession,
    *,
    email: str,
    full_name: str,
    sso_provider: str,
    sso_subject_id: str,
    idp_attributes: dict[str, Any] | None = None,
    organization_id: uuid.UUID | None = None,
) -> tuple[User, bool]:
    """Find or create a user record from SSO login data.

    On first login, a new User is created with the role mapped from IdP
    attributes. On subsequent logins, the user's last_login is updated
    and full_name is synced from the IdP.

    Args:
        db: Async database session.
        email: User's email address.
        full_name: User's display name from IdP.
        sso_provider: SSO provider identifier ('saml' or 'oidc').
        sso_subject_id: Unique subject identifier from IdP.
        idp_attributes: Raw attributes from IdP for role mapping.
        organization_id: Optional organization to associate.

    Returns:
        Tuple of (User, is_new) where is_new indicates first-time creation.
    """
    idp_attributes = idp_attributes or {}

    # Look up by sso_provider + sso_subject_id first (most reliable)
    stmt = select(User).where(
        User.sso_provider == sso_provider,
        User.sso_subject_id == sso_subject_id,
    )
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is not None:
        # Existing user — update last_login and sync name
        user.last_login = datetime.now(timezone.utc)
        if full_name and full_name != user.full_name:
            user.full_name = full_name
        await db.flush()
        logger.info("SSO login: existing user %s (provider=%s)", user.email, sso_provider)
        return user, False

    # Check by email as fallback (user may have been pre-provisioned)
    stmt = select(User).where(User.email == email)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is not None:
        # Link existing email-based user to SSO
        user.sso_provider = sso_provider
        user.sso_subject_id = sso_subject_id
        user.last_login = datetime.now(timezone.utc)
        if full_name and full_name != user.full_name:
            user.full_name = full_name
        await db.flush()
        logger.info("SSO login: linked existing user %s to provider=%s", user.email, sso_provider)
        return user, False

    # New user — provision with role mapping
    role = map_role_from_idp(idp_attributes)
    user = User(
        id=uuid.uuid4(),
        email=email,
        full_name=full_name or email,
        role=role,
        sso_provider=sso_provider,
        sso_subject_id=sso_subject_id,
        organization_id=organization_id,
        last_login=datetime.now(timezone.utc),
        is_active=True,
    )
    db.add(user)
    await db.flush()

    logger.info(
        "SSO login: new user provisioned email=%s, role=%s, provider=%s",
        user.email, user.role, sso_provider,
    )
    return user, True
