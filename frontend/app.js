const els = {
  pair: document.querySelector("#pairInput"),
  market: document.querySelector("#marketInput"),
  strategy: document.querySelector("#strategyInput"),
  mode: document.querySelector("#modeInput"),
  risk: document.querySelector("#riskInput"),
  reward: document.querySelector("#rewardInput"),
  leverage: document.querySelector("#leverageInput"),
  lookback: document.querySelector("#lookbackInput"),
  refresh: document.querySelector("#refreshButton"),
  status: document.querySelector("#connectionStatus"),
  subtitle: document.querySelector("#subtitle"),
  lastPrice: document.querySelector("#lastPrice"),
  changePct: document.querySelector("#changePct"),
  signalText: document.querySelector("#signalText"),
  signalMeta: document.querySelector("#signalMeta"),
  scoreText: document.querySelector("#scoreText"),
  rsiText: document.querySelector("#rsiText"),
  positionText: document.querySelector("#positionText"),
  positionMeta: document.querySelector("#positionMeta"),
  feedText: document.querySelector("#feedText"),
  priceChartTitle: document.querySelector("#priceChartTitle"),
  lineLegend: document.querySelector("#lineLegend"),
  scoreChartTitle: document.querySelector("#scoreChartTitle"),
  scoreChartText: document.querySelector("#scoreChartText"),
  strategyList: document.querySelector("#strategyList"),
  stateList: document.querySelector("#stateList"),
  eventList: document.querySelector("#eventList"),
  coinPicker: document.querySelector("#coinPicker"),
  coinSearch: document.querySelector("#coinSearchInput"),
  selectedCount: document.querySelector("#selectedCount"),
  customCoin: document.querySelector("#customCoinInput"),
  addCoin: document.querySelector("#addCoinButton"),
  startTracking: document.querySelector("#startTrackingButton"),
  stopTracking: document.querySelector("#stopTrackingButton"),
  trackingMeta: document.querySelector("#trackingMeta"),
  trackingBody: document.querySelector("#trackingBody"),
  refreshAccount: document.querySelector("#refreshAccountButton"),
  walletBalance: document.querySelector("#walletBalance"),
  walletStatus: document.querySelector("#walletStatus"),
  currentRiskText: document.querySelector("#currentRiskText"),
  riskModelText: document.querySelector("#riskModelText"),
  riskButtons: document.querySelector("#riskButtons"),
  customRisk: document.querySelector("#customRiskInput"),
  applyCustomRisk: document.querySelector("#applyCustomRiskButton"),
  positionsMeta: document.querySelector("#positionsMeta"),
  positionsBody: document.querySelector("#positionsBody"),
  priceChart: document.querySelector("#priceChart"),
  scoreChart: document.querySelector("#scoreChart"),
  rsiChart: document.querySelector("#rsiChart"),
};

let refreshTimer = null;
let lastSnapshot = null;
let liveSource = null;
let liveQuote = null;
let liveSourceKey = "";
let availableCoins = [];
let selectedCoins = new Set(["BTC", "ETH", "SOL"]);
let trackingTimer = null;
let isTracking = false;
let saveTimer = null;
let settingsLoaded = false;

function fmtNumber(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return Number(value).toLocaleString(undefined, {
    maximumFractionDigits: digits,
    minimumFractionDigits: Math.min(digits, 2),
  });
}

function fmtPrice(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "--";
  const digits = n >= 1000 ? 2 : n >= 1 ? 4 : 8;
  return n.toLocaleString(undefined, { maximumFractionDigits: digits });
}

