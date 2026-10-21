"""
Leveraged Crypto Trade Screener
================================

A phone-friendly Streamlit app that:
  * downloads live candles for a crypto pair (with several data-source fallbacks),
  * runs a style-weighted multi-timeframe analysis (Supertrend, EMAs, RSI, MACD, ADX, volume),
  * back-tests the same rules over roughly the last 12 months (costs and leverage included),
  * works out position size, liquidation price, stop, target and costs,
  * gives a big GO LONG / GO SHORT / NO-GO answer with a confidence %,
  * learns a little from the Win / Loss results you log.

NOT FINANCIAL ADVICE. Leveraged trading can lose your entire collateral.

How the file is organised
-------------------------
1. Settings and constants
2. Data layer (exchanges, fallbacks, caching)
3. Indicators (computed with pandas / numpy only - nothing to compile)
4. Scoring and multi-timeframe analysis
5. Position, liquidation and costs
6. Backtest
7. Decision logic
8. Learning log (self-learning feedback)
9. Charts
10. User interface  -> main(), only runs under `streamlit run app.py`

All the maths lives in plain functions so it can be tested without the UI.
"""

from __future__ import annotations

import html as _html
import io
import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import streamlit as st

import plotly.graph_objects as go
from plotly.subplots import make_subplots

# =============================================================================
# 1. SETTINGS AND CONSTANTS
# =============================================================================

APP_DIR = Path(__file__).resolve().parent
LEARNING_LOG_PATH = APP_DIR / "learning_log.json"

# UAE time (Gulf Standard Time, UTC+4, no daylight saving) - used for all displayed times.
LOCAL_TZ = timezone(timedelta(hours=4))
LOCAL_TZ_LABEL = "UAE time (UTC+4)"

TAKER_FEE = 0.0004             # 0.04 % taker fee, charged on entry AND on exit
MAINT_MARGIN_RATE = 0.005      # 0.5 % maintenance margin used for the liquidation estimate
DEFAULT_FUNDING_8H = 0.0001    # 0.01 % per 8 hours, used if no live funding rate is available
REQUEST_TIMEOUT = 8            # seconds per HTTP request
MIN_BARS = 60                  # fewer candles than this = not enough data for a timeframe
LIVE_BARS = 500                # candles downloaded per timeframe for the live analysis
CHART_BARS = 150               # candles drawn on the chart (keeps it readable on a phone)
MIN_CONFIDENCE_FOR_GO = 55     # below this the answer is always NO-GO

# Pairs offered in the picker and scanned by default (majors first, then the user's extras).
# Pairs not on Binance (e.g. HYPE, PUMP) are served automatically by Bybit / OKX / KuCoin etc.
POPULAR_PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT",
                 "AVAXUSDT", "LINKUSDT", "TONUSDT", "DOTUSDT", "LTCUSDT", "BCHUSDT", "NEARUSDT",
                 "SUIUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "INJUSDT", "TRXUSDT",
                 "PENGUUSDT", "ZECUSDT", "HYPEUSDT", "PUMPUSDT", "PEPEUSDT", "WIFUSDT", "ENAUSDT", "TAOUSDT"]

INTERVAL_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400,
                    "1d": 86400, "1w": 604800}

# Everything that changes between trade styles lives here.
STYLES = {
    "Swing": dict(
        tfs=["4h", "1d", "1w"], weights=[0.45, 0.35, 0.20], primary="4h",
        lev_range=(2, 5), st_period=10, st_mult=3.0,
        stop_atr=2.0, min_stop_pct=0.010, rr=2.5, slippage=0.0002, hold_hours=72, max_hold_bars=42,
        bt_candles=2190 + 260, min_score=30,
        desc="Holds for days. Uses 4-hour, daily and weekly charts. Low leverage (2-5x).",
    ),
    "Day": dict(
        tfs=["15m", "1h", "4h"], weights=[0.30, 0.45, 0.25], primary="1h",
        lev_range=(3, 8), st_period=10, st_mult=3.0,
        stop_atr=1.5, min_stop_pct=0.006, rr=2.0, slippage=0.0003, hold_hours=8, max_hold_bars=24,
        bt_candles=8760 + 260, min_score=30,
        desc="Holds for hours, closed the same day. Uses 15-minute, 1-hour and 4-hour charts (3-8x).",
    ),
    "Scalp": dict(
        tfs=["5m", "15m", "1h"], weights=[0.50, 0.30, 0.20], primary="5m",
        lev_range=(5, 15), st_period=10, st_mult=2.5,
        stop_atr=2.0, min_stop_pct=0.0035, rr=2.0, slippage=0.0004, hold_hours=1, max_hold_bars=36,
        # 12 months of 5-minute candles is ~105,000 - far too many for a free
        # server, so we test the most recent ~12,000 candles (about 6 weeks).
        bt_candles=12000, min_score=30,
        desc="Quick trades of minutes to an hour. Uses 5-minute, 15-minute and 1-hour charts (5-15x).",
    ),
}

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "crypto-trade-screener/1.0", "Accept": "application/json"})


class DataError(Exception):
    """Friendly error raised when market data cannot be loaded."""


# =============================================================================
# 2. DATA LAYER
# =============================================================================
# Streamlit Community Cloud runs in the USA. Binance's main API (and Bybit)
# block US addresses, so we try several public sources in order and use the
# first one that answers. No API keys are needed for any of them.

def parse_pair(raw: str) -> tuple[str, str]:
    """Turn 'btc/usdt', 'BTC-USDT' or 'btc' into ('BTC', 'USDT')."""
    clean = re.sub(r"[^A-Z0-9]", "", (raw or "").upper())
    if not clean:
        raise DataError("Please type a pair such as BTCUSDT.")
    for quote in ("USDT", "USDC", "USD"):
        if clean.endswith(quote) and len(clean) > len(quote):
            return clean[: -len(quote)], quote
    return clean, "USDT"            # 'BTC' on its own -> BTC/USDT


def _get_json(url: str, params: dict | None = None):
    r = HTTP.get(url, params=params, timeout=REQUEST_TIMEOUT)
    if r.status_code == 451:
        raise DataError("blocked in this region (HTTP 451)")
    if r.status_code == 403:
        raise DataError("access refused (HTTP 403)")
    if r.status_code >= 400:
        raise DataError(f"HTTP {r.status_code}: {r.text[:120]}")
    return r.json()


def _frame(rows, cols, time_unit) -> pd.DataFrame:
    """Build a tidy OHLCV frame (ascending time, UTC) from raw rows."""
    df = pd.DataFrame(rows, columns=cols)
    df = df[["time", "open", "high", "low", "close", "volume"]].copy()
    df["time"] = pd.to_datetime(pd.to_numeric(df["time"]), unit=time_unit, utc=True)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna().sort_values("time").reset_index(drop=True)


# ---- individual sources: fetch(symbol, interval, limit, end_ms) -> ascending df

def _binance_fetch(base_url):
    def fetch(sym, iv, limit, end_ms):
        params = {"symbol": sym, "interval": iv, "limit": min(limit, 1000)}
        if end_ms:
            params["endTime"] = end_ms
        rows = _get_json(f"{base_url}/api/v3/klines", params)
        if isinstance(rows, dict):
            raise DataError(rows.get("msg", "unknown error"))
        return _frame([r[:6] for r in rows], ["time", "open", "high", "low", "close", "volume"], "ms")
    return fetch


def _bybit_fetch(sym, iv, limit, end_ms):
    ivmap = {"5m": "5", "15m": "15", "1h": "60", "4h": "240", "1d": "D", "1w": "W"}
    params = {"category": "spot", "symbol": sym, "interval": ivmap[iv], "limit": min(limit, 1000)}
    if end_ms:
        params["end"] = end_ms
    j = _get_json("https://api.bybit.com/v5/market/kline", params)
    if j.get("retCode") != 0:
        raise DataError(j.get("retMsg", "unknown error"))
    rows = [r[:6] for r in j["result"]["list"]]
    return _frame(rows, ["time", "open", "high", "low", "close", "volume"], "ms")


def _okx_fetch(sym, iv, limit, end_ms):
    ivmap = {"5m": "5m", "15m": "15m", "1h": "1H", "4h": "4H", "1d": "1Dutc", "1w": "1Wutc"}
    if end_ms:   # older data comes from the history endpoint, 100 per page
        url = "https://www.okx.com/api/v5/market/history-candles"
        params = {"instId": sym, "bar": ivmap[iv], "limit": min(limit, 100), "after": end_ms + 1}
    else:
        url = "https://www.okx.com/api/v5/market/candles"
        params = {"instId": sym, "bar": ivmap[iv], "limit": min(limit, 300)}
    j = _get_json(url, params)
    if j.get("code") != "0":
        raise DataError(j.get("msg", "unknown error"))
    rows = [r[:6] for r in j["data"]]
    return _frame(rows, ["time", "open", "high", "low", "close", "volume"], "ms")


