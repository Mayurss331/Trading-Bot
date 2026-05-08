"""
Crypto Intraday Signal Analyzer  —  IST Timezone
==================================================
Downloads 1-hour crypto data, converts to IST (UTC+5:30),
applies Supertrend + EMA 9/21 + RSI on hourly candles,
and shows every signal for the chosen date with IST timestamps.

Output:
  • Hourly signal table for the target date in IST
  • Open trade details: entry, SL, target
  • Intraday chart with Supertrend line, SL/target zones, entry/exit markers
  • Day summary: high, low, range, trend direction

Usage:
    python intraday_ist.py                          # Apr 10 2025 (default), both coins
    python intraday_ist.py --date 2025-04-10        # explicit date
    python intraday_ist.py --coin BTC-USD           # BTC only
    python intraday_ist.py --date 2025-03-15 --coin ETH-USD
"""

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.patches as mpatches
from matplotlib.lines import Line2D
import warnings, os, argparse
from datetime import datetime, date, timedelta
import pytz
warnings.filterwarnings('ignore')

IST       = pytz.timezone('Asia/Kolkata')
UTC       = pytz.utc
TARGET_DATE = '2025-04-10'
COINS     = ['BTC-USD', 'ETH-USD']
TIMEFRAME = '1h'
BAR_FREQ  = '1h'
WARMUP_DAYS = 60          # days of data before target date for indicator warmup


# ── Data ─────────────────────────────────────────────────────────────────────

def fetch_hourly(coin: str, target_date: str) -> pd.DataFrame:
    import yfinance as yf
    t     = pd.Timestamp(target_date)
    start = (t - pd.Timedelta(days=WARMUP_DAYS)).strftime('%Y-%m-%d')
    end   = (t + pd.Timedelta(days=2)).strftime('%Y-%m-%d')        # +1 to include full day
    df    = yf.download(coin, start=start, end=end,
                        interval=TIMEFRAME, auto_adjust=True, progress=False)
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df    = df[['Open','High','Low','Close','Volume']].dropna()
    # convert UTC → IST
    if df.index.tz is None:
        df.index = df.index.tz_localize('UTC')
    df.index = df.index.tz_convert(IST)
    return df


# ── Indicators (hourly) ───────────────────────────────────────────────────────

def ema(s, n):  return s.ewm(span=n, adjust=False).mean()

def rsi(s, n=14):
    d = s.diff()
    g = d.clip(lower=0).ewm(com=n-1, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(com=n-1, adjust=False).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))

def atr_s(df, n=14):
    tr = pd.concat([(df['High']-df['Low']),
                    (df['High']-df['Close'].shift(1)).abs(),
                    (df['Low'] -df['Close'].shift(1)).abs()], axis=1).max(axis=1)
    return tr.ewm(span=n, adjust=False).mean()

def compute_supertrend(df, n=10, mult=3.5):
    mid  = (df['High'] + df['Low']) / 2
    _atr = atr_s(df, n)
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
        if pd.isna(prev):     st.iloc[i] = bl.iloc[i]
        elif prev == bu.iloc[i-1]:
            st.iloc[i] = bl.iloc[i] if df['Close'].iloc[i] > bu.iloc[i] else bu.iloc[i]
        else:
            st.iloc[i] = bu.iloc[i] if df['Close'].iloc[i] < bl.iloc[i] else bl.iloc[i]
    sig = (df['Close'] > st).astype(int)
    return sig, st

def compute_ema_rsi(df):
    fast = ema(df['Close'], 9); slow = ema(df['Close'], 21)
    _rsi = rsi(df['Close'], 14)
    sig  = pd.Series(0, index=df.index)
    sig[fast > slow]  =  1
    sig[fast < slow]  = -1
    sig[(_rsi < 45) & (fast > slow)] = 0
    sig[(_rsi > 55) & (fast < slow)] = 0
    return sig, fast, slow, _rsi

def macd_line(df):
    return ema(df['Close'], 12) - ema(df['Close'], 26)


# ── Build signal rows ─────────────────────────────────────────────────────────

