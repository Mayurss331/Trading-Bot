"""
Value-Quality Strategy  —  1-Year Return Calculator
=====================================================
Simulates the Value-Quality strategy (P/B + ROE rank, semi-annual rebalance)
over the last 1 calendar year (2024) on a ₹10,000 starting investment.

Usage:
    python value_quality_1yr.py              # demo (simulated data)
    python value_quality_1yr.py --year 2023  # pick a different year
"""

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import argparse, os, warnings
warnings.filterwarnings('ignore')

# ── Config ────────────────────────────────────────────────────────────────────
INVESTMENT  = 10_000          # ₹10,000 starting capital
COST        = 0.005           # 0.5% round-trip transaction cost
STCG        = 0.20            # 20% short-term capital gains tax
TOP_VQ      = 20              # top-N stocks by composite rank
WARMUP_YRS  = 1               # data needed before the target year starts

# ── Simulate price + fundamental data ─────────────────────────────────────────
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

    sec_f    = np.zeros((T, n_sectors))
    for t in range(1, T):
        sec_f[t] = 0.12 * sec_f[t-1] + np.random.normal(0.00035, 0.010, n_sectors)

    assign = np.arange(n_stocks) % n_sectors
    px     = np.zeros((T, n_stocks))
    px[0]  = np.random.uniform(200, 3000, n_stocks)
    for t in range(1, T):
        idio  = np.random.normal(0.0001, 0.012, n_stocks)
        ret   = 0.70 * sec_f[t, assign] + 0.30 * idio
        px[t] = px[t-1] * (1 + ret)

    rng       = np.random.default_rng(seed)
    pb_base   = rng.uniform(0.6, 7.0, n_stocks)
    roe_base  = rng.uniform(0.04, 0.42, n_stocks)
    pb_noise  = rng.normal(0, 0.01, (T, n_stocks))
    roe_noise = rng.normal(0, 0.005, (T, n_stocks))
    pb_arr    = np.clip(pb_base + pb_noise.cumsum(0) * 0.02, 0.3, 15)
    roe_arr   = np.clip(roe_base + roe_noise.cumsum(0) * 0.005, 0.01, 0.60)

    price_df = pd.DataFrame(px,      index=dates, columns=names)
    pb_df    = pd.DataFrame(pb_arr,  index=dates, columns=names)
    roe_df   = pd.DataFrame(roe_arr, index=dates, columns=names)

    # equal-weight benchmark
    log_ret = np.diff(np.log(px), axis=0).mean(axis=1)
    bench   = np.concatenate([[10000], 10000 * np.cumprod(np.exp(log_ret))])
    bench_s = pd.Series(bench, index=dates, name='NIFTY50')

    return price_df, pb_df, roe_df, bench_s


# ── Value-Quality backtester ───────────────────────────────────────────────────
def run_value_quality(price_df, pb_df, roe_df, capital: float):
    semi_px  = price_df.resample('6ME').last().ffill()
    semi_pb  = pb_df.resample('6ME').last().ffill()
    semi_roe = roe_df.resample('6ME').last().ffill()
    all_dates = price_df.index
    daily_px  = price_df.ffill()

    equity   = {}
    holdings = {}
    cash     = capital

    rebalance_log = []   # record each rebalance event

    for i in range(1, len(semi_px)):
        date   = semi_px.index[i]
        px_now = semi_px.iloc[i]

        cols  = semi_pb.columns.intersection(semi_roe.columns).intersection(px_now.dropna().index)
        pb_v  = semi_pb.iloc[i][cols].dropna()
        roe_v = semi_roe.iloc[i][cols].dropna()
        cols  = pb_v.index.intersection(roe_v.index)
        pb_v, roe_v = pb_v[cols], roe_v[cols]

        vrank  = pb_v.rank(ascending=True)
        qrank  = roe_v.rank(ascending=False)
        combo  = vrank + qrank
        target = list(combo.nsmallest(TOP_VQ).index)

        # sell all
        for tkr, p in list(holdings.items()):
            sp     = px_now.get(tkr, p['cost_px'])
            gross  = p['shares'] * sp
            profit = max(0, (sp - p['cost_px']) * p['shares'])
            cash  += gross * (1 - COST / 2) - profit * STCG
        holdings = {}

        # buy
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
        rebalance_log.append({
            'date':     date.strftime('%Y-%m-%d'),
            'holdings': ', '.join(target[:10]) + ('...' if len(target) > 10 else ''),
            'n_stocks': len(holdings),
            'nav':      round(port_val, 2),
        })

        # daily NAV
        prev_date = semi_px.index[i - 1]
        period    = all_dates[(all_dates > prev_date) & (all_dates <= date)]
        for d in period:
            val = cash + sum(p['shares'] * daily_px.loc[d].get(tkr, p['cost_px'])
                             for tkr, p in holdings.items())
            equity[d] = max(val, 1)

    eq = pd.Series(equity).sort_index()
    eq = eq / eq.iloc[0] * capital
    return eq, rebalance_log