def _kucoin_fetch(sym, iv, limit, end_ms):
    ivmap = {"5m": "5min", "15m": "15min", "1h": "1hour", "4h": "4hour", "1d": "1day", "1w": "1week"}
    end_s = int((end_ms or time.time() * 1000) // 1000)
    start_s = end_s - min(limit, 1500) * INTERVAL_SECONDS[iv]
    j = _get_json("https://api.kucoin.com/api/v1/market/candles",
                  {"type": ivmap[iv], "symbol": sym, "startAt": start_s, "endAt": end_s})
    if j.get("code") != "200000":
        raise DataError(j.get("msg", "unknown error"))
    # KuCoin order is: time, open, CLOSE, HIGH, LOW, volume, turnover
    rows = [r[:6] for r in j["data"]]
    return _frame(rows, ["time", "open", "close", "high", "low", "volume"], "s")


def _coinbase_fetch(sym, iv, limit, end_ms):
    gran = {"5m": 300, "15m": 900, "1h": 3600, "1d": 86400}[iv]
    end = datetime.fromtimestamp((end_ms or time.time() * 1000) / 1000, tz=timezone.utc)
    start = end - timedelta(seconds=gran * min(limit, 300))
    rows = _get_json(f"https://api.exchange.coinbase.com/products/{sym}/candles",
                     {"granularity": gran, "start": start.isoformat(), "end": end.isoformat()})
    if isinstance(rows, dict):
        raise DataError(rows.get("message", "unknown error"))
    # Coinbase order is: time, LOW, HIGH, open, close, volume
    return _frame(rows, ["time", "low", "high", "open", "close", "volume"], "s")


def _kraken_fetch(sym, iv, limit, end_ms):
    ivmap = {"5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440, "1w": 10080}
    if end_ms:              # Kraken only serves the latest 720 candles
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    j = _get_json("https://api.kraken.com/0/public/OHLC", {"pair": sym, "interval": ivmap[iv]})
    if j.get("error"):
        raise DataError(", ".join(j["error"]))
    key = [k for k in j["result"] if k != "last"][0]
    rows = [[r[0], r[1], r[2], r[3], r[4], r[6]] for r in j["result"][key]]
    return _frame(rows, ["time", "open", "high", "low", "close", "volume"], "s")


def _kraken_symbols(base, quote):
    kb = {"BTC": "XBT", "DOGE": "XDG"}.get(base, base)
    return [f"{kb}{quote}", f"{kb}USD"] if quote != "USD" else [f"{kb}USD"]


def _coinbase_symbols(base, quote):
    return [f"{base}-{quote}", f"{base}-USD"] if quote != "USD" else [f"{base}-USD"]


# Order matters: first one that works is used.
SOURCES = [
    dict(name="Binance (public data mirror)", fetch=_binance_fetch("https://data-api.binance.vision"),
         page=1000, paginate=True, ivs=set(INTERVAL_SECONDS), syms=lambda b, q: [f"{b}{q}"]),
    dict(name="Binance", fetch=_binance_fetch("https://api.binance.com"),
         page=1000, paginate=True, ivs=set(INTERVAL_SECONDS), syms=lambda b, q: [f"{b}{q}"]),
    dict(name="Bybit", fetch=_bybit_fetch, page=1000, paginate=True,
         ivs=set(INTERVAL_SECONDS), syms=lambda b, q: [f"{b}{q}"]),
    dict(name="OKX", fetch=_okx_fetch, page=100, paginate=True,
         ivs=set(INTERVAL_SECONDS), syms=lambda b, q: [f"{b}-{q}"]),
    dict(name="KuCoin", fetch=_kucoin_fetch, page=1500, paginate=True,
         ivs=set(INTERVAL_SECONDS), syms=lambda b, q: [f"{b}-{q}"]),
    dict(name="Coinbase", fetch=_coinbase_fetch, page=300, paginate=True,
         ivs={"5m", "15m", "1h", "1d"}, syms=_coinbase_symbols),
    dict(name="Kraken", fetch=_kraken_fetch, page=720, paginate=False,
         ivs=set(INTERVAL_SECONDS), syms=_kraken_symbols),
]

# If a source lacks an interval we build it from a smaller one.
RESAMPLE_FROM = {"4h": ("1h", 4, "4h"), "1w": ("1d", 7, "W-MON")}


def _resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    d = df.set_index("time")
    if rule.startswith("W"):
        r = d.resample(rule, label="left", closed="left")
    else:
        r = d.resample(rule, origin="epoch")
    out = r.agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    return out.dropna().reset_index()


def _paginate(src, sym, iv, n, budget_s) -> pd.DataFrame:
    """Download up to n candles from one source, walking backwards in time."""
    t0 = time.time()
    first = src["fetch"](sym, iv, min(n, src["page"]), None)
    if first.empty:
        raise DataError("no candles returned")
    frames, total = [first], len(first)
    while total < n and src["paginate"] and time.time() - t0 < budget_s:
        oldest = frames[-1]["time"].iloc[0]
        try:
            page = src["fetch"](sym, iv, min(n - total, src["page"]), int(oldest.timestamp() * 1000) - 1)
        except Exception:
            break
        page = page[page["time"] < oldest]
        if page.empty:
            break
        frames.append(page)
        total += len(page)
    df = pd.concat(frames).drop_duplicates("time").sort_values("time")
    return df.tail(n).reset_index(drop=True)


def load_klines(pair: str, interval: str, n: int, budget_s: float = 20.0):
    """Try every source in order. Returns (df, source_name, errors).

    For long downloads (more than LIVE_BARS candles, i.e. backtest history), a source
    that only has a short history (e.g. a coin listed recently on Binance but older
    on OKX) doesn't win automatically: up to 3 more sources are tried and the longest
    history is kept. If none has ~12 months, the longest available is used."""
    base, quote = parse_pair(pair)
    errors, best, extra_tries = [], None, 0
    want_long = n > LIVE_BARS
    for src in SOURCES:
        native, factor, rule = interval, 1, None
        if interval not in src["ivs"]:
            if interval in RESAMPLE_FROM and RESAMPLE_FROM[interval][0] in src["ivs"]:
                native, factor, rule = RESAMPLE_FROM[interval]
            else:
                continue
        for sym in src["syms"](base, quote):
            try:
                df = _paginate(src, sym, native, n * factor, budget_s)
                if rule:
                    df = _resample(df, rule).tail(n).reset_index(drop=True)
                if len(df) < 2:
                    raise DataError("too few candles")
                found = (df, f"{src['name']} ({sym})")
                if not want_long or len(df) >= 0.8 * n:
                    if best is None or len(df) >= len(best[0]):
                        return found[0], found[1], errors
                    return best[0], best[1], errors
                if best is None or len(df) > len(best[0]):
                    best = found
                extra_tries += 1
                break                   # try the next source
            except Exception as e:  # network error, unknown symbol, geo-block ...
                errors.append(f"{src['name']} {sym}: {str(e)[:120]}")
        if best is not None and extra_tries > 3:
            break
    if best is not None:
        return best[0], best[1], errors
    return None, None, errors


@st.cache_data(ttl=60, show_spinner=False)
def fetch_live(pair: str, interval: str, n: int = LIVE_BARS):
    """Recent candles for the live analysis/chart (cached 60 seconds)."""
    return load_klines(pair, interval, n, budget_s=15)


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_history(pair: str, interval: str, n: int):
    """Long history for the backtest (cached 30 minutes)."""
    return load_klines(pair, interval, n, budget_s=35)


def load_funding_rate(pair: str):
    """Latest perpetual-futures funding rate per 8h. Returns (rate, source, is_estimate)."""
    base, _ = parse_pair(pair)
    tries = [
        ("Binance Futures", "https://fapi.binance.com/fapi/v1/premiumIndex",
         {"symbol": f"{base}USDT"}, lambda j: j["lastFundingRate"]),
        ("Bybit", "https://api.bybit.com/v5/market/tickers",
         {"category": "linear", "symbol": f"{base}USDT"}, lambda j: j["result"]["list"][0]["fundingRate"]),
        ("OKX", "https://www.okx.com/api/v5/public/funding-rate",
         {"instId": f"{base}-USDT-SWAP"}, lambda j: j["data"][0]["fundingRate"]),
    ]
    for name, url, params, pick in tries:
        try:
            rate = float(pick(_get_json(url, params)))
            if math.isfinite(rate) and abs(rate) < 0.05:
                return rate, name, False
        except Exception:
            continue
    return DEFAULT_FUNDING_8H, "estimate (0.01% per 8h)", True


@st.cache_data(ttl=300, show_spinner=False)
def fetch_funding(pair: str):
    return load_funding_rate(pair)


# =============================================================================
# 3. INDICATORS  (pure pandas / numpy)
# =============================================================================

def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rma(s: pd.Series, n: int) -> pd.Series:
    """Wilder's smoothing (used by RSI, ATR, ADX)."""
    return s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    return rma(tr, n)


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = rma(delta.clip(lower=0), n)
    loss = rma(-delta.clip(upper=0), n)
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100.0).where(gain.notna())


def adx(df: pd.DataFrame, n: int = 14):
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr_n = atr(df, n)
    plus_di = 100 * rma(plus_dm, n) / tr_n
    minus_di = 100 * rma(minus_dm, n) / tr_n
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return rma(dx, n), plus_di, minus_di


def supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0):
    """Classic Supertrend. Returns (line, direction) where direction is +1 up / -1 down."""
    a = atr(df, period).to_numpy()
    h, l, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    hl2 = (h + l) / 2
    ub, lb = hl2 + mult * a, hl2 - mult * a
    n = len(df)
    fub, flb = np.full(n, np.nan), np.full(n, np.nan)
    line, direction = np.full(n, np.nan), np.zeros(n, dtype=int)
    for i in range(n):
        if np.isnan(a[i]):
            continue
        if i == 0 or np.isnan(fub[i - 1]):
            fub[i], flb[i], direction[i] = ub[i], lb[i], 1
            line[i] = flb[i]
            continue
        fub[i] = ub[i] if (ub[i] < fub[i - 1] or c[i - 1] > fub[i - 1]) else fub[i - 1]
        flb[i] = lb[i] if (lb[i] > flb[i - 1] or c[i - 1] < flb[i - 1]) else flb[i - 1]
        if direction[i - 1] == 1:
            direction[i] = -1 if c[i] < flb[i] else 1
        else:
            direction[i] = 1 if c[i] > fub[i] else -1
        line[i] = flb[i] if direction[i] == 1 else fub[i]
    return pd.Series(line, index=df.index), pd.Series(direction, index=df.index)


def add_indicators(df: pd.DataFrame, st_period: int = 10, st_mult: float = 3.0) -> pd.DataFrame:
    d = df.copy()
    for n in (9, 21, 50, 200):
        d[f"ema{n}"] = ema(d["close"], n)
    d["rsi"] = rsi(d["close"], 14)
    macd_line = ema(d["close"], 12) - ema(d["close"], 26)
    d["macd"] = macd_line
    d["macd_signal"] = macd_line.ewm(span=9, adjust=False).mean()
    d["macd_hist"] = d["macd"] - d["macd_signal"]
    d["atr"] = atr(d, 14)
    d["adx"], d["plus_di"], d["minus_di"] = adx(d, 14)
    d["vol_ratio"] = d["volume"] / d["volume"].rolling(20, min_periods=10).mean()
    d["st_line"], d["st_dir"] = supertrend(d, st_period, st_mult)
    return d


# =============================================================================
# 4. SCORING AND MULTI-TIMEFRAME ANALYSIS
# =============================================================================
# Each timeframe gets a score between -100 (very bearish) and +100 (very bullish):
#   Supertrend direction ............ +/-30  (the key verification indicator)
#   EMA trend (3 checks x 10) ....... +/-30
#   MACD histogram .................. +/-10
#   RSI momentum .................... +/-10
#   ADX trend strength (+DI vs -DI) . +/-10
#   Volume surge with candle colour . +/-10

def score_components(d: pd.DataFrame) -> pd.DataFrame:
    """Vectorised score for every candle (used both live and in the backtest)."""
    c = d["close"]
    out = pd.DataFrame(index=d.index)
    out["supertrend"] = 30 * d["st_dir"].astype(float)
    long_ema = d["ema200"].where(d["ema200"].notna(), d["ema50"])
    out["ema_long"] = 10 * np.sign(c - long_ema).fillna(0)
    out["ema_fast"] = 10 * np.sign(d["ema9"] - d["ema21"]).fillna(0)
    out["ema_mid"] = 10 * np.sign(d["ema21"] - d["ema50"]).fillna(0)
    out["macd"] = 10 * np.sign(d["macd_hist"]).fillna(0)
    r = d["rsi"]
    out["rsi"] = np.select([(r > 55) & (r < 75), (r < 45) & (r > 25)], [10.0, -10.0], 0.0)
    out["adx"] = np.where(d["adx"] >= 20, 10 * np.sign(d["plus_di"] - d["minus_di"]).fillna(0), 0.0)
    out["volume"] = np.where(d["vol_ratio"] >= 1.2, 10 * np.sign(c - d["open"]), 0.0)
    out["total"] = out.sum(axis=1).clip(-100, 100)
    return out


def _arrow(v):
    return "🟢" if v > 0 else ("🔴" if v < 0 else "⚪")


def explain_last(d: pd.DataFrame, comp: pd.DataFrame) -> list[dict]:
    """Plain-English explanation of each factor on the latest candle."""
    last, cl = d.iloc[-1], comp.iloc[-1]
    long_name = "EMA 200" if pd.notna(last["ema200"]) else "EMA 50 (not enough data for EMA 200)"
    rows = [
        dict(factor="Supertrend", pts=cl["supertrend"],
             text="Price is above the Supertrend line - main trend is UP." if last["st_dir"] > 0
             else "Price is below the Supertrend line - main trend is DOWN."),
        dict(factor=f"Price vs {long_name}", pts=cl["ema_long"],
             text="Price is above the long-term average (bullish)." if cl["ema_long"] > 0
             else "Price is below the long-term average (bearish)."),
        dict(factor="EMA 9 vs 21", pts=cl["ema_fast"],
             text="Short-term average is above the medium one (rising)." if cl["ema_fast"] > 0
             else "Short-term average is below the medium one (falling)."),
        dict(factor="EMA 21 vs 50", pts=cl["ema_mid"],
             text="Medium-term trend is up." if cl["ema_mid"] > 0 else "Medium-term trend is down."),
        dict(factor="MACD", pts=cl["macd"],
             text="Momentum is pushing up." if cl["macd"] > 0 else "Momentum is pushing down."),
    ]
    r = last["rsi"]
    if r >= 75:
        rt = f"RSI {r:.0f}: overbought - price may be stretched, no points."
    elif r <= 25:
        rt = f"RSI {r:.0f}: oversold - price may bounce, no points."
    elif cl["rsi"] > 0:
        rt = f"RSI {r:.0f}: healthy buying pressure."
    elif cl["rsi"] < 0:
        rt = f"RSI {r:.0f}: selling pressure."
    else:
        rt = f"RSI {r:.0f}: neutral."
    rows.append(dict(factor="RSI (14)", pts=cl["rsi"], text=rt))
    a = last["adx"]
    if pd.notna(a) and a >= 20:
        at = f"ADX {a:.0f}: a real trend is present ({'buyers' if cl['adx'] > 0 else 'sellers'} in control)."
    else:
        at = f"ADX {a:.0f}: weak/choppy market - no points." if pd.notna(a) else "ADX: not enough data."
    rows.append(dict(factor="ADX (trend strength)", pts=cl["adx"], text=at))
    vr = last["vol_ratio"]
    if pd.isna(vr):
        vt = "Volume: not enough data."
    elif cl["volume"] != 0:
        vt = f"Volume is {vr:.1f}x normal on a {'green' if cl['volume'] > 0 else 'red'} candle."
    else:
        vt = f"Volume is {vr:.1f}x normal - nothing special."
    rows.append(dict(factor="Volume", pts=cl["volume"], text=vt))
    return rows


