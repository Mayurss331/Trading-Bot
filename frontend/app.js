'use strict';

const EXECUTION_PREF_KEY = 'coindcx-dashboard.execution-mode';
const DASHBOARD_PREF_KEY = 'coindcx-dashboard.preferences';
const DEFAULT_TIMEFRAME = '15m';
const DEFAULT_STRATEGY = 'confluence';
const DEFAULT_CUSTOM_STRATEGY_CODE = `from strategies.base import LONG, SHORT, StrategyMeta, base_frame, ema, finalize

META = StrategyMeta(
    id="ema_pullback",
    name="EMA Pullback Strategy",
    description="EMA trend filter with pullback entries.",
    chart_label="EMA 21",
    score_label="Score",
)

def analyze(bars, ctx):
    frame = base_frame(bars)
    frame["ema_fast"] = ema(frame["Close"], 9)
    frame["ema_slow"] = ema(frame["Close"], 21)
    frame["score"] = 0
    frame.loc[frame["ema_fast"] > frame["ema_slow"], "score"] = 2
    frame.loc[frame["ema_fast"] < frame["ema_slow"], "score"] = -2
    frame.loc[(frame["score"] > 0) & (frame["rsi"] > 50), "entry_side"] = LONG
    frame.loc[(frame["score"] < 0) & (frame["rsi"] < 50), "entry_side"] = SHORT
    frame["exit_long"] = frame["ema_fast"] < frame["ema_slow"]
    frame["exit_short"] = frame["ema_fast"] > frame["ema_slow"]
    frame["st_line"] = frame["ema_slow"]
    frame["reason"] = "EMA 9/21 trend alignment with RSI confirmation."
    return finalize(META, frame, ctx)
`;

function normalizeTimeframe(value) {
  if (!value) return DEFAULT_TIMEFRAME;
  return value;
}

function normalizeStrategy(value) {
  return value || DEFAULT_STRATEGY;
}

// ─── State ───────────────────────────────────────────────────────────────────
const state = {
  ws: null,
  wsKey: '',
  wsLastTick: 0,
  wsStaleTimer: null,
  wsReconnectTimer: null,
  priceChart: null,
  candleSeries: null,
  superSeries: null,
  trackingInterval: null,
  selectedCoins: new Set(['BTC', 'ETH', 'SOL']),
  availableCoins: [],
  intelligenceInterval: null,
  lastSnapshot: null,
  realOrdersArmed: false,
  trackingActive: false,
  trackerCollapsed: false,
  preferencesSaveTimer: null,
  theme: 'dark',
  targetLine: null,
  stopLine: null,
  fvgHighLine: null,
  fvgLowLine: null,
  sweepMarkers: null,
  livePositions: [],
  expertPicksInterval: null,
  reportsFilter: 'all',
  customStrategies: [],
  builtinBacktestStrategies: [],
  selectedCustomStrategyId: null,
  lastBacktestRunId: null,
  activeBacktestJobId: null,
  backtestJobTimer: null,
};

// ─── DOM refs ─────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const el = {
  pair:          $('pairInput'),
  market:        $('marketInput'),
  strategy:      $('strategyInput'),
  mode:          $('modeInput'),
  realOrders:    $('realOrdersToggle'),
  executionModeLabel: $('executionModeLabel'),
  risk:          $('riskInput'),
  lookback:      $('lookbackInput'),
  timeframe:     $('timeframeInput'),
  refresh:       $('refreshButton'),
  menuButton:    $('menuButton'),
  sideDrawer:    $('sideDrawer'),
  drawerBackdrop:$('drawerBackdrop'),
  drawerClose:   $('drawerCloseButton'),
  themeSelect:   $('themeSelect'),
  trackerSidebar:$('trackerSidebar'),
  trackerCollapse:$('trackerCollapseBtn'),
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
  positionsCount:$('positionsCount'),
  accountPnl:    $('accountPnl'),
  accountMode:   $('accountModeText'),
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
  pair2:         $('pair2Input'),
  market2:       $('market2Input'),
  trackedCoinsMenu: $('trackedCoinsMenu'),
  coinsMenuItems: $('coinsMenuItems'),
  volScanButton: $('volScanButton'),
  volScanBody:   $('volScanBody'),
  volScanMeta:   $('volScanMeta'),
  volSidebar:    $('volSidebar'),
  volSidebarClose: $('volSidebarClose'),
  actExpertPicks: $('actExpertPicks'),
  expertSidebar:  $('expertSidebar'),
  expertSidebarClose: $('expertSidebarClose'),
  expertRefreshBtn: $('expertRefreshBtn'),
  sentimentValue: $('sentimentValue'),
  sentimentClass: $('sentimentClass'),
  sentimentTime: $('sentimentTime'),
  expertPosBody:  $('expertPosBody'),
  expertSignalsTable: $('expertSignalsTable'),
  actDashboard:  $('actDashboard'),
  actVolScanner: $('actVolScanner'),
  actReports:    $('actReports'),
  actBacktests:  $('actBacktests'),
  reportsSidebar: $('reportsSidebar'),
  reportsSidebarClose: $('reportsSidebarClose'),
  reportsFilterGroup: $('reportsFilterGroup'),
  reportsRefreshBtn: $('reportsRefreshBtn'),
  reportsClearHistoryBtn: $('reportsClearHistoryBtn'),
  reportsClearConfirm: $('reportsClearConfirm'),
  reportsClearConfirmInput: $('reportsClearConfirmInput'),
  reportsClearConfirmBtn: $('reportsClearConfirmBtn'),
  reportsClearCancelBtn: $('reportsClearCancelBtn'),
  reportsStatus: $('reportsStatus'),
  reportsOverview: $('reportsOverview'),
  reportsTableBody: $('reportsTableBody'),
  rStatTotal:    $('rStatTotal'),
  rStatWinRate:  $('rStatWinRate'),
  rStatNetPnl:   $('rStatNetPnl'),
  rStatPF:       $('rStatPF'),
  rStatDD:       $('rStatDD'),
  rStatDur:      $('rStatDur'),
  reportEmailInput: $('reportEmailInput'),
  btnSendReport: $('btnSendReport'),
  backtestsSidebar: $('backtestsSidebar'),
  backtestsSidebarClose: $('backtestsSidebarClose'),
  backtestsRefreshBtn: $('backtestsRefreshBtn'),
  customStrategySelect: $('customStrategySelect'),
  customStrategyTitle: $('customStrategyTitle'),
  customStrategySlug: $('customStrategySlug'),
  customStrategyDescription: $('customStrategyDescription'),
  customStrategyCode: $('customStrategyCode'),
  customStrategyEnabled: $('customStrategyEnabled'),
  customStrategyStatus: $('customStrategyStatus'),
  newCustomStrategyBtn: $('newCustomStrategyBtn'),
  saveCustomStrategyBtn: $('saveCustomStrategyBtn'),
  validateCustomStrategyBtn: $('validateCustomStrategyBtn'),
  backtestStrategySelect: $('backtestStrategySelect'),
  btTimeframeSelect: $('btTimeframeSelect'),
  btInitialCapital: $('btInitialCapital'),
  btRisk: $('btRisk'),
  btLookback: $('btLookback'),
  btLookbackHint: $('btLookbackHint'),
  btWarmupBars: $('btWarmupBars'),
  btCommission: $('btCommission'),
  btSpread: $('btSpread'),
  btSlippage: $('btSlippage'),
  btLeverage: $('btLeverage'),
  btRiskReward: $('btRiskReward'),
  btTargetMode: $('btTargetMode'),
  btFillModel: $('btFillModel'),
  btSizing: $('btSizing'),
  btRiskMode: $('btRiskMode'),
  btOppositeMode: $('btOppositeMode'),
  btSameBarPriority: $('btSameBarPriority'),
  btAllowShorts: $('btAllowShorts'),
  btFinalizeOpen: $('btFinalizeOpen'),
  btAiVerify: $('btAiVerify'),
  btAiMinConfidence: $('btAiMinConfidence'),
  btAiCandles: $('btAiCandles'),
  runBacktestBtn: $('runBacktestBtn'),
  backtestStatus: $('backtestStatus'),
  backtestSummary: $('backtestSummary'),
  backtestEquityChart: $('backtestEquityChart'),
  backtestTradesBody: $('backtestTradesBody'),
  backtestEvents: $('backtestEvents'),
  btTradesCsv: $('btTradesCsv'),
  btEquityCsv: $('btEquityCsv'),
  toastMsg:      $('toastMsg'),
};

// ─── Formatting helpers ───────────────────────────────────────────────────────
function fmtPrice(v) {
  if (v == null) return '—';
  const n = parseFloat(v);
  if (isNaN(n)) return '—';
  return String(n);
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

function escapeHtml(v) {
  if (v == null) return '';
  return String(v).replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

function fmtReportNum(v, maxDp = 8) {
  if (v == null) return '—';
  const n = parseFloat(v);
  if (!Number.isFinite(n)) return '—';
  if (n === 0) return '0';
  const abs = Math.abs(n);
  const dp = abs >= 100 ? 2 : abs >= 1 ? 4 : maxDp;
  return n.toFixed(dp).replace(/\.?0+$/, '');
}

function fmtTs(iso) {
  if (!iso) return '—';
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

function currentRiskAmount() {
  const n = parseFloat(el.risk.value);
  return Number.isFinite(n) && n > 0 ? n : 0;
}

function cssVar(name) {
  return getComputedStyle(document.body).getPropertyValue(name).trim();
}

function applyTheme(theme) {
  const allowed = new Set(['dark', 'light', 'contrast']);
  state.theme = allowed.has(theme) ? theme : 'dark';
  document.body.dataset.theme = state.theme;
  if (el.themeSelect) el.themeSelect.value = state.theme;

  if (state.priceChart) {
    initPriceChart();
    if (state.lastSnapshot?.bars) {
      updatePriceChart(state.lastSnapshot.bars);
      drawScoreChart(state.lastSnapshot.bars);
      drawRsiChart(state.lastSnapshot.bars);
    }
  }
}

function setDrawerOpen(open) {
  el.sideDrawer.classList.toggle('open', open);
  el.sideDrawer.setAttribute('aria-hidden', open ? 'false' : 'true');
  el.menuButton.setAttribute('aria-expanded', open ? 'true' : 'false');
  el.drawerBackdrop.hidden = !open;
  document.body.classList.toggle('drawer-open', open);
}

function redrawChartsSoon() {
  setTimeout(() => {
    if (!state.lastSnapshot?.bars) return;
    updatePriceChart(state.lastSnapshot.bars);
    updatePositionLines(state.lastSnapshot?.state);
    drawScoreChart(state.lastSnapshot.bars);
    drawRsiChart(state.lastSnapshot.bars);
  }, 260);
}

function setTrackerCollapsed(collapsed, persist = false) {
  state.trackerCollapsed = Boolean(collapsed);
  const left = document.querySelector('.col-left');
  left?.classList.toggle('tracker-collapsed', state.trackerCollapsed);
  el.trackerSidebar?.classList.toggle('collapsed', state.trackerCollapsed);
  el.trackerCollapse?.setAttribute('aria-expanded', state.trackerCollapsed ? 'false' : 'true');
  if (persist) saveDashboardPreferences();
  redrawChartsSoon();
}

function loadExecutionPreference() {
  try {
    state.realOrdersArmed = localStorage.getItem(EXECUTION_PREF_KEY) === 'real';
  } catch {
    state.realOrdersArmed = false;
  }
  renderExecutionPreference();
}

function saveExecutionPreference(armed) {
  state.realOrdersArmed = Boolean(armed);
  try {
    localStorage.setItem(EXECUTION_PREF_KEY, state.realOrdersArmed ? 'real' : 'paper');
  } catch {}
  renderExecutionPreference();
  saveDashboardPreferences();
}

function renderExecutionPreference() {
  el.realOrders.checked = state.realOrdersArmed;
  el.executionModeLabel.textContent = state.realOrdersArmed ? 'Real orders armed' : 'Paper only';
  el.executionModeLabel.className = state.realOrdersArmed ? 'execution-live' : '';
}

function readDashboardPreferences() {
  try {
    const raw = JSON.parse(localStorage.getItem(DASHBOARD_PREF_KEY) || '{}');
    return raw && raw.settings ? raw.settings : raw;
  } catch {
    return {};
  }
}

function saveDashboardPreferences() {
  const prefs = {
    savedAt: new Date().toISOString(),
    pair: el.pair.value.trim(),
    market: el.market.value.trim(),
    strategy: normalizeStrategy(el.strategy?.value),
    timeframe: normalizeTimeframe(el.timeframe?.value),
    btTimeframe: el.btTimeframeSelect?.value || DEFAULT_TIMEFRAME,
    mode: el.mode.value,
    risk: el.risk.value,
    lookback: el.lookback.value,
    pair2: el.pair2?.value?.trim() || '',
    market2: el.market2?.value?.trim() || '',
    chain: el.chainSelect?.value || 'CT_501',
    theme: state.theme,
    selectedCoins: [...state.selectedCoins],
    trackingActive: state.trackingActive,
    trackerCollapsed: state.trackerCollapsed,
    executionMode: state.realOrdersArmed ? 'real' : 'paper',
  };
  try {
    localStorage.setItem(DASHBOARD_PREF_KEY, JSON.stringify({ settings: prefs, savedAt: prefs.savedAt }));
  } catch {}
  fetch('/api/settings?key=dashboard', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ settings: prefs }),
    keepalive: true,
  }).catch(() => {});
}

