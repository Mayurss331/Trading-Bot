# Custom Strategy Backtesting Module Plan

## Goal

Add a first-class backtesting module that lets us run any strategy script against the same candle data shape already used by the dashboard and live monitor.

The module should support:

- Built-in strategies from `strategies/registry.py`.
- User-provided Python strategy scripts.
- CoinDCX spot/futures candle data using the current fetch path.
- Future CSV/imported data as long as it can be normalized to the same OHLCV format.
- Repeatable result artifacts: summary metrics, equity curve, trades, events, and chart-ready bar annotations.

## Current Logic Summary

### Existing Data Shape

The live/dashboard path already standardizes candles as a `pandas.DataFrame` with:

- Index: timestamp, currently converted to IST in `Crypto/live_confluence_monitor.py`.
- Required columns: `Open`, `High`, `Low`, `Close`, `Volume`.

This is produced by:

- `bot.fetch_closed_bars(...)` in `Crypto/live_confluence_monitor.py`.
- Spot candles from `/market_data/candles`.
- Futures candles from `/market_data/candlesticks`.
- Trade-derived fallback candles from trade history.
- `backend/tasks/candle_store.py`, which persists normalized candles into `candles_5m` as lowercase DB columns.

The new backtest module should treat this OHLCV frame as the canonical input contract.

### Existing Strategy Shape

The dashboard strategies live under `strategies/` and expose:

```python
def analyze(bars: pd.DataFrame, ctx: StrategyContext) -> dict:
    ...
```

The standard result contains:

- `meta`: `StrategyMeta`.
- `frame`: input bars plus indicators and signal columns.
- `state`: latest paper state.
- `events`: recent replay events.
- `action`: latest action.
- `indicators`: latest indicator values.

The reusable signal columns are:

- `entry_side`: `1` long, `-1` short, `0` flat.
- `exit_long`: boolean.
- `exit_short`: boolean.
- Optional risk columns: `stop_px`, `tp1_px`, `tp2_px`, `tp1_frac`.
- Optional chart/diagnostic columns: `score`, `reason`, `rsi`, `st_line`, `ema_fast`, `ema_slow`, `bb_*`, strategy-specific columns.

The current `strategies/base.py::replay_strategy` is useful for latest-state paper replay, but it is intentionally small:

- It only returns final paper state and last 80 events.
- It does not produce full trade ledger, equity curve, drawdown, Sharpe, win rate, or reusable results.
- It always sizes trades from per-trade risk dollars rather than a portfolio-level account model.

### Existing UI/API Shape

Current dashboard integration is centered on:

- `GET /api/snapshot`: builds one strategy snapshot and stores signal events.
- `GET /api/track`: scans multiple futures coins.
- `GET /api/strategies`: lists built-in strategy metadata.
- `GET /api/trades` and `/api/reports/*`: read stored live/paper trade history.

The frontend already knows how to render chart-ready bars, strategy details, state, and events. The backtest module should return a similar `bars` payload plus richer backtest artifacts.

## External Research Notes

These are the design points from common Python backtesting tools and pandas docs that are relevant to this repo:

- `backtesting.py` requires a `DataFrame` with `Open`, `High`, `Low`, `Close`, and optional `Volume`, and it exposes explicit assumptions for cash, spread, commission, margin, current-close fills, hedging, exclusive orders, and final trade handling. This closely matches the data contract we already have and confirms that those broker settings should be explicit inputs. Reference: <https://kernc.github.io/backtesting.py/doc/backtesting/backtesting.html>
- Backtrader uses a central orchestrator (`Cerebro`) that combines data feeds, strategies, observers, analyzers, writers, and execution. This supports designing our module as separate layers: data provider, strategy adapter, execution engine, analyzer, persistence, and presentation. Reference: <https://www.backtrader.com/docu/cerebro/>
- Backtrader strategies are event-driven through `next`, with access to data feeds, broker, position, orders, trades, observers, and analyzers. That reinforces the need for row-by-row execution semantics, not just vectorized final returns. Reference: <https://www.backtrader.com/docu/strategy/>
- pandas rolling windows look backward from the current observation, and the repo already uses `shift(1)` in older scripts to prevent trading on information from the same decision bar. The new module should document and enforce a fill model so strategy authors understand whether signals generated on bar `t` fill on bar `t` close or bar `t+1` open. Reference: <https://pandas.pydata.org/pandas-docs/dev/user_guide/window.html>

