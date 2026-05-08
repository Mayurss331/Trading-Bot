"""
Multi-Strategy Confluence Signal Analyzer  —  IST Timezone
============================================================
Runs 5 strategies simultaneously on 5m data and scores each candle.
Only take a trade when 3+ strategies agree → dramatically cuts false signals.

Strategies (each votes +1 bullish / -1 bearish / 0 neutral):
  1. Supertrend      (ATR-10, mult 3.5)
  2. EMA 9/21 + RSI  (fast/slow crossover + RSI confirmation)
  3. MACD            (12,26,9 — line above/below signal)
  4. Bollinger Bands (price vs midband + squeeze breakout)
  5. RSI             (momentum strength filter)

Confluence Score  = sum of all 5 votes  (-5 to +5)
  +4 / +5  →  🟢🟢 STRONG LONG    → enter FULL size
  +2 / +3  →  🟢   MODERATE LONG  → enter HALF size
   0 / ±1  →  🟡   NEUTRAL        → NO TRADE (conflicting)
  -2 / -3  →  🔴   MODERATE SHORT → small short or stay flat
  -4 / -5  →  🔴🔴 STRONG SHORT   → short or exit all longs

Usage:
    python confluence_ist.py                           # April 10 2025, BTC + ETH
    python confluence_ist.py --date 2025-04-10
    python confluence_ist.py --date 2025-04-10 --coin BTC-USD
"""

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import matplotlib.dates as mdates
import matplotlib.patches as mpatches
from matplotlib.lines import Line2D
import warnings, os, argparse
from datetime import date, timedelta
import pytz
warnings.filterwarnings('ignore')

IST         = pytz.timezone('Asia/Kolkata')
TARGET_DATE = '2025-04-10'
COINS       = ['BTC-USD', 'ETH-USD']
TIMEFRAME   = '5m'
BAR_FREQ    = '5min'
BAR_MINUTES = 5
WARMUP_DAYS = 30
LONG_ENTRY_SCORE  = 3
LONG_EXIT_SCORE   = 2
SHORT_ENTRY_SCORE = -3
SHORT_EXIT_SCORE  = -2


# ══════════════════════════════════════════════════════════════════════════════
# DATA
# ══════════════════════════════════════════════════════════════════════════════

def fetch(coin, target_date):
    import yfinance as yf
    t     = pd.Timestamp(target_date)
    start = (t - pd.Timedelta(days=WARMUP_DAYS)).strftime('%Y-%m-%d')
    end   = (t + pd.Timedelta(days=2)).strftime('%Y-%m-%d')
    df    = yf.download(coin, start=start, end=end,
                        interval=TIMEFRAME, auto_adjust=True, progress=False)
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df    = df[['Open','High','Low','Close','Volume']].dropna()
    if df.index.tz is None:
        df.index = df.index.tz_localize('UTC')
    df.index = df.index.tz_convert(IST)
    return df


# ══════════════════════════════════════════════════════════════════════════════
# INDICATORS
# ══════════════════════════════════════════════════════════════════════════════

def ema(s, n):  return s.ewm(span=n, adjust=False).mean()
def sma(s, n):  return s.rolling(n).mean()

def rsi_s(s, n=14):
    d = s.diff()
    g = d.clip(lower=0).ewm(com=n-1, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(com=n-1, adjust=False).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))

def atr_s(df, n=14):
    tr = pd.concat([(df['High']-df['Low']),
                    (df['High']-df['Close'].shift(1)).abs(),
                    (df['Low'] -df['Close'].shift(1)).abs()], axis=1).max(axis=1)
    return tr.ewm(span=n, adjust=False).mean()

def supertrend(df, n=10, mult=3.5):
    mid  = (df['High'] + df['Low']) / 2
    _atr = atr_s(df, n)
    bu_b = mid + mult * _atr; bl_b = mid - mult * _atr
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
    return (df['Close'] > st).astype(int), st   # signal, line

def macd_signals(df):
    line   = ema(df['Close'], 12) - ema(df['Close'], 26)
    signal = ema(line, 9)
    hist   = line - signal
    return line, signal, hist

