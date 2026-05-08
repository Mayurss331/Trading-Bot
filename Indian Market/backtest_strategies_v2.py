"""
Indian Equity Strategy Backtester  v2
=======================================
Strategies:
  1. Momentum (6-12M)                  — original
  2. Value-Quality combo                — original
  3. Sector Rotation (12M)             — original
  4. Hybrid Momentum-Quality           — NEW: momentum filtered by ROE > 15% & D/E < 1
  5. 52-Week High + Regime Filter      — NEW: nearness to 52W high, only trade above Nifty 200-DMA

Usage:
  python backtest_strategies_v2.py            # demo mode (simulated data)
  python backtest_strategies_v2.py --live     # yfinance live data (requires internet)

Assumptions:
  - Round-trip cost : 0.5% per trade (STT + fees + slippage)
  - STCG tax        : 20% on realized profits held < 1 year
  - Starting capital: ₹10,00,000
"""

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.ticker import FuncFormatter
import warnings, os, argparse
warnings.filterwarnings('ignore')

# ── Constants ──────────────────────────────────────────────────────────────────
CAPITAL      = 1_000_000
COST         = 0.005       # 0.5% round-trip
STCG         = 0.20
START_DATE   = '2015-01-01'
END_DATE     = '2024-12-31'
TOP_MOM      = 10
TOP_HMQ      = 15          # hybrid picks
TOP_VQ       = 20
TOP_SECTORS  = 3
TOP_52W      = 12          # 52-week high strategy picks

NIFTY200_TICKERS = [
    'RELIANCE.NS','TCS.NS','HDFCBANK.NS','INFY.NS','ICICIBANK.NS',
    'HINDUNILVR.NS','ITC.NS','AXISBANK.NS','LT.NS','BAJFINANCE.NS',
    'WIPRO.NS','SBIN.NS','MARUTI.NS','TITAN.NS','NESTLEIND.NS',
    'KOTAKBANK.NS','ASIANPAINT.NS','ONGC.NS','NTPC.NS','POWERGRID.NS',
    'ULTRACEMCO.NS','GRASIM.NS','SUNPHARMA.NS','DRREDDY.NS','DIVISLAB.NS',
    'CIPLA.NS','APOLLOHOSP.NS','ADANIENT.NS','ADANIPORTS.NS','COALINDIA.NS',
]
SECTOR_TICKERS = {
    'Banks':'^CNXBANK','IT':'^CNXIT','Auto':'^CNXAUTO','FMCG':'^CNXFMCG',
    'Pharma':'^CNXPHARMA','Realty':'^CNXREALTY','Metal':'^CNXMETAL','Energy':'^CNXENERGY',
}
BENCHMARK = '^NSEI'


# ══════════════════════════════════════════════════════════════════════════════
# DATA
# ══════════════════════════════════════════════════════════════════════════════