# ── Monthly breakdown table ───────────────────────────────────────────────────
def monthly_table(eq: pd.Series, capital: float) -> pd.DataFrame:
    monthly = eq.resample('ME').last()
    rows = []
    for i, (date, nav) in enumerate(monthly.items()):
        prev_nav = monthly.iloc[i - 1] if i > 0 else capital
        m_ret    = (nav / prev_nav - 1) * 100
        rows.append({
            'Month':       date.strftime('%b %Y'),
            'Portfolio ₹': round(nav, 2),
            'Month Ret %': round(m_ret, 2),
            'Gain ₹':      round(nav - capital, 2),
            'Total Ret %': round((nav / capital - 1) * 100, 2),
        })
    return pd.DataFrame(rows)


# ── Plot ──────────────────────────────────────────────────────────────────────
def plot_result(eq: pd.Series, bench: pd.Series, capital: float, year: int, out: str):
    bench_yr = bench[bench.index.year == year]
    bench_yr = bench_yr / bench_yr.iloc[0] * capital

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8),
                                    facecolor='#0d0d0d',
                                    gridspec_kw={'height_ratios': [3, 1], 'hspace': 0.35})

    def style(ax):
        ax.set_facecolor('#161616')
        ax.tick_params(colors='#777', labelsize=8)
        ax.spines[['top', 'right']].set_visible(False)
        ax.spines[['bottom', 'left']].set_color('#333')

    # Equity curve
    style(ax1)
    ax1.plot(eq.index, eq, color='#378ADD', lw=2.2, label='Value-Quality')
    ax1.plot(bench_yr.index, bench_yr, color='#888780', lw=1.4,
             ls='--', alpha=0.7, label='Nifty 50 (benchmark)')
    ax1.axhline(capital, color='#444', lw=0.8, ls=':')
    ax1.set_ylabel('Portfolio value (₹)', color='#999', fontsize=9)
    ax1.set_title(f'Value-Quality Strategy  ·  ₹{capital:,.0f} invested on 1 Jan {year}',
                  color='#ddd', fontsize=11, fontweight='bold', pad=10)
    ax1.legend(framealpha=0, labelcolor='white', fontsize=9)

    final     = eq.iloc[-1]
    gain      = final - capital
    total_ret = (final / capital - 1) * 100
    color     = '#2ecc71' if gain >= 0 else '#e74c3c'
    ax1.annotate(
        f'Final: ₹{final:,.0f}\nGain: ₹{gain:+,.0f}  ({total_ret:+.1f}%)',
        xy=(eq.index[-1], final),
        xytext=(-120, -30), textcoords='offset points',
        color=color, fontsize=9, fontweight='bold',
        arrowprops=dict(arrowstyle='->', color=color, lw=1.2),
    )

    # Drawdown
    style(ax2)
    roll_max = eq.cummax()
    dd       = (eq - roll_max) / roll_max * 100
    ax2.fill_between(dd.index, dd, 0, color='#378ADD', alpha=0.3)
    ax2.plot(dd.index, dd, color='#378ADD', lw=1)
    ax2.set_ylabel('Drawdown %', color='#999', fontsize=9)
    ax2.set_title('Drawdown', color='#ddd', fontsize=9, pad=5)

    plt.suptitle(f'{year} — Value-Quality 1-Year Return  ·  Demo (simulated data)',
                 color='white', fontsize=12, fontweight='bold', y=0.98)
    plt.savefig(out, dpi=150, bbox_inches='tight', facecolor='#0d0d0d')
    print(f'  Chart → {out}')
    plt.close()


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--year', type=int, default=2024,
                        help='Calendar year to evaluate (default: 2024)')
    args = parser.parse_args()
    year = args.year

    sim_start = f'{year - WARMUP_YRS}-01-01'
    sim_end   = f'{year}-12-31'

    print('=' * 60)
    print(f'  Value-Quality  |  ₹{INVESTMENT:,} invested  |  Year {year}')
    print('=' * 60)

    price_df, pb_df, roe_df, bench_s = simulate_data(sim_start, sim_end)

    # full run on whole window so semi-annual rebalances are properly seeded
    eq_full, rebalance_log = run_value_quality(price_df, pb_df, roe_df, INVESTMENT)

    # slice to the target year
    eq = eq_full[eq_full.index.year == year]
    if eq.empty:
        print(f'No data for {year}. Try a different --year.')
        return

    eq = eq / eq.iloc[0] * INVESTMENT      # re-index to ₹10,000 at Jan 1

    # ── Summary ────────────────────────────────────────────────────────────
    final     = eq.iloc[-1]
    gain      = final - INVESTMENT
    total_ret = (final / INVESTMENT - 1) * 100
    max_dd    = ((eq - eq.cummax()) / eq.cummax()).min() * 100

    bench_yr  = bench_s[bench_s.index.year == year]
    bench_ret = (bench_yr.iloc[-1] / bench_yr.iloc[0] - 1) * 100

    print(f'\n  Starting investment : ₹{INVESTMENT:>10,.2f}')
    print(f'  Ending value        : ₹{final:>10,.2f}')
    print(f'  Absolute gain/loss  : ₹{gain:>+10,.2f}')
    print(f'  Total return        :   {total_ret:>+8.2f}%')
    print(f'  Max drawdown        :   {max_dd:>8.2f}%')
    print(f'  Nifty 50 return     :   {bench_ret:>+8.2f}%  (benchmark)')
    print(f'  Alpha vs Nifty      :   {total_ret - bench_ret:>+8.2f}%')

    # ── Monthly breakdown ───────────────────────────────────────────────────
    tbl = monthly_table(eq, INVESTMENT)
    print(f'\n{"─"*62}')
    print(f'  Monthly Breakdown — Value-Quality  ({year})')
    print(f'{"─"*62}')
    print(tbl.to_string(index=False))
    print(f'{"─"*62}')

    # ── Rebalance events in target year ────────────────────────────────────
    yr_rebalances = [r for r in rebalance_log if r['date'].startswith(str(year))]
    if yr_rebalances:
        print(f'\n  Rebalances in {year}:')
        for r in yr_rebalances:
            print(f"    {r['date']}  |  {r['n_stocks']} stocks  |  NAV ₹{r['nav']:,.0f}")
            print(f"               Top picks: {r['holdings']}")

    # ── Save outputs ────────────────────────────────────────────────────────
    here    = os.path.dirname(os.path.abspath(__file__))
    out_csv = os.path.join(here, f'vq_1yr_{year}.csv')
    out_img = os.path.join(here, f'vq_1yr_{year}.png')

    tbl.to_csv(out_csv, index=False)
    print(f'\n  CSV  → {out_csv}')

    plot_result(eq, bench_s, INVESTMENT, year, out_img)

    print('\nNote: Results are based on SIMULATED data. '
          'Use --live flag in backtest_strategies.py for real prices.')


if __name__ == '__main__':
    main()