def bollinger(df, n=20, k=2.0):
    mid  = sma(df['Close'], n)
    std  = df['Close'].rolling(n).std()
    return mid - k*std, mid, mid + k*std


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGY VOTES  (each returns +1, 0, or -1 per bar)
# ══════════════════════════════════════════════════════════════════════════════

def vote_supertrend(df):
    sig, line = supertrend(df)
    return sig.map({1: 1, 0: -1}), line          # long=+1, flat=-1

def vote_ema_rsi(df):
    fast = ema(df['Close'], 9); slow = ema(df['Close'], 21)
    _rsi = rsi_s(df['Close'], 14)
    v = pd.Series(0, index=df.index)
    v[(fast > slow) & (_rsi > 45)] =  1
    v[(fast < slow) & (_rsi < 55)] = -1
    return v

def vote_macd(df):
    line, signal, hist = macd_signals(df)
    v = pd.Series(0, index=df.index)
    v[hist > 0] =  1
    v[hist < 0] = -1
    return v

def vote_bollinger(df):
    lo, mid, hi = bollinger(df)
    v = pd.Series(0, index=df.index)
    v[df['Close'] > mid] =  1    # above midband = bullish
    v[df['Close'] < mid] = -1    # below midband = bearish
    # bonus: breakout above upper → strong bull; below lower → strong bear
    v[df['Close'] > hi] =  1
    v[df['Close'] < lo] = -1
    return v

def vote_rsi(df):
    _rsi = rsi_s(df['Close'], 14)
    v = pd.Series(0, index=df.index)
    v[(_rsi > 50) & (_rsi < 70)] =  1   # trending bullish
    v[(_rsi < 50) & (_rsi > 30)] = -1   # trending bearish
    v[_rsi >= 70] =  0                   # overbought → neutral (could reverse)
    v[_rsi <= 30] =  0                   # oversold   → neutral (could bounce)
    return v


# ══════════════════════════════════════════════════════════════════════════════
# CONFLUENCE SCORING
# ══════════════════════════════════════════════════════════════════════════════

def confluence_label(score):
    if   score >= 4:  return ('🟢🟢 STRONG LONG',    'ENTER FULL SIZE',  '#00e676')
    elif score == 3:  return ('🟢   STRONG LONG',    'ENTER FULL SIZE',  '#2ecc71')
    elif score == 2:  return ('🟢   MODERATE LONG',  'ENTER HALF SIZE',  '#27ae60')
    elif score == 1:  return ('🟡   WEAK LONG',       'WAIT / SMALL',     '#f39c12')
    elif score == 0:  return ('🟡   NEUTRAL',         'NO TRADE',         '#888888')
    elif score == -1: return ('🟡   WEAK SHORT',      'WAIT / SMALL',     '#e67e22')
    elif score == -2: return ('🔴   MODERATE SHORT',  'REDUCE / SHORT',   '#e74c3c')
    elif score == -3: return ('🔴   STRONG SHORT',    'EXIT / SHORT',     '#c0392b')
    else:             return ('🔴🔴 STRONG SHORT',    'EXIT ALL / SHORT', '#922b21')

