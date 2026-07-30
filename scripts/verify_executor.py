"""Verify the MT5 executor's reconciliation logic with a mock broker backend.

No MT5 terminal, no network, no AI: a MockBackend stands in for the mt5linux
bridge, and sim positions are written straight to a throwaway SQLite DB. Covers
the state machine (open / close / idempotence / re-open guard / age guard /
login failure isolation) and the order arithmetic (distance-preserved SL/TP
across the futures-vs-spot basis, lot rounding, symbol fallback).

Run: .venv/bin/python scripts/verify_executor.py
"""

import datetime as dt
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'exec.db')}"
os.environ["NVIDIA_API_KEY"] = "dummy"
os.environ["EXECUTOR_ACCOUNTS"] = (
    '{"real_ai_selected": {"login": 433936518, "password": "pw1", "server": "Exness-MT5Trial7"},'
    ' "real_trend_following": {"login": 463718096, "password": "pw2", "server": "Exness-MT5Trial17"}}'
)

from types import SimpleNamespace  # noqa: E402

from app.config import settings  # noqa: E402
from app.db.database import get_session, init_db  # noqa: E402
from app.db.models import Trade, TradeStatus  # noqa: E402
from app.trading import executor  # noqa: E402
from app.trading.executor import (  # noqa: E402
    ORDER_TYPE_BUY,
    TRADE_RETCODE_DONE,
    BrokerPosition,
    sync_all,
)

FAILURES = []


def check(label, got, want):
    ok = got == want
    if not ok:
        FAILURES.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f" (want {want!r})"))


def approx(label, got, want, tol=0.01):
    ok = got is not None and abs(got - want) <= tol
    if not ok:
        FAILURES.append(f"{label}: got {got!r}, want ~{want!r}")
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f" (want ~{want!r})"))


class MockBackend:
    """In-memory broker: per-login position books, Exness-spot-like tick."""

    def __init__(self, bid=4096.10, ask=4096.30, symbols=("XAUUSD",), fail_logins=()):
        self.bid, self.ask = bid, ask
        self.symbols = set(symbols)
        self.fail_logins = set(fail_logins)
        self.books: dict[int, list[BrokerPosition]] = {}
        self.current_login = None
        self.sent_orders: list[dict] = []
        self._ticket = 1000

    def connect(self):
        return True

    def login(self, login, password, server):
        if login in self.fail_logins:
            return False
        self.current_login = login
        self.books.setdefault(login, [])
        return True

    def symbol_info(self, name):
        if name not in self.symbols:
            return None
        return SimpleNamespace(digits=2, volume_min=0.01, volume_step=0.01, visible=True)

    def symbol_select(self, name):
        return name in self.symbols

    def tick(self, name):
        return SimpleNamespace(bid=self.bid, ask=self.ask)

    def positions(self, magic):
        return list(self.books[self.current_login])

    def order_send(self, request):
        self.sent_orders.append({**request, "login": self.current_login})
        book = self.books[self.current_login]
        if "position" in request:  # close
            book[:] = [p for p in book if p.ticket != request["position"]]
        else:  # open
            self._ticket += 1
            book.append(
                BrokerPosition(
                    ticket=self._ticket,
                    symbol=request["symbol"],
                    direction="LONG" if request["type"] == ORDER_TYPE_BUY else "SHORT",
                    lots=request["volume"],
                    price_open=request["price"],
                    sl=request["sl"],
                    tp=request["tp"],
                    comment=request["comment"],
                )
            )
        return SimpleNamespace(retcode=TRADE_RETCODE_DONE, comment="done")

    def broker_close_all(self, login):
        """Simulate the broker's own SL/TP closing everything on an account."""
        self.books[login] = []


def open_sim_trade(session, account, direction="LONG", entry=4113.57, sl=4096.37,
                   tp=4147.97, lots=0.01, age_min=0.0) -> Trade:
    t = Trade(
        strategy="trend_following", is_shadow=False, account=account,
        direction=direction, status=TradeStatus.OPEN.value,
        entry_price=entry, stop_loss=sl, take_profit=tp,
        size=lots * 100, risk_amount=abs(entry - sl), lots=lots,
        created_at=dt.datetime.utcnow() - dt.timedelta(minutes=age_min),
    )
    session.add(t)
    session.commit()
    return t


def close_sim_trade(session, trade):
    trade.status = TradeStatus.CLOSED_SL.value
    trade.pnl = -trade.risk_amount
    trade.closed_at = dt.datetime.utcnow()
    session.commit()


init_db()
LOGIN_AI, LOGIN_TF = 433936518, 463718096

# ---------------------------------------------------------------------------
print("\n[1] open mirror: sim position -> broker order with preserved distances")
executor._opened_once.clear()
mock = MockBackend()
session = get_session()
t_ai = open_sim_trade(session, "real_ai_selected")  # LONG, SL dist 17.20, TP dist 34.40
sync_all(backend=mock, session_factory=get_session)

