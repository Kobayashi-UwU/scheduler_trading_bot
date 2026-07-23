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

There is no test suite, linter, or formatter configured in this repo (no pytest/ruff/black
in `requirements.txt`, `tests/` is empty). Don't assume `pytest` or `make test` exists.

Dashboard runs at `http://localhost:8000` once the server is up.

## Configuration

All settings come from `app/config.py` (`pydantic-settings`), loaded from `.env` (see
`.env.example` for the full list). Key ones: `NVIDIA_API_KEY` (required, no default),
`SYMBOL` (default `GC=F`, COMEX gold futures), `ANALYSIS_INTERVAL_MIN`, `START_BALANCE`,
`RISK_PER_TRADE`, `MIN_CONFIDENCE`, `MIN_RISK_REWARD`, `MAX_OPEN_POSITIONS`,
`DATABASE_URL` (defaults to local SQLite; Railway sets this to Postgres — note Railway
gives `postgres://` and `config.py` normalizes it to `postgresql://` for SQLAlchemy).

`STRATEGIES` and `ALL_SELECTIONS` (the fixed strategy stable) also live in `app/config.py`
— this is the single source of truth for what strategies exist; adding a new strategy
means updating this list AND adding a prompt/rules file under `app/ai/strategies/`.

## Architecture: the cycle is the core

Everything revolves around `app/cycle.py`, run by two APScheduler jobs registered in
`app/scheduler.py`:

- **`run_analysis_cycle`** (every `ANALYSIS_INTERVAL_MIN`, default 15): the full AI loop.
- **`run_price_check`** (every `PRICE_CHECK_INTERVAL_MIN`, default 1): fast job that just
  marks open positions to market and auto-closes on SL/TP hits, without calling the AI.

Both skip entirely when `is_market_open()` (in `app/data/yahoo.py`) says gold markets are
closed (weekends).

### The analysis cycle, step by step

1. `app/data/yahoo.py` fetches multi-timeframe candles (`fetch_all_timeframes`); falls
   back from `SYMBOL` to `FALLBACK_SYMBOL` if the primary fetch fails.
2. `app/data/indicators.py` (`summarize`) computes EMA/RSI/MACD/ATR etc. per timeframe.
3. `app/ai/prompts.py` (`build_market_briefing`) builds the user prompt: indicator
   summary + current open positions + recent trade history, **per account** (see below).
4. `app/ai/client.py` (`call_deepseek`) calls the NVIDIA chat-completions endpoint.
   Notable non-obvious behavior:
   - Tries `nvidia_model`, then `nvidia_fallback_model`, then hardcoded
     `deepseek-ai/deepseek-v4-flash`, but **always sorts flash-tier models first** —
     `-pro` models on the NVIDIA free tier tend to hang, so flash is tried first even if
     configured as the fallback, and `-pro` gets zero retries plus a short 60s read
     timeout to fail fast.
   - Flash-tier calls use non-streaming; `-pro` uses SSE streaming with a non-streaming
     retry if the stream comes back empty.
   - Raises `AIClientError` on total failure; `cycle.py` catches this and logs a rejected
     `Analysis` row rather than crashing the cycle.
5. `app/ai/parser.py` (`parse_and_validate`) strictly validates the JSON contract: regime,
   selected strategy, and a signal **per strategy** (not just the selected one — see
   shadow trading), checking schema, min confidence, min R:R, etc. Anything invalid becomes
   `ok=False` with a `reject_reason` and never reaches the trading engine.
6. `app/trading/engine.py` (`process_cycle`) executes trades. Every `Analysis` row is
   logged regardless of whether it produced a trade.
7. `manage_open_positions` runs once more immediately after, using the same price, to
   catch SL/TP hits that just opened.

### Shadow trading — the key architectural concept

The AI doesn't just pick one strategy and trade it — **every strategy in `STRATEGIES`
runs its own simulated account in parallel** ("shadow" accounts), plus one extra
`"ai_selected"` account that trades whichever strategy the AI chose that cycle. This is
the experiment's control group: it lets you compare "the AI's strategy selection" against
"just running each individual strategy alone" and against buy-and-hold.

This shows up throughout the trading layer:

- `app/trading/engine.py`: `ALL_ACCOUNTS = [AI_SELECTED_ACCOUNT] + STRATEGIES`. Every
  balance/position/trade-history query takes an `account` string and filters
  `Trade.is_shadow` + `Trade.strategy` accordingly (`_account_filter`). The
  `ai_selected` account is `is_shadow=False`; every strategy's own shadow account is
  `is_shadow=True`.
- `Trade.risk_amount`/size are computed **per account's own balance**, so each shadow
  account compounds independently.
- `process_cycle` first applies every strategy's `evaluations[strategy]` signal to that
  strategy's own shadow account, then separately applies `parsed.live_signal` (the AI's
  actually-selected strategy) to `ai_selected`.
- An `EquityPoint` row is written **per account** after every position change, which is
  what powers the strategy-leaderboard / equity-curve comparison in the dashboard.
- `app/trading/stats.py` computes profit factor / expectancy / drawdown per account, not
  just globally — win rate alone is treated as a misleading metric by design (see README).

When touching the trading engine or parser, keep in mind a single AI response fans out
into up to `len(STRATEGIES) + 1` independent position/account updates, not one.

### Data flow summary

```
Yahoo Finance (yfinance) → indicators.summarize → prompts.build_market_briefing
    → ai.client.call_deepseek → ai.parser.parse_and_validate
    → db.models.Analysis (always logged) → trading.engine.process_cycle
    → per-account Trade + EquityPoint rows → web.routes (JSON API) → static dashboard
```

Persistence is SQLAlchemy ORM (`app/db/models.py`: `Analysis`, `Trade`, `EquityPoint`) over
SQLite locally / Postgres on Railway (`app/db/database.py`). `scripts/migrate_sqlite_to_postgres.py`
is a one-time, idempotent (skips tables with existing rows) migration for moving an existing
local/Railway SQLite volume to Postgres.

The web layer (`app/web/routes.py`) is a thin read-only JSON API over the same
per-account abstractions (`ALL_ACCOUNTS`, `compute_stats`) — it never writes trades itself.
The frontend is static files served directly by FastAPI (`app/web/static/`, mounted at `/`),
not a separate build.

## Deployment

Single-process Docker deploy (`Dockerfile`) on Railway: one container runs both the
FastAPI web server and the in-process APScheduler jobs — there is no separate worker
process. `DATABASE_URL` is swapped to Railway's Postgres plugin URL in production; SQLite
is for local dev only.