function setStatus(text, mode) {
  els.status.textContent = text;
  els.status.className = `status-pill ${mode || ""}`.trim();
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function fitCanvas(canvas) {
  const ratio = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(320, Math.floor(rect.width));
  const height = Number(canvas.getAttribute("height")) || 220;
  canvas.width = Math.floor(width * ratio);
  canvas.height = Math.floor(height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  return { ctx, width, height };
}

function drawGrid(ctx, width, height, pad) {
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#121517";
  ctx.fillRect(0, 0, width, height);
  ctx.strokeStyle = "#273039";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i += 1) {
    const y = pad.top + ((height - pad.top - pad.bottom) * i) / 4;
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(width - pad.right, y);
    ctx.stroke();
  }
}

function makeScale(values, minPad = 0.08) {
  const nums = values.filter((v) => Number.isFinite(v));
  if (!nums.length) return { min: 0, max: 1 };
  let min = Math.min(...nums);
  let max = Math.max(...nums);
  if (min === max) {
    min *= 0.99;
    max *= 1.01;
  }
  const pad = (max - min) * minPad;
  return { min: min - pad, max: max + pad };
}

function drawLine(ctx, points, color, width = 2) {
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.beginPath();
  let started = false;
  for (const point of points) {
    if (!Number.isFinite(point.y)) {
      started = false;
      continue;
    }
    if (!started) {
      ctx.moveTo(point.x, point.y);
      started = true;
    } else {
      ctx.lineTo(point.x, point.y);
    }
  }
  ctx.stroke();
}

function drawAxisLabels(ctx, width, height, pad, scale, digits = 2) {
  ctx.fillStyle = "#97a2aa";
  ctx.font = "11px ui-monospace, Menlo, Consolas, monospace";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  for (let i = 0; i <= 4; i += 1) {
    const y = pad.top + ((height - pad.top - pad.bottom) * i) / 4;
    const value = scale.max - ((scale.max - scale.min) * i) / 4;
    ctx.fillText(fmtNumber(value, digits), width - 8, y);
  }
}

function drawPriceChart(snapshot) {
  const { ctx, width, height } = fitCanvas(els.priceChart);
  const pad = { top: 14, right: 74, bottom: 24, left: 12 };
  drawGrid(ctx, width, height, pad);
  const bars = snapshot.bars || [];
  if (bars.length < 2) return;

  const values = bars.flatMap((b) => [Number(b.high), Number(b.low), Number(b.supertrend)]);
  const scale = makeScale(values);
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const xAt = (i) => pad.left + (plotW * i) / (bars.length - 1);
  const yAt = (v) => pad.top + plotH - ((v - scale.min) / (scale.max - scale.min)) * plotH;
  const candleW = Math.max(2, Math.min(9, plotW / bars.length / 1.4));

  bars.forEach((bar, i) => {
    const open = Number(bar.open);
    const high = Number(bar.high);
    const low = Number(bar.low);
    const close = Number(bar.close);
    if (![open, high, low, close].every(Number.isFinite)) return;
    const x = xAt(i);
    const up = close >= open;
    ctx.strokeStyle = up ? cssVar("--green") : cssVar("--red");
    ctx.fillStyle = up ? "rgba(56, 201, 120, 0.62)" : "rgba(239, 98, 98, 0.62)";
    ctx.beginPath();
    ctx.moveTo(x, yAt(low));
    ctx.lineTo(x, yAt(high));
    ctx.stroke();
    const top = Math.min(yAt(open), yAt(close));
    const bodyH = Math.max(2, Math.abs(yAt(open) - yAt(close)));
    ctx.fillRect(x - candleW / 2, top, candleW, bodyH);
  });

  drawLine(
    ctx,
    bars.map((b, i) => ({ x: xAt(i), y: yAt(Number(b.close)) })),
    cssVar("--cyan"),
    2
  );
  drawLine(
    ctx,
    bars.map((b, i) => ({ x: xAt(i), y: yAt(Number(b.supertrend)) })),
    cssVar("--amber"),
    1.5
  );
  drawAxisLabels(ctx, width, height, pad, scale, scale.max > 1000 ? 0 : 4);
}

function drawScoreChart(snapshot) {
  const { ctx, width, height } = fitCanvas(els.scoreChart);
  const pad = { top: 12, right: 34, bottom: 22, left: 28 };
  drawGrid(ctx, width, height, pad);
  const bars = (snapshot.bars || []).filter((b) => b.raw_score !== null || b.score !== null);
  if (!bars.length) return;
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const values = bars.map((b) => Number(b.raw_score ?? b.score)).filter(Number.isFinite);
  const min = Math.min(-5, ...values);
  const max = Math.max(5, ...values);
  const range = max - min || 1;
  const yAt = (v) => pad.top + plotH - ((v - min) / range) * plotH;
  const w = Math.max(2, plotW / bars.length - 1);

  ctx.strokeStyle = "#56616b";
  [min <= -3 && max >= -3 ? -3 : null, 0, min <= 3 && max >= 3 ? 3 : null].forEach((level) => {
    if (level === null) return;
    ctx.beginPath();
    ctx.moveTo(pad.left, yAt(level));
    ctx.lineTo(width - pad.right, yAt(level));
    ctx.stroke();
  });

  bars.forEach((bar, i) => {
    const score = Number(bar.raw_score ?? bar.score);
    const x = pad.left + (plotW * i) / bars.length;
    const zero = yAt(0);
    const y = yAt(score);
    ctx.fillStyle = score >= 3 ? cssVar("--green") : score <= -3 ? cssVar("--red") : cssVar("--blue");
    ctx.fillRect(x, Math.min(zero, y), w, Math.max(2, Math.abs(zero - y)));
  });
}

function renderStrategy(strategy, indicators) {
  const notes = strategy?.notes?.length ? strategy.notes.join(" ") : "--";
  const rows = [
    ["Name", strategy?.name || "--"],
    ["Rule", strategy?.description || "--"],
    ["Reason", strategy?.reason || "--"],
    ["Notes", notes],
    ["Spread bps", fmtNumber(indicators?.spread_bps, 1)],
    ["Spot", fmtPrice(indicators?.spot_price)],
    ["Futures", fmtPrice(indicators?.futures_price)],
  ];
  els.strategyList.innerHTML = rows
    .filter(([key, value]) => key !== "Spread bps" || value !== "--" || strategy?.id === "arbitrage")
    .filter(([key, value]) => key !== "Spot" || value !== "--" || strategy?.id === "arbitrage")
    .filter(([key, value]) => key !== "Futures" || value !== "--" || strategy?.id === "arbitrage")
    .map(([key, value]) => `<dt>${key}</dt><dd>${value ?? "--"}</dd>`)
    .join("");
}

function drawRsiChart(snapshot) {
  const { ctx, width, height } = fitCanvas(els.rsiChart);
  const pad = { top: 12, right: 38, bottom: 22, left: 28 };
  drawGrid(ctx, width, height, pad);
  const bars = (snapshot.bars || []).filter((b) => b.rsi !== null);
  if (!bars.length) return;
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const xAt = (i) => pad.left + (plotW * i) / (bars.length - 1);
  const yAt = (v) => pad.top + plotH - (v / 100) * plotH;

  ctx.strokeStyle = "#56616b";
  [30, 50, 70].forEach((level) => {
    ctx.beginPath();
    ctx.moveTo(pad.left, yAt(level));
    ctx.lineTo(width - pad.right, yAt(level));
    ctx.stroke();
  });
  drawLine(
    ctx,
    bars.map((b, i) => ({ x: xAt(i), y: yAt(Number(b.rsi)) })),
    cssVar("--cyan"),
    2
  );
}

function renderState(state) {
  const rows = [
    ["Side", state.side],
    ["Trade", state.trade_id],
    ["Entry", fmtPrice(state.entry_px)],
    ["Stop", fmtPrice(state.stop_px)],
    ["Target", fmtPrice(state.target_px)],
    ["Quantity", fmtNumber(state.qty, 8)],
    ["Notional", `$${fmtNumber(state.notional, 2)}`],
    ["Margin", `$${fmtNumber(state.margin_required, 2)}`],
    ["Reward", state.reward_ratio ? `1:${fmtNumber(state.reward_ratio, 2)}` : "--"],
    ["Realized PnL", `$${fmtNumber(state.realized_pnl, 2)}`],
    ["Broker", state.broker_order_status || "--"],
  ];
  els.stateList.innerHTML = rows
    .map(([key, value]) => `<dt>${key}</dt><dd>${value ?? "--"}</dd>`)
    .join("");
}

function renderEvents(events) {
  if (!events || !events.length) {
    els.eventList.innerHTML = "<li>No entries or exits in the current replay window.</li>";
    return;
  }
  els.eventList.innerHTML = events
    .slice()
    .reverse()
    .map((event) => `<li>${String(event).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]))}</li>`)
    .join("");
}

function pairForCoin(coin) {
  return `B-${coin.toUpperCase()}_USDT`;
}

function marketForCoin(coin) {
  return `${coin.toUpperCase()}USDT`;
}

function renderCoinPicker() {
  if (!els.coinPicker) return;
  const query = (els.coinSearch?.value || "").trim().toUpperCase();
  const filtered = availableCoins
    .filter((item) => {
      if (!query) return true;
      return `${item.coin} ${item.pair} ${item.market}`.toUpperCase().includes(query);
    })
    .slice(0, 160);
  els.selectedCount.textContent = `${selectedCoins.size} selected · ${availableCoins.length} available`;
  els.coinPicker.innerHTML = filtered
    .map((item) => {
      const coin = item.coin;
      const active = selectedCoins.has(coin);
      const leverage = item.max_leverage ? `${fmtNumber(item.max_leverage, 0)}x` : "";
      return `<button type="button" class="coin-chip ${active ? "active" : ""}" data-coin="${coin}" title="${item.pair}">
        <strong>${coin}</strong><small>${leverage}</small>
      </button>`;
    })
    .join("");
  if (!filtered.length) {
    els.coinPicker.innerHTML = `<div class="empty-picker">No matching futures markets found.</div>`;
    return;
  }
  els.coinPicker.querySelectorAll(".coin-chip").forEach((button) => {
    button.addEventListener("click", () => {
      const coin = button.dataset.coin;
      if (selectedCoins.has(coin)) {
        selectedCoins.delete(coin);
      } else {
        selectedCoins.add(coin);
      }
      renderCoinPicker();
      saveSettingsSoon();
    });
  });
}

function addCustomCoin() {
  const coin = (els.customCoin.value || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
  if (!coin) return;
  if (!availableCoins.some((item) => item.coin === coin)) {
    availableCoins.push({ coin, pair: pairForCoin(coin), market: marketForCoin(coin) });
  }
  selectedCoins.add(coin);
  els.customCoin.value = "";
  renderCoinPicker();
  saveSettingsSoon();
}

function renderTrackingRows(rows) {
  if (!rows || !rows.length) {
    els.trackingBody.innerHTML = `<tr><td colspan="9">No coins selected.</td></tr>`;
    return;
  }
  els.trackingBody.innerHTML = rows
    .map((row) => {
      const sideClass = row.side === "LONG" ? "positive" : row.side === "SHORT" ? "negative" : "neutral";
      const fresh = Number(row.freshness_minutes);
      const freshText = Number.isFinite(fresh) ? `${fmtNumber(fresh, 1)}m` : "--";
      return `
        <tr class="${row.ok ? "" : "bad-row"}" data-pair="${row.pair || ""}" data-market="${row.market || ""}">
          <td><button type="button" class="row-coin">${row.coin || "--"}</button></td>
          <td>${fmtPrice(row.last_price)}</td>
          <td><span class="${sideClass}">${row.signal || row.message || "--"}</span></td>
          <td>${fmtNumber(row.score, 1)}</td>
          <td>${fmtNumber(row.rsi, 1)}</td>
          <td>${row.position || "--"}</td>
          <td>${fmtNumber(row.margin_required, 2)}</td>
          <td>${row.reward_ratio ? `1:${fmtNumber(row.reward_ratio, 2)}` : "--"}</td>
          <td>${freshText}</td>
        </tr>
      `;
    })
    .join("");
  els.trackingBody.querySelectorAll("tr[data-pair]").forEach((row) => {
    row.addEventListener("click", () => {
      if (!row.dataset.pair) return;
      els.pair.value = row.dataset.pair;
      els.market.value = row.dataset.market;
      els.mode.value = "futures";
      loadSnapshot();
    });
  });
}

function renderRiskButtons(suggestions) {
  if (!suggestions || !suggestions.length) {
    els.riskButtons.innerHTML = `<span class="muted-text">No wallet data</span>`;
    return;
  }
  els.riskButtons.innerHTML = suggestions
    .map((item) => `<button type="button" data-risk="${Number(item.amount).toFixed(4)}">${item.label} · ${fmtNumber(item.amount, 2)}</button>`)
    .join("");
  els.riskButtons.querySelectorAll("button").forEach((button) => {
    button.addEventListener("click", () => {
      applyRiskAmount(button.dataset.risk);
    });
  });
}

function applyRiskAmount(amount) {
  const risk = Number(amount);
  if (!Number.isFinite(risk) || risk <= 0) return;
  els.risk.value = risk.toFixed(4);
  updateRiskText();
  saveSettingsSoon();
  loadSnapshot();
  if (isTracking) loadTracking();
}

function collectSettings() {
  return {
    pair: els.pair.value,
    market: els.market.value,
    mode: els.mode.value,
    strategy: els.strategy.value,
    risk: els.risk.value,
    reward_ratio: els.reward.value,
    leverage: els.leverage.value,
    lookback_days: els.lookback.value,
    selected_coins: [...selectedCoins],
  };
}

async function saveSettings() {
  if (!settingsLoaded) return;
  try {
    await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(collectSettings()),
    });
  } catch (error) {
    console.warn("Could not save settings", error);
  }
}