function scheduleDashboardPreferencesSave() {
  clearTimeout(state.preferencesSaveTimer);
  state.preferencesSaveTimer = setTimeout(saveDashboardPreferences, 300);
}

async function loadDashboardPreferences() {
  const localPrefs = readDashboardPreferences();
  try {
    const res = await fetch('/api/settings?key=dashboard');
    const data = await res.json();
    if (data.ok && data.settings && Object.keys(data.settings).length) {
      const dbSavedAt = Date.parse(data.settings.savedAt || data.updated_at || '') || 0;
      const localSavedAt = Date.parse(localPrefs.savedAt || '') || 0;
      if (localSavedAt > dbSavedAt) return localPrefs;
      return data.settings;
    }
  } catch {}
  return localPrefs;
}

async function applyRuntimeConfig() {
  try {
    const res = await fetch('/api/health');
    const data = await res.json();
    if (data.ok && data.place_orders === true && !localStorage.getItem(EXECUTION_PREF_KEY)) {
      state.realOrdersArmed = true;
      renderExecutionPreference();
    }
  } catch {}
}

async function applyDashboardPreferences() {
  const prefs = await loadDashboardPreferences();
  const setValue = (node, value) => {
    if (node && value != null && value !== '') node.value = value;
  };

  setValue(el.pair, prefs.pair);
  setValue(el.market, prefs.market);
  setValue(el.strategy, normalizeStrategy(prefs.strategy));
  setValue(el.mode, prefs.mode);
  setValue(el.risk, prefs.risk);
  setValue(el.lookback, prefs.lookback);
  setValue(el.timeframe, normalizeTimeframe(prefs.timeframe));
  if (prefs.btTimeframe && el.btTimeframeSelect) {
    setValue(el.btTimeframeSelect, prefs.btTimeframe);
    _applyLookbackConstraints(el.btTimeframeSelect.value);
  }
  setValue(el.pair2, prefs.pair2);
  setValue(el.market2, prefs.market2);
  setValue(el.chainSelect, prefs.chain);
  applyTheme(prefs.theme || state.theme);

  if (prefs.executionMode === 'real' || prefs.executionMode === 'paper') {
    state.realOrdersArmed = prefs.executionMode === 'real';
    try {
      localStorage.setItem(EXECUTION_PREF_KEY, prefs.executionMode);
    } catch {}
    renderExecutionPreference();
  }

  if (Array.isArray(prefs.selectedCoins) && prefs.selectedCoins.length) {
    state.selectedCoins = new Set(
      prefs.selectedCoins
        .map(c => String(c).trim().toUpperCase())
        .filter(Boolean)
        .slice(0, 12)
    );
  }
  state.trackingActive = Boolean(prefs.trackingActive);
  setTrackerCollapsed(Boolean(prefs.trackerCollapsed));
}

// ─── WebSocket ────────────────────────────────────────────────────────────────
function setWsStatus(status) {
  el.wsDot.className = `ws-dot ${status}`;
  const labels = { live: 'Live', stale: 'Stale', error: 'Disconnected', '': 'Connecting' };
  el.wsLabel.textContent = labels[status] || 'Connecting';
}

function currentWebSocketConfig() {
  const pair   = el.pair.value.trim()   || 'B-ETH_USDT';
  const market = el.market.value.trim() || 'ETHUSDT';
  const mode   = el.mode.value || 'futures';
  const key = `${pair}|${market}|${mode}`;
  const url = `ws://${location.host}/ws/quotes?pair=${encodeURIComponent(pair)}&market=${encodeURIComponent(market)}&mode=${mode}&interval=3`;
  return { key, url };
}

function connectWebSocket(force = false) {
  const { key, url } = currentWebSocketConfig();
  const activeStates = new Set([WebSocket.CONNECTING, WebSocket.OPEN]);
  if (!force && state.ws && state.wsKey === key && activeStates.has(state.ws.readyState)) {
    return;
  }

  clearTimeout(state.wsReconnectTimer);
  state.wsReconnectTimer = null;

  if (state.ws) {
    const oldWs = state.ws;
    oldWs.onclose = null;
    oldWs.onerror = null;
    try { oldWs.close(); } catch {}
  }
  setWsStatus('');

  const ws = new WebSocket(url);
  state.ws = ws;
  state.wsKey = key;

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
    if (state.ws !== ws) return;
    state.ws = null;
    setWsStatus('error');
    clearTimeout(state.wsReconnectTimer);
    state.wsReconnectTimer = setTimeout(() => connectWebSocket(true), 5000);
  };
}

