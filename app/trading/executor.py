"""Mirror real-tier simulated positions onto live MT5 accounts (Exness).

Design: **reconciliation, not event-forwarding.** Every sync compares the desired
state (the sim's open position per real account, straight from the DB) with the
actual state (broker positions tagged with our magic number) and issues the
minimal orders to converge. The executor is therefore stateless across restarts:
nothing is lost if it, the terminal container, or the whole service dies — SL/TP
live server-side at the broker, and the next sync re-derives everything.

Price-feed basis: the bot's levels come from Yahoo GC=F (COMEX futures), which
trades a few dollars away from broker spot XAUUSD. Absolute prices are therefore
never copied — orders open at market, and SL/TP are re-derived by preserving the
sim trade's *stop/target distances* around the actual fill price, which keeps the
dollar risk identical across the basis gap.

One MT5 terminal serves all accounts: the official API's login() switches the
terminal between logins, so accounts are synced sequentially each pass.

The real MT5 API is reached through an mt5linux rpyc bridge running in the
Wine/MT5 container (see CLAUDE.md → Deployment). Everything here talks to a small
backend interface so tests can inject MockBackend instead.
"""

import datetime as dt
import json
import logging
import threading
from dataclasses import dataclass

from app.db.database import get_session
from app.config import settings

log = logging.getLogger("executor")

# MetaTrader5 module constants, inlined. The rpyc proxy exposes functions but not
# module-level constants; these values are the stable MQL5 enum values.
ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
TRADE_ACTION_DEAL = 1
ORDER_TIME_GTC = 0
ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1
TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_INVALID_FILL = 10030

# Per-(account, trade_id) memory of mirrors we already opened once. Prevents
# re-opening when the broker closed a position (its SL/TP hit) a beat before the
# sim's own price check notices. In-memory only: after a restart the
# max-open-age guard covers the same gap.
_opened_once: set[tuple[str, int]] = set()
_lock = threading.Lock()

# Surfaced via /api/executor/status.
STATUS: dict = {"last_run": None, "accounts": {}}


@dataclass
class BrokerPosition:
    ticket: int
    symbol: str
    direction: str  # "LONG" / "SHORT"
    lots: float
    price_open: float
    sl: float
    tp: float
    comment: str


def load_executor_accounts() -> dict[str, dict]:
    """Parse EXECUTOR_ACCOUNTS: {"real_ai_selected": {"login":..,"password":..,"server":..}}."""
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
        if not all(k in cfg for k in ("login", "password", "server")):
            log.error("EXECUTOR_ACCOUNTS[%s] missing login/password/server — skipped", account)
            continue
        ok[account] = cfg
    return ok


class Mt5LinuxBackend:
    """Talks to the MT5 terminal in the Wine container via an mt5linux rpyc bridge."""

    def __init__(self, host: str, port: int):
        self._host = host
        self._port = port
        self._mt5 = None

    def connect(self) -> bool:
        if self._mt5 is not None:
            return True
        try:
            from mt5linux import MetaTrader5  # lazy: only needed when enabled

            self._mt5 = MetaTrader5(host=self._host, port=self._port)
            if not self._mt5.initialize():
                log.error("mt5 initialize() failed: %s", self._mt5.last_error())
                self._mt5 = None
                return False
            return True
        except Exception as exc:
            log.error("cannot reach MT5 bridge at %s:%s: %s", self._host, self._port, exc)
            self._mt5 = None
            return False

    def login(self, login: int, password: str, server: str) -> bool:
        try:
            return bool(self._mt5.login(login, password=password, server=server))
        except Exception as exc:
            log.error("mt5 login(%s) failed: %s", login, exc)
            return False

    def symbol_info(self, name: str):
        return self._mt5.symbol_info(name)

    def symbol_select(self, name: str) -> bool:
        return bool(self._mt5.symbol_select(name, True))

    def tick(self, name: str):
        return self._mt5.symbol_info_tick(name)

    def positions(self, magic: int) -> list[BrokerPosition]:
        raw = self._mt5.positions_get() or []
        out = []
        for p in raw:
            if p.magic != magic:
                continue
            out.append(
                BrokerPosition(
                    ticket=p.ticket,
                    symbol=p.symbol,
                    direction="LONG" if p.type == ORDER_TYPE_BUY else "SHORT",
                    lots=p.volume,
                    price_open=p.price_open,
                    sl=p.sl,
                    tp=p.tp,
                    comment=p.comment or "",
                )
            )
        return out

    def order_send(self, request: dict):
        return self._mt5.order_send(request)


_backend: Mt5LinuxBackend | None = None


def _get_backend() -> Mt5LinuxBackend:
    global _backend
    if _backend is None:
        _backend = Mt5LinuxBackend(settings.mt5_bridge_host, settings.mt5_bridge_port)
    return _backend


def _tag(trade_id: int) -> str:
    return f"sim:{trade_id}"[:31]  # MT5 comment limit


def _resolve_symbol(backend) -> str | None:
    """Find the broker's gold symbol; Exness account types use different suffixes."""
    candidates = [settings.executor_symbol] + [
        s.strip() for s in settings.executor_symbol_fallbacks.split(",") if s.strip()
    ]
    for name in candidates:
        info = backend.symbol_info(name)
        if info is not None:
            if getattr(info, "visible", True) or backend.symbol_select(name):
                return name
    log.error("no gold symbol found at broker; tried %s", candidates)
    return None


def _round_to(value: float, digits: int) -> float:
    return round(value, digits)