function saveSettingsSoon() {
  if (!settingsLoaded) return;
  if (saveTimer) window.clearTimeout(saveTimer);
  saveTimer = window.setTimeout(saveSettings, 350);
}

async function loadSettings() {
  try {
    const response = await fetch("/api/settings", { cache: "no-store" });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || response.statusText);
    const settings = data.settings || {};
    els.pair.value = settings.pair || els.pair.value;
    els.market.value = settings.market || els.market.value;
    els.mode.value = settings.mode || els.mode.value;
    els.strategy.value = settings.strategy || els.strategy.value;
    els.risk.value = settings.risk || els.risk.value;
    els.reward.value = settings.reward_ratio || els.reward.value;
    els.leverage.value = settings.leverage || els.leverage.value;
    els.lookback.value = settings.lookback_days || els.lookback.value;
    if (Array.isArray(settings.selected_coins) && settings.selected_coins.length) {
      selectedCoins = new Set(settings.selected_coins.map((coin) => String(coin).toUpperCase()));
    }
  } catch (error) {
    console.warn("Could not load settings", error);
  } finally {
    settingsLoaded = true;
  }
}

function updateRiskText() {
  els.currentRiskText.textContent = `$${fmtNumber(els.risk.value, 2)}`;
  els.riskModelText.textContent = `Target 1:${fmtNumber(els.reward.value, 2)} · margin at ${fmtNumber(els.leverage.value, 2)}x`;
}

