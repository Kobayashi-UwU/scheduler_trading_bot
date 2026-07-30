# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A paper-trading experiment: pulls live XAUUSD/gold data from Yahoo Finance, asks DeepSeek
(via NVIDIA's free-tier API) to classify the market regime, pick a fixed trading strategy,
and emit a trade signal — then simulates that trade with no real money. Everything is
logged so the AI's real edge (if any) can be measured over time. No real orders are ever
placed; there is no broker integration.

## Commands

```bash
make up        # venv + install deps, then run the server in the background (nohup), logs to logs/server.log
make down      # stop the background server
make restart   # down + up
make logs      # tail -f logs/server.log
make status    # check if the background server is running

# Run directly in the foreground (useful for iterating):
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

The Makefile carries two fixes for Nix-based environments like Replit; keep them if you
edit it. `pip` is invoked as `PIP_USER=0 python -m pip` because `PIP_CONFIG_FILE` points at
a Nix pip.conf that forces `--user` installs, which a venv rejects outright. And uvicorn is
invoked as `python -m uvicorn` rather than `.venv/bin/uvicorn`, because pip writes that
console script with a `#!/usr/bin/env python3` shebang when the venv's python is a symlink
into the Nix store — so the script resolves to the *system* python and fails with
`ModuleNotFoundError: No module named 'uvicorn'` despite the package being installed.

`make up` needs a `.env` with at least `NVIDIA_API_KEY` (there's no default), or startup
dies in `Settings()` before the server binds.

There is no test suite, linter, or formatter configured (no pytest/ruff/black in
`requirements.txt`; no `tests/` directory exists at all, despite what the README's project
tree shows). Don't assume `pytest` or `make test` exists. To exercise a code path, call it
directly — the cycle functions are plain sync functions and safe to invoke by hand:

```bash
# real-account tier: migration (incl. legacy-schema backfill), lot sizing/margin math,
# and process_cycle fan-out across both tiers. Self-contained — builds its own throwaway
# SQLite DB, makes no AI calls, touches nothing real. Run this after any engine/sizing edit.
.venv/bin/python scripts/verify_real_tier.py

# one full analysis cycle (makes a real AI call + writes real rows to the DB)
.venv/bin/python -c "from app.db.database import init_db; init_db(); from app.cycle import run_analysis_cycle; run_analysis_cycle()"

# just the data/indicator half, no AI call, no DB writes
.venv/bin/python -c "from app.data.yahoo import fetch_candles; from app.data.indicators import summarize; print(summarize(fetch_candles('1h')))"
```

Dashboard runs at `http://localhost:8000` once the server is up.

## Configuration

All settings come from `app/config.py` (`pydantic-settings`), loaded from `.env` (see
`.env.example` for the full list). Key ones: `NVIDIA_API_KEY` (required, no default),
`SYMBOL` (default `GC=F`, COMEX gold futures), `ANALYSIS_INTERVAL_MIN`, `START_BALANCE`,
`RISK_PER_TRADE`, `MIN_CONFIDENCE`, `MIN_RISK_REWARD`, `DATABASE_URL` (defaults to local
SQLite; Railway sets this to Postgres — note Railway gives `postgres://` and `config.py`
normalizes it to `postgresql://` for SQLAlchemy).

Two settings exist but are **not enforced by any code**:

- `MAX_OPEN_POSITIONS` — only ever interpolated into the prompt as an advisory constraint
  (`prompts.py`). The engine independently hardcodes one open position per account
  (`_open_trade` bails if `get_open_position` returns anything), so changing this value
  does not change behavior.
- `DASHBOARD_PASSWORD` — read into settings and present in `.env.example`, but nothing
  references it. There is no auth on any route; the dashboard and full JSON API (including
  raw prompts/responses via `/api/analyses/{id}`) are public.

## Architecture: the cycle is the core

Everything revolves around `app/cycle.py`, run by two APScheduler jobs registered in
`app/scheduler.py`:

- **`run_analysis_cycle`** (every `ANALYSIS_INTERVAL_MIN`, default 15; also fires
  immediately at startup via `next_run_time`): the full AI loop.
- **`run_price_check`** (every `PRICE_CHECK_INTERVAL_MIN`, default 1): fast job that just
  marks open positions to market and auto-closes on SL/TP hits, without calling the AI.

Both jobs use `max_instances=1, coalesce=True`, and both skip entirely when
`is_market_open()` (in `app/data/yahoo.py`) says gold markets are closed (weekends —
a pure UTC weekday/hour check, no holiday calendar).

### The analysis cycle, step by step

1. `app/data/yahoo.py` fetches multi-timeframe candles (`fetch_all_timeframes` → 15m/1h/1d,
   three sequential `yf.download` calls); each timeframe falls back from `SYMBOL` to
   `FALLBACK_SYMBOL` if the primary fetch returns empty or raises.
2. `app/data/indicators.py` (`summarize`) computes EMA/RSI/MACD/ATR/swing levels per
   timeframe. Returns `{}` for any timeframe with fewer than 30 bars, so the AI can
   receive an empty indicator block rather than an error.
3. `app/ai/prompts.py` (`build_market_briefing`) builds the user prompt: indicator
   summary + current open positions + recent trade history, **per account** (see below).
   `SYSTEM_PROMPT` is assembled at import time by concatenating every strategy's frozen
   rules (~13.5k chars) — it is a module-level constant, not rebuilt per cycle.
4. `app/ai/client.py` (`call_deepseek`) calls the NVIDIA chat-completions endpoint.
   Notable non-obvious behavior:
   - Tries `nvidia_model`, then `nvidia_fallback_model`, then hardcoded
     `deepseek-ai/deepseek-v4-flash`, but **always sorts flash-tier models first** —
     `-pro` models on the NVIDIA free tier tend to hang, so flash is tried first even if
     configured as the fallback, and `-pro` gets a short 60s read timeout to fail fast.
   - Flash-tier calls use non-streaming; `-pro` uses SSE streaming with a non-streaming
     retry if the stream comes back empty.
   - **Retries transient failures (timeouts, 5xx, 429, empty bodies) forever** with capped
     exponential backoff, cycling models each pass. Only non-retryable errors (bad key,
     malformed request → 4xx other than 429) raise `AIClientError`. Consequence: a
     persistently failing API blocks that scheduler thread indefinitely, and because
     `max_instances=1`, subsequent analysis cycles are skipped for the duration. The 1-min
     `run_price_check` job is separate and keeps managing SL/TP throughout.
5. `app/ai/parser.py` (`parse_and_validate`) strictly validates the JSON contract: regime,
   selected strategy, and a signal **per strategy** (not just the selected one — see
   shadow trading), checking schema, SL/TP ordering, min confidence, min R:R.
   **Validation failure is two-tiered, and this distinction matters:**
   - Bad JSON, an invalid `regime`, or an invalid `selected_strategy` reject the *whole*
     cycle (`ParsedCycle.ok=False`, `reject_reason` set) — nothing reaches the engine.
   - A bad or missing *individual* strategy evaluation only sets `ok=False` on that one
     `StrategySignal`. The cycle stays `ok=True`; the engine silently skips that account.
     A response containing an empty `strategy_evaluations` object still parses as `ok=True`.
   - `ParsedCycle.live_signal` is `None` when `selected_strategy` is `stay_out`/empty, and
     is looked up out of `evaluations` — so the AI's chosen strategy can be selected but
     still contribute no trade if its own evaluation failed validation.
6. `app/trading/engine.py` (`process_cycle`) executes trades. Every `Analysis` row is
   logged regardless of whether it produced a trade.
7. `manage_open_positions` runs once more immediately after, using the same price, to
   catch SL/TP hits that just opened.

### Shadow trading — the key architectural concept

The AI doesn't just pick one strategy and trade it — **every strategy in `STRATEGIES`
runs its own simulated account in parallel** ("shadow" accounts), plus one extra
`"ai_selected"` account that trades whichever strategy the AI chose that cycle. This is
the experiment's control group: it lets you compare "the AI's strategy selection" against
"just running each individual strategy alone".

This shows up throughout the trading layer:

- `app/trading/engine.py`: `ALL_ACCOUNTS = [AI_SELECTED_ACCOUNT] + STRATEGIES`. Every
  balance/position/trade-history query takes an `account` string and filters
  `Trade.is_shadow` + `Trade.strategy` accordingly (`_account_filter`). The
  `ai_selected` account is `is_shadow=False`; every strategy's own shadow account is
  `is_shadow=True`. There is no `account` column — the (`is_shadow`, `strategy`) pair
  *is* the account identity.
- `Trade.risk_amount`/size are computed **per account's own balance**, so each shadow
  account compounds independently. Balance is always derived, never stored:
  `get_balance` = `START_BALANCE` + sum of that account's realized PnL.
- `process_cycle` first applies every strategy's `evaluations[strategy]` signal to that
  strategy's own shadow account, then separately applies `parsed.live_signal` (the AI's
  actually-selected strategy) to `ai_selected`. The same signal therefore usually produces
  *two* trades — one shadow, one live — that are independent rows with independent sizing.
- `app/trading/stats.py` computes profit factor / expectancy / drawdown per account, not
  just globally — win rate alone is treated as a misleading metric by design (see README).

When touching the trading engine or parser, keep in mind a single AI response fans out
into up to `len(ALL_ACCOUNTS)` independent position/account updates, not one.

### Two account tiers

Accounts are split into two tiers, both defined in `app/config.py`:

- **`PAPER_ACCOUNTS`** — the original experiment: `ai_selected` + one shadow account per
  strategy, each starting at `START_BALANCE` (10000) with continuous sizing.
- **`REAL_ACCOUNTS`** — a broker-shaped tier (`real_ai_selected`, `real_trend_following` by
  default, set by `REAL_ACCOUNTS`), each starting at `REAL_START_BALANCE` (100). Named
  `real_<source>`; use `real_source_for()` / `is_real_account()` rather than string surgery.
  **Still a simulation — nothing places live broker orders.**

`ALL_ACCOUNTS = PAPER_ACCOUNTS + REAL_ACCOUNTS`. Which one a given code path should use is
load-bearing:

- `cycle.py` builds the AI prompt from **`PAPER_ACCOUNTS` only**, deliberately. The real tier
  mirrors the same signals, so including it would change the prompt and break continuity
  with the analyses already logged by the running production experiment. Don't "fix" this.
- `/api/stats`, `/api/positions` and bare `/api/trades` return **paper only**, so the
  original dashboard sections are unaffected. The real tier has its own
  `/api/real/stats`, `/api/real/positions`, `/api/real/skips`.
- `compute_all_stats` is paper-only; `compute_real_stats` is the real tier.
- `start_balance_for(account)` is the only correct way to get an account's base balance —
  `settings.start_balance` is now wrong for half the accounts.

Account identity lives in **`Trade.account`**, not the legacy `(is_shadow, strategy)` pair.
`is_shadow` is still written for backwards compatibility but is no longer used for
filtering; `_account_filter` matches on `account` alone.

### Real-tier sizing: why $100 can't risk 1%

`app/trading/sizing.py` (`size_real`) is where the real tier diverges, and the arithmetic
is the whole point of the tier. On XAUUSD 1.00 lot = 100 oz, so the minimum 0.01 lot = 1 oz,
and a 1-oz position stopped out at a $14 stop distance loses exactly $14 — 14% of a $100
account. Production stops have ranged $6.70–$25, i.e. **7–25% risk per trade**, so
`REAL_RISK_PER_TRADE` (1%) is unreachable and sizing floors at `REAL_MIN_LOT`
(`Sizing.floored_to_min` records this). Reaching a genuine 1% at a $14 stop needs ~$1,400 of
capital. Consequences to keep in mind:

- `REAL_MAX_RISK_PCT` (default 0.30) is the ceiling on that forced overshoot. Signals above
  it are **skipped, not resized** — nothing smaller than one lot exists. Every skip writes a
  `TradeSkip` row so the dashboard can show what a $100 account had to pass over; silent
  skipping would read as "no signal".
- Losses compound the problem: after one $17 loss on $100, the same $25 stop is 30% of what
  remains, so the cap starts rejecting trades a full-size account would take.
- Margin is checked as `lots * contract_size * entry / leverage <= balance`. At 1:100 a 0.01
  lot of gold needs ~$41 of a $100 balance; below roughly 1:45 no position can be opened at
  all, which is why `REAL_LEVERAGE` is not cosmetic.
- Paper sizing is untouched: `risk_amount / stop_distance` in oz, always hitting 1% exactly.
  `Trade.lots` / `margin_used` / `risk_pct` are NULL for paper trades and populated for real
  ones — that NULL-ness is the cheapest way to tell the tiers apart in raw SQL.

### Simulation semantics (what the paper fills actually assume)

These are deliberate simplifications baked into `engine.py`; know them before trusting or
changing any performance number:

- **Entry fills at the AI's proposed `signal.entry`**, not at the current market price —
  even though `current_price` is available and may differ materially. `entry` is only
  validated for ordering relative to SL/TP, never for proximity to the live price.
- **SL/TP fills at exactly the SL/TP level** (`_close_trade(..., trade.stop_loss, ...)`),
  so there is no slippage, spread, or gap modeling; a gap through the stop still books a
  clean 1R loss.
- **Only the latest close is compared against SL/TP**, not the bar's high/low — intrabar
  touches between price checks are missed entirely.
- **SL is checked before TP** (`if hit_sl: ... elif hit_tp:`), so a bar that spans both
  levels always resolves as a loss.
- Sizing is `risk_amount / |entry - stop_loss|` with no cap, so a very tight stop yields an
  arbitrarily large position; there is no margin or leverage check.
- `run_price_check` calls `fetch_all_timeframes()` (all three timeframes) every minute but
  only reads the last 15m close — the unused `yahoo.get_current_price()` helper is the
  cheap equivalent.

### Data flow summary

```
Yahoo Finance (yfinance) → indicators.summarize → prompts.build_market_briefing
    → ai.client.call_deepseek → ai.parser.parse_and_validate
    → db.models.Analysis (always logged) → trading.engine.process_cycle
    → per-account Trade + EquityPoint rows → web.routes (JSON API) → static dashboard
```

The web layer (`app/web/routes.py`) is a thin read-only JSON API over the same
per-account abstractions (`ALL_ACCOUNTS`, `compute_stats`) — it never writes trades itself.
The frontend is a single static file served directly by FastAPI (`app/web/static/index.html`,
mounted at `/` via `StaticFiles`), not a separate build: vanilla JS polling
`/api/status`, `/api/stats`, `/api/positions`, `/api/trades`, `/api/analyses`. Note the
route mount order — `include_router(router)` before the `/` `StaticFiles` mount is what
keeps `/api/*` reachable.

## Adding or changing a strategy

The strategy stable is defined in **two places that must be kept in sync manually**:

1. `app/config.py` → `STRATEGIES` (a `list[str]`) and `ALL_SELECTIONS` (that list plus
   `"stay_out"`). This drives the parser's per-strategy validation loop and
   `engine.ALL_ACCOUNTS`.
2. `app/ai/strategies/__init__.py` → `STRATEGIES` (a `dict[str, str]` of name → frozen
   rules text), one module per strategy exporting an UPPER_CASE prompt string. This drives
   `SYSTEM_PROMPT` and the JSON schema shown to the model.

Both are imported under the same name `STRATEGIES` from different modules, so check which
one a file means: `prompts.py` uses the dict, `parser.py`/`engine.py` use the list. They
currently hold identical keys in identical order. If they diverge, nothing raises — a name
in the config list but not the dict is never described to the AI nor requested in the
schema, so its evaluation comes back `"missing evaluation"` every cycle while still getting
a (permanently idle) shadow account. A name in the dict but not the list is prompted for
and then discarded.

`STRATEGY_VERSION` in that `__init__.py` is currently unused; the README's intent that
stats never mix across rule versions is not implemented — rule edits silently pollute an
existing strategy's track record.

## Persistence

SQLAlchemy ORM (`app/db/models.py`: `Analysis`, `Trade`, `EquityPoint`) over SQLite
locally / Postgres on Railway (`app/db/database.py`).

- Schema is created with `Base.metadata.create_all` at startup, and because `create_all` only
  creates *missing tables* and never adds a column to an existing one, column additions live
  in **`app/db/migrations.py`** (`run_migrations`, called from `init_db`). There is no Alembic.
  Every step is guarded so it is a no-op on an already-migrated or brand-new database; add
  new columns there, and verify against Postgres as well as SQLite, since raw
  `ALTER TABLE` / `CREATE INDEX IF NOT EXISTS` and boolean predicates behave differently
  across the two. Production carries live trade history, so a wiped DB is not an option.
- Timestamps are naive UTC (`server_default=func.now()`, `dt.datetime.utcnow()`); the
  dashboard compensates by appending `Z` client-side.
- `EquityPoint` rows are **written but never read**: `process_cycle` and
  `manage_open_positions` append one row per account per position change, but no endpoint
  or query consumes them. `stats.py` reconstructs its own equity curve from closed `Trade`
  rows to compute max drawdown, so the table is currently write-only accumulation — wire it
  into an endpoint if you want a real time-series equity curve.
- `scripts/migrate_sqlite_to_postgres.py` is a one-time, idempotent (skips tables with
  existing rows) migration for moving an existing local/Railway SQLite volume to Postgres.

## Treat the README as a design doc, not documentation

`README.md` is the original plan and has drifted from the implementation. Where they
disagree, the code wins. Known divergences: it describes 5 strategies (there are 10);
a `signal` object in the AI contract (the code uses `strategy_evaluations`, one signal per
strategy); a separate `app/trading/shadow.py` (folded into `engine.py`); a `tests/`
directory (absent); buy-and-hold as a comparison baseline (not implemented anywhere);
a candle chart with entry/SL/TP markers (the dashboard is tables only and never calls the
`/api/candles` endpoint, which is served but unused); and the `DASHBOARD_PASSWORD` lock
(not implemented). It is still the best source for *why* the experiment is designed this
way — especially the reasoning on profit factor/expectancy over win rate.

Other defined-but-unused code, so you don't assume it's load-bearing: the `Direction` enum
and `Analysis.trade_id` column in `models.py` (`Trade.direction` is a plain string, and the
Analysis→Trade link exists only as `Trade.analysis_id`), and `yahoo.get_current_price()`.

### MT5 executor (mirroring the real tier onto broker accounts)

`app/trading/executor.py` mirrors the real-tier sim positions onto live MT5 accounts
(currently Exness **demo**). Core design decisions, all deliberate:

- **Reconciliation, not event-forwarding.** Each sync (scheduler job, every
  `EXECUTOR_SYNC_INTERVAL_MIN`) compares the sim's open position per real account (from
  the DB) with broker positions tagged `EXECUTOR_MAGIC`, and issues the minimal orders to
  converge. Stateless across restarts; SL/TP live server-side at the broker, so positions
  stay protected even if everything here dies.
