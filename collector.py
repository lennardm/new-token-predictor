"""
collector.py — WebSocket listener for pump.fun token launches and trades.

Connects to pumpportal.fun, subscribes to new-token events, collects trades
for each token for 20 minutes, and applies a 60-second viability filter.
Launches enricher and price_fetcher as concurrent asyncio tasks.
"""

import asyncio
import json
import logging
import time
import signal

import websockets
from dotenv import load_dotenv

load_dotenv()

import db
import enricher as enricher_mod
import price_fetcher as price_fetcher_mod
from config import (
    COLLECTION_WINDOW_SEC,
    DB_PATH,
    MIN_BUYS,
    MIN_UNIQUE_BUYERS,
    PUMPPORTAL_WS,
    VIABILITY_WINDOW_SEC,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("collector")

# Mints currently being actively subscribed to trade events
_active_subscriptions: set[str] = set()


def _extract_token(event: dict) -> dict:
    return {
        "mint": event.get("mint"),
        "name": event.get("name"),
        "symbol": event.get("symbol"),
        "creator": event.get("traderPublicKey") or event.get("creator"),
        "bonding_curve": event.get("bondingCurveKey"),
        "created_at": event.get("timestamp") or time.time(),
        "metadata_uri": event.get("uri") or event.get("metadataUri"),
        "twitter_url": event.get("twitter"),
        "telegram_url": event.get("telegram"),
        "website_url": event.get("website"),
        "description": event.get("description"),
        "status": "watching",
    }


def _extract_trade(event: dict) -> dict | None:
    sol = event.get("solAmount") or 0.0
    tok = event.get("tokenAmount") or 0.0
    price_sol = (sol / tok) if tok else None
    sig = event.get("signature")
    if not sig:
        return None
    return {
        "token_mint": event.get("mint"),
        "ts": event.get("timestamp") or time.time(),
        "tx_type": "buy" if event.get("txType") == "buy" else "sell",
        "sol_amount": sol,
        "token_amount": tok,
        "price_sol": price_sol,
        "wallet": event.get("traderPublicKey"),
        "signature": sig,
    }


async def _subscribe_token(ws, mint: str) -> None:
    if mint in _active_subscriptions:
        return
    _active_subscriptions.add(mint)
    await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": [mint]}))
    log.debug("Subscribed to trades for %s", mint)


async def _unsubscribe_token(ws, mint: str) -> None:
    _active_subscriptions.discard(mint)
    try:
        await ws.send(json.dumps({"method": "unsubscribeTokenTrade", "keys": [mint]}))
        log.debug("Unsubscribed from trades for %s", mint)
    except websockets.ConnectionClosed:
        log.debug("Skipping unsubscribe for %s — connection already closed", mint)


async def _viability_check(
    conn: db.sqlite3.Connection, ws, mint: str, sleep_sec: float | None = None
) -> None:
    """Run 60-second viability check; kill or promote the token."""
    await asyncio.sleep(VIABILITY_WINDOW_SEC if sleep_sec is None else sleep_sec)
    stats = db.get_trade_stats(conn, mint, VIABILITY_WINDOW_SEC)
    buys = stats["buy_count"]
    buyers = stats["unique_buyers"]
    if buys < MIN_BUYS or buyers < MIN_UNIQUE_BUYERS:
        log.info(
            "DEAD  %s  (buys=%d unique_buyers=%d in first %ds)",
            mint, buys, buyers, VIABILITY_WINDOW_SEC,
        )
        db.set_token_status(conn, mint, "dead")
        await _unsubscribe_token(ws, mint)
    else:
        log.info(
            "TRACK %s  (buys=%d unique_buyers=%d)", mint, buys, buyers
        )
        db.set_token_status(conn, mint, "tracking")
        # Schedule unsubscribe at T+20min
        asyncio.create_task(_finish_collection(conn, ws, mint))


async def _finish_collection(
    conn: db.sqlite3.Connection, ws, mint: str
) -> None:
    """Unsubscribe from token trades after full collection window."""
    row = conn.execute(
        "SELECT created_at FROM tokens WHERE mint = ?", (mint,)
    ).fetchone()
    if not row:
        return
    elapsed = time.time() - row["created_at"]
    remaining = COLLECTION_WINDOW_SEC - elapsed
    if remaining > 0:
        await asyncio.sleep(remaining)
    db.set_token_status(conn, mint, "done")
    await _unsubscribe_token(ws, mint)
    log.info("DONE  %s  (collection window closed)", mint)


async def _recover_active_tokens(conn: db.sqlite3.Connection, ws) -> None:
    """On startup/reconnect, re-subscribe and re-schedule tasks for in-progress tokens."""
    now = time.time()
    n_watching = n_tracking = 0

    for row in conn.execute(
        "SELECT mint, created_at FROM tokens WHERE status = 'watching' ORDER BY created_at"
    ).fetchall():
        mint, created_at = row["mint"], row["created_at"]
        elapsed = now - created_at
        if elapsed < VIABILITY_WINDOW_SEC:
            # Still inside viability window — wait out the remainder
            await _subscribe_token(ws, mint)
            asyncio.create_task(
                _viability_check(conn, ws, mint, sleep_sec=VIABILITY_WINDOW_SEC - elapsed)
            )
        else:
            # Past window — decide immediately from stored trades
            stats = db.get_trade_stats(conn, mint, VIABILITY_WINDOW_SEC)
            if stats["buy_count"] < MIN_BUYS or stats["unique_buyers"] < MIN_UNIQUE_BUYERS:
                db.set_token_status(conn, mint, "dead")
                log.info("DEAD  %s  (recovery verdict)", mint)
            elif elapsed < COLLECTION_WINDOW_SEC:
                db.set_token_status(conn, mint, "tracking")
                await _subscribe_token(ws, mint)
                asyncio.create_task(_finish_collection(conn, ws, mint))
                log.info("TRACK %s  (recovery verdict)", mint)
            else:
                db.set_token_status(conn, mint, "done")
                log.info("DONE  %s  (recovery: window elapsed)", mint)
        n_watching += 1

    for row in conn.execute(
        "SELECT mint, created_at FROM tokens WHERE status = 'tracking' ORDER BY created_at"
    ).fetchall():
        mint, created_at = row["mint"], row["created_at"]
        elapsed = now - created_at
        if elapsed < COLLECTION_WINDOW_SEC:
            await _subscribe_token(ws, mint)
            asyncio.create_task(_finish_collection(conn, ws, mint))
            log.info(
                "RESUME %s  (%.0fs remaining)", mint, COLLECTION_WINDOW_SEC - elapsed
            )
        else:
            db.set_token_status(conn, mint, "done")
            log.info("DONE  %s  (recovery: window elapsed)", mint)
        n_tracking += 1

    if n_watching or n_tracking:
        log.info(
            "Recovery complete: %d watching resolved, %d tracking resumed",
            n_watching, n_tracking,
        )


async def _listen(conn: db.sqlite3.Connection) -> None:
    reconnect_delay = 2
    while True:
        try:
            log.info("Connecting to %s", PUMPPORTAL_WS)
            async with websockets.connect(
                PUMPPORTAL_WS,
                ping_interval=20,
                ping_timeout=30,
                max_size=2**20,
            ) as ws:
                reconnect_delay = 2
                _active_subscriptions.clear()
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                await ws.send(json.dumps({"method": "subscribeNewMigration"}))
                log.info("Subscribed to new-token and migration events")
                await _recover_active_tokens(conn, ws)

                async for raw in ws:
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    method = event.get("txType") or event.get("method") or ""
                    mint = event.get("mint")

                    if not mint:
                        continue

                    # Migration event — token graduated from bonding curve
                    if event.get("txType") == "migrate":
                        migrated_at = event.get("timestamp") or time.time()
                        db.set_token_migrated(conn, mint, migrated_at)
                        if mint in _active_subscriptions:
                            await _unsubscribe_token(ws, mint)
                            elapsed = migrated_at - (conn.execute(
                                "SELECT created_at FROM tokens WHERE mint = ?", (mint,)
                            ).fetchone() or {}).get("created_at", migrated_at)
                            log.info(
                                "MIGRATED  %s  (%.0fs after creation)",
                                mint, elapsed,
                            )
                        else:
                            log.info("MIGRATED  %s  (not actively tracked)", mint)
                        continue

                    # New token creation event — txType is "create" (API now always sends it)
                    if event.get("txType") == "create":
                        token_data = _extract_token(event)
                        db.insert_token(conn, token_data)
                        log.info(
                            "NEW   %s  (%s / %s)",
                            mint,
                            token_data["name"],
                            token_data["symbol"],
                        )
                        await _subscribe_token(ws, mint)
                        asyncio.create_task(_viability_check(conn, ws, mint))

                    # Trade event
                    elif method in ("buy", "sell"):
                        trade = _extract_trade(event)
                        if trade:
                            db.insert_trade(conn, trade)

        except (websockets.ConnectionClosed, OSError) as exc:
            log.warning("WebSocket disconnected: %s — retrying in %ds", exc, reconnect_delay)
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)
        except Exception:
            log.exception("Unexpected error in WebSocket loop")
            await asyncio.sleep(reconnect_delay)


async def main() -> None:
    conn = db.init_db(DB_PATH)
    log.info("Database initialised at %s", DB_PATH)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _shutdown():
        log.info("Shutdown signal received")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    tasks = [
        asyncio.create_task(_listen(conn)),
        asyncio.create_task(enricher_mod.run(conn)),
        asyncio.create_task(price_fetcher_mod.run(conn)),
    ]

    await stop.wait()

    log.info("Shutting down...")
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    log.info("Shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