function renderPositions(positions) {
  const rows = positions || [];
  els.positionsMeta.textContent = rows.length ? `${rows.length} open` : "No active positions";
  if (!rows.length) {
    els.positionsBody.innerHTML = `<tr><td colspan="8">No active futures positions.</td></tr>`;
    return;
  }
  els.positionsBody.innerHTML = rows
    .map((pos) => {
      const sideClass = pos.side === "LONG" ? "positive" : "negative";
      const pnl = Number(pos.unrealized_pnl);
      const pnlClass = Number.isFinite(pnl) ? (pnl >= 0 ? "positive" : "negative") : "neutral";
      return `
        <tr data-pair="${pos.pair || ""}" data-market="${marketForCoin(pos.coin || "")}">
          <td><button type="button" class="row-coin">${pos.coin || "--"}</button></td>
          <td><span class="${sideClass}">${pos.side || "--"}</span></td>
          <td>${fmtNumber(pos.quantity, 6)}</td>
          <td>${fmtPrice(pos.avg_price)}</td>
          <td>${fmtPrice(pos.mark_price)}</td>
          <td>${fmtPrice(pos.stop_loss_trigger)}</td>
          <td>${fmtPrice(pos.take_profit_trigger)}</td>
          <td><span class="${pnlClass}">${fmtNumber(pos.unrealized_pnl, 2)}</span></td>
        </tr>
      `;
    })
    .join("");
  els.positionsBody.querySelectorAll("tr[data-pair]").forEach((row) => {
    row.addEventListener("click", () => {
      if (!row.dataset.pair) return;
      els.pair.value = row.dataset.pair;
      els.market.value = row.dataset.market;
      els.mode.value = "futures";
      loadSnapshot();
    });
  });
}