- **Distances, not absolute prices.** The bot's levels come from Yahoo `GC=F` (COMEX
  futures), a few dollars off broker spot XAUUSD. Mirrors open at market and re-derive
  SL/TP by preserving the sim trade's stop/target distances around the actual fill.
  Never "fix" this by copying absolute prices.
- **One terminal, many logins.** MT5's `login()` switches the terminal between accounts,
  so one Wine container serves every account sequentially. Account→login mapping lives in
  `EXECUTOR_ACCOUNTS` (JSON env var; credentials only in Railway variables, never in git).
- Guards: `executor_max_open_age_min` (no late mirrors at drifted prices), an in-memory
  `_opened_once` set (no re-open when the broker's SL fires before the sim's price check
  notices), symbol fallback for Exness suffixes (`XAUUSD` → `XAUUSDm`…), and FOK→IOC
  filling-mode fallback. Status surfaces at `/api/executor/status`.
- The MT5 API is reached via an **mt5linux rpyc bridge** (`rpyc.classic`, port 8001 by
  convention) running inside the Wine/MT5 container. `mt5linux` MUST be installed with
  `--no-deps` (its numpy/urllib3 pins are stale and unresolvable on Python 3.12; only
  `rpyc` is actually needed) — that's why it is absent from `requirements.txt` and
  installed as a separate step in both `Dockerfile` and `Makefile`.

`scripts/verify_executor.py` tests the whole reconciler against a mock backend (open /
idempotence / close / re-open guard / age guard / login-failure isolation / symbol
fallback / distance math). Run it after touching the executor. The real order path
(`Mt5LinuxBackend` → Wine terminal → Exness) cannot run in this environment — it can only
be validated on Railway against the demo accounts.

## Deployment

Single-process Docker deploy (`Dockerfile`) on Railway: one container runs both the
FastAPI web server and the in-process APScheduler jobs — there is no separate worker
process, so scaling to more than one replica would double-execute every cycle and every
account's trades. `DATABASE_URL` is swapped to Railway's Postgres plugin URL in production;
SQLite is for local dev only.

The MT5 terminal is a **second Railway service** (MT5 is Windows-only, so it runs under
Wine): a stock `gmag11/metatrader5_vnc` image, no repo code, reached over Railway's
private network at `MT5_BRIDGE_HOST:MT5_BRIDGE_PORT`. It needs a volume (the terminal
installs itself into `/config` on first boot) and exposes a web-VNC UI for eyeballing the
terminal. The main bot works fine when this service is down — sync just records
"cannot reach MT5 bridge" and retries.
