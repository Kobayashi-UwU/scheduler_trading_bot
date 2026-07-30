"""Verify the cTrader executor's reconciliation logic with a mock broker client.

No live cTrader connection, no network, no AI: a MockCTraderClient stands in for
app.trading.ctrader_client.CTraderClient, and sim positions are written straight
to a throwaway SQLite DB. Covers the state machine (open / close / idempotence /
re-open guard / age guard / account-auth-failure isolation) and the order
arithmetic (distance-preserved SL/TP across the futures-vs-spot basis, volume
"cents" conversion derived from the broker's own lotSize).

Run: .venv/bin/python scripts/verify_ctrader_executor.py
"""

import datetime as dt
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'exec.db')}"
os.environ["NVIDIA_API_KEY"] = "dummy"
os.environ["CTRADER_CLIENT_ID"] = "test-client-id"
os.environ["CTRADER_CLIENT_SECRET"] = "test-client-secret"
os.environ["EXECUTOR_ACCOUNTS"] = (
    '{"real_ai_selected": {"ctid_trader_account_id": 8068629},'
    ' "real_trend_following": {"ctid_trader_account_id": 8068630}}'
)

from app.config import settings  # noqa: E402
from app.db.database import get_session, init_db  # noqa: E402
from app.db.models import BrokerToken, Trade, TradeStatus  # noqa: E402
from app.trading import executor  # noqa: E402
from app.trading.ctrader_client import BrokerPosition, ExecResult, SymbolMeta, to_lots, to_volume  # noqa: E402
from app.trading.executor import sync_all  # noqa: E402

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


class MockCTraderClient:
    """In-memory broker: per-ctid position books, one XAUUSD-like symbol."""

    def __init__(self, bid=4096.10, ask=4096.30, symbol_names=("XAUUSD",), fail_ctids=(),
                 lot_size=10000, digits=2, min_volume=100, max_volume=10_000_000, step_volume=100):
        self.bid, self.ask = bid, ask
        self.symbol_names = set(symbol_names)
        self.fail_ctids = set(fail_ctids)
        self.symbol_meta = SymbolMeta(
            symbol_id=1, digits=digits, min_volume=min_volume,
            max_volume=max_volume, step_volume=step_volume, lot_size=lot_size,
        )
        self.books: dict[int, list[BrokerPosition]] = {}
        self.sent_orders: list[dict] = []
        self._position_id = 1000

    def connect(self):
        return True

    def close(self):
        pass

    def authorize_account(self, ctid, access_token):
        if ctid in self.fail_ctids:
            return False
        self.books.setdefault(ctid, [])
        return True

    def get_symbol(self, ctid, name):
        if name not in self.symbol_names:
            return None
        return self.symbol_meta

    def positions(self, ctid):
        return list(self.books[ctid])

    def new_market_order(self, ctid, symbol_id, direction, volume, label):
        price = self.ask if direction == "LONG" else self.bid
        self._position_id += 1
        pos = BrokerPosition(position_id=self._position_id, symbol_id=symbol_id,
                              direction=direction, volume=volume, label=label)
        self.books[ctid].append(pos)
        self.sent_orders.append({
            "ctid": ctid, "direction": direction, "volume": volume,
            "label": label, "price": price, "position_id": pos.position_id,
        })
        return ExecResult(ok=True, position_id=pos.position_id, fill_price=price)

    def close_position(self, ctid, position_id, volume):
        book = self.books[ctid]
        book[:] = [p for p in book if p.position_id != position_id]
        self.sent_orders.append({"ctid": ctid, "close": position_id})
        return ExecResult(ok=True, position_id=position_id)

    def amend_sl_tp(self, ctid, position_id, stop_loss, take_profit):
        for o in self.sent_orders:
            if o.get("position_id") == position_id and "close" not in o:
                o["sl"] = stop_loss
                o["tp"] = take_profit

    def broker_close_all(self, ctid):
        """Simulate the broker's own SL/TP closing everything on an account."""
        self.books[ctid] = []


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


def seed_token(session, account, ctid):
    session.add(BrokerToken(
        account=account, ctid_trader_account_id=ctid,
        access_token=f"token-{account}", refresh_token=f"refresh-{account}",
        expires_at=dt.datetime.utcnow() + dt.timedelta(days=30),
    ))
    session.commit()


init_db()
CTID_AI, CTID_TF = 8068629, 8068630

setup_session = get_session()
seed_token(setup_session, "real_ai_selected", CTID_AI)
seed_token(setup_session, "real_trend_following", CTID_TF)
setup_session.close()

# ---------------------------------------------------------------------------
print("\n[1] open mirror: sim position -> broker order with preserved distances")
executor._opened_once.clear()
mock = MockCTraderClient()
session = get_session()
t_ai = open_sim_trade(session, "real_ai_selected")  # LONG, SL dist 17.20, TP dist 34.40
sync_all(client=mock, session_factory=get_session)

check("one order sent (only ai account has a position)", len(mock.sent_orders), 1)
o = mock.sent_orders[0]
check("routed to the right account", o["ctid"], CTID_AI)
check("BUY (LONG) order", o["direction"], "LONG")
approx("volume mirrors sim lots (0.01 lot -> 100 cents-of-oz)", o["volume"], 100)
approx("fills at broker ask (not Yahoo entry)", o["price"], 4096.30)
approx("SL dist preserved across basis (17.20)", o["price"] - o["sl"], 17.20)
approx("TP dist preserved across basis (34.40)", o["tp"] - o["price"], 34.40)
check("label tags sim trade", o["label"], f"sim:{t_ai.id}")
check("status action recorded",
      executor.STATUS["accounts"]["real_ai_selected"]["last_action"], f"open sim:{t_ai.id}")

