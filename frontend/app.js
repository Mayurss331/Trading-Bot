'use strict';

// ─── State ───────────────────────────────────────────────────────────────────
const state = {
  ws: null,
  wsLastTick: 0,
  wsStaleTimer: null,
  priceChart: null,
  candleSeries: null,
  superSeries: null,
  trackingInterval: null,
  selectedCoins: new Set(['BTC', 'ETH', 'SOL']),
  availableCoins: [],
  intelligenceInterval: null,
  lastSnapshot: null,
};

// ─── DOM refs ─────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const el = {
  pair:          $('pairInput'),
  market:        $('marketInput'),
  strategy:      $('strategyInput'),
  mode:          $('modeInput'),
  risk:          $('riskInput'),
  lookback:      $('lookbackInput'),
  refresh:       $('refreshButton'),
  feedText:      $('feedText'),
  lastPrice:     $('lastPrice'),
  changePct:     $('changePct'),
  signalText:    $('signalText'),
  signalMeta:    $('signalMeta'),
  scoreText:     $('scoreText'),
  rsiText:       $('rsiText'),
  positionText:  $('positionText'),
  positionMeta:  $('positionMeta'),
  wsDot:         $('wsDot'),
  wsLabel:       $('wsLabel'),
  priceChartTitle: $('priceChartTitle'),
  priceChartSub:   $('priceChartSub'),
  priceChart:    $('priceChart'),
  scoreChart:    $('scoreChart'),
  rsiChart:      $('rsiChart'),
  strategyList:  $('strategyList'),
  stateList:     $('stateList'),
  eventList:     $('eventList'),
  coinPicker:    $('coinPicker'),
  coinSearch:    $('coinSearchInput'),
  customCoin:    $('customCoinInput'),
  addCoin:       $('addCoinButton'),
  startTracking: $('startTrackingButton'),
  stopTracking:  $('stopTrackingButton'),
  trackingMeta:  $('trackingMeta'),
  trackingBody:  $('trackingBody'),
  walletBalance: $('walletBalance'),
  walletStatus:  $('walletStatus'),
  currentRisk:   $('currentRiskText'),
  riskButtons:   $('riskButtons'),
  customRisk:    $('customRiskInput'),
  applyRisk:     $('applyCustomRiskButton'),
  refreshAccount:$('refreshAccountButton'),
  positionsMeta: $('positionsMeta'),
  positionsBody: $('positionsBody'),
  signalsBody:   $('signalsBody'),
  rankBody:      $('rankBody'),
  chainSelect:   $('chainSelect'),
  journalBody:   $('journalBody'),
  refreshJournal:$('refreshJournalButton'),
};

// ─── Formatting helpers ───────────────────────────────────────────────────────
function fmtPrice(v) {
  if (v == null) return '—';
  const n = parseFloat(v);
  if (isNaN(n)) return '—';
  if (n >= 1000)  return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (n >= 1)     return n.toFixed(4);
  return n.toFixed(8);
}

function fmtPct(v) {
  if (v == null) return '—';
  const n = parseFloat(v);
  if (isNaN(n)) return '—';
  const sign = n >= 0 ? '+' : '';
  return `${sign}${n.toFixed(2)}%`;
}

function fmtNum(v, dp = 2) {
  if (v == null) return '—';
  const n = parseFloat(v);
  if (isNaN(n)) return '—';
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(2) + 'K';
  return n.toFixed(dp);
}