// ─── Price chart (lightweight-charts) ────────────────────────────────────────
function initPriceChart() {
  if (typeof LightweightCharts === 'undefined') {
    console.warn('LightweightCharts not available; skipping chart init.');
    state.priceChart = null;
    state.candleSeries = null;
    state.superSeries = null;
    state.targetLine = null;
    state.stopLine = null;
    return;
  }
  if (state.priceChart) {
    state.priceChart.remove();
    state.priceChart = null;
    state.candleSeries = null;
    state.superSeries = null;
    state.targetLine = null;
    state.stopLine = null;
    state.fvgHighLine = null;
    state.fvgLowLine = null;
    state.sweepMarkers = null;
  }
  const container = el.priceChart;
  const chart = LightweightCharts.createChart(container, {
    layout: {
      background: { color: cssVar('--panel') || '#181c22' },
      textColor: cssVar('--text-dim') || '#5a6270',
    },
    grid: {
      vertLines: { color: cssVar('--border-dim') || '#1e2430' },
      horzLines: { color: cssVar('--border-dim') || '#1e2430' },
    },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: {
      borderColor: cssVar('--border') || '#252b34',
      priceFormat: { minMove: 0.00000001, precision: 8 },
    },
    timeScale: { borderColor: cssVar('--border') || '#252b34', timeVisible: true, secondsVisible: false },
    width: container.clientWidth,
    height: container.clientHeight,
  });

  const hasNewSeriesApi = typeof chart.addSeries === 'function';
  const candleOptions = {
    upColor: cssVar('--long') || '#26c485', downColor: cssVar('--short') || '#e05252',
    borderUpColor: cssVar('--long') || '#26c485', borderDownColor: cssVar('--short') || '#e05252',
    wickUpColor: cssVar('--long') || '#26c485', wickDownColor: cssVar('--short') || '#e05252',
  };
  const lineOptions = {
    color: cssVar('--neutral') || '#5b9cf6', lineWidth: 1.5, priceLineVisible: false, lastValueVisible: false,
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

function clearPositionLines() {
  if (!state.candleSeries) return;
  if (state.targetLine) {
    try { state.candleSeries.removePriceLine(state.targetLine); } catch {}
    state.targetLine = null;
  }
  if (state.stopLine) {
    try { state.candleSeries.removePriceLine(state.stopLine); } catch {}
    state.stopLine = null;
  }
}

function updatePositionLines(stateData) {
  if (!state.candleSeries) return;
  const side = stateData?.side;
  let stopPx = stateData?.stop_px;
  let targetPx = stateData?.target_px;

  // Prefer live exchange position TP/SL over paper state values
  const currentCoin = (state.lastSnapshot?.coin || '').toUpperCase();
  if (currentCoin && state.livePositions.length > 0) {
    const livePos = state.livePositions.find(p =>
      p.coin && p.coin.toUpperCase() === currentCoin && p.side === side
    );
    if (livePos) {
      if (livePos.take_profit_trigger != null && Number.isFinite(parseFloat(livePos.take_profit_trigger))) {
        targetPx = livePos.take_profit_trigger;
      }
      if (livePos.stop_loss_trigger != null && Number.isFinite(parseFloat(livePos.stop_loss_trigger))) {
        stopPx = livePos.stop_loss_trigger;
      }
    }
  }

  if (side !== 'LONG' && side !== 'SHORT') {
    clearPositionLines();
    return;
  }
  if (!Number.isFinite(parseFloat(stopPx)) || !Number.isFinite(parseFloat(targetPx))) {
    clearPositionLines();
    return;
  }

  const stopColor = side === 'LONG' ? (cssVar('--short') || '#e05252') : (cssVar('--long') || '#26c485');
  const targetColor = side === 'LONG' ? (cssVar('--long') || '#26c485') : (cssVar('--short') || '#e05252');
  const lineStyle = LightweightCharts?.LineStyle?.Dashed ?? 2;

  if (!state.stopLine) {
    try {
      state.stopLine = state.candleSeries.createPriceLine({
        price: parseFloat(stopPx),
        color: stopColor,
        lineWidth: 2,
        lineStyle,
        axisLabelVisible: true,
        title: 'Stop ' + stopPx,
      });
    } catch {}
  } else {
    try {
      state.stopLine.applyOptions({ price: parseFloat(stopPx), color: stopColor, title: 'Stop ' + stopPx });
    } catch {}
  }

  if (!state.targetLine) {
    try {
      state.targetLine = state.candleSeries.createPriceLine({
        price: parseFloat(targetPx),
        color: targetColor,
        lineWidth: 2,
        lineStyle,
        axisLabelVisible: true,
        title: 'Target ' + targetPx,
      });
    } catch {}
  } else {
    try {
      state.targetLine.applyOptions({ price: parseFloat(targetPx), color: targetColor, title: 'Target ' + targetPx });
    } catch {}
  }
}

// ─── Daily Sweep overlays ─────────────────────────────────────────────────────
function _setSeriesMarkers(series, markers) {
  // LWC v5: createSeriesMarkers primitive; v4 fallback: series.setMarkers
  if (typeof LightweightCharts?.createSeriesMarkers === 'function') {
    if (state.sweepMarkers) {
      try { state.sweepMarkers.setData(markers); } catch {}
    } else {
      state.sweepMarkers = LightweightCharts.createSeriesMarkers(series, markers);
    }
  } else {
    try { series.setMarkers(markers); } catch {}
  }
}

function clearSweepOverlays() {
  if (!state.candleSeries) return;
  if (state.sweepMarkers) {
    try { state.sweepMarkers.setData([]); } catch {}
    try { state.candleSeries.detachPrimitive?.(state.sweepMarkers); } catch {}
    state.sweepMarkers = null;
  } else {
    try { state.candleSeries.setMarkers?.([]); } catch {}
  }
  if (state.fvgHighLine) {
    try { state.candleSeries.removePriceLine(state.fvgHighLine); } catch {}
    state.fvgHighLine = null;
  }
  if (state.fvgLowLine) {
    try { state.candleSeries.removePriceLine(state.fvgLowLine); } catch {}
    state.fvgLowLine = null;
  }
}

function updateSweepOverlays(bars, strategyId, indicators) {
  clearSweepOverlays();
  if (strategyId !== 'daily_sweep' || !state.candleSeries) return;

  const COLORS = {
    bull:   cssVar('--bull')   || '#00c896',
    bear:   cssVar('--bear')   || '#f03858',
    amber:  cssVar('--amber')  || '#f5a020',
    blue:   cssVar('--blue')   || '#3a7ef4',
    violet: cssVar('--violet') || '#7c5cf6',
  };
  const dashed = LightweightCharts?.LineStyle?.Dashed ?? 2;

  const markers = [];
  for (const b of bars) {
    const t = Math.floor(new Date(b.time).getTime() / 1000);
    // BOS — first bar of new 1H bias direction
    if (b.bos != null && b.bos !== 0) {
      markers.push({ time: t, position: b.bos > 0 ? 'belowBar' : 'aboveBar',
        color: COLORS.violet, shape: 'circle', text: 'BOS' });
    }
    // Sweep (manipulation entry)
    if (b.sweep) {
      markers.push({ time: t, position: b.bias > 0 ? 'belowBar' : 'aboveBar',
        color: COLORS.amber, shape: b.bias > 0 ? 'arrowUp' : 'arrowDown', text: 'SWEEP' });
    }
    // CHoCH
    if (b.choch) {
      markers.push({ time: t, position: b.bias > 0 ? 'belowBar' : 'aboveBar',
        color: COLORS.blue, shape: 'square', text: 'CHoCH' });
    }
    // FVG entry
    if (b.entry_side && b.entry_side !== 0) {
      markers.push({ time: t, position: b.entry_side > 0 ? 'belowBar' : 'aboveBar',
        color: b.entry_side > 0 ? COLORS.bull : COLORS.bear,
        shape: b.entry_side > 0 ? 'arrowUp' : 'arrowDown', text: 'FVG' });
    }
  }
  markers.sort((a, b) => a.time - b.time);
  _setSeriesMarkers(state.candleSeries, markers);

  // FVG band lines (current active FVG from indicators)
  const fvgLow  = indicators?.fvg_low;
  const fvgHigh = indicators?.fvg_high;
  if (fvgLow != null && fvgHigh != null && Number.isFinite(+fvgLow) && Number.isFinite(+fvgHigh)) {
    try {
      state.fvgHighLine = state.candleSeries.createPriceLine({
        price: +fvgHigh, color: COLORS.violet, lineWidth: 1,
        lineStyle: dashed, axisLabelVisible: true, title: 'FVG Hi',
      });
      state.fvgLowLine = state.candleSeries.createPriceLine({
        price: +fvgLow, color: COLORS.violet, lineWidth: 1,
        lineStyle: dashed, axisLabelVisible: true, title: 'FVG Lo',
      });
    } catch {}
  }
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
  const dpr = window.devicePixelRatio || 1;
  const cssWidth = Math.max(1, canvas.clientWidth || canvas.offsetWidth || 300);
  const cssHeight = Math.max(1, canvas.clientHeight || canvas.offsetHeight || Number(canvas.getAttribute('height')) || 140);
  canvas.width = Math.round(cssWidth * dpr);
  canvas.height = Math.round(cssHeight * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssWidth, cssHeight);

  const scores = (bars || [])
    .map(b => b.score ?? b.raw_score)
    .map(Number)
    .filter(Number.isFinite);
  if (scores.length === 0) return;

  const W = cssWidth, H = cssHeight;
  const SCORE_MIN = -5, SCORE_MAX = 5;
  const maxVisibleBars = Math.max(1, Math.floor(W / 3));
  const visibleScores = scores.slice(-maxVisibleBars);
  const stepW = W / visibleScores.length;
  const barW = Math.max(1, Math.floor(stepW * 0.72));
  const zeroY = H / 2;
  const scale = H / (SCORE_MAX - SCORE_MIN) / 2;
  const ENTRY_THRESH = 3;

  // Threshold lines
  ctx.strokeStyle = cssVar('--border') || '#252b34';
  ctx.setLineDash([3, 3]);
  ctx.lineWidth = 1;
  [ENTRY_THRESH, -ENTRY_THRESH].forEach(s => {
    const y = zeroY - s * scale;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
  });
  ctx.setLineDash([]);

  // Zero line
  ctx.strokeStyle = cssVar('--border-dim') || '#1e2430';
  ctx.beginPath(); ctx.moveTo(0, zeroY); ctx.lineTo(W, zeroY); ctx.stroke();

  // Bars
  visibleScores.forEach((s, i) => {
    const x = Math.floor(i * stepW);
    const barH = s * scale;
    const y = barH >= 0 ? zeroY - barH : zeroY;
    const height = Math.max(1, Math.abs(barH));
    ctx.fillStyle = s >= ENTRY_THRESH
      ? cssVar('--long') || '#26c485'
      : s <= -ENTRY_THRESH
        ? cssVar('--short') || '#e05252'
        : cssVar('--text-dim') || '#3a4250';
    ctx.fillRect(x, Math.min(y, zeroY), barW, height);
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
  zone(70, cssVar('--short') || '#e05252');
  zone(50, cssVar('--border') || '#252b34');
  zone(30, cssVar('--long') || '#26c485');

  const step = (W - 2 * pad) / (rsiVals.length - 1);
  ctx.strokeStyle = cssVar('--neutral') || '#5b9cf6'; ctx.lineWidth = 1.5;
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
  const strategy = normalizeStrategy(el.strategy.value);
  const mode     = el.mode.value || 'futures';
  const risk     = parseFloat(el.risk.value) || 10;
  const lookback = parseInt(el.lookback.value) || 3;
  const timeframe = normalizeTimeframe(el.timeframe?.value);
  const pair2    = el.pair2?.value?.trim() || '';
  const market2  = el.market2?.value?.trim() || '';

  let data;
  try {
    let url = `/api/snapshot?pair=${encodeURIComponent(pair)}&market=${encodeURIComponent(market)}` +
      `&strategy=${strategy}&mode=${mode}&risk=${risk}&lookback_days=${lookback}&timeframe=${encodeURIComponent(timeframe)}`;
    if (pair2) url += `&pair2=${encodeURIComponent(pair2)}`;
    if (market2) url += `&market2=${encodeURIComponent(market2)}`;
    url += `&exec_mode=${state.realOrdersArmed ? 'real' : 'paper'}`;
    const res = await fetch(url);
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
  const tfLabel = data.timeframe || normalizeTimeframe(el.timeframe?.value);
  el.priceChartSub.textContent = `${data.strategy?.name || ''} · ${data.mode?.toUpperCase()} · ${tfLabel}`;

  const stats = data.stats || {};
  const action = data.action || {};
  const stateData = data.state || {};
  const score = stats.score ?? null;
  const rsi   = stats.rsi   ?? null;
  if (data.timeframe && el.timeframe && el.timeframe.value !== data.timeframe) {
    el.timeframe.value = data.timeframe;
  }

  // Metrics
  el.lastPrice.textContent = fmtPrice(stats.close);
  el.changePct.textContent = stats.change_pct != null ? fmtPct(stats.change_pct) : '—';
  el.changePct.className = 'metric-sub ' + (stats.change_pct >= 0 ? 'score-pos' : 'score-neg');

  el.signalText.textContent = action.label || '—';
  el.signalText.className = 'metric-value ' + (action.side === 'LONG' ? 'long' : action.side === 'SHORT' ? 'short' : '');
  el.signalMeta.textContent = `${action.type || '—'} · ${action.side || '—'}`;

  const scoreLabelEl = document.getElementById('scoreLabelText');
  if (data.strategy?.id === 'daily_sweep') {
    const indNow = data.indicators || {};
    const biasLbl = indNow.bias_label || 'NEUTRAL';
    const phaseLbl = { neutral: 'Neutral', accumulation: 'Accumulation', manipulation: 'Manipulation', distribution: 'Distribution' }[indNow.phase] || indNow.phase || '—';
    if (scoreLabelEl) scoreLabelEl.textContent = 'Bias / Phase';
    el.scoreText.textContent = biasLbl;
    el.scoreText.className = 'metric-value ' + (biasLbl === 'LONG' ? 'long' : biasLbl === 'SHORT' ? 'short' : '');
    el.rsiText.textContent = phaseLbl;
  } else {
    if (scoreLabelEl) scoreLabelEl.textContent = 'Score / RSI';
    el.scoreText.textContent = score != null ? `${score > 0 ? '+' : ''}${score} / ${rsi?.toFixed(1) || '—'}` : '—';
    el.scoreText.className = 'metric-value ' + (score >= 3 ? 'long' : score <= -3 ? 'short' : '');
    el.rsiText.textContent = rsi != null ? `RSI ${rsi.toFixed(1)}` : '—';
  }

  const side = stateData.side;
  el.positionText.textContent = side || 'FLAT';
  el.positionText.className = 'metric-value ' + (side === 'LONG' ? 'long' : side === 'SHORT' ? 'short' : '');
  el.positionMeta.textContent = stateData.realized_pnl != null
    ? `Trade #${stateData.trade_id || 0} · PnL $${parseFloat(stateData.realized_pnl).toFixed(2)}`
    : '—';

  // Charts
  const bars = data.bars || [];
  updatePriceChart(bars);
  updatePositionLines(stateData);
  updateSweepOverlays(bars, data.strategy?.id, data.indicators);
  drawScoreChart(bars);
  drawRsiChart(bars);

  // Strategy details
  const strat = data.strategy || {};
  const ind   = data.indicators || {};
  const isSweep = data.strategy?.id === 'daily_sweep';

  const stratRows = [
    ['Name', strat.name],
    ['Description', strat.description],
    ['Reason', strat.reason],
    ...(strat.notes || []).map((n, i) => [`Note ${i + 1}`, n]),
  ];
  if (isSweep) {
    const phaseLabel = { neutral: 'Neutral', accumulation: 'Accumulation', manipulation: 'Manipulation', distribution: 'Distribution' };
    stratRows.push(['1H Bias', ind.bias_label || '—']);
    stratRows.push(['Phase', phaseLabel[ind.phase] || ind.phase || '—']);
    if (ind.fvg_low != null && ind.fvg_high != null)
      stratRows.push(['FVG Band', `${fmtPrice(ind.fvg_low)} – ${fmtPrice(ind.fvg_high)}`]);
    if (ind.prev_day_high != null) stratRows.push(['Prev Day High', fmtPrice(ind.prev_day_high)]);
    if (ind.prev_day_low  != null) stratRows.push(['Prev Day Low',  fmtPrice(ind.prev_day_low)]);
  }
  el.strategyList.innerHTML = stratRows
    .map(([k, v]) => v ? `<dt>${k}</dt><dd>${v}</dd>` : '').join('');

  // Trade state
  const stateItems = [
    ['Side', stateData.side], ['Trade #', stateData.trade_id],
    ['Entry', stateData.entry_px ? fmtPrice(stateData.entry_px) : null],
    ['Stop', stateData.stop_px ? fmtPrice(stateData.stop_px) : null],
    ['Target', stateData.target_px ? fmtPrice(stateData.target_px) : null],
    ['Qty', stateData.qty ? parseFloat(stateData.qty).toFixed(6) : null],
    ['PnL', stateData.realized_pnl != null ? `$${parseFloat(stateData.realized_pnl).toFixed(2)}` : null],
    ['Execution', state.realOrdersArmed ? 'Real orders armed' : 'Paper only'],
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
      el.positionsCount.textContent = '0';
      el.positionsMeta.textContent = 'No account data';
      el.accountPnl.textContent = '—';
      el.accountPnl.className = 'account-value';
      el.accountMode.textContent = `${mode.toUpperCase()} account`;
      el.currentRisk.textContent = `$${currentRiskAmount().toFixed(2)}`;
      el.riskButtons.innerHTML = '';
      el.positionsBody.innerHTML = '<tr><td colspan="8" class="empty-row">Connect CoinDCX API keys to show active positions.</td></tr>';
      return;
    }

    const avail = parseFloat(data.available_quote_balance) || 0;
    el.walletBalance.textContent = `${avail.toFixed(2)} ${data.currency || 'USDT'}`;
    el.walletStatus.textContent = `Available ${data.currency || 'USDT'} balance`;
    el.currentRisk.textContent = `$${currentRiskAmount().toFixed(2)}`;
    el.accountMode.textContent = `${(data.mode || mode).toUpperCase()} account`;

    // Risk buttons
    el.riskButtons.innerHTML = (data.risk_suggestions || []).map(s =>
      `<button class="risk-btn" data-amount="${s.amount.toFixed(4)}">${s.label} ($${s.amount.toFixed(2)})</button>`
    ).join('');
    el.riskButtons.querySelectorAll('.risk-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        el.risk.value = btn.dataset.amount;
        el.currentRisk.textContent = `$${parseFloat(btn.dataset.amount).toFixed(2)}`;
        saveDashboardPreferences();
      });
    });

    // Positions
    const positions = data.positions || [];
    state.livePositions = positions;
    const totalPnl = positions.reduce((sum, p) => sum + (parseFloat(p.unrealized_pnl) || 0), 0);
    const pnlCls = totalPnl >= 0 ? 'score-pos' : 'score-neg';
    el.positionsCount.textContent = String(positions.length);
    el.positionsMeta.textContent = positions.length ? `${positions.length} open` : 'No open positions';
    el.accountPnl.textContent = positions.length ? `${totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(2)} ${data.currency || 'USDT'}` : '0.00';
    el.accountPnl.className = `account-value ${pnlCls}`;
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

    // Re-draw chart position lines with live exchange data
    if (state.lastSnapshot?.state) {
      updatePositionLines(state.lastSnapshot.state);
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

// ─── Volatility scanner ───────────────────────────────────────────────────────
async function loadVolatilityScan() {
  if (!el.volScanBody || !el.volScanButton) return;
  el.volScanButton.disabled = true;
  el.volScanButton.textContent = 'Scanning…';
  el.volScanBody.innerHTML = '<tr><td colspan="8" class="empty-row">Scanning all futures coins…</td></tr>';
  if (el.volScanMeta) el.volScanMeta.textContent = '';

  try {
    const res = await fetch('/api/volatility-scan');
    const data = await res.json();
    const coins = data.coins || [];

    if (el.volScanMeta) {
      el.volScanMeta.textContent = `· ${data.count || 0}/${data.total_scanned || 0} coins · ${data.elapsed_seconds || 0}s`;
    }

    if (coins.length === 0) {
      el.volScanBody.innerHTML = '<tr><td colspan="8" class="empty-row">No coins found under $10.</td></tr>';
      return;
    }

    // Find max ATR% for heat-map coloring
    const maxAtr = Math.max(...coins.map(c => c.atr_pct || 0), 0.01);

    el.volScanBody.innerHTML = coins.map(c => {
      const chgCls = (c.change_pct || 0) >= 0 ? 'score-pos' : 'score-neg';
      const heat = Math.min((c.atr_pct || 0) / maxAtr, 1);
      const heatAlpha = (heat * 0.18).toFixed(3);
      const heatBg = heat > 0.6 ? `rgba(38,196,133,${heatAlpha})` : heat > 0.3 ? `rgba(91,156,246,${heatAlpha})` : 'transparent';
      return `<tr class="vol-row" data-coin="${c.coin}" data-pair="${c.pair}" data-market="${c.market}" style="background:${heatBg};cursor:pointer" title="Click to load ${c.coin}">
        <td>${c.rank}</td>
        <td><strong>${c.coin}</strong></td>
        <td class="mono">${fmtPrice(c.price)}</td>
        <td class="mono vol-atr">${c.atr_pct?.toFixed(2) ?? '—'}%</td>
        <td class="mono">${c.range_pct?.toFixed(1) ?? '—'}%</td>
        <td class="mono">${c.stddev_pct?.toFixed(3) ?? '—'}%</td>
        <td class="mono">${fmtNum(c.volume_usd)}</td>
        <td class="${chgCls}">${c.change_pct >= 0 ? '+' : ''}${c.change_pct?.toFixed(2) ?? '—'}%</td>
      </tr>`;
    }).join('');

    // Click-to-load: clicking a row switches the main chart to that coin
    el.volScanBody.querySelectorAll('.vol-row').forEach(row => {
      row.addEventListener('click', () => {
        const pair = row.dataset.pair;
        const market = row.dataset.market;
        if (pair && market) {
          el.pair.value = pair;
          el.market.value = market;
          saveDashboardPreferences();
          updateTrackedCoinsMenu();
          loadSnapshot();
          loadAccount();
        }
      });
    });
  } catch (err) {
    console.error('Volatility scan failed', err);
    el.volScanBody.innerHTML = '<tr><td colspan="8" class="empty-row">Scan failed. Check console.</td></tr>';
  } finally {
    el.volScanButton.disabled = false;
    el.volScanButton.textContent = 'Scan';
  }
}

// ─── Expert Picks ──────────────────────────────────────────────────────────────
async function loadExpertPicks(forceRefresh = false) {
  try {
    const url = forceRefresh
      ? '/api/expert-picks/recommendations?force=true'
      : '/api/expert-picks/recommendations';
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    // Render sentiment
    if (data.sentiment) {
      const val = data.sentiment.value;
      const cls = data.sentiment.classification || 'Unknown';
      if (el.sentimentValue) {
        el.sentimentValue.textContent = val !== null ? val : '—';
        el.sentimentValue.className = 'sentiment-value ' + (val < 35 ? 'fear' : val > 65 ? 'greed' : 'neutral');
      }
      if (el.sentimentClass) el.sentimentClass.textContent = cls;
      if (el.sentimentTime && data.sentiment.timestamp) {
        const d = new Date(data.sentiment.timestamp * 1000);
        el.sentimentTime.textContent = `Updated ${d.toLocaleTimeString()}`;
      }
    }

    // Render positions recommendations
    if (el.expertPosBody) {
      const posRecs = data.positions_recommendations || [];
      if (posRecs.length === 0) {
        el.expertPosBody.innerHTML = '<tr><td colspan="4" class="empty-row">No active positions</td></tr>';
      } else {
        el.expertPosBody.innerHTML = posRecs.map(p => {
          const rec = p.recommendation || '';
          const recClass = rec.startsWith('CLOSE') ? 'rec-close' : rec === 'HOLD' ? 'rec-hold' : 'rec-watch';
          return `<tr>
            <td><strong>${p.coin}</strong></td>
            <td><span class="pos-${p.position?.toLowerCase()}">${p.position}</span></td>
            <td><span class="sig-${p.signal?.toLowerCase()}">${p.signal}</span></td>
            <td><span class="${recClass}">${rec.replace('_', ' ')}</span></td>
          </tr>`;
        }).join('');
      }
    }

    // Render top signals
    if (el.expertSignalsTable) {
      const topSig = data.top_signals || [];
      if (topSig.length === 0) {
        el.expertSignalsTable.innerHTML = '<tr><td colspan="4" class="empty-row">No signals available</td></tr>';
      } else {
        el.expertSignalsTable.innerHTML = topSig.map((s, i) => {
          return `<tr>
            <td>${i + 1}</td>
            <td><strong>${s.coin}</strong></td>
            <td><span class="sig-${s.action?.toLowerCase()}">${s.action}</span></td>
            <td>${s.confidence || '—'}%</td>
          </tr>`;
        }).join('');
      }
    }
  } catch (err) {
    console.error('Expert Picks failed', err);
  }
}

function toggleExpertSidebar(open) {
  const isOpen = open ?? el.expertSidebar?.hidden;
  if (!el.expertSidebar) return;

  // Close other sidebars if opening expert sidebar
  if (isOpen && el.volSidebar && !el.volSidebar.hidden) {
    el.volSidebar.hidden = true;
    el.actVolScanner?.setAttribute('aria-pressed', 'false');
  }
  if (isOpen && el.reportsSidebar && !el.reportsSidebar.hidden) {
    el.reportsSidebar.hidden = true;
    el.actReports?.setAttribute('aria-pressed', 'false');
  }
  if (isOpen && el.backtestsSidebar && !el.backtestsSidebar.hidden) {
    el.backtestsSidebar.hidden = true;
    el.actBacktests?.setAttribute('aria-pressed', 'false');
  }

  el.expertSidebar.hidden = !isOpen;

  if (isOpen) {
    loadExpertPicks();
    if (!state.expertPicksInterval) {
      state.expertPicksInterval = setInterval(() => loadExpertPicks(), 5 * 60 * 1000);
    }
    el.actExpertPicks?.setAttribute('aria-pressed', 'true');
    el.actDashboard?.setAttribute('aria-pressed', 'false');
  } else {
    if (state.expertPicksInterval) {
      clearInterval(state.expertPicksInterval);
      state.expertPicksInterval = null;
    }
    el.actExpertPicks?.setAttribute('aria-pressed', 'false');
  }
}

// ─── Activity bar / Volatility sidebar ────────────────────────────────────────
function toggleVolSidebar(open) {
  const isOpen = open ?? el.volSidebar?.hidden;
  if (!el.volSidebar) return;

  // Close other sidebars if opening vol sidebar
  if (isOpen && el.expertSidebar && !el.expertSidebar.hidden) {
    el.expertSidebar.hidden = true;
    el.actExpertPicks?.setAttribute('aria-pressed', 'false');
    if (state.expertPicksInterval) {
      clearInterval(state.expertPicksInterval);
      state.expertPicksInterval = null;
    }
  }
  if (isOpen && el.reportsSidebar && !el.reportsSidebar.hidden) {
    el.reportsSidebar.hidden = true;
    el.actReports?.setAttribute('aria-pressed', 'false');
  }
  if (isOpen && el.backtestsSidebar && !el.backtestsSidebar.hidden) {
    el.backtestsSidebar.hidden = true;
    el.actBacktests?.setAttribute('aria-pressed', 'false');
  }

  el.volSidebar.hidden = !isOpen;
  el.actVolScanner?.setAttribute('aria-pressed', isOpen ? 'true' : 'false');
  el.actDashboard?.setAttribute('aria-pressed', isOpen ? 'false' : 'true');
}

// ─── Reports sidebar ──────────────────────────────────────────────────────────
function toggleReportsSidebar(open) {
  const isOpen = open ?? el.reportsSidebar?.hidden;
  if (!el.reportsSidebar) return;

  // Close other sidebars
  if (isOpen && el.volSidebar && !el.volSidebar.hidden) {
    el.volSidebar.hidden = true;
    el.actVolScanner?.setAttribute('aria-pressed', 'false');
    el.actDashboard?.setAttribute('aria-pressed', 'true');
  }
  if (isOpen && el.expertSidebar && !el.expertSidebar.hidden) {
    el.expertSidebar.hidden = true;
    el.actExpertPicks?.setAttribute('aria-pressed', 'false');
    if (state.expertPicksInterval) {
      clearInterval(state.expertPicksInterval);
      state.expertPicksInterval = null;
    }
  }
  if (isOpen && el.backtestsSidebar && !el.backtestsSidebar.hidden) {
    el.backtestsSidebar.hidden = true;
    el.actBacktests?.setAttribute('aria-pressed', 'false');
  }

  el.reportsSidebar.hidden = !isOpen;
  el.actReports?.setAttribute('aria-pressed', isOpen ? 'true' : 'false');
  el.actDashboard?.setAttribute('aria-pressed', isOpen ? 'false' : 'true');
}

// ─── Backtests sidebar ───────────────────────────────────────────────────────
function toggleBacktestsSidebar(open) {
  const isOpen = open ?? el.backtestsSidebar?.hidden;
  if (!el.backtestsSidebar) return;

  if (isOpen && el.volSidebar && !el.volSidebar.hidden) {
    el.volSidebar.hidden = true;
    el.actVolScanner?.setAttribute('aria-pressed', 'false');
  }
  if (isOpen && el.expertSidebar && !el.expertSidebar.hidden) {
    el.expertSidebar.hidden = true;
    el.actExpertPicks?.setAttribute('aria-pressed', 'false');
    if (state.expertPicksInterval) {
      clearInterval(state.expertPicksInterval);
      state.expertPicksInterval = null;
    }
  }
  if (isOpen && el.reportsSidebar && !el.reportsSidebar.hidden) {
    el.reportsSidebar.hidden = true;
    el.actReports?.setAttribute('aria-pressed', 'false');
  }

  el.backtestsSidebar.hidden = !isOpen;
  el.actBacktests?.setAttribute('aria-pressed', isOpen ? 'true' : 'false');
  el.actDashboard?.setAttribute('aria-pressed', isOpen ? 'false' : 'true');
  if (isOpen) {
    if (!el.customStrategyCode?.value) resetCustomStrategyForm();
    loadBacktestStrategies();
  }
}

function showToast(msg, type = 'ok') {
  const t = el.toastMsg;
  if (!t) return;
  t.textContent = msg;
  t.className = `toast-msg toast-${type} show`;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.classList.remove('show'); }, type === 'ok' ? 6000 : 4000);
}

async function loadReportsOverview() {
  const exec = state.reportsFilter === 'all' ? '' : state.reportsFilter;
  try {
    const res = await fetch(`/api/reports/overview?exec_mode=${exec}`);
    const d = await res.json();
    if (!d.ok) return;
    if (el.rStatTotal) {
      const total = d.total_trades ?? 0;
      const closed = d.closed_trades ?? total;
      const open = d.open_trades ?? 0;
      el.rStatTotal.textContent = open > 0 ? `${closed}/${total}` : total;
      el.rStatTotal.title = open > 0 ? `${closed} closed, ${open} open` : `${total} closed trade(s)`;
    }
    if (el.rStatWinRate) el.rStatWinRate.textContent = d.win_rate != null ? d.win_rate.toFixed(1) + '%' : '—';
    if (el.rStatNetPnl) {
      const v = d.net_pnl;
      el.rStatNetPnl.textContent = v != null ? (v >= 0 ? '+$' : '-$') + Math.abs(v).toFixed(2) : '—';
      el.rStatNetPnl.className = 'report-stat-value ' + (v > 0 ? 'pnl-pos' : v < 0 ? 'pnl-neg' : '');
    }
    if (el.rStatPF) el.rStatPF.textContent = d.profit_factor != null ? d.profit_factor.toFixed(2) : '—';
    if (el.rStatDD) el.rStatDD.textContent = d.max_drawdown != null ? '$' + d.max_drawdown.toFixed(2) : '—';
    if (el.rStatDur) el.rStatDur.textContent = d.avg_duration_minutes != null ? Math.round(d.avg_duration_minutes) + ' min' : '—';
  } catch (err) {
    console.error('Reports overview failed', err);
  }
}

async function loadReportsTrades() {
  const exec = state.reportsFilter === 'all' ? '' : state.reportsFilter;
  try {
    const res = await fetch(`/api/reports/trades?exec_mode=${exec}&limit=500`);
    const d = await res.json();
    if (!d.ok || !el.reportsTableBody) return;
    const trades = d.trades || [];
    if (trades.length === 0) {
      el.reportsTableBody.innerHTML = '<tr><td colspan="16" class="empty-row">No trades recorded.</td></tr>';
      return;
    }
    el.reportsTableBody.innerHTML = trades.map((t, i) => {
      const side = t.side === 1 ? '<span class="pos-long">LONG</span>' : t.side === -1 ? '<span class="pos-short">SHORT</span>' : '—';
      const pnl = t.pnl != null
        ? `<span class="${t.pnl >= 0 ? 'pnl-pos' : 'pnl-neg'}">${t.pnl >= 0 ? '+' : ''}$${parseFloat(t.pnl).toFixed(2)}</span>`
        : '—';
      const marketMode = t.mode
        ? `<span class="mode-badge market-mode">${escapeHtml(t.mode)}</span>`
        : '—';
      const execMode = t.execution_mode
        ? `<span class="mode-badge mode-${escapeHtml(t.execution_mode)}">${escapeHtml(t.execution_mode)}</span>`
        : '—';
      const fmtTs = v => v ? v.slice(0, 16).replace('T', ' ') : '—';
      const strategy = t.strategy ? escapeHtml(t.strategy) : '—';
      return `<tr>
        <td class="mono">${t.id ?? i + 1}</td>
        <td><strong>${escapeHtml(t.pair || '—')}</strong></td>
        <td>${side}</td>
        <td>${marketMode}</td>
        <td>${execMode}</td>
        <td>${strategy}</td>
        <td>${fmtTs(t.entry_ts)}</td>
        <td>${fmtTs(t.exit_ts)}</td>
        <td class="mono">${fmtReportNum(t.entry_px)}</td>
        <td class="mono">${fmtReportNum(t.exit_px)}</td>
        <td class="mono">${fmtReportNum(t.stop_px)}</td>
        <td class="mono">${fmtReportNum(t.target_px)}</td>
        <td class="mono">${fmtReportNum(t.qty)}</td>
        <td class="mono">${t.risk_usd != null ? '$' + fmtReportNum(t.risk_usd, 4) : '—'}</td>
        <td>${pnl}</td>
        <td>${escapeHtml(t.exit_reason || '—')}</td>
      </tr>`;
    }).join('');
  } catch (err) {
    console.error('Reports trades failed', err);
  }
}

async function loadReports() {
  await Promise.all([loadReportsOverview(), loadReportsTrades()]);
}

function applySelectOptions(selectEl, options, currentValue, fallbackValue) {
  if (!selectEl) return;
  const desired = currentValue || fallbackValue;
  const html = options
    .map(opt => `<option value="${opt.value}">${opt.label}</option>`)
    .join('');
  selectEl.innerHTML = html;
  const values = new Set(options.map(opt => opt.value));
  const nextValue = values.has(desired) ? desired : (values.has(fallbackValue) ? fallbackValue : options[0]?.value);
  if (nextValue != null) selectEl.value = nextValue;
}

async function loadStrategies() {
  try {
    const res = await fetch('/api/strategies');
    const data = await res.json();
    if (!data.ok) return;
    const strategies = (data.strategies || [])
      .map(s => ({ value: s.id, label: s.name || s.id }));
    if (!strategies.length) return;
    const pref = readDashboardPreferences();
    applySelectOptions(el.strategy, strategies, pref.strategy, DEFAULT_STRATEGY);
  } catch (err) {
    console.error('Failed to load strategies', err);
  }
}

function setBacktestStatus(message, type = '') {
  if (!el.backtestStatus) return;
  el.backtestStatus.textContent = message || '—';
  el.backtestStatus.className = `bt-status ${type ? `bt-status-${type}` : ''}`;
}

function setCustomStrategyStatus(message, type = '') {
  if (!el.customStrategyStatus) return;
  el.customStrategyStatus.textContent = message || '—';
  el.customStrategyStatus.className = `bt-status ${type ? `bt-status-${type}` : ''}`;
}

function slugifyStrategyTitle(title) {
  return String(title || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, 64);
}

function resetCustomStrategyForm() {
  state.selectedCustomStrategyId = null;
  if (el.customStrategySelect) el.customStrategySelect.value = '';
  if (el.customStrategyTitle) el.customStrategyTitle.value = '';
  if (el.customStrategySlug) el.customStrategySlug.value = '';
  if (el.customStrategyDescription) el.customStrategyDescription.value = '';
  if (el.customStrategyCode) el.customStrategyCode.value = DEFAULT_CUSTOM_STRATEGY_CODE;
  if (el.customStrategyEnabled) el.customStrategyEnabled.checked = true;
  setCustomStrategyStatus('New strategy', '');
}

function populateCustomStrategySelect() {
  if (!el.customStrategySelect) return;
  const options = ['<option value="">New custom strategy</option>'].concat(
    state.customStrategies.map(s => `<option value="${s.id}">${escapeHtml(s.title || s.slug)} · v${s.version || 1}</option>`)
  );
  el.customStrategySelect.innerHTML = options.join('');
  if (state.selectedCustomStrategyId) el.customStrategySelect.value = String(state.selectedCustomStrategyId);
}

function populateBacktestStrategySelect() {
  if (!el.backtestStrategySelect) return;
  const builtins = state.builtinBacktestStrategies
    .map(s => `<option value="builtin:${s.id}">${escapeHtml(s.name || s.id)}</option>`);
  const custom = state.customStrategies
    .filter(s => s.enabled)
    .map(s => `<option value="custom:${s.id}">${escapeHtml(s.title || s.slug)} · v${s.version || 1}</option>`);
  el.backtestStrategySelect.innerHTML = [
    '<optgroup label="Built in">',
    ...builtins,
    '</optgroup>',
    '<optgroup label="Custom">',
    ...custom,
    '</optgroup>',
  ].join('');
  if (!el.backtestStrategySelect.value && state.builtinBacktestStrategies.length) {
    el.backtestStrategySelect.value = `builtin:${DEFAULT_STRATEGY}`;
  }
}

function renderCustomStrategyForm(strategy) {
  if (!strategy) {
    resetCustomStrategyForm();
    return;
  }
  state.selectedCustomStrategyId = strategy.id;
  if (el.customStrategySelect) el.customStrategySelect.value = String(strategy.id);
  if (el.customStrategyTitle) el.customStrategyTitle.value = strategy.title || '';
  if (el.customStrategySlug) el.customStrategySlug.value = strategy.slug || '';
  if (el.customStrategyDescription) el.customStrategyDescription.value = strategy.description || '';
  if (el.customStrategyCode) el.customStrategyCode.value = strategy.code || '';
  if (el.customStrategyEnabled) el.customStrategyEnabled.checked = Boolean(strategy.enabled);
  const status = strategy.validation_status || 'not validated';
  setCustomStrategyStatus(`${status}${strategy.validation_message ? ': ' + strategy.validation_message : ''}`, status === 'valid' ? 'ok' : status === 'invalid' ? 'err' : '');
}

async function loadBacktestStrategies() {
  if (!el.backtestsSidebar) return;
  try {
    const res = await fetch('/api/backtests/strategies');
    const data = await res.json();
    if (!data.ok) return;
    state.builtinBacktestStrategies = data.builtins || [];
    state.customStrategies = data.custom || [];
    populateCustomStrategySelect();
    populateBacktestStrategySelect();
    if (!state.selectedCustomStrategyId && state.customStrategies.length) {
      const first = state.customStrategies[0];
      await loadCustomStrategy(first.id);
    } else if (!state.customStrategies.length && !el.customStrategyCode?.value) {
      resetCustomStrategyForm();
    }
  } catch (err) {
    console.error('Backtest strategy load failed', err);
  }
}

async function loadCustomStrategy(id) {
  if (!id) {
    resetCustomStrategyForm();
    return;
  }
  try {
    const res = await fetch(`/api/backtests/custom-strategies/${id}`);
    const data = await res.json();
    if (!data.ok) {
      setCustomStrategyStatus(data.message || 'Could not load strategy.', 'err');
      return;
    }
    renderCustomStrategyForm(data.strategy);
  } catch (err) {
    setCustomStrategyStatus('Network error while loading strategy.', 'err');
  }
}

async function saveCustomStrategy() {
  const payload = {
    title: el.customStrategyTitle?.value?.trim() || '',
    slug: el.customStrategySlug?.value?.trim() || '',
    description: el.customStrategyDescription?.value?.trim() || '',
    code: el.customStrategyCode?.value || '',
    enabled: Boolean(el.customStrategyEnabled?.checked),
  };
  if (!payload.title) {
    setCustomStrategyStatus('Title is required.', 'err');
    return;
  }
  if (!payload.slug) payload.slug = slugifyStrategyTitle(payload.title);
  if (el.customStrategySlug) el.customStrategySlug.value = payload.slug;
  const id = state.selectedCustomStrategyId;
  const btn = el.saveCustomStrategyBtn;
  if (btn) { btn.disabled = true; btn.textContent = 'Saving...'; }
  setCustomStrategyStatus('Saving...', '');
  try {
    const res = await fetch(id ? `/api/backtests/custom-strategies/${id}` : '/api/backtests/custom-strategies', {
      method: id ? 'PUT' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!data.ok) {
      setCustomStrategyStatus(data.message || 'Saved with validation errors.', data.strategy ? 'err' : 'err');
      if (!data.strategy) return;
    }
    renderCustomStrategyForm(data.strategy);
    await loadBacktestStrategies();
    setCustomStrategyStatus(data.message || 'Saved.', data.ok ? 'ok' : 'err');
  } catch (err) {
    setCustomStrategyStatus('Network error while saving.', 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Save'; }
  }
}

async function validateCustomStrategyCurrent() {
  if (!state.selectedCustomStrategyId) {
    await saveCustomStrategy();
    return;
  }
  const btn = el.validateCustomStrategyBtn;
  if (btn) { btn.disabled = true; btn.textContent = 'Validating...'; }
  try {
    const res = await fetch(`/api/backtests/custom-strategies/${state.selectedCustomStrategyId}/validate`, { method: 'POST' });
    const data = await res.json();
    setCustomStrategyStatus(data.message || (data.ok ? 'Valid.' : 'Invalid.'), data.ok ? 'ok' : 'err');
    await loadBacktestStrategies();
  } catch (err) {
    setCustomStrategyStatus('Network error while validating.', 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Validate'; }
  }
}

function backtestRequestPayload() {
  const selected = el.backtestStrategySelect?.value || `builtin:${normalizeStrategy(el.strategy?.value)}`;
  const [kind, rawId] = selected.split(':');
  return {
    pair: el.pair.value.trim() || 'B-ETH_USDT',
    market: el.market.value.trim() || 'ETHUSDT',
    mode: el.mode.value || 'futures',
    strategy: kind === 'builtin' ? rawId : normalizeStrategy(el.strategy.value),
    custom_strategy_id: kind === 'custom' ? parseInt(rawId, 10) : null,
    timeframe: normalizeTimeframe(el.btTimeframeSelect?.value || el.timeframe?.value),
    lookback_days: parseInt(el.btLookback?.value || el.lookback?.value || '14', 10),
    warmup_bars: parseInt(el.btWarmupBars?.value || '50', 10),
    initial_capital: parseFloat(el.btInitialCapital?.value || '10000'),
    risk: parseFloat(el.btRisk?.value || el.risk?.value || '10'),
    commission_bps: parseFloat(el.btCommission?.value || '0'),
    spread_bps: parseFloat(el.btSpread?.value || '0'),
    slippage_bps: parseFloat(el.btSlippage?.value || '0'),
    leverage: parseFloat(el.btLeverage?.value || '1'),
    risk_reward_ratio: parseFloat(el.btRiskReward?.value || '2'),
    target_mode: el.btTargetMode?.value || 'strategy_or_rr',
    allow_shorts: Boolean(el.btAllowShorts?.checked),
    fill_model: el.btFillModel?.value || 'next_open',
    position_sizing: el.btSizing?.value || 'risk_fixed',
    risk_mode: el.btRiskMode?.value || 'fixed_amount',
    opposite_signal_mode: el.btOppositeMode?.value || 'ignore',
    same_bar_priority: el.btSameBarPriority?.value || 'stop_first',
    finalize_open_trade: Boolean(el.btFinalizeOpen?.checked),
    ai_verification_enabled: Boolean(el.btAiVerify?.checked),
    ai_min_confidence: parseFloat(el.btAiMinConfidence?.value || '70'),
    ai_candles: parseInt(el.btAiCandles?.value || '80', 10),
  };
}

function setBacktestButtonRunning(running) {
  if (!el.runBacktestBtn) return;
  el.runBacktestBtn.disabled = Boolean(running);
  el.runBacktestBtn.textContent = running ? 'Running...' : 'Run Backtest';
}

function stopBacktestPolling() {
  if (state.backtestJobTimer) {
    clearTimeout(state.backtestJobTimer);
    state.backtestJobTimer = null;
  }
}

function renderBacktestJob(job) {
  const pct = Math.max(0, Math.min(100, Math.round((parseFloat(job.progress || 0)) * 100)));
  const stats = job.stats || {};
  const stage = job.stage || job.status || 'running';
  const statusType = job.status === 'failed' ? 'err' : (job.status === 'completed' ? 'ok' : '');
  setBacktestStatus(`${pct}% · ${stage} · ${job.message || 'Running backtest.'}`, statusType);

  if (el.backtestSummary) {
    const barsDone = stats.bars_done != null && stats.bars_total != null
      ? `${stats.bars_done}/${stats.bars_total}`
      : (stats.bars_total ?? '—');
    const cells = [
      ['Stage', stage],
      ['Progress', `${pct}%`],
      ['Bars', barsDone],
      ['Trades', stats.trades ?? '—'],
      ['Skipped', stats.skipped ?? '—'],
      ['AI Checks', stats.ai_checks ?? 0],
      ['AI Pass', stats.ai_approved ?? 0],
    ];
    el.backtestSummary.innerHTML = cells.map(([k, v]) => `<div class="bt-stat"><span>${k}</span><strong>${escapeHtml(String(v))}</strong></div>`).join('');
  }

  if (el.backtestEvents) {
    const events = job.events || [];
    if (events.length) {
      el.backtestEvents.innerHTML = events.slice().reverse().slice(0, 35).map(e => `<div>${escapeHtml(e)}</div>`).join('');
    } else {
      el.backtestEvents.innerHTML = `<div>${escapeHtml(job.message || 'Queued')}</div>`;
    }
  }
}

async function pollBacktestJob(jobId) {
  if (!jobId || state.activeBacktestJobId !== jobId) return;
  try {
    const res = await fetch(`/api/backtests/jobs/${jobId}`);
    const job = await res.json();
    if (!job.ok) {
      setBacktestStatus(job.message || 'Backtest job not found.', 'err');
      setBacktestButtonRunning(false);
      state.activeBacktestJobId = null;
      return;
    }
    renderBacktestJob(job);
    if (job.status === 'completed' && job.result) {
      renderBacktestResult(job.result);
      setBacktestStatus(`Run #${job.result.run_id}`, 'ok');
      setBacktestButtonRunning(false);
      state.activeBacktestJobId = null;
      stopBacktestPolling();
      return;
    }
    if (job.status === 'failed') {
      setBacktestStatus(job.error || job.message || 'Backtest failed.', 'err');
      setBacktestButtonRunning(false);
      state.activeBacktestJobId = null;
      stopBacktestPolling();
      return;
    }
    state.backtestJobTimer = setTimeout(() => pollBacktestJob(jobId), job.stage === 'ai_verification' ? 1200 : 800);
  } catch (err) {
    setBacktestStatus('Network error while checking backtest progress.', 'err');
    setBacktestButtonRunning(false);
    state.activeBacktestJobId = null;
    stopBacktestPolling();
  }
}

async function runBacktest() {
  stopBacktestPolling();
  state.activeBacktestJobId = null;
  setBacktestButtonRunning(true);
  setBacktestStatus('Starting...', '');
  if (el.backtestTradesBody) {
    el.backtestTradesBody.innerHTML = '<tr><td colspan="7" class="empty-row">Backtest running...</td></tr>';
  }
  if (el.backtestEvents) el.backtestEvents.innerHTML = '<div>Queued</div>';
  try {
    const res = await fetch('/api/backtests/run/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(backtestRequestPayload()),
    });
    const data = await res.json();
    if (!data.ok) {
      setBacktestStatus(data.message || 'Backtest failed to start.', 'err');
      setBacktestButtonRunning(false);
      return;
    }
    state.activeBacktestJobId = data.job_id;
    renderBacktestJob(data);
    pollBacktestJob(data.job_id);
  } catch (err) {
    setBacktestStatus('Network error while starting backtest.', 'err');
    setBacktestButtonRunning(false);
  }
}

function renderBacktestResult(data) {
  state.lastBacktestRunId = data.run_id;
  const s = data.summary || {};
  if (el.backtestSummary) {
    const cells = [
      ['Final', s.final_equity != null ? '$' + fmtReportNum(s.final_equity, 2) : '—'],
      ['Return', s.total_return_pct != null ? fmtPct(s.total_return_pct) : '—'],
      ['Max DD', s.max_drawdown_pct != null ? fmtPct(s.max_drawdown_pct) : '—'],
      ['Sharpe', s.sharpe ?? '—'],
      ['Win Rate', s.win_rate_pct != null ? fmtPct(s.win_rate_pct) : '—'],
      ['Trades', s.trades ?? '—'],
      ['Skipped', s.skipped_signals ?? 0],
      ['Lev Used', s.max_leverage_used != null ? `${fmtReportNum(s.max_leverage_used, 2)}x` : '—'],
      ['R:R', s.risk_reward_ratio != null ? fmtReportNum(s.risk_reward_ratio, 2) : (data.config?.risk_reward_ratio ?? '—')],
    ];
    if (data.config?.ai_verification_enabled || s.ai_verified_trades) {
      cells.push(['AI Verified', s.ai_verified_trades ?? 0]);
    }
    el.backtestSummary.innerHTML = cells.map(([k, v]) => `<div class="bt-stat"><span>${k}</span><strong>${v}</strong></div>`).join('');
  }
  if (el.backtestTradesBody) {
    const trades = data.trades || [];
    el.backtestTradesBody.innerHTML = trades.length ? trades.slice().reverse().map((t, i) => {
      const pnl = parseFloat(t.net_pnl || 0);
      const ai = t.ai_confidence != null ? ` · AI ${parseFloat(t.ai_confidence).toFixed(1)}%` : '';
      return `<tr>
        <td>${trades.length - i}</td>
        <td class="${t.side === 'LONG' ? 'bull' : 'bear'}">${t.side || '—'}</td>
        <td>${fmtTs(t.entry_ts)}</td>
        <td>${fmtTs(t.exit_ts)}</td>
        <td class="mono">${fmtReportNum(t.qty, 8)}</td>
        <td class="${pnl >= 0 ? 'bull' : 'bear'} mono">${pnl >= 0 ? '+' : ''}${fmtReportNum(pnl, 4)}</td>
        <td>${escapeHtml(t.exit_reason || '—')}${t.leverage_used ? ` · ${parseFloat(t.leverage_used).toFixed(2)}x` : ''}${ai}</td>
      </tr>`;
    }).join('') : '<tr><td colspan="7" class="empty-row">No trades generated.</td></tr>';
  }
  if (el.backtestEvents) {
    const events = data.events || [];
    el.backtestEvents.innerHTML = events.slice().reverse().slice(0, 25).map(e => `<div>${escapeHtml(e)}</div>`).join('');
  }
  if (el.btTradesCsv) {
    el.btTradesCsv.hidden = false;
    el.btTradesCsv.href = `/api/backtests/${data.run_id}/trades.csv`;
  }
  if (el.btEquityCsv) {
    el.btEquityCsv.hidden = false;
    el.btEquityCsv.href = `/api/backtests/${data.run_id}/equity.csv`;
  }
  drawBacktestEquityChart(data.equity || []);
}

function drawBacktestEquityChart(points) {
  const canvas = el.backtestEquityChart;
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(rect.width * dpr));
  canvas.height = Math.max(1, Math.floor(rect.height * dpr));
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = cssVar('--surface-2') || '#10172a';
  ctx.fillRect(0, 0, W, H);
  if (!points.length) return;
  const vals = points.map(p => parseFloat(p.equity)).filter(Number.isFinite);
  if (vals.length < 2) return;
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const span = Math.max(max - min, Math.abs(max) * 0.001, 1);
  const pad = 14;
  ctx.strokeStyle = cssVar('--border-3') || '#233248';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(pad, H - pad);
  ctx.lineTo(W - pad, H - pad);
  ctx.stroke();
  ctx.strokeStyle = cssVar('--bull') || '#00c896';
  ctx.lineWidth = 1.8;
  ctx.beginPath();
  vals.forEach((v, i) => {
    const x = pad + (i / (vals.length - 1)) * (W - pad * 2);
    const y = pad + ((max - v) / span) * (H - pad * 2);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.stroke();
}

// Keyed by timeframe id — holds max_lookback_days, recommended_lookback_days, group
const _timeframeMeta = {};

function _buildTimeframeOptgroups(selectEl, tfList, currentValue, fallback) {
  if (!selectEl || !tfList.length) return;
  const groups = {};
  tfList.forEach(t => {
    const g = t.group || 'other';
    if (!groups[g]) groups[g] = [];
    groups[g].push(t);
  });
  const groupOrder = ['minutes', 'hours', 'daily', 'other'];
  const groupLabels = { minutes: 'Minutes', hours: 'Hours', daily: 'Daily', other: 'Other' };
  selectEl.innerHTML = groupOrder
    .filter(g => groups[g])
    .map(g =>
      `<optgroup label="${groupLabels[g]}">${
        groups[g].map(t => `<option value="${t.id}">${t.label || t.id}</option>`).join('')
      }</optgroup>`
    ).join('');
  const desired = currentValue || fallback || DEFAULT_TIMEFRAME;
  const allValues = new Set(tfList.map(t => t.id));
  selectEl.value = allValues.has(desired) ? desired : (allValues.has(fallback) ? fallback : tfList[0]?.id);
}

function _applyLookbackConstraints(tfId) {
  const meta = _timeframeMeta[tfId];
  if (!meta || !el.btLookback) return;
  const maxDays = meta.max_lookback_days;
  const recDays = meta.recommended_lookback_days;
  el.btLookback.max = maxDays;
  if (parseInt(el.btLookback.value, 10) > maxDays) el.btLookback.value = recDays;
  if (el.btLookbackHint) {
    el.btLookbackHint.textContent = `Max ${maxDays}d · Recommended ${recDays}d`;
  }
}

async function loadTimeframes() {
  try {
    const res = await fetch('/api/timeframes');
    const data = await res.json();
    if (!data.ok) return;
    const tfList = data.timeframes || [];
    if (!tfList.length) return;
    // Cache metadata for lookback constraint logic
    tfList.forEach(t => { _timeframeMeta[t.id] = t; });
    const pref = readDashboardPreferences();
    // Dashboard timeframe (flat list, no optgroups needed)
    const flatOpts = tfList.map(t => ({ value: t.id, label: t.label || t.id }));
    applySelectOptions(el.timeframe, flatOpts, pref.timeframe, DEFAULT_TIMEFRAME);
    // Backtest timeframe (grouped)
    _buildTimeframeOptgroups(el.btTimeframeSelect, tfList, pref.btTimeframe || DEFAULT_TIMEFRAME, DEFAULT_TIMEFRAME);
    _applyLookbackConstraints(el.btTimeframeSelect?.value || DEFAULT_TIMEFRAME);
  } catch (err) {
    console.error('Failed to load timeframes', err);
  }
}

function setReportsStatus(message, type = '') {
  if (!el.reportsStatus) return;
  el.reportsStatus.textContent = message || '';
  el.reportsStatus.className = `reports-status ${type ? `reports-status-${type}` : ''}`;
}

function showClearHistoryConfirm() {
  if (!el.reportsClearConfirm) {
    showToast('Reload the page to enable clear history confirmation.', 'err');
    return;
  }
  el.reportsClearConfirm.hidden = false;
  setReportsStatus('Confirm below to clear all stored history.', 'warn');
  el.reportsClearConfirmInput.value = '';
  el.reportsClearConfirmInput.focus();
}

function hideClearHistoryConfirm() {
  if (el.reportsClearConfirm) el.reportsClearConfirm.hidden = true;
  if (el.reportsClearConfirmInput) el.reportsClearConfirmInput.value = '';
  setReportsStatus('', '');
}

async function clearReportHistory(confirmText = '') {
  const typed = confirmText || el.reportsClearConfirmInput?.value || '';
  if (typed !== 'CLEAR HISTORY') {
    setReportsStatus('Type CLEAR HISTORY exactly to enable deletion.', 'err');
    return;
  }

  const btn = el.reportsClearConfirmBtn || el.reportsClearHistoryBtn;
  if (btn) { btn.disabled = true; btn.textContent = 'Clearing...'; }
  setReportsStatus('Clearing stored history...', 'warn');
  try {
    const res = await fetch('/api/reports/clear-history', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: typed }),
    });
    const d = await res.json();
    if (!d.ok) {
      setReportsStatus(d.message || 'Could not clear history.', 'err');
      showToast(d.message || 'Could not clear history.', 'err');
      return;
    }
    if (el.reportsTableBody) {
      el.reportsTableBody.innerHTML = '<tr><td colspan="16" class="empty-row">No trades recorded.</td></tr>';
    }
    if (el.journalBody) {
        el.journalBody.innerHTML = '<tr><td colspan="7" class="empty-row">No trades logged yet.</td></tr>';
    }
    hideClearHistoryConfirm();
    setReportsStatus(d.message || 'History cleared.', 'ok');
    showToast(d.message || 'History cleared.', 'ok');
    await Promise.all([loadReports(), loadTradeJournal()]);
  } catch (err) {
    setReportsStatus('Network error — could not clear history.', 'err');
    showToast('Network error — could not clear history.', 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Confirm Clear'; }
  }
}

async function sendReport() {
  const email = (el.reportEmailInput?.value || '').trim();
  if (!email || !email.includes('@')) {
    showToast('Enter a valid email address.', 'err');
    return;
  }
  const btn = el.btnSendReport;
  if (btn) { btn.disabled = true; btn.textContent = 'Sending…'; }
  try {
    const res = await fetch('/api/reports/send-email', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ to: email }),
    });
    const d = await res.json();
    if (d.ok) {
      const parts = d.paper_count + d.real_count > 0
        ? `${d.paper_count + d.real_count} trade(s) across ${[d.paper_count && 'paper', d.real_count && 'real'].filter(Boolean).join(' & ')} — PDF attached`
        : 'No trades yet — summary sent';
      showToast(`✓ Report sent to ${email}. ${parts}.`, 'ok');
    } else {
      showToast(d.message || 'Failed to send report.', 'err');
    }
  } catch (err) {
    showToast('Network error — could not send report.', 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Send Now'; }
  }
}

// ─── Futures tracker ──────────────────────────────────────────────────────────
async function loadFuturesMarkets() {
  try {
    const res = await fetch('/api/futures-markets');
    const data = await res.json();
    state.availableCoins = (data.coins || []).map(c => c.coin);
    state.selectedCoins.forEach(coin => {
      if (!state.availableCoins.includes(coin)) state.availableCoins.push(coin);
    });
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
      saveDashboardPreferences();
      renderCoinPicker();
      updateTrackedCoinsMenu();
    });
  });
}

