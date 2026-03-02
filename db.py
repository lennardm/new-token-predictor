import os
import sqlite3
import time
from config import DB_PATH, ENRICHMENT_DELAY_SEC, SNAPSHOT_DELAYS_MIN


def init_db(path: str = DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _create_tables(conn)
    return conn


def _create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tokens (
            mint                TEXT PRIMARY KEY,
            name                TEXT,
            symbol              TEXT,
            creator             TEXT,
            bonding_curve       TEXT,
            created_at          REAL,
            metadata_uri        TEXT,
            twitter_url         TEXT,
            telegram_url        TEXT,
            website_url         TEXT,
            description         TEXT,
            status              TEXT DEFAULT 'watching',
            bonded_at           REAL,
            migrated_at         REAL
        );

        CREATE TABLE IF NOT EXISTS trades (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            token_mint   TEXT NOT NULL REFERENCES tokens(mint),
            ts           REAL,
            tx_type      TEXT,
            sol_amount   REAL,
            token_amount REAL,
            price_sol    REAL,
            wallet       TEXT,
            signature    TEXT UNIQUE
        );

        CREATE INDEX IF NOT EXISTS idx_trades_mint_ts ON trades(token_mint, ts);

        CREATE TABLE IF NOT EXISTS token_risk (
            token_mint                    TEXT PRIMARY KEY REFERENCES tokens(mint),
            fetched_at                    REAL,
            rugcheck_score                INTEGER,
            rugcheck_level                TEXT,
            risks                         TEXT,
            mint_authority_revoked        INTEGER,
            freeze_authority_revoked      INTEGER,
            top_holder_pct                REAL,
            lp_locked                     INTEGER,
            source                        TEXT,
            st_score                      INTEGER,
            st_rugged                     INTEGER,
            st_jupiter_verified           INTEGER,
            st_top10_pct                  REAL,
            st_snipers_count              INTEGER,
            st_snipers_pct                REAL,
            st_snipers_balance            REAL,
            st_bundlers_count             INTEGER,
            st_bundlers_pct               REAL,
            st_bundlers_balance           REAL,
            st_bundlers_initial_pct       REAL,
            st_bundlers_initial_balance   REAL,
            st_insiders_count             INTEGER,
            st_insiders_pct               REAL,
            st_insiders_balance           REAL,
            st_dev_pct                    REAL,
            st_dev_amount                 REAL,
            st_curve_pct                  REAL,
            st_holders                    INTEGER,
            st_sniper_wallets             TEXT,
            st_bundler_wallets            TEXT,
            st_insider_wallets            TEXT
        );

        CREATE TABLE IF NOT EXISTS social_mentions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            token_mint    TEXT NOT NULL REFERENCES tokens(mint),
            fetched_at    REAL,
            delay_minutes INTEGER,
            source        TEXT,
            mention_count INTEGER,
            titles        TEXT
        );

        CREATE TABLE IF NOT EXISTS price_snapshots (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            token_mint           TEXT NOT NULL REFERENCES tokens(mint),
            fetched_at           REAL,
            delay_minutes        INTEGER,
            price_usd            REAL,
            market_cap_usd       REAL,
            liquidity_usd        REAL,
            volume_1h            REAL,
            volume_6h            REAL,
            volume_24h           REAL,
            price_change_1h_pct  REAL,
            price_change_6h_pct  REAL,
            price_change_24h_pct REAL,
            buys_1h              INTEGER,
            sells_1h             INTEGER,
            buys_24h             INTEGER,
            sells_24h            INTEGER,
            ath_price_usd        REAL,
            ath_market_cap_usd   REAL,
            ath_timestamp        REAL,
            pair_address         TEXT,
            source               TEXT,
            UNIQUE(token_mint, delay_minutes)
        );

        CREATE TABLE IF NOT EXISTS market_snapshots (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            ts                  REAL NOT NULL,
            sol_price_usd       REAL,
            sol_change_24h_pct  REAL,
            sol_volume_24h_usd  REAL,
            btc_price_usd       REAL,
            btc_change_24h_pct  REAL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_market_snapshots_bucket
            ON market_snapshots(CAST(ts / 300 AS INTEGER));
    """)
    conn.commit()
    _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after initial schema without dropping existing data."""
    # price_snapshots
    ps_cols = {row[1] for row in conn.execute("PRAGMA table_info(price_snapshots)")}
    for col, typ in [
        ("volume_1h",            "REAL"),
        ("volume_6h",            "REAL"),
        ("price_change_1h_pct",  "REAL"),
        ("price_change_6h_pct",  "REAL"),
        ("price_change_24h_pct", "REAL"),
        ("buys_1h",              "INTEGER"),
        ("sells_1h",             "INTEGER"),
        ("buys_24h",             "INTEGER"),
        ("sells_24h",            "INTEGER"),
        ("ath_timestamp",        "REAL"),
    ]:
        if col not in ps_cols:
            conn.execute(f"ALTER TABLE price_snapshots ADD COLUMN {col} {typ}")

    # tokens
    t_cols = {row[1] for row in conn.execute("PRAGMA table_info(tokens)")}
    if "bonded_at" not in t_cols:
        conn.execute("ALTER TABLE tokens ADD COLUMN bonded_at REAL")
    if "migrated_at" not in t_cols:
        conn.execute("ALTER TABLE tokens ADD COLUMN migrated_at REAL")

    # token_risk
    tr_cols = {row[1] for row in conn.execute("PRAGMA table_info(token_risk)")}
    for col, typ in [
        ("st_score",                    "INTEGER"),
        ("st_rugged",                   "INTEGER"),
        ("st_jupiter_verified",         "INTEGER"),
        ("st_top10_pct",                "REAL"),
        ("st_snipers_count",            "INTEGER"),
        ("st_snipers_pct",              "REAL"),
        ("st_snipers_balance",          "REAL"),
        ("st_bundlers_count",           "INTEGER"),
        ("st_bundlers_pct",             "REAL"),
        ("st_bundlers_balance",         "REAL"),
        ("st_bundlers_initial_pct",     "REAL"),
        ("st_bundlers_initial_balance", "REAL"),
        ("st_insiders_count",           "INTEGER"),
        ("st_insiders_pct",             "REAL"),
        ("st_insiders_balance",         "REAL"),
        ("st_dev_pct",                  "REAL"),
        ("st_dev_amount",               "REAL"),
        ("st_curve_pct",                "REAL"),
        ("st_holders",                  "INTEGER"),
        ("st_sniper_wallets",           "TEXT"),
        ("st_bundler_wallets",          "TEXT"),
        ("st_insider_wallets",          "TEXT"),
    ]:
        if col not in tr_cols:
            conn.execute(f"ALTER TABLE token_risk ADD COLUMN {col} {typ}")

    conn.commit()


def insert_token(conn: sqlite3.Connection, data: dict) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO tokens
            (mint, name, symbol, creator, bonding_curve, created_at,
             metadata_uri, twitter_url, telegram_url, website_url, description, status)
        VALUES
            (:mint, :name, :symbol, :creator, :bonding_curve, :created_at,
             :metadata_uri, :twitter_url, :telegram_url, :website_url, :description, :status)
        """,
        data,
    )
    conn.commit()


def insert_trade(conn: sqlite3.Connection, data: dict) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO trades
            (token_mint, ts, tx_type, sol_amount, token_amount, price_sol, wallet, signature)
        VALUES
            (:token_mint, :ts, :tx_type, :sol_amount, :token_amount, :price_sol, :wallet, :signature)
        """,
        data,
    )
    conn.commit()


