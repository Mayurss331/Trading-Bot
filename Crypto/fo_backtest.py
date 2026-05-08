"""
Crypto F&O Strategy Backtester
================================
Futures Strategies  (leveraged, daily rebalance):
  1. Supertrend 2x Long          — best signal from spot backtest × 2x leverage
  2. EMA 9/21 + RSI  2x L/S     — long in bull, short in bear (both sides)
  3. Supertrend 3x Long          — aggressive version

Options Strategies  (30-day monthly cycle, Black-Scholes pricing):
  4. Long ATM Call               — 10% capital/month at risk, bullish bet
  5. Covered Call                — spot + sell 10%-OTM call (earn premium)
  6. Protective Put              — spot + buy 5%-OTM put  (downside hedge)
  7. Long Straddle               — buy call + put, profit from big moves
  8. Iron Condor                 — sell OTM strangle, profit from sideways

Assets  : BTC-USD, ETH-USD
Period  : 2023-01-01 → yesterday  (covers 2023 recovery + 2024-25 bull run)
Capital : $100
Costs   : Futures 0.05%/leg (perp) | Options per BS price | Spot 0.1%/leg

Usage:
    python fo_backtest.py           # live data  (yfinance)
    python fo_backtest.py --demo    # simulated  (offline)
"""

import numpy as np
import pandas as pd
import math
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.ticker import FuncFormatter
import warnings, os, argparse
from datetime import date, timedelta
warnings.filterwarnings('ignore')

# ── Config defaults (overridden by CLI args) ───────────────────────────────────
CAPITAL      = 100.0
START        = '2023-01-01'
END          = (date.today() - timedelta(days=1)).strftime('%Y-%m-%d')
COINS        = ['BTC-USD', 'ETH-USD']
FUT_COST     = 0.0005   # 0.05% per leg (Binance perp taker)
SPOT_COST    = 0.001    # 0.1% per leg  (Binance spot)
FUNDING_RATE = 0.0001   # 0.01%/day funding (longs pay shorts, simplified)
RISK_FREE    = 0.0      # crypto has no risk-free base
OPT_ALLOC    = 0.10     # fraction of capital risked on pure option plays per month

COLORS = {
    'Supertrend 2x':       '#E05CDA',
    'EMA+RSI 2x L/S':      '#1D9E75',
    'Supertrend 3x':       '#FF6B6B',
    'Long Call':           '#378ADD',
    'Covered Call':        '#EF9F27',
    'Protective Put':      '#4ECDC4',
    'Long Straddle':       '#F4A261',
    'Iron Condor':         '#A8DADC',
    'Buy & Hold':          '#888780',
}


# ══════════════════════════════════════════════════════════════════════════════
# DATA
# ══════════════════════════════════════════════════════════════════════════════

def fetch_live(coin: str) -> pd.DataFrame:
    import yfinance as yf
    df = yf.download(coin, start=START, end=END, auto_adjust=True, progress=False)
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df = df[['Open', 'High', 'Low', 'Close', 'Volume']].dropna()
    print(f"  {coin}: {len(df)} bars  {df.index[0].date()} → {df.index[-1].date()}")
    return df


def simulate_coin(seed=42, drift=0.50, vol=0.75) -> pd.DataFrame:
    np.random.seed(seed)
    dates = pd.date_range(START, END, freq='B')
    T     = len(dates)
    dt    = 1 / 252
    rets  = np.random.normal(drift * dt, vol * np.sqrt(dt), T)
    crash = np.random.choice(T, size=int(T * 0.02), replace=False)
    rets[crash] -= np.random.uniform(0.05, 0.12, len(crash))
    px    = 20_000 * np.cumprod(np.exp(rets))
    hi    = px * np.random.uniform(1.001, 1.04, T)
    lo    = px * np.random.uniform(0.96, 0.999, T)
    op    = np.roll(px, 1); op[0] = px[0]
    vol_s = np.random.uniform(1e9, 4e10, T)
    return pd.DataFrame({'Open': op, 'High': hi, 'Low': lo,
                         'Close': px, 'Volume': vol_s}, index=dates)


