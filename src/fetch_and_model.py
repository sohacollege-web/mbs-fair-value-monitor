"""
Agency MBS Fair Value Monitor
-----------------------------
Estimates a fair-value mortgage spread from public supply/demand technicals
(rate volatility, Fed MBS holdings, and commercial bank demand), flags
rich/cheap and crowding conditions, and analyzes behavior across rate regimes.

Data: FRED (Federal Reserve Economic Data) - free, no API key required.
Run:  python src/fetch_and_model.py
Output: data/merged.csv, three charts in figures/, and a printed regime table.
"""

import io
import os
import urllib.request
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import statsmodels.api as sm

FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "data")
FIG = os.path.join(HERE, "figures")
os.makedirs(DATA, exist_ok=True)
os.makedirs(FIG, exist_ok=True)


def fetch_fred(series_id):
    """Download one FRED series as a pandas Series."""
    url = FRED.format(series_id)
    raw = urllib.request.urlopen(url, timeout=30).read().decode("utf-8")
    df = pd.read_csv(io.StringIO(raw))
    df.columns = ["date", "value"]
    df["date"] = pd.to_datetime(df["date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df.dropna().set_index("date")["value"]


def build():
    # 1. Core series
    print("Downloading FRED series...")
    mortgage30 = fetch_fred("MORTGAGE30US")  # 30y mortgage rate, weekly
    ust10 = fetch_fred("DGS10")              # 10y Treasury yield, daily
    fed_mbs = fetch_fred("WSHOMCB")          # Fed SOMA MBS holdings, weekly ($mn)

    # Commercial bank demand: Treasury & agency securities held by all banks
    # (H.8). A proxy for bank appetite for the asset class. Wrapped so the
    # script still runs if the series id ever changes.
    have_banks = True
    try:
        banks = fetch_fred("TASACBW027SBOG")  # $bn, weekly
    except Exception as e:
        have_banks = False
        print("Could not fetch bank series, continuing without it:", e)

    # 2. Rate volatility: 21-day rolling std of daily 10y changes
    ratevol_daily = ust10.diff().rolling(21).std()

    # 3. Resample to month-end
    m_mortgage = mortgage30.resample("ME").last()
    m_ust10 = ust10.resample("ME").last()
    m_fed = fed_mbs.resample("ME").last()
    m_ratevol = ratevol_daily.resample("ME").last()

    spread = (m_mortgage - m_ust10).rename("spread")
    fed_chg = m_fed.diff().rename("fed_mbs_chg")
    ratevol = m_ratevol.rename("ratevol")

    cols = [spread, ratevol, fed_chg]
    drivers = ["ratevol", "fed_mbs_chg"]
    if have_banks:
        bank_chg = banks.resample("ME").last().diff().rename("bank_chg")
        cols.append(bank_chg)
        drivers.append("bank_chg")

    df = pd.concat(cols, axis=1).dropna()

    # 4. Regression -> fair value
    X = sm.add_constant(df[drivers])
    model = sm.OLS(df["spread"], X).fit()
    print(model.summary())

    df["fair_value"] = model.predict(X)
    df["residual"] = df["spread"] - df["fair_value"]  # +ve = cheap, -ve = rich

    # 5. Crowding overlay (0-100): rich (tight vs fair value) + calm (low vol)
    def z(s):
        return (s - s.mean()) / s.std()
    crowd_z = 0.5 * z(-df["residual"]) + 0.5 * z(-df["ratevol"])
    df["crowding_score"] = 100 / (1 + np.exp(-crowd_z))
    df.to_csv(os.path.join(DATA, "merged.csv"))

    # 6. Charts
    plt.figure(figsize=(9, 4))
    plt.plot(df.index, df["spread"], label="Actual spread")
    plt.plot(df.index, df["fair_value"], "--", label="Fair value")
    plt.title("Agency MBS Spread: Actual vs. Fair Value")
    plt.ylabel("Mortgage spread (%)"); plt.legend(); plt.tight_layout()
    plt.savefig(os.path.join(FIG, "actual_vs_fairvalue.png"), dpi=130)

    plt.figure(figsize=(9, 4))
    plt.bar(df.index, df["residual"], width=20,
            color=np.where(df["residual"] >= 0, "#2E7D32", "#C62828"))
    plt.axhline(0, color="black", linewidth=0.8)
    plt.title("Rich / Cheap Signal (Actual minus Fair Value)")
    plt.ylabel("Residual (%)   +cheap / -rich"); plt.tight_layout()
    plt.savefig(os.path.join(FIG, "rich_cheap_signal.png"), dpi=130)

    plt.figure(figsize=(9, 4))
    plt.plot(df.index, df["crowding_score"], color="#6A1B9A")
    plt.axhline(80, color="#C62828", linestyle="--", linewidth=0.8, label="Crowded (>80)")
    plt.axhline(20, color="#2E7D32", linestyle="--", linewidth=0.8, label="Uncrowded (<20)")
    plt.ylim(0, 100)
    plt.title("MBS Crowding Score (0 = nobody in, 100 = everyone piled in)")
    plt.ylabel("Crowding score"); plt.legend(); plt.tight_layout()
    plt.savefig(os.path.join(FIG, "crowding_score.png"), dpi=130)

    # 7. Regime analysis: how spreads and crowding behaved in key episodes
    regimes = {
        "2013 Taper Tantrum": ("2013-05-01", "2013-12-31"),
        "2020 COVID shock":   ("2020-02-01", "2020-06-30"),
        "2022 QT / rate shock": ("2022-01-01", "2022-12-31"),
    }
    rows = []
    for name, (start, end) in regimes.items():
        w = df.loc[start:end]
        if len(w) < 2:
            continue
        rows.append({
            "Regime": name,
            "Avg spread (%)": round(w["spread"].mean(), 2),
            "Spread change (pp)": round(w["spread"].iloc[-1] - w["spread"].iloc[0], 2),
            "Avg crowding": round(w["crowding_score"].mean(), 0),
        })
    regime_table = pd.DataFrame(rows)
    print("\n=== Regime analysis ===")
    print(regime_table.to_string(index=False))
    regime_table.to_csv(os.path.join(DATA, "regime_summary.csv"), index=False)

    print("\nDone. Wrote data/merged.csv, data/regime_summary.csv, and three figures/.")
    print(f"R-squared: {model.rsquared:.3f}  |  drivers: {', '.join(drivers)}")


if __name__ == "__main__":
    build()
