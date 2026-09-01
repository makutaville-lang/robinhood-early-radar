import os
import time
import json
import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
import requests

# ---------------- CONFIG ----------------
CHAIN = os.getenv("CHAIN", "robinhood")
BLOCKSCOUT = os.getenv("BLOCKSCOUT", "https://robinhoodchain.blockscout.com/api/v2")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "300"))  # 5 minutes

# Discovery + ranking thresholds. Designed to catch moves BEFORE +100% whenever possible.
MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "40000"))
MIN_MARKET_CAP_USD = float(os.getenv("MIN_MARKET_CAP_USD", "75000"))
MAX_MARKET_CAP_USD = float(os.getenv("MAX_MARKET_CAP_USD", "20000000"))

EARLY_SCORE = int(os.getenv("EARLY_SCORE", "55"))
HIGH_SCORE = int(os.getenv("HIGH_SCORE", "72"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "60"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Always monitor these contracts even if they do not appear in recent transfer discovery.
# Add one contract per line in watchlist.txt.
WATCHLIST_FILE = os.getenv("WATCHLIST_FILE", "watchlist.txt")

DB_FILE = os.getenv("DB_FILE", "radar.db")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("radar")

session = requests.Session()
session.headers.update({"User-Agent": "RobinhoodEarlyRadar/1.0"})

def now_ts() -> int:
    return int(time.time())

def safe_float(v, default=0.0):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default

def get_json(url: str, params=None, timeout=20):
    r = session.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()

def init_db():
    con = sqlite3.connect(DB_FILE)
    con.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            ts INTEGER NOT NULL,
            address TEXT NOT NULL,
            symbol TEXT,
            price REAL,
            liquidity REAL,
            market_cap REAL,
            volume_h1 REAL,
            volume_h6 REAL,
            volume_h24 REAL,
            buys_h1 INTEGER,
            sells_h1 INTEGER,
            price_h1 REAL,
            price_h6 REAL,
            price_h24 REAL,
            holders INTEGER,
            PRIMARY KEY (ts, address)
        )
    """)
    con.execute("""
        CREATE INDEX IF NOT EXISTS idx_snap_addr_ts
        ON snapshots(address, ts)
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            address TEXT PRIMARY KEY,
            last_alert_ts INTEGER NOT NULL,
            last_score INTEGER NOT NULL
        )
    """)
    con.commit()
    return con

def load_watchlist() -> List[str]:
    path = Path(WATCHLIST_FILE)
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        s = line.strip()
        if s and not s.startswith("#") and s.startswith("0x") and len(s) == 42:
            out.append(s)
    return out

def _gt_pools(endpoint: str, pages: int = 2) -> Dict[str, dict]:
    """Discover Robinhood tokens via GeckoTerminal public API."""
    tokens = {}
    headers = {
        "accept": "application/json",
        "Accept": "application/json;version=20230203",
    }
    for page in range(1, pages + 1):
        try:
            r = session.get(
                f"https://api.geckoterminal.com/api/v2/networks/robinhood/{endpoint}",
                params={"page": page, "include": "base_token"},
                headers=headers,
                timeout=20,
            )
            r.raise_for_status()
            payload = r.json()
        except Exception as e:
            log.warning("GeckoTerminal %s discovery failed: %s", endpoint, e)
            break
        included = {}
        for item in payload.get("included", []) or []:
            if item.get("type") == "token":
                included[item.get("id")] = item
        for pool in payload.get("data", []) or []:
            rel = ((pool.get("relationships") or {}).get("base_token") or {}).get("data") or {}
            token_id = rel.get("id") or ""
            if not token_id.startswith("robinhood_"):
                continue
            addr = token_id.replace("robinhood_", "", 1)
            meta = included.get(token_id, {})
            attrs = meta.get("attributes") or {}
            if addr.startswith("0x") and len(addr) == 42:
                tokens[addr.lower()] = {
                    "address": addr,
                    "symbol": attrs.get("symbol") or "?",
                    "name": attrs.get("name") or "?",
                }
        time.sleep(0.35)
    return tokens

def discover_recent_tokens(pages: int = 2) -> Dict[str, dict]:
    return _gt_pools("new_pools", pages=pages)

def discover_token_list(pages: int = 2) -> Dict[str, dict]:
    tokens = {}
    tokens.update(_gt_pools("pools", pages=pages))
    tokens.update(_gt_pools("trending_pools", pages=1))
    return tokens

def get_holders(address: str) -> int:
    # Blockscout currently returns 403 from some Railway egress IPs.
    return 0

def dex_pairs_batch(addresses: List[str]) -> List[dict]:
    """DEX Screener supports up to 30 token addresses per request."""
    all_pairs = []
    for i in range(0, len(addresses), 30):
        batch = addresses[i:i+30]
        url = f"https://api.dexscreener.com/tokens/v1/{CHAIN}/" + ",".join(batch)
        try:
            data = get_json(url)
            if isinstance(data, list):
                all_pairs.extend(data)
        except Exception as e:
            log.warning("DEX Screener batch failed: %s", e)
        time.sleep(0.12)
    return all_pairs

def best_pair_per_token(pairs: List[dict]) -> Dict[str, dict]:
    best = {}
    for p in pairs:
        if p.get("chainId") != CHAIN:
            continue
        token = p.get("baseToken") or {}
        addr = (token.get("address") or "").lower()
        if not addr:
            continue
        liq = safe_float((p.get("liquidity") or {}).get("usd"))
        existing = best.get(addr)
        existing_liq = safe_float((existing.get("liquidity") or {}).get("usd")) if existing else -1
        if liq > existing_liq:
            best[addr] = p
    return best

def snapshot_from_pair(p: dict, holders: int) -> dict:
    tx = p.get("txns") or {}
    vol = p.get("volume") or {}
    pc = p.get("priceChange") or {}
    base = p.get("baseToken") or {}
    return {
        "address": base.get("address"),
        "name": base.get("name") or "?",
        "symbol": base.get("symbol") or "?",
        "price": safe_float(p.get("priceUsd")),
        "liquidity": safe_float((p.get("liquidity") or {}).get("usd")),
        "market_cap": safe_float(p.get("marketCap")) or safe_float(p.get("fdv")),
        "volume_h1": safe_float(vol.get("h1")),
        "volume_h6": safe_float(vol.get("h6")),
        "volume_h24": safe_float(vol.get("h24")),
        "buys_h1": int((tx.get("h1") or {}).get("buys") or 0),
        "sells_h1": int((tx.get("h1") or {}).get("sells") or 0),
        "price_h1": safe_float(pc.get("h1")),
        "price_h6": safe_float(pc.get("h6")),
        "price_h24": safe_float(pc.get("h24")),
        "holders": holders,
        "pair_created_at": int(p.get("pairCreatedAt") or 0),
        "url": p.get("url") or "",
    }

def previous_snapshot(con, address: str, minutes_ago: int = 15) -> Optional[dict]:
    cutoff = now_ts() - minutes_ago * 60
    row = con.execute("""
        SELECT ts,address,symbol,price,liquidity,market_cap,volume_h1,volume_h6,
               volume_h24,buys_h1,sells_h1,price_h1,price_h6,price_h24,holders
        FROM snapshots
        WHERE lower(address)=lower(?) AND ts <= ?
        ORDER BY ts DESC LIMIT 1
    """, (address, cutoff)).fetchone()
    if not row:
        return None
    cols = ["ts","address","symbol","price","liquidity","market_cap","volume_h1",
            "volume_h6","volume_h24","buys_h1","sells_h1","price_h1","price_h6",
            "price_h24","holders"]
    return dict(zip(cols, row))

def pct_change(new: float, old: float) -> float:
    if not old or old <= 0:
        return 0.0
    return (new - old) / old * 100.0

def score_token(s: dict, prev15: Optional[dict]) -> Tuple[int, List[str]]:
    score = 0
    why = []
    liq = s["liquidity"]
    mc = s["market_cap"]
    p1 = s["price_h1"]
    p6 = s["price_h6"]
    p24 = s["price_h24"]
    v1 = s["volume_h1"]
    v6 = s["volume_h6"]
    buys = s["buys_h1"]
    sells = s["sells_h1"]
    holders = s["holders"]

    # Hard quality gates handled separately, score is momentum + quality.
    if 3 <= p1 < 10:
        score += 8; why.append(f"1h +{p1:.1f}%")
    elif 10 <= p1 < 25:
        score += 14; why.append(f"1h +{p1:.1f}%")
    elif 25 <= p1 <= 80:
        score += 18; why.append(f"1h +{p1:.1f}%")
    elif p1 > 80:
        score += 8; why.append(f"1h already +{p1:.0f}% (extended)")

    if 10 <= p6 < 40:
        score += 10; why.append(f"6h +{p6:.1f}%")
    elif 40 <= p6 <= 150:
        score += 15; why.append(f"6h +{p6:.1f}%")
    elif p6 > 150:
        score += 6; why.append(f"6h already +{p6:.0f}% (extended)")

    if 20 <= p24 <= 120:
        score += 10; why.append(f"24h +{p24:.1f}%")
    elif p24 > 120:
        score += 3; why.append(f"24h +{p24:.0f}% (chase risk)")

    # Turnover matters: high volume relative to liquidity and market cap.
    if liq > 0 and v1 / liq >= 0.15:
        score += 8; why.append(f"1h vol/liquidity {v1/liq:.2f}x")
    if mc > 0 and v6 / mc >= 0.15:
        score += 7; why.append(f"6h vol/MC {v6/mc:.2f}x")
    if mc > 0 and s["volume_h24"] / mc >= 0.50:
        score += 6; why.append(f"24h vol/MC {s['volume_h24']/mc:.2f}x")

    total = buys + sells
    buy_ratio = buys / total if total else 0
    if total >= 15 and buy_ratio >= 0.58:
        score += 8; why.append(f"1h buy ratio {buy_ratio:.0%}")
    if total >= 40:
        score += 4; why.append(f"{total} 1h transactions")

    if holders >= 100:
        score += 3; why.append(f"{holders:,} holders")
    if holders >= 500:
        score += 3

    if prev15:
        liq_growth = pct_change(liq, prev15.get("liquidity", 0))
        holder_growth = pct_change(holders, prev15.get("holders", 0))
        v1_growth = pct_change(v1, prev15.get("volume_h1", 0))
        price_growth = pct_change(s["price"], prev15.get("price", 0))
        if liq_growth >= 5:
            score += 10; why.append(f"liquidity +{liq_growth:.1f}%/15m")
        if holder_growth >= 3:
            score += 8; why.append(f"holders +{holder_growth:.1f}%/15m")
        if v1_growth >= 20:
            score += 9; why.append(f"1h-volume pace +{v1_growth:.0f}%/15m")
        if 2 <= price_growth <= 15:
            score += 7; why.append(f"price +{price_growth:.1f}%/15m")

    # Penalize obvious late-stage vertical moves.
    if p24 > 250:
        score -= 10
    if p6 > 250:
        score -= 10
    if liq < 75000:
        score -= 5

    return max(0, min(100, score)), why

def quality_gate(s: dict) -> Tuple[bool, str]:
    mc = s["market_cap"]
    liq = s["liquidity"]
    if liq < MIN_LIQUIDITY_USD:
        return False, f"liquidity ${liq:,.0f} below floor"
    if mc and mc < MIN_MARKET_CAP_USD:
        return False, f"market cap ${mc:,.0f} below floor"
    if mc and mc > MAX_MARKET_CAP_USD:
        return False, f"market cap ${mc:,.0f} above radar ceiling"
    if s["volume_h1"] < 2000 and s["volume_h6"] < 10000:
        return False, "volume too low"
    return True, ""

def store_snapshot(con, s: dict):
    ts = now_ts()
    con.execute("""
        INSERT OR REPLACE INTO snapshots
        (ts,address,symbol,price,liquidity,market_cap,volume_h1,volume_h6,volume_h24,
         buys_h1,sells_h1,price_h1,price_h6,price_h24,holders)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        ts,s["address"],s["symbol"],s["price"],s["liquidity"],s["market_cap"],
        s["volume_h1"],s["volume_h6"],s["volume_h24"],s["buys_h1"],s["sells_h1"],
        s["price_h1"],s["price_h6"],s["price_h24"],s["holders"]
    ))
    # Keep ~14 days at 5-min frequency without unbounded growth.
    con.execute("DELETE FROM snapshots WHERE ts < ?", (ts - 14*86400,))
    con.commit()