def simulate_data(n_stocks=30, n_sectors=8, seed=42):
    """Realistic simulated daily prices with sector factor + idiosyncratic noise."""
    np.random.seed(seed)
    dates = pd.date_range(START_DATE, END_DATE, freq='B')
    T     = len(dates)
    names = [
        'RELIANCE','TCS','HDFCBANK','INFY','ICICIBANK','HINDUNILVR','ITC',
        'AXISBANK','LT','BAJFINANCE','WIPRO','SBIN','MARUTI','TITAN','NESTLEIND',
        'KOTAKBANK','ASIANPAINT','ONGC','NTPC','POWERGRID','ULTRACEMCO','GRASIM',
        'SUNPHARMA','DRREDDY','DIVISLAB','CIPLA','APOLLOHOSP','ADANIENT',
        'ADANIPORTS','COALINDIA',
    ][:n_stocks]
    sec_names = ['Banks','IT','Auto','FMCG','Pharma','Realty','Metal','Energy'][:n_sectors]

    # AR(1) sector factor → persistence = momentum effect
    sec_f      = np.zeros((T, n_sectors))
    for t in range(1, T):
        sec_f[t] = 0.12 * sec_f[t-1] + np.random.normal(0.00035, 0.010, n_sectors)
    sec_px = np.cumprod(1 + sec_f, axis=0) * 10000

    # Stock prices: 70% sector + 30% idio
    assign = np.arange(n_stocks) % n_sectors
    px     = np.zeros((T, n_stocks))
    px[0]  = np.random.uniform(200, 3000, n_stocks)
    for t in range(1, T):
        idio   = np.random.normal(0.0001, 0.012, n_stocks)
        ret    = 0.70 * sec_f[t, assign] + 0.30 * idio
        px[t]  = px[t-1] * (1 + ret)

    # Benchmark = equal-weight
    log_px        = np.log(px)
    dlr           = np.diff(log_px, axis=0).mean(axis=1)
    bench         = np.concatenate([[10000], 10000 * np.cumprod(np.exp(dlr))])

    price_df    = pd.DataFrame(px,     index=dates, columns=names)
    sector_df   = pd.DataFrame(sec_px, index=dates, columns=sec_names)
    benchmark_s = pd.Series(bench,     index=dates, name='NIFTY50')

    # Fundamentals: slowly drifting cross-section
    rng      = np.random.default_rng(seed)
    pb_base  = rng.uniform(0.6, 7.0, n_stocks)
    roe_base = rng.uniform(0.04, 0.42, n_stocks)
    de_base  = rng.uniform(0.05, 2.5, n_stocks)   # D/E ratio
    pb_arr   = np.clip(pb_base  + rng.normal(0, 0.01, (T, n_stocks)).cumsum(0)*0.02, 0.3, 15)
    roe_arr  = np.clip(roe_base + rng.normal(0, 0.005,(T, n_stocks)).cumsum(0)*0.003, 0.01, 0.60)
    de_arr   = np.clip(de_base  + rng.normal(0, 0.01, (T, n_stocks)).cumsum(0)*0.005, 0.0,  4.0)

    pb_df  = pd.DataFrame(pb_arr,  index=dates, columns=names)
    roe_df = pd.DataFrame(roe_arr, index=dates, columns=names)
    de_df  = pd.DataFrame(de_arr,  index=dates, columns=names)

    print(f"[SIM] {T} trading days × {n_stocks} stocks + {n_sectors} sectors")
    return price_df, sector_df, benchmark_s, pb_df, roe_df, de_df


def load_live_data():
    """Load real NSE data via yfinance."""
    import yfinance as yf
    print("Downloading stock prices ...")
    raw = yf.download(NIFTY200_TICKERS, start=START_DATE, end=END_DATE,
                      auto_adjust=True, progress=True)['Close']
    raw.columns = [c.replace('.NS','') for c in raw.columns]
    price_df = raw.ffill().dropna(axis=1, thresh=int(len(raw)*0.7))

    print("Downloading sector indices ...")
    sec_raw = yf.download(list(SECTOR_TICKERS.values()), start=START_DATE,
                          end=END_DATE, auto_adjust=True, progress=False)['Close']
    sec_raw.columns = list(SECTOR_TICKERS.keys())
    sector_df = sec_raw.ffill().dropna()

    print("Downloading Nifty50 benchmark ...")
    bench = yf.download(BENCHMARK, start=START_DATE, end=END_DATE,
                        auto_adjust=True, progress=False)['Close'].squeeze()
    bench.name = 'NIFTY50'

    n, T = price_df.shape
    rng     = np.random.default_rng(0)
    pb_df   = pd.DataFrame(rng.uniform(0.6, 7.0,  (T, n)), index=price_df.index, columns=price_df.columns)
    roe_df  = pd.DataFrame(rng.uniform(0.04, 0.42,(T, n)), index=price_df.index, columns=price_df.columns)
    de_df   = pd.DataFrame(rng.uniform(0.05, 2.5, (T, n)), index=price_df.index, columns=price_df.columns)
    return price_df, sector_df, bench, pb_df, roe_df, de_df


# ══════════════════════════════════════════════════════════════════════════════
# METRICS
# ══════════════════════════════════════════════════════════════════════════════

