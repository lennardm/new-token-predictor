"""
analyzer.py — Offline feature extraction + correlation/ML analysis.

Run after collecting data:
    python analyzer.py [--db data/tokens.db] [--out results/]
"""

# ---------------------------------------------------------------------------
# Version history
# ---------------------------------------------------------------------------
# v1.0  2025-02-28  Initial version — trade features + RugCheck + CryptoPanic
#                   social + basic targets (market_cap_1h, ath, survived_24h)
# v1.1  2025-03-01  SQL rewrite of load_trades_features (vectorised, no Python
#                   loop); add ST risk columns; add reached_20k/50k/100k/1m
#                   targets; drop broken 2x_by_1h; RandomForestClassifier for
#                   binary targets; add is_mayhem_mode, initial_market_cap_sol,
#                   hour_of_day features
# v1.2  2025-03-07  Fix correlation_analysis (pairwise dropna, was always
#                   empty); fix XGBoost (XGBClassifier for binary targets, was
#                   producing NaN); log1p-transform market_cap targets for
#                   regression; median imputation so all tokens are used instead
#                   of being dropped for missing ST columns
# ---------------------------------------------------------------------------

__version__ = "1.2"
__date__    = "2025-03-07"

import argparse
import json
import os
import sqlite3
import warnings

import numpy as np
import pandas as pd
from scipy import stats

warnings.filterwarnings("ignore")

# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------