def can_alert(con, address: str, score: int) -> bool:
    row = con.execute(
        "SELECT last_alert_ts,last_score FROM alerts WHERE lower(address)=lower(?)",
        (address,)
    ).fetchone()
    if not row:
        return True
    last_ts, last_score = row
    elapsed = now_ts() - last_ts
    # Allow a faster re-alert if score materially strengthens.
    return elapsed >= COOLDOWN_MINUTES*60 or score >= last_score + 12

def mark_alert(con, address: str, score: int):
    con.execute("""
        INSERT INTO alerts(address,last_alert_ts,last_score) VALUES(?,?,?)
        ON CONFLICT(address) DO UPDATE SET last_alert_ts=excluded.last_alert_ts,
                                           last_score=excluded.last_score
    """, (address, now_ts(), score))
    con.commit()

def money(v: float) -> str:
    if v >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v/1_000:.0f}K"
    return f"${v:,.0f}"

def format_alert(s: dict, score: int, why: List[str]) -> str:
    if score >= HIGH_SCORE and s["price_h1"] <= 80 and s["price_h24"] <= 250:
        flag = "🚨 HIGH PRIORITY"
    else:
        flag = "🟡 EARLY RADAR"

    tx_total = s["buys_h1"] + s["sells_h1"]
    br = s["buys_h1"] / tx_total if tx_total else 0
    return (
        f"{flag} — {s['symbol']} | score {score}/100\n"
        f"Price: ${s['price']:.10g}\n"
        f"MC/FDV: {money(s['market_cap'])} | Liquidity: {money(s['liquidity'])}\n"
        f"Change: 1h {s['price_h1']:+.1f}% | 6h {s['price_h6']:+.1f}% | 24h {s['price_h24']:+.1f}%\n"
        f"Volume: 1h {money(s['volume_h1'])} | 6h {money(s['volume_h6'])} | 24h {money(s['volume_h24'])}\n"
        f"1h buys/sells: {s['buys_h1']}/{s['sells_h1']} ({br:.0%} buys) | holders: {s['holders']:,}\n"
        f"Why: {', '.join(why[:7])}\n"
        f"Contract: {s['address']}\n"
        f"{s['url']}\n"
        f"⚠️ Signal only — verify contract/security before buying."
    )