Recommendation: start with an internal engine because the repo already has strategy contracts and CoinDCX-specific data fetchers. Keep the design compatible with future adapters to `backtesting.py` or `backtrader` if we later need optimization, multi-asset portfolio simulation, or complex broker modeling.

## Requirements

### Functional Requirements

1. Run built-in strategies over historical candles.
2. Run user-provided Python strategy code stored in the database with a title.
3. Accept the same market inputs as `/api/snapshot`: `pair`, `market`, `mode`, `timeframe`, `lookback_days`, `strategy`, `risk`.
4. Add backtest-specific inputs:
   - `initial_capital`
   - `commission_bps`
   - `spread_bps`
   - `slippage_bps`
   - `leverage` as max allowed leverage; the engine computes used leverage per trade and skips oversized signals.
   - `allow_shorts`
   - `fill_model`: `next_open` or `close`
   - `position_sizing`: `risk_fixed`, `cash_fraction`, or `fixed_qty`
   - `finalize_open_trade`
5. Produce a full trade ledger:
   - entry/exit timestamps
   - side
   - entry/exit prices
   - quantity
   - gross PnL
   - fees
   - net PnL
   - return %
   - R multiple when risk is available
   - exit reason: `STOP`, `TARGET`, `TP1`, `SIGNAL`, `FINAL`, `LIQUIDATION`
6. Produce portfolio time series:
   - equity
   - cash
   - open position value
   - drawdown
   - exposure
7. Produce summary metrics:
   - final equity
   - total return
   - CAGR when period is long enough
   - volatility
   - Sharpe
   - Sortino
   - max drawdown
   - Calmar
   - win rate
   - trade count
   - average win/loss
   - profit factor
   - average duration
   - largest win/loss
8. Return chart-ready bars using the existing `bars_payload(...)` style, extended with equity/drawdown markers.
9. Persist backtest runs and allow loading previous runs.
10. Keep all live order placement disabled; backtesting must never call order APIs.

### Strategy Script Requirements

Custom strategy scripts should support a minimal, safe contract:

```python
import pandas as pd
from strategies.base import StrategyContext, StrategyMeta, base_frame, finalize

META = StrategyMeta(
    id="my_strategy",
    name="My Strategy",
    description="Short description.",
    chart_label="Main line",
    score_label="Score",
)

def analyze(bars: pd.DataFrame, ctx: StrategyContext) -> dict:
    frame = base_frame(bars)
    frame["entry_side"] = 0
    frame["exit_long"] = False
    frame["exit_short"] = False
    frame["score"] = 0
    frame["reason"] = "..."
    return finalize(META, frame, ctx)
```

For the backtest module, `finalize(...)` is not strictly required if the script can return a compatible `frame`; however, using the existing contract keeps dashboard and backtest behavior aligned.

### Non-Functional Requirements

- Deterministic results for the same input data and settings.
- No network dependency when running against stored candles or uploaded CSV.
- Input validation for DB-stored custom strategy code, strategy IDs, dates, and numeric settings.
- Clear separation between paper/live bot state and historical backtest state.
- Good error messages for missing columns, empty data, invalid custom strategy code, and insufficient warmup bars.
- Performance acceptable for at least 10,000 candles and 20 strategies per request.

## Proposed Architecture

```text
backend/
  backtesting/
    __init__.py
    config.py          # BacktestConfig dataclass + validation
    data.py            # fetch/load/normalize OHLCV data
    strategy_loader.py # built-in and DB-stored custom strategy loading
    engine.py          # row-by-row broker/execution simulation
    metrics.py         # performance metrics
    schemas.py         # API response models / typed payloads
    persistence.py     # DB storage for runs/trades/equity
    service.py         # orchestrates data -> strategy -> engine -> metrics

  routers/
    backtests.py       # HTTP endpoints
```

### Core Flow