def add_trade_details(conf_df):
    """Add concrete trade lifecycle fields: ENTRY/HOLD/EXIT with active SL/Target/PnL."""
    conf_df = conf_df.copy()

    position     = 0   # 0 flat, +1 long, -1 short
    trade_id     = 0
    entry_px     = np.nan
    entry_ts     = None
    active_sl    = np.nan
    active_tgt   = np.nan
    risk_abs     = np.nan

    trade_states = []
    sides        = []
    trade_ids    = []
    entry_times  = []
    entry_prices = []
    sl_values    = []
    tgt_values   = []
    pnl_values   = []
    r_values     = []

    for _, row in conf_df.iterrows():
        score      = int(row['_score'])
        px         = float(row['Close'])
        st_line_px = float(row['SL'])
        trade      = '— FLAT'
        close_now  = False

        if position == 0:
            if score >= LONG_ENTRY_SCORE:
                position   = 1
                trade_id  += 1
                entry_px   = px
                entry_ts   = row['_ts']
                active_sl  = st_line_px
                risk_abs   = abs(entry_px - active_sl)
                if risk_abs < 1e-9:
                    risk_abs = max(abs(entry_px) * 0.001, 1e-6)
                active_tgt = entry_px + 2 * risk_abs
                trade      = '🟢 LONG ENTRY'
            elif score <= SHORT_ENTRY_SCORE:
                position   = -1
                trade_id  += 1
                entry_px   = px
                entry_ts   = row['_ts']
                active_sl  = st_line_px
                risk_abs   = abs(entry_px - active_sl)
                if risk_abs < 1e-9:
                    risk_abs = max(abs(entry_px) * 0.001, 1e-6)
                active_tgt = entry_px - 2 * risk_abs
                trade      = '🔴 SHORT ENTRY'
        elif position == 1:
            active_sl = max(active_sl, st_line_px)  # trail only upward for long
            if score < LONG_EXIT_SCORE:
                trade = '🟠 EXIT LONG'
                close_now = True
            else:
                trade = '↑ HOLD LONG'
        elif position == -1:
            active_sl = min(active_sl, st_line_px)  # trail only downward for short
            if score > SHORT_EXIT_SCORE:
                trade = '🟠 EXIT SHORT'
                close_now = True
            else:
                trade = '↓ HOLD SHORT'

        if position == 0:
            side       = '—'
            t_id       = 0
            entry_time = '—'
            e_px       = np.nan
            sl_px      = np.nan
            tgt_px     = np.nan
            pnl_pct    = np.nan
            r_mult     = np.nan
        else:
            side       = 'LONG' if position == 1 else 'SHORT'
            t_id       = trade_id
            entry_time = entry_ts.strftime('%Y-%m-%d %H:%M')
            e_px       = float(entry_px)
            sl_px      = float(active_sl)
            tgt_px     = float(active_tgt)
            if side == 'LONG':
                pnl_pct = (px / entry_px - 1) * 100
                r_mult  = (px - entry_px) / risk_abs
            else:
                pnl_pct = (entry_px / px - 1) * 100
                r_mult  = (entry_px - px) / risk_abs

        trade_states.append(trade)
        sides.append(side)
        trade_ids.append(t_id)
        entry_times.append(entry_time)
        entry_prices.append(e_px)
        sl_values.append(sl_px)
        tgt_values.append(tgt_px)
        pnl_values.append(pnl_pct)
        r_values.append(r_mult)

        if close_now:
            position   = 0
            entry_px   = np.nan
            entry_ts   = None
            active_sl  = np.nan
            active_tgt = np.nan
            risk_abs   = np.nan

    conf_df['Trade']          = trade_states
    conf_df['Side']           = sides
    conf_df['Trade ID']       = trade_ids
    conf_df['Entry Time']     = entry_times
    conf_df['Entry Px']       = entry_prices
    conf_df['Active SL']      = sl_values
    conf_df['Active Target']  = tgt_values
    conf_df['Live PnL %']     = pnl_values
    conf_df['Live R']         = r_values
    return conf_df

