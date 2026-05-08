"""
Crypto F&O Trade Signal Visualizer
====================================
Shows every trade for the two best strategies with:
  • Entry price and date      (green ▲ long / red ▼ short)
  • Stop-Loss level           (red dashed line — Supertrend line / 1.5×ATR)
  • Target level              (green dashed line — 2:1 reward:risk)
  • Exit marker               (◎ target hit | ✕ stopped out | ◇ signal exit)
  • Shaded SL-to-target zone  (red below entry, green above)
  • Current live signal       (what to do TODAY)

Strategies charted:
  1. Supertrend 2x (long-only)  — best risk-adj on BTC
  2. EMA 9/21 + RSI (long/short) — both sides of market

Usage:
    python trade_signals.py           # live data
    python trade_signals.py --demo    # simulated
    python trade_signals.py --days 60 # zoom window (default 90)
"""

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.dates as mdates
from matplotlib.lines import Line2D
import warnings, os, argparse
from datetime import date, timedelta
warnings.filterwarnings('ignore')

START = '2023-01-01'
END   = (date.today() - timedelta(days=1)).strftime('%Y-%m-%d')
COINS = ['BTC-USD', 'ETH-USD']

# (overridden by CLI args in main)


# ── Data ─────────────────────────────────────────────────────────────────────

def fetch(coin):
    import yfinance as yf
    df = yf.download(coin, start=START, end=END, auto_adjust=True, progress=False)
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    return df[['Open','High','Low','Close','Volume']].dropna()

def simulate(seed=42):
    np.random.seed(seed)
    dates = pd.date_range(START, END, freq='B')
    T     = len(dates)
    rets  = np.random.normal(0.50/252, 0.75/np.sqrt(252), T)
    crash = np.random.choice(T, int(T*0.02), replace=False)
    rets[crash] -= np.random.uniform(0.05, 0.12, len(crash))
    px = 20000 * np.cumprod(np.exp(rets))
    hi = px * np.random.uniform(1.001, 1.04, T)
    lo = px * np.random.uniform(0.96, 0.999, T)
    op = np.roll(px,1); op[0]=px[0]
    vol = np.random.uniform(1e9,4e10,T)
    return pd.DataFrame({'Open':op,'High':hi,'Low':lo,'Close':px,'Volume':vol},index=dates)


# ── Indicators ────────────────────────────────────────────────────────────────

def ema(s, n):  return s.ewm(span=n, adjust=False).mean()