def analyse_timeframe(df: pd.DataFrame, st_period: int, st_mult: float) -> dict:
    d = add_indicators(df, st_period, st_mult)
    comp = score_components(d)
    last = d.iloc[-1]
    return dict(data=d, comp=comp, score=float(comp["total"].iloc[-1]),
                st_dir=int(last["st_dir"]), close=float(last["close"]), atr=float(last["atr"]),
                rsi=float(last["rsi"]), adx=float(last["adx"]) if pd.notna(last["adx"]) else float("nan"),
                rows=explain_last(d, comp), bars=len(d), last_time=d["time"].iloc[-1])


def multi_timeframe(pair: str, style: str, st_period: int, st_mult: float, fetcher=None) -> dict:
    """Analyse each of the style's timeframes and combine with the style weights."""
    cfg = STYLES[style]
    fetcher = fetcher or fetch_live
    tfs, problems, sources = {}, [], set()
    for tf, w in zip(cfg["tfs"], cfg["weights"]):
        df, source, errs = fetcher(pair, tf, LIVE_BARS)
        if df is None:
            problems.append(f"{tf}: could not download data")
            continue
        if len(df) < MIN_BARS:
            problems.append(f"{tf}: only {len(df)} candles available (need {MIN_BARS})")
            continue
        res = analyse_timeframe(df, st_period, st_mult)
        res.update(weight=w, source=source)
        tfs[tf] = res
        sources.add(source)
    if cfg["primary"] not in tfs:
        raise DataError(
            f"Could not load enough {cfg['primary']} data for {pair.upper()}. "
            "Check the pair name (e.g. BTCUSDT) - it may not be listed on the free data sources, "
            "or the data sources may be busy. Tap Refresh prices to try again.")
    wsum = sum(r["weight"] for r in tfs.values())
    combined = sum(r["score"] * r["weight"] for r in tfs.values()) / wsum
    return dict(tfs=tfs, score=combined, problems=problems, sources=sorted(sources),
                primary=cfg["primary"], higher=cfg["tfs"][-1])


# =============================================================================
# 5. POSITION, LIQUIDATION AND COSTS
# =============================================================================

def liquidation_price(entry: float, leverage: float, direction: int, mmr: float = MAINT_MARGIN_RATE) -> float:
    """Approximate isolated-margin liquidation price.
    long:  entry x (1 - 1/leverage + mmr)
    short: entry x (1 + 1/leverage - mmr)
    Real exchanges differ slightly (tiers, fees), so treat it as an estimate."""
    if direction >= 0:
        return entry * (1 - 1 / leverage + mmr)
    return entry * (1 + 1 / leverage - mmr)


def max_safe_leverage(stop_pct: float, buffer: float = 1.25, mmr: float = MAINT_MARGIN_RATE) -> int:
    """Highest leverage whose liquidation sits comfortably (25%) beyond the stop."""
    if stop_pct <= 0:
        return 1
    return int(max(1, min(25, math.floor(1 / (stop_pct * buffer + mmr)))))


def compute_position(collateral: float, leverage: float, entry: float, atr_value: float,
                     direction: int, style: str, funding_rate: float) -> dict:
    cfg = STYLES[style]
    direction = direction if direction != 0 else 1
    notional = collateral * leverage
    qty = notional / entry
    # Stop = stop_atr x ATR, but never tighter than min_stop_pct (otherwise costs eat the trade).
    stop_dist = max(cfg["stop_atr"] * atr_value, cfg["min_stop_pct"] * entry)
    stop = entry - direction * stop_dist
    target = entry + direction * cfg["rr"] * stop_dist
    stop_pct = stop_dist / entry
    liq = liquidation_price(entry, leverage, direction)
    liq_pct = abs(entry - liq) / entry
    fees = notional * TAKER_FEE * 2
    slippage = notional * cfg["slippage"] * 2
    funding = notional * abs(funding_rate) * cfg["hold_hours"] / 8   # assume we pay (conservative)
    costs = fees + slippage + funding
    risk = notional * stop_pct + costs
    reward = notional * stop_pct * cfg["rr"] - costs
    return dict(
        direction=direction, notional=notional, qty=qty, entry=entry, stop=stop, target=target,
        stop_pct=stop_pct, target_pct=stop_pct * cfg["rr"], liq=liq, liq_pct=liq_pct,
        liq_before_stop=liq_pct <= stop_pct, liq_close=liq_pct < stop_pct * 1.25,
        fees=fees, slippage=slippage, funding=funding, costs=costs,
        costs_pct_collateral=costs / collateral * 100,
        risk=min(risk, collateral), risk_pct_collateral=min(risk, collateral) / collateral * 100,
        reward=reward, rr_after_costs=(reward / risk) if risk > 0 else 0.0,
        max_safe_lev=max_safe_leverage(stop_pct), hold_hours=cfg["hold_hours"],
    )


# =============================================================================
# 6. BACKTEST
# =============================================================================
# Rules (same ingredients as the live signal, on the style's primary timeframe):
#   LONG  when Supertrend flips up,   price is above EMA 50 and the score >= min_score
#   SHORT when Supertrend flips down, price is below EMA 50 and the score <= -min_score
#   Entry at the NEXT candle's open (+ slippage). Stop = stop_atr x ATR (at least min_stop_pct),
#   target = rr x stop distance.
#   Exit at stop, target, liquidation, an opposite Supertrend flip, or after max_hold_bars.
#   If stop and target are both touched in one candle we assume the stop (conservative).
#   Costs: taker fee both ways, slippage both ways, funding per 8h held. Leverage applied.
#   Equity compounds: each trade uses the whole account at the chosen leverage.

def run_backtest(df: pd.DataFrame, style: str, leverage: float, funding_rate: float,
                 st_period: int, st_mult: float) -> dict:
    cfg = STYLES[style]
    d = add_indicators(df, st_period, st_mult)
    score = score_components(d)["total"].to_numpy()
    o, h, l, c = (d[k].to_numpy() for k in ("open", "high", "low", "close"))
    a, sdir, e50 = d["atr"].to_numpy(), d["st_dir"].to_numpy(), d["ema50"].to_numpy()
    times = d["time"]
    hours_per_bar = INTERVAL_SECONDS[cfg["primary"]] / 3600
    slip, n = cfg["slippage"], len(d)
    start = 210 if n > 500 else MIN_BARS
    trades, i = [], start
    while i < n - 2:
        sig = 0
        if sdir[i] == 1 and sdir[i - 1] == -1 and c[i] > e50[i] and score[i] >= cfg["min_score"]:
            sig = 1
        elif sdir[i] == -1 and sdir[i - 1] == 1 and c[i] < e50[i] and score[i] <= -cfg["min_score"]:
            sig = -1
        if sig == 0 or not np.isfinite(a[i]) or not np.isfinite(e50[i]):
            i += 1
            continue
        k = i + 1
        entry = o[k] * (1 + sig * slip)
        stop_dist = max(cfg["stop_atr"] * a[i], cfg["min_stop_pct"] * entry)
        stop = entry - sig * stop_dist
        target = entry + sig * cfg["rr"] * stop_dist
        liq = liquidation_price(entry, leverage, sig)
        adverse = max(stop, liq) if sig == 1 else min(stop, liq)    # first bad level reached
        adverse_is_liq = (liq >= stop) if sig == 1 else (liq <= stop)
        last_bar = min(n - 1, k + cfg["max_hold_bars"] - 1)
        j, exit_px, reason = k, None, None
        while True:
            hit_bad = l[j] <= adverse if sig == 1 else h[j] >= adverse
            hit_good = h[j] >= target if sig == 1 else l[j] <= target
            if hit_bad:
                gap = o[j] < adverse if sig == 1 else o[j] > adverse
                exit_px = o[j] if (gap and j > k) else adverse
                reason = "liquidated" if adverse_is_liq else "stop"
                break
            if hit_good:
                exit_px, reason = target, "target"
                break
            if sdir[j] == -sig:
                exit_px, reason = c[j], "trend flip"
                break
            if j >= last_bar:
                exit_px, reason = c[j], "time limit"
                break
            j += 1
        bars_held = j - k + 1
        risk_pct = stop_dist / entry
        if reason == "liquidated":
            ret = -1.0
        else:
            px = exit_px * (1 - sig * slip)
            gross = sig * (px - entry) / entry
            funding = abs(funding_rate) * bars_held * hours_per_bar / 8
            ret = max((gross - 2 * TAKER_FEE - funding) * leverage, -1.0)
        trades.append(dict(entry_time=times.iloc[k], exit_time=times.iloc[j],
                           side="LONG" if sig == 1 else "SHORT", entry=entry, exit=exit_px,
                           reason=reason, bars=bars_held, ret=ret,
                           r=ret / (leverage * risk_pct) if risk_pct > 0 else 0.0))
        i = j + 1

    period_start = times.iloc[start] if n > start else times.iloc[0]
    res = dict(n_candles=max(n - start, 0), start=period_start, end=times.iloc[-1],
               days=(times.iloc[-1] - period_start).total_seconds() / 86400,
               trades=pd.DataFrame(trades), n=len(trades))
    if not trades:
        res.update(win_rate=0.0, avg_win=0.0, avg_loss=0.0, exp_r=0.0, exp_pct=0.0, pf=0.0,
                   max_dd=0.0, total_ret=0.0, liquidations=0, wiped=False, long_n=0, short_n=0,
                   equity=pd.DataFrame({"time": [period_start], "equity": [100.0]}),
                   sides={1: side_stats(None), -1: side_stats(None)})
        return res
    t = res["trades"]
    wins, losses = t[t["ret"] > 0], t[t["ret"] <= 0]
    eq, curve, wiped = 100.0, [100.0], False
    for r_ in t["ret"]:
        eq = max(eq * (1 + r_), 0.0)
        curve.append(eq)
        if eq <= 0:
            wiped = True
            break
    curve = np.array(curve)
    peak = np.maximum.accumulate(curve)
    dd = np.where(peak > 0, (peak - curve) / peak, 0)
    eq_times = [period_start] + list(t["exit_time"].iloc[: len(curve) - 1])
    loss_sum = losses["ret"].sum()
    res.update(
        win_rate=len(wins) / len(t) * 100,
        avg_win=wins["ret"].mean() * 100 if len(wins) else 0.0,
        avg_loss=losses["ret"].mean() * 100 if len(losses) else 0.0,
        exp_r=float(t["r"].mean()), exp_pct=float(t["ret"].mean() * 100),
        pf=float(wins["ret"].sum() / abs(loss_sum)) if loss_sum != 0 else float("inf"),
        max_dd=float(dd.max() * 100), total_ret=float(curve[-1] - 100),
        liquidations=int((t["reason"] == "liquidated").sum()), wiped=wiped,
        equity=pd.DataFrame({"time": eq_times, "equity": curve}),
        long_n=int((t["side"] == "LONG").sum()), short_n=int((t["side"] == "SHORT").sum()),
    )
    # Separate stats for long-only and short-only trades (used by each side's decision).
    res["sides"] = {1: side_stats(t[t["side"] == "LONG"]), -1: side_stats(t[t["side"] == "SHORT"])}
    return res


# =============================================================================
# 7. DECISION LOGIC
# =============================================================================

def side_stats(trades: pd.DataFrame | None) -> dict:
    """Win rate / expectancy for a set of backtest trades (used per direction)."""
    if trades is None or len(trades) == 0:
        return dict(n=0, win_rate=0.0, exp_r=0.0, exp_pct=0.0, pf=0.0, avg_win=0.0, avg_loss=0.0)
    w, lo = trades[trades["ret"] > 0], trades[trades["ret"] <= 0]
    ls = lo["ret"].sum()
    return dict(n=len(trades), win_rate=len(w) / len(trades) * 100,
                exp_r=float(trades["r"].mean()), exp_pct=float(trades["ret"].mean() * 100),
                pf=float(w["ret"].sum() / abs(ls)) if ls != 0 else float("inf"),
                avg_win=float(w["ret"].mean() * 100) if len(w) else 0.0,
                avg_loss=float(lo["ret"].mean() * 100) if len(lo) else 0.0)