def load_trades_features(conn: sqlite3.Connection) -> pd.DataFrame:
    """
    Extract per-token features from trade data.

    Phase 1: single SQL GROUP BY for all simple aggregations — avoids loading
             5M+ raw rows into Python for counts/sums.
    Phase 2: vectorised pandas for max drawdown (needs ordered price series).
    Phase 3: wallet-rank approach for top-5 concentration (no apply() loop).
    """
    # --- Phase 1: SQL aggregations ---
    agg = pd.read_sql_query(
        """
        SELECT
            t.token_mint,
            COUNT(CASE WHEN t.tx_type = 'buy'  THEN 1 END)                        AS buy_count,
            COUNT(CASE WHEN t.tx_type = 'sell' THEN 1 END)                        AS sell_count,
            COALESCE(SUM(CASE WHEN t.tx_type = 'buy'  THEN t.sol_amount END), 0)  AS total_buy_vol,
            COALESCE(SUM(CASE WHEN t.tx_type = 'sell' THEN t.sol_amount END), 0)  AS total_sell_vol,
            COALESCE(SUM(t.sol_amount), 0)                                         AS total_volume_sol,
            COUNT(DISTINCT CASE WHEN t.tx_type = 'buy'  THEN t.wallet END)        AS unique_buyers,
            COUNT(DISTINCT CASE WHEN t.tx_type = 'sell' THEN t.wallet END)        AS unique_sellers,
            -- time-bucketed volumes (rel_ts = ts - created_at)
            COALESCE(SUM(CASE WHEN t.ts - tok.created_at <=   60 THEN t.sol_amount END), 0) AS vol_1min_sol,
            COALESCE(SUM(CASE WHEN t.ts - tok.created_at <=  300 THEN t.sol_amount END), 0) AS vol_5min_sol,
            COALESCE(SUM(CASE WHEN t.ts - tok.created_at <= 1200 THEN t.sol_amount END), 0) AS vol_20min_sol,
            COUNT(DISTINCT CASE WHEN t.tx_type = 'buy' AND t.ts - tok.created_at <=   60 THEN t.wallet END) AS buyers_1min,
            COUNT(DISTINCT CASE WHEN t.tx_type = 'buy' AND t.ts - tok.created_at <=  300 THEN t.wallet END) AS buyers_5min,
            -- price velocity: linear regression components, vectorised in Python below
            COUNT(CASE WHEN t.tx_type = 'buy' AND t.token_amount > 0 THEN 1 END) AS _n_pv,
            SUM(CASE WHEN t.tx_type = 'buy' AND t.token_amount > 0
                THEN (t.ts - tok.created_at) END)                                 AS _sum_x,
            SUM(CASE WHEN t.tx_type = 'buy' AND t.token_amount > 0
                THEN t.sol_amount / t.token_amount END)                           AS _sum_y,
            SUM(CASE WHEN t.tx_type = 'buy' AND t.token_amount > 0
                THEN (t.ts - tok.created_at) * (t.sol_amount / t.token_amount) END) AS _sum_xy,
            SUM(CASE WHEN t.tx_type = 'buy' AND t.token_amount > 0
                THEN (t.ts - tok.created_at) * (t.ts - tok.created_at) END)      AS _sum_x2,
            -- inter-trade interval: approximate mean as total_duration / (n-1)
            CASE WHEN COUNT(*) > 1
                THEN (MAX(t.ts) - MIN(t.ts)) / (COUNT(*) - 1)
                ELSE NULL END                                                      AS interval_mean_sec,
            -- token metadata
            CASE WHEN tok.twitter_url  IS NOT NULL AND tok.twitter_url  != '' THEN 1 ELSE 0 END AS has_twitter,
            CASE WHEN tok.telegram_url IS NOT NULL AND tok.telegram_url != '' THEN 1 ELSE 0 END AS has_telegram,
            CASE WHEN tok.website_url  IS NOT NULL AND tok.website_url  != '' THEN 1 ELSE 0 END AS has_website,
            tok.initial_market_cap_sol,
            COALESCE(tok.is_mayhem_mode, 0)                                        AS is_mayhem_mode,
            CAST((tok.created_at % 86400) / 3600 AS INTEGER)                      AS hour_of_day
        FROM trades t
        JOIN tokens tok ON tok.mint = t.token_mint
        GROUP BY t.token_mint
        """,
        conn,
    )

    # Derived ratios
    agg["buy_sell_ratio_count"] = agg["buy_count"] / (agg["sell_count"] + 1)
    agg["buy_sell_ratio_vol"]   = agg["total_buy_vol"] / (agg["total_sell_vol"] + 1e-9)

    # Price velocity: slope = (n·Σxy − Σx·Σy) / (n·Σx² − (Σx)²)
    denom = agg["_n_pv"] * agg["_sum_x2"] - agg["_sum_x"] ** 2
    agg["price_velocity"] = np.where(
        (agg["_n_pv"] >= 2) & (denom.abs() > 1e-18),
        (agg["_n_pv"] * agg["_sum_xy"] - agg["_sum_x"] * agg["_sum_y"]) / denom,
        np.nan,
    )
    agg = agg.drop(columns=["_n_pv", "_sum_x", "_sum_y", "_sum_xy", "_sum_x2"])

    # --- Phase 2: Max drawdown (vectorised cummax, needs ordered price series) ---
    price_series = pd.read_sql_query(
        """
        SELECT t.token_mint,
               t.sol_amount / t.token_amount AS price_sol
        FROM trades t
        WHERE t.tx_type = 'buy' AND t.token_amount > 0
        ORDER BY t.token_mint, t.ts
        """,
        conn,
    )
    price_series["cum_max"] = price_series.groupby("token_mint")["price_sol"].cummax()
    price_series["dd"] = (
        (price_series["cum_max"] - price_series["price_sol"])
        / (price_series["cum_max"] + 1e-18)
    )
    max_dd = price_series.groupby("token_mint")["dd"].max().rename("max_drawdown")

    # --- Phase 3: Top-5 wallet concentration (vectorised rank, no apply loop) ---
    wallet_vols = pd.read_sql_query(
        """
        SELECT token_mint, wallet, SUM(sol_amount) AS vol
        FROM trades
        WHERE tx_type = 'buy'
        GROUP BY token_mint, wallet
        """,
        conn,
    )
    wallet_vols["rank"] = wallet_vols.groupby("token_mint")["vol"].rank(
        ascending=False, method="first"
    )
    top5_sum   = wallet_vols[wallet_vols["rank"] <= 5].groupby("token_mint")["vol"].sum()
    total_sum  = wallet_vols.groupby("token_mint")["vol"].sum()
    top5_conc  = (top5_sum / total_sum.replace(0, np.nan)).rename("top5_wallet_concentration")

    return agg.set_index("token_mint").join(max_dd).join(top5_conc)


