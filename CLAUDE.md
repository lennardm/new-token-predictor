# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Goal

**Can we predict, with useful certainty, whether a new pump.fun token will reach a target market cap?**

Given all trade data from the first X minutes of a token's life plus enrichment metrics (rug score, holder distribution, social mentions, macro market conditions), train a classifier to estimate the probability that the token will reach market cap thresholds such as $20k, $30k, $50k, $100k, $1M.

Roadmap:
1. **Data collection** (current focus) — run `collector.py` continuously to build a labelled dataset; `price_snapshots` at 1h/2h/4h/8h/24h provide the ground-truth outcome labels
2. **Prediction** — use `analyzer.py` to train and evaluate models; target: probability estimates with better-than-random accuracy (baseline is ~50% for binary thresholds, but even 55–60% edge is tradeable)
3. **Live inference** — feed real-time trade data and metrics into the trained model as new tokens arrive
4. **Trading bot** — act on predicted probabilities: buy tokens above a confidence threshold, sell at target or stop-loss

The key question for step 2: at what prediction confidence does the signal become actionable? That depends on the win-rate needed to be profitable after fees and slippage.

## Project

`new-token-predictor` — monitors pump.fun token launches in real time, collects trades for the first 20 minutes, enriches tokens with risk/social data, fetches price snapshots at scheduled delays, and captures macro market conditions for outcome correlation and ML analysis.

## File Structure

| File | Purpose |
|------|---------|
| `config.py` | All constants and env-based API keys |
| `db.py` | SQLite schema + all CRUD helpers |
| `collector.py` | WebSocket listener — entry point, launches enricher + price_fetcher |
| `enricher.py` | Background RugCheck / GoPlus / CryptoPanic loop (every 30s) |
| `price_fetcher.py` | Background DexScreener / SolanaTracker / CoinGecko snapshot loop (every 5 min) |
| `analyzer.py` | Offline correlation + ML analysis script |
| `.env.example` | Template for `CRYPTOPANIC_TOKEN`, `SOLANATRACKER_KEY` |
| `requirements.txt` | Python dependencies |
| `data/tokens.db` | SQLite database (created at runtime) |

## Database Schema

- **tokens** — one row per pump.fun token; tracks lifecycle status (`watching` → `tracking` → `done` / `dead`); `bonded_at` = DexScreener-detected graduation time (up to 5 min late); `migrated_at` = real-time PumpPortal migration event timestamp (accurate to the second)
- **trades** — individual trade events; `UNIQUE(signature)` with `INSERT OR IGNORE`
- **token_risk** — RugCheck/GoPlus enrichment + SolanaTracker risk data; UPSERT on `token_mint`; ST columns: score, rugged, jupiter_verified, top10_pct, snipers (count/pct/balance), bundlers (count/pct/balance/initial_pct/initial_balance), insiders (count/pct/balance), dev (pct/amount), curve_pct, holders
- **social_mentions** — CryptoPanic mention counts at collection time + 24h
- **price_snapshots** — DexScreener/SolanaTracker snapshots at 1h/2h/4h/8h/24h; `UNIQUE(token_mint, delay_minutes)`; columns: price, market cap, liquidity, volume (1h/6h/24h), price change % (1h/6h/24h), buy/sell counts (1h/24h), ATH price + market cap
- **market_snapshots** — CoinGecko SOL + BTC price/change/volume polled every 5 min; `UNIQUE(CAST(ts/300 AS INTEGER))` deduplicates within each 5-minute window

## Key Design Decisions