async function loadAccount() {
  updateRiskText();
  const params = new URLSearchParams({
    pair: els.pair.value,
    market: els.market.value,
    mode: "futures",
  });
  try {
    const response = await fetch(`/api/account?${params.toString()}`, { cache: "no-store" });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || response.statusText);
    if (!data.has_credentials) {
      els.walletBalance.textContent = "--";
      els.walletStatus.textContent = data.message || "API keys not set.";
      renderRiskButtons([]);
      renderPositions([]);
      return;
    }
    els.walletBalance.textContent = `${fmtNumber(data.available_quote_balance, 2)} ${data.currency || ""}`;
    els.walletStatus.textContent = `${data.mode?.toUpperCase() || "FUTURES"} wallet · read-only`;
    renderRiskButtons(data.risk_suggestions || []);
    renderPositions(data.positions || []);
  } catch (error) {
    els.walletBalance.textContent = "--";
    els.walletStatus.textContent = error.message;
    renderRiskButtons([]);
    renderPositions([]);
  }
}

async function loadTracking() {
  const coins = [...selectedCoins];
  if (!coins.length) {
    renderTrackingRows([]);
    els.trackingMeta.textContent = "Select at least one coin.";
    return;
  }
  const params = new URLSearchParams({
    coins: coins.join(","),
    strategy: els.strategy.value,
    risk: els.risk.value,
    reward_ratio: els.reward.value,
    leverage: els.leverage.value,
    lookback_days: els.lookback.value,
  });
  els.trackingMeta.textContent = `Tracking ${coins.length} futures coins with ${els.strategy.selectedOptions[0]?.textContent || "strategy"}...`;
  try {
    const response = await fetch(`/api/track?${params.toString()}`, { cache: "no-store" });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || response.statusText);
    renderTrackingRows(data.tracked || []);
    els.trackingMeta.textContent = `Tracking ${coins.join(", ")} · ${data.strategy} · ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    els.trackingMeta.textContent = error.message;
  }
}

function startTracking() {
  isTracking = true;
  if (trackingTimer) window.clearInterval(trackingTimer);
  loadTracking();
  trackingTimer = window.setInterval(loadTracking, 15000);
}

function stopTracking() {
  isTracking = false;
  if (trackingTimer) window.clearInterval(trackingTimer);
  trackingTimer = null;
  els.trackingMeta.textContent = "Tracking stopped.";
  saveSettingsSoon();
}

async function loadFuturesMarkets() {
  try {
    const response = await fetch("/api/futures-markets", { cache: "no-store" });
    const data = await response.json();
    if (response.ok && data.ok) {
      availableCoins = data.coins || [];
      selectedCoins = new Set([...selectedCoins].filter((coin) => availableCoins.some((item) => item.coin === coin)));
      if (!selectedCoins.size) {
        selectedCoins = new Set(availableCoins.slice(0, 3).map((item) => item.coin));
      }
      renderCoinPicker();
    }
  } catch (error) {
    availableCoins = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE"].map((coin) => ({
      coin,
      pair: pairForCoin(coin),
      market: marketForCoin(coin),
    }));
    renderCoinPicker();
  }
}

function applyLiveQuote(quote) {
  if (!quote || !quote.ok) return;
  liveQuote = quote;
  const price = Number(quote.last_price);
  if (Number.isFinite(price)) {
    els.lastPrice.textContent = fmtPrice(price);
  }
  const bidAsk = [quote.bid, quote.ask].every((v) => Number.isFinite(Number(v)))
    ? `Bid ${fmtPrice(quote.bid)} · Ask ${fmtPrice(quote.ask)}`
    : "1s live stream";
  els.changePct.textContent = bidAsk;
  els.changePct.className = "neutral";
  setStatus("Live", "ok");

  if (!lastSnapshot || !Number.isFinite(price)) return;
  const bars = lastSnapshot.bars || [];
  const last = bars[bars.length - 1];
  if (!last) return;
  last.close = price;
  last.high = Math.max(Number(last.high) || price, price);
  last.low = Math.min(Number(last.low) || price, price);
  lastSnapshot.ticker = quote.ticker;
  drawPriceChart(lastSnapshot);
}

function render(snapshot) {
  lastSnapshot = snapshot;
  if (!snapshot.ok) {
    setStatus("No Data", "bad");
    els.subtitle.textContent = snapshot.message || "CoinDCX returned no data";
    return;
  }
  const stats = snapshot.stats || {};
  const action = snapshot.action || {};
  const state = snapshot.state || {};
  const strategy = snapshot.strategy || {};
  const indicators = snapshot.indicators || {};
  const change = Number(stats.change_pct);
  const freshness = Number(snapshot.freshness_minutes);
  if (Number.isFinite(freshness) && freshness > 30) {
    setStatus("Stale", "bad");
  } else {
    setStatus("Live", "ok");
  }
  els.subtitle.textContent = `${snapshot.pair} / ${snapshot.market} · ${snapshot.mode.toUpperCase()} · ${strategy.name || "Strategy"} · ${fmtNumber(snapshot.freshness_minutes, 1)} min freshness`;
  els.lastPrice.textContent = fmtPrice(liveQuote?.last_price ?? snapshot.ticker?.last_price ?? stats.close);
  if (liveQuote && liveQuote.pair === snapshot.pair && liveQuote.mode === snapshot.mode) {
    applyLiveQuote(liveQuote);
  } else {
    els.changePct.textContent = Number.isFinite(change) ? `${change >= 0 ? "+" : ""}${fmtNumber(change, 2)}% lookback` : "--";
    els.changePct.className = Number.isFinite(change) ? (change >= 0 ? "positive" : "negative") : "";
  }
  els.signalText.textContent = action.label || "--";
  els.signalMeta.textContent = `${action.type || "--"} · ${action.side || "--"}`;
  els.signalMeta.className = action.side === "LONG" ? "positive" : action.side === "SHORT" ? "negative" : "neutral";
  els.scoreText.textContent = Number.isFinite(Number(stats.score)) ? `${Number(stats.score) >= 0 ? "+" : ""}${fmtNumber(stats.score, 1)}` : "--";
  els.rsiText.textContent = `RSI ${fmtNumber(stats.rsi, 1)} · ${strategy.chart_label || "Line"} ${fmtPrice(stats.supertrend)}`;
  els.positionText.textContent = state.side || "--";
  els.positionMeta.textContent = `Trade #${state.trade_id ?? 0} · PnL $${fmtNumber(state.realized_pnl, 2)}`;
  els.feedText.textContent = `Feed: ${snapshot.used_pair || "--"} / ${snapshot.used_source || "--"} · Bars: ${stats.bars || 0}`;
  els.priceChartTitle.textContent = `Price + ${strategy.chart_label || "Strategy Line"}`;
  els.lineLegend.textContent = strategy.chart_label || "Strategy Line";
  els.scoreChartTitle.textContent = strategy.score_label || "Strategy Score";
  els.scoreChartText.textContent = strategy.description || "Signal score by selected strategy";
  renderStrategy(strategy, indicators);
  renderState(state);
  renderEvents(snapshot.events);
  drawPriceChart(snapshot);
  drawScoreChart(snapshot);
  drawRsiChart(snapshot);
}