function fmtTs(iso) {
  if (!iso) return '—';
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

// ─── WebSocket ────────────────────────────────────────────────────────────────
function setWsStatus(status) {
  el.wsDot.className = `ws-dot ${status}`;
  const labels = { live: 'Live', stale: 'Stale', error: 'Disconnected', '': 'Connecting' };
  el.wsLabel.textContent = labels[status] || 'Connecting';
}

function connectWebSocket() {
  const pair   = el.pair.value.trim()   || 'B-ETH_USDT';
  const market = el.market.value.trim() || 'ETHUSDT';
  const mode   = el.mode.value || 'futures';

  if (state.ws) { try { state.ws.close(); } catch {} }
  setWsStatus('');

  const url = `ws://${location.host}/ws/quotes?pair=${encodeURIComponent(pair)}&market=${encodeURIComponent(market)}&mode=${mode}&interval=3`;
  const ws = new WebSocket(url);
  state.ws = ws;

  ws.onopen = () => setWsStatus('live');

  ws.onmessage = ({ data }) => {
    try {
      const quote = JSON.parse(data);
      state.wsLastTick = Date.now();
      setWsStatus('live');

      if (quote.last_price != null) {
        el.lastPrice.textContent = fmtPrice(quote.last_price);
        if (state.candleSeries && quote.last_price) {
          // Update last candle close with live price
          const bars = state.lastSnapshot?.bars;
          if (bars && bars.length > 0) {
            const last = bars[bars.length - 1];
            const t = Math.floor(new Date(last.time).getTime() / 1000);
            try {
              state.candleSeries.update({
                time: t,
                open: last.open, high: Math.max(last.high, quote.last_price),
                low: Math.min(last.low, quote.last_price), close: quote.last_price,
              });
            } catch {}
          }
        }
      }
    } catch {}

    // Stale detection: if no tick for 15s mark stale
    clearTimeout(state.wsStaleTimer);
    state.wsStaleTimer = setTimeout(() => setWsStatus('stale'), 15_000);
  };

  ws.onerror = () => setWsStatus('error');
  ws.onclose = () => {
    setWsStatus('error');
    setTimeout(connectWebSocket, 5000);
  };
}

// ─── Price chart (lightweight-charts) ────────────────────────────────────────
function initPriceChart() {
  if (typeof LightweightCharts === 'undefined') {
    console.warn('LightweightCharts not available; skipping chart init.');
    state.priceChart = null;
    state.candleSeries = null;
    state.superSeries = null;
    return;
  }
  if (state.priceChart) {
    state.priceChart.remove();
    state.priceChart = null;
    state.candleSeries = null;
    state.superSeries = null;
  }
  const container = el.priceChart;
  const chart = LightweightCharts.createChart(container, {
    layout: {
      background: { color: '#181c22' },
      textColor: '#5a6270',
    },
    grid: {
      vertLines: { color: '#1e2430' },
      horzLines: { color: '#1e2430' },
    },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderColor: '#252b34' },
    timeScale: { borderColor: '#252b34', timeVisible: true, secondsVisible: false },
    width: container.clientWidth,
    height: container.clientHeight,
  });

  const hasNewSeriesApi = typeof chart.addSeries === 'function';
  const candleOptions = {
    upColor: '#26c485', downColor: '#e05252',
    borderUpColor: '#26c485', borderDownColor: '#e05252',
    wickUpColor: '#26c485', wickDownColor: '#e05252',
  };
  const lineOptions = {
    color: '#5b9cf6', lineWidth: 1.5, priceLineVisible: false, lastValueVisible: false,
  };

  if (hasNewSeriesApi && LightweightCharts?.CandlestickSeries && LightweightCharts?.LineSeries) {
    state.candleSeries = chart.addSeries(LightweightCharts.CandlestickSeries, candleOptions);
    state.superSeries = chart.addSeries(LightweightCharts.LineSeries, lineOptions);
  } else {
    state.candleSeries = chart.addCandlestickSeries(candleOptions);
    state.superSeries = chart.addLineSeries(lineOptions);
  }

  const ro = new ResizeObserver(() => {
    chart.applyOptions({ width: container.clientWidth, height: container.clientHeight });
  });
  ro.observe(container);
  state.priceChart = chart;
}