print("\n[2] idempotence: second sync sends nothing")
sync_all(client=mock, session_factory=get_session)
check("no new orders", len(mock.sent_orders), 1)
check("broker still has exactly one position", len(mock.books[CTID_AI]), 1)

print("\n[3] sim close -> broker close")
close_sim_trade(session, t_ai)
sync_all(client=mock, session_factory=get_session)
check("close order sent", len(mock.sent_orders), 2)
check("close targeted the mirrored position", "close" in mock.sent_orders[-1], True)
check("broker book now empty", len(mock.books[CTID_AI]), 0)

print("\n[4] broker-side SL hit does not get re-opened")
executor._opened_once.clear()
t2 = open_sim_trade(session, "real_ai_selected", direction="SHORT",
                    entry=4030.0, sl=4044.0, tp=4002.0)
sync_all(client=mock, session_factory=get_session)
check("mirror opened (SHORT)", mock.books[CTID_AI][0].direction, "SHORT")
o = mock.sent_orders[-1]
approx("SHORT fills at bid", o["price"], 4096.10)
approx("SHORT SL above entry by 14.00", o["sl"] - o["price"], 14.00)
mock.broker_close_all(CTID_AI)  # broker's own SL fires first
sync_all(client=mock, session_factory=get_session)
check("no re-open after broker closed it", len(mock.books[CTID_AI]), 0)
check("status explains why",
      "already mirrored once" in executor.STATUS["accounts"]["real_ai_selected"]["last_action"], True)
close_sim_trade(session, t2)

print("\n[5] stale sim position is not mirrored late")
executor._opened_once.clear()
t3 = open_sim_trade(session, "real_ai_selected", age_min=45)
n_before = len(mock.sent_orders)
sync_all(client=mock, session_factory=get_session)
check("no order for a 45min-old sim trade", len(mock.sent_orders), n_before)
check("status explains age skip",
      "min old" in executor.STATUS["accounts"]["real_ai_selected"]["last_action"], True)
close_sim_trade(session, t3)

print("\n[6] direction flip: stale mirror closed, new one opened")
executor._opened_once.clear()
t4 = open_sim_trade(session, "real_trend_following", direction="LONG")
sync_all(client=mock, session_factory=get_session)
check("TF mirror open on its own account", len(mock.books[CTID_TF]), 1)
close_sim_trade(session, t4)
t5 = open_sim_trade(session, "real_trend_following", direction="SHORT",
                    entry=4024.2, sl=4031.13, tp=4010.0)
sync_all(client=mock, session_factory=get_session)
check("old mirror closed + new opened", len(mock.books[CTID_TF]), 1)
check("new mirror is SHORT", mock.books[CTID_TF][0].direction, "SHORT")
check("new mirror tagged with new trade id", mock.books[CTID_TF][0].label, f"sim:{t5.id}")
close_sim_trade(session, t5)

print("\n[7] one account's auth failing doesn't block the other")
executor._opened_once.clear()
mock2 = MockCTraderClient(fail_ctids={CTID_AI})
t6 = open_sim_trade(session, "real_ai_selected")
t7 = open_sim_trade(session, "real_trend_following")
sync_all(client=mock2, session_factory=get_session)
check("ai account reports auth error",
      "auth failed" in (executor.STATUS["accounts"]["real_ai_selected"]["error"] or ""), True)
check("tf account still mirrored", len(mock2.books[CTID_TF]), 1)
close_sim_trade(session, t6)
close_sim_trade(session, t7)
sync_all(client=mock2, session_factory=get_session)

print("\n[8] volume conversion derived from the broker's own lotSize, not a hardcoded factor")
sym = SymbolMeta(symbol_id=1, digits=2, min_volume=100, max_volume=10_000_000, step_volume=100, lot_size=10_000)
check("0.01 lot -> 100 (1oz * 100) at lotSize=10000", to_volume(0.01, sym), 100)
check("1.00 lot -> 10000 at lotSize=10000", to_volume(1.0, sym), 10_000)
approx("round-trip back to lots", to_lots(to_volume(0.25, sym), sym), 0.25)
sym2 = SymbolMeta(symbol_id=2, digits=2, min_volume=1, max_volume=1_000_000, step_volume=1, lot_size=100_000)
check("same 0.01 lot at a different lotSize gives a different raw volume", to_volume(0.01, sym2), 1000)

print("\n[9] config parsing")
from app.trading.executor import load_executor_accounts  # noqa: E402

accts = load_executor_accounts()
check("both accounts parsed", sorted(accts), ["real_ai_selected", "real_trend_following"])
check("ctid carried through", accts["real_ai_selected"]["ctid_trader_account_id"], CTID_AI)
settings.executor_accounts = "{bad json"
check("bad JSON -> empty, no crash", load_executor_accounts(), {})
settings.executor_accounts = '{"real_ai_selected": {"login": 1}}'
check("missing ctid_trader_account_id -> skipped", load_executor_accounts(), {})

print("\n[10] missing BrokerToken row is reported, not a crash")
executor._opened_once.clear()
mock4 = MockCTraderClient()
settings.executor_accounts = '{"real_ai_selected": {"ctid_trader_account_id": 8068629}}'
t9 = open_sim_trade(session, "real_ai_selected")
no_token_session = get_session()
no_token_session.query(BrokerToken).filter(BrokerToken.account == "real_ai_selected").delete()
no_token_session.commit()
no_token_session.close()
sync_all(client=mock4, session_factory=get_session)
check("no order sent without a token", len(mock4.sent_orders), 0)
check("status explains missing token",
      "BrokerToken" in (executor.STATUS["accounts"]["real_ai_selected"]["error"] or ""), True)
close_sim_trade(session, t9)

session.close()
print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("ALL EXECUTOR CHECKS PASSED")
