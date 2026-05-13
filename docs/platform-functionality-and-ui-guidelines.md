# CoinDCX Bot Platform: Functionality And UI Guidelines

This document describes the Trading-Bot platform, its core functionality, major feature areas, backend capabilities, data model, and UI guidelines. Use it as a product reference and design brief when creating or improving detailed screens for the platform.

## 1. Platform Summary

The platform is a CoinDCX-focused crypto trading dashboard and bot system. It combines live market data, strategy analysis, paper trading, optional live execution, account monitoring, multi-coin futures tracking, volatility discovery, expert signal overlays, smart-money intelligence, and performance reporting.

The platform has four main surfaces:

- **Dashboard frontend**: static single-page UI in `frontend/index.html`, `frontend/app.js`, and `frontend/styles.css`.
- **FastAPI backend**: API and WebSocket server in `backend/main.py`.
- **Trading engine**: CoinDCX data, indicators, paper/live execution logic in `Crypto/live_confluence_monitor.py`.
- **Strategy library**: reusable strategy modules in `strategies/`.

The system is paper-first. Live orders require explicit credentials and execution flags.

## 2. Primary User Goals

Users of the platform need to:

- Monitor CoinDCX spot/futures markets in near real time.
- Run technical strategies against closed candle data.
- Compare strategy signals across one or many futures coins.
- Track paper-trade entries, exits, stop-loss, target, and PnL.
- Inspect CoinDCX wallet balance and active futures positions.
- Discover high-volatility low-priced futures coins.
- Cross-check active positions against external expert signals.
- Review historical trades, performance metrics, and reports.
- Clear stored history when starting a fresh test cycle.
- Configure dashboard preferences that persist across sessions.

## 3. High-Level Architecture

### Frontend

The frontend is a static dashboard served by FastAPI. It uses:

- Native HTML/CSS/JavaScript.
- Lightweight Charts for price/indicator charts.
- REST APIs for snapshots, reports, account data, intelligence, and scanners.
- WebSocket quotes for live last price, bid, and ask updates.
- Local storage and backend settings for preferences.

### Backend

The backend is a FastAPI app that:

- Serves the frontend.
- Exposes REST APIs under `/api/*`.
- Exposes WebSocket quotes under `/ws/quotes`.
- Initializes SQLite tables.
- Runs APScheduler jobs for candle storage, background tracking, and daily reports.
- Loads the CoinDCX bot engine through `backend/bot_loader.py`.

### Trading Engine

The trading engine:

- Fetches CoinDCX public candles, trades, tickers, and market metadata.
- Signs private CoinDCX requests for account/order APIs.
- Builds technical indicator frames.
- Manages paper trade state and optional live spot, margin, or futures execution.
- Applies risk sizing, stop-loss, target, trailing stop, and TP/SL logic.

### Database

SQLite database: `tradingbot.db`

Main tables:

- `trades`: completed paper/real trades.
- `signal_events`: snapshot and tracker signal history.
- `candles_5m`: stored candle history for tracked pairs.
- `account_snapshots`: wallet/account snapshots.
- `position_snapshots`: positions attached to account snapshots.
- `snapshots`: generic state snapshot payloads.
- `user_settings`: persisted dashboard preferences.

## 4. Main Dashboard Features

### 4.1 Top Controls

The top control area lets users select:

- Pair, for example `B-ETH_USDT`.
- Market, for example `ETHUSDT`.
- Risk amount.
- Secondary pair/market for pair-stat-arb strategy.
- Strategy.
- Mode: spot, margin, or futures.
- Timeframe. Currently the runtime is oriented around `15m`.
- Lookback window.
- Execution intent: paper or real.

UI guidelines:

- Keep controls compact and scannable.
- Use clear labels and stable input widths.
- Keep dangerous execution intent visually distinct from normal paper mode.
- Any real-order action should require explicit confirmation and should never look like a passive filter.

### 4.2 Live Snapshot

The snapshot view fetches the selected pair and strategy from `/api/snapshot`.

It displays:

- Latest price and percent change.
- Current strategy signal/action.
- Score and RSI.
- Current paper position state.
- Price chart with candles and overlays.
- Strategy score chart.
- RSI chart.
- Strategy description, reason, notes, and recent events.

The backend builds this by:

- Fetching closed candles from CoinDCX.
- Fetching spot/futures ticker data when needed.
- Running the selected strategy through `strategies.registry`.
- Returning bars, indicators, action, state, and replayed paper events.