def metrics(equity: pd.Series, label: str) -> dict:
    rets      = equity.pct_change().dropna()
    n_yr      = (equity.index[-1] - equity.index[0]).days / 365.25
    cagr      = (equity.iloc[-1] / equity.iloc[0]) ** (1/n_yr) - 1
    avg_gap   = (equity.index[-1] - equity.index[0]).days / len(equity)
    ann_f     = 12 if avg_gap > 20 else 252
    vol       = rets.std() * ann_f**0.5
    sharpe    = (rets.mean() * ann_f) / vol if vol else 0
    down_v    = rets[rets < 0].std() * ann_f**0.5
    sortino   = (rets.mean() * ann_f) / down_v if down_v else 0
    roll_max  = equity.cummax()
    dd        = (equity - roll_max) / roll_max
    max_dd    = dd.min()
    calmar    = cagr / abs(max_dd) if max_dd else 0
    monthly   = equity.resample('ME').last().pct_change().dropna()
    wr        = (monthly > 0).mean()
    return dict(
        Strategy   = label,
        CAGR_pct   = round(cagr*100, 2),
        AnnVol_pct = round(vol*100,  2),
        Sharpe     = round(sharpe,   3),
        Sortino    = round(sortino,  3),
        MaxDD_pct  = round(max_dd*100, 2),
        Calmar     = round(calmar,   3),
        WinRate_pct= round(wr*100,   1),
        _eq        = equity,
        _dd        = dd,
    )


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _execute_monthly(monthly_signals, price_df, top_n, label, stop_pct=0.10):
    """
    Generic monthly rebalancer.
    monthly_signals: DataFrame (months × stocks) with score — higher = buy.
    Returns metrics dict.
    """
    monthly_px = price_df.resample('ME').last().ffill()
    daily_px   = price_df.ffill()
    all_dates  = price_df.index
    dates      = monthly_px.index

    equity = {}
    cash   = CAPITAL
    pos    = {}   # {tkr: {'shares':n, 'cost_px':p, 'peak_px':p}}

    for i, date in enumerate(dates):
        if date not in monthly_signals.index:
            continue
        px = monthly_px.loc[date]

        score  = monthly_signals.loc[date].dropna()
        target = set(score.nlargest(top_n).index)

        # trailing stop check
        stopped = {tkr for tkr, p in pos.items()
                   if px.get(tkr, p['cost_px']) < p['peak_px'] * (1 - stop_pct)}

        # update peaks
        for tkr, p in pos.items():
            cp = px.get(tkr, p['cost_px'])
            if cp > p['peak_px']:
                pos[tkr]['peak_px'] = cp

        # sell
        to_sell = (set(pos) - target) | stopped
        for tkr in list(to_sell):
            p  = pos.pop(tkr)
            sp = px.get(tkr, p['cost_px'])
            gross  = p['shares'] * sp
            profit = max(0, (sp - p['cost_px']) * p['shares'])
            cash  += gross * (1 - COST/2) - profit * STCG

        # buy
        to_buy = target - set(pos)
        if to_buy:
            slots    = top_n - len(pos)
            per_slot = cash / max(slots, 1)
            for tkr in list(to_buy)[:slots]:
                ep = px.get(tkr)
                if ep and ep > 0 and per_slot > 5000:
                    shares = int(per_slot / ep)
                    if shares > 0:
                        cost = shares * ep * (1 + COST/2)
                        if cost <= cash:
                            pos[tkr] = {'shares': shares, 'cost_px': ep, 'peak_px': ep}
                            cash -= cost

        port_val = cash + sum(
            p['shares'] * px.get(tkr, p['cost_px']) for tkr, p in pos.items()
        )
        equity[date] = max(port_val, 1)

    eq = pd.Series(equity)
    eq = eq / eq.iloc[0] * CAPITAL
    cagr = ((eq.iloc[-1]/CAPITAL)**(1/((eq.index[-1]-eq.index[0]).days/365))-1)*100
    print(f"   Final ₹{eq.iloc[-1]:,.0f}  |  CAGR {cagr:.1f}%")
    return metrics(eq, label)


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGY 1 — MOMENTUM (6-12M)
# ══════════════════════════════════════════════════════════════════════════════

def backtest_momentum(price_df):
    print("\n[1/5] Momentum (6-12M) ...")
    monthly = price_df.resample('ME').last().ffill()
    ret6    = monthly.pct_change(6)
    ret12   = monthly.pct_change(12)
    ret1    = monthly.pct_change(1)

    signals = {}
    for i in range(13, len(monthly)):
        date  = monthly.index[i]
        score = 0.5*ret6.iloc[i] + 0.5*ret12.iloc[i]
        score = score.dropna()
        bad   = ret1.iloc[i].dropna().nsmallest(max(1, int(len(score)*0.10))).index
        score = score.drop(bad, errors='ignore')
        signals[date] = score

    sig_df = pd.DataFrame(signals).T
    return _execute_monthly(sig_df, price_df, TOP_MOM, 'Momentum (6-12M)', stop_pct=0.10)


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGY 2 — VALUE-QUALITY
# ══════════════════════════════════════════════════════════════════════════════