# ══════════════════════════════════════════════════════════════════════════════
# TECHNICAL INDICATORS
# ══════════════════════════════════════════════════════════════════════════════

def ema(s, n):   return s.ewm(span=n, adjust=False).mean()
def sma(s, n):   return s.rolling(n).mean()

def rsi(s, n=14):
    d    = s.diff()
    gain = d.clip(lower=0).ewm(com=n-1, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(com=n-1, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss.replace(0, np.nan))

def atr(hi, lo, cl, n=14):
    tr = pd.concat([(hi - lo),
                    (hi - cl.shift(1)).abs(),
                    (lo - cl.shift(1)).abs()], axis=1).max(axis=1)
    return tr.ewm(span=n, adjust=False).mean()

def supertrend_signal(df, n=10, mult=3.5):
    mid  = (df['High'] + df['Low']) / 2
    _atr = atr(df['High'], df['Low'], df['Close'], n)
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
    return (df['Close'] > st).astype(int).shift(1).fillna(0)   # 1=long, 0=flat


def ema_rsi_signal(df):
    fast = ema(df['Close'], 9); slow = ema(df['Close'], 21)
    _rsi = rsi(df['Close'], 14)
    sig  = pd.Series(0, index=df.index)
    sig[fast > slow]  =  1   # long
    sig[fast < slow]  = -1   # short
    sig[(_rsi < 45) & (fast > slow)] = 0   # filter weak longs
    sig[(_rsi > 55) & (fast < slow)] = 0   # filter weak shorts
    return sig.shift(1).fillna(0)


# ══════════════════════════════════════════════════════════════════════════════
# BLACK-SCHOLES PRICING  (no scipy — uses math.erf)
# ══════════════════════════════════════════════════════════════════════════════

def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def bs_call(S, K, T, sigma, r=0.0):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)

def bs_put(S, K, T, sigma, r=0.0):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)

def hist_vol_series(close: pd.Series, window=30) -> pd.Series:
    """Annualized 30-day historical volatility."""
    return np.log(close / close.shift(1)).rolling(window).std() * np.sqrt(252)


# ══════════════════════════════════════════════════════════════════════════════
# FUTURES BACKTESTER  (daily mark-to-market, funding, liquidation)
# ══════════════════════════════════════════════════════════════════════════════

def backtest_futures(df, signal: pd.Series, capital: float,
                     leverage: int = 2, long_short: bool = False) -> tuple:
    """
    Daily futures with leverage.
    signal: 1=long, 0=flat, -1=short (only used if long_short=True)
    Liquidation triggers at loss > 90% of margin.
    Returns (equity Series, trades list).
    """
    price  = df['Close']
    equity = pd.Series(capital, index=price.index, dtype=float)
    margin = capital          # margin account balance
    notional   = 0.0          # current position notional
    entry_px   = 0.0
    direction  = 0             # +1 or -1
    days_held  = 0
    trades     = []

    liq_threshold = 1 / leverage * 0.9   # lose 90% of margin → liquidated

    for i in range(1, len(price)):
        date = price.index[i]
        px   = price.iloc[i]
        sig  = signal.iloc[i]

        # ── decide target direction ────────────────────────────────────────
        target_dir = 0
        if sig == 1:
            target_dir = 1
        elif long_short and sig == -1:
            target_dir = -1

        # ── close existing position if direction changes or flat ───────────
        if direction != 0 and (target_dir != direction or target_dir == 0):
            pnl     = notional * direction * (px / entry_px - 1)
            funding = notional * FUNDING_RATE * days_held
            exit_cost = abs(notional) * FUT_COST
            net_pnl = pnl - funding - exit_cost
            margin  = max(margin + net_pnl, 0)
            trades.append({'direction': 'L' if direction == 1 else 'S',
                           'entry': round(entry_px, 2), 'exit': round(px, 2),
                           'pnl_pct': round((px / entry_px - 1) * direction * leverage * 100, 2),
                           'win': net_pnl > 0})
            notional  = 0.0; direction = 0; days_held = 0

        # ── open new position ──────────────────────────────────────────────
        if direction == 0 and target_dir != 0 and margin > 1:
            notional  = margin * leverage
            entry_px  = px
            direction = target_dir
            entry_cost = notional * FUT_COST
            margin   -= entry_cost

        # ── daily funding on open position ─────────────────────────────────
        if direction != 0:
            days_held += 1
            margin    -= notional * FUNDING_RATE   # simplified: longs always pay

        # ── liquidation check ──────────────────────────────────────────────
        if direction != 0:
            unrealised_pct = direction * (px / entry_px - 1)
            if unrealised_pct < -liq_threshold:
                trades.append({'direction': 'L' if direction == 1 else 'S',
                               'entry': round(entry_px, 2), 'exit': round(px, 2),
                               'pnl_pct': round(unrealised_pct * leverage * 100, 2),
                               'win': False})
                margin = max(margin * 0.05, 0)   # liquidated, keep 5% (insurance fund)
                notional = 0.0; direction = 0; days_held = 0

        # ── mark-to-market ─────────────────────────────────────────────────
        if direction != 0:
            unrealised = notional * direction * (px / entry_px - 1)
            equity.iloc[i] = margin + unrealised
        else:
            equity.iloc[i] = margin

    return equity.clip(lower=0), trades


