"""Mirror real-tier simulated positions onto live cTrader accounts (Axiory).

Design: **reconciliation, not event-forwarding.** Every sync compares the desired
state (the sim's open position per real account, straight from the DB) with the
actual state (broker positions tagged with our own label) and issues the minimal
orders to converge. The executor is therefore stateless across restarts: nothing
is lost if it or the whole service dies — SL/TP live server-side at the broker,
and the next sync re-derives everything.

Price-feed basis: the bot's levels come from Yahoo GC=F (COMEX futures), which
trades a few dollars away from broker spot XAUUSD. Absolute prices are therefore
never copied — orders open at market, and SL/TP are re-derived by preserving the
sim trade's *stop/target distances* around the actual fill price, which keeps the
dollar risk identical across the basis gap.

Talks to the broker via the cTrader Open API (see app/trading/ctrader_client.py —
raw protobuf over a TLS socket, no Twisted/terminal-container needed, unlike the
old MT5/Wine approach). A fresh connection is opened per sync pass: the whole
reconciliation round trip for 1-2 accounts takes a few seconds, well under the
API's ~30s idle-disconnect window, so no persistent connection/heartbeat thread
is needed. One connection authorizes every configured account in turn (the API
supports many account-auths per connection), then the client is closed.

Everything here talks to a small client interface so tests can inject a mock
(see scripts/verify_ctrader_executor.py) instead of a live socket.
"""

import datetime as dt
import json
import logging
import threading

from app.config import settings
from app.db.database import get_session
from app.trading import ctrader_tokens
from app.trading.ctrader_client import (
    CTRADER_PORT,
    CTraderClient,
    SymbolMeta,
    host_for,
    to_lots,
    to_volume,
)

log = logging.getLogger("executor")

# Per-(account, trade_id) memory of mirrors we already opened once. Prevents
# re-opening when the broker closed a position (its SL/TP hit) a beat before the
# sim's own price check notices. In-memory only: after a restart the
# max-open-age guard covers the same gap.
_opened_once: set[tuple[str, int]] = set()
_lock = threading.Lock()

# Surfaced via /api/executor/status.
STATUS: dict = {"last_run": None, "accounts": {}}


def load_executor_accounts() -> dict[str, dict]:
    """Parse EXECUTOR_ACCOUNTS: {"real_ai_selected": {"ctid_trader_account_id": 123}}."""
    raw = settings.executor_accounts.strip()
    if not raw:
        return {}
    try:
        accounts = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.error("EXECUTOR_ACCOUNTS is not valid JSON: %s", exc)
        return {}
    ok = {}
    for account, cfg in accounts.items():
        if "ctid_trader_account_id" not in cfg:
            log.error("EXECUTOR_ACCOUNTS[%s] missing ctid_trader_account_id — skipped", account)
            continue
        ok[account] = cfg
    return ok


def _tag(trade_id: int) -> str:
    return f"sim:{trade_id}"[:100]  # cTrader label MaxLength = 100


def _round_volume(volume: int, symbol: SymbolMeta) -> int:
    step = symbol.step_volume or 1
    volume = round(volume / step) * step
    volume = max(volume, symbol.min_volume or step)
    if symbol.max_volume:
        volume = min(volume, symbol.max_volume)
    return volume


def _close_position(client, ctid: int, pos, why: str) -> bool:
    result = client.close_position(ctid, pos.position_id, pos.volume)
    if result.ok:
        log.info("MIRROR CLOSE position %s (%s)", pos.position_id, why)
    else:
        log.error("MIRROR CLOSE position %s failed: %s", pos.position_id, result.error)
    return result.ok


def _open_mirror(client, ctid: int, symbol: SymbolMeta, account: str, desired) -> bool:
    """Open a market order mirroring the sim trade, preserving stop/target
    distances. MARKET orders can't carry SL/TP in the same request (unsupported
    by the API), so SL/TP is attached in a follow-up amend once we know the real
    fill price."""
    sl_dist = abs(desired.entry_price - desired.stop_loss)
    tp_dist = abs(desired.take_profit - desired.entry_price)
    is_long = desired.direction == "LONG"

    volume = _round_volume(to_volume(desired.lots, symbol), symbol)

    result = client.new_market_order(ctid, symbol.symbol_id, desired.direction, volume, _tag(desired.id))
    if not result.ok:
        log.error("MIRROR OPEN %s failed: %s", account, result.error)
        return False

    if result.position_id is not None and result.fill_price is not None:
        price = result.fill_price
        sl = round(price - sl_dist if is_long else price + sl_dist, symbol.digits)
        tp = round(price + tp_dist if is_long else price - tp_dist, symbol.digits)
        client.amend_sl_tp(ctid, result.position_id, sl, tp)
        log.info(
            "MIRROR OPEN %s %s %.2f lot @ %.2f (SL dist %.2f, TP dist %.2f) sim trade #%s",
            account, desired.direction, to_lots(volume, symbol), price, sl_dist, tp_dist, desired.id,
        )
    else:
        log.warning("MIRROR OPEN %s filled but execution event had no fill price — SL/TP not attached", account)
    return True


