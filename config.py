import os

# WebSocket
PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"

# Collection windows
COLLECTION_WINDOW_SEC = 1200  # 20 minutes of trade collection
VIABILITY_WINDOW_SEC = 60     # first 60s viability check
MIN_BUYS = 10
MIN_UNIQUE_BUYERS = 5

# Price snapshot delays (minutes after token creation)
SNAPSHOT_DELAYS_MIN = [60, 120, 240, 480, 1440]

# DexScreener
DEXSCREENER_BASE = "https://api.dexscreener.com"
DEXSCREENER_RPM = 55  # stay under 60 req/min

# RugCheck
RUGCHECK_BASE = "https://api.rugcheck.xyz/v1"
ENRICHMENT_DELAY_SEC = 45  # wait before querying RugCheck (indexing lag)

# GoPlus fallback
GOPLUS_BASE = "https://api.gopluslabs.io/api/v1"

# CryptoPanic
CRYPTOPANIC_BASE = "https://cryptopanic.com/api/v1"
CRYPTOPANIC_TOKEN = os.getenv("CRYPTOPANIC_TOKEN", "")
# Minimum total buy volume (SOL) for a token to qualify for CryptoPanic lookup.
# Tune this after collecting data — check the distribution of early buy volumes.
CRYPTOPANIC_MIN_BUY_SOL = 5.0

# SolanaTracker
SOLANATRACKER_BASE = "https://data.solanatracker.io"
SOLANATRACKER_KEY = os.getenv("SOLANATRACKER_KEY", "")

# CoinGecko (free tier, no key required)
COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# Database
DB_PATH = "data/tokens.db"
