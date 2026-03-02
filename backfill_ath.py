"""
backfill_ath.py — One-off script to fill in missing ath_price_usd /
ath_market_cap_usd on price_snapshots rows collected before the
SolanaTracker ATH fix.

Safe to run alongside collector.py (WAL mode).
Safe to interrupt and re-run — only NULL rows are touched.
"""

import asyncio
import logging
import sqlite3
import time

import aiohttp
from dotenv import load_dotenv

load_dotenv()

import db
from config import DB_PATH, SOLANATRACKER_BASE, SOLANATRACKER_KEY

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("backfill_ath")

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)
REQ_INTERVAL = 15.0   # seconds between calls — collector also hits SolanaTracker heavily
RATE_LIMIT_BACKOFF = 300  # 5 min pause on 429 to let collector's snapshot cycle pass


def _float(val) -> float | None:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


async def fetch_ath(session: aiohttp.ClientSession, mint: str) -> tuple[float | None, float | None]:
    """Returns (ath_price_usd, ath_market_cap_usd) or (None, None) on failure."""
    if not SOLANATRACKER_KEY:
        return None, None
    url = f"{SOLANATRACKER_BASE}/tokens/{mint}"
    headers = {"x-api-key": SOLANATRACKER_KEY}
    while True:
        try:
            async with session.get(url, headers=headers, timeout=REQUEST_TIMEOUT) as resp:
                if resp.status == 429:
                    log.warning("Rate limited — backing off %ds", RATE_LIMIT_BACKOFF)
                    await asyncio.sleep(RATE_LIMIT_BACKOFF)
                    continue
                if resp.status != 200:
                    return None, None
                data = await resp.json(content_type=None)
                break
        except Exception as exc:
            log.debug("SolanaTracker failed for %s: %s", mint, exc)
            return None, None

    # Pick pool with highest liquidity
    pools = data.get("pools", [])
    best: dict = {}
    for pool in pools:
        if not isinstance(pool, dict):
            continue
        liq = (pool.get("liquidity") or {}).get("usd", 0) or 0
        best_liq = (best.get("liquidity") or {}).get("usd", 0) or 0
        if liq > best_liq:
            best = pool
    if not best:
        best = data

    price_usd  = _float((best.get("price") or {}).get("usd") or best.get("priceUsd"))
    market_cap = _float(best.get("marketCap") or best.get("market_cap"))
    ath_price  = _float(data.get("ath") or data.get("athPrice"))

    ath_market_cap = None
    if ath_price and price_usd and market_cap and price_usd > 0:
        ath_market_cap = ath_price * (market_cap / price_usd)

    return ath_price, ath_market_cap


async def run() -> None:
    if not SOLANATRACKER_KEY:
        log.error("SOLANATRACKER_KEY not set — nothing to do")
        return

    conn = db.init_db(DB_PATH)

    mints = [
        row[0] for row in conn.execute("""
            SELECT DISTINCT token_mint
            FROM price_snapshots
            WHERE ath_market_cap_usd IS NULL AND source != 'none'
            ORDER BY token_mint
        """).fetchall()
    ]

    total = len(mints)
    log.info("Tokens to backfill: %d", total)

    done = skipped = 0
    t_start = time.time()

    async with aiohttp.ClientSession() as session:
        for i, mint in enumerate(mints, 1):
            ath_price, ath_mc = await fetch_ath(session, mint)

            if ath_price is not None or ath_mc is not None:
                conn.execute(
                    """
                    UPDATE price_snapshots
                    SET ath_price_usd = ?, ath_market_cap_usd = ?
                    WHERE token_mint = ? AND ath_market_cap_usd IS NULL
                    """,
                    (ath_price, ath_mc, mint),
                )
                conn.commit()
                done += 1
            else:
                skipped += 1

            if i % 50 == 0 or i == total:
                elapsed = time.time() - t_start
                rate = i / elapsed
                eta = (total - i) / rate if rate > 0 else 0
                log.info(
                    "[%d/%d] filled=%d  skipped=%d  eta=%.0fs",
                    i, total, done, skipped, eta,
                )

            await asyncio.sleep(REQ_INTERVAL)

    log.info("Done. filled=%d  skipped=%d  total=%d", done, skipped, total)
    conn.close()


if __name__ == "__main__":
    asyncio.run(run())