def rsi(s, n=14):
    d = s.diff()
    g = d.clip(lower=0).ewm(com=n-1, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(com=n-1, adjust=False).mean()
    return 100 - 100/(1 + g/l.replace(0, np.nan))

def atr_series(df, n=14):
    tr = pd.concat([(df['High']-df['Low']),
                    (df['High']-df['Close'].shift(1)).abs(),
                    (df['Low'] -df['Close'].shift(1)).abs()], axis=1).max(axis=1)
    return tr.ewm(span=n, adjust=False).mean()

def compute_supertrend(df, n=10, mult=3.5):
    """Returns (signal_series, supertrend_line_series)."""
    mid  = (df['High'] + df['Low']) / 2
    _atr = atr_series(df, n)
    bu_b = mid + mult * _atr
    bl_b = mid - mult * _atr
    bu   = bu_b.copy(); bl = bl_b.copy()
    st   = pd.Series(np.nan, index=df.index)
    for i in range(1, len(df)):
        bu.iloc[i] = (bu_b.iloc[i] if bu_b.iloc[i] < bu.iloc[i-1]
                       or df['Close'].iloc[i-1] > bu.iloc[i-1] else bu.iloc[i-1])
        bl.iloc[i] = (bl_b.iloc[i] if bl_b.iloc[i] > bl.iloc[i-1]
                       or df['Close'].iloc[i-1] < bl.iloc[i-1] else bl.iloc[i-1])
        prev = st.iloc[i-1]
        if pd.isna(prev):
            st.iloc[i] = bl.iloc[i]
        elif prev == bu.iloc[i-1]:
            st.iloc[i] = bl.iloc[i] if df['Close'].iloc[i] > bu.iloc[i] else bu.iloc[i]
        else:
            st.iloc[i] = bu.iloc[i] if df['Close'].iloc[i] < bl.iloc[i] else bl.iloc[i]
    sig = (df['Close'] > st).astype(int).shift(1).fillna(0)
    return sig, st

def compute_ema_rsi(df):
    """Returns signal_series (+1 long, -1 short, 0 flat)."""
    fast = ema(df['Close'], 9); slow = ema(df['Close'], 21)
    _rsi = rsi(df['Close'], 14)
    sig  = pd.Series(0, index=df.index)
    sig[fast > slow]  =  1
    sig[fast < slow]  = -1
    sig[(_rsi < 45) & (fast > slow)] = 0
    sig[(_rsi > 55) & (fast < slow)] = 0
    return sig.shift(1).fillna(0)


# ── Trade Extraction ──────────────────────────────────────────────────────────

def extract_trades(df, signal, st_line=None, rr=2.0, leverage=2):
    """
    Walk forward through signal, extract all trades with:
      entry, SL, target, exit price, exit type, P&L.
    SL  : Supertrend line at entry (trailing), or 1.5×ATR for EMA strategy
    Tgt : entry ± rr × risk  (2:1 by default)
    """
    price   = df['Close']
    _atr    = atr_series(df)
    trades  = []
    in_trade = False
    entry_date = entry_px = sl_px = target_px = direction = None
    trail_sl   = None

    for i in range(1, len(price)):
        date = price.index[i]
        px   = float(price.iloc[i])
        sig  = int(signal.iloc[i])
        curr_st = float(st_line.iloc[i]) if st_line is not None else None
        curr_atr = float(_atr.iloc[i])

        # ── inside a trade ────────────────────────────────────────────────
        if in_trade:
            # update trailing SL (only move in trade's favour)
            if st_line is not None:
                if direction == 1:
                    trail_sl = max(trail_sl, curr_st)   # move up for longs
                else:
                    trail_sl = min(trail_sl, curr_st)

            # check stop loss
            sl_hit = (direction == 1 and px <= trail_sl) or \
                     (direction == -1 and px >= trail_sl)
            # check target
            tgt_hit = (direction == 1 and px >= target_px) or \
                      (direction == -1 and px <= target_px)
            # check signal flip to close
            sig_exit = (direction == 1 and sig != 1) or \
                       (direction == -1 and sig != -1)

            if sl_hit:
                exit_px   = trail_sl
                exit_type = 'SL'
            elif tgt_hit:
                exit_px   = target_px
                exit_type = 'Target'
            elif sig_exit:
                exit_px   = px
                exit_type = 'Signal'
            else:
                continue

            pnl_pct = (exit_px / entry_px - 1) * direction * 100 * leverage
            trades.append({
                'direction':  'Long' if direction == 1 else 'Short',
                'entry_date': entry_date,
                'exit_date':  date,
                'entry':      round(entry_px, 2),
                'sl':         round(trail_sl, 2),
                'target':     round(target_px, 2),
                'exit':       round(exit_px, 2),
                'exit_type':  exit_type,
                'pnl_pct':    round(pnl_pct, 2),
                'win':        pnl_pct > 0,
            })
            in_trade = False

        # ── open new trade ────────────────────────────────────────────────
        if not in_trade and sig in (1, -1):
            direction  = sig
            entry_date = date
            entry_px   = px

            if st_line is not None:
                sl_px = curr_st
            else:
                sl_px = (entry_px - 1.5 * curr_atr) if direction == 1 \
                        else (entry_px + 1.5 * curr_atr)

            risk      = abs(entry_px - sl_px)
            target_px = entry_px + direction * rr * risk
            trail_sl  = sl_px
            in_trade  = True

    # close any open trade at end of data
    if in_trade:
        px = float(price.iloc[-1])
        pnl_pct = (px / entry_px - 1) * direction * 100 * leverage
        trades.append({
            'direction':  'Long' if direction == 1 else 'Short',
            'entry_date': entry_date,
            'exit_date':  price.index[-1],
            'entry':      round(entry_px, 2),
            'sl':         round(trail_sl, 2),
            'target':     round(target_px, 2),
            'exit':       round(px, 2),
            'exit_type':  'Open',
            'pnl_pct':    round(pnl_pct, 2),
            'win':        pnl_pct > 0,
        })

    return trades


# ── Plotting ──────────────────────────────────────────────────────────────────

def plot_strategy(df, trades, st_line, strategy_name, coin, zoom_days, out):
    price = df['Close']
    zoom_start = price.index[-zoom_days] if zoom_days < len(price) else price.index[0]
    zoom_trades = [t for t in trades if t['exit_date'] >= zoom_start
                   or t['entry_date'] >= zoom_start]

    fig, axes = plt.subplots(3, 1, figsize=(16, 14),
                              facecolor='#0d0d0d',
                              gridspec_kw={'height_ratios': [4, 1, 1.5], 'hspace': 0.38})

    def style(ax):
        ax.set_facecolor('#161616')
        ax.tick_params(colors='#888', labelsize=8)
        ax.spines[['top','right']].set_visible(False)
        ax.spines[['bottom','left']].set_color('#2a2a2a')
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%b %y'))
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))

    # ═══════════════════════════════════════════════════════════════════════
    # Panel 1: Price + Supertrend + trade markers (ZOOMED)
    # ═══════════════════════════════════════════════════════════════════════
    ax = axes[0]
    style(ax)
    zoom_px = price[price.index >= zoom_start]

    # price line
    ax.plot(zoom_px.index, zoom_px, color='#e0e0e0', lw=1.4, label='Price', zorder=3)

    # Supertrend line
    if st_line is not None:
        zoom_st = st_line[st_line.index >= zoom_start]
        bull = zoom_px > zoom_st
        for is_bull, grp in zoom_px.groupby((~bull).cumsum()):
            color = '#2ecc71' if (zoom_px.loc[grp.index] > zoom_st.loc[grp.index]).all() else '#e74c3c'
            ax.plot(grp.index, zoom_st.loc[grp.index],
                    color=color, lw=1.8, alpha=0.8, zorder=2)

    # ── Draw each trade's SL and target zone ───────────────────────────────
    for t in zoom_trades:
        e_date = max(t['entry_date'], zoom_start)
        x_date = t['exit_date']
        if e_date >= x_date:
            continue

        entry = t['entry']
        sl    = t['sl']
        tgt   = t['target']
        dirn  = 1 if t['direction'] == 'Long' else -1

        # shaded risk zone (entry → SL)
        ax.fill_between([e_date, x_date], sl, entry,
                        color='#e74c3c', alpha=0.08, zorder=1)
        # shaded reward zone (entry → target)
        ax.fill_between([e_date, x_date], entry, tgt,
                        color='#2ecc71', alpha=0.08, zorder=1)

        # SL dashed line
        ax.plot([e_date, x_date], [sl, sl],
                color='#e74c3c', lw=1.0, ls='--', alpha=0.7, zorder=4)
        ax.annotate(f'SL ${sl:,.0f}', xy=(e_date, sl),
                    xytext=(4, -10), textcoords='offset points',
                    color='#e74c3c', fontsize=6.5, va='top')

        # target dashed line
        ax.plot([e_date, x_date], [tgt, tgt],
                color='#2ecc71', lw=1.0, ls='--', alpha=0.7, zorder=4)
        ax.annotate(f'T ${tgt:,.0f}', xy=(e_date, tgt),
                    xytext=(4, 4), textcoords='offset points',
                    color='#2ecc71', fontsize=6.5)

        # entry marker
        e_marker = '^' if dirn == 1 else 'v'
        e_color  = '#2ecc71' if dirn == 1 else '#e74c3c'
        ax.scatter(t['entry_date'], entry, marker=e_marker, color=e_color,
                   s=90, zorder=6, label=('Long entry' if dirn == 1 else 'Short entry'))
        ax.annotate(f"{'L' if dirn==1 else 'S'} ${entry:,.0f}",
                    xy=(t['entry_date'], entry),
                    xytext=(6, 6 if dirn == 1 else -12), textcoords='offset points',
                    color=e_color, fontsize=6.5, fontweight='bold')

        # exit marker
        exit_markers = {'Target': ('*', '#FFD700', 120),
                        'SL':     ('X', '#e74c3c', 70),
                        'Signal': ('D', '#f39c12', 55),
                        'Open':   ('o', '#7F77DD', 55)}
        em, ec, es = exit_markers.get(t['exit_type'], ('o','#fff', 55))
        ax.scatter(t['exit_date'], t['exit'], marker=em, color=ec,
                   s=es, zorder=6, edgecolors='white', linewidth=0.5)
        pnl_color = '#2ecc71' if t['win'] else '#e74c3c'
        ax.annotate(f"{t['exit_type']} {t['pnl_pct']:+.1f}%",
                    xy=(t['exit_date'], t['exit']),
                    xytext=(6, -14), textcoords='offset points',
                    color=pnl_color, fontsize=6.5, fontweight='bold')

    ax.set_title(f'{coin}  ·  {strategy_name}  ·  Last {zoom_days} days  '
                 f'(entry ▲▼ | SL — — | Target - - | ★ hit | ✕ stopped)',
                 color='#ddd', fontsize=10, fontweight='bold', pad=10)
    ax.set_ylabel('Price (USD)', color='#999', fontsize=9)

    # custom legend
    legend_elements = [
        Line2D([0],[0], color='#e0e0e0', lw=1.4, label='Price'),
        Line2D([0],[0], color='#2ecc71', lw=2, label='Supertrend (bull)'),
        Line2D([0],[0], color='#e74c3c', lw=2, label='Supertrend (bear)'),
        mpatches.Patch(color='#2ecc71', alpha=0.3, label='Reward zone'),
        mpatches.Patch(color='#e74c3c', alpha=0.3, label='Risk zone'),
        Line2D([0],[0], marker='^', color='#2ecc71', ls='None', ms=8, label='Long entry'),
        Line2D([0],[0], marker='v', color='#e74c3c', ls='None', ms=8, label='Short entry'),
        Line2D([0],[0], marker='*', color='#FFD700', ls='None', ms=10, label='Target hit ★'),
        Line2D([0],[0], marker='X', color='#e74c3c', ls='None', ms=8, label='SL hit ✕'),
        Line2D([0],[0], marker='D', color='#f39c12', ls='None', ms=7, label='Signal exit ◇'),
        Line2D([0],[0], marker='o', color='#7F77DD', ls='None', ms=7, label='Trade open ●'),
    ]
    ax.legend(handles=legend_elements, framealpha=0, labelcolor='white',
              fontsize=7, ncol=4, loc='upper left')

    # ═══════════════════════════════════════════════════════════════════════
    # Panel 2: P&L per trade (bar chart)
    # ═══════════════════════════════════════════════════════════════════════
    ax2 = axes[1]
    style(ax2)
    if zoom_trades:
        xpos   = [t['entry_date'] for t in zoom_trades]
        pnls   = [t['pnl_pct']    for t in zoom_trades]
        colors = ['#2ecc71' if p > 0 else '#e74c3c' for p in pnls]
        ax2.bar(xpos, pnls, color=colors, width=10, edgecolor='none', alpha=0.8)
        ax2.axhline(0, color='#444', lw=0.8)
        ax2.set_ylabel('Trade P&L %', color='#999', fontsize=8)
        ax2.set_title('Per-trade P&L (with leverage)', color='#ddd', fontsize=9, pad=4)
        ax2.xaxis.set_major_formatter(mdates.DateFormatter('%b %y'))
        ax2.xaxis.set_major_locator(mdates.MonthLocator(interval=2))

    # ═══════════════════════════════════════════════════════════════════════
    # Panel 3: Full period equity + trade table text
    # ═══════════════════════════════════════════════════════════════════════
    ax3 = axes[2]
    style(ax3)

    # recent trades table
    recent = zoom_trades[-12:] if len(zoom_trades) > 12 else zoom_trades
    col_labels = ['Dir', 'Entry Date', 'Entry $', 'SL $', 'Target $',
                  'Exit $', 'Exit Type', 'P&L %', 'W/L']
    rows = [[t['direction'],
             t['entry_date'].strftime('%Y-%m-%d'),
             f"${t['entry']:>10,.0f}",
             f"${t['sl']:>10,.0f}",
             f"${t['target']:>10,.0f}",
             f"${t['exit']:>10,.0f}",
             t['exit_type'],
             f"{t['pnl_pct']:>+.1f}%",
             '✓ Win' if t['win'] else '✗ Loss']
            for t in recent]

    ax3.axis('off')
    if rows:
        tbl = ax3.table(cellText=rows, colLabels=col_labels,
                        loc='center', cellLoc='center')
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(7.5)
        tbl.scale(1, 1.4)
        # style header
        for j in range(len(col_labels)):
            tbl[0, j].set_facecolor('#1a1a2e')
            tbl[0, j].set_text_props(color='#ddd', fontweight='bold')
        # style rows
        for i, t in enumerate(recent, 1):
            bg = '#1a2a1a' if t['win'] else '#2a1a1a'
            for j in range(len(col_labels)):
                tbl[i, j].set_facecolor(bg)
                tbl[i, j].set_text_props(color='#ccc')
                tbl[i, j].set_edgecolor('#333')
    ax3.set_title(f'Recent trades  (last {len(recent)} shown)',
                  color='#ddd', fontsize=9, pad=8)

    plt.suptitle(f'{coin}  ·  {strategy_name}  ·  {START} → {END}  ·  '
                 f'{len(trades)} total trades  ·  2:1 Risk-Reward',
                 color='white', fontsize=12, fontweight='bold', y=0.98)
    plt.savefig(out, dpi=150, bbox_inches='tight', facecolor='#0d0d0d')
    print(f'  Chart → {out}')
    plt.close()


