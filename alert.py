"""
Crypto breakout + volume spike alerts -> Telegram
Scans the top 100 coins by market cap (CoinGecko) and checks the last CLOSED
candle on Binance public market data.

Trigger (both must be true):
  1. Breakout:     close > highest high of the previous LOOKBACK candles
  2. Volume spike: volume > VOLUME_MULT x average volume of those candles

Run once per candle interval (e.g. hourly for 1h candles). Stateless: each run
only looks at the latest closed candle, so you get one alert per candle.
"""
import os
import time
import requests
from concurrent.futures import ThreadPoolExecutor

# ====== PASTE YOUR DETAILS HERE (or set them as environment variables) ======
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "PASTE_YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "PASTE_YOUR_CHAT_ID_HERE")
# ============================================================================

# ---- Settings you can tweak ----
INTERVAL = "1h"        # candle size: 15m, 1h, 4h, 1d
LOOKBACK = 10          # candles used for the breakout high + average volume
VOLUME_MULT = 1.5      # volume must be at least this many times the average
TOP_N = 100            # how many top coins to scan
SEND_TEST_MESSAGE = False  # set True once to confirm the bot can message you
REWARD_RISK = 2.0      # target = entry + this many times your risk
STOP_BUFFER = 0.002    # stop sits 0.2% below the stop level (breathing room)

BINANCE = "https://data-api.binance.vision/api/v3/klines"  # public, no key
COINGECKO = "https://api.coingecko.com/api/v3/coins/markets"
SKIP = {"usdt", "usdc", "dai", "fdusd", "tusd", "usde", "usds", "usdd", "pyusd",
        "steth", "wsteth", "weth", "wbtc", "weeth", "wbeth", "cbbtc", "susde"}


def get_top_coins():
    r = requests.get(
        COINGECKO,
        params={"vs_currency": "usd", "order": "market_cap_desc",
                "per_page": TOP_N, "page": 1},
        timeout=20,
    )
    r.raise_for_status()
    return [c["symbol"].upper() for c in r.json() if c["symbol"].lower() not in SKIP]


def check_coin(symbol):
    pair = f"{symbol}USDT"
    try:
        r = requests.get(
            BINANCE,
            params={"symbol": pair, "interval": INTERVAL, "limit": LOOKBACK + 2},
            timeout=15,
        )
        if r.status_code != 200:
            return None  # coin not listed vs USDT on Binance
        candles = r.json()[:-1]  # drop the still-forming candle
        if len(candles) < LOOKBACK + 1:
            return None
        last, prev = candles[-1], candles[-1 - LOOKBACK:-1]
        close, volume = float(last[4]), float(last[5])
        prev_high = max(float(c[2]) for c in prev)
        avg_vol = sum(float(c[5]) for c in prev) / len(prev)
        if avg_vol == 0:
            return None
        vol_ratio = volume / avg_vol
        if close > prev_high and vol_ratio >= VOLUME_MULT:
            last_low = float(last[3])
            entry = close
            # stop: just below the breakout candle's low or the broken level,
            # whichever is lower (so a normal retest does not stop you out)
            stop = min(last_low, prev_high) * (1 - STOP_BUFFER)
            risk = entry - stop
            target = entry + REWARD_RISK * risk
            return {
                "pair": pair, "close": close, "prev_high": prev_high,
                "vol_ratio": vol_ratio,
                "change": (close - float(last[1])) / float(last[1]) * 100,
                "entry": entry, "stop": stop, "target": target,
                "risk_pct": risk / entry * 100,
                "reward_pct": (target - entry) / entry * 100,
            }
    except Exception as e:
        print(f"{pair}: error {e}")
    return None


def send_telegram(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
              "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=15,
    )
    if r.status_code != 200:
        print("Telegram error:", r.text)


def main():
    if SEND_TEST_MESSAGE:
        send_telegram("✅ Alert bot is connected.")

    symbols = get_top_coins()
    print(f"Scanning {len(symbols)} coins on {INTERVAL} candles...")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [r for r in pool.map(check_coin, symbols) if r]

    print(f"{len(results)} signal(s) found")
    for s in sorted(results, key=lambda x: -x["vol_ratio"]):
        base = s["pair"].replace("USDT", "")
        msg = (
            f"🚀 <b>{base}</b> breakout ({INTERVAL})\n"
            f"Price: ${s['close']:,.6g} ({s['change']:+.2f}% this candle)\n"
            f"Broke {LOOKBACK}-candle high: ${s['prev_high']:,.6g}\n"
            f"Volume: {s['vol_ratio']:.1f}x average\n\n"
            f"Entry: ${s['entry']:,.6g}\n"
            f"Stop loss: ${s['stop']:,.6g} (-{s['risk_pct']:.1f}%)\n"
            f"Target: ${s['target']:,.6g} (+{s['reward_pct']:.1f}%)\n"
            f"Reward:Risk = {REWARD_RISK:.0f}:1\n\n"
            f"Not financial advice. Rule-based levels only.\n"
            f"https://www.binance.com/en/trade/{base}_USDT"
        )
        send_telegram(msg)
        time.sleep(0.5)  # stay under Telegram rate limits


if __name__ == "__main__":
    main()
