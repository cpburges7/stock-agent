# Stock Trading Agent

AI-powered trading agent for the Bentley University StockTrak Summer 2026 competition.
Claude analyzes real-time market data and outputs structured buy/sell/short recommendations.
You execute trades manually on StockTrak; the agent tells you what to do.

## Architecture

```
n8n (9:30 AM ET)  ──► POST /analyze  ──► screener → technicals → news → Claude → JSON recs
n8n (hourly)      ──► POST /monitor  ──► open positions → live prices → exit alerts → Gmail
```

## Project Structure

```
stock-agent/
├── main.py                  FastAPI app (/analyze, /monitor, /health, /positions)
├── agent/
│   ├── brain.py             Claude integration + commission validation + retry logic
│   ├── data_fetcher.py      Assembles full data packet (screener + technicals + news)
│   ├── screener.py          Dynamic watchlist: most-actives + volume + earnings + sectors
│   ├── technical.py         RSI, MACD, SMA, Bollinger Bands, ATR, volume ratio (pandas-ta)
│   ├── news.py              Alpaca → NewsAPI → RSS fallback, sentiment tagging
│   ├── monitor.py           Hourly exit logic: take-profit / stop-loss / time-stop alerts
│   ├── logger.py            Persistent trade log (logs/trades.json)
│   └── prompts.py           All Claude prompts + data packet formatter
├── logs/trades.json         Auto-created; gitignored
└── tests/test_brain.py      Unit tests (commission math, validation, retry logic)
```

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Fill in: ANTHROPIC_API_KEY, ALPACA_API_KEY, ALPACA_SECRET_KEY, AGENT_API_KEY
# Optional: NEWS_API_KEY (newsapi.org free tier)
```

## Running Locally

```bash
# Full morning analysis (builds watchlist, fetches data, calls Claude)
python -m agent.brain

# Hourly position monitor
python -m agent.monitor

# Test individual modules
python -m agent.screener
python -m agent.technical
python -m agent.news

# API server
uvicorn main:app --reload --port 8000

# Tests
python -m pytest tests/ -v
```

## API Endpoints

All endpoints require `X-API-Key: <AGENT_API_KEY>` header.

### `POST /analyze`
Called by n8n at 9:30 AM ET. Send current portfolio state; receive full recommendations.

```json
{
  "portfolio": {
    "cash": 50008.22,
    "total_value": 50008.22,
    "open_positions": [],
    "open_shorts": []
  },
  "triggered_by": "n8n"
}
```

### `POST /monitor`
Called by n8n hourly. Returns exit alerts for open positions.

```json
{ "alerts": [...], "count": 2 }
```

### `PATCH /positions`
Call after manually executing a trade on StockTrak to keep the monitor in sync.

```json
{
  "open_positions": [
    {
      "ticker": "NVDA",
      "direction": "LONG",
      "entry_price": 142.50,
      "shares": 69,
      "target_price": 158.00,
      "stop_loss": 135.00,
      "time_horizon_days": 4,
      "opened_date": "2026-06-09"
    }
  ],
  "open_shorts": []
}
```

### `GET /health`
Returns `{"status": "ok", "time": "..."}`.

## Key Rules (Enforced in Code)

| Rule | Enforcement |
|------|------------|
| No fractional shares | `math.floor()` in `brain._correct_long/short()` |
| $10 commission per trade | Added to every cost calculation |
| Min trade $1,000 | Rejected in `_correct_long/short()` |
| Max 25% per position | Capped in `_correct_long/short()` |
| Min price $3.00 | Rejected in validation + screener filter |
| 15% cash reserve | Enforced by `_enforce_cash_reserve()` |
| Confidence ≥ 70 to trade | Claude prompt + watchlist-only below threshold |

## n8n Workflow Setup

1. Create HTTP Request node → `POST https://your-railway-url/analyze`
2. Add header: `X-API-Key: <AGENT_API_KEY>`
3. Body: JSON with portfolio state (cash, total_value, open_positions, open_shorts)
4. Schedule: Weekdays 9:30 AM ET
5. Second workflow: `POST /monitor` every hour 10 AM–3:30 PM ET
6. Add Gmail node: send alert email if `response.count > 0`

## Deployment (Railway)

1. Push to GitHub
2. Connect repo in Railway → auto-deploys on push
3. Set environment variables in Railway dashboard (same as `.env`)
4. `Procfile` already configured: `web: uvicorn main:app --host 0.0.0.0 --port $PORT`

## API Keys Needed

| Key | Where to get |
|-----|-------------|
| `ANTHROPIC_API_KEY` | console.anthropic.com |
| `ALPACA_API_KEY` + `ALPACA_SECRET_KEY` | alpaca.markets (free account) |
| `NEWS_API_KEY` | newsapi.org (free tier, optional) |
| `AGENT_API_KEY` | Generate any strong random string |