async function loadSnapshot() {
  setStatus("Loading", "");
  const params = new URLSearchParams({
    pair: els.pair.value,
    market: els.market.value,
    strategy: els.strategy.value,
    mode: els.mode.value,
    risk: els.risk.value,
    reward_ratio: els.reward.value,
    leverage: els.leverage.value,
    lookback_days: els.lookback.value,
    limit: "260",
  });
  try {
    const response = await fetch(`/api/snapshot?${params.toString()}`, { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || response.statusText);
    render(data);
    connectLiveStream();
  } catch (error) {
    setStatus("Error", "bad");
    els.subtitle.textContent = error.message;
  }
}

function liveParams() {
  return new URLSearchParams({
    pair: els.pair.value,
    market: els.market.value,
    mode: els.mode.value,
    interval: "1",
  });
}

function connectLiveStream() {
  const nextKey = liveParams().toString();
  if (liveSource && liveSourceKey === nextKey && liveSource.readyState !== EventSource.CLOSED) {
    return;
  }
  if (liveSource) {
    liveSource.close();
    liveSource = null;
  }
  liveQuote = null;
  liveSourceKey = nextKey;
  liveSource = new EventSource(`/api/live?${nextKey}`);
  liveSource.addEventListener("quote", (event) => {
    try {
      applyLiveQuote(JSON.parse(event.data));
    } catch (error) {
      console.warn("Bad live quote", error);
    }
  });
  liveSource.onerror = () => {
    setStatus("Reconnecting", "");
  };
}

function scheduleRefresh() {
  if (refreshTimer) window.clearInterval(refreshTimer);
  refreshTimer = window.setInterval(loadSnapshot, 15000);
}

els.refresh.addEventListener("click", loadSnapshot);
els.refreshAccount.addEventListener("click", loadAccount);
els.applyCustomRisk.addEventListener("click", () => applyRiskAmount(els.customRisk.value));
els.customRisk.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    applyRiskAmount(els.customRisk.value);
  }
});
els.coinSearch.addEventListener("input", renderCoinPicker);
els.addCoin.addEventListener("click", addCustomCoin);
els.customCoin.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    addCustomCoin();
  }
});
els.startTracking.addEventListener("click", startTracking);
els.stopTracking.addEventListener("click", stopTracking);
[els.pair, els.market, els.strategy, els.mode, els.risk, els.reward, els.leverage, els.lookback].forEach((input) => {
  input.addEventListener("change", () => {
    if (liveSource) {
      liveSource.close();
      liveSource = null;
    }
    liveSourceKey = "";
    updateRiskText();
    saveSettingsSoon();
    loadSnapshot();
    if (isTracking) loadTracking();
  });
});

async function loadStrategies() {
  try {
    const response = await fetch("/api/strategies", { cache: "no-store" });
    const data = await response.json();
    if (!response.ok || !data.ok) return;
    const current = els.strategy.value;
    els.strategy.innerHTML = data.strategies
      .map((strategy) => `<option value="${strategy.id}">${strategy.name}</option>`)
      .join("");
    els.strategy.value = data.strategies.some((strategy) => strategy.id === current) ? current : "confluence";
  } catch (error) {
    console.warn("Could not load strategies", error);
  }
}

window.addEventListener("resize", () => {
  if (lastSnapshot) render(lastSnapshot);
});

Promise.all([loadStrategies(), loadSettings(), loadFuturesMarkets()]).then(() => {
  updateRiskText();
  loadAccount();
  loadSnapshot();
  startTracking();
});
scheduleRefresh();