def upsert_token_risk(conn: sqlite3.Connection, data: dict) -> None:
    """Upsert RugCheck / GoPlus risk data — does not touch ST columns."""
    conn.execute(
        """
        INSERT INTO token_risk
            (token_mint, fetched_at, rugcheck_score, rugcheck_level, risks,
             mint_authority_revoked, freeze_authority_revoked, top_holder_pct, lp_locked, source)
        VALUES
            (:token_mint, :fetched_at, :rugcheck_score, :rugcheck_level, :risks,
             :mint_authority_revoked, :freeze_authority_revoked, :top_holder_pct, :lp_locked, :source)
        ON CONFLICT(token_mint) DO UPDATE SET
            fetched_at               = excluded.fetched_at,
            rugcheck_score           = excluded.rugcheck_score,
            rugcheck_level           = excluded.rugcheck_level,
            risks                    = excluded.risks,
            mint_authority_revoked   = excluded.mint_authority_revoked,
            freeze_authority_revoked = excluded.freeze_authority_revoked,
            top_holder_pct           = excluded.top_holder_pct,
            lp_locked                = excluded.lp_locked,
            source                   = excluded.source
        """,
        data,
    )
    conn.commit()


def upsert_token_risk_st(conn: sqlite3.Connection, data: dict) -> None:
    """Upsert SolanaTracker risk data — does not touch RugCheck columns."""
    conn.execute(
        """
        INSERT INTO token_risk
            (token_mint, fetched_at,
             st_score, st_rugged, st_jupiter_verified,
             st_top10_pct,
             st_snipers_count, st_snipers_pct, st_snipers_balance,
             st_bundlers_count, st_bundlers_pct, st_bundlers_balance,
             st_bundlers_initial_pct, st_bundlers_initial_balance,
             st_insiders_count, st_insiders_pct, st_insiders_balance,
             st_dev_pct, st_dev_amount,
             st_curve_pct, st_holders,
             st_sniper_wallets, st_bundler_wallets, st_insider_wallets)
        VALUES
            (:token_mint, :fetched_at,
             :st_score, :st_rugged, :st_jupiter_verified,
             :st_top10_pct,
             :st_snipers_count, :st_snipers_pct, :st_snipers_balance,
             :st_bundlers_count, :st_bundlers_pct, :st_bundlers_balance,
             :st_bundlers_initial_pct, :st_bundlers_initial_balance,
             :st_insiders_count, :st_insiders_pct, :st_insiders_balance,
             :st_dev_pct, :st_dev_amount,
             :st_curve_pct, :st_holders,
             :st_sniper_wallets, :st_bundler_wallets, :st_insider_wallets)
        ON CONFLICT(token_mint) DO UPDATE SET
            st_score                    = excluded.st_score,
            st_rugged                   = excluded.st_rugged,
            st_jupiter_verified         = excluded.st_jupiter_verified,
            st_top10_pct                = excluded.st_top10_pct,
            st_snipers_count            = excluded.st_snipers_count,
            st_snipers_pct              = excluded.st_snipers_pct,
            st_snipers_balance          = excluded.st_snipers_balance,
            st_bundlers_count           = excluded.st_bundlers_count,
            st_bundlers_pct             = excluded.st_bundlers_pct,
            st_bundlers_balance         = excluded.st_bundlers_balance,
            st_bundlers_initial_pct     = excluded.st_bundlers_initial_pct,
            st_bundlers_initial_balance = excluded.st_bundlers_initial_balance,
            st_insiders_count           = excluded.st_insiders_count,
            st_insiders_pct             = excluded.st_insiders_pct,
            st_insiders_balance         = excluded.st_insiders_balance,
            st_dev_pct                  = excluded.st_dev_pct,
            st_dev_amount               = excluded.st_dev_amount,
            st_curve_pct                = excluded.st_curve_pct,
            st_holders                  = excluded.st_holders,
            st_sniper_wallets           = excluded.st_sniper_wallets,
            st_bundler_wallets          = excluded.st_bundler_wallets,
            st_insider_wallets          = excluded.st_insider_wallets
        """,
        data,
    )
    conn.commit()