# ══════════════════════════════════════════════════════════════════════════════
# OPTIONS BACKTESTER  (monthly 30-day cycle)
# ══════════════════════════════════════════════════════════════════════════════

def _monthly_pairs(df: pd.DataFrame):
    """Yield (start_date, end_date, S_start, S_end, vol_at_start) monthly."""
    monthly  = df.resample('ME').last()
    hv       = hist_vol_series(df['Close'], 30).resample('ME').last()
    for i in range(1, len(monthly)):
        s_date = monthly.index[i - 1]
        e_date = monthly.index[i]
        S      = float(monthly['Close'].iloc[i - 1])
        S_exp  = float(monthly['Close'].iloc[i])
        vol    = float(hv.iloc[i - 1]) if not np.isnan(hv.iloc[i - 1]) else 0.70
        vol    = max(vol, 0.20)           # floor: crypto rarely < 20% vol
        yield s_date, e_date, S, S_exp, vol


def _daily_nav_interpolate(df, start, end, nav_start, nav_end):
    """Linearly interpolate daily NAV between two monthly snapshots."""
    dates = df.index[(df.index >= start) & (df.index <= end)]
    if len(dates) < 2:
        return {d: nav_end for d in dates}
    steps = len(dates) - 1
    return {d: nav_start + (nav_end - nav_start) * (j / steps)
            for j, d in enumerate(dates)}


def backtest_options(df, strategy_fn, capital: float,
                     strategy_name: str) -> tuple:
    """
    Monthly options cycle.  strategy_fn signature:
      (S, S_exp, vol, capital, T=30/365) -> (net_pnl_usd, trade_info_dict)
    Returns (equity Series, trades list).
    """
    T_opt = 30 / 365
    equity_dict = {}
    cash        = capital
    trades      = []

    prev_nav = capital
    for s_date, e_date, S, S_exp, vol in _monthly_pairs(df):
        if cash <= 0:
            break
        net_pnl, info = strategy_fn(S, S_exp, vol, cash, T=T_opt)
        info.update({'month': s_date.strftime('%Y-%m'), 'S': round(S, 2),
                     'S_exp': round(S_exp, 2), 'vol_pct': round(vol * 100, 1)})
        trades.append(info)
        cash = max(cash + net_pnl, 0)

        # daily NAV interpolation for equity curve
        daily = _daily_nav_interpolate(df, s_date, e_date, prev_nav, cash)
        equity_dict.update(daily)
        prev_nav = cash

    eq = pd.Series(equity_dict).sort_index()
    if eq.empty:
        eq = pd.Series(capital, index=df.index)
    return eq.clip(lower=0), trades


# ══════════════════════════════════════════════════════════════════════════════
# OPTIONS STRATEGY PAYOFFS
# ══════════════════════════════════════════════════════════════════════════════

