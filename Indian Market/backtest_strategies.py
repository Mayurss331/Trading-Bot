"""
Indian Equity Strategy Backtester
===================================
Implements all 3 top strategies from the research report:
  1. Momentum (6-12M price-relative, monthly rebalance)
  2. Value-Quality combo (P/B + ROE, semi-annual rebalance)
  3. Sector Rotation (12M momentum, quarterly rebalance)

Usage:
  python backtest_strategies.py               # demo mode (simulated data)
  python backtest_strategies.py --live        # yfinance live data

Assumptions:
  - Round-trip cost: 0.5% per trade (STT + fees + slippage)
  - STCG tax: 20% on profits held < 1 year
  - Starting capital: INR 10,00,000
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

# ── Constants ─────────────────────────────────────────────────────────────────
CAPITAL         = 1_000_000
COST            = 0.005          # 0.5% round-trip
STCG            = 0.20
START_DATE      = '2015-01-01'
END_DATE        = '2024-12-31'
TOP_MOM         = 10
TOP_VQ          = 20
TOP_SECTORS     = 3


# ── Simulate data ─────────────────────────────────────────────────────────────
def simulate_data(n_stocks=30, n_sectors=8, seed=42):
    np.random.seed(seed)
    dates = pd.date_range(START_DATE, END_DATE, freq='B')
    T = len(dates)
    names = [
        'RELIANCE','TCS','HDFCBANK','INFY','ICICIBANK','HINDUNILVR','ITC',
        'AXISBANK','LT','BAJFINANCE','WIPRO','SBIN','MARUTI','TITAN','NESTLEIND',
        'KOTAKBANK','ASIANPAINT','ONGC','NTPC','POWERGRID','ULTRACEMCO','GRASIM',
        'SUNPHARMA','DRREDDY','DIVISLAB','CIPLA','APOLLOHOSP','ADANIENT',
        'ADANIPORTS','COALINDIA',
    ][:n_stocks]
    sec_names = ['Banks','IT','Auto','FMCG','Pharma','Realty','Metal','Energy'][:n_sectors]

    # ── sector factor with persistence (AR1) ──────────────────────────────
    sec_f = np.zeros((T, n_sectors))
    sec_f[0] = 0
    for t in range(1, T):
        sec_f[t] = 0.12 * sec_f[t-1] + np.random.normal(0.00035, 0.010, n_sectors)

    sec_px = np.cumprod(1 + sec_f, axis=0) * 10000

    # ── stock prices: 70% sector + 30% idiosyncratic ─────────────────────
    assign = np.arange(n_stocks) % n_sectors
    px = np.zeros((T, n_stocks))
    px[0] = np.random.uniform(200, 3000, n_stocks)
    for t in range(1, T):
        idio = np.random.normal(0.0001, 0.012, n_stocks)
        ret  = 0.70 * sec_f[t, assign] + 0.30 * idio
        px[t] = px[t-1] * (1 + ret)

    # ── benchmark: equal-weight average ──────────────────────────────────
    log_px = np.log(px)
    daily_log_ret = np.diff(log_px, axis=0).mean(axis=1)
    bench = np.concatenate([[10000], 10000 * np.cumprod(np.exp(daily_log_ret))])

    price_df    = pd.DataFrame(px, index=dates, columns=names)
    sector_df   = pd.DataFrame(sec_px, index=dates, columns=sec_names)
    benchmark_s = pd.Series(bench, index=dates, name='NIFTY50')

    # simulated fundamentals (static cross-section, slowly drifting)
    rng = np.random.default_rng(seed)
    pb_base  = rng.uniform(0.6, 7.0, n_stocks)
    roe_base = rng.uniform(0.04, 0.42, n_stocks)
    pb_noise  = rng.normal(0, 0.01, (T, n_stocks))
    roe_noise = rng.normal(0, 0.005, (T, n_stocks))
    pb_arr  = np.clip(pb_base + pb_noise.cumsum(0)*0.02, 0.3, 15)
    roe_arr = np.clip(roe_base + roe_noise.cumsum(0)*0.005, 0.01, 0.60)

    pb_df  = pd.DataFrame(pb_arr,  index=dates, columns=names)
    roe_df = pd.DataFrame(roe_arr, index=dates, columns=names)

    print(f"[SIM] {T} trading days × {n_stocks} stocks + {n_sectors} sectors")
    return price_df, sector_df, benchmark_s, pb_df, roe_df


# ── Metrics ───────────────────────────────────────────────────────────────────
def metrics(equity: pd.Series, label: str) -> dict:
    rets   = equity.pct_change().dropna()
    n_yr   = (equity.index[-1] - equity.index[0]).days / 365.25
    cagr   = (equity.iloc[-1] / equity.iloc[0]) ** (1/n_yr) - 1
    # Detect frequency: if avg gap > 20 days -> monthly series
    avg_gap = (equity.index[-1] - equity.index[0]).days / len(equity)
    ann_factor = 12 if avg_gap > 20 else 252
    vol    = rets.std() * ann_factor**0.5
    sharpe = (rets.mean() * ann_factor) / vol if vol else 0
    down_v = rets[rets < 0].std() * ann_factor**0.5
    sortino= (rets.mean() * ann_factor) / down_v if down_v else 0
    roll_max = equity.cummax()
    dd       = (equity - roll_max) / roll_max
    max_dd   = dd.min()
    calmar   = cagr / abs(max_dd) if max_dd else 0
    monthly  = equity.resample('ME').last().pct_change().dropna()
    wr       = (monthly > 0).mean()
    return dict(
        Strategy=label,
        CAGR_pct   = round(cagr*100, 2),
        AnnVol_pct = round(vol*100, 2),
        Sharpe     = round(sharpe, 3),
        Sortino    = round(sortino, 3),
        MaxDD_pct  = round(max_dd*100, 2),
        Calmar     = round(calmar, 3),
        WinRate_pct= round(wr*100, 1),
        _eq        = equity,
        _dd        = dd,
    )


# ── Strategy 1: Momentum ──────────────────────────────────────────────────────
def backtest_momentum(price_df: pd.DataFrame) -> dict:
    print("\n[1/4] Momentum (6-12M) ...")
    monthly = price_df.resample('ME').last().ffill()
    dates   = monthly.index

    # Build returns lookbacks
    ret6  = monthly.pct_change(6)
    ret12 = monthly.pct_change(12)
    ret1  = monthly.pct_change(1)

    equity = {}
    cash   = CAPITAL
    pos    = {}   # {ticker: {'shares': n, 'cost_px': p}}

    for i in range(13, len(dates)):
        date = dates[i]
        px   = monthly.iloc[i]

        # ── score: 50/50 6M+12M, exclude bottom 10% last-month losers ──
        score = 0.5 * ret6.iloc[i] + 0.5 * ret12.iloc[i]
        score = score.dropna()
        bad   = ret1.iloc[i].dropna().nsmallest(max(1, int(len(score)*0.10))).index
        score = score.drop(bad, errors='ignore')
        target = set(score.nlargest(TOP_MOM).index)

        # ── 10% trailing stop: remove from target if stopped ──
        stopped = set()
        for tkr, p in pos.items():
            cp = px.get(tkr, p['cost_px'])
            if cp < p['cost_px'] * 0.90:
                stopped.add(tkr)

        # ── sell: positions not in target or stopped ──
        to_sell = (set(pos) - target) | stopped
        for tkr in to_sell:
            p  = pos.pop(tkr)
            sp = px.get(tkr, p['cost_px'])
            gross  = p['shares'] * sp
            profit = max(0, (sp - p['cost_px']) * p['shares'])
            cash  += gross * (1 - COST/2) - profit * STCG

        # ── buy: new entries ──
        to_buy = target - set(pos)
        if to_buy:
            slots     = TOP_MOM - len(pos)
            per_slot  = cash / max(slots, 1) if slots > 0 else 0
            for tkr in list(to_buy)[:slots]:
                ep = px.get(tkr)
                if ep and ep > 0 and per_slot > 5000:
                    shares = int(per_slot / ep)
                    if shares > 0:
                        cost = shares * ep * (1 + COST/2)
                        if cost <= cash:
                            pos[tkr] = {'shares': shares, 'cost_px': ep}
                            cash -= cost

        # ── mark to market ──
        port_val = cash + sum(p['shares'] * px.get(tkr, p['cost_px'])
                              for tkr, p in pos.items())
        equity[date] = max(port_val, 1)

    eq = pd.Series(equity)
    eq = eq / eq.iloc[0] * CAPITAL
    print(f"   Final ₹{eq.iloc[-1]:,.0f}  |  CAGR {((eq.iloc[-1]/CAPITAL)**(1/((eq.index[-1]-eq.index[0]).days/365))-1)*100:.1f}%")
    return metrics(eq, 'Momentum (6-12M)')


# ── Strategy 2: Value-Quality ─────────────────────────────────────────────────
def backtest_value_quality(price_df, pb_df, roe_df) -> dict:
    print("\n[2/4] Value-Quality ...")
    semi_px  = price_df.resample('6ME').last().ffill()
    semi_pb  = pb_df.resample('6ME').last().ffill()
    semi_roe = roe_df.resample('6ME').last().ffill()
    all_dates= price_df.index
    daily_px = price_df.ffill()

    equity   = {}
    holdings = {}   # {ticker: {'shares': n, 'cost_px': p}}
    cash     = CAPITAL

    for i in range(1, len(semi_px)):
        date   = semi_px.index[i]
        px_now = semi_px.iloc[i]

        # score
        cols   = semi_pb.columns.intersection(semi_roe.columns).intersection(px_now.dropna().index)
        pb_v   = semi_pb.iloc[i][cols].dropna()
        roe_v  = semi_roe.iloc[i][cols].dropna()
        cols   = pb_v.index.intersection(roe_v.index)
        pb_v, roe_v = pb_v[cols], roe_v[cols]

        # rank: low P/B = value (ascending rank = good), high ROE = quality (desc rank = good)
        vrank  = pb_v.rank(ascending=True)
        qrank  = roe_v.rank(ascending=False)
        combo  = vrank + qrank          # low combined rank = best
        target = set(combo.nsmallest(TOP_VQ).index)

        # sell all old holdings
        for tkr, p in list(holdings.items()):
            sp     = px_now.get(tkr, p['cost_px'])
            gross  = p['shares'] * sp
            profit = max(0, (sp - p['cost_px']) * p['shares'])
            cash  += gross * (1 - COST/2) - profit * STCG
        holdings = {}

        # buy new selection
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

        # daily NAV between rebalances
        prev_date = semi_px.index[i-1]
        period = all_dates[(all_dates > prev_date) & (all_dates <= date)]
        for d in period:
            val = cash + sum(p['shares'] * daily_px.loc[d].get(tkr, p['cost_px'])
                             for tkr, p in holdings.items())
            equity[d] = max(val, 1)

    eq = pd.Series(equity).sort_index()
    eq = eq / eq.iloc[0] * CAPITAL
    print(f"   Final ₹{eq.iloc[-1]:,.0f}  |  CAGR {((eq.iloc[-1]/CAPITAL)**(1/((eq.index[-1]-eq.index[0]).days/365))-1)*100:.1f}%")
    return metrics(eq, 'Value-Quality')


# ── Strategy 3: Sector Rotation ───────────────────────────────────────────────
def backtest_sector_rotation(sector_df: pd.DataFrame) -> dict:
    print("\n[3/4] Sector Rotation ...")
    qtr    = sector_df.resample('QE').last().ffill()
    all_d  = sector_df.index
    daily  = sector_df.ffill()
    equity = {}
    pos    = {}   # {sector: {'units': n, 'cost_px': p}}
    cash   = CAPITAL
    ETF_COST = 0.001  # 0.1% per leg for ETF

    for i in range(4, len(qtr)):
        date     = qtr.index[i]
        px_now   = qtr.iloc[i]
        px_prev4 = qtr.iloc[i-4]

        ret12 = (px_now / px_prev4 - 1).dropna()
        top   = set(ret12.nlargest(TOP_SECTORS).index)

        # sell old
        for sec, p in list(pos.items()):
            sp    = px_now.get(sec, p['cost_px'])
            gross = p['units'] * sp
            profit= max(0, (sp - p['cost_px']) * p['units'])
            cash += gross * (1 - ETF_COST) - profit * STCG
        pos = {}

        # buy top sectors
        per_sec = cash / len(top)
        for sec in top:
            ep = px_now.get(sec)
            if ep and ep > 0:
                units = per_sec / ep
                cost  = units * ep * (1 + ETF_COST)
                if cost <= cash + 1:
                    pos[sec] = {'units': units, 'cost_px': ep}
                    cash -= cost

        # daily NAV
        prev_date = qtr.index[i-1]
        period = all_d[(all_d > prev_date) & (all_d <= date)]
        for d in period:
            val = cash + sum(p['units'] * daily.loc[d].get(sec, p['cost_px'])
                             for sec, p in pos.items())
            equity[d] = max(val, 1)

    eq = pd.Series(equity).sort_index()
    eq = eq / eq.iloc[0] * CAPITAL
    print(f"   Final ₹{eq.iloc[-1]:,.0f}  |  CAGR {((eq.iloc[-1]/CAPITAL)**(1/((eq.index[-1]-eq.index[0]).days/365))-1)*100:.1f}%")
    return metrics(eq, 'Sector Rotation')


# ── Strategy 4: Hybrid ────────────────────────────────────────────────────────
def backtest_hybrid(price_df, sector_df, pb_df, roe_df) -> dict:
    """
    Hybrid strategy combining all three signals:
      • Sector filter  : only stocks inside top-3 sectors (12M sector momentum)
      • Momentum signal: 50/50 blend of 6M and 12M price return (normalised)
      • Value-Quality  : low P/B + high ROE composite rank (normalised)
      • Regime blend   : bull → 55% MOM / 45% VQ | bear → 30% MOM / 70% VQ
      • Risk           : 8% trailing stop, monthly rebalance, top 15 holdings
    """
    print("\n[4/4] Hybrid (Sector-filter + Momentum + Value-Quality) ...")

    TOP_HYBRID  = 15
    TRAIL_STOP  = 0.08

    monthly_px  = price_df.resample('ME').last().ffill()
    monthly_sec = sector_df.resample('ME').last().ffill()
    monthly_pb  = pb_df.resample('ME').last().ffill()
    monthly_roe = roe_df.resample('ME').last().ffill()

    # stock → sector mapping (mirrors simulate_data assignment)
    stk_names = price_df.columns.tolist()
    sec_names = sector_df.columns.tolist()
    n_sec     = len(sec_names)
    stock_sector = {stk_names[i]: sec_names[i % n_sec] for i in range(len(stk_names))}

    ret6      = monthly_px.pct_change(6)
    ret12     = monthly_px.pct_change(12)
    ret1      = monthly_px.pct_change(1)
    sec_ret12 = monthly_sec.pct_change(12)

    equity = {}
    cash   = CAPITAL
    pos    = {}   # {ticker: {'shares', 'cost_px', 'peak_px'}}

    for i in range(13, len(monthly_px)):
        date = monthly_px.index[i]
        px   = monthly_px.iloc[i]

        # ── 1. Market regime: 12M equal-weight benchmark return ───────────
        bench_ret = monthly_px.iloc[i].mean() / monthly_px.iloc[i - 12].mean() - 1
        bull      = bench_ret > 0
        mom_w     = 0.55 if bull else 0.30
        vq_w      = 0.45 if bull else 0.70

        # ── 2. Sector filter: stocks in top-3 sectors by 12M return ───────
        s_ret      = sec_ret12.iloc[i].dropna()
        top_sectors = set(
            s_ret.nlargest(TOP_SECTORS).index if len(s_ret) >= TOP_SECTORS else s_ret.index
        )
        sector_universe = {tkr for tkr, sec in stock_sector.items() if sec in top_sectors}

        # ── 3. Momentum score (normalised to [0, 1]) ───────────────────────
        raw_mom = (0.5 * ret6.iloc[i] + 0.5 * ret12.iloc[i]).dropna()
        bad     = ret1.iloc[i].dropna().nsmallest(max(1, int(len(raw_mom) * 0.10))).index
        raw_mom = raw_mom.drop(bad, errors='ignore')
        rng_mom = raw_mom.max() - raw_mom.min()
        score_mom = (raw_mom - raw_mom.min()) / (rng_mom + 1e-9) if rng_mom > 0 else raw_mom * 0 + 0.5

        # ── 4. Value-Quality score (normalised to [0, 1]) ──────────────────
        cols = (monthly_pb.columns
                .intersection(monthly_roe.columns)
                .intersection(px.dropna().index))
        pb_v  = monthly_pb.iloc[i][cols].dropna()
        roe_v = monthly_roe.iloc[i][cols].dropna()
        cols  = pb_v.index.intersection(roe_v.index)
        if len(cols) > 0:
            vrank    = pb_v[cols].rank(ascending=True)   # low P/B = good
            qrank    = roe_v[cols].rank(ascending=False)  # high ROE = good
            raw_vq   = -(vrank + qrank)                  # negate: higher = better
            rng_vq   = raw_vq.max() - raw_vq.min()
            score_vq = (raw_vq - raw_vq.min()) / (rng_vq + 1e-9) if rng_vq > 0 else raw_vq * 0 + 0.5
        else:
            score_vq = pd.Series(dtype=float)

        # ── 5. Composite score over sector universe ────────────────────────
        universe = score_mom.index.intersection(score_vq.index).intersection(sector_universe)
        if len(universe) < 5:                      # fallback: drop sector filter
            universe = score_mom.index.intersection(score_vq.index)

        if len(universe) > 0:
            combo  = mom_w * score_mom[universe] + vq_w * score_vq[universe]
            target = set(combo.nlargest(TOP_HYBRID).index)
        else:
            target = set()

        # ── 6. Trailing stop ──────────────────────────────────────────────
        stopped = set()
        for tkr, p in pos.items():
            cp = px.get(tkr, p['peak_px'])
            p['peak_px'] = max(p['peak_px'], cp)
            if cp < p['peak_px'] * (1 - TRAIL_STOP):
                stopped.add(tkr)

        # ── 7. Sell ───────────────────────────────────────────────────────
        for tkr in (set(pos) - target) | stopped:
            p      = pos.pop(tkr)
            sp     = px.get(tkr, p['cost_px'])
            gross  = p['shares'] * sp
            profit = max(0, (sp - p['cost_px']) * p['shares'])
            cash  += gross * (1 - COST / 2) - profit * STCG

        # ── 8. Buy ────────────────────────────────────────────────────────
        to_buy = target - set(pos)
        if to_buy:
            slots    = TOP_HYBRID - len(pos)
            per_slot = cash / max(slots, 1) if slots > 0 else 0
            for tkr in list(to_buy)[:slots]:
                ep = px.get(tkr)
                if ep and ep > 0 and per_slot > 5000:
                    shares = int(per_slot / ep)
                    if shares > 0:
                        cost = shares * ep * (1 + COST / 2)
                        if cost <= cash:
                            pos[tkr] = {'shares': shares, 'cost_px': ep, 'peak_px': ep}
                            cash -= cost

        # ── 9. Mark to market ─────────────────────────────────────────────
        port_val = cash + sum(
            p['shares'] * px.get(tkr, p['cost_px']) for tkr, p in pos.items()
        )
        equity[date] = max(port_val, 1)

    eq = pd.Series(equity)
    eq = eq / eq.iloc[0] * CAPITAL
    cagr = ((eq.iloc[-1] / CAPITAL) ** (1 / ((eq.index[-1] - eq.index[0]).days / 365)) - 1) * 100
    print(f"   Final ₹{eq.iloc[-1]:,.0f}  |  CAGR {cagr:.1f}%")
    return metrics(eq, 'Hybrid')


# ── Benchmark ─────────────────────────────────────────────────────────────────
def benchmark_metrics(bench_s: pd.Series) -> dict:
    b = bench_s.dropna()
    b = b / b.iloc[0] * CAPITAL
    return metrics(b, 'Nifty 50 (Benchmark)')


# ── Plot ──────────────────────────────────────────────────────────────────────
def plot_all(results, out='backtest_results.png'):
    COLORS = {
        'Momentum (6-12M)':     '#1D9E75',
        'Value-Quality':        '#378ADD',
        'Sector Rotation':      '#EF9F27',
        'Hybrid':               '#E05CDA',
        'Nifty 50 (Benchmark)': '#888780',
    }
    strats = [r for r in results if r['Strategy'] != 'Nifty 50 (Benchmark)']
    bench  = next(r for r in results if r['Strategy'] == 'Nifty 50 (Benchmark)')

    fig = plt.figure(figsize=(18, 13), facecolor='#0d0d0d')
    gs  = gridspec.GridSpec(3, 5, figure=fig,
                            hspace=0.50, wspace=0.38,
                            left=0.06, right=0.97, top=0.93, bottom=0.06)

    def style(ax):
        ax.set_facecolor('#161616')
        ax.tick_params(colors='#777', labelsize=8)
        ax.spines[['top','right']].set_visible(False)
        ax.spines[['bottom','left']].set_color('#333')

    # ── Equity curves ──────────────────────────────────────────────────────
    ax1 = fig.add_subplot(gs[0, :])
    style(ax1)
    b_eq = bench['_eq']
    ax1.plot(b_eq.index, b_eq/CAPITAL*100, color=COLORS['Nifty 50 (Benchmark)'],
             lw=1.5, ls='--', label='Nifty 50', alpha=0.7)
    for r in strats:
        eq = r['_eq']
        ax1.plot(eq.index, eq/CAPITAL*100, color=COLORS[r['Strategy']],
                 lw=2.0, label=r['Strategy'])
    ax1.axhline(100, color='#444', lw=0.7, ls=':')
    ax1.set_ylabel('Value (indexed 100)', color='#999', fontsize=9)
    ax1.set_title('Portfolio equity curves  ·  ₹10L starting capital  ·  2015–2024  ·  Demo (simulated data)',
                  color='#ddd', fontsize=11, fontweight='bold', pad=10)
    ax1.legend(framealpha=0, labelcolor='white', fontsize=8, ncol=4,
               loc='upper left')
    ax1.yaxis.set_major_formatter(FuncFormatter(lambda v,_: f'{v:.0f}'))

    # ── Drawdown ────────────────────────────────────────────────────────────
    ax2 = fig.add_subplot(gs[1, :])
    style(ax2)
    ax2.fill_between(bench['_dd'].index, bench['_dd']*100, 0,
                     color=COLORS['Nifty 50 (Benchmark)'], alpha=0.2)
    ax2.plot(bench['_dd'].index, bench['_dd']*100,
             color=COLORS['Nifty 50 (Benchmark)'], lw=1, ls='--', alpha=0.6, label='Nifty 50')
    for r in strats:
        dd = r['_dd']
        ax2.fill_between(dd.index, dd*100, 0,
                         color=COLORS[r['Strategy']], alpha=0.18)
        ax2.plot(dd.index, dd*100, color=COLORS[r['Strategy']], lw=1.3, label=r['Strategy'])
    ax2.set_ylabel('Drawdown %', color='#999', fontsize=9)
    ax2.set_title('Underwater (drawdown) chart', color='#ddd', fontsize=10, pad=6)
    ax2.legend(framealpha=0, labelcolor='white', fontsize=8, ncol=4, loc='lower left')
    ax2.yaxis.set_major_formatter(FuncFormatter(lambda v,_: f'{v:.0f}%'))

    # ── Bar metrics ─────────────────────────────────────────────────────────
    all_r   = strats + [bench]
    labels  = [r['Strategy'].replace(' (Benchmark)','').replace(' (6-12M)','') for r in all_r]
    bcolors = [COLORS[r['Strategy']] for r in all_r]
    metric_specs = [
        ('CAGR_pct',    'CAGR %'),
        ('Sharpe',      'Sharpe ratio'),
        ('Sortino',     'Sortino ratio'),
        ('MaxDD_pct',   'Max drawdown %', True),
        ('WinRate_pct', 'Monthly win rate %'),
    ]
    for col, (key, title, *extra) in enumerate(metric_specs):
        ax = fig.add_subplot(gs[2, col])
        style(ax)
        vals = [abs(r[key]) for r in all_r]
        bars = ax.bar(range(len(all_r)), vals, color=bcolors, width=0.55, edgecolor='none')
        ax.set_xticks(range(len(all_r)))
        ax.set_xticklabels(labels, color='#999', fontsize=7, rotation=12, ha='right')
        ax.set_title(title, color='#ddd', fontsize=9, pad=5)
        for b, v in zip(bars, vals):
            ax.text(b.get_x()+b.get_width()/2, v+0.2, f'{v:.1f}',
                    ha='center', va='bottom', color='#ccc', fontsize=7.5)

    plt.suptitle('Indian Equity Strategy Backtest  ·  Momentum · Value-Quality · Sector Rotation · Hybrid',
                 color='white', fontsize=13, fontweight='bold', y=0.97)
    plt.savefig(out, dpi=150, bbox_inches='tight', facecolor='#0d0d0d')
    print(f"  Chart → {out}")
    plt.close()


# ── Summary ───────────────────────────────────────────────────────────────────
def summary_table(results):
    cols = ['Strategy','CAGR_pct','AnnVol_pct','Sharpe','Sortino','MaxDD_pct','Calmar','WinRate_pct']
    rn   = {'CAGR_pct':'CAGR %','AnnVol_pct':'Vol %','MaxDD_pct':'MaxDD %','WinRate_pct':'WinRate %'}
    df   = pd.DataFrame([{k:r[k] for k in cols} for r in results]).rename(columns=rn).set_index('Strategy')
    print('\n' + '='*85)
    print('  BACKTEST SUMMARY   2015-2024   Capital ₹10,00,000')
    print('='*85)
    print(df.to_string())
    print('='*85)
    print('\nAssumptions:')
    print('  · Round-trip cost 0.5% | STCG tax 20% | Demo = simulated data')
    print('  · Momentum: monthly rebalance, top 10, 10% trailing stop')
    print('  · Value-Quality: semi-annual rebalance, top 20, P/B + ROE rank')
    print('  · Sector Rotation: quarterly, top 3 sectors by 12M return')
    print('  · Hybrid: monthly rebalance, top 15, sector filter + blended MOM/VQ score + 8% trailing stop')
    print('  · Live mode: python backtest_strategies.py --live  (requires yfinance + internet)')
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backtest_summary.csv')
    df.to_csv(out); print(f'  CSV → {out}')


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()

    print('='*60)
    print('  Indian Equity Strategy Backtester')
    print(f"  Mode: {'LIVE (yfinance)' if args.live else 'DEMO (simulated)'}")
    print('='*60)

    if args.live:
        from backtest_strategies import load_live_data
        price_df, sector_df, bench, pb_df, roe_df = load_live_data()
    else:
        price_df, sector_df, bench, pb_df, roe_df = simulate_data()

    results = []
    for fn, kwargs, name in [
        (backtest_momentum,        dict(price_df=price_df),                                                    'Momentum'),
        (backtest_value_quality,   dict(price_df=price_df, pb_df=pb_df, roe_df=roe_df),                       'Value-Quality'),
        (backtest_sector_rotation, dict(sector_df=sector_df),                                                  'Sector Rotation'),
        (backtest_hybrid,          dict(price_df=price_df, sector_df=sector_df, pb_df=pb_df, roe_df=roe_df),  'Hybrid'),
        (benchmark_metrics,        dict(bench_s=bench),                                                        'Benchmark'),
    ]:
        try:
            results.append(fn(**kwargs))
        except Exception as e:
            print(f'  [!] {name} failed: {e}')
            import traceback; traceback.print_exc()

    if len(results) >= 2:
        summary_table(results)
        out_img = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backtest_results.png')
        plot_all(results, out=out_img)
    print('\nDone.')

if __name__ == '__main__':
    main()