def build_confluence(df_full, df_day):
    """Compute all strategy votes on full data, slice to day."""
    v_st,  st_line = vote_supertrend(df_full)
    v_er           = vote_ema_rsi(df_full)
    v_mc           = vote_macd(df_full)
    v_bb           = vote_bollinger(df_full)
    v_rs           = vote_rsi(df_full)

    _rsi_full      = rsi_s(df_full['Close'], 14)
    ml, ms, mh     = macd_signals(df_full)
    _, bb_mid, _   = bollinger(df_full)
    _atr_full      = atr_s(df_full)

    rows = []
    idx  = df_day.index

    for ts in idx:
        o_val   = float(df_full['Open'].loc[ts])
        h_val   = float(df_full['High'].loc[ts])
        l_val   = float(df_full['Low'].loc[ts])
        px      = float(df_full['Close'].loc[ts])
        v_val   = float(df_full['Volume'].loc[ts])
        sv      = int(v_st.loc[ts])
        ev      = int(v_er.loc[ts])
        mv      = int(v_mc.loc[ts])
        bv      = int(v_bb.loc[ts])
        rv      = int(v_rs.loc[ts])
        score   = sv + ev + mv + bv + rv
        label, action, color = confluence_label(score)
        r_val   = float(_rsi_full.loc[ts])
        vol_avg = float(df_full['Volume'].rolling(20).mean().loc[ts])
        vol_now = float(df_full['Volume'].loc[ts])
        vol_str = '↑ High' if vol_now > vol_avg * 1.3 else ('↓ Low' if vol_now < vol_avg * 0.7 else '→ Avg')

        # SL / Target using Supertrend line
        sl_val  = float(st_line.loc[ts])
        risk    = abs(px - sl_val)
        tgt_2   = round(px + 2 * risk, 2) if sv == 1 else round(px - 2 * risk, 2)

        rows.append({
            'IST Time':  ts.strftime('%I:%M %p'),
            'IST DateTime': ts.strftime('%Y-%m-%d %H:%M'),
            'Open':      o_val,
            'High':      h_val,
            'Low':       l_val,
            'Close':     px,
            'Volume Raw':v_val,
            '_ts':       ts,
            '_px':       px,
            'ST':        '▲' if sv == 1 else '▼',
            'EMA+RSI':   '▲' if ev == 1 else ('▼' if ev == -1 else '—'),
            'MACD':      '▲' if mv == 1 else '▼',
            'BB':        '▲' if bv == 1 else '▼',
            'RSI Vote':  '▲' if rv == 1 else ('▼' if rv == -1 else '—'),
            'Score':     score,
            'Signal':    label,
            'Action':    action,
            'RSI':       round(r_val, 1),
            'Volume':    vol_str,
            'SL':        round(sl_val, 2),
            'Target 2:1':tgt_2,
            '_color':    color,
            '_score':    score,
        })

    conf_df = pd.DataFrame(rows)
    conf_df = add_trade_details(conf_df)
    return conf_df, st_line


# ══════════════════════════════════════════════════════════════════════════════
# CHART
# ══════════════════════════════════════════════════════════════════════════════