async function loadTracking() {
  const coins = [...state.selectedCoins].join(',');
  if (!coins) return;
  const strategy = normalizeStrategy(el.strategy.value);
  const risk = parseFloat(el.risk.value) || 10;
  const timeframe = normalizeTimeframe(el.timeframe?.value);
  el.trackingMeta.textContent = `Tracking ${state.selectedCoins.size} coin(s)… (last update: ${new Date().toLocaleTimeString()})`;
  try {
    const res = await fetch(`/api/track?coins=${encodeURIComponent(coins)}&strategy=${strategy}&risk=${risk}&lookback_days=2&timeframe=${encodeURIComponent(timeframe)}&exec_mode=${state.realOrdersArmed ? 'real' : 'paper'}`);
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
  state.trackingActive = true;
  saveDashboardPreferences();
  updateTrackedCoinsMenu();
  loadTracking();
  clearInterval(state.trackingInterval);
  state.trackingInterval = setInterval(loadTracking, 15_000);
}

function stopTracking() {
  state.trackingActive = false;
  saveDashboardPreferences();
  clearInterval(state.trackingInterval);
  state.trackingInterval = null;
  el.trackingMeta.textContent = 'Tracking stopped.';
  updateTrackedCoinsMenu();
}

function updateTrackedCoinsMenu() {
  const menu = document.getElementById('trackedCoinsMenu');
  const items = document.getElementById('coinsMenuItems');
  
  if (!menu || !items) return;
  
  if (!state.trackingActive || state.selectedCoins.size === 0) {
    menu.style.display = 'none';
    return;
  }
  
  menu.style.display = 'flex';
  
  const currentPair = el.pair.value.toUpperCase();
  
  items.innerHTML = Array.from(state.selectedCoins).map(coin => {
    // Determine if this coin is currently selected
    const isActive = currentPair.includes(coin);
    const activeClass = isActive ? 'active' : '';
    return `<button class="coin-menu-item ${activeClass}" data-coin="${coin}" title="Switch to ${coin}">${coin}</button>`;
  }).join('');
  
  // Add click handlers to menu items
  items.querySelectorAll('.coin-menu-item').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      const coin = btn.dataset.coin;
      switchTrackedCoin(coin);
      
      // Update active states
      items.querySelectorAll('.coin-menu-item').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
    });
  });
}

