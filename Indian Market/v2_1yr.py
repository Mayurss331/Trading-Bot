"""
v2 Strategies  —  1-Year Return Calculator
===========================================
Runs all 5 strategies from backtest_strategies_v2.py on a ₹10,000 investment
for a chosen calendar year and prints a full comparison.

Strategies:
  1. Momentum (6-12M)
  2. Value-Quality
  3. Sector Rotation
  4. Hybrid Momentum-Quality  (ROE > 15%, D/E < 1 quality gate)
  5. 52-Week High + Regime Filter  (cash when Nifty < 200 DMA)

Usage:
    python v2_1yr.py              # evaluates 2024
    python v2_1yr.py --year 2023  # pick a different year
    python v2_1yr.py --year 2022 --investment 50000
"""

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import argparse, os, warnings
warnings.filterwarnings('ignore')

# ── Config ────────────────────────────────────────────────────────────────────
COST        = 0.005   # 0.5% round-trip
STCG        = 0.20    # 20% short-term capital gains tax
TOP_MOM     = 10
TOP_VQ      = 20
TOP_SECTORS = 3
TOP_HMQ     = 15
TOP_52W     = 12
WARMUP_YRS  = 2       # years of data before the target year (for 52W high, 200 DMA etc.)

COLORS = {
    'Momentum':              '#1D9E75',
    'Value-Quality':         '#378ADD',
    'Sector Rotation':       '#EF9F27',
    'Hybrid Mom-Quality':    '#D4537E',
    '52W High + Regime':     '#7F77DD',
    'Nifty 50 (Benchmark)':  '#888780',
}


# ── Simulate data ─────────────────────────────────────────────────────────────
def simulate_data(start: str, end: str, n_stocks=30, n_sectors=8, seed=42):
    np.random.seed(seed)
    dates = pd.date_range(start, end, freq='B')
    T     = len(dates)
    names = [
        'RELIANCE','TCS','HDFCBANK','INFY','ICICIBANK','HINDUNILVR','ITC',
        'AXISBANK','LT','BAJFINANCE','WIPRO','SBIN','MARUTI','TITAN','NESTLEIND',
        'KOTAKBANK','ASIANPAINT','ONGC','NTPC','POWERGRID','ULTRACEMCO','GRASIM',
        'SUNPHARMA','DRREDDY','DIVISLAB','CIPLA','APOLLOHOSP','ADANIENT',
        'ADANIPORTS','COALINDIA',
    ][:n_stocks]
    sec_names = ['Banks','IT','Auto','FMCG','Pharma','Realty','Metal','Energy'][:n_sectors]

    sec_f      = np.zeros((T, n_sectors))
    for t in range(1, T):
        sec_f[t] = 0.12 * sec_f[t-1] + np.random.normal(0.00035, 0.010, n_sectors)
    sec_px = np.cumprod(1 + sec_f, axis=0) * 10000

    assign = np.arange(n_stocks) % n_sectors
    px     = np.zeros((T, n_stocks))
    px[0]  = np.random.uniform(200, 3000, n_stocks)
    for t in range(1, T):
        idio  = np.random.normal(0.0001, 0.012, n_stocks)
        ret   = 0.70 * sec_f[t, assign] + 0.30 * idio
        px[t] = px[t-1] * (1 + ret)

    dlr   = np.diff(np.log(px), axis=0).mean(axis=1)
    bench = np.concatenate([[10000], 10000 * np.cumprod(np.exp(dlr))])

    rng      = np.random.default_rng(seed)
    pb_base  = rng.uniform(0.6, 7.0, n_stocks)
    roe_base = rng.uniform(0.04, 0.42, n_stocks)
    de_base  = rng.uniform(0.05, 2.5, n_stocks)
    pb_arr   = np.clip(pb_base  + rng.normal(0, 0.01, (T, n_stocks)).cumsum(0)*0.02,  0.3,  15)
    roe_arr  = np.clip(roe_base + rng.normal(0, 0.005,(T, n_stocks)).cumsum(0)*0.003, 0.01, 0.60)
    de_arr   = np.clip(de_base  + rng.normal(0, 0.01, (T, n_stocks)).cumsum(0)*0.005, 0.0,  4.0)

    return (
        pd.DataFrame(px,     index=dates, columns=names),
        pd.DataFrame(sec_px, index=dates, columns=sec_names),
        pd.Series(bench,     index=dates, name='NIFTY50'),
        pd.DataFrame(pb_arr, index=dates, columns=names),
        pd.DataFrame(roe_arr,index=dates, columns=names),
        pd.DataFrame(de_arr, index=dates, columns=names),
    )


