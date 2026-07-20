# 🤖 AI Gold Trader — XAUUSD Paper-Trading Experiment

An experiment to answer one question: **Can an LLM (DeepSeek V4 Pro) actually trade?**

The bot pulls live XAUUSD (gold) price data from Yahoo Finance, asks DeepSeek to analyze
the chart and propose trades, then **paper-trades** those signals (no real money, ever).
Every prediction and its outcome is logged so we can measure the AI's real performance
over time, and a web dashboard lets us monitor everything from anywhere.

> ⚠️ **This is a simulation / research project.** No real orders are placed. It exists
> purely to test whether AI predictions have any edge.

---

## 1. High-Level Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────────┐
│  Yahoo Finance   │────▶│   Data Service    │────▶│   AI Analyst        │
│  (XAUUSD OHLCV)  │     │  fetch + indicators│     │ (DeepSeek V4 Pro    │
└─────────────────┘     └──────────────────┘     │  via NVIDIA API)    │
                                                  └──────────┬──────────┘
                                                             │ trade signal (JSON)
                                                             ▼
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────────┐
│  Web Dashboard   │◀────│    Database       │◀────│  Paper-Trade Engine │
│  (monitor 24/7)  │     │ (trades, P&L,     │     │ opens/closes mock   │
└─────────────────┘     │  AI reasoning)    │     │ positions vs price  │
                        └──────────────────┘     └─────────────────────┘

                     Everything runs on a scheduler (e.g. every 15 min)
                     and is deployed on Railway.
```

## 2. Tech Stack

| Layer          | Choice                              | Why                                             |
|----------------|-------------------------------------|-------------------------------------------------|
| Backend        | **Python + FastAPI**                | `yfinance` makes Yahoo data trivial; async API   |
| Scheduler      | **APScheduler** (in-process)        | One process = simplest Railway deploy            |
| Database       | **SQLite** (Railway volume) → can upgrade to Postgres later | Zero-config, good enough for logs |
| AI             | **DeepSeek V4 Pro** via NVIDIA `integrate.api.nvidia.com` (free tier) | Already have the key |
| Frontend       | **Single-page dashboard** (HTML + [Lightweight Charts](https://github.com/tradingview/lightweight-charts) + vanilla JS or Alpine.js) served by FastAPI | No separate frontend deploy |
| Deploy         | **Railway** (Dockerfile or Nixpacks) | Always-on worker + web in one service            |

## 3. Data: Yahoo Finance

- **Symbol:** `GC=F` (COMEX gold futures — most reliable on Yahoo) with `XAUUSD=X` as fallback.
- **Library:** `yfinance` (free, no API key).
- **What we fetch each cycle:**
  - Candles: last N bars on multiple timeframes (e.g. 15m × 200 bars, 1h × 100, 1d × 60)
  - Current price for marking open positions to market.
- **Computed indicators** (so the AI gets more than raw candles): EMA 20/50/200, RSI 14,
  MACD, ATR 14, recent swing highs/lows, daily change %.
- Gold market hours: closed weekends (~Fri 22:00 → Sun 23:00 UTC). The scheduler skips
  cycles when the market is closed.

## 4. AI Analysis Loop (the core) — Multi-Strategy Selection

**Design principle:** a stateless LLM that freestyles a new trading philosophy every call
is unmeasurable. Instead, we define a **fixed stable of strategies** (frozen rules), and
the AI's job each cycle is to (1) classify the market situation, (2) pick the strategy
that fits — or stay out — and (3) apply that strategy's rules to produce a trade.
Every layer is logged separately so we can tell *which layer* was right or wrong.

```
market data → AI: classify regime → AI: select strategy (or STAY_OUT)
           → apply that strategy's fixed rules → trade signal
           → meanwhile: shadow-execute ALL strategies for comparison (§5)