def opt_long_call(S, S_exp, vol, capital, T=30/365):
    """
    Buy ATM call with OPT_ALLOC fraction of capital.
    Max loss = premium paid. Profit = (S_exp - S)+ per unit × units.
    """
    premium = bs_call(S, S, T, vol)          # ATM: K = S
    budget  = capital * OPT_ALLOC
    units   = budget / max(premium, 1e-6)    # number of contracts
    payoff  = max(S_exp - S, 0) * units
    net     = payoff - budget                 # net P&L
    win     = net > 0
    return net, {'strategy': 'Long Call', 'K': round(S, 2),
                 'premium_paid': round(budget, 4), 'payoff': round(payoff, 4),
                 'net_pnl': round(net, 4), 'win': win}


def opt_covered_call(S, S_exp, vol, capital, T=30/365):
    """
    Long 1 unit of spot + sell 10%-OTM call.
    Spot gain/loss + call premium received - payoff if called away.
    """
    K_call   = S * 1.10
    premium  = bs_call(S, K_call, T, vol)
    units    = capital / S              # BTC units purchased (fractional)
    spot_cost = capital * SPOT_COST
    spot_pnl  = units * (S_exp - S) - spot_cost
    call_pnl  = premium * units - max(S_exp - K_call, 0) * units  # premium - payoff if exercised
    net       = spot_pnl + call_pnl
    return net, {'strategy': 'Covered Call', 'K_call': round(K_call, 2),
                 'premium_recv': round(premium * units, 4),
                 'spot_pnl': round(spot_pnl, 4), 'net_pnl': round(net, 4),
                 'win': net > 0}


def opt_protective_put(S, S_exp, vol, capital, T=30/365):
    """
    Long spot + buy 5%-OTM put for downside insurance.
    """
    K_put    = S * 0.95
    premium  = bs_put(S, K_put, T, vol)
    units    = capital / S
    put_cost = premium * units
    spot_pnl = units * (S_exp - S) - capital * SPOT_COST
    put_pnl  = max(K_put - S_exp, 0) * units - put_cost    # payoff - premium
    net      = spot_pnl + put_pnl
    return net, {'strategy': 'Protective Put', 'K_put': round(K_put, 2),
                 'premium_paid': round(put_cost, 4),
                 'spot_pnl': round(spot_pnl, 4), 'net_pnl': round(net, 4),
                 'win': net > 0}


def opt_straddle(S, S_exp, vol, capital, T=30/365):
    """
    Buy ATM call + ATM put with OPT_ALLOC each.
    Profits when BTC makes a large move in either direction.
    Break-even: price moves ≥ (call_prem + put_prem).
    """
    call_prem = bs_call(S, S, T, vol)
    put_prem  = bs_put(S, S, T, vol)
    budget    = capital * OPT_ALLOC         # total spend on straddle
    units     = budget / max(call_prem + put_prem, 1e-6)
    call_pnl  = max(S_exp - S, 0) * units
    put_pnl   = max(S - S_exp, 0) * units
    net       = call_pnl + put_pnl - budget
    return net, {'strategy': 'Long Straddle', 'K': round(S, 2),
                 'premium_paid': round(budget, 4),
                 'break_even_up': round(S + call_prem + put_prem, 2),
                 'break_even_dn': round(S - call_prem - put_prem, 2),
                 'net_pnl': round(net, 4), 'win': net > 0}


