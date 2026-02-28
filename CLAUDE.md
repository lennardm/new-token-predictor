# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

- **tokens** — one row per pump.fun token; tracks lifecycle status (`watching` → `tracking` → `done` / `dead`)
- **trades** — individual trade events; `UNIQUE(signature)` with `INSERT OR IGNORE`
- **token_risk** — RugCheck/GoPlus enrichment; UPSERT on `token_mint`
- **social_mentions** — CryptoPanic mention counts at collection time + 24h
- **price_snapshots** — DexScreener/SolanaTracker snapshots at 1h/2h/4h/8h/24h; `UNIQUE(token_mint, delay_minutes)`
- **market_snapshots** — CoinGecko SOL + BTC price/change/volume polled every 5 min; `UNIQUE(CAST(ts/300 AS INTEGER))` deduplicates within each 5-minute window

## Key Design Decisions

- Single persistent WebSocket to `pumpportal.fun` with exponential backoff on disconnect
- Viability filter: < 10 buys OR < 5 unique buyers in first 60s → status `dead`, unsubscribed
- All background services (`enricher`, `price_fetcher`) launched together via `asyncio.gather()` in `collector.py`
- `price_fetcher.run()` polls CoinGecko at the top of every 5-minute cycle for macro data before processing token snapshots
- DexScreener batch endpoint: `/tokens/v1/solana/{mint1,mint2,...}` (up to 30 per call)
- GoPlus used as fallback if RugCheck fails
- Running ATH market cap computed across snapshots (DexScreener doesn't expose ATH directly)
- `pumpfun_launch_rate_1h` is not stored — computed at analysis time from raw token timestamps to avoid redundancy

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
| SolanaTracker | Fallback when DexScreener has no data |

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
| `SOLANATRACKER_KEY` | Optional | SolanaTracker API key (fallback price source) |