def _send_deal(backend, request: dict):
    """order_send with a filling-mode fallback (brokers differ on FOK vs IOC)."""
    for filling in (ORDER_FILLING_FOK, ORDER_FILLING_IOC):
        result = backend.order_send({**request, "type_filling": filling})
        if result is None:
            return None
        if result.retcode != TRADE_RETCODE_INVALID_FILL:
            return result
    return result


def _close_position(backend, pos: BrokerPosition, why: str) -> bool:
    tick = backend.tick(pos.symbol)
    if tick is None:
        log.error("close %s: no tick for %s", pos.ticket, pos.symbol)
        return False
    closing_long = pos.direction == "LONG"
    result = _send_deal(
        backend,
        {
            "action": TRADE_ACTION_DEAL,
            "position": pos.ticket,
            "symbol": pos.symbol,
            "volume": pos.lots,
            "type": ORDER_TYPE_SELL if closing_long else ORDER_TYPE_BUY,
            "price": tick.bid if closing_long else tick.ask,
            "deviation": settings.executor_deviation_points,
            "magic": settings.executor_magic,
            "comment": pos.comment,
            "type_time": ORDER_TIME_GTC,
        },
    )
    ok = result is not None and result.retcode == TRADE_RETCODE_DONE
    if ok:
        log.info("MIRROR CLOSE ticket %s (%s)", pos.ticket, why)
    else:
        log.error("MIRROR CLOSE ticket %s failed: retcode=%s", pos.ticket,
                  getattr(result, "retcode", None))
    return ok


def _open_mirror(backend, account: str, desired) -> bool:
    """Open a market order mirroring the sim trade, preserving stop/target distances."""
    symbol = _resolve_symbol(backend)
    if symbol is None:
        return False
    info = backend.symbol_info(symbol)
    tick = backend.tick(symbol)
    if tick is None:
        log.error("open mirror %s: no tick for %s", account, symbol)
        return False

    digits = getattr(info, "digits", 2)
    sl_dist = abs(desired.entry_price - desired.stop_loss)
    tp_dist = abs(desired.take_profit - desired.entry_price)
    is_long = desired.direction == "LONG"
    price = tick.ask if is_long else tick.bid
    sl = _round_to(price - sl_dist if is_long else price + sl_dist, digits)
    tp = _round_to(price + tp_dist if is_long else price - tp_dist, digits)

    # Defensive: sim lots should already respect broker granularity, but round anyway.
    step = getattr(info, "volume_step", 0.01) or 0.01
    lots = max(getattr(info, "volume_min", 0.01) or 0.01,
               round(round(desired.lots / step) * step, 8))

    result = _send_deal(
        backend,
        {
            "action": TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lots,
            "type": ORDER_TYPE_BUY if is_long else ORDER_TYPE_SELL,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": settings.executor_deviation_points,
            "magic": settings.executor_magic,
            "comment": _tag(desired.id),
            "type_time": ORDER_TIME_GTC,
        },
    )
    ok = result is not None and result.retcode == TRADE_RETCODE_DONE
    if ok:
        log.info(
            "MIRROR OPEN %s %s %.2f lot %s @ market (SL dist %.2f, TP dist %.2f) sim trade #%s",
            account, desired.direction, lots, symbol, sl_dist, tp_dist, desired.id,
        )
    else:
        log.error("MIRROR OPEN %s failed: retcode=%s comment=%s", account,
                  getattr(result, "retcode", None), getattr(result, "comment", None))
    return ok


def _sync_account(backend, account: str, cfg: dict, desired) -> None:
    st = STATUS["accounts"].setdefault(account, {})
    st["last_sync"] = dt.datetime.utcnow().isoformat()
    st["error"] = None
    st["last_action"] = "none"

    if not backend.login(int(cfg["login"]), cfg["password"], cfg["server"]):
        st["error"] = f"login failed for {cfg['login']} on {cfg['server']}"
        return

    actual = backend.positions(settings.executor_magic)
    want_tag = _tag(desired.id) if desired is not None else None

    matched = False
    for pos in actual:
        if want_tag is not None and pos.comment == want_tag and pos.direction == desired.direction:
            matched = True
            continue
        # Anything else with our magic is a stale mirror (sim closed it, or a
        # different/older sim trade) — close it.
        if _close_position(backend, pos, f"not desired (want {want_tag})"):
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

    if _open_mirror(backend, account, desired):
        with _lock:
            _opened_once.add(key)
        st["last_action"] = f"open sim:{desired.id}"
    else:
        st["error"] = "open failed (see logs)"


def sync_all(backend=None, session_factory=get_session) -> None:
    """One reconciliation pass across every configured account."""
    from app.trading.engine import get_open_position  # local: avoid import cycle

    accounts = load_executor_accounts()
    STATUS["last_run"] = dt.datetime.utcnow().isoformat()
    if not accounts:
        STATUS["error"] = "EXECUTOR_ACCOUNTS empty or invalid"
        return
    STATUS["error"] = None

    if backend is None:
        backend = _get_backend()
    if not backend.connect():
        STATUS["error"] = "cannot reach MT5 bridge"
        return

    session = session_factory()
    try:
        for account, cfg in accounts.items():
            try:
                desired = get_open_position(session, account)
                _sync_account(backend, account, cfg, desired)
            except Exception as exc:  # one bad account must not block the other
                log.exception("sync failed for %s", account)
                STATUS["accounts"].setdefault(account, {})["error"] = str(exc)
    finally:
        session.close()


def run_executor_sync() -> None:
    """Scheduler entrypoint."""
    from app.data.yahoo import is_market_open

    if not settings.executor_enabled:
        return
    if not is_market_open():
        return
    sync_all()