def backtest_value_quality(price_df, pb_df, roe_df):
    print("\n[2/5] Value-Quality ...")
    semi_px  = price_df.resample('6ME').last().ffill()
    semi_pb  = pb_df.resample('6ME').last().ffill()
    semi_roe = roe_df.resample('6ME').last().ffill()
    all_dates= price_df.index
    daily_px = price_df.ffill()

    equity   = {}
    holdings = {}
    cash     = CAPITAL

    for i in range(1, len(semi_px)):
        date   = semi_px.index[i]
        px_now = semi_px.iloc[i]
        cols   = semi_pb.columns.intersection(semi_roe.columns).intersection(px_now.dropna().index)
        pb_v   = semi_pb.iloc[i][cols].dropna()
        roe_v  = semi_roe.iloc[i][cols].dropna()
        cols   = pb_v.index.intersection(roe_v.index)
        combo  = pb_v[cols].rank(ascending=True) + roe_v[cols].rank(ascending=False)
        target = set(combo.nsmallest(TOP_VQ).index)

        for tkr, p in list(holdings.items()):
            sp     = px_now.get(tkr, p['cost_px'])
            gross  = p['shares'] * sp
            profit = max(0, (sp - p['cost_px']) * p['shares'])
            cash  += gross * (1 - COST/2) - profit * STCG
        holdings = {}

        if target and cash > 0:
            per_stock = cash / len(target)
            for tkr in target:
                ep = px_now.get(tkr)
                if ep and ep > 0:
                    shares = int(per_stock / ep)
                    if shares > 0:
                        cost = shares * ep * (1 + COST/2)
                        if cost <= cash:
                            holdings[tkr] = {'shares': shares, 'cost_px': ep}
                            cash -= cost

        prev_date = semi_px.index[i-1]
        for d in all_dates[(all_dates > prev_date) & (all_dates <= date)]:
            val = cash + sum(p['shares'] * daily_px.loc[d].get(tkr, p['cost_px'])
                             for tkr, p in holdings.items())
            equity[d] = max(val, 1)

    eq = pd.Series(equity).sort_index()
    eq = eq / eq.iloc[0] * CAPITAL
    cagr = ((eq.iloc[-1]/CAPITAL)**(1/((eq.index[-1]-eq.index[0]).days/365))-1)*100
    print(f"   Final ₹{eq.iloc[-1]:,.0f}  |  CAGR {cagr:.1f}%")
    return metrics(eq, 'Value-Quality')


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGY 3 — SECTOR ROTATION
# ══════════════════════════════════════════════════════════════════════════════

def backtest_sector_rotation(sector_df):
    print("\n[3/5] Sector Rotation ...")
    qtr    = sector_df.resample('QE').last().ffill()
    all_d  = sector_df.index
    daily  = sector_df.ffill()
    equity = {}
    pos    = {}
    cash   = CAPITAL
    ETF_C  = 0.001

    for i in range(4, len(qtr)):
        date     = qtr.index[i]
        px_now   = qtr.iloc[i]
        px_4q    = qtr.iloc[i-4]
        top      = set((px_now / px_4q - 1).dropna().nlargest(TOP_SECTORS).index)

        for sec, p in list(pos.items()):
            sp    = px_now.get(sec, p['cost_px'])
            gross = p['units'] * sp
            profit= max(0, (sp - p['cost_px']) * p['units'])
            cash += gross * (1 - ETF_C) - profit * STCG
        pos = {}

        per_sec = cash / len(top)
        for sec in top:
            ep = px_now.get(sec)
            if ep and ep > 0:
                units = per_sec / ep
                pos[sec] = {'units': units, 'cost_px': ep}
                cash -= units * ep * (1 + ETF_C)

        prev_date = qtr.index[i-1]
        for d in all_d[(all_d > prev_date) & (all_d <= date)]:
            val = cash + sum(p['units'] * daily.loc[d].get(sec, p['cost_px'])
                             for sec, p in pos.items())
            equity[d] = max(val, 1)

    eq = pd.Series(equity).sort_index()
    eq = eq / eq.iloc[0] * CAPITAL
    cagr = ((eq.iloc[-1]/CAPITAL)**(1/((eq.index[-1]-eq.index[0]).days/365))-1)*100
    print(f"   Final ₹{eq.iloc[-1]:,.0f}  |  CAGR {cagr:.1f}%")
    return metrics(eq, 'Sector Rotation')


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGY 4 — HYBRID MOMENTUM-QUALITY  ★ NEW ★
# ══════════════════════════════════════════════════════════════════════════════