def opt_iron_condor(S, S_exp, vol, capital, T=30/365):
    """
    Sell 5%-OTM strangle, buy 10%-OTM strangle for protection.
    Max profit = net premium received.
    Max loss   = spread width - net premium.
    Profit zone: S stays between sell strikes.
    """
    K1 = S * 0.90   # buy put  (protection)
    K2 = S * 0.95   # sell put
    K3 = S * 1.05   # sell call
    K4 = S * 1.10   # buy call  (protection)

    spread_width = S * 0.05     # 5% of spot per spread leg
    units        = capital / (spread_width * 10)   # position sizing
    units        = max(units, 0.001)

    # net premium received per unit
    net_prem = ((bs_put(S, K2, T, vol)  - bs_put(S, K1, T, vol)) +
                (bs_call(S, K3, T, vol) - bs_call(S, K4, T, vol)))
    premium_recv = net_prem * units

    # payoff at expiry
    put_spread_pnl  = -(max(K2 - S_exp, 0) - max(K1 - S_exp, 0)) * units
    call_spread_pnl = -(max(S_exp - K3, 0) - max(S_exp - K4, 0)) * units
    net = premium_recv + put_spread_pnl + call_spread_pnl

    return net, {'strategy': 'Iron Condor', 'profit_zone': f'{round(K2,0)}-{round(K3,0)}',
                 'premium_recv': round(premium_recv, 4),
                 'put_pnl': round(put_spread_pnl, 4),
                 'call_pnl': round(call_spread_pnl, 4),
                 'net_pnl': round(net, 4), 'win': net > 0}


# ══════════════════════════════════════════════════════════════════════════════
# METRICS
# ══════════════════════════════════════════════════════════════════════════════

def compute_metrics(equity: pd.Series, trades: list, label: str) -> dict:
    eq   = equity.ffill().fillna(CAPITAL)
    rets = eq.pct_change().dropna()
    n_yr = max((eq.index[-1] - eq.index[0]).days / 365.25, 0.01)
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / n_yr) - 1
    vol  = rets.std() * np.sqrt(252)
    shp  = (rets.mean() * 252) / vol if vol > 0 else 0
    dv   = rets[rets < 0].std() * np.sqrt(252)
    srt  = (rets.mean() * 252) / dv if dv > 0 else 0
    roll = eq.cummax()
    dd   = (eq - roll) / roll
    mdd  = dd.min()
    cal  = cagr / abs(mdd) if mdd != 0 else 0
    n_t  = len(trades)
    wr   = np.mean([t['win'] for t in trades]) * 100 if trades else 0
    wins = [t.get('pnl_pct', t.get('net_pnl', 0)) for t in trades if t['win']]
    loss = [t.get('pnl_pct', t.get('net_pnl', 0)) for t in trades if not t['win']]
    avg_w = np.mean(wins)  if wins else 0
    avg_l = np.mean(loss)  if loss else 0
    pf    = abs(avg_w / avg_l) if avg_l else 0
    return dict(Strategy=label, Final_USD=round(eq.iloc[-1], 4),
                PnL_USD=round(eq.iloc[-1] - CAPITAL, 4),
                Return_pct=round((eq.iloc[-1]/CAPITAL - 1)*100, 2),
                CAGR_pct=round(cagr*100, 2), Vol_pct=round(vol*100, 2),
                Sharpe=round(shp, 3), Sortino=round(srt, 3),
                MaxDD_pct=round(mdd*100, 2), Calmar=round(cal, 3),
                WinRate_pct=round(wr, 1), Trades=n_t,
                AvgWin=round(avg_w, 2), AvgLoss=round(avg_l, 2),
                ProfitFactor=round(pf, 2),
                _eq=eq, _dd=dd)


# ══════════════════════════════════════════════════════════════════════════════
# PLOT
# ══════════════════════════════════════════════════════════════════════════════