# ── Generic monthly rebalancer ─────────────────────────────────────────────────
def _run_monthly(signals_df, price_df, top_n, capital, stop_pct=0.10):
    """signals_df: DataFrame (month-end index × stocks), higher score = buy."""
    monthly_px = price_df.resample('ME').last().ffill()
    equity     = {}
    cash       = capital
    pos        = {}   # {tkr: {'shares', 'cost_px', 'peak_px'}}

    for date in monthly_px.index:
        if date not in signals_df.index:
            continue
        px     = monthly_px.loc[date]
        score  = signals_df.loc[date].dropna()
        target = set(score.nlargest(top_n).index)

        # trailing stop + peak update
        stopped = set()
        for tkr, p in pos.items():
            cp = px.get(tkr, p['peak_px'])
            if cp > p['peak_px']:
                pos[tkr]['peak_px'] = cp
            if cp < p['peak_px'] * (1 - stop_pct):
                stopped.add(tkr)

        # sell
        for tkr in list((set(pos) - target) | stopped):
            p      = pos.pop(tkr)
            sp     = px.get(tkr, p['cost_px'])
            gross  = p['shares'] * sp
            profit = max(0, (sp - p['cost_px']) * p['shares'])
            cash  += gross * (1 - COST / 2) - profit * STCG

        # buy
        to_buy = target - set(pos)
        if to_buy:
            slots    = top_n - len(pos)
            per_slot = cash / max(slots, 1)
            for tkr in list(to_buy)[:slots]:
                ep = px.get(tkr)
                if ep and ep > 0:
                    shares = int(per_slot / ep)
                    if shares > 0:
                        cost = shares * ep * (1 + COST / 2)
                        if cost <= cash:
                            pos[tkr] = {'shares': shares, 'cost_px': ep, 'peak_px': ep}
                            cash -= cost

        port_val = cash + sum(
            p['shares'] * px.get(tkr, p['cost_px']) for tkr, p in pos.items()
        )
        equity[date] = max(port_val, 1)

    eq = pd.Series(equity)
    return eq / eq.iloc[0] * capital


# ── Strategy 1: Momentum ───────────────────────────────────────────────────────
def run_momentum(price_df, capital):
    monthly = price_df.resample('ME').last().ffill()
    ret6    = monthly.pct_change(6)
    ret12   = monthly.pct_change(12)
    ret1    = monthly.pct_change(1)
    signals = {}
    for i in range(13, len(monthly)):
        date  = monthly.index[i]
        score = (0.5 * ret6.iloc[i] + 0.5 * ret12.iloc[i]).dropna()
        bad   = ret1.iloc[i].dropna().nsmallest(max(1, int(len(score) * 0.10))).index
        signals[date] = score.drop(bad, errors='ignore')
    return _run_monthly(pd.DataFrame(signals).T, price_df, TOP_MOM, capital, 0.10)