function switchTrackedCoin(coin) {
  // Map common coin symbols to their pair/market format
  const coinMap = {
    'BTC': { pair: 'B-BTC_USDT', market: 'BTCUSDT' },
    'ETH': { pair: 'B-ETH_USDT', market: 'ETHUSDT' },
    'SOL': { pair: 'B-SOL_USDT', market: 'SOLUSDT' },
    'XAU': { pair: 'B-XAU_USDT', market: 'XAUSUSDT' },
    'BNB': { pair: 'B-BNB_USDT', market: 'BNBUSDT' },
    'NEAR': { pair: 'B-NEAR_USDT', market: 'NEARUSDT' },
    'ARB': { pair: 'B-ARB_USDT', market: 'ARBUSDT' },
    'DOGE': { pair: 'B-DOGE_USDT', market: 'DOGEUSDT' },
    'XRP': { pair: 'B-XRP_USDT', market: 'XRPUSDT' },
    'ADA': { pair: 'B-ADA_USDT', market: 'ADAUSDT' },
  };
  
  // Get the mapping or create a default one
  const mapping = coinMap[coin] || {
    pair: `B-${coin}_USDT`,
    market: `${coin}USDT`
  };
  
  // Update inputs
  el.pair.value = mapping.pair;
  el.market.value = mapping.market;
  
  // Save preferences and reload
  saveDashboardPreferences();
  updateTrackedCoinsMenu();
  loadSnapshot();
  loadAccount();
}

