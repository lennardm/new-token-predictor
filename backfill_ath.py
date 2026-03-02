"""
backfill_ath.py — Backfill ATH + bonding data for existing tokens.

For each done/tracking token without ATH data:
  1. Call DexScreener to check if the token has bonded (dexId != pumpfun).
  2. If bonded: call ST /tokens/{mint}/ath, update price_snapshots, set bonded_at.
  3. If bonded and no ST risk data: call ST /tokens/{mint} for risk fields.

Safe to run alongside collector.py (WAL mode).
Safe to interrupt and re-run — only NULL rows are touched.
"""

import asyncio
import logging
import time

import aiohttp
from dotenv import load_dotenv

load_dotenv()

import db
from config import (
    DB_PATH,
    DEXSCREENER_BASE,
    SOLANATRACKER_BASE,
    SOLANATRACKER_KEY,
)
from price_fetcher import (
    _BONDED_DEX_IDS,
    _fetch_dexscreener,
    _fetch_solanatracker_ath,
    _fetch_solanatracker_risk,
    _float,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("backfill_ath")

DS_BATCH = 30       # DexScreener batch size
ST_INTERVAL = 2.0   # seconds between SolanaTracker calls
DS_INTERVAL = 1.1   # seconds between DexScreener batches


async def run() -> None:
    if not SOLANATRACKER_KEY:
        log.error("SOLANATRACKER_KEY not set — nothing to do")
        return

    conn = db.init_db(DB_PATH)

    # Tokens missing ATH data across all their snapshots
    mints = [
        row[0] for row in conn.execute("""
            SELECT DISTINCT t.mint
            FROM tokens t
            JOIN price_snapshots ps ON ps.token_mint = t.mint
            WHERE ps.ath_market_cap_usd IS NULL
              AND t.status IN ('done', 'tracking')
            ORDER BY t.created_at
        """).fetchall()
    ]

    total = len(mints)
    log.info("Tokens to check: %d", total)

    bonded = skipped = ath_filled = risk_filled = 0
    t_start = time.time()

    async with aiohttp.ClientSession() as session:
        # Process in DexScreener batches to detect bonding cheaply
        for batch_start in range(0, total, DS_BATCH):
            batch = mints[batch_start: batch_start + DS_BATCH]
            ds = await _fetch_dexscreener(session, batch)

            for mint in batch:
                pair = ds.get(mint)
                dex_id = (pair.get("dexId") or "").lower() if pair else ""
                is_bonded = dex_id in _BONDED_DEX_IDS

                if not is_bonded:
                    skipped += 1
                    continue

                bonded += 1
                now = time.time()

                # Record bonded_at if not set
                row = conn.execute(
                    "SELECT bonded_at, created_at FROM tokens WHERE mint = ?", (mint,)
                ).fetchone()
                if row and row["bonded_at"] is None:
                    # Approximate: use fetched_at of earliest snapshot
                    earliest = conn.execute(
                        "SELECT MIN(fetched_at) FROM price_snapshots WHERE token_mint = ?",
                        (mint,),
                    ).fetchone()
                    bonded_at = earliest[0] if earliest and earliest[0] else now
                    db.set_token_bonded(conn, mint, bonded_at)

                # Fetch ATH
                await asyncio.sleep(ST_INTERVAL)
                ath = await _fetch_solanatracker_ath(session, mint)
                if ath and ath.get("ath_market_cap_usd"):
                    conn.execute(
                        """
                        UPDATE price_snapshots
                        SET ath_price_usd = ?,
                            ath_market_cap_usd = ?,
                            ath_timestamp = ?
                        WHERE token_mint = ? AND ath_market_cap_usd IS NULL
                        """,
                        (
                            ath["ath_price_usd"],
                            ath["ath_market_cap_usd"],
                            ath["ath_timestamp"],
                            mint,
                        ),
                    )
                    conn.commit()
                    ath_filled += 1
                    log.info(
                        "ATH  %s  ath_mc=$%.0f  bonded_at~%s",
                        mint, ath["ath_market_cap_usd"] or 0,
                        "yes",
                    )

                # Fetch ST risk data if missing
                has_st = conn.execute(
                    "SELECT st_score FROM token_risk WHERE token_mint = ? AND st_score IS NOT NULL",
                    (mint,),
                ).fetchone()
                if not has_st:
                    await asyncio.sleep(ST_INTERVAL)
                    risk = await _fetch_solanatracker_risk(session, mint)
                    if risk:
                        db.upsert_token_risk_st(conn, {
                            "token_mint": mint,
                            "fetched_at": now,
                            **risk,
                        })
                        risk_filled += 1

            await asyncio.sleep(DS_INTERVAL)

            done_so_far = batch_start + len(batch)
            elapsed = time.time() - t_start
            rate = done_so_far / elapsed
            eta = (total - done_so_far) / rate if rate > 0 else 0
            log.info(
                "[%d/%d] bonded=%d  ath_filled=%d  risk_filled=%d  skipped=%d  eta=%.0fs",
                done_so_far, total, bonded, ath_filled, risk_filled, skipped, eta,
            )

    log.info(
        "Done. bonded=%d  ath_filled=%d  risk_filled=%d  skipped=%d",
        bonded, ath_filled, risk_filled, skipped,
    )
    conn.close()


if __name__ == "__main__":
    asyncio.run(run())