function updatePriceChart(bars) {
  if (!state.candleSeries || !bars || bars.length === 0) return;

  const candles = bars
    .filter(b => b.open != null && b.high != null && b.low != null && b.close != null)
    .map(b => ({
      time: Math.floor(new Date(b.time).getTime() / 1000),
      open: b.open, high: b.high, low: b.low, close: b.close,
    }));

  const supertrend = bars
    .filter(b => b.supertrend != null)
    .map(b => ({ time: Math.floor(new Date(b.time).getTime() / 1000), value: b.supertrend }));

  try { state.candleSeries.setData(candles); } catch {}
  try { state.superSeries.setData(supertrend); } catch {}
}

// ─── Score chart (custom canvas) ─────────────────────────────────────────────
function drawScoreChart(bars) {
  const canvas = el.scoreChart;
  const ctx = canvas.getContext('2d');
  canvas.width = canvas.offsetWidth;
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  const scores = bars.map(b => b.score ?? null).filter(s => s != null);
  if (scores.length === 0) return;

  const W = canvas.width, H = canvas.height;
  const SCORE_MIN = -5, SCORE_MAX = 5;
  const barW = Math.max(1, Math.floor(W / scores.length));
  const zeroY = H / 2;
  const scale = H / (SCORE_MAX - SCORE_MIN) / 2;
  const ENTRY_THRESH = 3;

  // Threshold lines
  ctx.strokeStyle = '#252b34';
  ctx.setLineDash([3, 3]);
  ctx.lineWidth = 1;
  [ENTRY_THRESH, -ENTRY_THRESH].forEach(s => {
    const y = zeroY - s * scale;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
  });
  ctx.setLineDash([]);

  // Zero line
  ctx.strokeStyle = '#1e2430';
  ctx.beginPath(); ctx.moveTo(0, zeroY); ctx.lineTo(W, zeroY); ctx.stroke();

  // Bars
  scores.slice(-Math.floor(W / barW)).forEach((s, i) => {
    const x = i * barW;
    const barH = s * scale;
    const y = barH >= 0 ? zeroY - barH : zeroY;
    ctx.fillStyle = s >= ENTRY_THRESH ? '#26c485' : s <= -ENTRY_THRESH ? '#e05252' : '#3a4250';
    ctx.fillRect(x, Math.min(y, zeroY), barW - 1, Math.abs(barH));
  });
}

