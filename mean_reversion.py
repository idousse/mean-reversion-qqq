"""
Replication and stress-test of "A Mean Reversion Strategy with 2.11 Sharpe" (Quantitativo).

The three variants of the article are a single parameterised strategy:

    base    : original rules
    regime  : + market regime filter (trade only above the SMA)
    stop    : + dynamic stop (exit below the SMA), no regime filter

What this module adds to a plain replication:
    * transaction costs, and a sensitivity table across cost levels
    * an out-of-sample split, so parameters are not evaluated on the period
      they were chosen on
    * a one-at-a-time parameter-sensitivity sweep, to see whether the edge
      depends on the article's specific parameter values
    * a Sharpe ratio recomputed by hand from the daily equity curve, with its
      standard error, next to the one reported by backtesting.py
    * cached price data, so results are reproducible run to run

Usage
-----
    python mean_reversion.py --variant base
    python mean_reversion.py --variant stop --costs 0,1,2.5,5,10 --oos-start 2019-01-01
    python mean_reversion.py --all-variants --plot
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from backtesting import Backtest, Strategy

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

SYMBOL = "QQQ"
START = "1999-03-10"
END = "2024-05-18"
CASH = 100_000
PERIODS_PER_YEAR = 252

# Cost levels used for the sensitivity table, in basis points per side.
# 0 bps reproduces the article; 5 bps is a plausible retail round-trip on a
# liquid ETF once spread and slippage are included.
DEFAULT_COSTS_BPS = (0.0, 1.0, 2.5, 5.0, 10.0)

VARIANTS = {
    "base": dict(regime_filter=False, dynamic_stop=False),
    "regime": dict(regime_filter=True, dynamic_stop=False),
    "stop": dict(regime_filter=False, dynamic_stop=True),
}

# Grid for the one-at-a-time parameter-sensitivity sweep. Each list brackets the
# article's default (the class attribute on RollingIBS) so the sweep shows
# whether that default sits on a stable plateau or a fragile spike. sma_window
# is only used by the regime/stop variants and is skipped for base.
DEFAULT_PARAM_GRID = {
    "range_window": [15, 20, 25, 30, 40],
    "high_window": [5, 8, 10, 15, 20],
    "band_mult": [1.5, 2.0, 2.5, 3.0, 3.5],
    "ibs_threshold": [0.2, 0.3, 0.4, 0.5],
    "sma_window": [150, 200, 250, 300, 350],
}


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #

def load_data(
    symbol: str = SYMBOL,
    start: str = START,
    end: str = END,
    cache_dir: str | Path = "data",
    refresh: bool = False,
) -> pd.DataFrame:
    """Load OHLCV data, caching it to CSV.

    The cache matters for reproducibility: Yahoo revises its adjusted history
    over time, so an uncached backtest can silently change results between runs.

    Prices are kept as downloaded (split/dividend adjusted, not rounded).
    Rounding adjusted prices to two decimals, as the original scripts did,
    distorts the early history, where an adjusted price can be a few dollars
    and the High/Low range a few cents.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{symbol}_{start}_{end}.csv"

    if path.exists() and not refresh:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
    else:
        import yfinance as yf  # imported lazily so cached runs need no network

        df = yf.download(symbol, start=start, end=end, auto_adjust=True, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.to_csv(path)

    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df.index = pd.to_datetime(df.index, utc=True).tz_localize(None)
    return df


# --------------------------------------------------------------------------- #
# Strategy
# --------------------------------------------------------------------------- #

class RollingIBS(Strategy):
    """Mean reversion on the IBS indicator and a volatility-scaled lower band.

    Entry  : Close < rolling_high(high_window) - band_mult * mean(High - Low)
             and IBS < ibs_threshold
    Exit   : Close > yesterday's High
             (+ Close < SMA if dynamic_stop)
    Filter : trade only while Close > SMA if regime_filter

    Two deliberate differences from the original scripts:

    1. Every comparison indexes the current bar explicitly ([-1]) instead of
       comparing whole arrays. Array comparisons happened to work because
       backtesting.py's array wrapper defines __bool__ as "last element", but
       relying on that is fragile and hard to defend.
    2. Entries are skipped while a position is open, so the strategy never
       pyramids and never opens and closes on the same bar. This makes the
       trade list mean what it says.
    """

    range_window = 25       # lookback for the mean daily range
    high_window = 10        # lookback for the rolling high
    band_mult = 2.5         # width of the lower band, in mean ranges
    ibs_threshold = 0.3     # buy only in the bottom of the daily range
    sma_window = 300        # regime filter / dynamic stop
    regime_filter = False
    dynamic_stop = False

    def init(self):
        high = pd.Series(self.data.High)
        low = pd.Series(self.data.Low)
        close = pd.Series(self.data.Close)

        daily_range = high - low
        mean_range = daily_range.rolling(self.range_window).mean()
        rolling_high = high.rolling(self.high_window).max()

        # IBS: where in the daily range did we close? 0 = at the low, 1 = at the high.
        ibs = ((close - low) / daily_range).replace([np.inf, -np.inf], np.nan)

        self.lower_band = self.I(
            lambda: (rolling_high - self.band_mult * mean_range).to_numpy(),
            name="lower_band",
        )
        self.ibs = self.I(lambda: ibs.to_numpy(), name="ibs")
        self.sma = self.I(
            lambda: close.rolling(self.sma_window).mean().to_numpy(), name="sma"
        )

    def next(self):
        # NaN comparisons are False, so nothing trades during the warm-up.
        price = self.data.Close[-1]
        sma = self.sma[-1]

        if self.regime_filter and not price > sma:
            if self.position:
                self.position.close()
            return

        if self.position:
            exit_signal = price > self.data.High[-2]
            if self.dynamic_stop and price < sma:
                exit_signal = True
            if exit_signal:
                self.position.close()
            return

        if price < self.lower_band[-1] and self.ibs[-1] < self.ibs_threshold:
            self.buy()


# --------------------------------------------------------------------------- #
# Running a backtest
# --------------------------------------------------------------------------- #

def run_backtest(
    data: pd.DataFrame,
    variant: str = "base",
    cost_bps: float = 0.0,
    cash: int = CASH,
    plot: bool = False,
    **overrides,
):
    """Run one variant at one cost level. Returns backtesting.py's stats Series."""
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}; choose from {list(VARIANTS)}")

    params = {**VARIANTS[variant], **overrides}
    bt = Backtest(data, RollingIBS, cash=cash, commission=cost_bps / 10_000)
    stats = bt.run(**params)
    if plot:
        bt.plot(open_browser=False)
    return stats