def plot_confluence(df_day, conf_df, st_line_full, coin, target_date, out):
    fig = plt.figure(figsize=(18, 15), facecolor='#0d0d0d')
    gs  = gridspec.GridSpec(4, 1, figure=fig, hspace=0.40,
                            height_ratios=[4, 1.2, 1, 1.5],
                            left=0.06, right=0.97, top=0.93, bottom=0.04)

    def style(ax):
        ax.set_facecolor('#161616')
        ax.tick_params(colors='#888', labelsize=8)
        ax.spines[['top','right']].set_visible(False)
        ax.spines[['bottom','left']].set_color('#2a2a2a')
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M', tz=IST))
        if BAR_MINUTES < 60:
            ax.xaxis.set_major_locator(mdates.HourLocator(interval=1, tz=IST))
        else:
            ax.xaxis.set_major_locator(mdates.HourLocator(interval=2, tz=IST))

    times   = conf_df['_ts'].tolist()
    prices  = conf_df['_px'].tolist()
    scores  = conf_df['_score'].tolist()
    st_vals = [float(st_line_full.loc[ts]) for ts in times]

    # ── Panel 1: Price + Supertrend + confluence background ───────────────
    ax1 = fig.add_subplot(gs[0])
    style(ax1)

    # colour background by confluence score
    for i in range(1, len(times)):
        s = scores[i]
        if   s >= 3:  bg = '#00441b'
        elif s == 2:  bg = '#1a3320'
        elif s == 1:  bg = '#1a2a1a'
        elif s == 0:  bg = '#1a1a1a'
        elif s == -1: bg = '#2a1a1a'
        elif s == -2: bg = '#331a1a'
        else:         bg = '#4a0000'
        ax1.axvspan(times[i-1], times[i], color=bg, alpha=0.45, zorder=0)

    ax1.plot(times, prices, color='#e0e0e0', lw=1.8, zorder=3, label='Price')

    # Supertrend line
    for i in range(1, len(times)):
        c = '#2ecc71' if scores[i] > 0 else '#e74c3c'
        ax1.plot([times[i-1], times[i]], [st_vals[i-1], st_vals[i]],
                 color=c, lw=2.2, alpha=0.85, zorder=2)

    # Entry/exit markers from explicit trade-state engine
    for _, row in conf_df.iterrows():
        ts = row['_ts']
        px = row['_px']
        trade = row['Trade']
        if trade == '🟢 LONG ENTRY':
            ax1.scatter(ts, px, marker='^', color='#00e676', s=150, zorder=6)
            ax1.annotate(f"LONG\n${px:,.0f}",
                         xy=(ts, px), xytext=(6, 10), textcoords='offset points',
                         color='#00e676', fontsize=7, fontweight='bold')
        elif trade == '🔴 SHORT ENTRY':
            ax1.scatter(ts, px, marker='v', color='#ff5252', s=130, zorder=6)
            ax1.annotate(f"SHORT\n${px:,.0f}",
                         xy=(ts, px), xytext=(6, -18), textcoords='offset points',
                         color='#ff5252', fontsize=7, fontweight='bold')
        elif trade in ('🟠 EXIT LONG', '🟠 EXIT SHORT'):
            ax1.scatter(ts, px, marker='o', color='#f39c12', s=100, zorder=6)

    # Plot active SL/Target only during active trades
    active_mask = conf_df['Side'] != '—'
    if active_mask.any():
        active_sl  = conf_df['Active SL'].where(active_mask)
        active_tgt = conf_df['Active Target'].where(active_mask)
        ax1.plot(times, active_sl,  color='#e74c3c', lw=1.2, ls='--', alpha=0.8, label='Active SL')
        ax1.plot(times, active_tgt, color='#2ecc71', lw=1.2, ls='--', alpha=0.8, label='Active Target')

    ax1.set_ylabel('Price (USD)', color='#999', fontsize=9)
    ax1.set_title(
        f'{coin}  ·  {TIMEFRAME}  ·  {target_date}  ·  IST  ·  Multi-Strategy Confluence\n'
        f'Background: 🟢 dark green = Strong Long  |  🔴 dark red = Strong Short  |  grey = No trade',
        color='#ddd', fontsize=10, fontweight='bold', pad=10)
    ax1.legend(handles=[
        Line2D([0],[0], color='#e0e0e0', lw=1.8, label='Price'),
        Line2D([0],[0], color='#2ecc71', lw=2,   label='Supertrend (bull)'),
        Line2D([0],[0], color='#e74c3c', lw=2,   label='Supertrend (bear)'),
        Line2D([0],[0], marker='^', color='#00e676', ls='None', ms=10, label='Long Entry'),
        Line2D([0],[0], marker='v', color='#ff5252', ls='None', ms=9,  label='Short Entry'),
        Line2D([0],[0], marker='o', color='#f39c12', ls='None', ms=8,  label='Exit'),
        Line2D([0],[0], color='#e74c3c', lw=1.2, ls='--', label='Active SL'),
        Line2D([0],[0], color='#2ecc71', lw=1.2, ls='--', label='Active Target'),
    ], framealpha=0, labelcolor='white', fontsize=8, ncol=5, loc='upper left')

    # ── Panel 2: Confluence score bar ─────────────────────────────────────
    ax2 = fig.add_subplot(gs[1])
    style(ax2)
    bar_colors = [('#00e676' if s >= 4 else '#2ecc71' if s >= 2 else
                   '#888' if s == 0 else '#e74c3c' if s >= -2 else '#922b21')
                  for s in scores]
    bar_width_days = (BAR_MINUTES * 0.8) / (24 * 60)
    ax2.bar(times, scores, color=bar_colors, width=bar_width_days, edgecolor='none', align='center')
    ax2.axhline(0,  color='#555', lw=0.8)
    ax2.axhline(3,  color='#2ecc71', lw=0.8, ls='--', alpha=0.6, label='Strong Long ≥3')
    ax2.axhline(-3, color='#e74c3c', lw=0.8, ls='--', alpha=0.6, label='Strong Short ≤-3')
    ax2.fill_between(times, 3, [max(s, 3) for s in scores],
                     where=[s >= 3 for s in scores], color='#2ecc71', alpha=0.3)
    ax2.fill_between(times, -3, [min(s, -3) for s in scores],
                     where=[s <= -3 for s in scores], color='#e74c3c', alpha=0.3)
    ax2.set_ylim(-5.5, 5.5)
    ax2.set_yticks(range(-5, 6))
    ax2.set_ylabel('Confluence\nScore', color='#999', fontsize=8)
    ax2.set_title('Confluence Score  (5 strategies voting: +1 bull / -1 bear each)',
                  color='#ddd', fontsize=9, pad=4)
    ax2.legend(framealpha=0, labelcolor='white', fontsize=7, loc='upper right', ncol=2)

    # ── Panel 3: RSI ──────────────────────────────────────────────────────
    ax3 = fig.add_subplot(gs[2])
    style(ax3)
    rsi_vals = conf_df['RSI'].tolist()
    ax3.plot(times, rsi_vals, color='#7F77DD', lw=1.5)
    ax3.axhline(70, color='#e74c3c', lw=0.8, ls='--', alpha=0.6)
    ax3.axhline(30, color='#2ecc71', lw=0.8, ls='--', alpha=0.6)
    ax3.axhline(50, color='#555',    lw=0.6, ls=':',  alpha=0.5)
    ax3.fill_between(times, rsi_vals, 70, where=[r > 70 for r in rsi_vals],
                     color='#e74c3c', alpha=0.3)
    ax3.fill_between(times, rsi_vals, 30, where=[r < 30 for r in rsi_vals],
                     color='#2ecc71', alpha=0.3)
    ax3.set_ylim(10, 90)
    ax3.set_ylabel('RSI (14)', color='#999', fontsize=8)
    ax3.set_title('RSI  (>70 = overbought  |  <30 = oversold)', color='#ddd', fontsize=9, pad=4)

    # ── Panel 4: Heatmap — strategy votes per bar ─────────────────────────
    ax4 = fig.add_subplot(gs[3])
    ax4.set_facecolor('#0d0d0d')
    ax4.tick_params(colors='#888', labelsize=8)
    ax4.spines[:].set_visible(False)

    strat_names = ['Supertrend', 'EMA+RSI', 'MACD', 'Bollinger', 'RSI Vote']
    vote_cols   = ['ST', 'EMA+RSI', 'MACD', 'BB', 'RSI Vote']
    vote_map    = {'▲': 1, '—': 0, '▼': -1}

    heatmap_data = np.array([
        [vote_map.get(conf_df[col].iloc[i], 0) for col in vote_cols]
        for i in range(len(conf_df))
    ]).T   # shape: (5 strategies, number of bars)

    im = ax4.imshow(heatmap_data, aspect='auto', cmap='RdYlGn',
                    vmin=-1, vmax=1, interpolation='nearest')
    ax4.set_yticks(range(5))
    ax4.set_yticklabels(strat_names, color='#ccc', fontsize=8)
    tick_step = max(1, int(60 / BAR_MINUTES))  # one label per hour
    tick_idx  = list(range(0, len(times), tick_step))
    if tick_idx and tick_idx[-1] != len(times) - 1:
        tick_idx.append(len(times) - 1)
    ax4.set_xticks(tick_idx)
    ax4.set_xticklabels([times[i].strftime('%H:%M') for i in tick_idx],
                        color='#888', fontsize=6.5, rotation=45, ha='right')

    # annotate vote symbols
    for y, col in enumerate(vote_cols):
        for x, val in enumerate(conf_df[col].tolist()):
            c = '#fff' if val == '▲' else ('#fff' if val == '▼' else '#555')
            ax4.text(x, y, val, ha='center', va='center', color=c, fontsize=8)

    ax4.set_title('Strategy Vote Heatmap  (▲ Bullish  |  ▼ Bearish  |  — Neutral)',
                  color='#ddd', fontsize=9, pad=6)
    plt.colorbar(im, ax=ax4, orientation='vertical', fraction=0.01,
                 label='Vote', ticks=[-1,0,1]).ax.tick_params(colors='#888')

    plt.suptitle(f'Multi-Strategy Confluence  ·  {coin}  ·  {target_date}  ·  {TIMEFRAME} IST',
                 color='white', fontsize=13, fontweight='bold', y=0.97)
    plt.savefig(out, dpi=150, bbox_inches='tight', facecolor='#0d0d0d')
    print(f'  Chart → {out}')
    plt.close()