def set_token_bonded(conn: sqlite3.Connection, mint: str, bonded_at: float) -> None:
    conn.execute(
        "UPDATE tokens SET bonded_at = ? WHERE mint = ? AND bonded_at IS NULL",
        (bonded_at, mint),
    )
    conn.commit()


def set_token_migrated(conn: sqlite3.Connection, mint: str, migrated_at: float) -> None:
    conn.execute(
        "UPDATE tokens SET migrated_at = ? WHERE mint = ? AND migrated_at IS NULL",
        (migrated_at, mint),
    )
    conn.commit()


def insert_social_mention(conn: sqlite3.Connection, data: dict) -> None:
    conn.execute(
        """
        INSERT INTO social_mentions
            (token_mint, fetched_at, delay_minutes, source, mention_count, titles)
        VALUES
            (:token_mint, :fetched_at, :delay_minutes, :source, :mention_count, :titles)
        """,
        data,
    )
    conn.commit()


def insert_snapshot(conn: sqlite3.Connection, data: dict) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO price_snapshots
            (token_mint, fetched_at, delay_minutes, price_usd, market_cap_usd,
             liquidity_usd, volume_1h, volume_6h, volume_24h,
             price_change_1h_pct, price_change_6h_pct, price_change_24h_pct,
             buys_1h, sells_1h, buys_24h, sells_24h,
             ath_price_usd, ath_market_cap_usd, ath_timestamp, pair_address, source)
        VALUES
            (:token_mint, :fetched_at, :delay_minutes, :price_usd, :market_cap_usd,
             :liquidity_usd, :volume_1h, :volume_6h, :volume_24h,
             :price_change_1h_pct, :price_change_6h_pct, :price_change_24h_pct,
             :buys_1h, :sells_1h, :buys_24h, :sells_24h,
             :ath_price_usd, :ath_market_cap_usd, :ath_timestamp, :pair_address, :source)
        """,
        data,
    )
    conn.commit()


def insert_market_snapshot(conn: sqlite3.Connection, data: dict) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO market_snapshots
            (ts, sol_price_usd, sol_change_24h_pct, sol_volume_24h_usd,
             btc_price_usd, btc_change_24h_pct)
        VALUES
            (:ts, :sol_price_usd, :sol_change_24h_pct, :sol_volume_24h_usd,
             :btc_price_usd, :btc_change_24h_pct)
        """,
        data,
    )
    conn.commit()