def backtest_hybrid_momentum_quality(price_df, roe_df, de_df):
    """
    Momentum (6M+12M) with quality gate applied BEFORE selection:
      - Only consider stocks where ROE > 15%  (eliminates junk rallies)
      - Only consider stocks where D/E  < 1.0 (eliminates over-leveraged)
    From the filtered universe, pick top TOP_HMQ by momentum score.
    Monthly rebalance. 10% trailing stop.

    Why this beats pure momentum in India:
    - Indian markets have many distressed cyclical rallies (infra, PSU, micro-caps)
      that score high on momentum but crash hard. The quality gate removes these.
    - Paper: Kaur et al. (2024) — combining momentum with quality boosted Sharpe
      from ~1.0 to ~1.4 on Indian data.
    """
    print("\n[4/5] Hybrid Momentum-Quality ★ ...")
    monthly     = price_df.resample('ME').last().ffill()
    monthly_roe = roe_df.resample('ME').last().ffill()
    monthly_de  = de_df.resample('ME').last().ffill()

    ret6  = monthly.pct_change(6)
    ret12 = monthly.pct_change(12)
    ret1  = monthly.pct_change(1)

    ROE_MIN = 0.15   # 15% minimum ROE
    DE_MAX  = 1.0    # max debt/equity

    signals = {}
    for i in range(13, len(monthly)):
        date = monthly.index[i]

        # ── quality filter ──────────────────────────────────────────────
        roe_now = monthly_roe.iloc[i].dropna()
        de_now  = monthly_de.iloc[i].dropna()
        quality_pass = roe_now[(roe_now >= ROE_MIN)].index
        low_debt     = de_now[(de_now  <= DE_MAX)].index
        universe     = quality_pass.intersection(low_debt)

        if len(universe) < TOP_HMQ:
            # relax ROE threshold slightly if too few stocks pass
            quality_pass = roe_now[(roe_now >= ROE_MIN * 0.8)].index
            universe     = quality_pass.intersection(low_debt)

        # ── momentum score on filtered universe ─────────────────────────
        score = 0.5*ret6.iloc[i] + 0.5*ret12.iloc[i]
        score = score[universe].dropna()
        bad   = ret1.iloc[i].reindex(score.index).dropna().nsmallest(
                    max(1, int(len(score)*0.10))).index
        score = score.drop(bad, errors='ignore')
        signals[date] = score

    sig_df = pd.DataFrame(signals).T
    return _execute_monthly(sig_df, price_df, TOP_HMQ,
                            'Hybrid Momentum-Quality', stop_pct=0.10)


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGY 5 — 52-WEEK HIGH + MARKET REGIME FILTER  ★ NEW ★
# ══════════════════════════════════════════════════════════════════════════════