UI guidelines:

- Show the selected coin, strategy, market mode, and timeframe near the chart title.
- Separate live quote status from closed-candle strategy status.
- Explain stale data through a small feed/status line rather than a modal.
- Use directional colors consistently: green for long/profit, red for short/loss, neutral blue for wait/hold.

### 4.3 WebSocket Quote Stream

Endpoint: `/ws/quotes`

Streams:

- Pair.
- Market.
- Mode.
- Coin.
- Server time.
- Last price.
- Bid.
- Ask.

UI guidelines:

- Show connection state in the top bar.
- Use a small dot and short label: Connecting, Live, Stale, Offline.
- Do not mix WebSocket price with strategy candle close without labeling the difference.

### 4.4 Price Chart

The price chart shows:

- OHLC candles.
- Strategy overlay such as Supertrend, Bollinger mid, or reference price.
- Stop and target lines when a position exists.
- Long/short entry markers where available.

UI guidelines:

- Keep the chart full-width inside its work area.
- Avoid decorative chart containers that reduce usable plot area.
- Use stable chart height.
- Keep legends concise.
- Stop and target lines should use different colors and labels.

### 4.5 Score And RSI Charts

The lower charts show:

- Strategy score or scanner metric.
- RSI values.

UI guidelines:

- Keep score and RSI panels aligned.
- Label the score according to the selected strategy metadata.
- Use threshold cues when helpful, but avoid overloading the chart with too many lines.

## 5. Strategy System

Strategy files live in `strategies/`. The shared contract is in `strategies/base.py`.

Every strategy returns:

- `meta`: id, name, description, chart label, score label.
- `frame`: indicator-enhanced candle frame.
- `state`: replayed paper state.
- `events`: replayed entries, exits, trails, stops, targets.
- `action`: latest action such as WAIT, ENTRY, EXIT, HOLD, SCAN, IGNORE.
- `reason`: current strategy reason.
- `indicators`: latest key indicator values.
- `notes`: optional warnings or extra context.

### 5.1 Confluence

ID: `confluence`

Combines:

- Supertrend direction.
- EMA 9/21 with RSI filter.
- MACD histogram.
- Bollinger mid/outer band position.
- RSI zone.

Behavior:

- Score >= +3 enters long.
- Score <= -3 enters short.
- Weaker score exits.

UI guidance:

- Show score as the primary signal.
- Display component indicators only when space allows.
- Good default strategy for the dashboard.

### 5.2 Trend Following

ID: `trend_following`

Uses:

- EMA 9/21.
- EMA 55 trend filter.
- Supertrend.
- RSI alignment.

Behavior:

- Enters when trend direction and momentum align.
- Exits when EMA, Supertrend, or RSI breaks.

UI guidance:

- Emphasize directional momentum and trend validity.
- Useful labels: Strong Bull, Strong Bear, Trend Weakening.

### 5.3 Mean Reversion

ID: `mean_reversion`

Uses:

- Bollinger Bands.
- RSI extremes.
- Mean reversion to Bollinger mid.

Behavior:

- Long when price is below lower band and RSI is oversold.
- Short when price is above upper band and RSI is overbought.
- Exits near mean or when RSI normalizes.

UI guidance:

- Show band position clearly.
- Avoid using trend-following language for this strategy.
- Use "stretched", "reverting", and "mean" terminology.

### 5.4 Volatility Squeeze

ID: `volatility_squeeze`

Uses:

- Bollinger Band width.
- Recent squeeze condition.
- RSI.
- Breakout beyond bands.

Behavior:

- Requires sufficient warmup bars.
- Enters directional breakout after low-volatility compression.
- Exits on mean reversion or RSI weakness.

UI guidance:

- Show warmup state if bars are insufficient.
- Surface squeeze state separately from breakout signal.

### 5.5 Mixed Consensus

ID: `mixed_consensus`

Meta-strategy using child strategies:

- Confluence.
- Trend Following.
- Mean Reversion.
- Volatility Squeeze.

Behavior:

- Enters when at least two directional strategies agree.
- Exits when consensus drops below threshold.

UI guidance:

- Show long votes and short votes.
- Use a vote breakdown panel.
- Avoid hiding child strategy errors; display them as notes.

### 5.6 Arbitrage Scanner