# --------------------------------------------------------------------------- #
# Performance measurement
# --------------------------------------------------------------------------- #

def performance_stats(equity: pd.Series, periods_per_year: int = PERIODS_PER_YEAR) -> dict:
    """Recompute performance from the daily equity curve.

    Worth doing by hand: on the original replication backtesting.py reported a
    Sharpe of 0.78 where the article reported 1.83, while annualised return and
    max drawdown matched the article almost exactly. A gap like that is a
    measurement disagreement, not a data disagreement, so the definition being
    used has to be explicit.

    Sharpe here is mean/std of daily returns (risk-free = 0), annualised by
    sqrt(252), over every calendar day in the backtest including flat days.
    Flat days pull volatility down, which flatters a strategy that is only
    invested a fraction of the time -- hence sharpe_invested as well.

    The standard error follows Lo (2002) under the (optimistic) assumption of
    iid returns: se = sqrt((1 + 0.5 * SR^2) / n).
    """
    returns = equity.pct_change().dropna()
    n = len(returns)
    if n == 0 or returns.std() == 0:
        return {}

    years = n / periods_per_year
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1
    vol = returns.std() * np.sqrt(periods_per_year)

    sr_daily = returns.mean() / returns.std()
    sharpe = sr_daily * np.sqrt(periods_per_year)
    se = np.sqrt((1 + 0.5 * sr_daily**2) / n) * np.sqrt(periods_per_year)

    invested = returns[returns != 0]
    sharpe_invested = (
        invested.mean() / invested.std() * np.sqrt(periods_per_year)
        if len(invested) > 1 and invested.std() > 0
        else np.nan
    )

    drawdown = equity / equity.cummax() - 1

    return {
        "CAGR [%]": 100 * cagr,
        "Vol (ann.) [%]": 100 * vol,
        "Sharpe": sharpe,
        "Sharpe SE": se,
        "Sharpe (invested days)": sharpe_invested,
        "Max DD [%]": 100 * drawdown.min(),
        "Days": n,
    }


def summarise(stats, label: str) -> dict:
    """One row combining backtesting.py's stats with the hand-computed ones."""
    equity = stats["_equity_curve"]["Equity"]
    row = {
        "label": label,
        "Return (Ann.) [%]": stats["Return (Ann.) [%]"],
        "Sharpe (library)": stats["Sharpe Ratio"],
    }
    row.update(performance_stats(equity))
    row["Max DD [%]"] = stats["Max. Drawdown [%]"]
    row["Exposure [%]"] = stats["Exposure Time [%]"]
    row["# Trades"] = stats["# Trades"]
    return row