check("one order sent (only ai account has a position)", len(mock.sent_orders), 1)
o = mock.sent_orders[0]
check("routed to the right login", o["login"], LOGIN_AI)
check("BUY order", o["type"], ORDER_TYPE_BUY)
approx("volume mirrors sim lots", o["volume"], 0.01)
approx("fills at broker ask (not Yahoo entry)", o["price"], 4096.30)
approx("SL dist preserved across basis (17.20)", o["price"] - o["sl"], 17.20)
approx("TP dist preserved across basis (34.40)", o["tp"] - o["price"], 34.40)
check("comment tags sim trade", o["comment"], f"sim:{t_ai.id}")
check("magic set", o["magic"], settings.executor_magic)
check("status action recorded",
      executor.STATUS["accounts"]["real_ai_selected"]["last_action"], f"open sim:{t_ai.id}")

print("\n[2] idempotence: second sync sends nothing")
sync_all(backend=mock, session_factory=get_session)
check("no new orders", len(mock.sent_orders), 1)
check("broker still has exactly one position", len(mock.books[LOGIN_AI]), 1)

print("\n[3] sim close -> broker close")
close_sim_trade(session, t_ai)
sync_all(backend=mock, session_factory=get_session)
check("close order sent", len(mock.sent_orders), 2)
check("close targeted the mirrored ticket", "position" in mock.sent_orders[-1], True)
check("broker book now empty", len(mock.books[LOGIN_AI]), 0)

print("\n[4] broker-side SL hit does not get re-opened")
executor._opened_once.clear()
t2 = open_sim_trade(session, "real_ai_selected", direction="SHORT",
                    entry=4030.0, sl=4044.0, tp=4002.0)
sync_all(backend=mock, session_factory=get_session)
check("mirror opened (SHORT)", mock.books[LOGIN_AI][0].direction, "SHORT")
o = mock.sent_orders[-1]
approx("SHORT fills at bid", o["price"], 4096.10)
approx("SHORT SL above entry by 14.00", o["sl"] - o["price"], 14.00)
mock.broker_close_all(LOGIN_AI)  # broker's own SL fires first
sync_all(backend=mock, session_factory=get_session)
check("no re-open after broker closed it", len(mock.books[LOGIN_AI]), 0)
check("status explains why",
      "already mirrored once" in executor.STATUS["accounts"]["real_ai_selected"]["last_action"], True)
close_sim_trade(session, t2)

print("\n[5] stale sim position is not mirrored late")
executor._opened_once.clear()
t3 = open_sim_trade(session, "real_ai_selected", age_min=45)
n_before = len(mock.sent_orders)
sync_all(backend=mock, session_factory=get_session)
check("no order for a 45min-old sim trade", len(mock.sent_orders), n_before)
check("status explains age skip",
      "min old" in executor.STATUS["accounts"]["real_ai_selected"]["last_action"], True)
close_sim_trade(session, t3)

print("\n[6] direction flip: stale mirror closed, new one opened")
executor._opened_once.clear()
t4 = open_sim_trade(session, "real_trend_following", direction="LONG")
sync_all(backend=mock, session_factory=get_session)
check("TF mirror open on its own login", len(mock.books[LOGIN_TF]), 1)
close_sim_trade(session, t4)
t5 = open_sim_trade(session, "real_trend_following", direction="SHORT",
                    entry=4024.2, sl=4031.13, tp=4010.0)
sync_all(backend=mock, session_factory=get_session)
check("old mirror closed + new opened", len(mock.books[LOGIN_TF]), 1)
check("new mirror is SHORT", mock.books[LOGIN_TF][0].direction, "SHORT")
check("new mirror tagged with new trade id", mock.books[LOGIN_TF][0].comment, f"sim:{t5.id}")
close_sim_trade(session, t5)

print("\n[7] one login failing doesn't block the other account")
executor._opened_once.clear()
mock2 = MockBackend(fail_logins={LOGIN_AI})
t6 = open_sim_trade(session, "real_ai_selected")
t7 = open_sim_trade(session, "real_trend_following")
sync_all(backend=mock2, session_factory=get_session)
check("ai account reports login error",
      "login failed" in (executor.STATUS["accounts"]["real_ai_selected"]["error"] or ""), True)
check("tf account still mirrored", len(mock2.books[LOGIN_TF]), 1)
close_sim_trade(session, t6)
close_sim_trade(session, t7)
sync_all(backend=mock2, session_factory=get_session)

print("\n[8] symbol fallback (Exness suffixed account types)")
executor._opened_once.clear()
mock3 = MockBackend(symbols=("XAUUSDm",))
t8 = open_sim_trade(session, "real_ai_selected")
sync_all(backend=mock3, session_factory=get_session)
check("fell back to XAUUSDm", mock3.sent_orders[-1]["symbol"], "XAUUSDm")
close_sim_trade(session, t8)

print("\n[9] config parsing")
from app.trading.executor import load_executor_accounts  # noqa: E402

accts = load_executor_accounts()
check("both accounts parsed", sorted(accts), ["real_ai_selected", "real_trend_following"])
check("server carried through", accts["real_ai_selected"]["server"], "Exness-MT5Trial7")
settings.executor_accounts = "{bad json"
check("bad JSON -> empty, no crash", load_executor_accounts(), {})
settings.executor_accounts = '{"real_ai_selected": {"login": 1}}'
check("missing fields -> skipped", load_executor_accounts(), {})

session.close()
print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("ALL EXECUTOR CHECKS PASSED")