def build_signal_table(df_day, st_sig, st_line, er_sig,
                       ema_fast, ema_slow, _rsi, _atr) -> pd.DataFrame:
    rows = []
    prev_st = None; prev_er = None

    for i, ts in enumerate(df_day.index):
        o_val  = float(df_day['Open'].iloc[i])
        h_val  = float(df_day['High'].iloc[i])
        l_val  = float(df_day['Low'].iloc[i])
        px     = float(df_day['Close'].iloc[i])
        v_val  = float(df_day['Volume'].iloc[i])
        st_now = int(st_sig.loc[ts])
        er_now = int(er_sig.loc[ts])
        st_val = float(st_line.loc[ts])
        r_val  = float(_rsi.loc[ts]) if not np.isnan(_rsi.loc[ts]) else 50
        a_val  = float(_atr.loc[ts])
        ef     = float(ema_fast.loc[ts])
        es     = float(ema_slow.loc[ts])

        # action
        st_action = ''
        if prev_st is not None:
            if st_now == 1 and prev_st == 0:  st_action = '🟢 LONG ENTRY'
            elif st_now == 0 and prev_st == 1: st_action = '🔴 EXIT LONG'
        if st_action == '' and st_now == 1: st_action = '↑ Hold Long'
        if st_action == '' and st_now == 0: st_action = '— Flat/Cash'

        er_action = ''
        if prev_er is not None:
            if er_now == 1 and prev_er != 1:   er_action = '🟢 LONG'
            elif er_now == -1 and prev_er != -1: er_action = '🔴 SHORT'
            elif er_now == 0 and prev_er != 0:   er_action = '⚪ EXIT'
        if er_action == '':
            er_action = ('↑ Long' if er_now == 1 else
                         '↓ Short' if er_now == -1 else '— Flat')

        # SL / Target for open trades (Supertrend)
        sl  = round(st_val, 2)
        rsk = abs(px - sl)
        tgt = round(px + 2 * rsk, 2) if st_now == 1 else round(px - 2 * rsk, 2)

        rows.append({
            'IST Time':        ts.strftime('%I:%M %p'),
            'IST DateTime':    ts.strftime('%Y-%m-%d %H:%M'),
            'Open ($)':        f'{o_val:>10,.2f}',
            'High ($)':        f'{h_val:>10,.2f}',
            'Low ($)':         f'{l_val:>10,.2f}',
            'Price ($)':       f'{px:>10,.2f}',
            'Volume':          f'{v_val:>12,.0f}',
            'ST Signal':       st_action,
            'ST Line':         f'{st_val:>10,.2f}',
            'SL ($)':          f'{sl:>10,.2f}' if st_now == 1 else '    —',
            'Target ($)':      f'{tgt:>10,.2f}' if st_now == 1 else '    —',
            'RSI':             f'{r_val:>5.1f}',
            'EMA Signal':      er_action,
            '_px':             px,
            '_st':             st_now,
            '_er':             er_now,
            '_sl':             sl,
            '_tgt':            tgt,
            '_ts':             ts,
        })
        prev_st = st_now; prev_er = er_now

    return pd.DataFrame(rows)


# ── Chart ─────────────────────────────────────────────────────────────────────