# --------------------------------------------------------------------------- #
# Cost sensitivity
# --------------------------------------------------------------------------- #

def cost_sensitivity(
    data: pd.DataFrame,
    variant: str = "base",
    costs_bps=DEFAULT_COSTS_BPS,
    **overrides,
) -> pd.DataFrame:
    """Rerun the strategy across cost levels.

    The article assumes one basis point of slippage; the original replication
    assumed none. With a few hundred short trades, the cost level is not a
    detail; this table shows where the edge stops existing.
    """
    rows = []
    for bps in costs_bps:
        stats = run_backtest(data, variant=variant, cost_bps=bps, **overrides)
        row = summarise(stats, f"{bps:g} bps")
        row["cost_bps"] = bps
        rows.append(row)

    table = pd.DataFrame(rows).set_index("label")
    # "Sharpe" is annualised over every calendar day; "Sharpe (invested days)"
    # annualises only over days the strategy holds a position. The article's
    # headline Sharpe uses the invested-days convention, so it is shown here
    # next to the honest all-calendar-day figure rather than instead of it.
    cols = [
        "CAGR [%]",
        "Vol (ann.) [%]",
        "Sharpe",
        "Sharpe (invested days)",
        "Sharpe SE",
        "Sharpe (library)",
        "Max DD [%]",
        "Exposure [%]",
        "# Trades",
    ]
    return table[cols]


def breakeven_cost(table: pd.DataFrame) -> float | None:
    """Lowest tested cost level at which the Sharpe goes non-positive."""
    failing = [float(label.split()[0]) for label, sharpe in table["Sharpe"].items()
               if sharpe <= 0]
    return min(failing) if failing else None


# --------------------------------------------------------------------------- #
# Out-of-sample split
# --------------------------------------------------------------------------- #

def _window_metrics(stats, data: pd.DataFrame, label: str, start, end) -> dict:
    """Metrics over one window [start, end] of a single continuous backtest.

    Return-based figures come from the equity-curve slice; exposure and trade
    count from the trades whose entry falls inside the window. Because the whole
    history is run once, indicators are warmed up only at the very start, so no
    window pays its own warm-up.
    """
    eq = stats["_equity_curve"]["Equity"]
    eq_win = eq[(eq.index >= start) & (eq.index <= end)]
    row = {"label": label}
    row.update(performance_stats(eq_win))
    if "CAGR [%]" not in row:  # window too short to measure
        return row

    idx = data.index
    lo = int(idx.searchsorted(start, "left"))
    hi = int(idx.searchsorted(end, "right")) - 1
    win_bars = max(hi - lo + 1, 1)
    last_bar = len(idx) - 1

    n_trades = 0
    in_position = 0
    for eb, xb in zip(stats["_trades"]["EntryBar"], stats["_trades"]["ExitBar"]):
        eb = int(eb)
        xb = last_bar if pd.isna(xb) else int(xb)
        if lo <= eb <= hi:
            n_trades += 1
        a, b = max(eb, lo), min(xb, hi)  # overlap of the trade with the window
        if b >= a:
            in_position += b - a + 1

    row["Exposure [%]"] = 100 * in_position / win_bars
    row["# Trades"] = n_trades
    return row


def oos_split(
    data: pd.DataFrame,
    variant: str = "base",
    oos_start: str = "2019-01-01",
    cost_bps: float = 5.0,
    **overrides,
) -> pd.DataFrame:
    """Compare in-sample and out-of-sample performance.

    The article's parameters (300-day SMA above all) were chosen by trying a
    handful of values over the whole history, so the headline numbers are
    in-sample by construction. Splitting does not undo that, but it shows how
    the strategy behaved on a period the parameters were not tuned on.

    Subtlety that matters for the SMA-based variants: the whole history is
    backtested *once* and each period is read off as a slice of that single run.
    Cutting a fresh out-of-sample slice and backtesting it in isolation would
    instead spend its first ~300 bars (over a year) warming the SMA up -- the
    strategy would not trade until mid-2020 and its drawdown would look
    artificially shallow. One continuous run warms the indicators up once, at
    the start of history, so no window silently loses its warm-up.
    """
    cutoff = pd.Timestamp(oos_start)
    stats = run_backtest(data, variant=variant, cost_bps=cost_bps, **overrides)
    start, end = data.index[0], data.index[-1]
    day = pd.Timedelta(days=1)

    rows = [
        _window_metrics(stats, data, "full", start, end),
        _window_metrics(stats, data, f"in-sample (<{oos_start})", start, cutoff - day),
        _window_metrics(stats, data, f"out-of-sample (>={oos_start})", cutoff, end),
    ]
    rows = [r for r in rows if "CAGR [%]" in r]  # drop windows too short to measure

    table = pd.DataFrame(rows).set_index("label")
    # "Sharpe (invested days)" reproduces the article's convention; "Sharpe"
    # is annualised over every calendar day. Both are shown, neither replaces
    # the other -- the gap between them is the point of the comparison.
    return table[
        ["CAGR [%]", "Sharpe", "Sharpe (invested days)", "Sharpe SE",
         "Max DD [%]", "Exposure [%]", "# Trades"]
    ]