# ── Strategy 2: Value-Quality ──────────────────────────────────────────────────
def run_value_quality(price_df, pb_df, roe_df, capital):
    semi_px  = price_df.resample('6ME').last().ffill()
    semi_pb  = pb_df.resample('6ME').last().ffill()
    semi_roe = roe_df.resample('6ME').last().ffill()
    all_dates = price_df.index
    daily_px  = price_df.ffill()
    equity    = {}
    holdings  = {}
    cash      = capital
    rebal_log = []

    for i in range(1, len(semi_px)):
        date   = semi_px.index[i]
        px_now = semi_px.iloc[i]
        cols   = (semi_pb.columns.intersection(semi_roe.columns)
                                 .intersection(px_now.dropna().index))
        pb_v   = semi_pb.iloc[i][cols].dropna()
        roe_v  = semi_roe.iloc[i][cols].dropna()
        cols   = pb_v.index.intersection(roe_v.index)
        combo  = pb_v[cols].rank(ascending=True) + roe_v[cols].rank(ascending=False)
        target = list(combo.nsmallest(TOP_VQ).index)

        for tkr, p in list(holdings.items()):
            sp     = px_now.get(tkr, p['cost_px'])
            gross  = p['shares'] * sp
            profit = max(0, (sp - p['cost_px']) * p['shares'])
            cash  += gross * (1 - COST / 2) - profit * STCG
        holdings = {}

        if target and cash > 0:
            per_stock = cash / len(target)
            for tkr in target:
                ep = px_now.get(tkr)
                if ep and ep > 0:
                    shares = int(per_stock / ep)
                    if shares > 0:
                        cost = shares * ep * (1 + COST / 2)
                        if cost <= cash:
                            holdings[tkr] = {'shares': shares, 'cost_px': ep}
                            cash -= cost

        port_val = cash + sum(p['shares'] * px_now.get(tkr, p['cost_px'])
                              for tkr, p in holdings.items())
        rebal_log.append({'date': date.strftime('%Y-%m-%d'),
                          'stocks': ', '.join(target[:8]) + ('...' if len(target) > 8 else ''),
                          'n': len(holdings), 'nav': round(port_val, 2)})

        prev_date = semi_px.index[i - 1]
        for d in all_dates[(all_dates > prev_date) & (all_dates <= date)]:
            val = cash + sum(p['shares'] * daily_px.loc[d].get(tkr, p['cost_px'])
                             for tkr, p in holdings.items())
            equity[d] = max(val, 1)

    eq = pd.Series(equity).sort_index()
    return eq / eq.iloc[0] * capital, rebal_log


# ── Strategy 3: Sector Rotation ────────────────────────────────────────────────
def run_sector_rotation(sector_df, capital):
    ETF_C  = 0.001
    qtr    = sector_df.resample('QE').last().ffill()
    all_d  = sector_df.index
    daily  = sector_df.ffill()
    equity = {}
    pos    = {}
    cash   = capital

    for i in range(4, len(qtr)):
        date   = qtr.index[i]
        px_now = qtr.iloc[i]
        top    = set((px_now / qtr.iloc[i - 4] - 1).dropna().nlargest(TOP_SECTORS).index)

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

        prev_date = qtr.index[i - 1]
        for d in all_d[(all_d > prev_date) & (all_d <= date)]:
            val = cash + sum(p['units'] * daily.loc[d].get(sec, p['cost_px'])
                             for sec, p in pos.items())
            equity[d] = max(val, 1)

    eq = pd.Series(equity).sort_index()
    return eq / eq.iloc[0] * capital


# ── Strategy 4: Hybrid Momentum-Quality ───────────────────────────────────────
def run_hybrid_mq(price_df, roe_df, de_df, capital):
    monthly     = price_df.resample('ME').last().ffill()
    monthly_roe = roe_df.resample('ME').last().ffill()
    monthly_de  = de_df.resample('ME').last().ffill()
    ret6        = monthly.pct_change(6)
    ret12       = monthly.pct_change(12)
    ret1        = monthly.pct_change(1)
    ROE_MIN, DE_MAX = 0.15, 1.0
    signals = {}
    for i in range(13, len(monthly)):
        date        = monthly.index[i]
        roe_now     = monthly_roe.iloc[i].dropna()
        de_now      = monthly_de.iloc[i].dropna()
        quality_ok  = roe_now[roe_now >= ROE_MIN].index
        low_debt    = de_now[de_now <= DE_MAX].index
        universe    = quality_ok.intersection(low_debt)
        if len(universe) < TOP_HMQ:
            quality_ok = roe_now[roe_now >= ROE_MIN * 0.8].index
            universe   = quality_ok.intersection(low_debt)
        score = (0.5 * ret6.iloc[i] + 0.5 * ret12.iloc[i])[universe].dropna()
        bad   = ret1.iloc[i].reindex(score.index).dropna().nsmallest(
                    max(1, int(len(score) * 0.10))).index
        signals[date] = score.drop(bad, errors='ignore')
    return _run_monthly(pd.DataFrame(signals).T, price_df, TOP_HMQ, capital, 0.10)


