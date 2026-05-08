"""
Crypto Daily Trading Strategy Backtester
==========================================
Research-backed strategies for BTC and ETH daily trading.

Strategies (long-only, daily candles, 2021-2024):
  1. EMA Crossover 9/21 + RSI 50 Filter
  2. RSI Mean Reversion  (RSI-14, buy <30 / sell >70)
  3. MACD + Bollinger Bands Confluence
  4. Supertrend          (ATR-10, multiplier 3.5)
  5. Rolling VWAP Mean Reversion  (20-day)
  6. SMA Crossover 40/100

Assets  : BTC-USD, ETH-USD
Period  : 2021-01-01 to 2024-12-31  (bull + bear + recovery + new ATH)
Costs   : 0.1% per leg = 0.2% round-trip (Binance spot fee)
Capital : $10,000 per strategy per coin

Usage:
    python crypto_backtest.py           # live yfinance download
    python crypto_backtest.py --demo    # simulated data (offline)
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

# ── Config ─────────────────────────────────────────────────────────────────────
CAPITAL    = 10_000       # USD per strategy per coin
COST       = 0.001        # 0.1% per leg  → 0.2% round-trip
START      = '2021-01-01'
END        = '2024-12-31'
COINS      = ['BTC-USD', 'ETH-USD']

COLORS = {
    'EMA 9/21 + RSI':       '#1D9E75',
    'RSI Mean Reversion':   '#378ADD',
    'MACD + BB':            '#EF9F27',
    'Supertrend':           '#E05CDA',
    'VWAP Reversion':       '#FF6B6B',
    'SMA 40/100':           '#4ECDC4',
    'Buy & Hold':           '#888780',
}


# ══════════════════════════════════════════════════════════════════════════════
# DATA
# ══════════════════════════════════════════════════════════════════════════════

def fetch_live(coin: str) -> pd.DataFrame:
    import yfinance as yf
    df = yf.download(coin, start=START, end=END, auto_adjust=True, progress=False)
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df = df[['Open','High','Low','Close','Volume']].dropna()
    print(f"  {coin}: {len(df)} daily bars  ({df.index[0].date()} → {df.index[-1].date()})")
    return df


def simulate_coin(seed: int = 42, annual_drift: float = 0.60,
                  annual_vol: float = 0.80) -> pd.DataFrame:
    """Crypto-like price path with fat tails and occasional crashes."""
    np.random.seed(seed)
    dates  = pd.date_range(START, END, freq='B')
    T      = len(dates)
    dt     = 1 / 252
    daily_drift = annual_drift * dt
    daily_vol   = annual_vol * np.sqrt(dt)

    log_rets = np.random.normal(daily_drift, daily_vol, T)
    # inject crash regime
    crash_periods = np.random.choice(T, size=int(T * 0.02), replace=False)
    log_rets[crash_periods] -= np.random.uniform(0.05, 0.15, len(crash_periods))

    price = 30_000 * np.cumprod(np.exp(log_rets))
    high  = price * np.random.uniform(1.001, 1.04, T)
    low   = price * np.random.uniform(0.96, 0.999, T)
    open_ = np.roll(price, 1); open_[0] = price[0]
    vol   = np.random.uniform(1e9, 5e10, T)

    return pd.DataFrame({'Open': open_, 'High': high,
                         'Low': low,   'Close': price,
                         'Volume': vol}, index=dates)


# ══════════════════════════════════════════════════════════════════════════════
# INDICATORS
# ══════════════════════════════════════════════════════════════════════════════

def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()

def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()

def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    delta = s.diff()
    gain  = delta.clip(lower=0).ewm(com=n - 1, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=n - 1, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)

def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=n, adjust=False).mean()

def macd(s: pd.Series):
    line   = ema(s, 12) - ema(s, 26)
    signal = ema(line, 9)
    return line, signal

def bollinger(s: pd.Series, n: int = 20, k: float = 2.0):
    mid   = sma(s, n)
    std   = s.rolling(n).std()
    return mid - k * std, mid, mid + k * std   # lower, mid, upper

def supertrend(high: pd.Series, low: pd.Series, close: pd.Series,
               n: int = 10, mult: float = 3.5) -> pd.Series:
    mid      = (high + low) / 2
    _atr     = atr(high, low, close, n)
    bu_basic = mid + mult * _atr
    bl_basic = mid - mult * _atr

    bu = bu_basic.copy()
    bl = bl_basic.copy()
    st = pd.Series(np.nan, index=close.index)

    for i in range(1, len(close)):
        bu.iloc[i] = bu_basic.iloc[i] if (bu_basic.iloc[i] < bu.iloc[i-1]
                                           or close.iloc[i-1] > bu.iloc[i-1]) else bu.iloc[i-1]
        bl.iloc[i] = bl_basic.iloc[i] if (bl_basic.iloc[i] > bl.iloc[i-1]
                                           or close.iloc[i-1] < bl.iloc[i-1]) else bl.iloc[i-1]
        if pd.isna(st.iloc[i-1]):
            st.iloc[i] = bl.iloc[i]
        elif st.iloc[i-1] == bu.iloc[i-1]:
            st.iloc[i] = bl.iloc[i] if close.iloc[i] > bu.iloc[i] else bu.iloc[i]
        else:
            st.iloc[i] = bu.iloc[i] if close.iloc[i] < bl.iloc[i] else bl.iloc[i]
    return st

def vwap_rolling(close: pd.Series, volume: pd.Series, n: int = 20) -> pd.Series:
    tp = close   # daily close as typical price (no intraday data)
    return (tp * volume).rolling(n).sum() / volume.rolling(n).sum()


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGY SIGNALS  (returns 1=long, 0=flat — NO look-ahead: shift(1) applied)
# ══════════════════════════════════════════════════════════════════════════════

def sig_ema_rsi(df: pd.DataFrame) -> pd.Series:
    """EMA 9/21 crossover confirmed by RSI > 50."""
    fast  = ema(df['Close'], 9)
    slow  = ema(df['Close'], 21)
    _rsi  = rsi(df['Close'], 14)
    raw   = ((fast > slow) & (_rsi > 50)).astype(int)
    return raw.shift(1).fillna(0)

def sig_rsi_mean_rev(df: pd.DataFrame) -> pd.Series:
    """Buy RSI < 30 in uptrend (price > EMA-100); exit RSI > 55."""
    _rsi  = rsi(df['Close'], 14)
    trend = df['Close'] > ema(df['Close'], 100)
    pos   = pd.Series(0, index=df.index)
    in_pos = False
    for i in range(1, len(df)):
        if not in_pos and _rsi.iloc[i] < 30 and trend.iloc[i]:
            in_pos = True
        elif in_pos and _rsi.iloc[i] > 55:
            in_pos = False
        pos.iloc[i] = 1 if in_pos else 0
    return pos.shift(1).fillna(0)

def sig_macd_bb(df: pd.DataFrame) -> pd.Series:
    """MACD bullish cross + price above BB lower band (momentum + not overbought)."""
    m_line, m_sig = macd(df['Close'])
    bb_lo, bb_mid, bb_up = bollinger(df['Close'], 20, 2)
    macd_cross  = (m_line > m_sig) & (m_line.shift(1) <= m_sig.shift(1))
    macd_on     = m_line > m_sig
    near_lo     = df['Close'] < bb_mid                  # below midband = not overbought
    raw         = (macd_on & near_lo).astype(int)
    # enter on cross, hold while MACD bullish and below upper band
    pos   = pd.Series(0, index=df.index)
    in_pos = False
    for i in range(1, len(df)):
        if not in_pos and macd_cross.iloc[i]:
            in_pos = True
        elif in_pos and (m_line.iloc[i] < m_sig.iloc[i] or df['Close'].iloc[i] > bb_up.iloc[i]):
            in_pos = False
        pos.iloc[i] = 1 if in_pos else 0
    return pos.shift(1).fillna(0)

def sig_supertrend(df: pd.DataFrame) -> pd.Series:
    """Long when close > Supertrend line."""
    st  = supertrend(df['High'], df['Low'], df['Close'], n=10, mult=3.5)
    raw = (df['Close'] > st).astype(int)
    return raw.shift(1).fillna(0)

def sig_vwap(df: pd.DataFrame) -> pd.Series:
    """Buy dip to rolling VWAP in uptrend (above SMA-50); exit above VWAP."""
    vw    = vwap_rolling(df['Close'], df['Volume'], 20)
    trend = df['Close'] > sma(df['Close'], 50)
    pos   = pd.Series(0, index=df.index)
    in_pos = False
    for i in range(1, len(df)):
        if not in_pos and df['Close'].iloc[i] < vw.iloc[i] and trend.iloc[i]:
            in_pos = True
        elif in_pos and df['Close'].iloc[i] > vw.iloc[i]:
            in_pos = False
        pos.iloc[i] = 1 if in_pos else 0
    return pos.shift(1).fillna(0)

def sig_sma_crossover(df: pd.DataFrame) -> pd.Series:
    """SMA 40 crosses above SMA 100."""
    fast = sma(df['Close'], 40)
    slow = sma(df['Close'], 100)
    raw  = (fast > slow).astype(int)
    return raw.shift(1).fillna(0)


# ══════════════════════════════════════════════════════════════════════════════
# BACKTESTER
# ══════════════════════════════════════════════════════════════════════════════

def backtest(df: pd.DataFrame, signal: pd.Series, capital: float) -> tuple:
    """
    Long-only backtester on daily close prices.
    Returns (equity Series, trades list).
    """
    price  = df['Close']
    equity = pd.Series(index=price.index, dtype=float)
    cash   = capital
    units  = 0.0
    entry_price = 0.0
    trades = []

    for i, (date, sig) in enumerate(signal.items()):
        px = price.loc[date]

        if units == 0 and sig == 1:                     # ── BUY ──
            cost_amt  = cash * COST
            units     = (cash - cost_amt) / px
            entry_price = px
            cash      = 0.0

        elif units > 0 and sig == 0:                    # ── SELL ──
            gross   = units * px
            cost_amt = gross * COST
            pnl     = gross - cost_amt - (units * entry_price * (1 + COST))
            cash    = gross - cost_amt
            trades.append({
                'entry_px': round(entry_price, 2),
                'exit_px':  round(px, 2),
                'pnl_pct':  round((px / entry_price - 1) * 100, 2),
                'win':      pnl > 0,
            })
            units       = 0.0
            entry_price = 0.0

        equity.loc[date] = cash + units * px

    # close any open position at last price
    if units > 0:
        px    = price.iloc[-1]
        gross = units * px
        cash  = gross * (1 - COST)
        pnl   = cash - (units * entry_price * (1 + COST))
        trades.append({
            'entry_px': round(entry_price, 2),
            'exit_px':  round(px, 2),
            'pnl_pct':  round((px / entry_price - 1) * 100, 2),
            'win':      pnl > 0,
        })
        equity.iloc[-1] = cash

    equity = equity.ffill().fillna(capital)
    return equity, trades


# ══════════════════════════════════════════════════════════════════════════════
# METRICS
# ══════════════════════════════════════════════════════════════════════════════

def compute_metrics(equity: pd.Series, trades: list, label: str) -> dict:
    rets    = equity.pct_change().dropna()
    n_yr    = (equity.index[-1] - equity.index[0]).days / 365.25
    cagr    = (equity.iloc[-1] / equity.iloc[0]) ** (1 / n_yr) - 1
    vol     = rets.std() * np.sqrt(252)
    sharpe  = (rets.mean() * 252) / vol if vol else 0
    down_v  = rets[rets < 0].std() * np.sqrt(252)
    sortino = (rets.mean() * 252) / down_v if down_v else 0
    roll_max = equity.cummax()
    dd       = (equity - roll_max) / roll_max
    max_dd   = dd.min()
    calmar   = cagr / abs(max_dd) if max_dd else 0

    n_trades   = len(trades)
    win_rate   = np.mean([t['win'] for t in trades]) * 100 if trades else 0
    avg_win    = np.mean([t['pnl_pct'] for t in trades if t['win']]) if any(t['win'] for t in trades) else 0
    avg_loss   = np.mean([t['pnl_pct'] for t in trades if not t['win']]) if any(not t['win'] for t in trades) else 0
    profit_f   = abs(avg_win / avg_loss) if avg_loss else 0

    return dict(
        Strategy    = label,
        Final_USD   = round(equity.iloc[-1], 2),
        CAGR_pct    = round(cagr * 100, 2),
        Vol_pct     = round(vol * 100, 2),
        Sharpe      = round(sharpe, 3),
        Sortino     = round(sortino, 3),
        MaxDD_pct   = round(max_dd * 100, 2),
        Calmar      = round(calmar, 3),
        WinRate_pct = round(win_rate, 1),
        Trades      = n_trades,
        AvgWin_pct  = round(avg_win, 2),
        AvgLoss_pct = round(avg_loss, 2),
        ProfitFactor= round(profit_f, 2),
        _eq         = equity,
        _dd         = dd,
    )


# ══════════════════════════════════════════════════════════════════════════════
# PLOT
# ══════════════════════════════════════════════════════════════════════════════

def plot_coin(results: list, coin: str, capital: float, out: str):
    strats = [r for r in results if r['Strategy'] != 'Buy & Hold']
    bah    = next(r for r in results if r['Strategy'] == 'Buy & Hold')

    fig = plt.figure(figsize=(18, 14), facecolor='#0d0d0d')
    gs  = gridspec.GridSpec(3, 3, figure=fig,
                            hspace=0.50, wspace=0.38,
                            left=0.06, right=0.97, top=0.93, bottom=0.06)

    def style(ax):
        ax.set_facecolor('#161616')
        ax.tick_params(colors='#777', labelsize=8)
        ax.spines[['top', 'right']].set_visible(False)
        ax.spines[['bottom', 'left']].set_color('#333')

    # ── Equity curves ──────────────────────────────────────────────────────
    ax1 = fig.add_subplot(gs[0, :])
    style(ax1)
    ax1.plot(bah['_eq'].index, bah['_eq'] / capital * 100,
             color=COLORS['Buy & Hold'], lw=1.5, ls='--', alpha=0.7, label='Buy & Hold')
    for r in strats:
        ax1.plot(r['_eq'].index, r['_eq'] / capital * 100,
                 color=COLORS.get(r['Strategy'], '#fff'), lw=1.8, label=r['Strategy'])
    ax1.axhline(100, color='#444', lw=0.7, ls=':')
    ax1.set_ylabel('Value (indexed 100)', color='#999', fontsize=9)
    ax1.set_title(f'{coin}  ·  $10,000 starting capital  ·  2021–2024  ·  Daily candles',
                  color='#ddd', fontsize=11, fontweight='bold', pad=10)
    ax1.legend(framealpha=0, labelcolor='white', fontsize=8, ncol=4, loc='upper left')
    ax1.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f'{v:.0f}'))

    # ── Drawdown ───────────────────────────────────────────────────────────
    ax2 = fig.add_subplot(gs[1, :])
    style(ax2)
    ax2.fill_between(bah['_dd'].index, bah['_dd'] * 100, 0,
                     color=COLORS['Buy & Hold'], alpha=0.2)
    ax2.plot(bah['_dd'].index, bah['_dd'] * 100,
             color=COLORS['Buy & Hold'], lw=1, ls='--', alpha=0.6, label='Buy & Hold')
    for r in strats:
        ax2.fill_between(r['_dd'].index, r['_dd'] * 100, 0,
                         color=COLORS.get(r['Strategy'], '#fff'), alpha=0.15)
        ax2.plot(r['_dd'].index, r['_dd'] * 100,
                 color=COLORS.get(r['Strategy'], '#fff'), lw=1.3, label=r['Strategy'])
    ax2.set_ylabel('Drawdown %', color='#999', fontsize=9)
    ax2.set_title('Underwater chart', color='#ddd', fontsize=10, pad=6)
    ax2.legend(framealpha=0, labelcolor='white', fontsize=8, ncol=4, loc='lower left')
    ax2.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f'{v:.0f}%'))

    # ── Bar charts ─────────────────────────────────────────────────────────
    all_r   = strats + [bah]
    labels  = [r['Strategy'].replace(' Mean Reversion', '\nMean Rev')
                             .replace(' + RSI', '\n+RSI')
                             .replace(' & Hold', '\n& Hold') for r in all_r]
    bcolors = [COLORS.get(r['Strategy'], '#fff') for r in all_r]

    for col, (key, title) in enumerate([
        ('CAGR_pct',     'CAGR %'),
        ('Sharpe',       'Sharpe ratio'),
        ('WinRate_pct',  'Win rate %'),
    ]):
        ax = fig.add_subplot(gs[2, col])
        style(ax)
        vals = [r[key] for r in all_r]
        bars = ax.bar(range(len(all_r)), vals, color=bcolors, width=0.55, edgecolor='none')
        ax.set_xticks(range(len(all_r)))
        ax.set_xticklabels(labels, color='#999', fontsize=6.5, rotation=10, ha='right')
        ax.set_title(title, color='#ddd', fontsize=9, pad=5)
        ax.axhline(0, color='#444', lw=0.6)
        for b, v in zip(bars, vals):
            ax.text(b.get_x() + b.get_width() / 2,
                    v + (max(vals) * 0.02 if v >= 0 else max(vals) * -0.04),
                    f'{v:.1f}', ha='center', va='bottom', color='#ccc', fontsize=7)

    plt.suptitle(f'Crypto Strategy Backtest  ·  {coin}  ·  6 Strategies vs Buy & Hold',
                 color='white', fontsize=13, fontweight='bold', y=0.97)
    plt.savefig(out, dpi=150, bbox_inches='tight', facecolor='#0d0d0d')
    print(f'  Chart → {out}')
    plt.close()


# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY TABLE
# ══════════════════════════════════════════════════════════════════════════════

def print_summary(coin: str, results: list, capital: float):
    cols = ['Strategy', 'Final_USD', 'CAGR_pct', 'Vol_pct', 'Sharpe',
            'Sortino', 'MaxDD_pct', 'WinRate_pct', 'Trades', 'ProfitFactor']
    rn   = {'Final_USD': 'Final $', 'CAGR_pct': 'CAGR %', 'Vol_pct': 'Vol %',
            'MaxDD_pct': 'MaxDD %', 'WinRate_pct': 'WinRate %', 'ProfitFactor': 'ProfitF'}
    df   = pd.DataFrame([{k: r[k] for k in cols} for r in results])
    df   = df.rename(columns=rn).set_index('Strategy')

    print(f'\n{"="*90}')
    print(f'  {coin}  |  $10,000 starting capital  |  2021-01-01 → 2024-12-31')
    print(f'{"="*90}')
    print(df.to_string())
    print(f'{"="*90}')

    best = max([r for r in results if r['Strategy'] != 'Buy & Hold'],
               key=lambda r: r['Sharpe'])
    print(f'\n  ★  Best risk-adjusted: {best["Strategy"]}  '
          f'(Sharpe {best["Sharpe"]:.2f}, CAGR {best["CAGR_pct"]:.1f}%, '
          f'MaxDD {best["MaxDD_pct"]:.1f}%)')

    bah_cagr = next(r['CAGR_pct'] for r in results if r['Strategy'] == 'Buy & Hold')
    top_strats = sorted([r for r in results if r['Strategy'] != 'Buy & Hold'],
                        key=lambda r: r['CAGR_pct'], reverse=True)
    print(f'  ★  Best raw return  : {top_strats[0]["Strategy"]}  '
          f'(CAGR {top_strats[0]["CAGR_pct"]:.1f}%  vs Buy&Hold {bah_cagr:.1f}%)')
    print(f'  ★  Best drawdown    : {min(results, key=lambda r: r["MaxDD_pct"])["Strategy"]}  '
          f'(MaxDD {min(results, key=lambda r: r["MaxDD_pct"])["MaxDD_pct"]:.1f}%)')


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

STRATEGIES = [
    ('EMA 9/21 + RSI',     sig_ema_rsi),
    ('RSI Mean Reversion', sig_rsi_mean_rev),
    ('MACD + BB',          sig_macd_bb),
    ('Supertrend',         sig_supertrend),
    ('VWAP Reversion',     sig_vwap),
    ('SMA 40/100',         sig_sma_crossover),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--demo', action='store_true',
                        help='Use simulated data instead of yfinance')
    args = parser.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))

    all_rows = []

    for coin_idx, coin in enumerate(COINS):
        print(f'\n{"─"*60}')
        print(f'  Processing {coin} ...')

        if args.demo:
            seeds = [42, 99]
            df    = simulate_coin(seed=seeds[coin_idx],
                                  annual_drift=0.50 if coin_idx == 0 else 0.40)
            print(f'  {coin}: {len(df)} simulated daily bars')
        else:
            try:
                df = fetch_live(coin)
            except Exception as e:
                print(f'  [!] yfinance failed ({e}), falling back to simulation')
                df = simulate_coin(seed=42 + coin_idx)

        results = []

        # ── Buy & Hold benchmark ──────────────────────────────────────────
        bah_eq = df['Close'] / df['Close'].iloc[0] * CAPITAL
        bah_trades = [{'entry_px': df['Close'].iloc[0],
                       'exit_px':  df['Close'].iloc[-1],
                       'pnl_pct':  (df['Close'].iloc[-1] / df['Close'].iloc[0] - 1) * 100,
                       'win':      df['Close'].iloc[-1] > df['Close'].iloc[0]}]
        results.append(compute_metrics(bah_eq, bah_trades, 'Buy & Hold'))

        # ── Run strategies ────────────────────────────────────────────────
        for name, sig_fn in STRATEGIES:
            try:
                sig = sig_fn(df)
                eq, trades = backtest(df, sig, CAPITAL)
                m = compute_metrics(eq, trades, name)
                results.append(m)
                bah_cagr = results[0]['CAGR_pct']
                print(f'    {name:<22} CAGR {m["CAGR_pct"]:+6.1f}%  '
                      f'Sharpe {m["Sharpe"]:5.2f}  MaxDD {m["MaxDD_pct"]:6.1f}%  '
                      f'Trades {m["Trades"]:3d}  WinRate {m["WinRate_pct"]:.0f}%')
            except Exception as e:
                print(f'    [!] {name} failed: {e}')

        print_summary(coin, results, CAPITAL)

        # ── Save chart ────────────────────────────────────────────────────
        out_img = os.path.join(here, f'crypto_{coin.replace("-","_")}.png')
        plot_coin(results, coin, CAPITAL, out_img)

        # ── Save CSV ──────────────────────────────────────────────────────
        cols = ['Strategy', 'Final_USD', 'CAGR_pct', 'Vol_pct', 'Sharpe',
                'Sortino', 'MaxDD_pct', 'Calmar', 'WinRate_pct', 'Trades',
                'AvgWin_pct', 'AvgLoss_pct', 'ProfitFactor']
        coin_df = pd.DataFrame([{k: r[k] for k in cols} for r in results])
        coin_df.insert(0, 'Coin', coin)
        all_rows.append(coin_df)

    # ── Combined CSV ──────────────────────────────────────────────────────
    out_csv = os.path.join(here, 'crypto_results.csv')
    pd.concat(all_rows, ignore_index=True).to_csv(out_csv, index=False)
    print(f'\n  Combined CSV → {out_csv}')

    # ── Cross-coin winner table ───────────────────────────────────────────
    combined = pd.concat(all_rows)
    print(f'\n{"="*70}')
    print('  OVERALL WINNER TABLE  (avg Sharpe across BTC + ETH)')
    print(f'{"="*70}')
    avg = (combined.groupby('Strategy')[['CAGR_pct','Sharpe','MaxDD_pct','WinRate_pct']]
                   .mean().sort_values('Sharpe', ascending=False))
    print(avg.round(2).to_string())
    print(f'{"="*70}')
    print('\nDone.')


if __name__ == '__main__':
    main()
