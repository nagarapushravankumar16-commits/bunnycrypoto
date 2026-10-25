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
INTERVAL = "15m"       # candle size: 15m, 1h, 4h, 1d
LOOKBACK = 10          # candles used for the breakout high + average volume
VOLUME_MULT = 1.5      # volume must be at least this many times the average
TOP_N = 100            # how many top coins to scan
SEND_TEST_MESSAGE = False  # set True once to confirm the bot can message you
REWARD_RISK = 1.5      # target = entry + this many times your risk
STOP_BUFFER = 0.002    # stop sits 0.2% below the stop level (breathing room)

# ---- Fee estimates (percent per side, EDIT to match your own fee tier) ----
# Check the real numbers on each exchange's fee page; these are placeholders.
BINANCE_FEE_PCT = 0.10    # Binance spot, per buy or per sell
COINDCX_FEE_PCT = 0.20    # CoinDCX spot, per buy or per sell (before GST)
GST_ON_FEE = 0.18         # India adds 18% GST on the trading fee
INDIA_TDS_PCT = 1.0       # 1% TDS on the sell value (CoinDCX / Indian exchanges)
FALLBACK_USDT_INR = 90.0  # used only if the live USDT->INR rate cannot be fetched
TRADE_SIZE_INR = 10000    # example trade size used to show costs in rupees

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


def get_usdt_inr():
    """Live USDT -> INR rate from CoinGecko. Falls back to a fixed rate if it fails."""
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "tether", "vs_currencies": "inr"}, timeout=15,
        )
        r.raise_for_status()
        return float(r.json()["tether"]["inr"])
    except Exception as e:
        print("INR rate error, using fallback:", e)
        return FALLBACK_USDT_INR


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


def fee_line(s, fee_pct, gst=0.0, tds_pct=0.0):
    """Round-trip cost and net profit % if the target is hit, on a 100-unit trade."""
    buy = 100.0
    sell = buy * (1 + s["reward_pct"] / 100)
    fee_rate = fee_pct / 100 * (1 + gst)
    fees = buy * fee_rate + sell * fee_rate
    tds = sell * tds_pct / 100
    net_target = (sell - buy) - fees - tds
    stop_sell = buy * (1 - s["risk_pct"] / 100)
    fees_stop = buy * fee_rate + stop_sell * fee_rate
    tds_stop = stop_sell * tds_pct / 100
    net_stop = (stop_sell - buy) - fees_stop - tds_stop
    return fees + tds, net_target, net_stop


def fee_text(s, inr):
    b_cost, b_win, b_loss = fee_line(s, BINANCE_FEE_PCT)
    c_cost, c_win, c_loss = fee_line(s, COINDCX_FEE_PCT, GST_ON_FEE, INDIA_TDS_PCT)
    k = TRADE_SIZE_INR / 100.0  # numbers above are per 100, scale to trade size
    return (
        f"<b>Estimated costs on \u20b9{TRADE_SIZE_INR:,} traded</b>\n"
        f"Binance ({BINANCE_FEE_PCT}%/side): cost \u20b9{b_cost*k:,.0f}, "
        f"net if target \u20b9{b_win*k:+,.0f}, net if stop \u20b9{b_loss*k:+,.0f}\n"
        f"CoinDCX ({COINDCX_FEE_PCT}%/side + GST + {INDIA_TDS_PCT:g}% TDS): "
        f"cost \u20b9{c_cost*k:,.0f}, net if target \u20b9{c_win*k:+,.0f}, "
        f"net if stop \u20b9{c_loss*k:+,.0f}"
    )


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
    inr = get_usdt_inr() if results else FALLBACK_USDT_INR
    for s in sorted(results, key=lambda x: -x["vol_ratio"]):
        base = s["pair"].replace("USDT", "")
        msg = (
            f"\U0001F680 <b>{base}</b> breakout ({INTERVAL})\n"
            f"Price: \u20b9{s['close']*inr:,.6g} (${s['close']:,.6g}, {s['change']:+.2f}% this candle)\n"
            f"Broke {LOOKBACK}-candle high: \u20b9{s['prev_high']*inr:,.6g}\n"
            f"Volume: {s['vol_ratio']:.1f}x average\n\n"
            f"Entry: \u20b9{s['entry']*inr:,.6g}\n"
            f"Stop loss: \u20b9{s['stop']*inr:,.6g} (-{s['risk_pct']:.1f}%)\n"
            f"Target: \u20b9{s['target']*inr:,.6g} (+{s['reward_pct']:.1f}%)\n"
            f"Reward:Risk = {REWARD_RISK:.1f}:1\n\n"
            f"{fee_text(s, inr)}\n\n"
            f"Rupee levels use USDT/INR \u20b9{inr:,.2f}. Check the live INR price on CoinDCX before trading.\n"
            f"Not financial advice. Rule-based levels only.\n"
            f"https://www.binance.com/en/trade/{base}_USDT"
        )
        send_telegram(msg)
        time.sleep(0.5)  # stay under Telegram rate limits


if __name__ == "__main__":
    main()
