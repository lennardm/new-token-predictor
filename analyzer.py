"""
analyzer.py — Offline feature extraction + correlation/ML analysis.

Run after collecting data:
    python analyzer.py [--db data/tokens.db] [--out results/]
"""

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
    trades_df = pd.read_sql_query(
        "SELECT token_mint, ts, tx_type, sol_amount, token_amount, wallet FROM trades",
        conn,
    )
    tokens_df = pd.read_sql_query(
        "SELECT mint, created_at, twitter_url, telegram_url, website_url FROM tokens",
        conn,
    )
    trades_df = trades_df.merge(
        tokens_df.rename(columns={"mint": "token_mint"}),
        on="token_mint",
        how="left",
    )
    trades_df["rel_ts"] = trades_df["ts"] - trades_df["created_at"]

    rows = []
    for mint, grp in trades_df.groupby("token_mint"):
        token_row = tokens_df[tokens_df["mint"] == mint].iloc[0]
        row = _extract_features(mint, grp, token_row)
        rows.append(row)

    return pd.DataFrame(rows).set_index("token_mint")


def _extract_features(mint: str, grp: pd.DataFrame, token_row: pd.Series) -> dict:
    buys = grp[grp["tx_type"] == "buy"]
    sells = grp[grp["tx_type"] == "sell"]

    total_buy_vol = buys["sol_amount"].sum()
    total_sell_vol = sells["sol_amount"].sum()
    total_vol = grp["sol_amount"].sum()

    buy_count = len(buys)
    sell_count = len(sells)

    unique_buyers = buys["wallet"].nunique()
    unique_sellers = sells["wallet"].nunique()

    # Time-bucketed volumes
    vol_1min = grp[grp["rel_ts"] <= 60]["sol_amount"].sum()
    vol_5min = grp[grp["rel_ts"] <= 300]["sol_amount"].sum()
    vol_20min = grp[grp["rel_ts"] <= 1200]["sol_amount"].sum()

    buyers_1min = grp[(grp["rel_ts"] <= 60) & (grp["tx_type"] == "buy")]["wallet"].nunique()
    buyers_5min = grp[(grp["rel_ts"] <= 300) & (grp["tx_type"] == "buy")]["wallet"].nunique()

    # Price velocity (linear slope over first 20 min, using price_sol approximation)
    price_df = grp[grp["token_amount"] > 0].copy()
    price_df["price_sol"] = price_df["sol_amount"] / price_df["token_amount"]
    price_df = price_df.sort_values("rel_ts")
    price_velocity = np.nan
    if len(price_df) >= 2:
        slope, *_ = np.polyfit(price_df["rel_ts"], price_df["price_sol"], 1)
        price_velocity = slope

    # Max drawdown in first 20 min
    max_drawdown = np.nan
    if len(price_df) >= 2:
        prices = price_df["price_sol"].values
        running_max = np.maximum.accumulate(prices)
        drawdowns = (running_max - prices) / (running_max + 1e-18)
        max_drawdown = float(drawdowns.max())

    # Top-5 wallet concentration (% of total buy volume)
    wallet_vols = buys.groupby("wallet")["sol_amount"].sum().sort_values(ascending=False)
    top5_vol = wallet_vols.head(5).sum()
    top5_concentration = top5_vol / total_buy_vol if total_buy_vol > 0 else np.nan

    # Inter-trade interval stats (bot fingerprint)
    sorted_ts = np.sort(grp["ts"].values)
    intervals = np.diff(sorted_ts)
    interval_mean = float(intervals.mean()) if len(intervals) > 0 else np.nan
    interval_std = float(intervals.std()) if len(intervals) > 1 else np.nan

    # Social presence
    has_twitter = 1 if token_row.get("twitter_url") else 0
    has_telegram = 1 if token_row.get("telegram_url") else 0
    has_website = 1 if token_row.get("website_url") else 0

    return {
        "token_mint": mint,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "buy_sell_ratio_count": buy_count / (sell_count + 1),
        "buy_sell_ratio_vol": total_buy_vol / (total_sell_vol + 1e-9),
        "unique_buyers": unique_buyers,
        "unique_sellers": unique_sellers,
        "total_volume_sol": total_vol,
        "vol_1min_sol": vol_1min,
        "vol_5min_sol": vol_5min,
        "vol_20min_sol": vol_20min,
        "buyers_1min": buyers_1min,
        "buyers_5min": buyers_5min,
        "price_velocity": price_velocity,
        "max_drawdown": max_drawdown,
        "top5_wallet_concentration": top5_concentration,
        "interval_mean_sec": interval_mean,
        "interval_std_sec": interval_std,
        "has_twitter": has_twitter,
        "has_telegram": has_telegram,
        "has_website": has_website,
    }