# ── Strategy 5: 52-Week High + Regime Filter ──────────────────────────────────
def run_52w_regime(price_df, benchmark_s, capital):
    monthly      = price_df.resample('ME').last().ffill()
    bench_daily  = benchmark_s.ffill()
    ma200_daily  = bench_daily.rolling(200, min_periods=100).mean()
    ma200_m      = ma200_daily.resample('ME').last().ffill()
    bench_m      = benchmark_s.resample('ME').last().ffill()
    rolling_52w  = price_df.rolling(252, min_periods=126).max()
    rolling_52w_m= rolling_52w.resample('ME').last().ffill()
    signals      = {}
    regime_log   = {}
    for i in range(13, len(monthly)):
        date      = monthly.index[i]
        px_now    = monthly.iloc[i]
        bench_now = bench_m.get(date)
        ma200_now = ma200_m.get(date)
        if bench_now is None or ma200_now is None:
            signals[date]    = pd.Series(dtype=float)
            regime_log[date] = False
            continue
        above = bench_now > ma200_now
        regime_log[date] = above
        if not above:
            signals[date] = pd.Series(dtype=float)
            continue
        high_52w      = rolling_52w_m.iloc[i]
        nearness      = (px_now / high_52w).dropna()
        signals[date] = nearness[nearness > 0]
    pct_in = sum(regime_log.values()) / max(len(regime_log), 1) * 100
    return _run_monthly(pd.DataFrame(signals).T, price_df, TOP_52W, capital, 0.10), pct_in


# ── Helpers ───────────────────────────────────────────────────────────────────
def slice_year(eq: pd.Series, year: int, capital: float) -> pd.Series:
    yr = eq[eq.index.year == year]
    if yr.empty:
        return yr
    return yr / yr.iloc[0] * capital


def monthly_table(eq: pd.Series, capital: float) -> pd.DataFrame:
    monthly = eq.resample('ME').last()
    rows = []
    for i, (date, nav) in enumerate(monthly.items()):
        prev = monthly.iloc[i - 1] if i > 0 else capital
        rows.append({
            'Month':       date.strftime('%b %Y'),
            'Value ₹':     round(nav, 2),
            'Month Ret %': round((nav / prev - 1) * 100, 2),
            'Total Ret %': round((nav / capital - 1) * 100, 2),
        })
    return pd.DataFrame(rows)


def summary_row(name: str, eq: pd.Series, capital: float) -> dict:
    final     = eq.iloc[-1]
    total_ret = (final / capital - 1) * 100
    max_dd    = ((eq - eq.cummax()) / eq.cummax()).min() * 100
    monthly   = eq.resample('ME').last().pct_change().dropna()
    win_rate  = (monthly > 0).mean() * 100
    return {
        'Strategy':    name,
        'Start ₹':     capital,
        'Final ₹':     round(final, 2),
        'Gain ₹':      round(final - capital, 2),
        'Return %':    round(total_ret, 2),
        'Max DD %':    round(max_dd, 2),
        'Win Rate %':  round(win_rate, 1),
    }