# --------------------------------------------------------------------------- #
# Parameter sensitivity
# --------------------------------------------------------------------------- #

def parameter_sensitivity(
    data: pd.DataFrame,
    variant: str = "base",
    grid: dict | None = None,
    cost_bps: float = 0.0,
) -> pd.DataFrame:
    """One-at-a-time (OAT) sensitivity of the strategy to each of its parameters.

    Every parameter is swept across its grid while the others stay at the
    article's defaults. This is the honest counterpart to a headline number: the
    article picked these values by trying "4-5 options" over the whole history,
    so the question is not whether they are optimal but whether the edge depends
    on them. A robust rule sits on a plateau; an overfit one on a spike.

    The strategy code is untouched -- values are passed as run parameters, the
    same mechanism backtesting.py uses to set any strategy attribute.
    """
    grid = grid or DEFAULT_PARAM_GRID
    defaults = {p: getattr(RollingIBS, p) for p in grid}
    uses_sma = VARIANTS[variant]["regime_filter"] or VARIANTS[variant]["dynamic_stop"]

    rows = []
    for param, values in grid.items():
        if param == "sma_window" and not uses_sma:
            continue  # base does not use the SMA; sweeping it would be noise
        for value in values:
            stats = run_backtest(data, variant=variant, cost_bps=cost_bps, **{param: value})
            perf = performance_stats(stats["_equity_curve"]["Equity"])
            rows.append({
                "param": param,
                "value": value,
                "default": value == defaults[param],
                "Sharpe (invested days)": perf["Sharpe (invested days)"],
                "Sharpe": perf["Sharpe"],
                "CAGR [%]": perf["CAGR [%]"],
                "Max DD [%]": stats["Max. Drawdown [%]"],
                "Exposure [%]": stats["Exposure Time [%]"],
                "# Trades": stats["# Trades"],
            })
    return pd.DataFrame(rows).set_index(["param", "value"])


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _print(title: str, table: pd.DataFrame) -> None:
    print(f"\n{title}\n{'-' * len(title)}")
    print(table.round(2).to_string())


