"""
price_fetcher.py — Background loop that fetches DexScreener / SolanaTracker
price snapshots at scheduled delays after each token's creation.
"""

import asyncio
import json
import logging
import sqlite3
import time

import aiohttp

import db
import enricher as enricher_mod
from config import (
    COINGECKO_BASE,
    CRYPTOPANIC_BASE,
    CRYPTOPANIC_MIN_BUY_SOL,
    CRYPTOPANIC_TOKEN,
    DEXSCREENER_BASE,
    DEXSCREENER_RPM,
    SNAPSHOT_DELAYS_MIN,
    SOLANATRACKER_BASE,
    SOLANATRACKER_KEY,
)

log = logging.getLogger("price_fetcher")

POLL_INTERVAL_SEC = 300  # run every 5 minutes
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)

# Minimum gap between DexScreener requests to stay under RPM cap
_DS_MIN_INTERVAL = 60 / DEXSCREENER_RPM  # ~1.09 s


async def _fetch_dexscreener(
    session: aiohttp.ClientSession, mints: list[str]
) -> dict[str, dict]:
    """
    Batch query DexScreener for up to 30 mints.
    Returns a dict keyed by mint address.
    """
    joined = ",".join(mints)
    url = f"{DEXSCREENER_BASE}/tokens/v1/solana/{joined}"
    try:
        async with session.get(url, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                log.debug("DexScreener %d for %d mints", resp.status, len(mints))
                return {}
            data = await resp.json(content_type=None)
    except Exception as exc:
        log.debug("DexScreener request failed: %s", exc)
        return {}

    results: dict[str, dict] = {}
    pairs = data if isinstance(data, list) else data.get("pairs", [])
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        base = pair.get("baseToken", {})
        mint = base.get("address")
        if not mint:
            continue
        # Keep the pair with highest liquidity if multiple
        if mint not in results or (
            (pair.get("liquidity") or {}).get("usd", 0)
            > (results[mint].get("liquidity") or {}).get("usd", 0)
        ):
            results[mint] = pair
    return results


def _parse_dexscreener_pair(pair: dict) -> dict:
    liquidity = pair.get("liquidity") or {}
    return {
        "price_usd": _float(pair.get("priceUsd")),
        "market_cap_usd": _float(pair.get("marketCap") or pair.get("fdv")),
        "liquidity_usd": _float(liquidity.get("usd")),
        "volume_24h": _float((pair.get("volume") or {}).get("h24")),
        "ath_price_usd": None,  # DexScreener doesn't expose ATH directly
        "ath_market_cap_usd": None,
        "pair_address": pair.get("pairAddress"),
        "source": "dexscreener",
    }


async def _fetch_solanatracker(
    session: aiohttp.ClientSession, mint: str
) -> dict | None:
    if not SOLANATRACKER_KEY:
        return None
    url = f"{SOLANATRACKER_BASE}/tokens/{mint}"
    headers = {"x-api-key": SOLANATRACKER_KEY}
    try:
        async with session.get(url, headers=headers, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
    except Exception as exc:
        log.debug("SolanaTracker request failed for %s: %s", mint, exc)
        return None

    # SolanaTracker response shape (best-effort parsing)
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
        # Try flat structure
        best = data

    price_usd = _float(
        (best.get("price") or {}).get("usd")
        or best.get("priceUsd")
    )
    market_cap = _float(
        best.get("marketCap")
        or (best.get("market_cap"))
    )
    liquidity = _float(
        (best.get("liquidity") or {}).get("usd")
        or best.get("liquidity")
    )
    ath = _float(data.get("ath") or data.get("athPrice"))

    return {
        "price_usd": price_usd,
        "market_cap_usd": market_cap,
        "liquidity_usd": liquidity,
        "volume_24h": _float(
            (best.get("volume") or {}).get("h24") or best.get("volume24h")
        ),
        "ath_price_usd": ath,
        "ath_market_cap_usd": None,
        "pair_address": best.get("poolId") or best.get("pairAddress"),
        "source": "solanatracker",
    }


async def _fetch_coingecko(session: aiohttp.ClientSession) -> dict | None:
    url = (
        f"{COINGECKO_BASE}/simple/price"
        "?ids=solana,bitcoin"
        "&vs_currencies=usd"
        "&include_24hr_change=true"
        "&include_24hr_vol=true"
    )
    try:
        async with session.get(url, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                log.debug("CoinGecko %d", resp.status)
                return None
            data = await resp.json(content_type=None)
    except Exception as exc:
        log.debug("CoinGecko request failed: %s", exc)
        return None

    sol = data.get("solana", {})
    btc = data.get("bitcoin", {})
    return {
        "ts":                 time.time(),
        "sol_price_usd":      _float(sol.get("usd")),
        "sol_change_24h_pct": _float(sol.get("usd_24h_change")),
        "sol_volume_24h_usd": _float(sol.get("usd_24h_vol")),
        "btc_price_usd":      _float(btc.get("usd")),
        "btc_change_24h_pct": _float(btc.get("usd_24h_change")),
    }


def _compute_launch_rate(conn: sqlite3.Connection, window_sec: int = 3600) -> float:
    cutoff = time.time() - window_sec
    row = conn.execute(
        "SELECT COUNT(*) FROM tokens WHERE created_at >= ?", (cutoff,)
    ).fetchone()
    return float(row[0]) if row else 0.0


def _float(val) -> float | None:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


async def _fetch_and_store_snapshot(
    session: aiohttp.ClientSession,
    conn: sqlite3.Connection,
    mints: list[str],
    delay_minutes: int,
) -> None:
    # DexScreener batch (up to 30)
    ds_results = await _fetch_dexscreener(session, mints)
    now = time.time()

    for mint in mints:
        if mint in ds_results:
            parsed = _parse_dexscreener_pair(ds_results[mint])
        else:
            # Fallback to SolanaTracker
            parsed = await _fetch_solanatracker(session, mint)
            await asyncio.sleep(_DS_MIN_INTERVAL)

        if parsed is None:
            log.debug("No price data for %s at delay=%d", mint, delay_minutes)
            # Store null snapshot so we don't retry endlessly
            parsed = {
                "price_usd": None,
                "market_cap_usd": None,
                "liquidity_usd": None,
                "volume_24h": None,
                "ath_price_usd": None,
                "ath_market_cap_usd": None,
                "pair_address": None,
                "source": "none",
            }

        # Compute running ATH market cap from existing snapshots
        prev_ath_mc = conn.execute(
            """
            SELECT MAX(COALESCE(ath_market_cap_usd, market_cap_usd))
            FROM price_snapshots
            WHERE token_mint = ?
            """,
            (mint,),
        ).fetchone()[0]
        current_mc = parsed.get("market_cap_usd")
        if current_mc and (prev_ath_mc is None or current_mc > prev_ath_mc):
            parsed["ath_market_cap_usd"] = current_mc
        else:
            parsed["ath_market_cap_usd"] = prev_ath_mc

        db.insert_snapshot(conn, {
            "token_mint": mint,
            "fetched_at": now,
            "delay_minutes": delay_minutes,
            **parsed,
        })
        log.info(
            "Snap  %s  delay=%d  price=%s  mc=%s  source=%s",
            mint,
            delay_minutes,
            parsed["price_usd"],
            parsed["market_cap_usd"],
            parsed["source"],
        )


async def _refresh_cryptopanic_24h(
    session: aiohttp.ClientSession, conn: sqlite3.Connection, mint: str
) -> None:
    """Re-fetch CryptoPanic at 24h — only for tokens that met the volume threshold at launch."""
    buy_vol = db.get_buy_volume_sol(conn, mint)
    if buy_vol < CRYPTOPANIC_MIN_BUY_SOL:
        return
    symbol_row = conn.execute(
        "SELECT symbol FROM tokens WHERE mint = ?", (mint,)
    ).fetchone()
    if not symbol_row or not symbol_row["symbol"]:
        return
    count, titles = await enricher_mod._fetch_cryptopanic(session, symbol_row["symbol"])
    db.insert_social_mention(conn, {
        "token_mint": mint,
        "fetched_at": time.time(),
        "delay_minutes": 1440,
        "source": "cryptopanic",
        "mention_count": count,
        "titles": json.dumps(titles),
    })


async def run(conn: sqlite3.Connection) -> None:
    """Background loop — check for due snapshots every POLL_INTERVAL_SEC seconds."""
    log.info("Price fetcher started")
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # --- macro market snapshot ---
                market_data = await _fetch_coingecko(session)
                if market_data is not None:
                    launch_rate = _compute_launch_rate(conn)
                    db.insert_market_snapshot(conn, market_data)
                    log.info(
                        "Market  SOL=%.2f (%.1f%%)  BTC=%.0f (%.1f%%)  launch_rate=%.1f/h",
                        market_data["sol_price_usd"] or 0,
                        market_data["sol_change_24h_pct"] or 0,
                        market_data["btc_price_usd"] or 0,
                        market_data["btc_change_24h_pct"] or 0,
                        launch_rate,
                    )

                for delay in SNAPSHOT_DELAYS_MIN:
                    rows = db.get_tokens_needing_snapshot(conn, delay)
                    if not rows:
                        continue
                    mints = [r["mint"] for r in rows]
                    log.info(
                        "Snapshot delay=%d  count=%d", delay, len(mints)
                    )
                    await _fetch_and_store_snapshot(session, conn, mints, delay)
                    await asyncio.sleep(_DS_MIN_INTERVAL)

                    # 24h: also refresh CryptoPanic
                    if delay == 1440:
                        for mint in mints:
                            await _refresh_cryptopanic_24h(session, conn, mint)
                            await asyncio.sleep(0.5)

            except Exception:
                log.exception("Price fetcher iteration error")
            await asyncio.sleep(POLL_INTERVAL_SEC)