# ══════════════════════════════════════════════════════════════════════════════
# PRINT
# ══════════════════════════════════════════════════════════════════════════════

def print_confluence_table(coin, target_date, conf_df):
    print(f'\n  {"═"*230}')
    print(f'  CONFLUENCE TABLE  —  {coin}  —  {target_date}  (IST)')
    print(f'  {"═"*230}')
    print(f"  {'IST Timestamp':<17} {'Open':>9} {'High':>9} {'Low':>9} {'Close':>9} {'Vol':>10} "
          f"{'Score':>7} {'Trade State':<14} {'Entry':>9} {'SL':>9} {'Target':>9} {'PnL%':>7} {'R':>6}  "
          f"{'ST':^3} {'EMA':^3} {'MACD':^4} {'BB':^3} {'RSIv':^4} {'RSI':>5} {'VolSt':>8}  {'Signal'}")
    print(f'  {"─"*230}')

    for _, row in conf_df.iterrows():
        score_txt = f"[{row['_score']:+d}]"
        entry_txt = '—' if pd.isna(row['Entry Px']) else f"{row['Entry Px']:,.2f}"
        sl_txt    = '—' if pd.isna(row['Active SL']) else f"{row['Active SL']:,.2f}"
        tgt_txt   = '—' if pd.isna(row['Active Target']) else f"{row['Active Target']:,.2f}"
        pnl_txt   = '—' if pd.isna(row['Live PnL %']) else f"{row['Live PnL %']:+.2f}"
        r_txt     = '—' if pd.isna(row['Live R']) else f"{row['Live R']:+.2f}"
        marker    = ' ◀ ENTER' if 'ENTRY' in row['Trade'] else (' ◀ EXIT' if 'EXIT' in row['Trade'] else '')
        print(f"  {row['IST DateTime']:<17} {row['Open']:>9,.2f} {row['High']:>9,.2f} "
              f"{row['Low']:>9,.2f} {row['Close']:>9,.2f} {row['Volume Raw']:>10,.0f} "
              f"{score_txt:>7} {row['Trade']:<14} {entry_txt:>9} {sl_txt:>9} {tgt_txt:>9} "
              f"{pnl_txt:>7} {r_txt:>6}  {row['ST']:^3} {row['EMA+RSI']:^3} {row['MACD']:^4} "
              f"{row['BB']:^3} {row['RSI Vote']:^4} {row['RSI']:>5.1f} {row['Volume']:>8}  {row['Signal']}{marker}")

    print(f'  {"─"*230}')

    # concrete entry points
    long_entries = conf_df[conf_df['Trade'] == '🟢 LONG ENTRY']
    if not long_entries.empty:
        print(f'\n  ✅ LONG ENTRIES:')
        for _, row in long_entries.iterrows():
            print(f"     {row['IST DateTime']}  Entry ${row['Entry Px']:,.2f}  "
                  f"SL ${row['Active SL']:,.2f}  Target ${row['Active Target']:,.2f}  "
                  f"Score [{row['_score']:+d}]")
    else:
        print(f'\n  ⚠️  No long entries triggered on this date.')

    short_entries = conf_df[conf_df['Trade'] == '🔴 SHORT ENTRY']
    if not short_entries.empty:
        print(f'\n  🔻 SHORT ENTRIES:')
        for _, row in short_entries.iterrows():
            print(f"     {row['IST DateTime']}  Entry ${row['Entry Px']:,.2f}  "
                  f"SL ${row['Active SL']:,.2f}  Target ${row['Active Target']:,.2f}  "
                  f"Score [{row['_score']:+d}]")