- Single persistent WebSocket to `pumpportal.fun` with exponential backoff on disconnect
- Graceful shutdown via `asyncio.Event`: SIGINT/SIGTERM sets the event, `main()` cancels subtasks and awaits them — no `CancelledError` traceback
- New token detection: `event.get("txType") == "create"` — the API now always sets `txType=create` on creation events; trade events use `buy`/`sell`
- Migration tracking: `subscribeNewMigration` subscribed on every connect; `txType=migrate` events set `migrated_at` and unsubscribe from token trades if still active (PumpSwap trades are not on the pumpportal stream); price snapshot collection continues unaffected; `time_to_migration = migrated_at - created_at` computed at analysis time
- `_unsubscribe_token` swallows `ConnectionClosed` — background viability/collection tasks survive WebSocket reconnects without crashing
- `_active_subscriptions` cleared on each reconnect so the new connection starts with a clean slate
- `data/` directory created automatically by `db.init_db()` via `os.makedirs(..., exist_ok=True)`
- Viability filter: < 10 buys OR < 5 unique buyers in first 60s → status `dead`, unsubscribed
- All background services (`enricher`, `price_fetcher`) launched as tasks in `main()`, awaited on shutdown
- `price_fetcher.run()` polls CoinGecko at the top of every 5-minute cycle for macro data before processing token snapshots
- GoPlus used as fallback if RugCheck fails
- DexScreener batch endpoint: `/tokens/v1/solana/{mint1,mint2,...}` (up to 30 per call); parsed fields: price, market cap, liquidity, volume (h1/h6/h24), priceChange (h1/h6/h24), txns buys+sells (h1/h24)
- ATH data: SolanaTracker `/tokens/{mint}/ath` called after DexScreener for bonded tokens only; returns `{highest_price, highest_market_cap, timestamp}` (timestamp in ms, converted to s)
- ST risk strategy: two separate paths — (1) `enricher.py` calls POST `/tokens/multi` (up to 20 mints per request) for ALL tokens passing the 60s viability filter, capturing full risk data ~45s after creation; (2) `price_fetcher.py` calls GET `/tokens/{mint}` for bonded tokens at snapshot time if ST data not yet stored
- `/tokens/multi` response structure: `{"tokens": {"{mint}": {risk, pools, events, buys, sells, holders, ...}}}`; parsed by `_parse_st_risk()` in `enricher.py`; `_fetch_solanatracker_risk()` in `price_fetcher.py` uses the same full field set
- `st_curve_pct` extracted from `pools[].curvePercentage` — present only on pump.fun bonding curve pools, None after bonding; useful as a live signal of how close a token is to graduating
- `upsert_token_risk_st()` updates only ST columns; `upsert_token_risk()` updates only RugCheck/GoPlus columns — both use `ON CONFLICT DO UPDATE` so they are safe to call in any order
- Startup recovery: on every connect/reconnect, `_recover_active_tokens()` re-subscribes `watching`/`tracking` tokens and reschedules their viability/collection tasks based on elapsed time; tokens past their windows are resolved immediately from stored trade data
- `_viability_check` accepts optional `sleep_sec` so recovery can wait out only the remaining viability window rather than a full 60s
- `pumpfun_launch_rate_1h` is not stored — computed at analysis time from raw token timestamps to avoid redundancy
- `market_snapshots` dedup uses `CREATE UNIQUE INDEX ON market_snapshots(CAST(ts / 300 AS INTEGER))` — SQLite does not support expressions in inline `UNIQUE` constraints
- Schema migrations handled by `_migrate()` in `db.py` — uses `PRAGMA table_info` to detect and `ALTER TABLE ADD COLUMN` for any missing columns; safe to run against existing DBs

## Macro Features (market_snapshots → analyzer.py)

| Feature | Source |
|---------|--------|
| `sol_price_usd` | CoinGecko at launch time |
| `sol_change_24h_pct` | CoinGecko — uptrend signal |
| `sol_volume_24h_usd` | CoinGecko — ecosystem activity |
| `btc_price_usd` | CoinGecko at launch time |
| `btc_change_24h_pct` | CoinGecko — market sentiment |
| `pumpfun_launch_rate_1h` | Computed from `tokens` table in `load_macro_features()` |

`analyzer.load_macro_features()` uses `pd.merge_asof(..., direction="backward")` to join each token to the latest market snapshot captured before its `created_at`.

## API Rate Budget

| API | Usage |
|-----|-------|
| CoinGecko (free, 10–30 RPM) | 1 req / 5 min = 0.2 RPM |
| DexScreener (55 RPM cap) | Batched, rate-limited via `_DS_MIN_INTERVAL` |
| RugCheck / GoPlus | On-demand per token, 45s after creation |
| CryptoPanic (100 req/month free) | Sampled — only tokens with buy volume ≥ `CRYPTOPANIC_MIN_BUY_SOL` (default 5 SOL) |
| SolanaTracker | POST `/tokens/multi` (≤20 mints) in enricher for all tracking tokens; GET `/tokens/{mint}/ath` for bonded token ATH; GET `/tokens/{mint}` as full price fallback |

### CryptoPanic sampling

CryptoPanic is limited to 100 requests/month on the free plan. To stay within budget, calls are gated by `CRYPTOPANIC_MIN_BUY_SOL` (set in `config.py`):

- **`delay_minutes=0`** (`enricher.py`) — skipped if total buy SOL recorded in `trades` at ~45s is below the threshold
- **`delay_minutes=1440`** (`price_fetcher.py`) — skipped if the same token never met the threshold (consistent gate)

The default of 5 SOL is a starting guess. Tune it after a few days of data using:

```sql
SELECT ROUND(SUM(sol_amount), 2) AS buy_vol_sol
FROM trades
WHERE tx_type = 'buy'
GROUP BY token_mint
ORDER BY buy_vol_sol DESC;
```

## Running

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in CRYPTOPANIC_TOKEN and SOLANATRACKER_KEY
python collector.py    # starts WebSocket + enricher + price_fetcher
python analyzer.py     # offline analysis after data collection
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `CRYPTOPANIC_TOKEN` | Optional | CryptoPanic API key for social mentions |
| `SOLANATRACKER_KEY` | Optional | SolanaTracker API key (ATH data + fallback price source) |