def plot_intraday(df_day, sig_df, st_line_day, coin, target_date, out):
    fig, axes = plt.subplots(3, 1, figsize=(16, 13),
                              facecolor='#0d0d0d',
                              gridspec_kw={'height_ratios': [4, 1.2, 1.2],
                                           'hspace': 0.38})

    def style(ax):
        ax.set_facecolor('#161616')
        ax.tick_params(colors='#888', labelsize=8)
        ax.spines[['top','right']].set_visible(False)
        ax.spines[['bottom','left']].set_color('#2a2a2a')
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M', tz=IST))
        ax.xaxis.set_major_locator(mdates.HourLocator(interval=2, tz=IST))

    times  = sig_df['_ts'].tolist()
    prices = sig_df['_px'].tolist()
    st_sig = sig_df['_st'].tolist()

    # ── Panel 1: Price + Supertrend + entries/exits/SL/Target ─────────────
    ax = axes[0]
    style(ax)

    ax.plot(times, prices, color='#e0e0e0', lw=1.6, zorder=3, label='BTC Price')

    # Supertrend line — green when bullish, red when bearish
    for i in range(1, len(times)):
        color = '#2ecc71' if st_sig[i] == 1 else '#e74c3c'
        ax.plot([times[i-1], times[i]],
                [sig_df['_sl'].iloc[i-1], sig_df['_sl'].iloc[i]],
                color=color, lw=2.0, alpha=0.8, zorder=2)

    # SL / Target shaded zones and markers
    in_trade = False; entry_t = entry_px = sl_px = tgt_px = None
    for i, row in sig_df.iterrows():
        ts = row['_ts']; px = row['_px']; st = row['_st']
        sl = row['_sl']; tgt = row['_tgt']

        if not in_trade and st == 1:
            in_trade = True; entry_t = ts; entry_px = px; sl_px = sl; tgt_px = tgt
            ax.scatter(ts, px, marker='^', color='#2ecc71', s=120, zorder=6)
            ax.annotate(f'LONG\n${px:,.0f}', xy=(ts, px),
                        xytext=(6, 8), textcoords='offset points',
                        color='#2ecc71', fontsize=7.5, fontweight='bold')
        elif in_trade and st == 0:
            in_trade = False
            ax.scatter(ts, px, marker='v', color='#f39c12', s=100, zorder=6)
            ax.annotate(f'EXIT\n${px:,.0f}', xy=(ts, px),
                        xytext=(6, -18), textcoords='offset points',
                        color='#f39c12', fontsize=7.5)

        if in_trade and entry_t is not None:
            ax.fill_between([entry_t, ts], sl_px, entry_px,
                            color='#e74c3c', alpha=0.06, zorder=1)
            ax.fill_between([entry_t, ts], entry_px, tgt_px,
                            color='#2ecc71', alpha=0.06, zorder=1)

    # SL line for last open trade
    if in_trade:
        ax.axhline(sl_px,  color='#e74c3c', lw=1.2, ls='--', alpha=0.8)
        ax.axhline(tgt_px, color='#2ecc71', lw=1.2, ls='--', alpha=0.8)
        ax.annotate(f'SL  ${sl_px:,.0f}', xy=(times[-1], sl_px),
                    xytext=(-100, -12), textcoords='offset points',
                    color='#e74c3c', fontsize=8, fontweight='bold')
        ax.annotate(f'TGT ${tgt_px:,.0f}', xy=(times[-1], tgt_px),
                    xytext=(-100, 6), textcoords='offset points',
                    color='#2ecc71', fontsize=8, fontweight='bold')

    day_high = max(prices); day_low = min(prices)
    ax.axhline(day_high, color='#888', lw=0.6, ls=':', alpha=0.5)
    ax.axhline(day_low,  color='#888', lw=0.6, ls=':', alpha=0.5)

    legend_el = [
        Line2D([0],[0], color='#e0e0e0', lw=1.6,  label='Price'),
        Line2D([0],[0], color='#2ecc71', lw=2,    label='Supertrend (Bull)'),
        Line2D([0],[0], color='#e74c3c', lw=2,    label='Supertrend (Bear)'),
        Line2D([0],[0], marker='^', color='#2ecc71', ls='None', ms=9, label='Long Entry'),
        Line2D([0],[0], marker='v', color='#f39c12', ls='None', ms=9, label='Exit'),
        mpatches.Patch(color='#2ecc71', alpha=0.3, label='Reward zone'),
        mpatches.Patch(color='#e74c3c', alpha=0.3, label='Risk zone'),
    ]
    ax.legend(handles=legend_el, framealpha=0, labelcolor='white',
              fontsize=8, ncol=4, loc='upper left')
    ax.set_ylabel('Price  (USD)', color='#999', fontsize=9)
    ax.set_title(f'{coin}  ·  {TIMEFRAME} Intraday  ·  {target_date}  ·  IST  '
                 f'(Day High ${day_high:,.0f}  |  Day Low ${day_low:,.0f}  '
                 f'|  Range ${day_high-day_low:,.0f})',
                 color='#ddd', fontsize=10, fontweight='bold', pad=10)

    # ── Panel 2: RSI ───────────────────────────────────────────────────────
    ax2 = axes[1]
    style(ax2)
    rsi_vals = [float(sig_df['RSI'].iloc[i].strip()) for i in range(len(sig_df))]
    ax2.plot(times, rsi_vals, color='#7F77DD', lw=1.5)
    ax2.axhline(70, color='#e74c3c', lw=0.8, ls='--', alpha=0.6)
    ax2.axhline(30, color='#2ecc71', lw=0.8, ls='--', alpha=0.6)
    ax2.axhline(50, color='#555',    lw=0.6, ls=':',  alpha=0.5)
    ax2.fill_between(times, rsi_vals, 70,
                     where=[r > 70 for r in rsi_vals], color='#e74c3c', alpha=0.3)
    ax2.fill_between(times, rsi_vals, 30,
                     where=[r < 30 for r in rsi_vals], color='#2ecc71', alpha=0.3)
    ax2.set_ylim(0, 100)
    ax2.set_ylabel('RSI (14)', color='#999', fontsize=9)
    ax2.set_title('RSI  (>70 overbought  |  <30 oversold)', color='#ddd', fontsize=9, pad=4)

    # ── Panel 3: EMA 9 vs EMA 21 ──────────────────────────────────────────
    ax3 = axes[2]
    style(ax3)
    ax3.plot(times, prices,                        color='#e0e0e0', lw=1.0, alpha=0.5)
    ax3.plot(times, sig_df['_sl'].tolist(),
             color='#EF9F27', lw=1.5, label='Supertrend line', alpha=0.9)
    # colour background by EMA signal
    er_vals = sig_df['_er'].tolist()
    for i in range(1, len(times)):
        c = '#2ecc71' if er_vals[i] == 1 else ('#e74c3c' if er_vals[i] == -1 else '#333')
        ax3.axvspan(times[i-1], times[i], color=c, alpha=0.12)
    ax3.set_ylabel('EMA Signal', color='#999', fontsize=9)
    ax3.set_title('EMA 9/21 + RSI regime  (green=Long  |  red=Short  |  grey=Flat)',
                  color='#ddd', fontsize=9, pad=4)

    plt.suptitle(f'Crypto Intraday  ·  {coin}  ·  {target_date}  ·  All times in IST (UTC+5:30)',
                 color='white', fontsize=12, fontweight='bold', y=0.98)
    fig.autofmt_xdate(rotation=0, ha='center')
    plt.savefig(out, dpi=150, bbox_inches='tight', facecolor='#0d0d0d')
    print(f'  Chart → {out}')
    plt.close()