def save_equity_plot(stats, variant: str, out_dir, cost_bps: float = 0.0):
    """Save a two-panel equity + drawdown figure as a committable PNG.

    backtesting.py only emits an interactive HTML plot, which is gitignored and
    does not render in a README. This is a plain matplotlib rendering of results
    that are already computed -- it does not touch the strategy. matplotlib is
    imported lazily so table-only runs do not need it.
    """
    import matplotlib
    matplotlib.use("Agg")  # headless: write a file, never open a window
    import matplotlib.pyplot as plt

    equity = stats["_equity_curve"]["Equity"]
    drawdown = equity / equity.cummax() - 1
    perf = performance_stats(equity)

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(10, 6), sharex=True,
        gridspec_kw={"height_ratios": [3, 1]},
    )
    ax1.plot(equity.index, equity.values, color="#1f77b4", lw=1.1)
    ax1.set_yscale("log")
    ax1.set_ylabel("Equity ($, log scale)")
    ax1.set_title(f"QQQ mean reversion: {variant} variant ({cost_bps:g} bps/side)")
    ax1.grid(True, which="both", alpha=0.2)

    box = (
        f"CAGR              {perf['CAGR [%]']:.1f}%\n"
        f"Sharpe (invested) {perf['Sharpe (invested days)']:.2f}\n"
        f"Sharpe (all days) {perf['Sharpe']:.2f}\n"
        f"Max drawdown      {perf['Max DD [%]']:.1f}%\n"
        f"Exposure          {stats['Exposure Time [%]']:.1f}%"
    )
    ax1.text(
        0.012, 0.97, box, transform=ax1.transAxes, va="top", ha="left",
        fontsize=8.5, family="monospace",
        bbox=dict(boxstyle="round", fc="white", ec="0.7", alpha=0.9),
    )

    ax2.fill_between(drawdown.index, drawdown.values * 100, 0, color="#d62728", alpha=0.4)
    ax2.set_ylabel("Drawdown (%)")
    ax2.grid(True, alpha=0.2)

    fig.tight_layout()
    path = Path(out_dir) / f"equity_{variant}.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def save_sensitivity_plot(table: pd.DataFrame, variant: str, out_dir):
    """Small-multiples plot of the parameter sweep: invested-day Sharpe against
    each parameter, with the article's default marked in red. A flat line means
    the edge does not hinge on that value; a peak at the default is a warning."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    params = list(dict.fromkeys(table.index.get_level_values("param")))
    ncols = 3
    nrows = -(-len(params) // ncols)  # ceil
    fig, axes = plt.subplots(nrows, ncols, figsize=(4.2 * ncols, 3.1 * nrows))
    axes = axes.flatten()

    for ax, param in zip(axes, params):
        sub = table.xs(param, level="param")
        ax.plot(sub.index, sub["Sharpe (invested days)"], "-o", color="#1f77b4", ms=4)
        default = sub[sub["default"]]
        ax.plot(default.index, default["Sharpe (invested days)"], "o",
                color="#d62728", ms=9, label="article default")
        ax.set_title(param)
        ax.set_xlabel("value")
        ax.set_ylabel("Sharpe (inv.)")
        ax.grid(alpha=0.2)
        ax.legend(fontsize=7, loc="best")

    for ax in axes[len(params):]:
        ax.axis("off")

    fig.suptitle(f"Parameter sensitivity: {variant} variant (invested-day Sharpe)")
    fig.tight_layout()
    path = Path(out_dir) / f"param_sensitivity_{variant}.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--variant", default="base", choices=list(VARIANTS))
    parser.add_argument("--all-variants", action="store_true")
    parser.add_argument("--symbol", default=SYMBOL)
    parser.add_argument("--start", default=START)
    parser.add_argument("--end", default=END)
    parser.add_argument("--costs", default=",".join(str(c) for c in DEFAULT_COSTS_BPS),
                        help="cost levels in bps per side, comma separated")
    parser.add_argument("--oos-start", default="2019-01-01")
    parser.add_argument("--headline-cost", type=float, default=5.0,
                        help="cost level used for the out-of-sample table")
    parser.add_argument("--refresh", action="store_true", help="re-download prices")
    parser.add_argument("--sensitivity", action="store_true",
                        help="sweep each strategy parameter around its default")
    parser.add_argument("--plot", action="store_true")
    parser.add_argument("--out", default="Results", help="directory for CSV output")
    args = parser.parse_args()

    costs = [float(c) for c in args.costs.split(",")]
    data = load_data(args.symbol, args.start, args.end, refresh=args.refresh)
    print(f"{args.symbol}: {len(data)} bars, {data.index[0].date()} to {data.index[-1].date()}")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    variants = list(VARIANTS) if args.all_variants else [args.variant]
    for variant in variants:
        table = cost_sensitivity(data, variant=variant, costs_bps=costs)
        _print(f"[{variant}] cost sensitivity", table)
        table.to_csv(out_dir / f"cost_sensitivity_{variant}.csv")

        cutoff = breakeven_cost(table)
        if cutoff is not None:
            print(f"Sharpe turns non-positive at {cutoff:g} bps per side.")

        split = oos_split(
            data, variant=variant, oos_start=args.oos_start, cost_bps=args.headline_cost
        )
        _print(f"[{variant}] in-sample vs out-of-sample @ {args.headline_cost:g} bps", split)
        split.to_csv(out_dir / f"oos_split_{variant}.csv")

        if args.sensitivity:
            sens = parameter_sensitivity(data, variant=variant)
            _print(f"[{variant}] parameter sensitivity (0 bps)", sens)
            sens.to_csv(out_dir / f"param_sensitivity_{variant}.csv")
            if args.plot:
                png = save_sensitivity_plot(sens, variant, out_dir)
                print(f"Saved plot {png}")

        if args.plot:
            # Plot the cost-free curve so the shape is comparable to the article;
            # the cost story lives in the sensitivity table above.
            stats0 = run_backtest(data, variant=variant, cost_bps=0.0)
            png = save_equity_plot(stats0, variant, out_dir, cost_bps=0.0)
            print(f"Saved plot {png}")

    print(f"\nCSV output written to {out_dir.resolve()}")


if __name__ == "__main__":
    main()