// ─── Event wiring ─────────────────────────────────────────────────────────────
function wireEvents() {
  document.addEventListener('click', e => {
    const target = e.target;
    if (!(target instanceof Element)) return;
    if (target.closest('#reportsClearHistoryBtn')) showClearHistoryConfirm();
    if (target.closest('#reportsClearConfirmBtn')) clearReportHistory();
    if (target.closest('#reportsClearCancelBtn')) hideClearHistoryConfirm();
  });
  el.menuButton.addEventListener('click', () => setDrawerOpen(true));
  el.drawerClose.addEventListener('click', () => setDrawerOpen(false));
  el.drawerBackdrop.addEventListener('click', () => setDrawerOpen(false));
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') setDrawerOpen(false);
  });
  el.themeSelect.addEventListener('change', () => {
    applyTheme(el.themeSelect.value);
    saveDashboardPreferences();
  });

  el.refresh.addEventListener('click', () => {
    saveDashboardPreferences();
    loadSnapshot();
    loadAccount();
  });
  el.realOrders.addEventListener('change', () => {
    if (el.realOrders.checked) {
      const confirmed = window.confirm(
        'Arm real-order intent for this dashboard? This remembers your choice, but live orders still require running the bot with --place-orders.'
      );
      if (!confirmed) {
        el.realOrders.checked = false;
        saveExecutionPreference(false);
        return;
      }
      saveExecutionPreference(true);
      return;
    }
    saveExecutionPreference(false);
  });
  el.refreshAccount.addEventListener('click', loadAccount);
  el.refreshJournal.addEventListener('click', loadTradeJournal);
  if (el.volScanButton) {
    el.volScanButton.addEventListener('click', loadVolatilityScan);
  }
  if (el.actVolScanner) {
    el.actVolScanner.addEventListener('click', () => toggleVolSidebar(true));
  }
  if (el.actDashboard) {
    el.actDashboard.addEventListener('click', () => {
      toggleVolSidebar(false);
      if (el.expertSidebar && !el.expertSidebar.hidden) {
        toggleExpertSidebar(false);
      }
      if (el.reportsSidebar && !el.reportsSidebar.hidden) {
        toggleReportsSidebar(false);
      }
      if (el.backtestsSidebar && !el.backtestsSidebar.hidden) {
        toggleBacktestsSidebar(false);
      }
      el.actDashboard?.setAttribute('aria-pressed', 'true');
    });
  }
  if (el.volSidebarClose) {
    el.volSidebarClose.addEventListener('click', () => toggleVolSidebar(false));
  }
  if (el.actExpertPicks) {
    el.actExpertPicks.addEventListener('click', () => toggleExpertSidebar(true));
  }
  if (el.expertSidebarClose) {
    el.expertSidebarClose.addEventListener('click', () => toggleExpertSidebar(false));
  }
  if (el.expertRefreshBtn) {
    el.expertRefreshBtn.addEventListener('click', () => loadExpertPicks(true));
  }
  if (el.actReports) {
    el.actReports.addEventListener('click', () => toggleReportsSidebar(true));
  }
  if (el.reportsSidebarClose) {
    el.reportsSidebarClose.addEventListener('click', () => toggleReportsSidebar(false));
  }
  if (el.actBacktests) {
    el.actBacktests.addEventListener('click', () => toggleBacktestsSidebar(true));
  }
  if (el.backtestsSidebarClose) {
    el.backtestsSidebarClose.addEventListener('click', () => toggleBacktestsSidebar(false));
  }
  if (el.backtestsRefreshBtn) {
    el.backtestsRefreshBtn.addEventListener('click', () => loadBacktestStrategies());
  }
  if (el.customStrategySelect) {
    el.customStrategySelect.addEventListener('change', () => loadCustomStrategy(el.customStrategySelect.value));
  }
  if (el.customStrategyTitle) {
    el.customStrategyTitle.addEventListener('input', () => {
      if (!state.selectedCustomStrategyId && el.customStrategySlug && !el.customStrategySlug.value) {
        el.customStrategySlug.value = slugifyStrategyTitle(el.customStrategyTitle.value);
      }
    });
  }
  if (el.newCustomStrategyBtn) {
    el.newCustomStrategyBtn.addEventListener('click', resetCustomStrategyForm);
  }
  if (el.saveCustomStrategyBtn) {
    el.saveCustomStrategyBtn.addEventListener('click', saveCustomStrategy);
  }
  if (el.validateCustomStrategyBtn) {
    el.validateCustomStrategyBtn.addEventListener('click', validateCustomStrategyCurrent);
  }
  if (el.runBacktestBtn) {
    el.runBacktestBtn.addEventListener('click', runBacktest);
  }
  if (el.reportsRefreshBtn) {
    el.reportsRefreshBtn.addEventListener('click', () => loadReports());
  }
  // Clear-history controls are handled by delegated clicks above so they still
  // work if the reports panel is re-rendered later.
  if (el.reportsClearConfirmInput) {
    el.reportsClearConfirmInput.addEventListener('keydown', e => {
      if (e.key === 'Enter') clearReportHistory();
      if (e.key === 'Escape') hideClearHistoryConfirm();
    });
  }
  if (el.reportsFilterGroup) {
    el.reportsFilterGroup.addEventListener('click', e => {
      const btn = e.target.closest('.reports-filter-btn');
      if (!btn) return;
      el.reportsFilterGroup.querySelectorAll('.reports-filter-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.reportsFilter = btn.dataset.filter || 'all';
      loadReports();
    });
  }
  if (el.btnSendReport) {
    el.btnSendReport.addEventListener('click', () => sendReport());
  }
  el.startTracking.addEventListener('click', startTracking);
  el.stopTracking.addEventListener('click', stopTracking);
  
  if (el.trackerCollapse) {
    el.trackerCollapse.addEventListener('click', () => {
      setTrackerCollapsed(!state.trackerCollapsed, true);
    });
  }
  
  el.addCoin.addEventListener('click', () => {
    const coin = el.customCoin.value.trim().toUpperCase();
    if (coin && state.selectedCoins.size < 12) {
      state.selectedCoins.add(coin);
      if (!state.availableCoins.includes(coin)) state.availableCoins.push(coin);
      el.customCoin.value = '';
      saveDashboardPreferences();
      renderCoinPicker();
      updateTrackedCoinsMenu();
    }
  });
  el.coinSearch.addEventListener('input', renderCoinPicker);
  el.applyRisk.addEventListener('click', () => {
    const v = parseFloat(el.customRisk.value);
    if (v > 0) {
      el.risk.value = v;
      el.currentRisk.textContent = `$${v.toFixed(2)}`;
      saveDashboardPreferences();
    }
  });
  if (el.chainSelect) {
    el.chainSelect.addEventListener('change', () => {
      saveDashboardPreferences();
      loadIntelligence();
    });
  }

  [el.pair, el.market, el.strategy, el.mode, el.risk, el.lookback, el.timeframe, el.pair2, el.market2]
    .filter(Boolean)
    .forEach(input => input.addEventListener('change', saveDashboardPreferences));

  el.risk.addEventListener('input', () => {
    el.currentRisk.textContent = `$${currentRiskAmount().toFixed(2)}`;
    scheduleDashboardPreferencesSave();
  });

  if (el.timeframe) {
    el.timeframe.addEventListener('change', () => {
      saveDashboardPreferences();
      loadSnapshot();
      loadAccount();
      if (state.trackingActive) {
        loadTracking();
      }
    });
  }

  if (el.btTimeframeSelect) {
    el.btTimeframeSelect.addEventListener('change', () => {
      _applyLookbackConstraints(el.btTimeframeSelect.value);
      saveDashboardPreferences();
    });
  }

  // Update tracked coins menu when pair changes
  el.pair.addEventListener('change', updateTrackedCoinsMenu);

  // Keyboard shortcut: Enter on pair/market inputs triggers refresh
  [el.pair, el.market].forEach(inp => inp.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      saveDashboardPreferences();
      updateTrackedCoinsMenu();
      loadSnapshot();
      loadAccount();
    }
  }));

  // Show/hide pair2 fields based on strategy
  function togglePair2Fields() {
    const show = el.strategy.value === 'pairs_stat_arb';
    document.querySelectorAll('.pair2-field').forEach(f => {
      f.style.display = show ? '' : 'none';
    });
  }
  el.strategy.addEventListener('change', () => {
    saveDashboardPreferences();
    togglePair2Fields();
    loadSnapshot();
    loadAccount();
    if (state.trackingActive) {
      loadTracking();
    }
  });
  togglePair2Fields();
}

// ─── Init ─────────────────────────────────────────────────────────────────────
async function init() {
  initPriceChart();
  loadExecutionPreference();
  await Promise.all([loadStrategies(), loadTimeframes()]);
  await applyDashboardPreferences();
  await applyRuntimeConfig();
  wireEvents();
  connectWebSocket();
  startIntelligencePolling();

  await loadFuturesMarkets();
  await loadSnapshot();
  loadAccount();
  loadTradeJournal();
  if (state.trackingActive) {
    startTracking();
    updateTrackedCoinsMenu();
  }
}

document.addEventListener('DOMContentLoaded', init);