# ── Plot ──────────────────────────────────────────────────────────────────────
def plot_results(eq_dict: dict, bench_eq: pd.Series, capital: float, year: int, out: str):
    fig, axes = plt.subplots(3, 1, figsize=(14, 12),
                              facecolor='#0d0d0d',
                              gridspec_kw={'height_ratios': [4, 2, 2], 'hspace': 0.42})

    def style(ax):
        ax.set_facecolor('#161616')
        ax.tick_params(colors='#777', labelsize=8)
        ax.spines[['top', 'right']].set_visible(False)
        ax.spines[['bottom', 'left']].set_color('#333')

    # ── Equity curves ──────────────────────────────────────────────────────
    ax1 = axes[0]
    style(ax1)
    ax1.plot(bench_eq.index, bench_eq, color=COLORS['Nifty 50 (Benchmark)'],
             lw=1.5, ls='--', alpha=0.7, label='Nifty 50')
    ax1.axhline(capital, color='#444', lw=0.8, ls=':')
    for name, eq in eq_dict.items():
        ax1.plot(eq.index, eq, color=COLORS.get(name, '#fff'), lw=2.0, label=name)
    ax1.set_ylabel('Portfolio value (₹)', color='#999', fontsize=9)
    ax1.set_title(f'All 5 v2 Strategies  ·  ₹{capital:,.0f} invested on 1 Jan {year}  ·  Demo (simulated)',
                  color='#ddd', fontsize=11, fontweight='bold', pad=10)
    ax1.legend(framealpha=0, labelcolor='white', fontsize=8, ncol=3)

    # ── Drawdown ───────────────────────────────────────────────────────────
    ax2 = axes[1]
    style(ax2)
    bench_dd = (bench_eq - bench_eq.cummax()) / bench_eq.cummax() * 100
    ax2.fill_between(bench_dd.index, bench_dd, 0,
                     color=COLORS['Nifty 50 (Benchmark)'], alpha=0.2)
    ax2.plot(bench_dd.index, bench_dd, color=COLORS['Nifty 50 (Benchmark)'],
             lw=1, ls='--', alpha=0.6, label='Nifty 50')
    for name, eq in eq_dict.items():
        dd = (eq - eq.cummax()) / eq.cummax() * 100
        ax2.fill_between(dd.index, dd, 0, color=COLORS.get(name, '#fff'), alpha=0.15)
        ax2.plot(dd.index, dd, color=COLORS.get(name, '#fff'), lw=1.3, label=name)
    ax2.set_ylabel('Drawdown %', color='#999', fontsize=9)
    ax2.set_title('Drawdown', color='#ddd', fontsize=9, pad=5)
    ax2.legend(framealpha=0, labelcolor='white', fontsize=7, ncol=3)

    # ── Bar: total return by strategy ──────────────────────────────────────
    ax3 = axes[2]
    style(ax3)
    all_names   = list(eq_dict.keys()) + ['Nifty 50 (Benchmark)']
    all_returns = [(eq.iloc[-1] / capital - 1) * 100 for eq in eq_dict.values()]
    bench_ret   = (bench_eq.iloc[-1] / capital - 1) * 100
    all_returns.append(bench_ret)
    bar_colors  = [COLORS.get(n, '#fff') for n in all_names]
    short_names = [n.replace('(Benchmark)', '').replace('Hybrid ', '').replace(' Filter', '') for n in all_names]
    bars = ax3.bar(range(len(all_names)), all_returns, color=bar_colors,
                   width=0.55, edgecolor='none')
    ax3.set_xticks(range(len(all_names)))
    ax3.set_xticklabels(short_names, color='#999', fontsize=8, rotation=10, ha='right')
    ax3.axhline(0, color='#444', lw=0.7)
    ax3.set_title(f'Total return %  ({year})', color='#ddd', fontsize=9, pad=5)
    for bar, val in zip(bars, all_returns):
        color = '#2ecc71' if val >= 0 else '#e74c3c'
        ax3.text(bar.get_x() + bar.get_width() / 2,
                 val + (0.1 if val >= 0 else -0.3),
                 f'{val:+.1f}%', ha='center', va='bottom' if val >= 0 else 'top',
                 color=color, fontsize=8, fontweight='bold')

    plt.suptitle(f'{year} — v2 Strategy Comparison  ·  ₹{capital:,.0f} investment',
                 color='white', fontsize=13, fontweight='bold', y=0.98)
    plt.savefig(out, dpi=150, bbox_inches='tight', facecolor='#0d0d0d')
    print(f'  Chart → {out}')
    plt.close()


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--year',       type=int,   default=2024)
    parser.add_argument('--investment', type=float, default=10_000)
    args    = parser.parse_args()
    year    = args.year
    capital = args.investment

    sim_start = f'{year - WARMUP_YRS}-01-01'
    sim_end   = f'{year}-12-31'

    print('=' * 65)
    print(f'  v2 Strategies  |  ₹{capital:,.0f} invested  |  Year {year}')
    print('=' * 65)
    print(f'  Simulating data {sim_start} → {sim_end} ...')

    price_df, sector_df, bench_s, pb_df, roe_df, de_df = simulate_data(sim_start, sim_end)

    print('  Running strategies ...')

    # Run full window, then slice to target year
    eq_mom    = slice_year(run_momentum(price_df, capital), year, capital)
    eq_vq, rebal_log = run_value_quality(price_df, pb_df, roe_df, capital)
    eq_vq     = slice_year(eq_vq, year, capital)
    eq_sec    = slice_year(run_sector_rotation(sector_df, capital), year, capital)
    eq_hmq    = slice_year(run_hybrid_mq(price_df, roe_df, de_df, capital), year, capital)
    eq_52w_full, pct_in_market = run_52w_regime(price_df, bench_s, capital)
    eq_52w    = slice_year(eq_52w_full, year, capital)

    # Benchmark
    bench_yr  = bench_s[bench_s.index.year == year]
    bench_eq  = bench_yr / bench_yr.iloc[0] * capital
    bench_ret = (bench_eq.iloc[-1] / capital - 1) * 100

    eq_dict = {
        'Momentum':           eq_mom,
        'Value-Quality':      eq_vq,
        'Sector Rotation':    eq_sec,
        'Hybrid Mom-Quality': eq_hmq,
        '52W High + Regime':  eq_52w,
    }

    # ── Summary table ──────────────────────────────────────────────────────
    rows = [summary_row(name, eq, capital) for name, eq in eq_dict.items()]
    rows.append({
        'Strategy': 'Nifty 50 (Benchmark)',
        'Start ₹':  capital,
        'Final ₹':  round(bench_eq.iloc[-1], 2),
        'Gain ₹':   round(bench_eq.iloc[-1] - capital, 2),
        'Return %': round(bench_ret, 2),
        'Max DD %': round(((bench_eq - bench_eq.cummax()) / bench_eq.cummax()).min() * 100, 2),
        'Win Rate %': round((bench_eq.resample('ME').last().pct_change().dropna() > 0).mean() * 100, 1),
    })

    df_sum = pd.DataFrame(rows).set_index('Strategy')
    print(f'\n{"="*75}')
    print(f'  RESULTS  —  ₹{capital:,.0f} invested on 1 Jan {year}')
    print(f'{"="*75}')
    print(df_sum.to_string())
    print(f'{"="*75}')
    print(f'  52W High + Regime was in market {pct_in_market:.0f}% of months in this window')

    # ── Monthly breakdown: best strategy ───────────────────────────────────
    best_name = max(eq_dict, key=lambda n: eq_dict[n].iloc[-1])
    best_ret  = (eq_dict[best_name].iloc[-1] / capital - 1) * 100
    print(f'\n  Best strategy this year: {best_name}  ({best_ret:+.2f}%)')
    print(f'\n{"─"*60}')
    print(f'  Monthly Breakdown — {best_name}  ({year})')
    print(f'{"─"*60}')
    print(monthly_table(eq_dict[best_name], capital).to_string(index=False))
    print(f'{"─"*60}')

    # ── Value-Quality rebalances in target year ────────────────────────────
    yr_rebal = [r for r in rebal_log if r['date'].startswith(str(year))]
    if yr_rebal:
        print(f'\n  Value-Quality rebalances in {year}:')
        for r in yr_rebal:
            print(f"    {r['date']}  |  {r['n']} stocks  |  NAV ₹{r['nav']:,.0f}")
            print(f"               Picks: {r['stocks']}")

    # ── Save ───────────────────────────────────────────────────────────────
    here    = os.path.dirname(os.path.abspath(__file__))
    out_csv = os.path.join(here, f'v2_1yr_{year}.csv')
    out_img = os.path.join(here, f'v2_1yr_{year}.png')

    df_sum.to_csv(out_csv)
    print(f'\n  CSV  → {out_csv}')

    plot_results(eq_dict, bench_eq, capital, year, out_img)

    print('\nNote: Results use SIMULATED data (same seed as main backtests).')


if __name__ == '__main__':
    main()
