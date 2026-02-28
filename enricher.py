"""
enricher.py — Background loop that fetches RugCheck risk data and CryptoPanic
social mentions for newly collected tokens.
"""

import asyncio
import json
import logging
import sqlite3
import time

import aiohttp

import db
from config import (
    CRYPTOPANIC_BASE,
    CRYPTOPANIC_MIN_BUY_SOL,
    CRYPTOPANIC_TOKEN,
    GOPLUS_BASE,
    RUGCHECK_BASE,
    SOLANATRACKER_BASE,
    SOLANATRACKER_KEY,
)

log = logging.getLogger("enricher")

POLL_INTERVAL_SEC = 30
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


async def _fetch_rugcheck(session: aiohttp.ClientSession, mint: str) -> dict | None:
    url = f"{RUGCHECK_BASE}/tokens/{mint}/report"
    try:
        async with session.get(url, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
    except Exception as exc:
        log.debug("RugCheck request failed for %s: %s", mint, exc)
        return None

    score = data.get("score", 0)
    level = data.get("scoreLabel", "").lower()  # "good" / "warn" / "danger"
    risks_raw = data.get("risks", [])
    risks = [r.get("name", str(r)) for r in risks_raw] if isinstance(risks_raw, list) else []

    # Mint / freeze authority
    mint_auth = 0
    freeze_auth = 0
    for field in data.get("tokenMeta", {}).get("extensions", []):
        pass
    mint_revoked = data.get("mintAuthorityRevoked")
    freeze_revoked = data.get("freezeAuthorityRevoked")
    if mint_revoked is not None:
        mint_auth = 1 if mint_revoked else 0
    if freeze_revoked is not None:
        freeze_auth = 1 if freeze_revoked else 0

    # Top holder percentage
    top_holder_pct: float | None = None
    holders = data.get("topHolders", [])
    if holders:
        pcts = [h.get("pct", 0) for h in holders if isinstance(h, dict)]
        top_holder_pct = max(pcts) if pcts else None

    # LP locked
    lp_locked = 1 if data.get("markets") and any(
        m.get("lp", {}).get("lpLockedPct", 0) > 0
        for m in data.get("markets", [])
        if isinstance(m, dict)
    ) else 0

    return {
        "rugcheck_score": score,
        "rugcheck_level": level,
        "risks": json.dumps(risks),
        "mint_authority_revoked": mint_auth,
        "freeze_authority_revoked": freeze_auth,
        "top_holder_pct": top_holder_pct,
        "lp_locked": lp_locked,
        "source": "rugcheck",
    }


async def _fetch_goplus(session: aiohttp.ClientSession, mint: str) -> dict | None:
    url = f"{GOPLUS_BASE}/solana/token_security?contract_addresses={mint}"
    try:
        async with session.get(url, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
    except Exception as exc:
        log.debug("GoPlus request failed for %s: %s", mint, exc)
        return None

    result = (data.get("result") or {}).get(mint, {})
    if not result:
        return None

    risks = []
    if result.get("is_honeypot") == "1":
        risks.append("honeypot")
    if result.get("is_blacklisted") == "1":
        risks.append("blacklisted")
    if result.get("is_mintable") == "1":
        risks.append("mintable")

    top_holder_pct: float | None = None
    holders = result.get("holders", [])
    if holders:
        pcts = [float(h.get("percent", 0)) * 100 for h in holders if isinstance(h, dict)]
        top_holder_pct = max(pcts) if pcts else None

    return {
        "rugcheck_score": None,
        "rugcheck_level": None,
        "risks": json.dumps(risks),
        "mint_authority_revoked": 1 if result.get("mint_address") == "0" else 0,
        "freeze_authority_revoked": 1 if result.get("freeze_address") == "0" else 0,
        "top_holder_pct": top_holder_pct,
        "lp_locked": 0,
        "source": "goplus",
    }


async def _fetch_cryptopanic(
    session: aiohttp.ClientSession, symbol: str
) -> tuple[int, list[str]]:
    if not CRYPTOPANIC_TOKEN:
        return 0, []
    url = (
        f"{CRYPTOPANIC_BASE}/posts/"
        f"?auth_token={CRYPTOPANIC_TOKEN}"
        f"&currencies={symbol}"
        f"&kind=news,media"
    )
    try:
        async with session.get(url, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                return 0, []
            data = await resp.json(content_type=None)
    except Exception as exc:
        log.debug("CryptoPanic request failed for %s: %s", symbol, exc)
        return 0, []

    results = data.get("results", [])
    titles = [r.get("title", "") for r in results if isinstance(r, dict)]
    return len(titles), titles


async def _enrich_token(
    session: aiohttp.ClientSession,
    conn: sqlite3.Connection,
    mint: str,
    symbol: str,
) -> None:
    # 1. RugCheck
    risk_data = await _fetch_rugcheck(session, mint)
    if risk_data is None:
        log.info("RugCheck miss for %s — trying GoPlus", mint)
        risk_data = await _fetch_goplus(session, mint)

    if risk_data is not None:
        db.upsert_token_risk(conn, {
            "token_mint": mint,
            "fetched_at": time.time(),
            **risk_data,
        })
        log.info(
            "Risk  %s  score=%s level=%s source=%s",
            mint,
            risk_data["rugcheck_score"],
            risk_data["rugcheck_level"],
            risk_data["source"],
        )
    else:
        # Insert a sentinel so we don't keep retrying
        db.upsert_token_risk(conn, {
            "token_mint": mint,
            "fetched_at": time.time(),
            "rugcheck_score": None,
            "rugcheck_level": None,
            "risks": "[]",
            "mint_authority_revoked": None,
            "freeze_authority_revoked": None,
            "top_holder_pct": None,
            "lp_locked": None,
            "source": "none",
        })
        log.warning("Risk data unavailable for %s", mint)

    # 2. CryptoPanic initial mentions — only for high-volume tokens
    if symbol:
        buy_vol = db.get_buy_volume_sol(conn, mint)
        if buy_vol >= CRYPTOPANIC_MIN_BUY_SOL:
            count, titles = await _fetch_cryptopanic(session, symbol)
            if count > 0:
                db.insert_social_mention(conn, {
                    "token_mint": mint,
                    "fetched_at": time.time(),
                    "delay_minutes": 0,
                    "source": "cryptopanic",
                    "mention_count": count,
                    "titles": json.dumps(titles),
                })
                log.info("Social %s  mentions=%d", mint, count)
        else:
            log.debug("Skipping CryptoPanic for %s — buy vol %.2f SOL < %.2f", mint, buy_vol, CRYPTOPANIC_MIN_BUY_SOL)


async def run(conn: sqlite3.Connection) -> None:
    """Background loop — poll every POLL_INTERVAL_SEC seconds."""
    log.info("Enricher started")
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                pending = db.get_tokens_needing_enrichment(conn)
                if pending:
                    log.info("Enriching %d token(s)", len(pending))
                    for row in pending:
                        await _enrich_token(session, conn, row["mint"], row["symbol"] or "")
                        await asyncio.sleep(0.5)  # gentle rate limiting
            except Exception:
                log.exception("Enricher iteration error")
            await asyncio.sleep(POLL_INTERVAL_SEC)