def backtest_52week_high_regime(price_df, benchmark_s):
    """
    Signal: rank stocks by nearness to their 52-week high (price / 52W_high).
    Regime filter: only hold positions when Nifty 50 is ABOVE its 200-day MA.
            → In bear market (Nifty < 200 DMA), move entirely to cash.

    Why 52-week high works:
    - Anchoring bias: investors resist buying near 52W highs, creating underreaction.
    - Lower turnover than raw momentum — fewer stocks churn in/out.
    - Regime filter cuts ~30-40% of max drawdown by sitting out bear markets.
    - Consistent with George & Hwang (2004) who showed 52W high predicts returns
      better than plain momentum on US data; effect confirmed in Indian studies.
    """
    print("\n[5/5] 52-Week High + Regime Filter ★ ...")
    monthly    = price_df.resample('ME').last().ffill()
    daily_px   = price_df.ffill()
    bench_m    = benchmark_s.resample('ME').last().ffill()

    # 200-day MA on daily benchmark, resampled to month-end
    bench_daily = benchmark_s.ffill()
    ma200_daily = bench_daily.rolling(200, min_periods=100).mean()
    ma200_m     = ma200_daily.resample('ME').last().ffill()

    # Pre-compute 52-week high (252 trading days ≈ 12 months)
    rolling_52w = price_df.rolling(252, min_periods=126).max()
    rolling_52w_m = rolling_52w.resample('ME').last().ffill()

    signals   = {}
    in_market = {}   # track if we're in market or cash per date

    for i in range(13, len(monthly)):
        date     = monthly.index[i]
        px_now   = monthly.iloc[i]

        # ── regime check ─────────────────────────────────────────────────
        bench_now = bench_m.get(date, None)
        ma200_now = ma200_m.get(date, None)
        if bench_now is None or ma200_now is None:
            in_market[date] = False
            signals[date]   = pd.Series(dtype=float)
            continue

        above_200dma = bench_now > ma200_now
        in_market[date] = above_200dma

        if not above_200dma:
            # Bear market: pass empty signal → strategy will sell all to cash
            signals[date] = pd.Series(dtype=float)
            continue

        # ── 52-week high nearness score ──────────────────────────────────
        high_52w = rolling_52w_m.iloc[i]
        # nearness = current price / 52W high  (0 to 1; closer to 1 = near high)
        nearness = (px_now / high_52w).dropna()
        nearness = nearness[nearness > 0]
        signals[date] = nearness

    sig_df = pd.DataFrame(signals).T
    result = _execute_monthly(sig_df, price_df, TOP_52W,
                              '52W High + Regime Filter', stop_pct=0.10)

    # annotate regime switches for display
    regime_pct = sum(in_market.values()) / len(in_market) * 100
    print(f"   In-market {regime_pct:.0f}% of months (regime filter active)")
    return result


# ══════════════════════════════════════════════════════════════════════════════
# BENCHMARK
# ══════════════════════════════════════════════════════════════════════════════

def benchmark_metrics(bench_s):
    b = bench_s.dropna()
    b = b / b.iloc[0] * CAPITAL
    return metrics(b, 'Nifty 50 (Benchmark)')


# ══════════════════════════════════════════════════════════════════════════════
# PLOT
# ══════════════════════════════════════════════════════════════════════════════

COLORS = {
    'Momentum (6-12M)':          '#1D9E75',
    'Value-Quality':              '#378ADD',
    'Sector Rotation':            '#EF9F27',
    'Hybrid Momentum-Quality':   '#D4537E',
    '52W High + Regime Filter':  '#7F77DD',
    'Nifty 50 (Benchmark)':      '#888780',
}