ID: `arbitrage`

Compares CoinDCX spot and futures quotes.

Behavior:

- Scanner-only.
- Returns spread in basis points.
- Flags large spot/futures deviations.
- Does not place orders.

UI guidance:

- Label as "scanner", not "trade signal".
- Show spread bps, spot price, futures price, and caveats about fees/slippage.

### 5.7 Funding / Basis Scanner

ID: `funding_basis`

Compares:

- Spot price.
- Futures price.
- Funding rate when available.

Behavior:

- Scanner-only.
- Flags basis deviation.

UI guidance:

- Use basis bps as primary metric.
- Show funding rate as contextual support, not a direct order trigger.

### 5.8 Pairs Stat-Arb Scanner

ID: `pairs_stat_arb`

Compares two assets using:

- Aligned close prices.
- Log price ratio.
- Rolling z-score.

Behavior:

- Scanner-only.
- Requires secondary pair data.
- Flags stretched pair ratio.

UI guidance:

- Show pair 1, pair 2, z-score, and threshold.
- Expose pair2 input only when this strategy is selected.

## 6. Futures Tracker

Endpoint: `/api/track`

The tracker lets users:

- Select multiple futures coins.
- Fetch each coin's latest strategy snapshot.
- Compare price, signal, score, RSI, position, PnL, and freshness.
- Click a coin row or menu item to load it into the main chart.

The tracker uses:

- Coin catalog from `/api/futures-markets`.
- Coin-to-pair helpers, for example `BTC -> B-BTC_USDT`.
- Background settings persisted in `user_settings`.

UI guidelines:

- Tracker rows should be dense and sortable-feeling even if not actually sorted.
- Use signal labels with consistent colors.
- Show freshness because stale data can invalidate a signal.
- Limit selected coins to a manageable number. The current UI caps at 12.

## 7. Wallet And Risk Panel

Endpoint: `/api/account`

Displays:

- Whether CoinDCX API credentials are configured.
- Available quote balance.
- Futures wallets.
- Active futures positions.
- Position side, quantity, average price, mark price, liquidation price, TP/SL, unrealized PnL, margin, and leverage.
- Risk suggestions based on wallet balance: 0.25%, 0.5%, 1%, 2%.

UI guidelines:

- Read-only account information must look distinct from trade controls.
- Risk suggestion buttons should update the risk input clearly.
- If credentials are missing, show a useful empty state, not an error.
- Highlight unrealized PnL with profit/loss color.

## 8. Trade Journal

Endpoint: `/api/trades`

Shows recent completed trades:

- Pair.
- Side.
- Entry/exit timestamp.
- Entry/exit price.
- Quantity.
- PnL.
- Exit reason.

UI guidelines:

- Keep the journal compact.
- Show recent trades in reverse chronological order.
- Avoid too many columns in the small drawer; deeper reporting belongs in Reports.

## 9. Reports

Endpoints:

- `/api/reports/trades`
- `/api/reports/overview`
- `/api/reports/pnl-series`
- `/api/reports/send-email`
- `/api/reports/clear-history`

Reports features:

- Filter by all, paper, or real execution mode.
- Show total/closed/open trades.
- Show win rate, net PnL, profit factor, max drawdown, and average duration.
- Show detailed trade rows.
- Send email report through Brevo.
- Clear stored history after typing `CLEAR HISTORY`.

Clear history deletes:

- Trades.
- Signal events.
- Stored candles.
- Account snapshots.
- Position snapshots.
- Generic snapshots.

Clear history keeps:

- Dashboard settings.
- Local environment configuration.
- Strategy code.

UI guidelines:

- Reports should feel like a utility panel, not a marketing screen.
- Destructive actions should be visually separated and require typed confirmation.
- Use clear table headers: Market means spot/margin/futures; Exec means paper/real.
- Keep the table horizontally scrollable with sticky headers.
- Do not hide important numeric fields such as stop, target, risk, and PnL.

## 10. Volatility Scanner

Endpoint: `/api/volatility-scan`

Purpose:

- Finds high-volatility CoinDCX futures coins priced at or below a maximum price, default `$10`.

Metrics:

- Price.
- ATR%.
- Full-period range%.
- Standard deviation of returns.
- Average volume in USD.
- Period change%.
- Rank by ATR%.

UI guidelines:

- Show ranked list.
- Use heat or intensity based on ATR%.
- Make rows clickable to load that coin into the main dashboard.
- Include scan metadata: count, total scanned, max price, elapsed seconds.
- Avoid implying that volatility alone is a buy signal.

## 11. Expert Picks

Endpoints:

- `/api/expert-picks/signals`
- `/api/expert-picks/sentiment`
- `/api/expert-picks/recommendations`

Sources:

- MyCryptoSignal API.
- Alternative.me Fear & Greed Index.
- CoinDCX active futures positions when credentials exist.

Features:

- Shows market sentiment.
- Shows top external signals.
- Cross-references active positions with external signals.
- Produces recommendations such as HOLD, CLOSE_LONG, CLOSE_SHORT, WATCH.

UI guidelines:

- Clearly attribute MyCryptoSignal.
- Separate "your positions" from "top market signals".
- Show confidence values where available.
- Recommendations should include reason text.
- Treat external signals as advisory, not automated order triggers.

## 12. Smart-Money Intelligence

Endpoints:

- `/api/intelligence/signals`
- `/api/intelligence/rankings`
- `/api/intelligence/trending`
- `/api/intelligence/social`

Source:

- Binance Web3 public endpoints.

Features:

- Smart-money signal table.
- Smart-money inflow rankings.
- Trending tokens.
- Social hype and sentiment.

UI guidelines:

- Use compact tables.
- Show chain selector.
- Display status and timestamps.
- Treat empty data as normal because upstream requests can fail or return no rows.
- Avoid mixing this data with CoinDCX strategy signals without a clear label.

## 13. Background Jobs

The backend starts APScheduler jobs:

- Candle aggregation every 5 minutes.
- Daily report email at 20:00 IST when `REPORT_EMAIL_TO` is configured.
- Background tracker scan when enabled.

Background tracker:

- Reads saved dashboard settings.
- Scans selected coins when tracking is active.
- Stores signal events.
- Can execute live orders only when settings and environment allow it.

UI guidelines:

- Make background tracking state visible.
- Persist user choices intentionally.
- Show whether execution mode is paper or real.
- Avoid silent real-order behavior.

## 14. Execution And Risk

Execution modes:

- **Paper**: no exchange orders.
- **Real**: order intent is enabled, but still depends on environment flags and credentials.

Market modes:

- **Spot**: long-only live behavior.
- **Margin**: supports short behavior where available.
- **Futures**: supports long/short futures positions and TP/SL management.

Risk behavior:

- Fixed risk dollars by default.
- Optional wallet percentage risk through environment variables.
- Quantity is derived from distance between entry and stop.
- Target defaults to risk-reward logic.
- Structure support/resistance can influence stops and targets.

UI guidelines:

- Always distinguish execution mode from market mode.
- Show risk amount, stop, target, quantity, and notional when available.
- For live mode, require explicit confirmation and visual warning.
- Never use ambiguous labels such as "Mode" alone when both market and execution mode exist.

## 15. Settings And Persistence

Endpoint:

- `/api/settings`

Settings can include:

- Selected coins.
- Tracking active state.
- Selected strategy.
- Risk.
- Lookback.
- Timeframe.
- Theme.
- Collapsed tracker panel.
- Execution preference.

UI guidelines:

- Persist settings quietly after user changes.
- Keep settings payload small.
- Do not store credentials in dashboard settings.
- Credentials belong in `.env`.

## 16. Themes

Available themes:

- Dark.
- Light.
- Contrast.

UI guidelines:

- Use CSS variables.
- Keep semantic colors consistent across themes.
- Test danger controls, PnL colors, and chart colors in all themes.
- Do not rely on color alone; use labels and icons where possible.

## 17. API Reference

Core:

- `GET /api/health`: service state and runtime flags.
- `GET /api/snapshot`: detailed strategy snapshot for one pair.
- `GET /api/strategies`: available strategies.
- `GET /api/futures-markets`: CoinDCX futures catalog.
- `GET /api/markets`: market search.
- `GET /api/track`: multi-coin futures tracker.
- `GET /api/trades`: recent trades.
- `GET /ws/quotes`: live quote WebSocket.

Account:

- `GET /api/account`: wallet, risk suggestions, and futures positions.
- `GET /api/account-snapshots`: stored account snapshots.

History:

- `GET /api/signals`: stored signal events.

Reports:

- `GET /api/reports/trades`: paginated trade history.
- `GET /api/reports/overview`: performance summary.
- `GET /api/reports/pnl-series`: daily/cumulative PnL series.
- `POST /api/reports/send-email`: send PDF report.
- `POST /api/reports/clear-history`: clear stored history with typed confirmation.

Scanners and intelligence:

- `GET /api/volatility-scan`: volatility-ranked futures coins.
- `GET /api/expert-picks/signals`: external signals.
- `GET /api/expert-picks/sentiment`: Fear & Greed.
- `GET /api/expert-picks/recommendations`: position-aware recommendations.
- `GET /api/intelligence/signals`: smart-money signals.
- `GET /api/intelligence/rankings`: inflow ranking.
- `GET /api/intelligence/trending`: trending tokens.
- `GET /api/intelligence/social`: social hype.

## 18. Data Display Guidelines

### Prices

- Use enough decimals for low-priced coins.
- Avoid fixed `2` decimals for crypto prices.
- Trim trailing zeros when possible.

### Percentages

- Show sign for change values.
- Use 2 decimals for most percentages.
- Use more precision only for funding rates or very small values.

### Timestamps

- Show local readable time in UI.
- Backend can return ISO strings.
- Label candle close time vs server time clearly.

### PnL

- Positive: green and plus sign.
- Negative: red and minus sign.
- Zero: neutral.

### Empty States

Use useful empty states:

- "No trades recorded yet."
- "Connect CoinDCX API keys to show active positions."
- "Waiting for warmup bars."
- "No active signals."
- "Scanner could not build complete quote."

## 19. Layout Guidelines

The platform is operational software. It should feel:

- Dense.
- Calm.
- Scannable.
- Fast.
- Reliable.

Avoid:

- Marketing-style hero sections.
- Large decorative cards.
- Excessively rounded components.
- Vague labels.
- Hidden destructive actions.
- One-hue visual themes.

Prefer:

- Compact panels.
- Clear tables.
- Sticky table headers where useful.
- Segmented controls for filters.
- Buttons only for actions.
- Read-only metrics separated from action controls.

## 20. Component Guidelines

### Buttons

- Primary action: blue/neutral fill.
- Secondary action: bordered/ghost.
- Destructive action: red border or red fill.
- Disabled: reduced opacity and no pointer emphasis.

### Segmented Controls

Use for:

- All/Paper/Real filters.
- Theme or mode choices if compact.

### Tables

Use for:

- Tracker rows.
- Trade logs.
- Positions.
- Smart-money data.
- Volatility scan.

Table rules:

- Use compact row height.
- Keep numeric values monospace where possible.
- Align related columns.
- Provide horizontal scroll on small screens.

### Status Messages

Use inline status messages for:

- Clear history confirmation.
- Failed scanner loads.
- Email send result.
- Credential missing states.

Use toast messages for:

- Short success/failure feedback.
- Actions that complete asynchronously.

## 21. Safety Guidelines

The platform can touch real trading accounts. UI must protect users from mistakes.

Rules:

- Real-order intent must never be hidden.
- Destructive database actions require typed confirmation.
- Live order placement requires credentials and environment flags.
- Shorts must be disabled unless market mode supports them.
- Clear history must not clear credentials or dashboard settings.
- Stale data should block or warn against trading decisions.

## 22. Suggested Future UI Improvements

High-value improvements:

- Add a dedicated "Execution Safety" panel.
- Add an audit log for clear-history and live-order toggles.
- Add pagination to Reports.
- Add sortable columns to tracker and reports.
- Add side-by-side strategy comparison for one coin.
- Add strategy component score breakdown for Confluence.
- Add a proper chart for report PnL series.
- Add explicit data freshness badges.
- Add "last background scan time" to tracker.
- Add import/export for dashboard settings.

## 23. Glossary

- **Pair**: CoinDCX pair identifier, for example `B-ETH_USDT`.
- **Market**: order/ticker market symbol, for example `ETHUSDT`.
- **Market mode**: spot, margin, or futures.
- **Execution mode**: paper or real.
- **Signal**: latest strategy/scanner action label.
- **Score**: numeric strategy strength or scanner metric.
- **Freshness**: age of latest candle data.
- **Paper trade**: simulated trade generated by replay/strategy logic.
- **Real trade**: trade associated with live order placement.
- **Tracker**: multi-coin futures scan table.
- **Snapshot**: detailed single-pair analysis result.