def decide(mtf: dict, style: str, leverage: float, position: dict, bt: dict | None,
           learn_mult: float, direction: int = 1) -> dict:
    """Evaluate ONE side (direction +1 = LONG, -1 = SHORT) and return its
    GO / NO-GO verdict + confidence. Call it once per side.

    position must be the position worked out for that same direction, and the
    backtest stats used are the trades of that direction only (bt["sides"])."""
    cfg = STYLES[style]
    direction = 1 if direction >= 0 else -1
    word = "LONG" if direction == 1 else "SHORT"
    score = mtf["score"]
    aligned = score * direction          # positive = the charts support this side
    reasons, blockers = [], []
    prim, high = mtf["tfs"][mtf["primary"]], mtf["tfs"].get(mtf["higher"])

    conf = min(92.0, max(1.0, 40 + aligned * 0.55))
    if aligned > 0:
        reasons.append(f"Charts lean towards a {word} (score {score:+.0f} out of 100).")
    if aligned < cfg["min_score"]:
        if aligned <= -cfg["min_score"]:
            blockers.append(f"Charts point the other way (score {score:+.0f}).")
        else:
            blockers.append(f"Signals are mixed (score {score:+.0f}; a {word} needs "
                            f"{'+' if direction == 1 else '-'}{cfg['min_score']} or more).")

    # --- Supertrend verification gate (for this side) ---
    if prim["st_dir"] == direction:
        reasons.append(f"✅ Main {mtf['primary']} Supertrend agrees.")
    else:
        blockers.append(f"Main {mtf['primary']} Supertrend disagrees.")
    if high is not None and mtf["higher"] != mtf["primary"]:
        if high["st_dir"] != direction and high["score"] * direction <= -40:
            blockers.append(f"Bigger {mtf['higher']} chart strongly points the other way.")
        elif high["st_dir"] != direction:
            conf *= 0.85
            reasons.append(f"⚠️ Bigger {mtf['higher']} Supertrend disagrees (confidence reduced).")
        else:
            reasons.append(f"✅ Bigger {mtf['higher']} Supertrend agrees too.")

    # --- backtest feedback, using only this side's past trades ---
    s = None
    if bt is not None:
        s = (bt.get("sides") or {}).get(direction) or bt
    if s is None:
        conf *= 0.8
        reasons.append("⚠️ Backtest could not run (confidence reduced).")
    elif s["n"] < 10:
        conf *= 0.85 if s["exp_r"] > 0 else 0.7
        reasons.append(f"⚠️ Backtest had only {s['n']} {word} trades - low reliability.")
    elif s["exp_r"] <= 0 or s["exp_pct"] <= 0 or (bt or {}).get("wiped"):
        blockers.append(f"Past {word} trades lost money after costs ({s['exp_r']:+.2f}R, "
                        f"{s['exp_pct']:+.2f}% per trade, {s['n']} trades).")
    elif s["exp_r"] < 0.1:
        conf *= 0.85
        reasons.append(f"⚠️ Past {word} trades only just made money ({s['exp_r']:+.2f}R) - confidence reduced.")
    else:
        if s["exp_r"] >= 0.25 and s["pf"] > 1.3:
            conf *= 1.05
        reasons.append(f"✅ Past {word} trades made {s['exp_r']:+.2f}R each after costs "
                       f"({s['n']} trades, {s['win_rate']:.0f}% wins).")

    # --- leverage / liquidation checks (this side's position) ---
    lo, hi = cfg["lev_range"]
    if leverage > hi:
        conf *= 0.9
        reasons.append(f"⚠️ {leverage:.0f}x is above the {lo}-{hi}x range for {style} (confidence reduced).")
    if position["liq_before_stop"]:
        blockers.append(f"At {leverage:.0f}x you'd be LIQUIDATED before the stop. "
                        f"Use {position['max_safe_lev']}x or less.")
    elif position["liq_close"]:
        conf *= 0.85
        reasons.append(f"⚠️ Liquidation is close to the stop. Safer: {position['max_safe_lev']}x or less.")
    if position["rr_after_costs"] < 1.0:
        blockers.append(f"After costs the reward is smaller than the risk (R:R {position['rr_after_costs']:.2f}).")

    # --- self-learning ---
    if abs(learn_mult - 1) > 0.005:
        conf *= learn_mult
        reasons.append(f"🧠 Your logged results adjust confidence x{learn_mult:.2f}.")

    conf = float(max(1.0, min(95.0, conf)))
    if not blockers and conf < MIN_CONFIDENCE_FOR_GO:
        blockers.append(f"Confidence {conf:.0f}% is below the {MIN_CONFIDENCE_FOR_GO}% needed for a GO.")
    if blockers:
        conf = min(conf, 49.0)            # a NO-GO never shows more than 49%
        verdict = "NO-GO"
    else:
        verdict = f"GO {word}"
    return dict(verdict=verdict, direction=direction, side=word, confidence=round(conf),
                reasons=reasons, blockers=blockers, score=score, aligned=aligned)


DIRECTION_MODES = ["Auto (best side)", "Long", "Short"]


def pick_side(decisions: dict, mode: str) -> int:
    """Which side the main banner shows.
    Long / Short -> that side. Auto -> the side with the higher confidence
    (on a tie a GO wins; if both are NO-GO the banner simply says NO-GO)."""
    if mode == "Long":
        return 1
    if mode == "Short":
        return -1
    lg, sh = decisions[1], decisions[-1]
    key = lambda d: (d["confidence"], d["verdict"] != "NO-GO", d["aligned"])
    return 1 if key(lg) >= key(sh) else -1


# =============================================================================
# 8. LEARNING LOG
# =============================================================================
# Results are saved to learning_log.json next to app.py. On Streamlit Community
# Cloud this file is WIPED whenever the app restarts, sleeps or is redeployed,
# so the app also offers CSV download / upload as a backup.

LOG_COLUMNS = ["date", "pair", "style", "direction", "confidence", "outcome"]


def load_log(path: Path = LEARNING_LOG_PATH) -> list[dict]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return [e for e in data if isinstance(e, dict) and e.get("outcome") in ("Win", "Loss")]
    except Exception:
        pass
    return []


def save_log(entries: list[dict], path: Path = LEARNING_LOG_PATH) -> bool:
    try:
        path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def log_to_df(entries: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(entries, columns=LOG_COLUMNS) if entries else pd.DataFrame(columns=LOG_COLUMNS)


def csv_to_entries(raw: bytes) -> list[dict]:
    df = pd.read_csv(io.BytesIO(raw))
    df.columns = [str(c).strip().lower() for c in df.columns]
    if not {"pair", "style", "outcome"}.issubset(df.columns):
        raise ValueError("CSV needs at least the columns pair, style and outcome.")
    out = []
    for _, r in df.iterrows():
        outcome = str(r.get("outcome", "")).strip().title()
        if outcome not in ("Win", "Loss"):
            continue
        conf = pd.to_numeric(r.get("confidence"), errors="coerce")
        out.append(dict(date=str(r.get("date", "")), pair=str(r["pair"]).upper(),
                        style=str(r["style"]).strip().title(), direction=str(r.get("direction", "")),
                        confidence=float(conf) if pd.notna(conf) else None, outcome=outcome))
    return out


def _bayes_wr(wins: int, n: int, prior: float = 0.5, strength: float = 10.0) -> float:
    """Win rate shrunk towards 50% (Beta prior worth 10 trades)."""
    return (wins + prior * strength) / (n + strength)


def learning_multiplier(entries: list[dict], style: str, pair: str,
                        direction: int | None = None) -> tuple[float, str]:
    """Bounded confidence multiplier (0.80 - 1.15) from your logged results.
    Uses the style's results; blends in that pair (5+ results) and, when a
    direction is given, that direction's results for the style (5+ results)."""
    pair = re.sub(r"[^A-Z0-9]", "", pair.upper())
    s = [e for e in entries if e.get("style") == style]
    if not s:
        return 1.0, f"No {style} results logged yet - no adjustment."
    rates = [_bayes_wr(sum(e["outcome"] == "Win" for e in s), len(s))]
    note = f"{style}: {len(s)} results, adjusted win rate {rates[0] * 100:.0f}%"
    p = [e for e in s if e.get("pair") == pair]
    if len(p) >= 5:
        rates.append(_bayes_wr(sum(e["outcome"] == "Win" for e in p), len(p)))
        note += f"; {pair}: {len(p)} results, adjusted {rates[-1] * 100:.0f}%"
    if direction in (1, -1):
        word = "LONG" if direction == 1 else "SHORT"
        dd = [e for e in s if str(e.get("direction", "")).upper() == word]
        if len(dd) >= 5:
            rates.append(_bayes_wr(sum(e["outcome"] == "Win" for e in dd), len(dd)))
            note += f"; {word}: {len(dd)} results, adjusted {rates[-1] * 100:.0f}%"
    wr = sum(rates) / len(rates)
    mult = float(min(1.15, max(0.80, 1 + (wr - 0.5))))
    return mult, note + f" -> x{mult:.2f}"


# =============================================================================
# 9. CHARTS
# =============================================================================

# Colours and chart theme shared with the iOS-style interface.
IOS = dict(green="#34C759", red="#FF3B30", orange="#FF9500", blue="#0A84FF", yellow="#FFD60A",
           purple="#BF5AF2", teal="#64D2FF", text="#F5F5F7", muted="#8E8E93", grid="rgba(255,255,255,0.06)")
FONT_STACK = '-apple-system, BlinkMacSystemFont, "SF Pro Display", "Inter", "Segoe UI", Roboto, sans-serif'


def _chart_layout(fig: go.Figure, height: int) -> None:
    """Clean, transparent Plotly look that blends into the app."""
    fig.update_layout(
        template="plotly_dark", height=height, margin=dict(l=0, r=0, t=8, b=0),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=FONT_STACK, size=11, color=IOS["muted"]),
        hoverlabel=dict(font=dict(family=FONT_STACK)), dragmode="pan",
    )
    fig.update_xaxes(showgrid=False, zeroline=False, showline=False, tickfont=dict(size=10))
    fig.update_yaxes(gridcolor=IOS["grid"], gridwidth=1, zeroline=False, showline=False,
                     side="right", tickfont=dict(size=10))


def to_local(ts: pd.Series) -> pd.Series:
    return ts.dt.tz_convert(LOCAL_TZ)