```

### The strategy stable (v1 — start with 5, not 10)

| # | Strategy            | Fixed rules (summary)                                          |
|---|---------------------|----------------------------------------------------------------|
| 1 | `trend_following`   | Trade 15m pullbacks in the direction of the 1h + 1d trend      |
| 2 | `mean_reversion`    | Fade the edges when price is ranging between clear levels      |
| 3 | `breakout`          | Enter on breaks of consolidation with ATR/volume expansion     |
| 4 | `sr_bounce`         | Trade reactions at major daily support/resistance levels       |
| 5 | `stay_out`          | Explicit "no strategy fits, don't trade" — prevents forced trades |

Each strategy lives in its own prompt file with frozen entry/exit/SL/TP rules and a
minimum 1:2 risk/reward. Rules changes = new **strategy version** (`trend_following_v2`),
and stats are never mixed across versions.

### The cycle (every 15 minutes, configurable)

1. **Fetch** fresh candles + compute indicators (multi-timeframe).
2. **Build market briefing** — compact structured summary:
   - OHLCV summary per timeframe + indicator values + key levels
   - Current open paper positions + running P&L
   - Recent trade history per strategy (in-context "memory" of what's been working)
3. **Call DeepSeek** (`deepseek-ai/deepseek-v4-pro`, NVIDIA endpoint, **temperature ≈ 0.1**
   — near-deterministic, same market → same decision) with a strict JSON-only contract:

   ```json
   {
     "regime": "trending_up | trending_down | ranging | volatile | unclear",
     "regime_reasoning": "why",
     "selected_strategy": "trend_following | mean_reversion | breakout | sr_bounce | stay_out",
     "selection_reasoning": "why this strategy fits the regime",
     "signal": {
       "action": "BUY | SELL | HOLD | CLOSE",
       "confidence": 0.0,
       "entry": 2410.5,
       "stop_loss": 2402.0,
       "take_profit": 2428.0,
       "risk_reward": 2.1,
       "reasoning": "how the strategy's rules apply to this setup"
     }
   }
   ```

4. **Validate** (schema, sane SL/TP distances, min R:R, min confidence, max open
   positions, signal must be consistent with the selected strategy's rules).
   Bad output → logged as `REJECTED`, no trade.
5. **Execute on paper** via the trade engine; shadow-execute the other strategies (§5).
6. **Log everything**: regime call, strategy selection, full prompt, raw response,
   parsed signal, decision — each layer separately queryable.

## 5. Paper-Trade Engine + Shadow Trading

Simulates a small account (e.g. starting balance **$10,000**, fixed risk per trade e.g. 1%).

- **Open:** on BUY/SELL signal above the confidence threshold → record entry price,
  SL, TP, size, timestamp, strategy used, and the AI's reasoning.
- **Manage:** every cycle (and on a faster 1-min price check), mark positions to the
  latest price. If price crosses SL or TP → auto-close and record the result.
- **Close:** AI can also emit `CLOSE` to exit early.
- **Track:** balance curve, win rate, **profit factor**, expectancy, average R multiple,
  max drawdown — per strategy AND for the AI-selected portfolio.

### Shadow trading — the control group 🔬

Every cycle, *all* strategies are evaluated and paper-traded in parallel virtual
accounts; the AI-selected one is additionally marked as the "real" pick. After enough
trades the dashboard answers the headline question:

> **Does the AI's strategy selection beat just running the best single strategy alone?**

- AI-selected P&L > every individual strategy → the AI adds real value reading the market.
- A single strategy alone beats the AI's picks → the selection layer is negative value.
- Also compared against **buy-and-hold gold** as a baseline.

Note on metrics: win rate alone is misleading (a 40% win rate with 1:3 R:R is very
profitable; 70% with poor R:R loses money). Profit factor and expectancy are the
scoreboard; win rate is just context.

## 6. Web Dashboard (deployed on Railway)

Single page, mobile-friendly, auto-refreshing:

- 📈 **Live chart** — XAUUSD candles with entry/SL/TP markers for every AI trade
- 💼 **Open positions** — direction, entry, current P&L, distance to SL/TP
- 📜 **Trade history** — every closed trade with outcome and the AI's original reasoning
- 🧠 **AI log** — each analysis cycle: what the AI saw, what it said, what we did
- 🏆 **Performance stats** — balance curve, win rate, profit factor, drawdown
- ⚔️ **Strategy leaderboard** — AI-selected portfolio vs each shadow strategy vs
  buy-and-hold, plus how often each strategy/regime was picked and how it did
- Optional simple password (`DASHBOARD_PASSWORD`) since it's public on Railway.

API endpoints backing it: `GET /api/status`, `/api/positions`, `/api/trades`,
`/api/analyses`, `/api/stats`, `/api/candles`.

## 7. Project Structure

```
scheduler_trading_bot/
├── README.md
├── .env.example            # NVIDIA_API_KEY=..., SYMBOL=GC=F, INTERVAL_MIN=15, ...
├── .gitignore              # .env, nvidia_api.sh, *.db  ← key never committed!
├── requirements.txt
├── Dockerfile              # or rely on Railway Nixpacks
├── app/
│   ├── main.py             # FastAPI app + scheduler startup
│   ├── config.py           # env-based settings
│   ├── data/
│   │   ├── yahoo.py        # candle fetching (yfinance)
│   │   └── indicators.py   # EMA/RSI/MACD/ATR etc.
│   ├── ai/
│   │   ├── client.py       # NVIDIA/DeepSeek API wrapper (retry, timeout, temp≈0.1)
│   │   ├── prompts.py      # market-briefing + regime/selection prompt builders
│   │   ├── strategies/     # one file per strategy: frozen rules, versioned
│   │   │   ├── trend_following.py
│   │   │   ├── mean_reversion.py
│   │   │   ├── breakout.py
│   │   │   └── sr_bounce.py
│   │   └── parser.py       # strict JSON validation (regime, selection, signal)
│   ├── trading/
│   │   ├── engine.py       # paper positions, SL/TP fills, balance
│   │   ├── shadow.py       # parallel virtual accounts, one per strategy
│   │   └── stats.py        # profit factor, expectancy, drawdown — per strategy
│   ├── db/
│   │   ├── models.py       # Trade, Analysis, EquityPoint (SQLAlchemy)
│   │   └── database.py
│   ├── scheduler.py        # 15-min analysis job + 1-min price-check job
│   └── web/
│       ├── routes.py       # JSON API
│       └── static/         # dashboard (index.html, chart.js code)
└── tests/
    ├── test_parser.py
    └── test_engine.py