def get_latest_market_snapshot_before(
    conn: sqlite3.Connection, ts: float
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM market_snapshots
        WHERE ts <= ?
        ORDER BY ts DESC
        LIMIT 1
        """,
        (ts,),
    ).fetchone()


def get_buy_volume_sol(conn: sqlite3.Connection, mint: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(sol_amount), 0) FROM trades WHERE token_mint = ? AND tx_type = 'buy'",
        (mint,),
    ).fetchone()
    return float(row[0]) if row else 0.0


def set_token_status(conn: sqlite3.Connection, mint: str, status: str) -> None:
    conn.execute("UPDATE tokens SET status = ? WHERE mint = ?", (status, mint))
    conn.commit()


def get_tokens_needing_enrichment(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    cutoff = time.time() - ENRICHMENT_DELAY_SEC
    return conn.execute(
        """
        SELECT t.mint, t.symbol
        FROM tokens t
        LEFT JOIN token_risk r ON r.token_mint = t.mint
        WHERE r.token_mint IS NULL
          AND t.created_at <= ?
          AND t.status != 'dead'
        ORDER BY t.created_at
        """,
        (cutoff,),
    ).fetchall()


def get_tokens_needing_snapshot(
    conn: sqlite3.Connection, delay_minutes: int
) -> list[sqlite3.Row]:
    delay_sec = delay_minutes * 60
    # Target window: created_at + delay_sec ± 5 min
    window = 300
    now = time.time()
    low = now - delay_sec - window
    high = now - delay_sec + window
    return conn.execute(
        """
        SELECT t.mint
        FROM tokens t
        LEFT JOIN price_snapshots ps
            ON ps.token_mint = t.mint AND ps.delay_minutes = ?
        WHERE ps.id IS NULL
          AND t.status != 'dead'
          AND t.created_at BETWEEN ? AND ?
        ORDER BY t.created_at
        LIMIT 30
        """,
        (delay_minutes, low, high),
    ).fetchall()


def get_trade_stats(
    conn: sqlite3.Connection, mint: str, window_sec: int
) -> dict:
    """Return buy count and unique buyer count within window_sec of token creation."""
    row = conn.execute(
        "SELECT created_at FROM tokens WHERE mint = ?", (mint,)
    ).fetchone()
    if not row:
        return {"buy_count": 0, "unique_buyers": 0}
    created_at = row["created_at"]
    cutoff = created_at + window_sec
    stats = conn.execute(
        """
        SELECT
            COUNT(*) FILTER (WHERE tx_type = 'buy') AS buy_count,
            COUNT(DISTINCT wallet) FILTER (WHERE tx_type = 'buy') AS unique_buyers
        FROM trades
        WHERE token_mint = ? AND ts <= ?
        """,
        (mint, cutoff),
    ).fetchone()
    return dict(stats)