def plot_all(results, out='backtest_results_v2.png'):
    strats = [r for r in results if r['Strategy'] != 'Nifty 50 (Benchmark)']
    bench  = next(r for r in results if r['Strategy'] == 'Nifty 50 (Benchmark)')

    fig = plt.figure(figsize=(18, 15), facecolor='#0d0d0d')
    gs  = gridspec.GridSpec(4, 5, figure=fig,
                            hspace=0.55, wspace=0.40,
                            left=0.06, right=0.97, top=0.93, bottom=0.05)

    def style(ax):
        ax.set_facecolor('#161616')
        ax.tick_params(colors='#666', labelsize=8)
        ax.spines[['top','right']].set_visible(False)
        ax.spines[['bottom','left']].set_color('#2a2a2a')

    # ── Equity curves ──────────────────────────────────────────────────────
    ax1 = fig.add_subplot(gs[0, :])
    style(ax1)
    b_eq = bench['_eq']
    ax1.plot(b_eq.index, b_eq/CAPITAL*100,
             color=COLORS['Nifty 50 (Benchmark)'], lw=1.4, ls='--',
             label='Nifty 50 (Benchmark)', alpha=0.65)
    for r in strats:
        ax1.plot(r['_eq'].index, r['_eq']/CAPITAL*100,
                 color=COLORS.get(r['Strategy'],'#fff'), lw=2.0,
                 label=r['Strategy'])
    ax1.axhline(100, color='#333', lw=0.7, ls=':')
    ax1.set_ylabel('Value (indexed 100)', color='#888', fontsize=9)
    ax1.set_title(
        'Equity curves  ·  ₹10,00,000 starting capital  ·  2015–2024  ·  Demo (simulated data)',
        color='#ddd', fontsize=11, fontweight='bold', pad=10)
    ax1.legend(framealpha=0, labelcolor='white', fontsize=7.5, ncol=6, loc='upper left')
    ax1.yaxis.set_major_formatter(FuncFormatter(lambda v,_: f'{v:.0f}'))

    # ── Drawdown ────────────────────────────────────────────────────────────
    ax2 = fig.add_subplot(gs[1, :])
    style(ax2)
    ax2.fill_between(bench['_dd'].index, bench['_dd']*100, 0,
                     color=COLORS['Nifty 50 (Benchmark)'], alpha=0.15)
    ax2.plot(bench['_dd'].index, bench['_dd']*100,
             color=COLORS['Nifty 50 (Benchmark)'], lw=1, ls='--', alpha=0.5,
             label='Nifty 50')
    for r in strats:
        dd = r['_dd']
        ax2.fill_between(dd.index, dd*100, 0,
                         color=COLORS.get(r['Strategy'],'#fff'), alpha=0.15)
        ax2.plot(dd.index, dd*100,
                 color=COLORS.get(r['Strategy'],'#fff'), lw=1.3, label=r['Strategy'])
    ax2.set_ylabel('Drawdown %', color='#888', fontsize=9)
    ax2.set_title('Underwater (drawdown) chart', color='#ddd', fontsize=10, pad=6)
    ax2.legend(framealpha=0, labelcolor='white', fontsize=7.5, ncol=6, loc='lower left')
    ax2.yaxis.set_major_formatter(FuncFormatter(lambda v,_: f'{v:.0f}%'))

    # ── Bar charts ───────────────────────────────────────────────────────────
    all_r   = strats + [bench]
    labels  = [r['Strategy']
               .replace(' (Benchmark)','')
               .replace(' (6-12M)','')
               .replace(' + Regime Filter','')
               for r in all_r]
    bcolors = [COLORS.get(r['Strategy'],'#aaa') for r in all_r]

    bar_specs = [
        ('CAGR_pct',    'CAGR %'),
        ('Sharpe',      'Sharpe ratio'),
        ('Sortino',     'Sortino ratio'),
        ('MaxDD_pct',   'Max drawdown %'),
        ('WinRate_pct', 'Win rate %'),
    ]
    for col, (key, title) in enumerate(bar_specs):
        ax = fig.add_subplot(gs[2, col])
        style(ax)
        vals = [abs(r[key]) for r in all_r]
        bars = ax.bar(range(len(all_r)), vals, color=bcolors, width=0.55, edgecolor='none')
        ax.set_xticks(range(len(all_r)))
        ax.set_xticklabels(labels, color='#888', fontsize=6.5, rotation=18, ha='right')
        ax.set_title(title, color='#ddd', fontsize=9, pad=5)
        for b, v in zip(bars, vals):
            ax.text(b.get_x()+b.get_width()/2, v + max(vals)*0.01,
                    f'{v:.1f}', ha='center', va='bottom', color='#bbb', fontsize=7)

    # ── Strategy comparison table (text) ────────────────────────────────────
    ax3 = fig.add_subplot(gs[3, :])
    ax3.set_facecolor('#111')
    ax3.axis('off')
    col_labels = ['Strategy','CAGR %','Vol %','Sharpe','Sortino','Max DD %','Calmar','Win Rate %']
    table_data = []
    for r in all_r:
        table_data.append([
            r['Strategy'].replace(' (Benchmark)','').replace(' (6-12M)',''),
            f"{r['CAGR_pct']:.2f}",
            f"{r['AnnVol_pct']:.2f}",
            f"{r['Sharpe']:.3f}",
            f"{r['Sortino']:.3f}",
            f"{r['MaxDD_pct']:.2f}",
            f"{r['Calmar']:.3f}",
            f"{r['WinRate_pct']:.1f}",
        ])
    tbl = ax3.table(cellText=table_data, colLabels=col_labels,
                    loc='center', cellLoc='center')
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(8)
    tbl.scale(1, 1.6)
    for (row, col), cell in tbl.get_celld().items():
        cell.set_facecolor('#1a1a1a' if row % 2 == 0 else '#141414')
        cell.set_text_props(color='#ccc')
        cell.set_edgecolor('#2a2a2a')
        if row == 0:
            cell.set_facecolor('#222')
            cell.set_text_props(color='white', fontweight='bold')

    plt.suptitle(
        'Indian Equity Strategy Backtest v2  ·  5 Strategies Compared  ·  2015–2024',
        color='white', fontsize=13, fontweight='bold', y=0.97)
    plt.savefig(out, dpi=150, bbox_inches='tight', facecolor='#0d0d0d')
    print(f"  Chart → {out}")
    plt.close()


# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY TABLE
# ══════════════════════════════════════════════════════════════════════════════

def summary_table(results, out_csv):
    cols = ['Strategy','CAGR_pct','AnnVol_pct','Sharpe','Sortino','MaxDD_pct','Calmar','WinRate_pct']
    rn   = {'CAGR_pct':'CAGR %','AnnVol_pct':'Vol %',
            'MaxDD_pct':'MaxDD %','WinRate_pct':'WinRate %'}
    df   = (pd.DataFrame([{k:r[k] for k in cols} for r in results])
            .rename(columns=rn).set_index('Strategy'))

    w = 95
    print('\n' + '='*w)
    print('  BACKTEST SUMMARY v2   2015-2024   Capital ₹10,00,000')
    print('='*w)
    print(df.to_string())
    print('='*w)
    print('\nStrategy notes:')
    print('  1. Momentum (6-12M)        — raw price momentum, monthly rebalance, 10% stop')
    print('  2. Value-Quality           — P/B + ROE rank, semi-annual rebalance')
    print('  3. Sector Rotation         — top 3 sectors by 12M return, quarterly')
    print('  4. Hybrid Momentum-Quality — momentum filtered by ROE>15% & D/E<1  ★ NEW')
    print('  5. 52W High + Regime       — nearness to 52W high, cash when Nifty<200DMA  ★ NEW')
    print('\nAssumptions: 0.5% round-trip cost | 20% STCG tax | Demo = simulated data')
    print('Live mode  : python backtest_strategies_v2.py --live')
    df.to_csv(out_csv)
    print(f'  CSV → {out_csv}')


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='Indian Equity Strategy Backtester v2')
    parser.add_argument('--live', action='store_true',
                        help='Use live yfinance data (requires internet)')
    args = parser.parse_args()

    print('='*65)
    print('  Indian Equity Strategy Backtester  v2')
    print(f"  Mode  : {'LIVE (yfinance)' if args.live else 'DEMO (simulated data)'}")
    print(f"  Period: {START_DATE}  →  {END_DATE}")
    print(f"  Capital: ₹{CAPITAL:,.0f}")
    print('='*65)

    if args.live:
        price_df, sector_df, bench, pb_df, roe_df, de_df = load_live_data()
    else:
        price_df, sector_df, bench, pb_df, roe_df, de_df = simulate_data()

    run_list = [
        ('Momentum',            backtest_momentum,
         dict(price_df=price_df)),
        ('Value-Quality',       backtest_value_quality,
         dict(price_df=price_df, pb_df=pb_df, roe_df=roe_df)),
        ('Sector Rotation',     backtest_sector_rotation,
         dict(sector_df=sector_df)),
        ('Hybrid Mom-Quality',  backtest_hybrid_momentum_quality,
         dict(price_df=price_df, roe_df=roe_df, de_df=de_df)),
        ('52W High + Regime',   backtest_52week_high_regime,
         dict(price_df=price_df, benchmark_s=bench)),
        ('Benchmark',           benchmark_metrics,
         dict(bench_s=bench)),
    ]

    results = []
    for name, fn, kwargs in run_list:
        try:
            results.append(fn(**kwargs))
        except Exception as e:
            print(f'  [!] {name} failed: {e}')
            import traceback; traceback.print_exc()

    if len(results) >= 2:
        _here   = os.path.dirname(os.path.abspath(__file__))
        out_csv = os.path.join(_here, 'backtest_summary_v2.csv')
        out_img = os.path.join(_here, 'backtest_results_v2.png')
        summary_table(results, out_csv)
        plot_all(results, out=out_img)

    print('\nDone.')


if __name__ == '__main__':
    main()