// ─── RSI chart (custom canvas) ────────────────────────────────────────────────
function drawRsiChart(bars) {
  const canvas = el.rsiChart;
  const ctx = canvas.getContext('2d');
  canvas.width = canvas.offsetWidth;
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  const rsiVals = bars.map(b => b.rsi).filter(v => v != null);
  if (rsiVals.length < 2) return;

  const W = canvas.width, H = canvas.height;
  const pad = 4;

  const zone = (level, color) => {
    const y = H - (level / 100) * H;
    ctx.strokeStyle = color; ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
    ctx.setLineDash([]);
  };
  zone(70, '#e05252'); zone(50, '#252b34'); zone(30, '#26c485');

  const step = (W - 2 * pad) / (rsiVals.length - 1);
  ctx.strokeStyle = '#5b9cf6'; ctx.lineWidth = 1.5;
  ctx.beginPath();
  rsiVals.forEach((v, i) => {
    const x = pad + i * step;
    const y = H - (v / 100) * H;
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.stroke();
}

// ─── Snapshot ─────────────────────────────────────────────────────────────────
async function loadSnapshot() {
  const pair   = el.pair.value.trim()   || 'B-ETH_USDT';
  const market = el.market.value.trim() || 'ETHUSDT';
  const strategy = el.strategy.value || 'confluence';
  const mode     = el.mode.value || 'futures';
  const risk     = parseFloat(el.risk.value) || 10;
  const lookback = parseInt(el.lookback.value) || 3;

  let data;
  try {
    const res = await fetch(
      `/api/snapshot?pair=${encodeURIComponent(pair)}&market=${encodeURIComponent(market)}` +
      `&strategy=${strategy}&mode=${mode}&risk=${risk}&lookback_days=${lookback}`
    );
    data = await res.json();
  } catch (err) {
    console.error('Snapshot fetch failed', err);
    return;
  }

  if (!data.ok) {
    el.feedText.textContent = `Error: ${data.message || 'fetch failed'}`;
    return;
  }

  state.lastSnapshot = data;

  // Feed / title
  el.feedText.textContent = `Feed: ${data.used_pair || pair} via ${data.used_source || '—'} · ${data.freshness_minutes?.toFixed(1) ?? '?'}m ago`;
  el.priceChartTitle.textContent = `${data.coin || 'Price'} · ${data.strategy?.chart_label || 'Strategy'}`;
  el.priceChartSub.textContent = `${data.strategy?.name || ''} · ${data.mode?.toUpperCase()}`;

  const stats = data.stats || {};
  const action = data.action || {};
  const stateData = data.state || {};
  const score = stats.score ?? null;
  const rsi   = stats.rsi   ?? null;

  // Metrics
  el.lastPrice.textContent = fmtPrice(stats.close);
  el.changePct.textContent = stats.change_pct != null ? fmtPct(stats.change_pct) : '—';
  el.changePct.className = 'metric-sub ' + (stats.change_pct >= 0 ? 'score-pos' : 'score-neg');

  el.signalText.textContent = action.label || '—';
  el.signalText.className = 'metric-value ' + (action.side === 'LONG' ? 'long' : action.side === 'SHORT' ? 'short' : '');
  el.signalMeta.textContent = `${action.type || '—'} · ${action.side || '—'}`;

  el.scoreText.textContent = score != null ? `${score > 0 ? '+' : ''}${score} / ${rsi?.toFixed(1) || '—'}` : '—';
  el.scoreText.className = 'metric-value ' + (score >= 3 ? 'long' : score <= -3 ? 'short' : '');
  el.rsiText.textContent = rsi != null ? `RSI ${rsi.toFixed(1)}` : '—';

  const side = stateData.side;
  el.positionText.textContent = side || 'FLAT';
  el.positionText.className = 'metric-value ' + (side === 'LONG' ? 'long' : side === 'SHORT' ? 'short' : '');
  el.positionMeta.textContent = stateData.realized_pnl != null
    ? `Trade #${stateData.trade_id || 0} · PnL $${parseFloat(stateData.realized_pnl).toFixed(2)}`
    : '—';

  // Charts
  const bars = data.bars || [];
  updatePriceChart(bars);
  drawScoreChart(bars);
  drawRsiChart(bars);

  // Strategy details
  const strat = data.strategy || {};
  el.strategyList.innerHTML = [
    ['Name', strat.name],
    ['Description', strat.description],
    ['Reason', strat.reason],
    ...(strat.notes || []).map((n, i) => [`Note ${i + 1}`, n]),
  ].map(([k, v]) => v ? `<dt>${k}</dt><dd>${v}</dd>` : '').join('');

  // Trade state
  const stateItems = [
    ['Side', stateData.side], ['Trade #', stateData.trade_id],
    ['Entry', stateData.entry_px ? fmtPrice(stateData.entry_px) : null],
    ['Stop', stateData.stop_px ? fmtPrice(stateData.stop_px) : null],
    ['Target', stateData.target_px ? fmtPrice(stateData.target_px) : null],
    ['Qty', stateData.qty ? parseFloat(stateData.qty).toFixed(6) : null],
    ['PnL', stateData.realized_pnl != null ? `$${parseFloat(stateData.realized_pnl).toFixed(2)}` : null],
    ['Broker Status', stateData.broker_order_status],
  ];
  el.stateList.innerHTML = stateItems
    .filter(([, v]) => v != null)
    .map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`)
    .join('');

  // Events
  const events = data.events || [];
  el.eventList.innerHTML = events.length === 0
    ? '<li>No events yet.</li>'
    : [...events].reverse().slice(0, 20).map(e => {
        const cls = e.toLowerCase().includes('entry') ? 'entry-event'
                  : e.toLowerCase().includes('exit')  ? 'exit-event' : '';
        return `<li class="${cls}">${e}</li>`;
      }).join('');

  // Reconnect WS with new pair/mode if changed
  connectWebSocket();
}

// ─── Intelligence ─────────────────────────────────────────────────────────────
async function loadIntelligence() {
  const chain = el.chainSelect?.value || 'CT_501';

  // Signals
  try {
    const res = await fetch(`/api/intelligence/signals?chain=${chain}`);
    const data = await res.json();
    const signals = (data.signals || []).slice(0, 15);
    if (signals.length === 0) {
      el.signalsBody.innerHTML = '<tr><td colspan="6" class="empty-row">No active signals.</td></tr>';
    } else {
      el.signalsBody.innerHTML = signals.map(s => {
        const dirCls = s.direction === 'buy' ? 'dir-buy' : 'dir-sell';
        const statusCls = s.status === 'active' ? 'status-active' : 'status-timeout';
        return `<tr>
          <td>${s.ticker || '—'}</td>
          <td class="${dirCls}">${(s.direction || '—').toUpperCase()}</td>
          <td>${s.smartMoneyCount ?? '—'}</td>
          <td>${s.maxGain != null ? '+' + parseFloat(s.maxGain).toFixed(1) + '%' : '—'}</td>
          <td>${s.exitRate != null ? s.exitRate + '%' : '—'}</td>
          <td class="${statusCls}">${s.status || '—'}</td>
        </tr>`;
      }).join('');
    }
  } catch {
    el.signalsBody.innerHTML = '<tr><td colspan="6" class="empty-row">Failed to load.</td></tr>';
  }

  // Inflow rank
  try {
    const res = await fetch(`/api/intelligence/rankings?chain=${chain}&period=24h`);
    const data = await res.json();
    const rankings = (data.rankings || []).slice(0, 10);
    if (rankings.length === 0) {
      el.rankBody.innerHTML = '<tr><td colspan="5" class="empty-row">No data.</td></tr>';
    } else {
      el.rankBody.innerHTML = rankings.map((r, i) => {
        const chg = parseFloat(r.priceChangeRate) || 0;
        const chgCls = chg >= 0 ? 'score-pos' : 'score-neg';
        return `<tr>
          <td>${i + 1}</td>
          <td>${r.tokenName || '—'}</td>
          <td class="${chgCls}">${fmtPct(chg)}</td>
          <td>${fmtNum(r.inflow)}</td>
          <td>${r.traders ?? '—'}</td>
        </tr>`;
      }).join('');
    }
  } catch {
    el.rankBody.innerHTML = '<tr><td colspan="5" class="empty-row">Failed to load.</td></tr>';
  }
}

function startIntelligencePolling() {
  loadIntelligence();
  clearInterval(state.intelligenceInterval);
  state.intelligenceInterval = setInterval(loadIntelligence, 60_000);
}

// ─── Account ──────────────────────────────────────────────────────────────────
async function loadAccount() {
  const pair   = el.pair.value.trim()   || 'B-ETH_USDT';
  const market = el.market.value.trim() || 'ETHUSDT';
  const mode   = el.mode.value || 'futures';
  try {
    const res = await fetch(`/api/account?pair=${encodeURIComponent(pair)}&market=${encodeURIComponent(market)}&mode=${mode}`);
    const data = await res.json();

    if (!data.has_credentials) {
      el.walletBalance.textContent = '—';
      el.walletStatus.textContent = data.message || 'Add API keys in .env';
      el.currentRisk.textContent = `$${parseFloat(el.risk.value).toFixed(2)}`;
      return;
    }

    const avail = parseFloat(data.available_quote_balance) || 0;
    el.walletBalance.textContent = `${avail.toFixed(2)} ${data.currency || 'USDT'}`;
    el.walletStatus.textContent = '';
    el.currentRisk.textContent = `$${parseFloat(el.risk.value).toFixed(2)}`;

    // Risk buttons
    el.riskButtons.innerHTML = (data.risk_suggestions || []).map(s =>
      `<button class="risk-btn" data-amount="${s.amount.toFixed(4)}">${s.label} ($${s.amount.toFixed(2)})</button>`
    ).join('');
    el.riskButtons.querySelectorAll('.risk-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        el.risk.value = btn.dataset.amount;
        el.currentRisk.textContent = `$${parseFloat(btn.dataset.amount).toFixed(2)}`;
      });
    });

    // Positions
    const positions = data.positions || [];
    el.positionsMeta.textContent = positions.length ? `${positions.length} open` : 'No open positions';
    if (positions.length === 0) {
      el.positionsBody.innerHTML = '<tr><td colspan="8" class="empty-row">No active positions.</td></tr>';
    } else {
      el.positionsBody.innerHTML = positions.map(p => {
        const pnl = parseFloat(p.unrealized_pnl) || 0;
        const pnlCls = pnl >= 0 ? 'score-pos' : 'score-neg';
        return `<tr>
          <td>${p.coin}</td>
          <td class="${p.side === 'LONG' ? 'dir-buy' : 'dir-sell'}">${p.side}</td>
          <td>${fmtNum(p.quantity, 4)}</td>
          <td>${fmtPrice(p.avg_price)}</td>
          <td>${fmtPrice(p.mark_price)}</td>
          <td>${fmtPrice(p.stop_loss_trigger)}</td>
          <td>${fmtPrice(p.take_profit_trigger)}</td>
          <td class="${pnlCls}">${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}</td>
        </tr>`;
      }).join('');
    }
  } catch (err) {
    console.error('Account load failed', err);
  }
}

// ─── Trade journal ────────────────────────────────────────────────────────────
async function loadTradeJournal() {
  try {
    const res = await fetch('/api/trades?limit=50');
    const data = await res.json();
    const trades = data.trades || [];
    if (trades.length === 0) {
      el.journalBody.innerHTML = '<tr><td colspan="7" class="empty-row">No trades logged yet.</td></tr>';
      return;
    }
    el.journalBody.innerHTML = trades.map(t => {
      const pnl = t.pnl != null ? parseFloat(t.pnl) : null;
      const pnlCls = pnl == null ? '' : pnl >= 0 ? 'pnl-pos' : 'pnl-neg';
      const sideLabel = t.side === 1 ? 'LONG' : 'SHORT';
      const sideCls   = t.side === 1 ? 'dir-buy' : 'dir-sell';
      return `<tr>
        <td>${t.id}</td>
        <td>${t.pair || '—'}</td>
        <td class="${sideCls}">${sideLabel}</td>
        <td class="mono">${fmtPrice(t.entry_px)}</td>
        <td class="mono">${fmtPrice(t.exit_px)}</td>
        <td class="${pnlCls}">${pnl != null ? (pnl >= 0 ? '+' : '') + pnl.toFixed(2) : '—'}</td>
        <td>${t.exit_reason || '—'}</td>
      </tr>`;
    }).join('');
  } catch {
    el.journalBody.innerHTML = '<tr><td colspan="7" class="empty-row">Failed to load.</td></tr>';
  }
}

// ─── Futures tracker ──────────────────────────────────────────────────────────
async function loadFuturesMarkets() {
  try {
    const res = await fetch('/api/futures-markets');
    const data = await res.json();
    state.availableCoins = (data.coins || []).map(c => c.coin);
    renderCoinPicker();
  } catch {}
}

function renderCoinPicker() {
  const search = (el.coinSearch.value || '').toUpperCase();
  const coins = state.availableCoins.filter(c => !search || c.includes(search));
  el.coinPicker.innerHTML = coins.map(coin => {
    const sel = state.selectedCoins.has(coin);
    return `<button class="coin-pill${sel ? ' selected' : ''}" data-coin="${coin}">${coin}</button>`;
  }).join('');
  el.coinPicker.querySelectorAll('.coin-pill').forEach(btn => {
    btn.addEventListener('click', () => {
      const coin = btn.dataset.coin;
      if (state.selectedCoins.has(coin)) state.selectedCoins.delete(coin);
      else if (state.selectedCoins.size < 12) state.selectedCoins.add(coin);
      renderCoinPicker();
    });
  });
}

async function loadTracking() {
  const coins = [...state.selectedCoins].join(',');
  if (!coins) return;
  const strategy = el.strategy.value || 'confluence';
  const risk = parseFloat(el.risk.value) || 10;
  el.trackingMeta.textContent = `Tracking ${state.selectedCoins.size} coin(s)… (last update: ${new Date().toLocaleTimeString()})`;
  try {
    const res = await fetch(`/api/track?coins=${encodeURIComponent(coins)}&strategy=${strategy}&risk=${risk}&lookback_days=2`);
    const data = await res.json();
    const rows = data.tracked || [];
    if (rows.length === 0) {
      el.trackingBody.innerHTML = '<tr><td colspan="7" class="empty-row">No data.</td></tr>';
      return;
    }
    el.trackingBody.innerHTML = rows.map(r => {
      if (!r.ok) return `<tr><td colspan="7">${r.coin}: ${r.message || 'error'}</td></tr>`;
      const score = r.score != null ? parseFloat(r.score) : null;
      const scoreCls = score >= 3 ? 'score-pos' : score <= -3 ? 'score-neg' : 'score-neu';
      const sideCls  = r.position === 'LONG' ? 'dir-buy' : r.position === 'SHORT' ? 'dir-sell' : '';
      const fresh = r.freshness_minutes != null ? r.freshness_minutes.toFixed(0) + 'm' : '—';
      return `<tr>
        <td>${r.coin}</td>
        <td class="mono">${fmtPrice(r.last_price)}</td>
        <td>${r.signal || '—'}</td>
        <td class="${scoreCls}">${score != null ? (score > 0 ? '+' : '') + score : '—'}</td>
        <td>${r.rsi != null ? parseFloat(r.rsi).toFixed(1) : '—'}</td>
        <td class="${sideCls}">${r.position || 'FLAT'}</td>
        <td>${fresh}</td>
      </tr>`;
    }).join('');
  } catch {}
}

function startTracking() {
  loadTracking();
  clearInterval(state.trackingInterval);
  state.trackingInterval = setInterval(loadTracking, 15_000);
}

function stopTracking() {
  clearInterval(state.trackingInterval);
  state.trackingInterval = null;
  el.trackingMeta.textContent = 'Tracking stopped.';
}

// ─── Event wiring ─────────────────────────────────────────────────────────────
function wireEvents() {
  el.refresh.addEventListener('click', loadSnapshot);
  el.refreshAccount.addEventListener('click', loadAccount);
  el.refreshJournal.addEventListener('click', loadTradeJournal);
  el.startTracking.addEventListener('click', startTracking);
  el.stopTracking.addEventListener('click', stopTracking);
  el.addCoin.addEventListener('click', () => {
    const coin = el.customCoin.value.trim().toUpperCase();
    if (coin && state.selectedCoins.size < 12) {
      state.selectedCoins.add(coin);
      if (!state.availableCoins.includes(coin)) state.availableCoins.push(coin);
      el.customCoin.value = '';
      renderCoinPicker();
    }
  });
  el.coinSearch.addEventListener('input', renderCoinPicker);
  el.applyRisk.addEventListener('click', () => {
    const v = parseFloat(el.customRisk.value);
    if (v > 0) { el.risk.value = v; el.currentRisk.textContent = `$${v.toFixed(2)}`; }
  });
  if (el.chainSelect) {
    el.chainSelect.addEventListener('change', loadIntelligence);
  }

  // Keyboard shortcut: Enter on pair/market inputs triggers refresh
  [el.pair, el.market].forEach(inp => inp.addEventListener('keydown', e => {
    if (e.key === 'Enter') loadSnapshot();
  }));
}

// ─── Init ─────────────────────────────────────────────────────────────────────
async function init() {
  initPriceChart();
  wireEvents();
  connectWebSocket();
  startIntelligencePolling();

  await loadFuturesMarkets();
  await loadSnapshot();
  loadAccount();
  loadTradeJournal();
}

document.addEventListener('DOMContentLoaded', init);