def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured. ALERT:\n%s", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    r = session.post(url, json=payload, timeout=20)
    r.raise_for_status()

def run_cycle(con):
    discovered = {}
    discovered.update(discover_recent_tokens())
    discovered.update(discover_token_list())
    for addr in load_watchlist():
        discovered.setdefault(addr.lower(), {"address": addr, "symbol": "WATCH", "name": "Watchlist"})

    addresses = [v["address"] for v in discovered.values()]
    if not addresses:
        log.warning("No token addresses discovered.")
        return

    log.info("Discovered %d candidate ERC-20 contracts", len(addresses))
    pairs = dex_pairs_batch(addresses)
    best = best_pair_per_token(pairs)
    log.info("DEX data found for %d candidates", len(best))

    ranked = []
    for addr_lower, p in best.items():
        liq = safe_float((p.get("liquidity") or {}).get("usd"))
        mc = safe_float(p.get("marketCap")) or safe_float(p.get("fdv"))
        # Skip holder query for clearly irrelevant pairs to keep calls light.
        if liq < MIN_LIQUIDITY_USD * 0.5:
            continue
        if mc and mc > MAX_MARKET_CAP_USD * 1.5:
            continue

        address = (p.get("baseToken") or {}).get("address")
        holders = get_holders(address) if address else 0
        s = snapshot_from_pair(p, holders)
        prev = previous_snapshot(con, s["address"], 15)
        score, why = score_token(s, prev)
        ok, reason = quality_gate(s)
        store_snapshot(con, s)
        if ok:
            ranked.append((score, s, why))
        else:
            log.debug("Gate %s: %s", s["symbol"], reason)

    ranked.sort(key=lambda x: x[0], reverse=True)
    if ranked:
        top = ", ".join(f"{s['symbol']}:{score}" for score,s,_ in ranked[:8])
        log.info("Top radar: %s", top)

    for score, s, why in ranked:
        if score >= EARLY_SCORE and can_alert(con, s["address"], score):
            msg = format_alert(s, score, why)
            try:
                send_telegram(msg)
                mark_alert(con, s["address"], score)
                log.info("Alerted %s score=%d", s["symbol"], score)
            except Exception as e:
                log.error("Alert delivery failed for %s: %s", s["symbol"], e)

def main():
    con = init_db()
    log.info("Robinhood Early Radar started | poll=%ss | early=%d high=%d", POLL_SECONDS, EARLY_SCORE, HIGH_SCORE)
    log.info("Data sources: Robinhood Blockscout + DEX Screener")
    while True:
        started = time.time()
        try:
            run_cycle(con)
        except KeyboardInterrupt:
            raise
        except Exception:
            log.exception("Cycle failed")
        elapsed = time.time() - started
        time.sleep(max(5, POLL_SECONDS - elapsed))

if __name__ == "__main__":
    main()