def price_chart(d: pd.DataFrame, pair: str, tf: str, pos: dict | None = None) -> go.Figure:
    """Candles + Supertrend + EMAs, volume and RSI. If pos is given, the entry, stop,
    target (and liquidation, when it is inside the visible range) are drawn as lines."""
    d = d.tail(CHART_BARS).copy()
    x = to_local(d["time"])
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.03,
                        row_heights=[0.62, 0.16, 0.22])
    fig.add_trace(go.Candlestick(x=x, open=d["open"], high=d["high"], low=d["low"], close=d["close"],
                                 name="Price", increasing_line_color=IOS["green"], increasing_fillcolor=IOS["green"],
                                 decreasing_line_color=IOS["red"], decreasing_fillcolor=IOS["red"],
                                 line=dict(width=1), showlegend=False), row=1, col=1)
    up = d["st_line"].where(d["st_dir"] == 1)
    dn = d["st_line"].where(d["st_dir"] == -1)
    fig.add_trace(go.Scatter(x=x, y=up, mode="lines", name="Supertrend up",
                             line=dict(color=IOS["green"], width=2)), row=1, col=1)
    fig.add_trace(go.Scatter(x=x, y=dn, mode="lines", name="Supertrend down",
                             line=dict(color=IOS["red"], width=2)), row=1, col=1)
    for n, col in ((9, IOS["yellow"]), (21, IOS["teal"]), (50, IOS["purple"]), (200, IOS["orange"])):
        if d[f"ema{n}"].notna().any():
            fig.add_trace(go.Scatter(x=x, y=d[f"ema{n}"], mode="lines", name=f"EMA {n}",
                                     line=dict(color=col, width=1), opacity=0.85), row=1, col=1)
    vol_col = np.where(d["close"] >= d["open"], "rgba(52,199,89,0.45)", "rgba(255,59,48,0.45)")
    fig.add_trace(go.Bar(x=x, y=d["volume"], marker_color=vol_col, name="Volume", showlegend=False),
                  row=2, col=1)
    fig.add_trace(go.Scatter(x=x, y=d["rsi"], mode="lines", name="RSI",
                             line=dict(color=IOS["blue"], width=1.2), showlegend=False), row=3, col=1)
    for lvl in (70, 30):
        fig.add_hline(y=lvl, line=dict(color=IOS["muted"], width=0.8, dash="dot"), row=3, col=1)
    if pos:
        side = "LONG" if pos["direction"] == 1 else "SHORT"
        lo_vis, hi_vis = float(d["low"].min()), float(d["high"].max())
        span = hi_vis - lo_vis
        levels = [("Entry", pos["entry"], IOS["text"], "dot"), ("Stop", pos["stop"], IOS["red"], "dash"),
                  ("Target", pos["target"], IOS["green"], "dash")]
        if lo_vis - span * 0.3 <= pos["liq"] <= hi_vis + span * 0.3:   # only if it won't squash the chart
            levels.append(("Liq.", pos["liq"], IOS["orange"], "dashdot"))
        for name, y, col, dash in levels:
            fig.add_hline(y=y, line=dict(color=col, width=1.2, dash=dash), row=1, col=1,
                          annotation_text=f"{name} {fmt_price(y)}", annotation_position="top left",
                          annotation_font=dict(size=9, color=col), annotation_bgcolor="rgba(0,0,0,0.55)")
    _chart_layout(fig, 440)
    fig.update_layout(
        xaxis_rangeslider_visible=False, hovermode="x unified", showlegend=False,
        margin=dict(l=0, r=0, t=4, b=0),
    )
    fig.update_yaxes(showticklabels=False, row=2, col=1)
    fig.update_yaxes(range=[0, 100], tickvals=[30, 70], row=3, col=1)
    return fig


def equity_chart(bt: dict) -> go.Figure:
    e = bt["equity"]
    up = e["equity"].iloc[-1] >= 100
    col = IOS["green"] if up else IOS["red"]
    fill = "rgba(52,199,89,0.12)" if up else "rgba(255,59,48,0.12)"
    fig = go.Figure(go.Scatter(x=to_local(pd.to_datetime(e["time"], utc=True)), y=e["equity"],
                               mode="lines", line=dict(color=col, width=2), fill="tozeroy",
                               fillcolor=fill, name="Account"))
    fig.add_hline(y=100, line=dict(color=IOS["muted"], width=0.8, dash="dot"))
    _chart_layout(fig, 240)
    lo_, hi_ = float(e["equity"].min()), float(e["equity"].max())
    pad = max((hi_ - lo_) * 0.1, 2)
    fig.update_layout(showlegend=False)
    fig.update_yaxes(range=[max(0, lo_ - pad), hi_ + pad])
    return fig


# =============================================================================
# 9b. MULTI-PAIR SCANNER CORE  (used by scanner.py and the Scan tab)
# =============================================================================
# Evaluates long AND short for each pair and style with exactly the same
# multi_timeframe() -> compute_position() -> run_backtest() -> decide() chain
# as the main screen. Each timeframe is downloaded once per pair and reused
# across styles (e.g. 4h is used by both Day and Swing).

SCAN_PAIRS = list(POPULAR_PAIRS)   # 28 pairs
SCAN_PAIRS_CLOUD = 12          # the Scan tab checks only the first 12 by default (keeps it quick)