def plot_coin(results, coin, out):
    bah    = next(r for r in results if r['Strategy'] == 'Buy & Hold')
    strats = [r for r in results if r['Strategy'] != 'Buy & Hold']
    fut    = [r for r in strats if any(x in r['Strategy'] for x in ['2x','3x'])]
    opt    = [r for r in strats if r not in fut]

    fig = plt.figure(figsize=(20, 15), facecolor='#0d0d0d')
    gs  = gridspec.GridSpec(3, 4, figure=fig, hspace=0.52, wspace=0.38,
                            left=0.05, right=0.97, top=0.93, bottom=0.05)

    def style(ax):
        ax.set_facecolor('#161616')
        ax.tick_params(colors='#777', labelsize=8)
        ax.spines[['top','right']].set_visible(False)
        ax.spines[['bottom','left']].set_color('#333')

    # ── Futures equity ─────────────────────────────────────────────────────
    ax1 = fig.add_subplot(gs[0, :2])
    style(ax1)
    ax1.plot(bah['_eq'].index, bah['_eq'], color=COLORS['Buy & Hold'],
             lw=1.5, ls='--', alpha=0.6, label='Buy & Hold')
    for r in fut:
        ax1.plot(r['_eq'].index, r['_eq'], color=COLORS.get(r['Strategy'],'#fff'),
                 lw=1.8, label=r['Strategy'])
    ax1.axhline(CAPITAL, color='#444', lw=0.7, ls=':')
    ax1.set_ylabel('Portfolio $', color='#999', fontsize=9)
    ax1.set_title(f'{coin}  FUTURES  ·  $100 capital  ·  Leveraged',
                  color='#ddd', fontsize=10, fontweight='bold', pad=8)
    ax1.legend(framealpha=0, labelcolor='white', fontsize=8)

    # ── Options equity ─────────────────────────────────────────────────────
    ax2 = fig.add_subplot(gs[0, 2:])
    style(ax2)
    ax2.plot(bah['_eq'].index, bah['_eq'], color=COLORS['Buy & Hold'],
             lw=1.5, ls='--', alpha=0.6, label='Buy & Hold')
    for r in opt:
        ax2.plot(r['_eq'].index, r['_eq'], color=COLORS.get(r['Strategy'],'#fff'),
                 lw=1.8, label=r['Strategy'])
    ax2.axhline(CAPITAL, color='#444', lw=0.7, ls=':')
    ax2.set_title(f'{coin}  OPTIONS  ·  $100 capital  ·  Monthly 30-day cycle',
                  color='#ddd', fontsize=10, fontweight='bold', pad=8)
    ax2.legend(framealpha=0, labelcolor='white', fontsize=8)

    # ── Drawdown ───────────────────────────────────────────────────────────
    ax3 = fig.add_subplot(gs[1, :])
    style(ax3)
    ax3.plot(bah['_dd'].index, bah['_dd']*100, color=COLORS['Buy & Hold'],
             lw=1.2, ls='--', alpha=0.6, label='Buy & Hold')
    for r in strats:
        ax3.fill_between(r['_dd'].index, r['_dd']*100, 0,
                         color=COLORS.get(r['Strategy'],'#fff'), alpha=0.12)
        ax3.plot(r['_dd'].index, r['_dd']*100,
                 color=COLORS.get(r['Strategy'],'#fff'), lw=1.2, label=r['Strategy'])
    ax3.set_ylabel('Drawdown %', color='#999', fontsize=9)
    ax3.set_title('Drawdown (all strategies)', color='#ddd', fontsize=10, pad=6)
    ax3.legend(framealpha=0, labelcolor='white', fontsize=7, ncol=5)
    ax3.yaxis.set_major_formatter(FuncFormatter(lambda v,_: f'{v:.0f}%'))

    # ── Return bar chart ───────────────────────────────────────────────────
    ax4 = fig.add_subplot(gs[2, :2])
    style(ax4)
    all_r = strats + [bah]
    labels  = [r['Strategy'] for r in all_r]
    returns = [r['Return_pct'] for r in all_r]
    bcolors = [COLORS.get(r['Strategy'],'#fff') for r in all_r]
    bars = ax4.bar(range(len(all_r)), returns, color=bcolors, width=0.6, edgecolor='none')
    ax4.set_xticks(range(len(all_r)))
    ax4.set_xticklabels(labels, color='#999', fontsize=7, rotation=18, ha='right')
    ax4.axhline(0, color='#444', lw=0.7)
    ax4.set_title('Total return % on $100', color='#ddd', fontsize=9, pad=5)
    for b, v in zip(bars, returns):
        c = '#2ecc71' if v >= 0 else '#e74c3c'
        ax4.text(b.get_x()+b.get_width()/2, v + (max(returns)*0.02 if v>=0 else -max(abs(r) for r in returns)*0.04),
                 f'{v:+.1f}%', ha='center', va='bottom' if v>=0 else 'top',
                 color=c, fontsize=7, fontweight='bold')

    # ── Sharpe bar chart ───────────────────────────────────────────────────
    ax5 = fig.add_subplot(gs[2, 2:])
    style(ax5)
    sharpes = [r['Sharpe'] for r in all_r]
    bars2   = ax5.bar(range(len(all_r)), sharpes, color=bcolors, width=0.6, edgecolor='none')
    ax5.set_xticks(range(len(all_r)))
    ax5.set_xticklabels(labels, color='#999', fontsize=7, rotation=18, ha='right')
    ax5.axhline(0, color='#444', lw=0.7)
    ax5.set_title('Sharpe ratio', color='#ddd', fontsize=9, pad=5)
    for b, v in zip(bars2, sharpes):
        ax5.text(b.get_x()+b.get_width()/2, v + 0.02,
                 f'{v:.2f}', ha='center', va='bottom', color='#ccc', fontsize=7)

    plt.suptitle(f'Crypto F&O Backtest  ·  {coin}  ·  Futures & Options  ·  '
                 f'{START} → {END}  ·  $100 capital',
                 color='white', fontsize=12, fontweight='bold', y=0.97)
    plt.savefig(out, dpi=150, bbox_inches='tight', facecolor='#0d0d0d')
    print(f'  Chart → {out}')
    plt.close()


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description='Crypto F&O backtester',
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""Examples:
  python fo_backtest.py                                  # defaults: 2023-01-01 → yesterday, $100
  python fo_backtest.py --start 2024-01-01               # from Jan 2024 to yesterday
  python fo_backtest.py --start 2024-06-01 --end 2024-12-31   # specific range
  python fo_backtest.py --end 2024-03-31                 # up to a specific date
  python fo_backtest.py --coin BTC-USD                   # only BTC
  python fo_backtest.py --coin ETH-USD --capital 500     # ETH with $500
  python fo_backtest.py --start 2024-01-01 --end 2024-12-31 --coin BTC-USD --capital 1000
        """)
    parser.add_argument('--demo',     action='store_true', help='Use simulated data (offline)')
    parser.add_argument('--start',    type=str, default=None,
                        help='Start date  YYYY-MM-DD  (default: 2023-01-01)')
    parser.add_argument('--end',      type=str, default=None,
                        help='End date    YYYY-MM-DD  (default: yesterday)')
    parser.add_argument('--coin',     type=str, default=None,
                        help='Single coin e.g. BTC-USD or ETH-USD (default: both)')
    parser.add_argument('--capital',  type=float, default=None,
                        help='Starting capital in USD (default: 100)')
    args = parser.parse_args()

    # apply overrides
    global START, END, CAPITAL, COINS
    if args.start:   START   = args.start
    if args.end:     END     = args.end
    if args.capital: CAPITAL = args.capital
    if args.coin:    COINS   = [args.coin]

    here    = os.path.dirname(os.path.abspath(__file__))
    all_csv = []

    print('=' * 65)
    print(f'  Crypto F&O Backtester  |  ${CAPITAL:,.0f}  |  {START} → {END}')
    print(f'  Coins : {", ".join(COINS)}')
    print('=' * 65)

    for coin_idx, coin in enumerate(COINS):
        print(f'\n{"─"*60}')
        print(f'  {coin}')

        if args.demo:
            df = simulate_coin(seed=42 + coin_idx)
            print(f'  {len(df)} simulated bars')
        else:
            try:
                df = fetch_live(coin)
            except Exception as e:
                print(f'  [!] yfinance error: {e} — using simulation')
                df = simulate_coin(seed=42 + coin_idx)

        results = []

        # ── Buy & Hold benchmark ──────────────────────────────────────────
        bah_eq = df['Close'] / df['Close'].iloc[0] * CAPITAL
        bah_tr = [{'win': bah_eq.iloc[-1] > CAPITAL,
                   'pnl_pct': (bah_eq.iloc[-1]/CAPITAL - 1)*100}]
        results.append(compute_metrics(bah_eq, bah_tr, 'Buy & Hold'))

        # ── FUTURES strategies ────────────────────────────────────────────
        st_sig = supertrend_signal(df)
        er_sig = ema_rsi_signal(df)

        fut_configs = [
            ('Supertrend 2x', st_sig, 2, False),
            ('EMA+RSI 2x L/S', er_sig, 2, True),
            ('Supertrend 3x', st_sig, 3, False),
        ]
        print(f'\n  FUTURES:')
        for name, sig, lev, ls in fut_configs:
            eq, trades = backtest_futures(df, sig, CAPITAL, leverage=lev, long_short=ls)
            m = compute_metrics(eq, trades, name)
            results.append(m)
            print(f'    {name:<18}  Final ${m["Final_USD"]:>8.2f}  '
                  f'Return {m["Return_pct"]:>+7.1f}%  '
                  f'Sharpe {m["Sharpe"]:>5.2f}  MaxDD {m["MaxDD_pct"]:>6.1f}%  '
                  f'Liq-safe trades {m["Trades"]}')

        # ── OPTIONS strategies ────────────────────────────────────────────
        opt_configs = [
            ('Long Call',      opt_long_call),
            ('Covered Call',   opt_covered_call),
            ('Protective Put', opt_protective_put),
            ('Long Straddle',  opt_straddle),
            ('Iron Condor',    opt_iron_condor),
        ]
        print(f'\n  OPTIONS  (30-day monthly cycle, Black-Scholes pricing):')
        for name, fn in opt_configs:
            eq, trades = backtest_options(df, fn, CAPITAL, name)
            m = compute_metrics(eq, trades, name)
            results.append(m)
            win_rate = f'{m["WinRate_pct"]:.0f}%' if m['Trades'] > 0 else 'n/a'
            print(f'    {name:<18}  Final ${m["Final_USD"]:>8.2f}  '
                  f'Return {m["Return_pct"]:>+7.1f}%  '
                  f'Sharpe {m["Sharpe"]:>5.2f}  WinRate {win_rate:>4s}  '
                  f'Cycles {m["Trades"]}')

        # ── Summary table ─────────────────────────────────────────────────
        cols = ['Strategy','Final_USD','PnL_USD','Return_pct','CAGR_pct',
                'Sharpe','MaxDD_pct','WinRate_pct','Trades','ProfitFactor']
        rn   = {'Final_USD':'Final $','PnL_USD':'P&L $','Return_pct':'Ret %',
                'CAGR_pct':'CAGR %','MaxDD_pct':'MaxDD %','WinRate_pct':'WinRt %',
                'ProfitFactor':'ProfF'}
        df_s = (pd.DataFrame([{k:r[k] for k in cols} for r in results])
                  .rename(columns=rn).set_index('Strategy'))

        print(f'\n{"="*90}')
        print(f'  {coin}  |  $100 invested  |  {START} → {END}')
        print(f'{"="*90}')
        print(df_s.to_string())
        print(f'{"="*90}')

        # Highlights
        best_ret  = max(results, key=lambda r: r['Return_pct'])
        best_shp  = max(results, key=lambda r: r['Sharpe'])
        least_dd  = min(results, key=lambda r: r['MaxDD_pct'])
        print(f'\n  ★ Best return     : {best_ret["Strategy"]:20s}  '
              f'${best_ret["Final_USD"]:.2f}  ({best_ret["Return_pct"]:+.1f}%)')
        print(f'  ★ Best risk-adj   : {best_shp["Strategy"]:20s}  '
              f'Sharpe {best_shp["Sharpe"]:.2f}')
        print(f'  ★ Lowest drawdown : {least_dd["Strategy"]:20s}  '
              f'MaxDD {least_dd["MaxDD_pct"]:.1f}%')

        # Plot
        out_img = os.path.join(here, f'fo_{coin.replace("-","_")}.png')
        plot_coin(results, coin, out_img)

        # CSV
        df_s.insert(0, 'Coin', coin)
        all_csv.append(df_s)

    # Combined CSV
    out_csv = os.path.join(here, 'fo_results.csv')
    pd.concat(all_csv).to_csv(out_csv)
    print(f'\n  Combined CSV → {out_csv}')
    print('\nDone.')


if __name__ == '__main__':
    main()