def load_risk_features(conn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql_query(
        """
        SELECT token_mint,
               rugcheck_score, rugcheck_level,
               mint_authority_revoked, freeze_authority_revoked,
               top_holder_pct, lp_locked, risks,
               st_score, st_rugged, st_jupiter_verified,
               st_top10_pct, st_holders,
               st_snipers_count, st_snipers_pct,
               st_bundlers_count, st_bundlers_initial_pct,
               st_insiders_count, st_insiders_pct,
               st_dev_pct, st_curve_pct
        FROM token_risk
        """,
        conn,
    )
    level_map = {"good": 0, "warn": 1, "danger": 2}
    df["rugcheck_level_num"] = df["rugcheck_level"].map(level_map)

    def risk_count(r):
        try:
            return len(json.loads(r))
        except Exception:
            return 0

    df["risk_flag_count"] = df["risks"].apply(risk_count)
    return df.drop(columns=["rugcheck_level", "risks"]).set_index("token_mint")


def load_social_features(conn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql_query(
        """
        SELECT token_mint, delay_minutes, mention_count
        FROM social_mentions
        WHERE source = 'cryptopanic'
        """,
        conn,
    )
    if df.empty:
        return pd.DataFrame(columns=["token_mint"]).set_index("token_mint")
    pivot = df.pivot_table(
        index="token_mint",
        columns="delay_minutes",
        values="mention_count",
        aggfunc="sum",
    )
    pivot.columns = [f"cp_mentions_{int(c)}min" for c in pivot.columns]
    return pivot


def load_macro_features(conn: sqlite3.Connection) -> pd.DataFrame:
    tokens_df = pd.read_sql_query(
        "SELECT mint, created_at FROM tokens", conn
    )
    market_df = pd.read_sql_query(
        """
        SELECT ts, sol_price_usd, sol_change_24h_pct, sol_volume_24h_usd,
               btc_price_usd, btc_change_24h_pct
        FROM market_snapshots
        ORDER BY ts ASC
        """,
        conn,
    )

    empty_cols = [
        "sol_price_usd", "sol_change_24h_pct", "sol_volume_24h_usd",
        "btc_price_usd", "btc_change_24h_pct", "pumpfun_launch_rate_1h",
    ]
    if market_df.empty:
        return pd.DataFrame(columns=["token_mint"] + empty_cols).set_index("token_mint")

    tokens_sorted = tokens_df.sort_values("created_at")
    merged = pd.merge_asof(
        tokens_sorted.rename(columns={"created_at": "ts"}),
        market_df.sort_values("ts"),
        on="ts",
        direction="backward",
    )

    # Compute pumpfun_launch_rate_1h from raw token timestamps
    created_arr = tokens_df["created_at"].values
    rates = [
        float(((created_arr >= t - 3600) & (created_arr < t)).sum())
        for t in merged["ts"].values
    ]
    merged["pumpfun_launch_rate_1h"] = rates

    return (
        merged.rename(columns={"mint": "token_mint"})
        .drop(columns=["ts"])
        .set_index("token_mint")
    )


def load_targets(conn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql_query(
        """
        SELECT token_mint, delay_minutes,
               market_cap_usd, ath_market_cap_usd, liquidity_usd
        FROM price_snapshots
        """,
        conn,
    )

    mc_1h = df[df["delay_minutes"] ==   60][["token_mint", "market_cap_usd"]].rename(
        columns={"market_cap_usd": "market_cap_1h"}
    )
    mc_2h = df[df["delay_minutes"] ==  120][["token_mint", "market_cap_usd"]].rename(
        columns={"market_cap_usd": "market_cap_2h"}
    )
    mc_4h = df[df["delay_minutes"] ==  240][["token_mint", "market_cap_usd"]].rename(
        columns={"market_cap_usd": "market_cap_4h"}
    )
    ath_mc = (
        df.groupby("token_mint")["ath_market_cap_usd"]
        .max()
        .reset_index()
    )
    survived = (
        df[df["delay_minutes"] == 1440]
        .assign(survived_24h=lambda x: (x["liquidity_usd"].fillna(0) > 0).astype(int))[
            ["token_mint", "survived_24h"]
        ]
    )

    targets = mc_1h
    for part in [mc_2h, mc_4h, ath_mc, survived]:
        targets = targets.merge(part, on="token_mint", how="left")
    targets = targets.set_index("token_mint")

    # Binary thresholds based on ATH market cap (project goal: $20k/$50k/$100k/$1M)
    for threshold, label in [
        (20_000,     "20k"),
        (50_000,     "50k"),
        (100_000,    "100k"),
        (1_000_000,  "1m"),
    ]:
        targets[f"reached_{label}"] = (
            targets["ath_market_cap_usd"].fillna(0) >= threshold
        ).astype(int)

    targets["10x_by_4h"] = (
        targets["ath_market_cap_usd"] >= 10 * targets["market_cap_1h"]
    ).fillna(0).astype(int)

    # Log-transformed regression targets — market cap is fat-tailed, log1p
    # produces a near-normal distribution that regressors can actually fit
    for col in ["market_cap_1h", "market_cap_2h", "market_cap_4h", "ath_market_cap_usd"]:
        if col in targets:
            targets[f"log_{col}"] = np.log1p(targets[col].clip(lower=0))

    return targets


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def correlation_analysis(features: pd.DataFrame, targets: pd.DataFrame) -> None:
    print("\n=== Correlation Analysis ===\n")
    combined = features.join(targets, how="inner")
    feature_cols = features.columns.tolist()

    for tcol in targets.columns:
        target_valid = combined[tcol].dropna()
        if len(target_valid) < 5:
            continue
        print(f"\n--- Target: {tcol}  (n={len(target_valid)}) ---")
        rows = []
        for fcol in feature_cols:
            # Pairwise dropna: only drop rows missing THIS feature or the target
            pair = combined[[tcol, fcol]].dropna()
            if len(pair) < 5 or pair[fcol].nunique() < 2:
                continue
            x, y = pair[fcol], pair[tcol]
            try:
                pearson_r,  pearson_p  = stats.pearsonr(x, y)
                spearman_r, spearman_p = stats.spearmanr(x, y)
                rows.append({
                    "feature":    fcol,
                    "pearson_r":  pearson_r,
                    "pearson_p":  pearson_p,
                    "spearman_r": spearman_r,
                    "spearman_p": spearman_p,
                })
            except Exception:
                pass
        if rows:
            corr_df = (
                pd.DataFrame(rows)
                .sort_values("spearman_r", key=abs, ascending=False)
                .head(15)
            )
            print(corr_df.to_string(index=False, float_format="{:.3f}".format))


def ml_analysis(features: pd.DataFrame, targets: pd.DataFrame) -> None:
    try:
        from sklearn.ensemble import RandomForestRegressor, RandomForestClassifier
        from sklearn.model_selection import cross_val_score
        from sklearn.preprocessing import StandardScaler
        from sklearn.pipeline import Pipeline
    except ImportError:
        print("scikit-learn not available — skipping ML analysis")
        return

    print("\n=== Feature Importance (RandomForest) ===\n")
    combined = features.join(targets, how="inner")
    feature_cols = [c for c in features.columns if combined[c].notna().sum() > 5]

    reg_targets  = ["log_market_cap_1h", "log_ath_market_cap_usd"]
    clf_targets  = ["survived_24h", "reached_50k", "reached_100k"]

    for tcol in reg_targets:
        if tcol not in combined:
            continue
        valid = combined[feature_cols + [tcol]].dropna()
        if len(valid) < 20:
            print(f"Skipping {tcol}: only {len(valid)} complete rows")
            continue
        X, y = valid[feature_cols].values, valid[tcol].values
        pipe = Pipeline([
            ("scaler", StandardScaler()),
            ("rf", RandomForestRegressor(n_estimators=200, random_state=42, n_jobs=-1)),
        ])
        cv = cross_val_score(pipe, X, y, cv=5, scoring="r2")
        pipe.fit(X, y)
        imp_df = (
            pd.DataFrame({"feature": feature_cols,
                          "importance": pipe.named_steps["rf"].feature_importances_})
            .sort_values("importance", ascending=False)
            .head(15)
        )
        print(f"\n--- {tcol}  CV R²={cv.mean():.3f} ± {cv.std():.3f} ---")
        print(imp_df.to_string(index=False, float_format="{:.4f}".format))

    for tcol in clf_targets:
        if tcol not in combined:
            continue
        valid = combined[feature_cols + [tcol]].dropna()
        if len(valid) < 20:
            print(f"Skipping {tcol}: only {len(valid)} complete rows")
            continue
        X, y = valid[feature_cols].values, valid[tcol].values
        pipe = Pipeline([
            ("scaler", StandardScaler()),
            ("rf", RandomForestClassifier(n_estimators=200, random_state=42, n_jobs=-1)),
        ])
        cv = cross_val_score(pipe, X, y, cv=5, scoring="roc_auc")
        pipe.fit(X, y)
        imp_df = (
            pd.DataFrame({"feature": feature_cols,
                          "importance": pipe.named_steps["rf"].feature_importances_})
            .sort_values("importance", ascending=False)
            .head(15)
        )
        print(f"\n--- {tcol}  CV ROC-AUC={cv.mean():.3f} ± {cv.std():.3f} ---")
        print(imp_df.to_string(index=False, float_format="{:.4f}".format))


def xgboost_analysis(features: pd.DataFrame, targets: pd.DataFrame) -> None:
    try:
        from xgboost import XGBClassifier, XGBRegressor
        from sklearn.model_selection import cross_val_score
    except ImportError:
        print("xgboost not available — skipping XGBoost analysis")
        return

    print("\n=== XGBoost Analysis ===\n")
    combined = features.join(targets, how="inner")
    feature_cols = [c for c in features.columns if combined[c].notna().sum() > 5]

    reg_tasks = ["log_market_cap_1h", "log_ath_market_cap_usd"]
    clf_tasks = ["survived_24h", "reached_50k", "reached_100k", "10x_by_4h"]

    for tcol in reg_tasks:
        if tcol not in combined:
            continue
        valid = combined[feature_cols + [tcol]].dropna()
        if len(valid) < 20:
            continue
        X, y = valid[feature_cols].values, valid[tcol].values
        model = XGBRegressor(
            n_estimators=200, learning_rate=0.05, max_depth=4,
            random_state=42, verbosity=0,
        )
        cv = cross_val_score(model, X, y, cv=5, scoring="r2")
        print(f"  {tcol:<30} r2={cv.mean():.3f} ± {cv.std():.3f}  (n={len(valid)})")

    for tcol in clf_tasks:
        if tcol not in combined:
            continue
        valid = combined[feature_cols + [tcol]].dropna()
        if len(valid) < 20:
            continue
        X, y = valid[feature_cols].values, valid[tcol].values
        model = XGBClassifier(
            n_estimators=200, learning_rate=0.05, max_depth=4,
            random_state=42, verbosity=0, eval_metric="logloss",
            use_label_encoder=False,
        )
        cv = cross_val_score(model, X, y, cv=5, scoring="roc_auc")
        print(f"  {tcol:<30} roc_auc={cv.mean():.3f} ± {cv.std():.3f}  (n={len(valid)})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Analyze collected token data")
    parser.add_argument("--db",  default="data/tokens.db", help="SQLite DB path")
    parser.add_argument("--out", default="results/",       help="Output directory")
    args = parser.parse_args()

    print(f"analyzer.py  v{__version__}  ({__date__})")

    if not os.path.exists(args.db):
        print(f"Database not found: {args.db}")
        return

    os.makedirs(args.out, exist_ok=True)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    print("Loading features...")
    trade_features  = load_trades_features(conn)
    risk_features   = load_risk_features(conn)
    social_features = load_social_features(conn)
    macro_features  = load_macro_features(conn)
    targets         = load_targets(conn)

    features = (
        trade_features
        .join(risk_features,   how="left")
        .join(social_features, how="left")
        .join(macro_features,  how="left")
    )

    # Median imputation for sparse columns (ST risk, macro) so rows aren't
    # dropped just because SolanaTracker hadn't indexed a token yet
    numeric_cols = features.select_dtypes(include="number").columns
    features[numeric_cols] = features[numeric_cols].fillna(
        features[numeric_cols].median()
    )

    print(f"Feature matrix: {features.shape}  Tokens with targets: {len(targets)}")

    features.to_csv(os.path.join(args.out, "features.csv"))
    targets.to_csv(os.path.join(args.out, "targets.csv"))
    print(f"Saved to {args.out}")

    if len(features) < 5:
        print("Not enough data for analysis (< 5 tokens). Collect more first.")
        return

    correlation_analysis(features, targets)
    ml_analysis(features, targets)
    xgboost_analysis(features, targets)

    print("\nDone.")


if __name__ == "__main__":
    main()