def default_leverage(style: str) -> int:
    """Middle of the style's suggested range (Swing 3x, Day 5x, Scalp 10x)."""
    lo, hi = STYLES[style]["lev_range"]
    return int((lo + hi) // 2)


def _plain_reasons(dec: dict, k: int = 2) -> list[str]:
    return [t for _, t in _reason_items(dec)][:k]


def _side_summary(dec: dict, pos: dict, bt: dict | None) -> dict:
    s = (bt or {}).get("sides", {}).get(dec["direction"]) if bt else None
    return dict(verdict=dec["verdict"], confidence=dec["confidence"], entry=pos["entry"], stop=pos["stop"],
                target=pos["target"], liquidation=pos["liq"], rr_after_costs=round(pos["rr_after_costs"], 2),
                liq_before_stop=bool(pos["liq_before_stop"]), max_safe_leverage=pos["max_safe_lev"],
                reasons=_plain_reasons(dec), blockers=[t for k_, t in _reason_items(dec) if k_ == "bad"],
                bt_n=int(s["n"]) if s else 0, bt_exp_r=round(float(s["exp_r"]), 3) if s else None,
                bt_exp_pct=round(float(s["exp_pct"]), 3) if s else None, bt_win_rate=round(float(s["win_rate"]), 1) if s else None)


def scan_pair(pair: str, styles: list[str], collateral: float = 100.0, entries: list[dict] | None = None,
              pause: float = 0.05) -> list[dict]:
    """Scan one pair for the given styles. Never raises: failures come back as ok=False."""
    entries = entries or []
    need: dict[str, int] = {}
    for s_ in styles:
        cfg = STYLES[s_]
        for tf in cfg["tfs"]:
            need[tf] = max(need.get(tf, 0), LIVE_BARS)
        need[cfg["primary"]] = max(need[cfg["primary"]], cfg["bt_candles"])
    data: dict[str, tuple] = {}
    for tf, n in need.items():
        df, src = None, None
        for attempt in range(2):                      # one polite retry
            try:
                df, src, _ = load_klines(pair, tf, n, budget_s=35 if n > LIVE_BARS else 15)
            except Exception:
                df = None
            if df is not None:
                break
            time.sleep(1.0)
        data[tf] = (df, src)
        time.sleep(pause)
    try:
        funding = load_funding_rate(pair)
    except Exception:
        funding = (DEFAULT_FUNDING_8H, "estimate (0.01% per 8h)", True)

    def fetcher(p, tf, n):                            # same shape as fetch_live()
        df, src = data.get(tf, (None, None))
        return (df.tail(n).reset_index(drop=True) if df is not None else None), src, []

    out = []
    for s_ in styles:
        cfg, lev = STYLES[s_], default_leverage(s_)
        base = dict(pair=pair, style=s_, leverage=lev, collateral=collateral)
        try:
            mtf = multi_timeframe(pair, s_, cfg["st_period"], cfg["st_mult"], fetcher=fetcher)
            prim = mtf["tfs"][mtf["primary"]]
            pos = {d: compute_position(collateral, lev, prim["close"], prim["atr"], d, s_, funding[0])
                   for d in (1, -1)}
            bt, bt_note = None, None
            hist, bt_src = data[cfg["primary"]]
            if hist is None or len(hist) < 300:
                bt_note = "not enough history"
            else:
                try:
                    bt = run_backtest(hist.tail(cfg["bt_candles"]).reset_index(drop=True), s_, lev,
                                      funding[0], cfg["st_period"], cfg["st_mult"])
                except Exception as e:
                    bt_note = f"backtest failed ({type(e).__name__})"
            decs = {}
            for d in (1, -1):
                mult, _ = learning_multiplier(entries, s_, pair, d)
                decs[d] = decide(mtf, s_, lev, pos[d], bt, mult, d)
            best = pick_side(decs, DIRECTION_MODES[0])
            last_t = prim["last_time"]
            base.update(
                ok=True, score=round(mtf["score"], 1), price=prim["close"],
                source=", ".join(mtf["sources"]), primary_tf=mtf["primary"],
                candle_time_utc=last_t.isoformat(),
                candle_time_local=f"{last_t.tz_convert(LOCAL_TZ):%d %b %H:%M} {LOCAL_TZ_LABEL}",
                funding_rate=funding[0], funding_estimated=bool(funding[2]), bt_note=bt_note,
                bt_days=round(bt["days"]) if bt else None, bt_candles=bt["n_candles"] if bt else 0,
                bt_source=(bt_src or ""),
                status=decs[best]["verdict"] if decs[best]["verdict"] != "NO-GO" else "NO-GO",
                best_side=decs[best]["side"], best_confidence=decs[best]["confidence"],
                long=_side_summary(decs[1], pos[1], bt), short=_side_summary(decs[-1], pos[-1], bt),
                problems=mtf["problems"],
            )
        except Exception as e:
            base.update(ok=False, error=str(e)[:200])
        out.append(base)
    return out


def scan_pairs(pairs: list[str], styles: list[str], collateral: float = 100.0,
               entries: list[dict] | None = None, workers: int = 4, progress=None) -> dict:
    """Scan many pairs (a few at a time, to be polite to the free APIs)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    t0 = time.time()
    results, done = [], 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(scan_pair, p, styles, collateral, entries): p for p in pairs}
        for f in as_completed(futs):
            try:
                results.extend(f.result())
            except Exception as e:
                results.extend(dict(pair=futs[f], style=s_, ok=False, error=str(e)[:200]) for s_ in styles)
            done += 1
            if progress:
                progress(done, len(pairs))
    order = {p: i for i, p in enumerate(pairs)}
    results.sort(key=lambda r: (order.get(r["pair"], 999), list(STYLES).index(r["style"])))
    skipped = sorted({r["pair"] for r in results if not r["ok"]} - {r["pair"] for r in results if r["ok"]},
                     key=lambda p: order.get(p, 999))
    return dict(results=results, skipped=skipped, seconds=round(time.time() - t0, 1),
                errors={r["pair"] + "/" + r["style"]: r.get("error") for r in results if not r["ok"]})



# =============================================================================
# 10. USER INTERFACE  (iOS-style, phone-first)
# =============================================================================
# The home screen is deliberately simple:
#   header -> settings card -> big decision card -> Long / Short mini cards
#   -> 4 key numbers -> tabs (Chart, Details, Backtest, Journal).

CSS = """
<style>
:root {--bg:#000000; --card:#1C1C1E; --card2:#2C2C2E; --sep:rgba(84,84,88,0.45);
       --text:#F5F5F7; --muted:#8E8E93; --green:#34C759; --red:#FF3B30; --orange:#FF9500; --blue:#0A84FF;}
/* hide Streamlit's own header, menu, toolbar, footer and deploy button */
header[data-testid="stHeader"], #MainMenu, footer, .stDeployButton, [data-testid="stToolbar"],
[data-testid="stDecoration"], [data-testid="stStatusWidget"], .viewerBadge_container__1QSob {display:none !important;}
.stApp {background:var(--bg); font-family:__FONT__;}
.stApp p, .stApp li, .stApp label, .stApp input, .stApp textarea, .stApp button, .stApp summary,
.stApp h1, .stApp h2, .stApp h3 {font-family:__FONT__;}
.block-container {max-width:500px !important; padding:0.8rem 1rem 4rem 1rem !important;}
/* header */
.appbar {display:flex; align-items:center; justify-content:space-between; gap:8px; margin-top:0.3rem;}
.appbar .title {font-size:1.5rem; font-weight:700; letter-spacing:-0.02em; color:var(--text);}
.pill {font-size:0.68rem; font-weight:600; color:var(--orange); border:1px solid rgba(255,149,0,0.4);
       background:rgba(255,149,0,0.10); border-radius:999px; padding:3px 10px; white-space:nowrap;}
/* cards */
.card {background:var(--card); border-radius:18px; padding:16px 16px; border:1px solid rgba(255,255,255,0.06);
       box-shadow:0 1px 3px rgba(0,0,0,0.5);}
.hero {display:flex; align-items:center; justify-content:space-between; gap:14px;}
.hero.go {border-color:rgba(52,199,89,0.35);} .hero.nogo {border-color:rgba(255,59,48,0.30);}
.eyebrow {font-size:0.72rem; color:var(--muted); font-weight:600; letter-spacing:0.07em; text-transform:uppercase;}
.hero .verdict {font-size:2.15rem; font-weight:800; letter-spacing:-0.02em; line-height:1.08; margin:3px 0 6px;}
.hero .summary {font-size:0.86rem; color:var(--text); opacity:0.8; line-height:1.35;}
.ring {flex:0 0 86px; position:relative; width:86px; height:86px;}
.ring svg {transform:rotate(-90deg);}
.ring .pct {position:absolute; top:0; left:0; right:0; bottom:0; display:flex; flex-direction:column;
            align-items:center; justify-content:center; font-size:1.3rem; font-weight:700; color:var(--text);}
.ring .pct small {font-size:0.58rem; color:var(--muted); font-weight:500; margin-top:-2px;}
.two {display:grid; grid-template-columns:1fr 1fr; gap:10px;}
.mini {background:var(--card); border-radius:16px; padding:12px 14px; border:1px solid rgba(255,255,255,0.06);}
.mini.picked {border-color:rgba(10,132,255,0.8);}
.mini .v {font-size:1.05rem; font-weight:700; margin:3px 0 9px;}
.bar {height:6px; background:var(--card2); border-radius:3px; overflow:hidden;}
.bar > div {height:100%; border-radius:3px;}
.mini .c {font-size:0.78rem; color:var(--muted); margin-top:6px;}
.tile {background:var(--card); border-radius:16px; padding:12px 14px; border:1px solid rgba(255,255,255,0.06);}
.tile .k {font-size:0.72rem; color:var(--muted); font-weight:500;}
.tile .val {font-size:1.2rem; font-weight:650; color:var(--text); margin-top:2px; letter-spacing:-0.01em;
            white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
.tile .s {font-size:0.72rem; color:var(--muted); margin-top:1px;}
.reasons {list-style:none; padding:0; margin:0;}
.reasons li {display:flex; gap:10px; align-items:flex-start; font-size:0.87rem; line-height:1.38; padding:8px 0;
             border-bottom:1px solid var(--sep); color:var(--text);}
.reasons li:first-child {padding-top:0;} .reasons li:last-child {border-bottom:none; padding-bottom:0;}
.dot {flex:0 0 8px; height:8px; border-radius:50%; margin-top:6px;}
.note {font-size:0.78rem; color:var(--muted); line-height:1.45;}
.warnline {font-size:0.82rem; color:var(--orange); line-height:1.4;}
.section {font-size:0.72rem; color:var(--muted); font-weight:600; letter-spacing:0.07em; text-transform:uppercase;
          margin:10px 2px -4px 2px;}
.disc {background:rgba(255,59,48,0.08); border:1px solid rgba(255,59,48,0.3); border-radius:14px; padding:12px 14px;
       font-size:0.85rem; line-height:1.45; color:var(--text);}
/* expanders as rounded cards */
[data-testid="stExpander"] details {background:var(--card); border-radius:16px !important;
                                    border:1px solid rgba(255,255,255,0.06) !important;}
[data-testid="stExpander"] summary p {font-weight:600; font-size:0.92rem;}
/* tabs look like an iOS segmented control */
[data-baseweb="tab-list"] {background:var(--card); border-radius:12px; padding:3px; gap:2px;}
[data-baseweb="tab-list"] button {flex:1 1 0; border-radius:9px; padding:7px 2px !important; margin:0 !important;}
[data-baseweb="tab-list"] button[aria-selected="true"] {background:var(--card2);}
[data-baseweb="tab-list"] button p {font-size:0.84rem; font-weight:600;}
[data-baseweb="tab-highlight"], [data-baseweb="tab-border"] {display:none !important;}
[role="tablist"] {background:var(--card); border-radius:12px; padding:3px; gap:2px; border:none !important;
                  box-shadow:none !important; display:flex; width:100%;}
[data-testid="stTab"] {flex:1 1 0; display:flex; justify-content:center; border-radius:9px;
                       padding:7px 2px !important; margin:0 !important;}
[data-testid="stTab"][aria-selected="true"] {background:var(--card2);}
[data-testid="stTab"] p {font-size:0.85rem; font-weight:600; color:var(--text);}
[data-testid="stTab"][aria-selected="false"] p {color:var(--muted);}
.react-aria-SelectionIndicator {display:none !important;}
.reasons li {margin-left:0 !important;}
.rows {background:var(--card); border-radius:16px; border:1px solid rgba(255,255,255,0.06); padding:2px 14px;}
.row {display:flex; justify-content:space-between; align-items:center; gap:12px; padding:10px 0;
      border-bottom:1px solid var(--sep);}
.row:last-child {border-bottom:none;}
.row .l {font-size:0.88rem; color:var(--text); line-height:1.25;}
.row .l small {display:block; color:var(--muted); font-size:0.72rem; margin-top:1px;}
.row .r {font-size:0.92rem; font-weight:600; color:var(--text); white-space:nowrap; text-align:right;}
.legend {display:flex; flex-wrap:wrap; gap:4px 12px; font-size:0.75rem; color:var(--muted); margin-bottom:-8px;}
.legend span b {display:inline-block; width:12px; height:3px; border-radius:2px; vertical-align:middle; margin-right:5px;}
/* buttons and forms */
.stButton button, .stDownloadButton button, .stFormSubmitButton button {border-radius:12px; font-weight:600;}
.stFormSubmitButton button {width:100%; padding:0.55rem;}
[data-testid="stFormSubmitButton"] button, [data-testid="stFormSubmitButton"] {width:100% !important;}
[data-testid="stFormSubmitButton"] button {padding:0.55rem;}
[data-testid="stForm"] {border:none; padding:0;}
</style>
""".replace("__FONT__", FONT_STACK)

DISCLAIMER = ("<b>Not financial advice.</b> Leveraged trading can lose your entire collateral. "
              "This tool is for education only - always do your own research.")


def fmt_price(p: float) -> str:
    if p is None or not math.isfinite(p):
        return "-"
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:,.4f}"
    return f"{p:.6g}"


def usd(v: float) -> str:
    return f"{v:,.2f} USDT"


def html(block: str):
    st.markdown(block, unsafe_allow_html=True)


def tiles(items, cls="two"):
    """A tidy grid of number tiles: items = [(label, value, sub-text), ...]."""
    cells = ""
    for it in items:
        k, v = it[0], it[1]
        sub = it[2] if len(it) > 2 and it[2] else ""
        colour = f' style="color:{it[3]}"' if len(it) > 3 and it[3] else ""
        cells += (f'<div class="tile"><div class="k">{_html.escape(k)}</div><div class="val"{colour}>'
                  f'{_html.escape(v)}</div>' + (f'<div class="s">{_html.escape(sub)}</div>' if sub else "")
                  + "</div>")
    html(f'<div class="{cls}">{cells}</div>')


def rows(items):
    """iOS-style grouped list: label (+ small sub-text) on the left, value on the right."""
    out = ""
    for it in items:
        k, v = it[0], it[1]
        sub = f"<small>{_html.escape(it[2])}</small>" if len(it) > 2 and it[2] else ""
        colour = f' style="color:{it[3]}"' if len(it) > 3 and it[3] else ""
        out += (f'<div class="row"><div class="l">{_html.escape(k)}{sub}</div>'
                f'<div class="r"{colour}>{_html.escape(v)}</div></div>')
    html(f'<div class="rows">{out}</div>')


def show_fig(fig, height=None):
    cfg = {"displayModeBar": False, "displaylogo": False, "scrollZoom": False, "responsive": True}
    try:
        st.plotly_chart(fig, width="stretch", config=cfg)        # newer Streamlit
    except Exception:
        st.plotly_chart(fig, use_container_width=True, config=cfg)  # older Streamlit


def submit(label: str) -> bool:
    """Full-width primary form button (works on old and new Streamlit)."""
    try:
        return st.form_submit_button(label, type="primary", width="stretch")
    except TypeError:
        return st.form_submit_button(label, type="primary", use_container_width=True)


def seg(label, options, default, key=None, help=None):
    """iOS-style segmented control (falls back to horizontal radio on old Streamlit)."""
    try:
        v = st.segmented_control(label, options, default=default, key=key, help=help, selection_mode="single")
    except Exception:
        v = st.radio(label, options, index=options.index(default), horizontal=True, key=key, help=help)
    return v if v in options else default


_KIND_COLOUR = {"bad": IOS["red"], "warn": IOS["orange"], "good": IOS["green"], "info": IOS["muted"]}


def _reason_items(dec: dict) -> list[tuple[str, str]]:
    """Blockers first, then reasons; leading emoji turned into coloured dots."""
    out = []
    for text, blocker in [(b, True) for b in dec["blockers"]] + [(r, False) for r in dec["reasons"]]:
        t, kind = text.strip(), ("bad" if blocker else "info")
        for mark, k in (("✅", "good"), ("⚠️", "warn"), ("🧠", "info"), ("❌", "bad")):
            if t.startswith(mark):
                t, kind = t[len(mark):].strip(), ("bad" if blocker else k)
        out.append((kind, t))
    return out


def reasons_html(items) -> str:
    lis = "".join(f'<li><span class="dot" style="background:{_KIND_COLOUR[k]}"></span>'
                  f'<span>{_html.escape(t)}</span></li>' for k, t in items)
    return f'<ul class="reasons">{lis}</ul>'


def ring_html(pct: int, colour: str) -> str:
    r, w = 36, 8
    c = 2 * math.pi * r
    off = c * (1 - max(0, min(100, pct)) / 100)
    return (f'<div class="ring"><svg width="86" height="86" viewBox="0 0 86 86">'
            f'<circle cx="43" cy="43" r="{r}" stroke="#2C2C2E" stroke-width="{w}" fill="none"/>'
            f'<circle cx="43" cy="43" r="{r}" stroke="{colour}" stroke-width="{w}" fill="none" '
            f'stroke-linecap="round" stroke-dasharray="{c:.1f}" stroke-dashoffset="{off:.1f}"/></svg>'
            f'<div class="pct">{pct}%<small>confidence</small></div></div>')


def verdict_colour(dec: dict) -> str:
    return IOS["green"] if dec["verdict"].startswith("GO") else IOS["red"]


def run_analysis(pair, style, collateral, leverage, st_period, st_mult, entries):
    """Do all the work for BOTH sides. Returns a dict with everything the tabs need."""
    cfg = STYLES[style]
    out = dict(mtf=None, bt=None, pos={}, decisions={}, bt_note=None,
               funding=(DEFAULT_FUNDING_8H, "estimate (0.01% per 8h)", True))
    with st.spinner("Downloading prices and analysing..."):
        mtf = multi_timeframe(pair, style, st_period, st_mult)
        out["funding"] = fetch_funding(pair)
        prim = mtf["tfs"][mtf["primary"]]
        out["mtf"] = mtf
        # Long and short have different stops, targets and liquidation prices.
        out["pos"] = {d: compute_position(collateral, leverage, prim["close"], prim["atr"], d,
                                          style, out["funding"][0]) for d in (1, -1)}
    with st.spinner("Running the 12-month backtest (the first run can take ~30 seconds)..."):
        try:
            hist, hist_src, _ = fetch_history(pair, cfg["primary"], cfg["bt_candles"])
            if hist is None or len(hist) < 300:
                out["bt_note"] = "Not enough price history to run a backtest for this pair."
            else:
                bt = run_backtest(hist, style, leverage, out["funding"][0], st_period, st_mult)
                bt["source"] = hist_src
                out["bt"] = bt
        except Exception as e:  # a backtest problem shouldn't stop everything else
            out["bt_note"] = f"Backtest could not run ({type(e).__name__})."
    for d in (1, -1):
        mult, _ = learning_multiplier(entries, style, pair, d)
        out["decisions"][d] = decide(mtf, style, leverage, out["pos"][d], out["bt"], mult, d)
    return out


def settings_card():
    """One settings card. Open on the first visit, folded away after the first Analyse."""
    ss = st.session_state
    defaults = dict(pair="BTCUSDT", style="Day", collateral=100.0, leverage=3,
                    custom_st=False, st_period=10, st_mult=3.0)
    for k, v in defaults.items():
        ss.setdefault(f"cfg_{k}", v)
    ss.setdefault("last_mode", DIRECTION_MODES[0])
    label = f"{ss.cfg_pair} · {ss.cfg_style} · {ss.cfg_collateral:,.0f} USDT · {ss.cfg_leverage}x"
    with st.expander(f"⚙️  {label}", expanded=not ss.get("settings_used", False)):
        with st.form("settings", border=False):
            c1, c2 = st.columns(2)
            in_list = ss.cfg_pair in POPULAR_PAIRS
            choice = c1.selectbox("Pair", POPULAR_PAIRS + ["Other (type it)"],
                                  index=POPULAR_PAIRS.index(ss.cfg_pair) if in_list else len(POPULAR_PAIRS))
            custom = c2.text_input("Or type a pair", value="" if in_list else ss.cfg_pair,
                                   placeholder="e.g. PEPEUSDT")
            style = seg("Trade style", list(STYLES), ss.cfg_style)
            collateral = st.number_input("Collateral (USDT)", min_value=1.0, max_value=10_000_000.0,
                                         value=float(ss.cfg_collateral), step=10.0)
            leverage = st.slider("Leverage", 1, 25, int(ss.cfg_leverage))
            st.caption("Suggested: " + " · ".join(f"{k} {v['lev_range'][0]}-{v['lev_range'][1]}x"
                                                  for k, v in STYLES.items()))
            with st.expander("Advanced: Supertrend"):
                custom_st = st.checkbox("Use my own Supertrend settings", value=ss.cfg_custom_st)
                c3, c4 = st.columns(2)
                st_period = c3.number_input("ATR period", 5, 50, int(ss.cfg_st_period))
                st_mult = c4.number_input("Multiplier", 1.0, 6.0, float(ss.cfg_st_mult), step=0.5)
            if submit("Analyse"):
                raw = custom.strip() or (choice if choice in POPULAR_PAIRS else "")
                try:
                    base, quote = parse_pair(raw)
                    if not 2 <= len(base) <= 15:
                        raise DataError("That pair name doesn't look right.")
                    ss.cfg_pair = base + quote
                except DataError as e:
                    st.error(f"{e} Keeping {ss.cfg_pair}.")
                ss.cfg_style, ss.cfg_collateral, ss.cfg_leverage = style, float(collateral), int(leverage)
                ss.cfg_custom_st, ss.cfg_st_period, ss.cfg_st_mult = custom_st, int(st_period), float(st_mult)
                ss.settings_used = True
                st.rerun()
        # Outside the form so it switches instantly (no new download needed).
        mode = seg("Show side", DIRECTION_MODES, ss.last_mode, key="direction_mode",
                   help="Auto shows whichever side (long or short) has the higher confidence. "
                        "The Long and Short cards always show both.")
        ss.last_mode = mode
    cfg = STYLES[ss.cfg_style]
    st_period = ss.cfg_st_period if ss.cfg_custom_st else cfg["st_period"]
    st_mult = ss.cfg_st_mult if ss.cfg_custom_st else cfg["st_mult"]
    return (ss.cfg_pair, ss.cfg_style, float(ss.cfg_collateral), int(ss.cfg_leverage),
            int(st_period), float(st_mult), mode)


def render_journal(entries, pair, style, decision):
    """Journal tab: log Win / Loss results, see the learning summary, back up / restore."""
    html('<div class="card"><div class="note" style="color:var(--text)">Log each trade as a <b>Win</b> or '
         '<b>Loss</b>. Future confidence for that style (and pair or direction, once it has 5+ results) is '
         'nudged between x0.80 and x1.15.</div><div class="warnline" style="margin-top:8px">Free Streamlit '
         'Cloud wipes the journal when the app restarts, sleeps or updates. Download a backup after '
         'logging.</div></div>')
    with st.form("log_trade", clear_on_submit=True, border=False):
        c1, c2 = st.columns(2)
        lp = c1.text_input("Pair", value=pair)
        ls = c2.selectbox("Style", list(STYLES), index=list(STYLES).index(style))
        ld = c1.selectbox("Direction", ["LONG", "SHORT"],
                          index=0 if (decision or {}).get("direction", 1) == 1 else 1)
        lc = c2.number_input("Confidence shown (%)", 0, 100, int((decision or {}).get("confidence", 50)))
        lo = st.radio("Outcome", ["Win", "Loss"], horizontal=True)
        ldate = st.date_input("Trade date", value=datetime.now(LOCAL_TZ).date())
        if submit("Save result"):
            try:
                b, q = parse_pair(lp)
                entries.append(dict(date=str(ldate), pair=b + q, style=ls, direction=ld,
                                    confidence=float(lc), outcome=lo))
                st.session_state.learning_log = entries
                ok = save_log(entries)
                st.success("Saved." + ("" if ok else " (Couldn't write the file, so it's kept for this "
                                       "session only - download a backup!)"))
            except DataError as e:
                st.error(str(e))

    df = log_to_df(entries)
    if df.empty:
        html('<div class="note">No results logged yet.</div>')
    else:
        items = [("Logged", str(len(df)), f"{(df['outcome'] == 'Win').mean() * 100:.0f}% wins overall")]
        for s in STYLES:
            sd = df[df["style"] == s]
            if len(sd):
                items.append((f"{s}", f"{(sd['outcome'] == 'Win').mean() * 100:.0f}% wins", f"{len(sd)} results"))
        tiles(items)
        _, note = learning_multiplier(entries, style, pair, (decision or {}).get("direction"))
        html(f'<div class="note">Current adjustment: {_html.escape(note)}</div>')
        with st.expander("All results"):
            st.dataframe(df.iloc[::-1], hide_index=True)
    st.download_button("Download backup (CSV)", df.to_csv(index=False).encode("utf-8"),
                       file_name="learning_log.csv", mime="text/csv", disabled=df.empty)
    with st.expander("Restore from backup"):
        up = st.file_uploader("Choose your learning_log.csv", type=["csv"])
        if up is not None and st.button("Restore these results"):
            try:
                new = csv_to_entries(up.getvalue())
                seen = {json.dumps(e, sort_keys=True) for e in entries}
                added = [e for e in new if json.dumps(e, sort_keys=True) not in seen]
                entries.extend(added)
                st.session_state.learning_log = entries
                save_log(entries)
                st.success(f"Restored {len(added)} results.")
            except Exception as e:
                st.error(f"Couldn't read that file: {str(e)[:150]}")
    with st.expander("Clear all results"):
        sure = st.checkbox("Yes, delete my logged results")
        if st.button("Delete everything", disabled=not sure):
            entries.clear()
            st.session_state.learning_log = entries
            save_log(entries)
            st.success("Cleared.")


def render_home(res, mode):
    """Decision card, Long/Short mini cards and the 4 key numbers."""
    decisions, mtf = res["decisions"], res["mtf"]
    chosen = pick_side(decisions, mode)
    dec, pos = decisions[chosen], res["pos"][chosen]
    is_go = dec["verdict"].startswith("GO")
    items = _reason_items(dec)
    summary = items[0][1] if items else ""
    how = "best side" if mode.startswith("Auto") else "your pick"
    html(f'<div class="card hero {"go" if is_go else "nogo"}"><div>'
         f'<div class="eyebrow">{dec["side"]} · {how}</div>'
         f'<div class="verdict" style="color:{verdict_colour(dec)}">{dec["verdict"]}</div>'
         f'<div class="summary">{_html.escape(summary)}</div></div>'
         f'{ring_html(dec["confidence"], verdict_colour(dec))}</div>')

    minis = ""
    for d in (1, -1):
        x = decisions[d]
        colour = verdict_colour(x)
        minis += (f'<div class="mini{" picked" if d == chosen else ""}"><div class="eyebrow">{x["side"]}</div>'
                  f'<div class="v" style="color:{colour}">{x["verdict"]}</div>'
                  f'<div class="bar"><div style="width:{x["confidence"]}%; background:{colour}"></div></div>'
                  f'<div class="c">{x["confidence"]}% confidence</div></div>')
    html(f'<div class="two">{minis}</div>')

    if len(items) > 1:
        html(f'<div class="card">{reasons_html(items[1:3])}</div>')
    with st.expander("Show all reasons"):
        for d in (chosen, -chosen):
            x = decisions[d]
            html(f'<div class="section">{x["side"]} · {x["verdict"]} · {x["confidence"]}%</div>')
            html(f'<div style="height:10px"></div>{reasons_html(_reason_items(x))}<div style="height:6px"></div>')

    sign = -1 if pos["direction"] == 1 else 1
    liq_col = IOS["red"] if pos["liq_before_stop"] else (IOS["orange"] if pos["liq_close"] else None)
    html(f'<div class="section">{dec["side"]} plan · {pos["notional"]:,.0f} USDT position</div>')
    tiles([
        ("Entry", fmt_price(pos["entry"]), "price now"),
        ("Stop-loss", fmt_price(pos["stop"]), f"{sign * pos['stop_pct'] * 100:+.2f}%", IOS["red"]),
        ("Target", fmt_price(pos["target"]), f"{-sign * pos['target_pct'] * 100:+.2f}%", IOS["green"]),
        ("Liquidation", fmt_price(pos["liq"]), f"{sign * pos['liq_pct'] * 100:+.2f}%", liq_col),
    ])
    if pos["liq_before_stop"]:
        html(f'<div class="warnline" style="color:{IOS["red"]}">You would be liquidated before the stop. '
             f'Use {pos["max_safe_lev"]}x or less.</div>')
    last_t = mtf["tfs"][mtf["primary"]]["last_time"].tz_convert(LOCAL_TZ)
    src = ", ".join(sorted({re.sub(r"\s*\([A-Z0-9-]+\)$", "", x) for x in mtf["sources"]}))
    html(f'<div class="note">{_html.escape(src)} · {mtf["primary"]} · updated {last_t:%H:%M} UAE time. '
         'The latest candle is still forming, so this can change.</div>')
    return chosen


def render_details(res, style, leverage, chosen):
    cfg = STYLES[style]
    lo, hi = cfg["lev_range"]
    mtf, pos = res["mtf"], (res["pos"] or {}).get(chosen)
    funding_rate, funding_src, funding_est = res["funding"]
    if pos:
        side = "LONG" if pos["direction"] == 1 else "SHORT"
        html(f'<div class="section">{side} position · {leverage}x isolated</div>')
        tiles([
            ("Position size", usd(pos["notional"]), f"{pos['qty']:.6g} coins"),
            ("Max safe leverage", f"{pos['max_safe_lev']}x", f"suggested {lo}-{hi}x"),
            ("Loss if stopped", usd(pos["risk"]), f"{pos['risk_pct_collateral']:.1f}% incl. costs", IOS["red"]),
            ("Profit at target", usd(pos["reward"]), f"R:R {pos['rr_after_costs']:.2f} after costs", IOS["green"]),
        ])
        if pos["liq_before_stop"]:
            st.error(f"At {leverage}x you'd be liquidated before your stop-loss. Use {pos['max_safe_lev']}x or less.")
        elif pos["liq_close"]:
            html(f'<div class="warnline">Liquidation is close to the stop. Safer: {pos["max_safe_lev"]}x or less.</div>')
        rows([
            ("Distance to stop", f"{pos['stop_pct'] * 100:.2f}%",
             f"{cfg['stop_atr']} x ATR, at least {cfg['min_stop_pct'] * 100:.2f}%"),
            ("Distance to liquidation", f"{pos['liq_pct'] * 100:.2f}%",
             f"estimate with {MAINT_MARGIN_RATE * 100:.1f}% maintenance margin"),
        ])
        html('<div class="section">Costs</div>')
        rows([
            ("Fees in + out", usd(pos["fees"]), "0.04% taker fee each way"),
            ("Slippage", usd(pos["slippage"]), f"{cfg['slippage'] * 100:.2f}% each way"),
            (f"Funding (~{pos['hold_hours']}h hold)", usd(pos["funding"]),
             f"{funding_rate * 100:.4f}% per 8h · {'estimated' if funding_est else funding_src} · assumed you pay"),
            ("Total costs", usd(pos["costs"]), f"{pos['costs_pct_collateral']:.2f}% of your collateral"),
        ])
        html('<div class="note">Follows the <b>Show side</b> switch in Settings (in Auto, the stronger side). '
             'A long\'s stop sits below the price and a short\'s above it.</div>')
    if mtf:
        with st.expander(f"Analysis · score {mtf['score']:+.0f} / 100"):
            html('<div class="note">Each timeframe is scored from −100 (very bearish) to +100 (very bullish) and '
                 'weighted for your style. A positive score helps the long side, a negative one the short side. '
                 'Supertrend is the key check: a side can only be GO if the main timeframe\'s Supertrend points '
                 'its way and the biggest timeframe doesn\'t strongly disagree.</div>')
            for tf, r in mtf["tfs"].items():
                main = " · main" if tf == mtf["primary"] else ""
                html(f'<div class="section">{tf}{main} · score {r["score"]:+.0f} · weight {r["weight"] * 100:.0f}%</div>')
                items = [("good" if row["pts"] > 0 else "bad" if row["pts"] < 0 else "info",
                          f"{row['factor']} ({row['pts']:+.0f}): {row['text']}") for row in r["rows"]]
                html(f'<div style="height:10px"></div>{reasons_html(items)}<div style="height:4px"></div>')
            for p in mtf["problems"]:
                html(f'<div class="warnline">Skipped {_html.escape(p)}.</div>')
    with st.expander("Risks & disclaimer", expanded=not pos):
        html(f'<div class="disc">{DISCLAIMER}</div>')
        st.markdown(
            "- **Leverage multiplies losses as well as gains.** At 10x, a 10% move against you wipes out your collateral.\n"
            "- The liquidation price is an **estimate**; your exchange may liquidate slightly earlier.\n"
            "- Prices come from public spot markets and can differ slightly from your futures exchange.\n"
            "- Funding rates change every few hours; the cost shown is an estimate.\n"
            "- Backtests use past data and simple rules. Markets change - the past does not predict the future.\n"
            "- News, hacks and big announcements can move prices instantly, jumping past your stop.\n"
            "- Never trade money you can't afford to lose. Consider the lower end of the leverage range.")
        if pos and pos["liq_before_stop"]:
            html(f'<div class="warnline" style="color:{IOS["red"]}">Your current leverage would be liquidated before the stop-loss.</div>')
        if leverage > hi:
            html(f'<div class="warnline">Your leverage ({leverage}x) is above the suggested {lo}-{hi}x for {style}.</div>')
        if funding_est:
            html('<div class="note">The live funding rate was unavailable, so 0.01% per 8 hours was assumed.</div>')
        html('<div class="note">Journal: free Streamlit Cloud wipes it on restart - download a CSV backup.</div>')


def render_backtest(res, style, leverage):
    cfg, bt = STYLES[style], res["bt"]
    if not bt:
        html(f'<div class="note">{_html.escape(res["bt_note"] or "Backtest not available.")}</div>')
        return
    pf = "∞" if bt["pf"] == float("inf") else f"{bt['pf']:.2f}"
    html(f'<div class="note">The {style} rules on {cfg["primary"]} candles, '
         f'{bt["start"].tz_convert(LOCAL_TZ):%d %b %Y} – {bt["end"].tz_convert(LOCAL_TZ):%d %b %Y} '
         f'(~{bt["days"]:.0f} days, {bt["n_candles"]:,} candles) at {leverage}x with fees, slippage and funding.'
         + (" Scalp tests only the latest few weeks to stay quick." if style == "Scalp" else "") + "</div>")
    exp_col = IOS["green"] if bt["exp_r"] > 0 and bt["exp_pct"] > 0 else IOS["red"]
    tiles([
        ("Trades", str(bt["n"]), f"{bt['long_n']} long · {bt['short_n']} short"),
        ("Win rate", f"{bt['win_rate']:.0f}%", f"avg win {bt['avg_win']:+.1f}% · loss {bt['avg_loss']:+.1f}%"),
        ("Expectancy", f"{bt['exp_r']:+.2f} R", f"{bt['exp_pct']:+.2f}% per trade", exp_col),
        ("Profit factor", pf, "won ÷ lost · 1.3+ is decent"),
        ("Max drawdown", f"{bt['max_dd']:.1f}%", "biggest fall from a high"),
        ("Total return", f"{bt['total_ret']:+.1f}%", f"{bt['liquidations']} liquidations"),
    ])
    if bt["n"] < 10:
        html(f'<div class="warnline">Only {bt["n"]} trades - too few to trust much (low reliability).</div>')
    if bt["wiped"]:
        html(f'<div class="warnline" style="color:{IOS["red"]}">With these settings the backtest account was wiped out.</div>')
    show_fig(equity_chart(bt))
    html('<div class="section">Long vs short trades</div>')
    side_items = []
    for d_, nm in ((1, "Long"), (-1, "Short")):
        s_ = bt["sides"][d_]
        col = IOS["green"] if s_["exp_r"] > 0 and s_["exp_pct"] > 0 else IOS["red"]
        side_items.append((f"{nm} · {s_['n']} trades", f"{s_['exp_r']:+.2f} R",
                           f"{s_['win_rate']:.0f}% wins · {s_['exp_pct']:+.2f}%/trade"
                           + (" · low reliability" if s_["n"] < 10 else ""), col))
    tiles(side_items)
    html('<div class="note">Each side\'s decision uses only its own past trades.</div>')
    if bt["n"]:
        with st.expander("Last 20 trades"):
            t = bt["trades"].tail(20).iloc[::-1].copy()
            t["when"] = to_local(pd.to_datetime(t["entry_time"], utc=True)).dt.strftime("%d %b %H:%M")
            t["result %"] = (t["ret"] * 100).round(1)
            st.dataframe(t[["when", "side", "reason", "result %"]], hide_index=True)
    html(f'<div class="note">Data: {_html.escape(bt.get("source", ""))}. Past results do not guarantee future results.</div>')


@st.cache_data(ttl=600, show_spinner=False)
def cached_scan(style: str, pairs: tuple, collateral: float, log_json: str) -> dict:
    """Scan results are remembered for 10 minutes."""
    entries = json.loads(log_json) if log_json else []
    res = scan_pairs(list(pairs), [style], collateral, entries, workers=4)
    res["time"] = datetime.now(timezone.utc).isoformat()
    return res


def render_scan(style: str, collateral: float, entries: list[dict]):
    """Scan tab: check many pairs for the selected style and list the best side of each."""
    lev = default_leverage(style)
    all_pairs = st.checkbox(f"Scan all {len(SCAN_PAIRS)} pairs (slower)", key="scan_all")
    pairs = SCAN_PAIRS if all_pairs else SCAN_PAIRS[:SCAN_PAIRS_CLOUD]
    html(f'<div class="note">Checks {len(pairs)} pairs for <b>{style}</b> at its suggested {lev}x, long and '
         'short, with the same rules as the main screen. Results are kept for 10 minutes. '
         'Phone alerts come from Grok Bot\'s scheduled scan, not from this page.</div>')
    key = f"scan_{style}_{len(pairs)}"
    try:
        clicked = st.button("Scan pairs", type="primary", width="stretch")
    except TypeError:
        clicked = st.button("Scan pairs", type="primary", use_container_width=True)
    if clicked:
        with st.spinner(f"Scanning {len(pairs)} pairs - about {max(20, len(pairs) * 3)} seconds..."):
            try:
                st.session_state[key] = cached_scan(style, tuple(pairs), float(collateral),
                                                    json.dumps(entries, sort_keys=True))
            except Exception as e:
                st.error(f"Scan failed ({type(e).__name__}). Please try again in a minute.")
    res = st.session_state.get(key)
    if not res:
        return
    ok = [r for r in res["results"] if r.get("ok")]
    ok.sort(key=lambda r: (r["status"] == "NO-GO", -r["best_confidence"]))
    n_go = sum(r["status"] != "NO-GO" for r in ok)
    when = datetime.fromisoformat(res["time"]).astimezone(LOCAL_TZ)
    html(f'<div class="section">{len(ok)} pairs · {n_go} GO · {when:%H:%M} {LOCAL_TZ_LABEL.split(" (")[0]}</div>')
    cells = ""
    for r in ok:
        go = r["status"] != "NO-GO"
        side_txt = r["status"] if go else f"{r['best_side'].title()} · no-go"
        colour = IOS["green"] if go else IOS["muted"]
        bg = ' style="background:rgba(52,199,89,0.14); margin:0 -14px; padding:10px 14px"' if go else ""
        cells += (f'<div class="row"{bg}><div class="l">{_html.escape(r["pair"])}'
                  f'<small>{_html.escape(side_txt)} · score {r["score"]:+.0f}</small></div>'
                  f'<div class="r" style="color:{colour}">{r["best_confidence"]}%</div></div>')
    html(f'<div class="rows">{cells}</div>')
    if res["skipped"]:
        html(f'<div class="note">Skipped (no data): {_html.escape(", ".join(res["skipped"]))}</div>')
    html(f'<div class="note">Took {res["seconds"]:.0f}s. Pick a pair in Settings to see its full analysis.</div>')


def render_tabs(res, pair, style, collateral, leverage, entries, mode=DIRECTION_MODES[0]):
    """Everything below the home screen, in four simple tabs."""
    decisions = res["decisions"]
    chosen = pick_side(decisions, mode) if decisions else 1
    decision = decisions.get(chosen) if decisions else None
    pos = (res["pos"] or {}).get(chosen)
    mtf = res["mtf"]
    t_chart, t_details, t_bt, t_journal, t_scan = st.tabs(["Chart", "Details", "Backtest", "Journal", "Scan"])
    with t_chart:
        if mtf:
            leg = [("Supertrend", IOS["green"]), ("EMA 9", IOS["yellow"]), ("21", IOS["teal"]),
                   ("50", IOS["purple"]), ("200", IOS["orange"])]
            html('<div class="legend">' + "".join(f'<span><b style="background:{c}"></b>{n}</span>' for n, c in leg)
                 + "</div>")
            show_fig(price_chart(mtf["tfs"][mtf["primary"]]["data"], pair, mtf["primary"], pos))
            side_txt = decision["side"].lower() if decision else "long"
            html(f'<div class="note">{pair} · {mtf["primary"]} candles · {LOCAL_TZ_LABEL}. Supertrend turns red in a '
                 f'down-trend. Dashed lines: entry, stop and target for the {side_txt} side. Below: volume, then '
                 'RSI (over 70 stretched up, under 30 stretched down). Pinch to zoom.</div>')
        else:
            html('<div class="note">No chart - see the message above.</div>')
    with t_details:
        render_details(res, style, leverage, chosen)
    with t_bt:
        render_backtest(res, style, leverage)
    with t_journal:
        render_journal(entries, pair, style, decision)
    with t_scan:
        render_scan(style, collateral, entries)


def main():
    st.set_page_config(page_title="Trade Screener", page_icon="📈", layout="centered",
                       initial_sidebar_state="collapsed")
    html(CSS)
    html('<div class="appbar"><div class="title">Trade Screener</div>'
         '<div class="pill">Not financial advice</div></div>')

    pair, style, collateral, leverage, st_period, st_mult, mode = settings_card()
    cfg = STYLES[style]
    lo, hi = cfg["lev_range"]
    if leverage > hi:
        html(f'<div class="warnline">{leverage}x is above the suggested {lo}-{hi}x for {style}. '
             'A small move could liquidate you.</div>')
    elif leverage < lo:
        html(f'<div class="note">{leverage}x is below the usual {lo}-{hi}x for {style}: safer, smaller gains.</div>')

    if "learning_log" not in st.session_state:
        st.session_state.learning_log = load_log()
    entries = st.session_state.learning_log

    res = dict(mtf=None, bt=None, pos={}, decisions={}, bt_note=None,
               funding=(DEFAULT_FUNDING_8H, "estimate", True))
    try:
        res = run_analysis(pair, style, collateral, leverage, st_period, st_mult, entries)
    except DataError as e:
        st.error(str(e))
    except requests.RequestException:
        st.error("Network problem while downloading prices. Please tap Refresh in a minute.")
    except Exception as e:
        st.error("Something went wrong while analysing. Try Refresh, another pair or another style.")
        with st.expander("Technical details (for troubleshooting)"):
            st.code(f"{type(e).__name__}: {str(e)[:300]}")

    try:
        if res["decisions"]:
            render_home(res, mode)
        if st.button("↻  Refresh prices"):
            fetch_live.clear()
            fetch_funding.clear()
            st.rerun()
        render_tabs(res, pair, style, collateral, leverage, entries, mode)
    except Exception as e:
        st.error("Part of the page couldn't be shown. Tap Refresh to try again.")
        with st.expander("Technical details (for troubleshooting)"):
            st.code(f"{type(e).__name__}: {str(e)[:300]}")


if __name__ == "__main__":
    main()