# ── Summary print ─────────────────────────────────────────────────────────────

def print_day_summary(coin, target_date, sig_df, df_day):
    prices   = sig_df['_px'].tolist()
    open_px  = prices[0]; close_px = prices[-1]
    high_px  = max(prices); low_px = min(prices)
    day_ret  = (close_px / open_px - 1) * 100

    # find last open trade
    last_st  = int(sig_df['_st'].iloc[-1])
    last_sl  = float(sig_df['_sl'].iloc[-1])
    last_tgt = float(sig_df['_tgt'].iloc[-1])
    last_px  = float(sig_df['_px'].iloc[-1])
    last_er  = int(sig_df['_er'].iloc[-1])
    last_ts  = sig_df['_ts'].iloc[-1].strftime('%I:%M %p IST')

    print(f'\n  ╔══════════════════════════════════════════════════════════╗')
    print(f'  ║  {coin}  ·  {target_date}  ·  IST Summary              ')
    print(f'  ╠══════════════════════════════════════════════════════════╣')
    print(f'  ║  Open   ({sig_df["_ts"].iloc[0].strftime("%H:%M IST")}) : ${open_px:>12,.2f}                    ')
    print(f'  ║  High               : ${high_px:>12,.2f}                    ')
    print(f'  ║  Low                : ${low_px:>12,.2f}                    ')
    print(f'  ║  Close  ({sig_df["_ts"].iloc[-1].strftime("%H:%M IST")}) : ${close_px:>12,.2f}  ({day_ret:+.2f}%)   ')
    print(f'  ║  Range              : ${high_px-low_px:>12,.2f}                    ')
    print(f'  ╠══════════════════════════════════════════════════════════╣')
    print(f'  ║  SIGNALS at {last_ts}                         ')
    print(f'  ╠══════════════════════════════════════════════════════════╣')

    st_str = '🟢 LONG' if last_st == 1 else '🔴 FLAT'
    print(f'  ║  Supertrend ({TIMEFRAME})    : {st_str}                         ')
    if last_st == 1:
        risk = abs(last_px - last_sl)
        print(f'  ║    Entry  (current): ${last_px:>12,.2f}                    ')
        print(f'  ║    Stop-Loss       : ${last_sl:>12,.2f}  ({abs(last_px-last_sl)/last_px*100:.1f}% risk)  ')
        print(f'  ║    Target  (2:1)   : ${last_tgt:>12,.2f}  ({abs(last_tgt-last_px)/last_px*100:.1f}% reward)')

    er_str = '🟢 LONG' if last_er == 1 else ('🔴 SHORT' if last_er == -1 else '⚪ FLAT')
    print(f'  ║  EMA 9/21 + RSI     : {er_str}                         ')
    print(f'  ╚══════════════════════════════════════════════════════════╝')

    print(f'\n  SIGNAL TABLE ({TIMEFRAME})  —  {coin}  —  {target_date}  (IST)')
    print(f'  {"─"*165}')
    header = (f"  {'IST Timestamp':<17} {'Open':>10} {'High':>10} {'Low':>10} {'Close':>10} {'Volume':>12}  "
              f"{'Supertrend Action':<20} {'SL ($)':>12} {'Target ($)':>12} {'RSI':>6}  {'EMA+RSI'}")
    print(header)
    print(f'  {"─"*165}')
    for _, row in sig_df.iterrows():
        entry_mark = '◀ ENTER' if '🟢 LONG ENTRY' in row['ST Signal'] else \
                     '◀ EXIT ' if '🔴 EXIT'       in row['ST Signal'] else ''
        print(f"  {row['IST DateTime']:<17} {row['Open ($)']:>10} {row['High ($)']:>10} "
              f"{row['Low ($)']:>10} {row['Price ($)']:>10} {row['Volume']:>12}  "
              f"{row['ST Signal']:<22} {row['SL ($)']:>12} {row['Target ($)']:>12} "
              f"{row['RSI']:>6}  {row['EMA Signal']:<14} {entry_mark}")
    print(f'  {"─"*165}')


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Crypto intraday signal analyzer — IST timezone',
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""Examples:
  python intraday_ist.py                          # April 10 2025, BTC + ETH
  python intraday_ist.py --date 2025-04-10        # explicit date
  python intraday_ist.py --coin BTC-USD           # BTC only
  python intraday_ist.py --date 2025-03-20 --coin ETH-USD
        """)
    parser.add_argument('--date', type=str, default=TARGET_DATE,
                        help='Date to analyze  YYYY-MM-DD  (default: 2025-04-10)')
    parser.add_argument('--coin', type=str, default=None,
                        help='e.g. BTC-USD or ETH-USD  (default: both)')
    args = parser.parse_args()

    target_date = args.date
    coins       = [args.coin] if args.coin else COINS
    here        = os.path.dirname(os.path.abspath(__file__))

    print('=' * 62)
    print(f'  Intraday Signal Analyzer  |  {target_date}  |  IST (UTC+5:30)')
    print('=' * 62)

    for coin in coins:
        print(f'\n  Downloading {TIMEFRAME} data for {coin} ...')
        try:
            df = fetch_hourly(coin, target_date)
        except Exception as e:
            print(f'  [!] Failed: {e}')
            continue

        # ── filter to target date in IST ──────────────────────────────────
        target_ist = pd.Timestamp(target_date).tz_localize(IST)
        df_day = df[df.index.date == target_ist.date()]

        # For today's date, keep only closed candles up to "now"
        now_utc = pd.Timestamp.now(tz='UTC')
        if target_ist.date() == now_utc.tz_convert(IST).date():
            latest_closed_ist = now_utc.floor(BAR_FREQ).tz_convert(IST)
            df_day = df_day[df_day.index <= latest_closed_ist]
            if not df_day.empty:
                print(f'  Live mode: using closed candles up to '
                      f'{latest_closed_ist.strftime("%Y-%m-%d %H:%M")} IST')

        if df_day.empty:
            print(f'  No data found for {target_date} in IST. '
                  f'(Market may have been closed or data not available)')
            continue

        print(f'  {len(df_day)} candles ({TIMEFRAME}) on {target_date} IST  '
              f'({df_day.index[0].strftime("%H:%M")} → {df_day.index[-1].strftime("%H:%M")} IST)')

        # ── compute indicators on full warmup window, then slice day ──────
        st_sig_full, st_line_full = compute_supertrend(df)
        er_sig_full, ef_full, es_full, rsi_full = compute_ema_rsi(df)
        atr_full = atr_s(df)

        st_sig  = st_sig_full.reindex(df_day.index)
        st_line = st_line_full.reindex(df_day.index)
        er_sig  = er_sig_full.reindex(df_day.index)
        ema_f   = ef_full.reindex(df_day.index)
        ema_s   = es_full.reindex(df_day.index)
        _rsi    = rsi_full.reindex(df_day.index)
        _atr    = atr_full.reindex(df_day.index)

        # ── build signal table ────────────────────────────────────────────
        sig_df = build_signal_table(df_day, st_sig, st_line,
                                    er_sig, ema_f, ema_s, _rsi, _atr)

        # ── print summary ─────────────────────────────────────────────────
        print_day_summary(coin, target_date, sig_df, df_day)

        # ── chart ─────────────────────────────────────────────────────────
        out = os.path.join(here, f'intraday_{coin.replace("-","_")}_{target_date}.png')
        plot_intraday(df_day, sig_df, st_line, coin, target_date, out)

    print('\nDone.')


if __name__ == '__main__':
    main()