# ── Current Signal Status ─────────────────────────────────────────────────────

def print_current_signal(df, coin, st_sig, st_line, er_sig):
    price  = df['Close']
    last   = price.index[-1]
    px     = float(price.iloc[-1])
    _atr   = atr_series(df)
    a      = float(_atr.iloc[-1])

    # Supertrend
    st_now  = int(st_sig.iloc[-1])
    st_val  = float(st_line.iloc[-1])
    st_risk = abs(px - st_val)
    st_tgt  = px + 2 * st_risk if st_now == 1 else px - 2 * st_risk

    # EMA+RSI
    er_now  = int(er_sig.iloc[-1])
    er_sl   = (px - 1.5*a) if er_now == 1 else (px + 1.5*a)
    er_tgt  = px + 2*1.5*a if er_now == 1 else px - 2*1.5*a

    def dir_str(s): return '🟢 LONG' if s==1 else ('🔴 SHORT' if s==-1 else '⚪ FLAT')

    print(f'\n  ┌─────────────────────────────────────────────────────┐')
    print(f'  │  LIVE SIGNAL  —  {coin}  as of {last.date()}         │')
    print(f'  ├─────────────────────────────────────────────────────┤')
    print(f'  │  Current price : ${px:>12,.2f}                       │')
    print(f'  ├─────────────────────────────────────────────────────┤')
    print(f'  │  Supertrend 2x : {dir_str(st_now):<8}                       │')
    if st_now == 1:
        print(f'  │    Entry now   : ${px:>12,.2f}                       │')
        print(f'  │    Stop-Loss   : ${st_val:>12,.2f}  (Supertrend line)  │')
        print(f'  │    Target 2:1  : ${st_tgt:>12,.2f}                       │')
        print(f'  │    Risk/trade  : ${st_risk:>12,.2f}  ({st_risk/px*100:.1f}% of price)   │')
    elif st_now == 0:
        print(f'  │    Waiting for Supertrend breakout above line     │')
        print(f'  │    Supertrend : ${st_val:>12,.2f}                       │')
    print(f'  ├─────────────────────────────────────────────────────┤')
    print(f'  │  EMA+RSI 2x   : {dir_str(er_now):<8}                       │')
    if er_now != 0:
        print(f'  │    Entry now   : ${px:>12,.2f}                       │')
        print(f'  │    Stop-Loss   : ${er_sl:>12,.2f}  (1.5×ATR)          │')
        print(f'  │    Target 2:1  : ${er_tgt:>12,.2f}                       │')
        print(f'  │    ATR (risk)  : ${a:>12,.2f}                       │')
    else:
        print(f'  │    EMA 9 < EMA 21 — waiting for crossover         │')
    print(f'  └─────────────────────────────────────────────────────┘')


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Crypto trade signal chart with SL / Target',
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""Examples:
  python trade_signals.py                                    # defaults
  python trade_signals.py --start 2024-01-01                # from Jan 2024
  python trade_signals.py --start 2024-01-01 --end 2024-12-31   # full year 2024
  python trade_signals.py --end 2024-06-30                  # up to mid-2024
  python trade_signals.py --coin BTC-USD                    # BTC only
  python trade_signals.py --coin ETH-USD --days 60          # ETH, 60-day zoom
  python trade_signals.py --start 2025-01-01 --coin BTC-USD --days 120
        """)
    parser.add_argument('--demo',  action='store_true', help='Simulated data (offline)')
    parser.add_argument('--start', type=str, default=None, help='Start date YYYY-MM-DD')
    parser.add_argument('--end',   type=str, default=None, help='End date   YYYY-MM-DD')
    parser.add_argument('--coin',  type=str, default=None, help='e.g. BTC-USD or ETH-USD')
    parser.add_argument('--days',  type=int, default=90,
                        help='Zoom window in days (default 90)')
    args  = parser.parse_args()

    global START, END, COINS
    if args.start: START = args.start
    if args.end:   END   = args.end
    if args.coin:  COINS = [args.coin]

    here  = os.path.dirname(os.path.abspath(__file__))

    print('=' * 60)
    print(f'  Trade Signal Visualizer  |  {START} → {END}')
    print('=' * 60)

    for coin_idx, coin in enumerate(COINS):
        print(f'\n  {coin}')
        if args.demo:
            df = simulate(seed=42 + coin_idx)
        else:
            try:
                df = fetch(coin)
            except Exception as e:
                print(f'  yfinance error ({e}) — simulating')
                df = simulate(seed=42 + coin_idx)

        # ── Compute indicators ─────────────────────────────────────────
        st_sig, st_line = compute_supertrend(df)
        er_sig          = compute_ema_rsi(df)

        # ── Extract trades ─────────────────────────────────────────────
        st_trades = extract_trades(df, st_sig, st_line=st_line, rr=2.0, leverage=2)
        er_trades = extract_trades(df, er_sig, st_line=None,    rr=2.0, leverage=2)

        # ── Stats ──────────────────────────────────────────────────────
        for name, trades in [('Supertrend 2x', st_trades), ('EMA+RSI 2x L/S', er_trades)]:
            n      = len(trades)
            wins   = [t for t in trades if t['win']]
            losses = [t for t in trades if not t['win']]
            avg_w  = np.mean([t['pnl_pct'] for t in wins])   if wins   else 0
            avg_l  = np.mean([t['pnl_pct'] for t in losses]) if losses else 0
            pf     = abs(avg_w / avg_l) if avg_l else 0
            print(f'    {name:<20}  {n:3d} trades  '
                  f'WinRate {len(wins)/max(n,1)*100:.0f}%  '
                  f'AvgWin {avg_w:+.1f}%  AvgLoss {avg_l:+.1f}%  '
                  f'ProfitFactor {pf:.2f}')

        # ── Current live signal ────────────────────────────────────────
        print_current_signal(df, coin, st_sig, st_line, er_sig)

        # ── Trade detail table ─────────────────────────────────────────
        print(f'\n  {'─'*70}')
        print(f'  ALL TRADES — {coin} — Supertrend 2x (last 10)')
        print(f'  {'─'*70}')
        hdr = f"  {'Dir':<6} {'Entry Date':<12} {'Entry $':>11} {'SL $':>11} {'Target $':>11} {'Exit $':>11} {'Type':<8} {'P&L %':>7} {'W/L'}"
        print(hdr)
        print(f"  {'─'*70}")
        for t in st_trades[-10:]:
            wl  = '✓ Win ' if t['win'] else '✗ Loss'
            print(f"  {t['direction']:<6} {str(t['entry_date'].date()):<12} "
                  f"${t['entry']:>10,.0f} ${t['sl']:>10,.0f} "
                  f"${t['target']:>10,.0f} ${t['exit']:>10,.0f} "
                  f"{t['exit_type']:<8} {t['pnl_pct']:>+6.1f}% {wl}")

        # ── Plot ───────────────────────────────────────────────────────
        for name, trades, sl_line_arg in [
            ('Supertrend 2x',   st_trades, st_line),
            ('EMA+RSI 2x L/S',  er_trades, None),
        ]:
            out = os.path.join(here, f'signals_{coin.replace("-","_")}_{name.replace(" ","_").replace("/","")}.png')
            plot_strategy(df, trades, sl_line_arg, name, coin, args.days, out)

    print('\nDone.')


if __name__ == '__main__':
    main()
