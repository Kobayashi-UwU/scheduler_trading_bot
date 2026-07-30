"""OAuth token lifecycle for the cTrader Open API.

Tokens live in the `BrokerToken` DB table (not env vars): a cTrader access token
is per-account and rotates roughly every ~30 days, so storing it in the DB lets
the executor refresh and persist it itself instead of requiring a manual Railway
variable edit + redeploy every rotation.
"""

import datetime as dt
import logging

import httpx
from sqlalchemy.orm import Session

from app.db.models import BrokerToken

log = logging.getLogger("ctrader_tokens")

TOKEN_URL = "https://openapi.ctrader.com/apps/token"


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    """One-time: swap an OAuth authorization code for access/refresh tokens."""
    resp = httpx.get(
        TOKEN_URL,
        params={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


def _refresh(client_id: str, client_secret: str, refresh_token: str) -> dict:
    resp = httpx.get(
        TOKEN_URL,
        params={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


def get_token(session: Session, account: str) -> BrokerToken | None:
    return session.query(BrokerToken).filter(BrokerToken.account == account).first()


def refresh_if_needed(session: Session, token: BrokerToken, client_id: str, client_secret: str,
                       margin_days: int) -> BrokerToken:
    """Proactively refresh when close to expiry, rather than waiting for an auth
    failure and trying to sniff an undocumented error code out of it."""
    margin = dt.timedelta(days=margin_days)
    if token.expires_at - dt.datetime.utcnow() > margin:
        return token
    try:
        data = _refresh(client_id, client_secret, token.refresh_token)
    except httpx.HTTPError as exc:
        log.error("token refresh failed for %s: %s", token.account, exc)
        return token

    new_access = data["accessToken"]
    new_refresh = data.get("refreshToken", token.refresh_token)
    new_expiry = dt.datetime.utcnow() + dt.timedelta(seconds=data.get("expiresIn", 0))

    # Multiple real-tier accounts can share one underlying OAuth grant (one
    # cTrader ID can hold several trading accounts, all authorized by the same
    # access/refresh token pair). Update every BrokerToken row still on the old
    # token together, so a sibling account never tries to reuse a refresh_token
    # that's already been rotated out from under it by this call.
    old_access_token = token.access_token
    session.query(BrokerToken).filter(BrokerToken.access_token == old_access_token).update({
        "access_token": new_access,
        "refresh_token": new_refresh,
        "expires_at": new_expiry,
    })
    session.commit()
    session.refresh(token)
    log.info("refreshed cTrader token for %s (new expiry %s)", token.account, token.expires_at.isoformat())
    return token
