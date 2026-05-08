# Trading-Bot

## CoinDCX Crypto Bot

The CoinDCX live bot lives at:

```bash
Trading-Bot/Crypto/live_confluence_monitor.py
```

It uses CoinDCX public market data for 5-minute candles/trades, calculates a
confluence score from Supertrend, EMA/RSI, MACD, Bollinger Bands, and RSI, then
prints paper ENTRY / EXIT signals. Live orders are disabled unless
`--place-orders` is passed.

### 1. Start With Paper Mode

Run this first. It does not place exchange orders.

```bash
cd /Users/harshal/Asur/Backtesting
venv/bin/python Trading-Bot/Crypto/live_confluence_monitor.py \
  --pair B-ETH_USDT \
  --market ETHUSDT \
  --risk 10 \
  --poll 15
```

For INR markets, use the matching CoinDCX pair/market, for example:

```bash
venv/bin/python Trading-Bot/Crypto/live_confluence_monitor.py \
  --pair I-BTC_INR \
  --market BTCINR \
  --risk 500 \
  --poll 15
```

### 2. Add CoinDCX API Credentials

Create an API key from the CoinDCX API dashboard. For safety:

- Enable only the permissions needed for trading.
- Keep withdrawals disabled.
- Bind the key to your VPS/static IP when possible.
- Test in paper mode before enabling live orders.

```bash
export COINDCX_API_KEY="your_key"
export COINDCX_API_SECRET="your_secret"
```

### 3. Live Spot Mode

Spot mode supports long entries and long exits. Shorts are ignored unless you use
margin or futures mode.

```bash
venv/bin/python Trading-Bot/Crypto/live_confluence_monitor.py \
  --pair B-ETH_USDT \
  --market ETHUSDT \
  --execution-mode spot \
  --risk 10 \
  --poll 15 \
  --place-orders
```

### 4. Live Futures Mode

Futures mode can manage long/short positions and attempts to attach stop-loss
and take-profit orders after entry.

```bash
venv/bin/python Trading-Bot/Crypto/live_confluence_monitor.py \
  --pair B-ETH_USDT \
  --execution-mode futures \
  --futures-margin-currency USDT \
  --position-margin-type crossed \
  --leverage 1 \
  --risk 10 \
  --poll 15 \
  --place-orders
```

Add `--allow-shorts` only after you are comfortable with the paper logs.

### 5. Current CoinDCX API Shape

CoinDCX public market data uses endpoints such as:

- `GET https://api.coindcx.com/exchange/ticker`
- `GET https://public.coindcx.com/market_data/candles`

Authenticated requests use compact JSON payloads signed with HMAC-SHA256 using
your API secret, and the signature is sent with:

- `X-AUTH-APIKEY`
- `X-AUTH-SIGNATURE`

The bot implements this signing flow internally.

### Notes

Do not treat arbitrage as low risk by default. On a retail connection, fees,
spread, partial fills, and latency can erase millisecond-sized edges quickly.
Start with paper logs, then tiny live size, then scale only after the bot has
survived boring market conditions and stressful ones.

## Visual Dashboard

The dashboard has a Python backend and a static frontend:

- Backend: `backend/server.py`
- Frontend: `frontend/index.html`, `frontend/styles.css`, `frontend/app.js`

Run it from the repo root:

```bash
cd /Users/harshal/Asur/Backtesting/Trading-Bot
../venv/bin/python backend/server.py
```

Local settings live in `.env`. Use `.env.example` as the template. Keep real
CoinDCX keys only in `.env`; it is ignored by git.

Then open:

```text
http://127.0.0.1:8000
```

The backend exposes:

- `GET /api/health`
- `GET /api/strategies`
- `GET /api/futures-markets`
- `GET /api/track?coins=BTC,ETH,SOL&strategy=confluence`
- `GET /api/account?mode=futures`
- `GET /api/live?pair=B-ETH_USDT&market=ETHUSDT&mode=futures&interval=1`
- `GET /api/snapshot?pair=B-ETH_USDT&market=ETHUSDT&mode=spot`
- `GET /api/snapshot?pair=B-ETH_USDT&market=ETHUSDT&mode=futures&strategy=trend_following`
- `GET /api/markets?q=ETH`
- `GET /api/account?market=ETHUSDT`

The visualizer shows price candles, Supertrend, confluence score, RSI, current
signal, paper trade state, and replayed bot events. It is paper-only; live order
placement remains in `Crypto/live_confluence_monitor.py` and still requires
`--place-orders`.

The full strategy snapshot refreshes on a slower cadence because it recalculates
bars, indicators, and replayed paper trades. The visible price is streamed
separately through `/api/live` every second, matching CoinDCX's public ticker
cadence.

Available dashboard strategies:

- `confluence`: existing multi-indicator vote system.
- `trend_following`: EMA 9/21, EMA 55, Supertrend, and RSI alignment.
- `mean_reversion`: Bollinger Band and RSI extreme reversals.
- `arbitrage`: paper-only spot/futures spread scanner.

The Futures Tracker at the top of the dashboard loads available CoinDCX USDT
futures instruments, lets you search/select multiple coins, start tracking them
together, and click any row to load that coin into the detailed chart below.

The Wallet + Risk panel is read-only. When `COINDCX_API_KEY` and
`COINDCX_API_SECRET` are set in `.env`, it shows available futures wallet balance
and suggested risk values at 0.25%, 0.5%, 1%, and 2% of available balance.
