"""The two gates in front of the API.

`require_app_access` (X-App-Password / APP_PASSWORD) decides whether a caller
may talk to the service at all, and is applied to every endpoint. `admin_access`
(X-Admin-Password / ADMIN_PASSWORD) decides whether that caller additionally
sees base price (`rate`) — what the shop paid, which staff should not see.

Both comparisons are constant-time, and both treat an empty configured password
as "no value can match" rather than "anything matches", so a missing
environment variable can never accidentally open a gate.
"""

import logging
import secrets

from fastapi import Depends, Header, HTTPException

from app.config import settings

logger = logging.getLogger(__name__)


def require_app_access(x_app_password: str | None = Header(None)) -> None:
    """Gate every endpoint behind APP_PASSWORD.

    Fails closed when APP_PASSWORD is unset: without this the entire book of
    record — every supplier, product, bill number and selling price — is
    readable by anyone who finds the URL, and anyone can overwrite it through
    /ingest or spend the OCR quota through /preview. An unconfigured service
    refusing to serve is a far better failure than a silently public one.
    """
    if not settings.app_password:
        logger.error("APP_PASSWORD is not set — refusing every request")
        raise HTTPException(
            status_code=503,
            detail=(
                "This service has no APP_PASSWORD configured and is refusing "
                "requests. Set it in the environment (see .env.example)."
            ),
        )
    if x_app_password is None or not secrets.compare_digest(
        x_app_password, settings.app_password
    ):
        # Which of the two it was, but never the value itself. A burst of
        # "missing" means a client that was never configured; a burst of
        # "wrong" means a stale password or someone guessing.
        logger.warning(
            "rejected: %s X-App-Password",
            "missing" if x_app_password is None else "wrong",
        )
        raise HTTPException(
            status_code=401, detail="Missing or invalid X-App-Password header"
        )


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
        logger.warning("rejected: wrong X-Admin-Password (base price stays hidden)")
        raise HTTPException(status_code=401, detail="Invalid admin password")
    return True


def require_admin(is_admin: bool = Depends(admin_access)) -> None:
    """For endpoints that are admin-only, not just admin-enhanced."""
    if not is_admin:
        raise HTTPException(
            status_code=401, detail="Admin authentication required (X-Admin-Password header)"
        )