1. API receives a backtest request.
2. `data.py` loads candles:
   - First path: current CoinDCX `fetch_closed_bars(...)`.
   - Later path: DB candles from `candles_5m`.
   - Later path: CSV/upload/import.
3. `strategy_loader.py` resolves either:
   - built-in `strategies.registry.get_strategy(...)`, or
   - custom strategy code loaded from the database by `custom_strategy_id` or slug.
4. Strategy returns a signal frame.
5. `engine.py` iterates candles chronologically and simulates entries/exits with explicit cost/fill assumptions.
6. `metrics.py` computes summary from equity and trades.
7. `persistence.py` stores run config, summary, trades, and sampled equity.
8. API returns a compact result for the UI and a `run_id` for detailed reload/export.

## Execution Engine Design

### Fill Model

Use `next_open` as the default to reduce look-ahead risk:

- Signal generated on bar `t`.
- Entry/exit market fill happens at bar `t+1` open.
- Stop/target checks use the high/low of the active bar after the position exists.

Allow `close` for comparison:

- Signal generated on bar `t`.
- Fill at bar `t` close.
- Mark result clearly as a close-fill simulation.

### Costs

For each fill:

```text
effective_buy_price  = raw_price * (1 + spread/2 + slippage)
effective_sell_price = raw_price * (1 - spread/2 - slippage)
commission           = abs(notional) * commission_rate
```

Keep these values in the trade ledger so results are auditable.

### Position Sizing

Start with `risk_fixed`, because it maps to the current dashboard mental model:

```text
risk_per_unit = abs(entry_px - stop_px)
qty = risk_amount / risk_per_unit
```

Risk amount can be:

- `fixed_amount`: risk input is dollars per trade.
- `percent_equity`: risk input is a percentage of current equity, so risk shrinks after losses and grows after wins.

Fallback when no valid stop exists:

```text
synthetic_stop_gap = max(entry_px * 0.001, atr, 1e-6)
```

Add later:

- `cash_fraction`: use a percentage of equity per trade.
- `fixed_qty`: fixed base units.

### Stop/Target Priority

When both stop and target are touched in the same candle, choose a conservative deterministic rule:

- Long: assume stop first unless `same_bar_priority=target` is explicitly set.
- Short: assume stop first unless explicitly overridden.

This avoids optimistic results on OHLC-only data.

### Opposite Signals

When an opposite entry appears while a position is active, the engine must use an explicit rule:

- `ignore`: keep the current position unless SL/TP or exit flag triggers.
- `exit_only`: close the current position, but do not enter the opposite side.
- `reverse`: close the current position and open the opposite side if leverage/cash checks pass.

### Leverage Guard

The leverage input is max allowed leverage. The engine computes used leverage per trade:

```text
notional = abs(entry_px * qty)
required_leverage = notional / current_equity
```

If `required_leverage > max_leverage`, the signal is skipped and logged.

### Guardrails

- For `next_open`, entry sizing and stop/target are based on the original signal candle, not the next candle's indicators.
- Stop/target checks happen before trailing-stop updates on each candle.
- Invalid strategy-line stop anchors are corrected through bracket derivation instead of trusted blindly.
- If a next-open fill has already crossed an explicit planned stop, the entry signal is skipped as stale.
- Gap-through-stop and gap-through-target exits fill at the candle open instead of the planned level.
- Equity is re-marked after intrabar exits so the curve reflects the final bar state.
- Custom strategy validation warns on common look-ahead risks such as negative shift, centered rolling windows, backward fill, and resampling.

## API Design

### `GET /api/backtests/strategies`

Returns built-in and DB-stored custom strategy metadata.

Custom strategy rows should include:

- `id`
- `title`
- `slug`
- `description`
- `version`
- `enabled`
- `created_at`
- `updated_at`

### `POST /api/backtests/custom-strategies`

Creates a DB-stored custom strategy.

Request:

```json
{
  "title": "EMA Pullback Strategy",
  "slug": "ema_pullback",
  "description": "EMA trend filter with pullback entries.",
  "code": "from strategies.base import ...",
  "enabled": true
}
```

Response:

```json
{
  "ok": true,
  "strategy": {
    "id": 12,
    "title": "EMA Pullback Strategy",
    "slug": "ema_pullback",
    "version": 1,
    "enabled": true
  }
}
```