def load_risk_features(conn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql_query(
        """
        SELECT token_mint, rugcheck_score, rugcheck_level,
               mint_authority_revoked, freeze_authority_revoked,
               top_holder_pct, lp_locked, risks
        FROM token_risk
        """,
        conn,
    )
    # Expand level to numeric
    level_map = {"good": 0, "warn": 1, "danger": 2}
    df["rugcheck_level_num"] = df["rugcheck_level"].map(level_map)

    # Risk flag counts
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
        """SELECT ts, sol_price_usd, sol_change_24h_pct, sol_volume_24h_usd,
                  btc_price_usd, btc_change_24h_pct
           FROM market_snapshots ORDER BY ts ASC""",
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

    # Compute pumpfun_launch_rate_1h from the tokens table
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
    # Get launch market cap (first available snapshot or 60min)
    launch_mc = df[df["delay_minutes"] == 60][["token_mint", "market_cap_usd"]].rename(
        columns={"market_cap_usd": "market_cap_1h"}
    )
    mc_2h = df[df["delay_minutes"] == 120][["token_mint", "market_cap_usd"]].rename(
        columns={"market_cap_usd": "market_cap_2h"}
    )
    mc_4h = df[df["delay_minutes"] == 240][["token_mint", "market_cap_usd"]].rename(
        columns={"market_cap_usd": "market_cap_4h"}
    )
    ath_mc = (
        df.groupby("token_mint")["ath_market_cap_usd"]
        .max()
        .reset_index()
        .rename(columns={"ath_market_cap_usd": "ath_market_cap_usd"})
    )
    survived = (
        df[df["delay_minutes"] == 1440]
        .assign(survived_24h=lambda x: (x["liquidity_usd"].fillna(0) > 0).astype(int))[
            ["token_mint", "survived_24h"]
        ]
    )

    targets = launch_mc
    for part in [mc_2h, mc_4h, ath_mc, survived]:
        targets = targets.merge(part, on="token_mint", how="left")

    targets = targets.set_index("token_mint")

    # Binary labels
    targets["2x_by_1h"] = (
        targets["market_cap_1h"] >= 2 * targets["market_cap_1h"]  # placeholder — see note
    ).astype(float)
    # Proper 2x: we'd need launch market cap; use ath vs 1h as proxy
    targets["10x_by_4h"] = (
        targets["ath_market_cap_usd"] >= 10 * targets["market_cap_1h"]
    ).fillna(0).astype(int)

    return targets


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def correlation_analysis(features: pd.DataFrame, targets: pd.DataFrame) -> None:
    print("\n=== Correlation Analysis ===\n")
    combined = features.join(targets, how="inner")
    feature_cols = features.columns.tolist()
    target_cols = targets.columns.tolist()

    for tcol in target_cols:
        valid = combined[[tcol] + feature_cols].dropna()
        if len(valid) < 5:
            continue
        print(f"\n--- Target: {tcol}  (n={len(valid)}) ---")
        rows = []
        for fcol in feature_cols:
            x = valid[fcol]
            y = valid[tcol]
            if x.nunique() < 2:
                continue
            try:
                pearson_r, pearson_p = stats.pearsonr(x, y)
                spearman_r, spearman_p = stats.spearmanr(x, y)
                rows.append({
                    "feature": fcol,
                    "pearson_r": pearson_r,
                    "pearson_p": pearson_p,
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
        from sklearn.ensemble import RandomForestRegressor
        from sklearn.model_selection import cross_val_score
        from sklearn.preprocessing import StandardScaler
        from sklearn.pipeline import Pipeline
    except ImportError:
        print("scikit-learn not available — skipping ML analysis")
        return

    print("\n=== Feature Importance (RandomForest) ===\n")
    combined = features.join(targets, how="inner")
    feature_cols = [c for c in features.columns if combined[c].notna().sum() > 5]

    for tcol in ["market_cap_1h", "ath_market_cap_usd", "survived_24h"]:
        if tcol not in combined:
            continue
        valid = combined[feature_cols + [tcol]].dropna()
        if len(valid) < 20:
            print(f"Skipping {tcol}: only {len(valid)} complete rows")
            continue
        X = valid[feature_cols].values
        y = valid[tcol].values

        pipe = Pipeline([
            ("scaler", StandardScaler()),
            ("rf", RandomForestRegressor(n_estimators=200, random_state=42, n_jobs=-1)),
        ])
        cv_scores = cross_val_score(pipe, X, y, cv=5, scoring="r2")
        pipe.fit(X, y)
        importances = pipe.named_steps["rf"].feature_importances_
        imp_df = (
            pd.DataFrame({"feature": feature_cols, "importance": importances})
            .sort_values("importance", ascending=False)
            .head(15)
        )
        print(f"\n--- Target: {tcol}  CV R²={cv_scores.mean():.3f} ± {cv_scores.std():.3f} ---")
        print(imp_df.to_string(index=False, float_format="{:.4f}".format))


def xgboost_analysis(features: pd.DataFrame, targets: pd.DataFrame) -> None:
    try:
        import xgboost as xgb
        from sklearn.model_selection import cross_val_score
    except ImportError:
        print("xgboost not available — skipping XGBoost analysis")
        return

    print("\n=== XGBoost Analysis ===\n")
    combined = features.join(targets, how="inner")
    feature_cols = [c for c in features.columns if combined[c].notna().sum() > 5]

    for tcol, task in [
        ("market_cap_1h", "reg:squarederror"),
        ("survived_24h", "binary:logistic"),
        ("10x_by_4h", "binary:logistic"),
    ]:
        if tcol not in combined:
            continue
        valid = combined[feature_cols + [tcol]].dropna()
        if len(valid) < 20:
            continue
        X = valid[feature_cols].values
        y = valid[tcol].values
        scoring = "r2" if "reg" in task else "roc_auc"
        model = xgb.XGBRegressor(
            objective=task,
            n_estimators=200,
            learning_rate=0.05,
            max_depth=4,
            random_state=42,
            verbosity=0,
            eval_metric="rmse" if "reg" in task else "logloss",
        )
        cv = cross_val_score(model, X, y, cv=5, scoring=scoring)
        print(f"Target: {tcol}  {scoring}={cv.mean():.3f} ± {cv.std():.3f}  (n={len(valid)})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Analyze collected token data")
    parser.add_argument("--db", default="data/tokens.db", help="SQLite DB path")
    parser.add_argument("--out", default="results/", help="Output directory")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"Database not found: {args.db}")
        return

    os.makedirs(args.out, exist_ok=True)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    print("Loading features...")
    trade_features = load_trades_features(conn)
    risk_features = load_risk_features(conn)
    social_features = load_social_features(conn)
    macro_features = load_macro_features(conn)
    targets = load_targets(conn)

    features = (
        trade_features
        .join(risk_features, how="left")
        .join(social_features, how="left")
        .join(macro_features, how="left")
    )

    print(f"Feature matrix: {features.shape}  Tokens with targets: {len(targets)}")

    # Save feature matrix
    features.to_csv(os.path.join(args.out, "features.csv"))
    targets.to_csv(os.path.join(args.out, "targets.csv"))
    print(f"Saved feature matrix and targets to {args.out}")

    if len(features) < 5:
        print("Not enough data for analysis (< 5 tokens). Collect more first.")
        return

    correlation_analysis(features, targets)
    ml_analysis(features, targets)
    xgboost_analysis(features, targets)

    print("\nDone.")


if __name__ == "__main__":
    main()