def _sync_account(client, session, account: str, cfg: dict, desired, symbol_cache: dict) -> None:
    st = STATUS["accounts"].setdefault(account, {})
    st["last_sync"] = dt.datetime.utcnow().isoformat()
    st["error"] = None
    st["last_action"] = "none"

    ctid = cfg["ctid_trader_account_id"]

    token = ctrader_tokens.get_token(session, account)
    if token is None:
        st["error"] = f"no BrokerToken row for {account} — run scripts/ctrader_oauth_setup.py"
        return
    token = ctrader_tokens.refresh_if_needed(
        session, token, settings.ctrader_client_id, settings.ctrader_client_secret,
        settings.ctrader_token_refresh_margin_days,
    )

    if not client.authorize_account(ctid, token.access_token):
        st["error"] = f"account auth failed for ctid {ctid}"
        return

    symbol = symbol_cache.get(ctid)
    if symbol is None:
        symbol = client.get_symbol(ctid, settings.executor_symbol)
        if symbol is None:
            st["error"] = f"symbol {settings.executor_symbol} not found for ctid {ctid}"
            return
        symbol_cache[ctid] = symbol

    actual = client.positions(ctid)
    want_tag = _tag(desired.id) if desired is not None else None

    matched = False
    for pos in actual:
        if want_tag is not None and pos.label == want_tag and pos.direction == desired.direction:
            matched = True
            continue
        # Anything else with our label scheme is a stale mirror (sim closed it, or
        # a different/older sim trade) — close it.
        if _close_position(client, ctid, pos, f"not desired (want {want_tag})"):
            st["last_action"] = "close"

    if desired is None or matched:
        return

    key = (account, desired.id)
    with _lock:
        if key in _opened_once:
            st["last_action"] = "skip (already mirrored once; broker likely closed it)"
            return

    age_min = (dt.datetime.utcnow() - desired.created_at).total_seconds() / 60
    if age_min > settings.executor_max_open_age_min:
        st["last_action"] = f"skip (sim trade {age_min:.0f}min old, cap {settings.executor_max_open_age_min})"
        log.warning("not mirroring %s sim trade #%s: %.0f min old", account, desired.id, age_min)
        return

    if _open_mirror(client, ctid, symbol, account, desired):
        with _lock:
            _opened_once.add(key)
        st["last_action"] = f"open sim:{desired.id}"
    else:
        st["error"] = "open failed (see logs)"


def sync_all(client=None, session_factory=get_session) -> None:
    """One reconciliation pass across every configured account."""
    from app.trading.engine import get_open_position  # local: avoid import cycle

    accounts = load_executor_accounts()
    STATUS["last_run"] = dt.datetime.utcnow().isoformat()
    if not accounts:
        STATUS["error"] = "EXECUTOR_ACCOUNTS empty or invalid"
        return
    if not settings.ctrader_client_id or not settings.ctrader_client_secret:
        STATUS["error"] = "CTRADER_CLIENT_ID/CTRADER_CLIENT_SECRET not configured"
        return
    STATUS["error"] = None

    if client is None:
        client = CTraderClient(
            host_for(settings.ctrader_environment), CTRADER_PORT,
            settings.ctrader_client_id, settings.ctrader_client_secret,
        )
    if not client.connect():
        STATUS["error"] = "cannot reach cTrader Open API"
        return

    symbol_cache: dict[int, SymbolMeta] = {}
    session = session_factory()
    try:
        for account, cfg in accounts.items():
            try:
                desired = get_open_position(session, account)
                _sync_account(client, session, account, cfg, desired, symbol_cache)
            except Exception as exc:  # one bad account must not block the other
                log.exception("sync failed for %s", account)
                STATUS["accounts"].setdefault(account, {})["error"] = str(exc)
    finally:
        session.close()
        client.close()


def run_executor_sync() -> None:
    """Scheduler entrypoint."""
    from app.data.yahoo import is_market_open

    if not settings.executor_enabled:
        return
    if not is_market_open():
        return
    sync_all()