def print_summary(coin, conf_df):
    last   = conf_df.iloc[-1]
    score  = last['_score']
    label, action, _ = confluence_label(score)

    print(f'\n  ┌{"─"*60}┐')
    print(f'  │  END-OF-DAY CONFLUENCE  —  {coin:<30}│')
    print(f'  ├{"─"*60}┤')
    print(f'  │  Last price    : ${last["_px"]:>12,.2f}{"":>24}│')
    print(f'  │  Supertrend    : {last["ST"]:^4}   EMA+RSI : {last["EMA+RSI"]:^4}   MACD : {last["MACD"]:^4}{"":>14}│')
    print(f'  │  Bollinger     : {last["BB"]:^4}   RSI Vote: {last["RSI Vote"]:^4}{"":>30}│')
    print(f'  │  RSI           : {last["RSI"]:>5.1f}{"":>42}│')
    print(f'  ├{"─"*60}┤')
    print(f'  │  Confluence Score : [{score:+d}]  {label:<34}│')
    print(f'  │  Recommended      : {action:<38}│')
    print(f'  │  Trade State      : {last["Trade"]:<38}│')
    if last['Side'] != '—':
        pnl_txt = f'{last["Live PnL %"]:+.2f}%' if not pd.isna(last['Live PnL %']) else '—'
        print(f'  │  Entry Time       : {last["Entry Time"]:<38}│')
        print(f'  │  Entry Price      : ${last["Entry Px"]:>12,.2f}{"":>23}│')
        print(f'  │  Active Stop-Loss : ${last["Active SL"]:>12,.2f}{"":>23}│')
        print(f'  │  Active Target    : ${last["Active Target"]:>12,.2f}{"":>23}│')
        print(f'  │  Live PnL         : {pnl_txt:<38}│')
    print(f'  └{"─"*60}┘')

    # how many bars each score zone
    total = len(conf_df)
    strong_long  = (conf_df['_score'] >= 3).sum()
    mod_long     = ((conf_df['_score'] >= 1) & (conf_df['_score'] < 3)).sum()
    neutral      = (conf_df['_score'] == 0).sum()
    short_zone   = (conf_df['_score'] <= -1).sum()
    print(f'\n  Day breakdown:')
    print(f'    Strong Long  (score≥3) : {strong_long:3d} / {total} bars ({TIMEFRAME})  '
          f'{"█"*strong_long}')
    print(f'    Moderate Long(score 1-2): {mod_long:3d} / {total} bars ({TIMEFRAME})  '
          f'{"█"*mod_long}')
    print(f'    Neutral      (score 0) : {neutral:3d} / {total} bars ({TIMEFRAME})')
    print(f'    Short zone   (score<0) : {short_zone:3d} / {total} bars ({TIMEFRAME})  '
          f'{"░"*short_zone}')


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description='Multi-strategy confluence analyzer — IST timezone',
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""Examples:
  python confluence_ist.py                                # April 10 2025
  python confluence_ist.py --date 2025-04-10
  python confluence_ist.py --date 2025-04-10 --coin BTC-USD
  python confluence_ist.py --date 2025-03-20 --coin ETH-USD
        """)
    parser.add_argument('--date', type=str, default=TARGET_DATE)
    parser.add_argument('--coin', type=str, default=None)
    args = parser.parse_args()

    target_date = args.date
    coins       = [args.coin] if args.coin else COINS
    here        = os.path.dirname(os.path.abspath(__file__))

    print('=' * 65)
    print(f'  Multi-Strategy Confluence  |  {target_date}  |  IST')
    print('=' * 65)
    print('  Strategies: Supertrend | EMA+RSI | MACD | Bollinger Bands | RSI')
    print('  Rule: Enter only when 3+ strategies agree (score ≥ +3 or ≤ -3)')

    for coin in coins:
        print(f'\n  Downloading {TIMEFRAME} data for {coin} ...')
        try:
            df = fetch(coin, target_date)
        except Exception as e:
            print(f'  [!] Error: {e}'); continue

        t_ist  = pd.Timestamp(target_date).tz_localize(IST)
        df_day = df[df.index.date == t_ist.date()]

        # For today's date, keep only closed candles up to "now"
        now_utc = pd.Timestamp.now(tz='UTC')
        if t_ist.date() == now_utc.tz_convert(IST).date():
            latest_closed_ist = now_utc.floor(BAR_FREQ).tz_convert(IST)
            df_day = df_day[df_day.index <= latest_closed_ist]
            if not df_day.empty:
                print(f'  Live mode: using closed candles up to '
                      f'{latest_closed_ist.strftime("%Y-%m-%d %H:%M")} IST')

        if df_day.empty:
            print(f'  No candles on {target_date}'); continue

        print(f'  {len(df_day)} candles ({TIMEFRAME})  '
              f'({df_day.index[0].strftime("%H:%M")} → {df_day.index[-1].strftime("%H:%M")} IST)')

        conf_df, st_line_full = build_confluence(df, df_day)

        print_confluence_table(coin, target_date, conf_df)
        print_summary(coin, conf_df)

        out = os.path.join(here,
                           f'confluence_{coin.replace("-","_")}_{target_date}.png')
        plot_confluence(df_day, conf_df, st_line_full, coin, target_date, out)

    print('\nDone.')


if __name__ == '__main__':
    main()