### `PUT /api/backtests/custom-strategies/{strategy_id}`

Updates title, description, enabled state, or code. Code changes should create a new version number so old backtest runs remain traceable.

### `GET /api/backtests/custom-strategies/{strategy_id}`

Returns the custom strategy metadata and code for editing.

### `POST /api/backtests/custom-strategies/{strategy_id}/validate`

Compiles and validates the stored code without running a full backtest.

Validation checks:

- Code compiles.
- `META` exists or can be generated from the DB title/slug.
- `analyze(bars, ctx)` exists.
- Returned frame has required signal columns or can be normalized.
- No blocked imports are detected by static pre-checks.

### `POST /api/backtests/run`

Request:

```json
{
  "pair": "B-ETH_USDT",
  "market": "ETHUSDT",
  "mode": "futures",
  "strategy": "confluence",
  "custom_strategy_id": null,
  "timeframe": "15m",
  "lookback_days": 30,
  "initial_capital": 10000,
  "risk": 10,
  "commission_bps": 5,
  "spread_bps": 2,
  "slippage_bps": 1,
  "leverage": 1,
  "allow_shorts": true,
  "fill_model": "next_open",
  "position_sizing": "risk_fixed",
  "finalize_open_trade": true
}
```

Response:

```json
{
  "ok": true,
  "run_id": 123,
  "strategy": {"id": "confluence", "name": "Confluence"},
  "data": {"pair": "B-ETH_USDT", "timeframe": "15m", "bars": 2880},
  "summary": {
    "final_equity": 10342.5,
    "total_return_pct": 3.42,
    "max_drawdown_pct": -4.8,
    "sharpe": 1.12,
    "win_rate_pct": 52.4,
    "trades": 21,
    "profit_factor": 1.31
  },
  "equity": [],
  "trades": [],
  "events": [],
  "bars": []
}
```

### `GET /api/backtests/{run_id}`

Loads a saved run.

### `GET /api/backtests/{run_id}/trades.csv`

Exports trade ledger.

### `GET /api/backtests/{run_id}/equity.csv`

Exports equity curve.

## Database Design

Add custom-strategy storage plus the backtest result tables:

### `custom_strategies`

- `id`
- `title`
- `slug`
- `description`
- `code`
- `version`
- `enabled`
- `created_at`
- `updated_at`
- `last_validated_at`
- `validation_status`
- `validation_message`

`title` is the user-facing name shown in the strategy selector. `slug` is the stable machine-readable ID used by API requests and internal loading.

### `backtest_runs`

- `id`
- `created_at`
- `pair`
- `market`
- `mode`
- `timeframe`
- `strategy`
- `custom_strategy_id`
- `custom_strategy_title`
- `custom_strategy_version`
- `custom_strategy_code_snapshot`
- `data_source`
- `start_ts`
- `end_ts`
- `bars_count`
- `config` JSON
- `summary` JSON

### `backtest_trades`

- `id`
- `run_id`
- `side`
- `entry_ts`
- `exit_ts`
- `entry_px`
- `exit_px`
- `qty`
- `gross_pnl`
- `fees`
- `net_pnl`
- `return_pct`
- `r_multiple`
- `exit_reason`
- `payload` JSON

### `backtest_equity_points`

- `id`
- `run_id`
- `ts`
- `equity`
- `cash`
- `position_value`
- `drawdown_pct`

Store full detail for correctness; API can sample points for chart performance.

## Frontend Design

Add a Backtest view reachable from the existing side actions.

### Controls

- Pair/market/mode/timeframe controls reused from dashboard.
- Strategy select including custom strategies.
- Custom strategy manager with title, slug, description, code, validation status, save, validate, enable/disable.
- Initial capital input.
- Risk input.
- Commission/spread/slippage inputs in bps.
- Max leverage input.
- Risk mode select: fixed dollars or percent of equity.
- Opposite signal mode: ignore, exit only, or reverse.
- Same-bar priority: stop first or target first.
- Allow shorts toggle.
- Fill model segmented control: `Next open`, `Close`.
- Run button.

### Result Surface

Use dense, operational UI rather than a marketing-style page:

- Summary metric strip: final equity, return, max drawdown, Sharpe, win rate, trades, profit factor.
- Equity and drawdown chart.
- Price chart with entry/exit markers.
- Trade ledger table.
- Run settings panel.
- Error/warning panel for data gaps, warmup, custom strategy code issues, or close-fill assumptions.

## Custom Strategy Safety

Custom strategies should be stored in the database with a user-facing `title`, stable `slug`, editable `code`, and incrementing `version`.

Rules:

- Strategy title is required and should be unique enough for the user to recognize.
- Slug must match a conservative pattern like `[a-zA-Z0-9_]+`.
- Load strategy code from the `custom_strategies.code` DB column.
- Compile and execute the code in a temporary module namespace.
- Require `META` and `analyze`.
- If `META` is missing, optionally generate `StrategyMeta` from DB fields: `slug`, `title`, and `description`.
- Store `custom_strategy_code_snapshot` on each `backtest_runs` row so historical results remain reproducible even after the strategy is edited.
- Do not run custom strategy code with live-order credentials or broker APIs.

Important limitation: Python in-process execution of DB-stored code is not a true sandbox. For single-user local use this is acceptable if clearly labeled. If this becomes multi-user or exposed publicly, custom strategy execution should move into an isolated process/container with restricted imports and resource limits.

## Implementation Plan

### Phase 1: Engine + Built-In Strategies

1. Add `backend/backtesting/config.py`.
2. Add `backend/backtesting/engine.py`.
3. Add `backend/backtesting/metrics.py`.
4. Add `backend/backtesting/service.py`.
5. Reuse `strategies.registry` and current `StrategyContext`.
6. Add tests with deterministic synthetic OHLCV data:
   - long entry/exit
   - short entry/exit
   - stop hit
   - target hit
   - same-bar stop/target priority
   - commission/slippage math
   - finalized open trade

### Phase 2: API + Persistence

1. Add DB models for runs/trades/equity.
2. Add persistence helpers.
3. Add `backend/routers/backtests.py`.
4. Register router in `backend/main.py`.
5. Add CSV export endpoints.

### Phase 3: DB-Stored Custom Strategies

1. Add `custom_strategies` DB model/table.
2. Add CRUD endpoints for custom strategies.
3. Add `strategy_loader.py` with built-in/custom DB resolution.
4. Add code validation and useful error messages.
5. Store title, slug, description, code, enabled state, validation status, and version.
6. Store code snapshot on every backtest run.
7. Add tests for valid strategy, missing `META`, generated `META`, missing `analyze`, invalid slug, disabled strategy, and strategy runtime error.

### Phase 4: Frontend

1. Add Backtest action/view.
2. Add controls and result layout.
3. Add custom strategy manager: title input, slug, description, code editor, validate, save, enable/disable.
4. Render summary metrics.
5. Render equity/drawdown.
6. Render trade ledger.
7. Render price markers.
8. Add CSV export buttons.

### Phase 5: Enhancements

- Use stored candles for offline repeatability.
- Add date range selection.
- Add multi-symbol batch backtesting.
- Add parameter sweeps/optimization.
- Add walk-forward validation.
- Add benchmark comparison.
- Add adapter for `backtesting.py` if we need external optimization features.

## Open Decisions

1. Should the first implementation store all backtest runs permanently, or keep results temporary unless the user clicks save?
2. Should DB-stored custom strategy code be versioned in-place with code snapshots on runs, or should every edit create a separate `custom_strategy_versions` row?
3. Should default fees use CoinDCX spot/futures taker fees from settings, or a simple editable default?
4. Should the first release support multi-asset portfolio backtests, or only one pair per run?
5. Should the backtest view live inside the current dashboard page, or become a separate `/backtests` frontend route?

## Recommended First Build

Build the narrow but correct version first:

- Single pair.
- Built-in strategies plus DB-stored custom strategies with title, slug, code, validation status, and version.
- CoinDCX fetched data using current fetcher.
- `next_open` fill default.
- Fixed risk-dollar sizing.
- Full trade ledger and equity metrics.
- API and simple Backtest UI.

This gives us a trustworthy base without pulling in a large external framework or rewriting the current strategy layer.
