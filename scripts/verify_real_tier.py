"""Local verification of the real-account tier.

Covers the three things most likely to be wrong:
  1. the hand-rolled migration against a *legacy* schema with existing rows
  2. lot sizing / margin / risk-cap math, checked against real production stops
  3. a full process_cycle fanning out into both tiers, incl. skip logging
"""

import os
import tempfile

DB_PATH = os.path.join(tempfile.mkdtemp(), "legacy.db")
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["NVIDIA_API_KEY"] = "dummy"
os.environ["REAL_START_BALANCE"] = "100"
os.environ["REAL_LEVERAGE"] = "100"
os.environ["REAL_MAX_RISK_PCT"] = "0.30"

from sqlalchemy import create_engine, text  # noqa: E402

engine = create_engine(os.environ["DATABASE_URL"])

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


# ---------------------------------------------------------------------------
# 1. Build a LEGACY trades table (pre-change schema) with rows, like production.
# ---------------------------------------------------------------------------
print("\n[1] legacy schema + rows, then migrate")
with engine.begin() as conn:
    conn.execute(text("""
        CREATE TABLE trades (
            id INTEGER NOT NULL PRIMARY KEY,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            closed_at DATETIME,
            strategy VARCHAR NOT NULL,
            is_shadow BOOLEAN,
            analysis_id INTEGER,
            direction VARCHAR NOT NULL,
            status VARCHAR,
            entry_price FLOAT NOT NULL,
            stop_loss FLOAT NOT NULL,
            take_profit FLOAT NOT NULL,
            size FLOAT NOT NULL,
            risk_amount FLOAT NOT NULL,
            exit_price FLOAT,
            pnl FLOAT,
            r_multiple FLOAT,
            confidence FLOAT,
            reasoning VARCHAR
        )
    """))
    # mirror real production rows: a live ai_selected trade and a shadow trade
    conn.execute(text("""
        INSERT INTO trades (strategy, is_shadow, direction, status, entry_price, stop_loss,
                            take_profit, size, risk_amount, exit_price, pnl, r_multiple, closed_at)
        VALUES ('trend_following', 0, 'LONG', 'CLOSED_TP', 4113.57, 4096.37, 4147.97,
                5.81, 100.0, 4147.97, 227.66, 2.28, '2026-07-29 10:00:00'),
               ('trend_following', 1, 'LONG', 'CLOSED_TP', 4113.57, 4096.37, 4147.97,
                5.81, 100.0, 4147.97, 227.66, 2.28, '2026-07-29 10:00:00'),
               ('exhaustion_fade', 1, 'SHORT', 'CLOSED_SL', 4018.30, 4025.00, 4005.00,
                14.9, 100.0, 4025.00, -99.50, -1.0, '2026-07-29 11:00:00')
    """))

from app.db.database import engine as app_engine, get_session, init_db  # noqa: E402

init_db()  # create_all + run_migrations

with app_engine.connect() as conn:
    cols = {r[1] for r in conn.execute(text("PRAGMA table_info(trades)"))}
    for col in ("account", "lots", "margin_used", "risk_pct"):
        check(f"column {col} added", col in cols, True)

    rows = conn.execute(text("SELECT strategy, is_shadow, account FROM trades ORDER BY id")).all()
    check("backfill live -> ai_selected", rows[0][2], "ai_selected")
    check("backfill shadow -> strategy", rows[1][2], "trend_following")
    check("backfill shadow #2", rows[2][2], "exhaustion_fade")
    nulls = conn.execute(text("SELECT COUNT(*) FROM trades WHERE account IS NULL")).scalar()
    check("no NULL accounts remain", nulls, 0)

# idempotency: run again, must not error or duplicate work
init_db()
init_db()
print("  PASS  migration is idempotent (ran 3x)")

# ---------------------------------------------------------------------------
# 2. Sizing math against REAL production stop distances.
# ---------------------------------------------------------------------------
print("\n[2] lot sizing vs real production stops (balance $100, 1:100, cap 30%)")
from app.trading.sizing import size_real  # noqa: E402

# tightest real stop: 4018.30 -> 4025.00 = 6.70
s = size_real(100.0, 4018.30, 4025.00)
check("tight stop tradeable", s.ok, True)
approx("  lots (floored to min)", s.lots, 0.01)
approx("  size in oz", s.size, 1.0)
approx("  risk $ = stop distance", s.risk_amount, 6.70)
approx("  risk %", s.risk_pct * 100, 6.70)
approx("  margin @1:100", s.margin, 40.18)
check("  flagged as floored to min", s.floored_to_min, True)
approx("  target risk was 1%", s.target_risk, 1.00)

# median real stop: 4113.57 -> 4096.37 = 17.20
s = size_real(100.0, 4113.57, 4096.37)
check("median stop tradeable", s.ok, True)
approx("  risk $", s.risk_amount, 17.20)
approx("  risk %", s.risk_pct * 100, 17.20)

# widest real stop: 4110 -> 4085 = 25.00
s = size_real(100.0, 4110.00, 4085.00)
check("widest stop tradeable at 30% cap", s.ok, True)
approx("  risk %", s.risk_pct * 100, 25.00)

