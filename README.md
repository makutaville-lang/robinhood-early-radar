# Robinhood Early-Mover Radar

This is a **standalone autonomous scanner**. It does not wait for you to type "Crypto Check".

## What it does
Every 5 minutes it:
1. Discovers ERC-20 activity directly from the Robinhood Chain Blockscout explorer.
2. Adds tokens from the explorer token list and your optional watchlist.
3. Pulls live DEX Screener data in batches.
4. Tracks price, liquidity, volume, transactions and holder counts over time in SQLite.
5. Scores acceleration.
6. Sends a Telegram alert when an early-mover score crosses the threshold.
7. Penalizes already-vertical moves so it is less likely to tell you to chase.

Default gates:
- Liquidity >= $40k
- MC/FDV roughly $75k-$20m
- Early alert score >= 55
- High-priority score >= 72
- 5-minute polling

These are intentionally more sensitive than a conservative investment screener. The alert is a **research signal, not an automatic trade**.

## Easiest setup: Telegram + cloud host
You only need to do two manual things because I cannot create accounts or secret tokens for you.

### 1. Create Telegram bot
- In Telegram, open **BotFather**.
- Send `/newbot`.
- Follow the prompts and copy the bot token.
- Send any message to your new bot.
- In a browser, visit:
  `https://api.telegram.org/botYOUR_BOT_TOKEN/getUpdates`
- Find `"chat":{"id":...}` and copy that number.

Never give the bot token to anyone you do not trust.

### 2. Run it
For a computer test:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="YOUR_TOKEN"
export TELEGRAM_CHAT_ID="YOUR_CHAT_ID"
python radar.py
```

For 24/7 operation, deploy this folder as a **background worker** on a cloud service that can run Docker/Python continuously. Add the two Telegram values as secret environment variables. The included `Dockerfile` and `Procfile` are ready for that.

## Important
- It does **not** place trades or hold wallet private keys.
- Never put a wallet seed phrase/private key into this scanner.
- DEX Screener and Blockscout can be unavailable or change APIs, so alerts are not guaranteed.
- Low-cap tokens can be manipulated or become illiquid. A momentum alert is not a safety approval.
- Before buying: verify the canonical contract, liquidity, holder concentration, sellability/honeypot risk, and major wallet behavior.

## Why this design
Robinhood Chain mainnet is chain ID 4663. DEX Screener identifies the chain as `robinhood`.
The scanner uses public, read-only endpoints and keeps its own time-series snapshots so it can detect changes such as liquidity/holder/volume acceleration rather than merely reading a 24-hour percentage.

## Files
- `radar.py` — scanner
- `watchlist.txt` — optional forced contracts
- `.env.example` — settings template
- `requirements.txt` — Python package
- `Dockerfile` — cloud/container deployment
- `Procfile` — compatible worker command
