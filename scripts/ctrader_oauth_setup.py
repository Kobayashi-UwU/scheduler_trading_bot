"""One-time interactive setup: OAuth-authorize a cTrader ID and save per-account
BrokerToken rows for the real-tier executor.

Run this once (and again any time you add a new mirrored account, or if the
grant is ever revoked). It:
  1. Starts a local one-shot HTTP server on the configured redirect URI to catch
     the OAuth authorization code — no manual copy-pasting of the redirected URL.
  2. Prints the authorize URL — open it in a browser, log into the cTrader ID
     that owns the Axiory demo accounts, and approve access. This step is 100%
     manual: the operator's real broker login happens in their own browser, this
     script never sees it.
  3. Exchanges the code for an access/refresh token.
  4. Lists every trading account reachable with that token
     (ProtoOAGetAccountListByAccessTokenReq) and auto-matches by traderLogin
     against KNOWN_LOGINS below — falls back to an interactive prompt for
     anything unmatched.
  5. Writes one BrokerToken row per matched account.

The redirect URI must be added on the app's settings page at
https://openapi.ctrader.com/apps first (same host/port as --redirect-uri).

Usage:
  .venv/bin/python scripts/ctrader_oauth_setup.py [--redirect-uri http://localhost:8765/callback]
"""

import argparse
import datetime as dt
import http.server
import os
import sys
import time
import urllib.parse
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings  # noqa: E402
from app.db.database import get_session, init_db  # noqa: E402
from app.db.models import BrokerToken  # noqa: E402
from app.trading import ctrader_tokens  # noqa: E402
from app.trading.ctrader_client import CTRADER_PORT, CTraderClient, host_for  # noqa: E402

AUTHORIZE_URL = "https://id.ctrader.com/my/settings/openapi/grantingaccess/"

# traderLogin -> real-tier account name, for auto-matching discovered accounts
# to the ones this project mirrors. Update if you add/change mirrored accounts.
KNOWN_LOGINS = {
    8068629: "real_ai_selected",
    8068630: "real_trend_following",
}


class _CodeCatcher(http.server.BaseHTTPRequestHandler):
    code: str | None = None
    error: str | None = None

    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in params:
            _CodeCatcher.code = params["code"][0]
            body = b"Authorized. You can close this tab and return to the terminal."
        else:
            _CodeCatcher.error = params.get("error_description", params.get("error", ["unknown error"]))[0]
            body = b"Authorization failed. Check the terminal."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # quiet — don't spam stdout with HTTP access logs


def wait_for_code(redirect_uri: str, timeout: float = 300.0) -> str:
    parsed = urllib.parse.urlparse(redirect_uri)
    server = http.server.HTTPServer((parsed.hostname, parsed.port or 80), _CodeCatcher)
    deadline = time.monotonic() + timeout
    print(f"Listening on {redirect_uri} for the OAuth redirect (timeout {int(timeout)}s)...")
    try:
        while _CodeCatcher.code is None and _CodeCatcher.error is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SystemExit("Timed out waiting for the OAuth redirect")
            server.timeout = remaining
            server.handle_request()
    finally:
        server.server_close()
    if _CodeCatcher.error:
        raise SystemExit(f"OAuth authorization failed: {_CodeCatcher.error}")
    return _CodeCatcher.code


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--redirect-uri", default="http://localhost:8765/callback")
    parser.add_argument("--scope", default="trading")
    parser.add_argument("--no-browser", action="store_true", help="print the URL instead of opening it")
    args = parser.parse_args()

    client_id = settings.ctrader_client_id
    client_secret = settings.ctrader_client_secret
    if not client_id or not client_secret:
        raise SystemExit("Set CTRADER_CLIENT_ID and CTRADER_CLIENT_SECRET first (.env or shell env)")

    auth_url = (
        f"{AUTHORIZE_URL}?client_id={urllib.parse.quote(client_id)}"
        f"&redirect_uri={urllib.parse.quote(args.redirect_uri)}"
        f"&scope={args.scope}&product=web"
    )
    print("\nOpen this URL, log into the cTrader ID that owns the Axiory demo accounts, and approve access:\n")
    print(f"  {auth_url}\n")
    if not args.no_browser:
        webbrowser.open(auth_url)

    code = wait_for_code(args.redirect_uri)
    print("Got authorization code, exchanging for tokens...")
    tokens = ctrader_tokens.exchange_code(client_id, client_secret, code, args.redirect_uri)
    access_token = tokens["accessToken"]
    refresh_token = tokens["refreshToken"]
    expires_at = dt.datetime.utcnow() + dt.timedelta(seconds=tokens.get("expiresIn", 0))

    client = CTraderClient(host_for(settings.ctrader_environment), CTRADER_PORT, client_id, client_secret)
    if not client.connect():
        raise SystemExit("Could not connect/app-auth to the cTrader Open API — check Client ID/Secret and "
                          "CTRADER_ENVIRONMENT (demo vs live)")
    try:
        accounts = client.get_accounts_by_token(access_token)
    finally:
        client.close()

    if not accounts:
        raise SystemExit("No trading accounts found for this token/scope")

    print(f"\nFound {len(accounts)} account(s):")
    init_db()
    session = get_session()
    try:
        for acct in accounts:
            env = "live" if acct.is_live else "demo"
            name = KNOWN_LOGINS.get(acct.trader_login)
            if name is None:
                print(f"  login {acct.trader_login} (ctid {acct.ctid_trader_account_id}, {env}) — not in KNOWN_LOGINS")
                name = input("    account name to save this as (blank to skip): ").strip()
                if not name:
                    continue
            else:
                print(f"  login {acct.trader_login} (ctid {acct.ctid_trader_account_id}, {env}) -> {name}")

            existing = ctrader_tokens.get_token(session, name)
            if existing is not None:
                existing.ctid_trader_account_id = acct.ctid_trader_account_id
                existing.access_token = access_token
                existing.refresh_token = refresh_token
                existing.expires_at = expires_at
            else:
                session.add(BrokerToken(
                    account=name,
                    ctid_trader_account_id=acct.ctid_trader_account_id,
                    access_token=access_token,
                    refresh_token=refresh_token,
                    expires_at=expires_at,
                ))
        session.commit()
    finally:
        session.close()

    print("\nDone. Point EXECUTOR_ACCOUNTS at the same ctids, e.g.:")
    print('  EXECUTOR_ACCOUNTS={"real_ai_selected": {"ctid_trader_account_id": 8068629}, '
          '"real_trend_following": {"ctid_trader_account_id": 8068630}}')


if __name__ == "__main__":
    main()