# risk cap must bite
os.environ["REAL_MAX_RISK_PCT"] = "0.15"
from app.config import settings  # noqa: E402
settings.real_max_risk_pct = 0.15
s = size_real(100.0, 4110.00, 4085.00)
check("25% risk rejected at 15% cap", s.ok, False)
check("  reason mentions cap", "over cap" in s.reason, True)
settings.real_max_risk_pct = 0.30

# margin must bite at low leverage
settings.real_leverage = 20.0
s = size_real(100.0, 4110.00, 4085.00)
check("1:20 leverage rejected (margin > balance)", s.ok, False)
check("  reason mentions margin", "margin" in s.reason, True)
settings.real_leverage = 100.0

# bigger balance -> genuine 1% sizing becomes reachable
s = size_real(10000.0, 4113.57, 4096.37)
check("$10k account can size properly", s.ok, True)
approx("  lots", s.lots, 0.05)
approx("  risk $ near 1% target", s.risk_amount, 86.0, tol=2.0)
check("  not floored to min", s.floored_to_min, False)

# depleted account
s = size_real(0.0, 4110.0, 4085.0)
check("depleted account rejected", s.ok, False)

# ---------------------------------------------------------------------------
# 3. Full process_cycle fan-out across both tiers.
# ---------------------------------------------------------------------------
print("\n[3] process_cycle fan-out")
from app.ai.parser import ParsedCycle, StrategySignal  # noqa: E402
from app.config import PAPER_ACCOUNTS, REAL_ACCOUNTS  # noqa: E402
from app.db.models import Trade, TradeSkip  # noqa: E402
from app.trading.engine import get_balance, get_open_position, process_cycle  # noqa: E402

check("real accounts configured", REAL_ACCOUNTS, ["real_ai_selected", "real_trend_following"])
check("paper accounts unchanged (11)", len(PAPER_ACCOUNTS), 11)

def signal(strategy, action="BUY", entry=4113.57, sl=4096.37, tp=4147.97, conf=0.75):
    return StrategySignal(ok=True, strategy=strategy, action=action, confidence=conf,
                          entry=entry, stop_loss=sl, take_profit=tp,
                          risk_reward=2.0, reasoning="test")

parsed = ParsedCycle(
    ok=True, regime="trending_up", selected_strategy="trend_following",
    evaluations={
        "trend_following": signal("trend_following"),
        "breakout": signal("breakout"),
        "mean_reversion": StrategySignal(ok=False, strategy="mean_reversion",
                                         reject_reason="confidence too low"),
    },
)

session = get_session()
process_cycle(session, parsed, current_price=4113.57, analysis_id=None)

# paper tier
paper_tf = get_open_position(session, "trend_following")
check("paper trend_following opened", paper_tf is not None, True)
check("  paper lots is NULL", paper_tf.lots, None)
approx("  paper size (oz) = risk/stop", paper_tf.size, (10000 + 128.16) * 0.01 / 17.20, tol=0.5)
check("paper ai_selected opened", get_open_position(session, "ai_selected") is not None, True)
check("paper breakout opened", get_open_position(session, "breakout") is not None, True)
check("invalid signal did not trade", get_open_position(session, "mean_reversion"), None)

# real tier
real_tf = get_open_position(session, "real_trend_following")
check("real_trend_following opened", real_tf is not None, True)
approx("  real lots", real_tf.lots, 0.01)
approx("  real size oz", real_tf.size, 1.0)
approx("  real risk $", real_tf.risk_amount, 17.20)
approx("  real risk %", real_tf.risk_pct, 17.20)
approx("  real margin", real_tf.margin_used, 41.14)
real_ai = get_open_position(session, "real_ai_selected")
check("real_ai_selected opened", real_ai is not None, True)
check("real tier ignores non-mirrored strategies",
      get_open_position(session, "real_breakout"), None)

check("real balances start at 100", get_balance(session, "real_trend_following"), 100.0)
check("paper balance unaffected by real tier",
      round(get_balance(session, "breakout"), 2), 10000.0)

# skip logging: wide stop + tight cap on a fresh real account
settings.real_max_risk_pct = 0.05
parsed2 = ParsedCycle(
    ok=True, regime="trending_up", selected_strategy="breakout",
    evaluations={"trend_following": signal("trend_following", entry=4110.0, sl=4085.0, tp=4160.0)},
)
session2 = get_session()
# close the existing real position first so a new one can be attempted
for acct in REAL_ACCOUNTS:
    pos = get_open_position(session2, acct)
    if pos:
        pos.status = "CLOSED_MANUAL"
        pos.pnl = 0.0
        pos.closed_at = __import__("datetime").datetime.utcnow()
session2.commit()
process_cycle(session2, parsed2, current_price=4110.0, analysis_id=None)

skips = session2.query(TradeSkip).all()
check("skip row written", len(skips) >= 1, True)
if skips:
    s0 = skips[0]
    check("  skip account is real tier", s0.account.startswith("real_"), True)
    check("  skip reason mentions cap", "over cap" in s0.reason, True)
    approx("  skip intended risk %", s0.intended_risk_pct, 25.0)
check("no real position opened after skip",
      get_open_position(session2, "real_trend_following"), None)
check("paper still traded despite real skip",
      get_open_position(session2, "trend_following") is not None, True)
settings.real_max_risk_pct = 0.30

# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("ALL CHECKS PASSED")
