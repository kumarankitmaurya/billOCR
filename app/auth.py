"""The admin gate: an `X-Admin-Password` header checked against ADMIN_PASSWORD.

Admin unlocks base price (`rate`) and catalog indexing. The comparison is
constant-time, and admin is disabled outright when ADMIN_PASSWORD is empty,
so an empty header can never match an empty password.
"""

import secrets

from fastapi import Depends, Header, HTTPException

from app.config import settings


def admin_access(x_admin_password: str | None = Header(None)) -> bool:
    """True for a valid admin header, False when none was sent.

    A header that's present but wrong is a 401, not a silent downgrade to
    public access — otherwise a typo'd password would look like "no rate
    data" rather than an error.
    """
    if x_admin_password is None:
        return False
    if not settings.admin_password or not secrets.compare_digest(
        x_admin_password, settings.admin_password
    ):
        raise HTTPException(status_code=401, detail="Invalid admin password")
    return True


def require_admin(is_admin: bool = Depends(admin_access)) -> None:
    """For endpoints that are admin-only, not just admin-enhanced."""
    if not is_admin:
        raise HTTPException(
            status_code=401, detail="Admin authentication required (X-Admin-Password header)"
        )