```

## 8. Open Questions (decide during implementation)

1. Exact frozen rules for each of the 5 strategies (entry, exit, SL/TP placement).
2. One AI call doing regime + selection + signal, vs two calls (classify first, then
   apply the chosen strategy in a focused second call).
3. Analysis interval: 15 min vs 1 h (fewer, higher-quality decisions vs more data points).
4. One position at a time vs one per strategy (shadow accounts always run one each).
5. How much trade history to feed back into the prompt (in-context "learning").
6. When to expand the stable beyond 5 strategies (only after each has ~50+ trades).
7. SQLite volume vs Railway Postgres once data grows.

## 9. Roadmap

- **Phase 1 — Core pipeline** ✅ plan → fetch Yahoo data, call DeepSeek, parse signal, log to DB (CLI only)
- **Phase 2 — Paper engine**: positions, SL/TP simulation, balance & stats
- **Phase 2.5 — Strategy stable + shadow trading**: 5 strategies, regime/selection layer, parallel virtual accounts
- **Phase 3 — Web dashboard**: FastAPI routes + chart UI + strategy leaderboard
- **Phase 4 — Railway deploy**: Dockerfile, env vars, volume for SQLite, health check
- **Phase 5 — Experiment & iterate**: run for weeks, compare strategy prompts, publish results

## 10. Environment Variables

| Variable            | Purpose                                   |
|---------------------|-------------------------------------------|
| `NVIDIA_API_KEY`    | DeepSeek access (never hard-code / commit)|
| `SYMBOL`            | default `GC=F`                            |
| `ANALYSIS_INTERVAL` | minutes between AI calls (default 15)     |
| `START_BALANCE`     | paper account size (default 10000)        |
| `RISK_PER_TRADE`    | fraction of balance risked (default 0.01) |
| `MIN_CONFIDENCE`    | signal threshold (default 0.6)            |
| `DASHBOARD_PASSWORD`| optional dashboard lock                   |

## 11. Free-Tier Constraints & Mitigations

- **NVIDIA free API**: rate limits / occasional slowness → retry with backoff, cap at
  ~96 calls/day (15-min cycle), never block the web server on AI calls.
- **yfinance**: unofficial API, can throttle → cache candles, back off on failure,
  fall back to `XAUUSD=X` if `GC=F` fails.
- **Railway**: keep it a single small service; SQLite on a volume avoids a paid DB.
# scheduler_trading_bot
