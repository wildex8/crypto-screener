#!/usr/bin/env python3
"""
Practice (paper) trading bot for the Leveraged Crypto Trade Screener.

NO REAL MONEY. NO EXCHANGE KEYS. It only reads public price data.

What it does each time run.sh runs (after the scan):
  1. Updates every open paper trade: downloads 5-minute candles (fallback 15m, 1h)
     from the same free data sources as the scanner, starting where it last
     stopped, and walks them in order. A trade closes at the stop, the target,
     the liquidation price (if reached first) or at market after the style's
     maximum hold time. If the stop and the target are both inside one candle
     the stop is assumed (conservative). Because it replays candles from the last
     check, overnight gaps (no runs 23:00-07:45 UAE) are handled exactly.
  2. Learns from closed trades (see LEARNING below) and filters the NEW GO
     signals: a signal in a bucket that has been losing needs more confidence,
     and a clearly losing bucket is blocked (with re-tests).
  3. Opens a paper trade for every NEW GO signal that passes: $5,000 notional at
     5x (1,000 USDT collateral), entry at the signal's price, its stop and target.
     At most one open trade per pair + style.

Costs: 0.04% taker fee on entry AND exit notional, the style's slippage each way,
funding per 8h held (live rate at entry from the scanner's funding fetch, or
0.01%/8h; always assumed paid = conservative). Liquidation (isolated margin,
0.5% maintenance) loses the whole collateral.

Max hold (same as the backtest's max_hold_bars, so paper results are comparable):
  Swing 7 days (42 x 4h) · Day 24h (24 x 1h) · Scalp (5m) 3h (36 x 5m)
  Scalp-15m 4h · Scalp-30m 8h · Scalp-1h 16h (16 candles each)

LEARNING (learning.json, human log learning_changes.txt):
  Every closed trade stores a snapshot of its features at entry and, for losses,
  post-mortem tags. Buckets: pair+style+side, style+side, and style+side+entry
  condition (low ADX, against the bigger chart, not all charts agree, high
  volatility, adverse funding, stretched RSI, thin backtest edge). Stats use the
  bucket's latest 20 closed trades. With >= 5 trades and negative expectancy the
  bucket's minimum confidence goes up +5/+10/+15 (cap +15); with >= 8 trades,
  expectancy <= -0.5R and win rate <= 30% it is blocked for 14 days, while 1 in
  every 4 of its signals still goes through as a probe. Penalties relax 5 points
  per new result once the bucket recovers; good probes unblock early. The core
  strategy code is never changed.

ACCOUNT PROTECTION + LET WINNERS RUN (trades.json "rules" section; switched on with
--enable-protection, ledgers without it behave exactly as before):
  All P&L here is REALISED P&L after costs (closed trades + half-positions already
  booked), by booking time. Unrealised P&L is not counted (it is shown, not used).
  Days and weeks are UAE time (Asia/Dubai, UTC+4); the week starts Monday 00:00 UAE.
  - Daily loss limit: once today's realised P&L has reached -3% of the account value
    at 00:00 UAE, no new trades until the next UAE day. Open trades keep their stops.
  - Profit lock: once today's realised P&L has reached the daily goal (+$500,
    --daily-goal), if it then falls back to half of the day's peak, no new trades for
    the rest of the day. Open trades are never force-closed.
  - Weekly limit: once the week's realised P&L has reached -8% of the account value
    at Monday 00:00 UAE, no new trades until next Monday (flagged once: just_triggered).
  - Open trade cap: at most 6 open trades; existing ones are never closed for it.
  Limits are sticky: a later winner does not re-open trading for that day/week.
  Let winners run (new and already-open trades, from when the rules were switched on):
  - at +1R (price moved by the initial stop distance in our favour) the stop moves to
    break-even plus costs (fees, slippage and funding to the deadline);
  - at the original target half the position is closed and booked; the other half is
    trailed with an ATR(14) x Supertrend-multiplier chandelier stop on the trade's own
    timeframe (best price since the target minus k x ATR; k = the style's Supertrend
    multiplier). The stop only ever tightens; the liquidation check stays. The
    runner may stay open up to 2x the style's normal max hold.
  - One trade = one result: pnl_usd / r_multiple of a closed trade include the booked
    half (R on the full position's initial risk); partials[] lists the half closes.

Usage:
  python paper_trader.py --run [--scan last_run.json] [--ledger DIR]     (used by run.sh)
  python paper_trader.py --summary [--ledger DIR]
  python paper_trader.py --seed-from-state /workspace/scanner/state.json  (read-only on state)

NOT FINANCIAL ADVICE.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import shutil
import sys
import time
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

warnings.filterwarnings("ignore")
import logging  # noqa: E402
import streamlit.logger as _st_logger  # noqa: E402
_st_logger.set_log_level("error")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import app  # noqa: E402

for _name in list(logging.root.manager.loggerDict):
    if _name.startswith("streamlit"):
        logging.getLogger(_name).setLevel(logging.ERROR)

import pandas as pd  # noqa: E402

DEFAULT_LEDGER = "/workspace/scanner/paper"
NOTIONAL = 5000.0
LEVERAGE = 5.0
COLLATERAL = NOTIONAL / LEVERAGE
FINE_TFS = ["5m", "15m", "1h"]
MAX_FINE_BARS = 6000
BACKUPS_KEPT = 60

# learning settings (deliberately conservative)
BASE_MIN_CONF = app.MIN_CONFIDENCE_FOR_GO       # 55
MIN_SAMPLE = 5
BLOCK_SAMPLE = 8
WINDOW = 20
MAX_PENALTY = 15
RELAX_STEP = 5
BLOCK_DAYS = 14
PROBE_EVERY = 4
BLOCK_EXP_R = -0.5
BLOCK_MAX_WR = 30.0

# account protection + let winners run (defaults; stored in trades.json "rules")
DEFAULT_PROTECTION = dict(daily_loss_pct=3.0, daily_goal_usd=500.0, giveback_frac=0.5,
                          weekly_loss_pct=8.0, max_open=6)
DEFAULT_RUNNERS = dict(breakeven_at_r=1.0, partial_frac=0.5, atr_period=14, runner_hold_mult=2.0)

REASON_TEXT = {
    "stop": "hit its stop", "liq": "was liquidated", "target": "hit its target",
    "timeout": "closed at the time limit", "breakeven": "closed at break-even (stop had moved to entry + costs)",
    "trail": "closed by the trailing stop after half was booked at target",
    "timeout_runner": "closed at the time limit after half was booked at target",
}

TAG_WORDS = {
    "low_adx": "low-ADX (weak trend)",
    "against_higher_tf": "against the bigger chart's trend",
    "weak_confluence": "not all charts agreeing",
    "high_volatility": "high-volatility",
    "adverse_funding": "adverse-funding",
    "stretched_rsi": "stretched-RSI",
    "thin_backtest_edge": "thin-backtest-edge",
    "quick_stop": "quick stop-out (bad timing / chop)",
    "timeout_loss": "went nowhere and timed out at a loss",
}
ENTRY_TAGS = ["low_adx", "against_higher_tf", "weak_confluence", "high_volatility",
              "adverse_funding", "stretched_rsi", "thin_backtest_edge"]


# ---------------------------------------------------------------- small helpers

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_t(s) -> datetime:
    t = datetime.fromisoformat(str(s))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def uae(t) -> str:
    if t is None:
        return None
    return f"{parse_t(t).astimezone(app.LOCAL_TZ):%d %b %Y %H:%M} UAE"


def dur(hours: float) -> str:
    m = int(round(hours * 60))
    d, rem = divmod(m, 1440)
    h, mm = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    return f"{h}h {mm:02d}m" if h else f"{mm}m"


def r2(x, n=2):
    return None if x is None else round(float(x), n)


def style_cfg(style: str) -> dict:
    return app.STYLES[style]


def max_hold_hours(style: str) -> float:
    c = style_cfg(style)
    return c["max_hold_bars"] * app.INTERVAL_SECONDS[c["primary"]] / 3600


def side_word(style: str, side: str, plural=True) -> str:
    return f"{style} {side.lower()}{'s' if plural else ''}"


# ---------------------------------------------------------------- ledger I/O

class Ledger:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "backups").mkdir(exist_ok=True)
        self.trades_path = self.root / "trades.json"
        self.learn_path = self.root / "learning.json"
        self.changes_txt = self.root / "learning_changes.txt"
        self._lockf = None

    def lock(self, timeout=300):
        self._lockf = open(self.root / ".lock", "w")
        t0 = time.time()
        while True:
            try:
                fcntl.flock(self._lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.time() - t0 > timeout:
                    raise RuntimeError("paper ledger is locked by another run")
                time.sleep(1)

    def unlock(self):
        if self._lockf:
            fcntl.flock(self._lockf, fcntl.LOCK_UN)
            self._lockf.close()
            self._lockf = None

    def _read(self, path, default):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except Exception:
            # corrupted file: keep it aside, never silently overwrite it
            bad = path.with_suffix(f".corrupt-{int(time.time())}.json")
            shutil.copy2(path, bad)
            raise RuntimeError(f"{path} is not valid JSON (copied to {bad.name}); fix or restore from backups/")

    def load(self):
        t = self._read(self.trades_path, {"version": 1, "trades": [], "processed_signals": {}})
        t.setdefault("trades", [])
        t.setdefault("processed_signals", {})
        l = self._read(self.learn_path, {"version": 1, "buckets": {}, "changes": []})
        l.setdefault("buckets", {})
        l.setdefault("changes", [])
        return t, l

    def _write(self, path, obj):
        if path.exists():
            stamp = utcnow().strftime("%Y%m%dT%H%M%S%f")
            shutil.copy2(path, self.root / "backups" / f"{path.stem}-{stamp}.json")
            olds = sorted((self.root / "backups").glob(f"{path.stem}-*.json"))
            for o in olds[:-BACKUPS_KEPT]:
                o.unlink()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, path)

    def save(self, trades, learning):
        self._write(self.trades_path, trades)
        self._write(self.learn_path, learning)


# ---------------------------------------------------------------- market data

def load_fine_candles(pair: str, since: datetime, now: datetime, loader=None):
    """Closed candles from `since` to `now`, finest timeframe that covers the gap.
    Returns (df, tf, source, warning)."""
    loader = loader or (lambda p, tf, n: app.load_klines(p, tf, n, budget_s=40))
    best = None
    for tf in FINE_TFS:
        iv = app.INTERVAL_SECONDS[tf]
        need = int(math.ceil((now - since).total_seconds() / iv)) + 3
        if need > MAX_FINE_BARS and tf != FINE_TFS[-1]:
            continue
        try:
            df, src, _ = loader(pair, tf, min(max(need, 3), MAX_FINE_BARS))
        except Exception:
            df, src = None, None
        if df is None or df.empty:
            continue
        df = df.copy()
        df["close_time"] = df["time"] + pd.Timedelta(seconds=iv)
        df = df[df["close_time"] <= pd.Timestamp(now)]
        covered = len(df) and df["time"].iloc[0] <= pd.Timestamp(since)
        if covered:
            return df, tf, src, None
        if best is None:
            best = (df, tf, src)
    if best is None:
        return None, None, None, f"{pair}: no candle data from any source"
    return best[0], best[1], best[2], (f"{pair}: data only from {best[0]['time'].iloc[0] if len(best[0]) else '?'}"
                                       f" (wanted {since.isoformat()}); checked what was available")


def fetch_funding(pair):
    try:
        return app.load_funding_rate(pair)
    except Exception:
        return app.DEFAULT_FUNDING_8H, "estimate (0.01% per 8h)", True


# ---------------------------------------------------------------- trade maths

def pnl_part(t: dict, exit_px: float, hours: float, frac: float = 1.0, liquidated=False) -> dict:
    """P&L after costs of `frac` of the position closed at exit_px. R and % are on the FULL position."""
    d = t["dir"]
    slip = t["slippage"]
    exit_fill = exit_px * (1 - d * slip)
    qty = t["qty"] * frac
    gross = d * qty * (exit_fill - t["entry_fill"])
    fees = app.TAKER_FEE * (t["notional"] * frac + qty * exit_fill)
    funding = t["notional"] * frac * abs(t["funding_rate_8h"]) * hours / 8
    slip_cost = qty * t["entry"] * slip + qty * exit_px * slip
    pnl = gross - fees - funding
    cap = t["collateral"] * frac
    if liquidated:
        pnl = -cap
    pnl = max(pnl, -cap)
    return dict(exit_fill=exit_fill, gross_usd=gross, fees_usd=fees, funding_usd=funding,
                slippage_usd=slip_cost, pnl_usd=pnl, pnl_pct_collateral=pnl / t["collateral"] * 100,
                r_multiple=pnl / t["planned_risk_usd"] if t["planned_risk_usd"] > 0 else 0.0)


def pnl_breakdown(t: dict, exit_px: float, hours: float, liquidated=False) -> dict:
    return pnl_part(t, exit_px, hours, 1.0, liquidated)


def remaining_frac(t: dict) -> float:
    return float((t.get("mgmt") or {}).get("remaining_frac", 1.0))


def booked_partial_usd(t: dict) -> float:
    return sum(p["pnl_usd"] for p in t.get("partials") or [])


# ---------------------------------------------------------------- let winners run

def get_rules(trades_doc: dict) -> dict:
    return trades_doc.get("rules") or {}


def init_mgmt(t: dict, now: datetime, runners: dict) -> dict:
    """Start break-even / partial / trailing management for a trade (new or already open)."""
    if t.get("mgmt"):
        return t["mgmt"]
    cfg = style_cfg(t["style"])
    d = t["dir"]
    risk_px = abs(t["entry_fill"] - t["stop"])
    t["mgmt"] = dict(
        since=now.isoformat(), initial_stop=t["stop"], initial_deadline=t["deadline"], risk_px=risk_px,
        be_trigger=t["entry_fill"] + d * runners.get("breakeven_at_r", 1.0) * risk_px,
        be_moved=False, be_price=None, partial_frac=runners.get("partial_frac", 0.5), partial_done=False,
        remaining_frac=1.0, trail_active=False, trail_stop=None, best_price=None,
        trail_mult=float(cfg["st_mult"]), atr_period=int(runners.get("atr_period", 14)),
        runner_hold_mult=float(runners.get("runner_hold_mult", 2.0)), last_atr=None, events=[])
    t.setdefault("partials", [])
    return t["mgmt"]


def breakeven_price(t: dict) -> float:
    """Price at which closing the rest of the trade nets $0 after fees, slippage and funding to the deadline."""
    d, fee, slip = t["dir"], app.TAKER_FEE, t["slippage"]
    frac = remaining_frac(t)
    qty, N = t["qty"] * frac, t["notional"] * frac
    hours = max((parse_t(t["deadline"]) - parse_t(t["opened_at"])).total_seconds() / 3600, 0)
    F = N * abs(t["funding_rate_8h"]) * hours / 8
    xf = (qty * t["entry_fill"] + d * (fee * N + F)) / (qty * (1 - d * fee))
    return xf / (1 - d * slip)


def fallback_atr(t: dict) -> float:
    f = t.get("features") or {}
    if f.get("atr_pct"):
        return float(f["atr_pct"]) / 100 * t["entry"]
    risk = abs(t["entry"] - (t.get("mgmt") or {}).get("initial_stop", t["stop"]))
    return risk / float(style_cfg(t["style"]).get("stop_atr", 2.0))


def make_atr_lookup(t: dict, now: datetime, loader=None):
    """ATR on the trade's own timeframe, as known at a given time (last candle closed before it)."""
    cache = {}
    loader = loader or (lambda p, tf, n: app.load_klines(p, tf, n, budget_s=30))
    n_atr = int((t.get("mgmt") or {}).get("atr_period", 14))

    def series():
        tf = t["entry_tf"]
        iv = app.INTERVAL_SECONDS[tf]
        since = parse_t(t["opened_at"])
        need = int(math.ceil((now - since).total_seconds() / iv)) + 5 * n_atr
        try:
            df, _, _ = loader(t["pair"], tf, min(max(need, 5 * n_atr), 1500))
        except Exception:
            df = None
        if df is None or df.empty or len(df) < n_atr + 1:
            return None
        df = df.copy()
        df["close_time"] = df["time"] + pd.Timedelta(seconds=iv)
        df = df[df["close_time"] <= pd.Timestamp(now)].reset_index(drop=True)
        a = app.atr(df, n_atr)
        out = pd.Series(a.to_numpy(), index=pd.DatetimeIndex(df["close_time"])).dropna()
        return out if len(out) else None

    def lookup(at: datetime) -> tuple[float, str]:
        if "s" not in cache:
            cache["s"] = series()
        s = cache["s"]
        if s is not None:
            i = s.index.searchsorted(pd.Timestamp(at), side="right") - 1
            if i >= 0:
                v = float(s.iloc[i])
                if v > 0 and not math.isnan(v):
                    return v, f"ATR({n_atr}) {t['entry_tf']}"
        return fallback_atr(t), "ATR estimate from entry"
    return lookup


def _tighten(t: dict, new_stop: float) -> bool:
    d = t["dir"]
    if (d == 1 and new_stop > t["stop"]) or (d == -1 and new_stop < t["stop"]):
        t["stop"] = new_stop
        return True
    return False


def walk_trade(t: dict, df: pd.DataFrame, now: datetime, atr_lookup=None, events=None) -> dict | None:
    """Replay candles in order. Mutates t (last_checked, last_price; with let-winners-run also stop,
    mgmt, partials, deadline) and returns a close dict if the trade closed, else None.
    Within a candle: the stop (or liquidation) is checked first, then the target, then the stop is
    moved (break-even / trail) for the NEXT candle - so a candle never helps itself."""
    d = t["dir"]
    m = t.get("mgmt")
    opened = parse_t(t["opened_at"])
    last = parse_t(t["last_checked"]) if t.get("last_checked") else None
    liq = t["liquidation"]
    if df is None or df.empty:
        return None
    for row in df.itertuples(index=False):
        ot, ct = row.time.to_pydatetime(), row.close_time.to_pydatetime()
        if last is None:
            if ot < opened:                  # never use prices from before the entry
                continue
        elif ct <= last:                     # already processed
            continue
        stop, target = t["stop"], t["target"]
        deadline = parse_t(t["deadline"])
        adverse = max(stop, liq) if d == 1 else min(stop, liq)
        adverse_is_liq = (liq >= stop) if d == 1 else (liq <= stop)
        o, h, l, c = float(row.open), float(row.high), float(row.low), float(row.close)
        hit_bad = (l <= adverse) if d == 1 else (h >= adverse)
        hit_good = ((h >= target) if d == 1 else (l <= target)) and not (m and m["partial_done"])
        exit_px, reason = None, None
        if hit_bad:                           # stop first if both are inside the candle
            gap = (o < adverse) if d == 1 else (o > adverse)
            exit_px = o if gap else adverse
            reason = "liq" if adverse_is_liq else "stop"
            if (d == 1 and exit_px <= liq) or (d == -1 and exit_px >= liq):
                reason = "liq"
            elif m and reason == "stop":
                reason = "trail" if m["partial_done"] else ("breakeven" if m["be_moved"] else "stop")
        elif hit_good and m:                  # book half at the target, trail the rest
            hours = max((ct - opened).total_seconds() / 3600, 0)
            frac = m["partial_frac"]
            part = pnl_part(t, target, hours, frac)
            t["partials"].append(dict(time=ct.isoformat(), time_uae=uae(ct), price=target, frac=frac,
                                      reason="half closed at target", **{k: r2(v, 6) for k, v in part.items()}))
            m.update(partial_done=True, remaining_frac=1.0 - frac, trail_active=True,
                     best_price=(h if d == 1 else l))
            t["deadline"] = (opened + timedelta(hours=t["max_hold_hours"] * m["runner_hold_mult"])).isoformat()
            be = breakeven_price(t)
            if not m["be_moved"]:
                m.update(be_moved=True, be_price=be)
            _tighten(t, be)
            ev = dict(time_uae=uae(ct), kind="partial", text=f"half closed at target {app.fmt_price(target)} "
                      f"(${part['pnl_usd']:+,.2f} booked); rest trails, stop {app.fmt_price(t['stop'])}",
                      pnl_usd=r2(part["pnl_usd"]))
            m["events"].append(ev)
            if events is not None:
                events.append(dict(ev, id=t["id"], pair=t["pair"], style=t["style"], side=t["side"]))
        elif hit_good:
            exit_px, reason = target, "target"
        elif ct >= deadline:
            exit_px, reason = c, ("timeout_runner" if m and m["partial_done"] else "timeout")
        t["last_checked"] = ct.isoformat()
        t["last_price"] = c
        if reason:
            exit_time = ct                                # candle close time = when we know
            hours = max((exit_time - opened).total_seconds() / 3600, 0)
            if not m or not t.get("partials"):
                return dict(exit_price=exit_px, reason=reason, exit_time=exit_time.isoformat(),
                            held_hours=hours, **pnl_breakdown(t, exit_px, hours, liquidated=(reason == "liq")))
            fin = pnl_part(t, exit_px, hours, m["remaining_frac"], liquidated=(reason == "liq"))
            parts = t["partials"]
            agg = {k: fin[k] + sum(p[k] for p in parts) for k in ("gross_usd", "fees_usd", "funding_usd",
                                                               "slippage_usd", "pnl_usd")}
            pnl = agg["pnl_usd"]
            avg_exit = (exit_px * m["remaining_frac"] + sum(p["price"] * p["frac"] for p in parts))
            return dict(exit_price=exit_px, reason=reason, exit_time=exit_time.isoformat(), held_hours=hours,
                        exit_fill=fin["exit_fill"], **agg, pnl_pct_collateral=pnl / t["collateral"] * 100,
                        r_multiple=pnl / t["planned_risk_usd"] if t["planned_risk_usd"] > 0 else 0.0,
                        final_part_pnl_usd=fin["pnl_usd"], avg_exit_price=avg_exit)
        if m:                                             # move the stop for the next candle
            moved = []
            if not m["be_moved"] and ((h >= m["be_trigger"]) if d == 1 else (l <= m["be_trigger"])):
                be = breakeven_price(t)
                m.update(be_moved=True, be_price=be)
                if _tighten(t, be):
                    moved.append(f"stop moved to break-even + costs {app.fmt_price(be)} (trade reached +1R)")
            if m["trail_active"]:
                m["best_price"] = max(m["best_price"], h) if d == 1 else min(m["best_price"], l)
                a, src = atr_lookup(ct) if atr_lookup else (fallback_atr(t), "ATR estimate from entry")
                m["last_atr"], m["atr_source"] = a, src
                trail = m["best_price"] - d * m["trail_mult"] * a
                old = t["stop"]
                if _tighten(t, trail):
                    m["trail_stop"] = t["stop"]
                    if abs(t["stop"] - old) / old > 0.001:
                        moved.append(f"trailing stop tightened to {app.fmt_price(t['stop'])}")
            for txt in moved:
                ev = dict(time_uae=uae(ct), kind="stop_move", text=txt, stop=t["stop"])
                m["events"].append(ev)
                if events is not None:
                    events.append(dict(ev, id=t["id"], pair=t["pair"], style=t["style"], side=t["side"]))
            if len(m["events"]) > 40:
                m["events"] = m["events"][-40:]
    return None


def unrealised(t: dict, now: datetime) -> dict | None:
    """Unrealised P&L of what is still open (the runner half after a partial close)."""
    if t.get("last_price") is None:
        return None
    hours = (now - parse_t(t["opened_at"])).total_seconds() / 3600
    return pnl_part(t, t["last_price"], hours, remaining_frac(t))


# ---------------------------------------------------------------- features & tags

def _klines_fetcher(pair, tf, n):
    return app.load_klines(pair, tf, n, budget_s=15)


def compute_features(sig: dict, fetcher=None) -> dict:
    """Snapshot of the setup at entry (indicators recomputed from live candles)."""
    style, d = sig["style"], (1 if sig["side"] == "LONG" else -1)
    cfg = style_cfg(style)
    f = dict(pair=sig["pair"], style=style, side=sig["side"], confidence=sig.get("confidence"),
             entry_tf=cfg["primary"], backtest_exp_r=sig.get("backtest_expectancy_r"),
             backtest_trades=sig.get("backtest_trades"), backtest_win_rate=sig.get("backtest_win_rate"),
             funding_rate_8h=sig.get("funding_rate_8h"),
             hour_uae=parse_t(sig.get("opened_at") or utcnow().isoformat()).astimezone(app.LOCAL_TZ).hour)
    try:
        mtf = app.multi_timeframe(sig["pair"], style, cfg["st_period"], cfg["st_mult"],
                                  fetcher=fetcher or _klines_fetcher)
        prim = mtf["tfs"][mtf["primary"]]
        dd = prim["data"]
        last = dd.iloc[-1]
        atr_med = float(dd["atr"].tail(100).median())
        f.update(
            score=r2(mtf["score"], 1), adx=r2(prim["adx"], 1), rsi=r2(prim["rsi"], 1),
            atr_pct=r2(prim["atr"] / prim["close"] * 100, 3),
            atr_vs_normal=r2(prim["atr"] / atr_med, 2) if atr_med > 0 else None,
            supertrend_dist_pct=r2(d * (last["close"] - last["st_line"]) / last["close"] * 100, 3),
            ema_alignment=int(sum([d * (last["ema9"] - last["ema21"]) > 0, d * (last["ema21"] - last["ema50"]) > 0,
                                   d * (last["close"] - (last["ema200"] if pd.notna(last["ema200"]) else last["ema50"])) > 0])),
            confluence=int(sum(1 for r in mtf["tfs"].values() if r["st_dir"] == d)),
            timeframes=len(mtf["tfs"]),
            higher_tf=mtf["higher"],
            higher_tf_agrees=bool(mtf["tfs"].get(mtf["higher"], {}).get("st_dir") == d),
        )
    except Exception as e:
        f["feature_error"] = str(e)[:120]
    return f


def entry_tags(f: dict) -> list[str]:
    d = 1 if f["side"] == "LONG" else -1
    tags = []
    if f.get("adx") is not None and f["adx"] < 20:
        tags.append("low_adx")
    if f.get("higher_tf_agrees") is False:
        tags.append("against_higher_tf")
    if f.get("confluence") is not None and f["confluence"] < f.get("timeframes", 3):
        tags.append("weak_confluence")
    if f.get("atr_vs_normal") is not None and f["atr_vs_normal"] >= 1.5:
        tags.append("high_volatility")
    fr = f.get("funding_rate_8h")
    if fr is not None and d * fr >= 0.0003:
        tags.append("adverse_funding")
    if f.get("rsi") is not None and ((d == 1 and f["rsi"] >= 70) or (d == -1 and f["rsi"] <= 30)):
        tags.append("stretched_rsi")
    er, n = f.get("backtest_exp_r"), f.get("backtest_trades") or 0
    if er is None or er < 0.1 or n < 10:
        tags.append("thin_backtest_edge")
    return tags


def postmortem_tags(t: dict) -> list[str]:
    if t["pnl_usd"] > 0:
        return []
    tags = list(t.get("entry_tags", []))
    if t["reason"] in ("stop", "liq", "breakeven") and t["held_hours"] < 0.25 * t["max_hold_hours"]:
        tags.insert(0, "quick_stop")
    if t["reason"] in ("timeout", "timeout_runner"):
        tags.insert(0, "timeout_loss")
    return tags


# ---------------------------------------------------------------- learning

def buckets_for(pair, style, side, tags) -> list[str]:
    out = [f"combo|{pair}|{style}|{side}", f"setup|{style}|{side}"]
    out += [f"cond|{style}|{side}|{tg}" for tg in tags if tg in ENTRY_TAGS]
    return out


def bucket_label(key: str) -> str:
    p = key.split("|")
    if p[0] == "combo":
        return f"{p[1]} {side_word(p[2], p[3])}"
    if p[0] == "setup":
        return f"{side_word(p[1], p[2])}"
    return f"{side_word(p[1], p[2])} on {TAG_WORDS.get(p[3], p[3])} setups"


def bucket_stats(trades: list[dict]) -> dict:
    n = len(trades)
    if not n:
        return dict(n=0, wins=0, win_rate=0.0, exp_r=0.0, exp_usd=0.0, total_usd=0.0)
    wins = sum(t["pnl_usd"] > 0 for t in trades)
    return dict(n=n, wins=wins, win_rate=round(wins / n * 100, 1),
                exp_r=round(sum(t["r_multiple"] for t in trades) / n, 3),
                exp_usd=round(sum(t["pnl_usd"] for t in trades) / n, 2),
                total_usd=round(sum(t["pnl_usd"] for t in trades), 2))


def target_penalty(s: dict) -> int:
    if s["n"] < MIN_SAMPLE or s["exp_r"] >= 0:
        return 0
    if s["exp_r"] <= -0.6:
        return 15
    if s["exp_r"] <= -0.3:
        return 10
    return 5


def _log_change(learning, now, key, change, reason, stats, out_changes):
    e = dict(time_utc=now.isoformat(), time_uae=uae(now), bucket=key, label=bucket_label(key),
             change=change, reason=reason, stats=stats)
    learning["changes"].append(e)
    out_changes.append(e)


def learn(trades_doc: dict, learning: dict, now: datetime) -> list[dict]:
    """Recompute every bucket from closed trades; apply bounded changes. Returns changes made."""
    closed = sorted([t for t in trades_doc["trades"] if t["status"] == "closed"], key=lambda t: t["exit_time"])
    groups: dict[str, list] = {}
    for t in closed:
        for k in buckets_for(t["pair"], t["style"], t["side"], t.get("entry_tags", [])):
            groups.setdefault(k, []).append(t)
    changes = []
    for k in sorted(set(groups) | set(learning["buckets"])):
        tr = groups.get(k, [])
        b = learning["buckets"].setdefault(k, dict(penalty=0, blocked=False, blocked_until=None,
                                                   seen_while_blocked=0, evaluated_n=0))
        s = bucket_stats(tr[-WINDOW:])
        s_all = bucket_stats(tr)
        b["stats"], b["stats_all"], b["label"] = s, s_all, bucket_label(k)
        # block expiry (time based) - re-test
        if b["blocked"] and b.get("blocked_until") and now >= parse_t(b["blocked_until"]):
            b["blocked"], b["blocked_until"], b["block_started"] = False, None, None
            _log_change(learning, now, k, "unblocked (14-day re-test)",
                        f"block period ended; trading again at +{b['penalty']} confidence", s, changes)
        if len(tr) == b.get("evaluated_n", 0):
            continue                                      # nothing new to learn
        b["evaluated_n"] = len(tr)
        # early unblock: probes since the block made money
        if b["blocked"] and b.get("block_started"):
            since = [t for t in tr if parse_t(t["exit_time"]) > parse_t(b["block_started"])]
            if len(since) >= 3 and sum(t["pnl_usd"] for t in since) > 0:
                b["blocked"], b["blocked_until"], b["block_started"] = False, None, None
                _log_change(learning, now, k, "unblocked early",
                            f"{len(since)} probe trades since the block made "
                            f"${sum(t['pnl_usd'] for t in since):+.0f}", s, changes)
        # confidence penalty
        tp = target_penalty(s)
        old = b["penalty"]
        new = tp if tp > old else max(tp, old - RELAX_STEP)
        new = min(new, MAX_PENALTY)
        if new != old:
            b["penalty"] = new
            why = (f"lost money: {s['n'] - s['wins']} of {s['n']} losers, {s['exp_r']:+.2f}R per trade"
                   if new > old else f"recovering: {s['wins']} of {s['n']} won, {s['exp_r']:+.2f}R per trade")
            _log_change(learning, now, k, f"min confidence {BASE_MIN_CONF + old}% -> {BASE_MIN_CONF + new}%",
                        why, s, changes)
        # block
        if (not b["blocked"] and s["n"] >= BLOCK_SAMPLE and s["exp_r"] <= BLOCK_EXP_R
                and s["win_rate"] <= BLOCK_MAX_WR):
            b["blocked"], b["seen_while_blocked"] = True, 0
            b["block_started"] = now.isoformat()
            b["blocked_until"] = (now + timedelta(days=BLOCK_DAYS)).isoformat()
            _log_change(learning, now, k, f"blocked until {uae(b['blocked_until'])} "
                        f"(1 in {PROBE_EVERY} signals still traded as a probe)",
                        f"clearly losing: {s['wins']} of {s['n']} won, {s['exp_r']:+.2f}R per trade", s, changes)
    return changes


def judge_signal(sig: dict, tags: list[str], learning: dict, dry=False) -> dict:
    """Apply the learned rules to one NEW GO signal."""
    keys = buckets_for(sig["pair"], sig["style"], sig["side"], tags)
    bs = {k: learning["buckets"].get(k) for k in keys if learning["buckets"].get(k)}
    pen_key = max(bs, key=lambda k: bs[k]["penalty"], default=None)
    penalty = bs[pen_key]["penalty"] if pen_key else 0
    required = BASE_MIN_CONF + min(penalty, MAX_PENALTY)
    blocked = [k for k, b in bs.items() if b.get("blocked")]
    res = dict(required_confidence=required, penalty_bucket=bucket_label(pen_key) if penalty else None,
               blocked_buckets=[bucket_label(k) for k in blocked], probe=False, allowed=True, reason=None)
    if blocked:
        probe = False
        for k in blocked:
            b = learning["buckets"][k]
            n = b.get("seen_while_blocked", 0) + 1
            if not dry:
                b["seen_while_blocked"] = n
            probe = probe or (n % PROBE_EVERY == 0)
        if probe:
            res.update(probe=True, reason=f"probe trade for blocked {', '.join(res['blocked_buckets'])}")
        else:
            res.update(allowed=False, reason=f"blocked by learning: {', '.join(res['blocked_buckets'])}")
            return res
    if sig.get("confidence") is not None and sig["confidence"] < required and not res["probe"]:
        res.update(allowed=False, reason=f"confidence {sig['confidence']}% below the {required}% now needed "
                                          f"for {res['penalty_bucket']}")
    return res


def lessons(learning: dict, trades_doc: dict) -> list[str]:
    L = []
    for k, b in sorted(learning["buckets"].items(), key=lambda kv: (-kv[1].get("penalty", 0), kv[0])):
        s = b.get("stats") or {}
        if b.get("blocked"):
            L.append(f"{bucket_label(k)} lost {s['n'] - s['wins']} of {s['n']} ({s['exp_r']:+.2f}R each) "
                     f"- blocked until {uae(b['blocked_until'])}, 1 in {PROBE_EVERY} signals still tested.")
        elif b.get("penalty", 0) > 0:
            L.append(f"{bucket_label(k)} lost {s['n'] - s['wins']} of {s['n']} ({s['exp_r']:+.2f}R each) "
                     f"- now needs {BASE_MIN_CONF + b['penalty']}% confidence.")
    losses = [t for t in trades_doc["trades"] if t["status"] == "closed" and t["pnl_usd"] <= 0]
    if losses:
        cnt = {}
        for t in losses:
            for tg in t.get("loss_tags", []):
                cnt[tg] = cnt.get(tg, 0) + 1
        for tg, c in sorted(cnt.items(), key=lambda kv: -kv[1])[:4]:
            L.append(f"{c} of {len(losses)} losses had: {TAG_WORDS.get(tg, tg)}.")
    return L


# ---------------------------------------------------------------- open / close

def open_trade(sig: dict, now: datetime, features: dict, tags: list[str], note: str = "", probe=False,
               funding=None, required=None) -> dict:
    style = sig["style"]
    cfg = style_cfg(style)
    d = 1 if sig["side"] == "LONG" else -1
    entry = float(sig["entry"])
    slip = cfg["slippage"]
    entry_fill = entry * (1 + d * slip)
    fr = funding or (sig.get("funding_rate_8h"), sig.get("funding_source"), False)
    if fr[0] is None:
        fr = fetch_funding(sig["pair"])
    stop, target = float(sig["stop"]), float(sig["target"])
    liq = app.liquidation_price(entry_fill, LEVERAGE, d)
    mh = max_hold_hours(style)
    tid = f"{sig['pair']}-{style}-{sig['side']}-{now:%Y%m%d%H%M%S}"
    features = dict(features, funding_rate_8h=fr[0])
    return dict(
        id=tid, status="open", pair=sig["pair"], style=style, side=sig["side"], dir=d,
        entry_tf=cfg["primary"], confidence=sig.get("confidence"), required_confidence=required,
        opened_at=now.isoformat(), signal_time=sig.get("signal_time") or now.isoformat(),
        entry=entry, entry_fill=entry_fill, stop=stop, target=target, liquidation=liq,
        notional=NOTIONAL, leverage=LEVERAGE, collateral=COLLATERAL, qty=NOTIONAL / entry_fill,
        planned_risk_usd=NOTIONAL * abs(entry_fill - stop) / entry_fill,
        slippage=slip, funding_rate_8h=float(fr[0]), funding_source=fr[1],
        max_hold_hours=mh, deadline=(now + timedelta(hours=mh)).isoformat(),
        last_checked=None, last_price=None, features=features, entry_tags=tags,
        probe=probe, note=note,
    )


def closed_view(t):
    return dict(id=t["id"], pair=t["pair"], style=t["style"], entry_tf=t["entry_tf"], side=t["side"],
                entry=t["entry"], exit=r2(t["exit_price"], 8), reason=t["reason"],
                pnl_usd=r2(t["pnl_usd"]), pnl_pct_collateral=r2(t["pnl_pct_collateral"]),
                fees_usd=r2(t["fees_usd"]), funding_usd=r2(t["funding_usd"]), slippage_usd=r2(t["slippage_usd"]),
                r_multiple=r2(t["r_multiple"]), held=dur(t["held_hours"]), held_hours=r2(t["held_hours"]),
                opened_uae=uae(t["opened_at"]), closed_uae=uae(t["exit_time"]),
                loss_tags=t.get("loss_tags", []), probe=t.get("probe", False),
                reason_text=REASON_TEXT.get(t["reason"], t["reason"]),
                half_closed_at_target=bool(t.get("partials")),
                booked_at_target_usd=r2(booked_partial_usd(t)) if t.get("partials") else None,
                final_part_pnl_usd=r2(t.get("final_part_pnl_usd")) if t.get("partials") else None,
                initial_stop=(t.get("mgmt") or {}).get("initial_stop", t.get("stop")))


def opened_view(t):
    return dict(id=t["id"], pair=t["pair"], style=t["style"], entry_tf=t["entry_tf"], side=t["side"],
                confidence=t["confidence"], entry=t["entry"], entry_fill=r2(t["entry_fill"], 8), stop=t["stop"],
                target=t["target"], liquidation=r2(t["liquidation"], 8), notional=t["notional"],
                collateral=t["collateral"], leverage=t["leverage"], opened_uae=uae(t["opened_at"]),
                closes_by_uae=uae(t["deadline"]), entry_tags=t["entry_tags"], probe=t["probe"], note=t["note"])


def open_view(t, now):
    u = unrealised(t, now)
    return dict(id=t["id"], pair=t["pair"], style=t["style"], entry_tf=t["entry_tf"], side=t["side"],
                entry=t["entry"], stop=t["stop"], target=t["target"], last_price=t.get("last_price"),
                unrealised_pnl_usd=r2(u["pnl_usd"]) if u else None,
                unrealised_pct_collateral=r2(u["pnl_pct_collateral"]) if u else None,
                held=dur((now - parse_t(t["opened_at"])).total_seconds() / 3600),
                opened_uae=uae(t["opened_at"]), closes_by_uae=uae(t["deadline"]),
                last_checked_uae=uae(t["last_checked"]) if t.get("last_checked") else None,
                **mgmt_view(t))


def mgmt_view(t) -> dict:
    m = t.get("mgmt")
    if not m:
        return {}
    return dict(initial_stop=m["initial_stop"], breakeven_moved=m["be_moved"],
                breakeven_at=r2(m["be_trigger"], 8), half_closed_at_target=m["partial_done"],
                booked_at_target_usd=r2(booked_partial_usd(t)) if t.get("partials") else None,
                remaining_pct=round(m["remaining_frac"] * 100), trailing=m["trail_active"],
                trail_stop=r2(m.get("trail_stop"), 8), managed_since_uae=uae(m["since"]))


def account_state(trades_doc, now, extra_open=None) -> dict | None:
    """Practice account: starting balance + realised P&L after costs (no compounding: size stays fixed).
    Returns None when no account has been set (--starting-balance)."""
    acc = trades_doc.get("account")
    if not acc or acc.get("starting_balance") is None:
        return None
    start = float(acc["starting_balance"])
    since = parse_t(acc["start_time"]) if acc.get("start_time") else None
    inside = [t for t in trades_doc["trades"] if since is None or parse_t(t["opened_at"]) >= since]
    closed = [t for t in inside if t["status"] == "closed"]
    opens = [t for t in inside if t["status"] == "open"]
    booked_open = sum(booked_partial_usd(t) for t in opens)        # half closes already booked
    realised = sum(t["pnl_usd"] for t in closed) + booked_open
    value = start + realised
    in_use = sum(t.get("collateral", COLLATERAL) * remaining_frac(t) for t in opens)
    unr = [unrealised(t, now) for t in opens]
    unr_usd = sum(u["pnl_usd"] for u in unr if u)
    equity = value + unr_usd
    free = value - in_use
    return dict(
        starting_balance_usd=r2(start), start_uae=uae(acc["start_time"]) if acc.get("start_time") else None,
        realised_pnl_usd=r2(realised), booked_on_open_trades_usd=r2(booked_open), account_value_usd=r2(value),
        collateral_in_use_usd=r2(in_use), open_trades=len(opens), free_cash_usd=r2(free),
        unrealised_pnl_usd=r2(unr_usd), equity_usd=r2(equity),
        return_pct=r2((value - start) / start * 100), equity_return_pct=r2((equity - start) / start * 100),
        collateral_per_trade_usd=COLLATERAL, can_open_new_trade=free >= COLLATERAL,
        sizing="fixed $1,000 collateral ($5,000 at 5x) per trade - not compounding yet")


def bookings(trades_doc) -> list[tuple[datetime, float]]:
    """Realised P&L events (time, usd) inside the account window: half closes and final closes."""
    acc = trades_doc.get("account") or {}
    since = parse_t(acc["start_time"]) if acc.get("start_time") else None
    out = []
    for t in trades_doc["trades"]:
        if since is not None and parse_t(t["opened_at"]) < since:
            continue
        parts = t.get("partials") or []
        for p in parts:
            out.append((parse_t(p["time"]), float(p["pnl_usd"])))
        if t["status"] == "closed":
            out.append((parse_t(t["exit_time"]), float(t["pnl_usd"]) - sum(float(p["pnl_usd"]) for p in parts)))
    return sorted(out, key=lambda x: x[0])


def _period(B, start_value, t0, now, limit_pct):
    """Running realised P&L from t0 to now; sticky limit check. Returns dict."""
    run, peak, low, hit_at = 0.0, 0.0, 0.0, None
    limit = -abs(limit_pct) / 100 * start_value
    for tm, p in B:
        if tm < t0 or tm > now:
            continue
        run += p
        peak, low = max(peak, run), min(low, run)
        if hit_at is None and run <= limit + 1e-9:
            hit_at = tm
    return dict(pnl=run, peak=peak, low=low, limit=limit, hit_at=hit_at)


def protection_state(trades_doc, now, cfg=None) -> dict | None:
    """Account protection status. None when the rules are not switched on for this ledger."""
    rules = get_rules(trades_doc)
    if cfg is None:
        if not rules.get("protection"):
            return None
        cfg = dict(DEFAULT_PROTECTION, **rules["protection"])
    open_n = sum(t["status"] == "open" for t in trades_doc["trades"])
    loc = now.astimezone(app.LOCAL_TZ)
    day0 = loc.replace(hour=0, minute=0, second=0, microsecond=0)
    week0 = day0 - timedelta(days=day0.weekday())
    nxt_day, nxt_week = day0 + timedelta(days=1), week0 + timedelta(days=7)
    acc = trades_doc.get("account")
    blocks = []
    res = dict(rules=dict(cfg), basis="realised P&L after costs (closed trades + half closes booked), UAE time",
               day_uae=f"{day0:%a %d %b %Y}", week_uae=f"week from Mon {week0:%d %b %Y}")
    if acc and acc.get("starting_balance") is not None:
        start = float(acc["starting_balance"])
        B = bookings(trades_doc)
        sod = start + sum(p for tm, p in B if tm < day0)
        sow = start + sum(p for tm, p in B if tm < week0)
        dy = _period(B, sod, day0, now, cfg["daily_loss_pct"])
        wk = _period(B, sow, week0, now, cfg["weekly_loss_pct"])
        # profit lock: after reaching the goal, falling back to (1 - giveback) of the peak so far
        run, peak, armed_at, lock_at = 0.0, 0.0, None, None
        for tm, p in B:
            if tm < day0 or tm > now:
                continue
            run += p
            peak = max(peak, run)
            if armed_at is None and peak >= cfg["daily_goal_usd"] - 1e-9:
                armed_at = tm
            if armed_at is not None and lock_at is None and run <= peak * (1 - cfg["giveback_frac"]) + 1e-9:
                lock_at = tm
        res["today"] = dict(start_of_day_value_usd=r2(sod), realised_pnl_usd=r2(dy["pnl"]), peak_usd=r2(dy["peak"]),
                            loss_limit_usd=r2(dy["limit"]), loss_limit_pct=cfg["daily_loss_pct"],
                            limit_hit=dy["hit_at"] is not None, limit_hit_uae=uae(dy["hit_at"]) if dy["hit_at"] else None,
                            room_left_usd=r2(max(dy["pnl"] - dy["limit"], 0)) if dy["hit_at"] is None else 0.0,
                            daily_goal_usd=cfg["daily_goal_usd"], goal_reached=armed_at is not None,
                            profit_lock_level_usd=r2(peak * (1 - cfg["giveback_frac"])) if armed_at else None,
                            profit_lock_hit=lock_at is not None, profit_lock_uae=uae(lock_at) if lock_at else None)
        res["week"] = dict(start_of_week_value_usd=r2(sow), realised_pnl_usd=r2(wk["pnl"]),
                           loss_limit_usd=r2(wk["limit"]), loss_limit_pct=cfg["weekly_loss_pct"],
                           limit_hit=wk["hit_at"] is not None, limit_hit_uae=uae(wk["hit_at"]) if wk["hit_at"] else None)
        if wk["hit_at"]:
            blocks.append(dict(rule="weekly", reason="weekly limit", until_uae=uae(nxt_week),
                               why=f"weekly limit: the week is ${wk['pnl']:+,.0f} realised, past the "
                                   f"-{cfg['weekly_loss_pct']:g}% limit (${wk['limit']:,.0f}); no new trades until "
                                   f"Mon {nxt_week:%d %b} 00:00 UAE"))
        if dy["hit_at"]:
            blocks.append(dict(rule="daily_loss", reason="daily loss limit reached", until_uae=uae(nxt_day),
                               why=f"daily loss limit reached: today is ${dy['pnl']:+,.0f} realised, past the "
                                   f"-{cfg['daily_loss_pct']:g}% limit (${dy['limit']:,.0f}); no new trades until "
                                   f"00:00 UAE"))
        if lock_at:
            blocks.append(dict(rule="profit_lock", reason="profit lock", until_uae=uae(nxt_day),
                               why=f"profit lock: today peaked at ${peak:+,.0f} and fell back to half; "
                                   f"keeping the day green, no new trades until 00:00 UAE"))
    res["open"] = dict(open_trades=open_n, max_open=int(cfg["max_open"]), at_cap=open_n >= int(cfg["max_open"]))
    if open_n >= int(cfg["max_open"]):
        blocks.append(dict(rule="max_open", reason=f"max {int(cfg['max_open'])} open trades", until_uae=None,
                           why=f"max {int(cfg['max_open'])} open trades: {open_n} are open; new trades resume "
                               f"when fewer than {int(cfg['max_open'])} are open"))
    res["new_trades_allowed"] = not blocks
    res["blocked_by"] = blocks
    res["status_text"] = ("New trades allowed." if not blocks else
                          "New trades paused - " + "; ".join(b["reason"] for b in blocks) + ".")
    return res


def protection_flags(trades_doc, prot, now) -> list[dict]:
    """Mark day/week limits the first time they are seen so the routine tells the user once."""
    if not prot:
        return []
    flags = trades_doc.setdefault("protection_flags", {})
    new = []
    for b in prot["blocked_by"]:
        if b["rule"] == "max_open":
            continue
        key = f"{b['rule']}|{prot['week_uae'] if b['rule'] == 'weekly' else prot['day_uae']}"
        if key not in flags:
            flags[key] = now.isoformat()
            new.append(dict(rule=b["rule"], reason=b["reason"], why=b["why"], until_uae=b["until_uae"]))
    cutoff = now - timedelta(days=21)
    trades_doc["protection_flags"] = {k: v for k, v in flags.items() if parse_t(v) > cutoff}
    prot["just_triggered"] = new
    return new


def enable_protection(ledger: "Ledger", daily_goal: float | None = None, now=None) -> dict:
    """Switch on account protection + let-winners-run for this ledger (trades.json backed up first)."""
    now = now or utcnow()
    trades_doc, _ = ledger.load()
    rules = trades_doc.get("rules") or {}
    prot = dict(DEFAULT_PROTECTION, **(rules.get("protection") or {}))
    if daily_goal is not None:
        prot["daily_goal_usd"] = float(daily_goal)
    trades_doc["rules"] = dict(
        protection=prot, let_winners_run=dict(DEFAULT_RUNNERS, **(rules.get("let_winners_run") or {})),
        enabled_at=rules.get("enabled_at") or now.isoformat(), updated_at=now.isoformat())
    ledger._write(ledger.trades_path, trades_doc)
    return trades_doc["rules"]


def has_free_cash(trades_doc, now) -> bool:
    a = account_state(trades_doc, now)
    return a is None or a["free_cash_usd"] >= COLLATERAL - 1e-9


def set_starting_balance(ledger: "Ledger", amount: float, start_time: str | None = None) -> dict:
    """Store the practice account in trades.json (backed up first). Start = first practice trade if not given.
    Trades, processed signals and learning are not touched."""
    trades_doc, learning = ledger.load()
    if start_time is None:
        ts = [t["opened_at"] for t in trades_doc["trades"]]
        start_time = min(ts, key=parse_t) if ts else utcnow().isoformat()
    trades_doc["account"] = dict(starting_balance=float(amount), start_time=start_time,
                                 set_at=utcnow().isoformat(), sizing="fixed", collateral_per_trade=COLLATERAL)
    ledger._write(ledger.trades_path, trades_doc)
    return account_state(trades_doc, utcnow())


def totals(trades_doc, now):
    closed = [t for t in trades_doc["trades"] if t["status"] == "closed"]
    opens = [t for t in trades_doc["trades"] if t["status"] == "open"]
    wins = sum(t["pnl_usd"] > 0 for t in closed)
    unr = [unrealised(t, now) for t in opens]
    return dict(closed_trades=len(closed), wins=wins, losses=len(closed) - wins,
                win_rate_pct=r2(wins / len(closed) * 100, 1) if closed else None,
                total_pnl_usd=r2(sum(t["pnl_usd"] for t in closed)),
                avg_r=r2(sum(t["r_multiple"] for t in closed) / len(closed)) if closed else None,
                open_trades=len(opens),
                open_unrealised_pnl_usd=r2(sum(u["pnl_usd"] for u in unr if u)))


# ---------------------------------------------------------------- one run

def run(ledger: Ledger, scan: dict | None, now: datetime | None = None, candle_loader=None,
        feature_fetcher=None, funding_fn=None, allow_without_dedupe=False) -> dict:
    now = now or utcnow()
    funding_fn = funding_fn or fetch_funding
    trades_doc, learning = ledger.load()
    out = dict(ledger=str(ledger.root), run_time_uae=uae(now), opened=[], closed=[], not_opened=[],
               open_trades=[], totals={}, account=None, protection=None, partial_closes=[], stop_moves=[],
               learning={}, warnings=[])
    runners = get_rules(trades_doc).get("let_winners_run")
    events = []

    # 1. update open trades
    for t in [t for t in trades_doc["trades"] if t["status"] == "open"]:
        since = parse_t(t["last_checked"]) if t.get("last_checked") else parse_t(t["opened_at"])
        df, tf, src, warn = load_fine_candles(t["pair"], since, now, loader=candle_loader)
        if warn:
            out["warnings"].append(warn)
        atr_lookup = None
        if runners:
            init_mgmt(t, now, runners)
            atr_lookup = make_atr_lookup(t, now, loader=candle_loader)
        res = walk_trade(t, df, now, atr_lookup=atr_lookup, events=events)
        t["last_data"] = f"{tf} {src}" if tf else None
        if res:
            t.update(res, status="closed")
            t["loss_tags"] = postmortem_tags(t)
            out["closed"].append(closed_view(t))
    out["partial_closes"] = [e for e in events if e["kind"] == "partial"]
    out["stop_moves"] = [e for e in events if e["kind"] == "stop_move"]

    # 2. learn
    changes = learn(trades_doc, learning, now)

    # 3. new signals
    skipped, probes = [], []
    passed = []
    sigs = (scan or {}).get("new_signals_before_learning", (scan or {}).get("new_signals", [])) or []
    dedupe_off = (scan or {}).get("dedupe") == "off"
    run_key = (scan or {}).get("run_time_utc", now.isoformat())

    def prep(sig):                                  # funding + feature snapshot (downloads, so in parallel)
        s2 = dict(sig, opened_at=now.isoformat())
        fr = funding_fn(sig["pair"])
        s2["funding_rate_8h"], s2["funding_source"] = fr[0], fr[1]
        feats = compute_features(s2, fetcher=feature_fetcher)
        return s2, fr, feats
    todo = [s for s in sigs if f"{s['pair']}|{s['style']}|{s['side']}|{run_key}" not in trades_doc["processed_signals"]]
    prepped = {}
    if todo:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(6, len(todo))) as ex:
            for sig, res_ in zip(todo, ex.map(prep, todo)):
                prepped[id(sig)] = res_
    for sig in sigs:
        pkey = f"{sig['pair']}|{sig['style']}|{sig['side']}|{run_key}"
        if pkey in trades_doc["processed_signals"]:
            prev = trades_doc["processed_signals"][pkey]
            if prev.get("passed", True):
                passed.append(sig)
            else:
                skipped.append(dict(pair=sig["pair"], style=sig["style"], side=sig["side"],
                                    confidence=sig.get("confidence"), reason=prev.get("reason")))
            continue
        s2, fr, feats = prepped.get(id(sig)) or prep(sig)
        tags = entry_tags(feats)
        verdict = judge_signal(s2, tags, learning)
        rec = dict(time=now.isoformat(), passed=verdict["allowed"], reason=verdict["reason"])
        if not verdict["allowed"]:
            skipped.append(dict(pair=sig["pair"], style=sig["style"], entry_tf=sig.get("timeframe"),
                                side=sig["side"], confidence=sig.get("confidence"),
                                required_confidence=verdict["required_confidence"], reason=verdict["reason"]))
            trades_doc["processed_signals"][pkey] = rec
            continue
        passed.append(sig)
        if verdict["probe"]:
            probes.append(dict(pair=sig["pair"], style=sig["style"], side=sig["side"], reason=verdict["reason"]))
        if dedupe_off and not allow_without_dedupe:
            out["not_opened"].append(dict(pair=sig["pair"], style=sig["style"], side=sig["side"],
                                          why="scan ran without dedupe (--no-state): no trades opened"))
            continue
        trades_doc["processed_signals"][pkey] = rec
        if any(t["status"] == "open" and t["pair"] == sig["pair"] and t["style"] == sig["style"]
               for t in trades_doc["trades"]):
            out["not_opened"].append(dict(pair=sig["pair"], style=sig["style"], side=sig["side"],
                                          why="a paper trade for this pair + style is already open"))
            continue
        prot = protection_state(trades_doc, now)
        if prot and not prot["new_trades_allowed"]:
            b = prot["blocked_by"][0]
            out["not_opened"].append(dict(pair=sig["pair"], style=sig["style"], side=sig["side"],
                                          confidence=sig.get("confidence"), entry_tf=sig.get("timeframe"),
                                          why=b["why"], reason=b["reason"],
                                          all_reasons=[x["reason"] for x in prot["blocked_by"]]))
            continue
        if not has_free_cash(trades_doc, now):
            a = account_state(trades_doc, now)
            out["not_opened"].append(dict(pair=sig["pair"], style=sig["style"], side=sig["side"],
                                          confidence=sig.get("confidence"), entry_tf=sig.get("timeframe"),
                                          why="not enough free cash", reason="not enough free cash",
                                          free_cash_usd=a["free_cash_usd"], needed_usd=COLLATERAL))
            continue
        t = open_trade(s2, now, feats, tags, probe=verdict["probe"], funding=fr,
                       required=verdict["required_confidence"])
        if runners:
            init_mgmt(t, now, runners)
        trades_doc["trades"].append(t)
        out["opened"].append(opened_view(t))

    # prune very old processed keys
    cutoff = now - timedelta(days=30)
    trades_doc["processed_signals"] = {k: v for k, v in trades_doc["processed_signals"].items()
                                       if parse_t(v["time"]) > cutoff}
    learning["updated_uae"] = uae(now)
    prot = protection_state(trades_doc, now)
    protection_flags(trades_doc, prot, now)
    out["protection"] = prot
    ledger.save(trades_doc, learning)
    if changes:
        with open(ledger.changes_txt, "a", encoding="utf-8") as fh:
            for c in changes:
                s = c["stats"]
                fh.write(f"{c['time_uae']} | {c['label']}: {c['change']} - {c['reason']} "
                         f"(n={s['n']}, win rate {s['win_rate']}%, {s['exp_r']:+.2f}R, ${s['total_usd']:+.0f})\n")

    out["open_trades"] = [open_view(t, now) for t in trades_doc["trades"] if t["status"] == "open"]
    out["totals"] = totals(trades_doc, now)
    out["account"] = account_state(trades_doc, now)
    out["learning"] = dict(
        skipped=skipped, probes=probes, changes=changes,
        active_rules=[dict(bucket=k, label=bucket_label(k), min_confidence=BASE_MIN_CONF + b["penalty"],
                           blocked=b["blocked"], blocked_until_uae=uae(b["blocked_until"]) if b.get("blocked_until") else None,
                           stats=b.get("stats"))
                      for k, b in learning["buckets"].items() if b["penalty"] > 0 or b["blocked"]],
        lessons=lessons(learning, trades_doc))
    out["_passed_signals"] = passed
    return out


def merge_into_scan(scan_path: Path, scan: dict, paper: dict) -> dict:
    """Add the paper section; NEW signals the learning filter skipped are removed from
    new_signals (kept in new_signals_before_learning) so alerts only show the ones that pass."""
    passed = paper.pop("_passed_signals", None)
    if passed is not None and scan.get("new_signals") is not None and "learning" in paper:
        scan["new_signals_before_learning"] = scan.get("new_signals_before_learning", scan["new_signals"])
        keep = {(s["pair"], s["style"], s["side"]) for s in passed}
        scan["new_signals"] = [s for s in scan["new_signals_before_learning"] if (s["pair"], s["style"], s["side"]) in keep]
        scan.setdefault("counts", {})["new_signals"] = len(scan["new_signals"])
        scan["counts"]["skipped_by_learning"] = len(paper["learning"]["skipped"])
    scan["paper"] = paper
    tmp = scan_path.with_suffix(".paper.tmp")
    tmp.write_text(json.dumps(scan, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, scan_path)
    return scan


# ---------------------------------------------------------------- seeding from state.json (read-only)

def seed_from_state(ledger: Ledger, state_path: Path, now=None, day_uae: str | None = None) -> dict:
    now = now or utcnow()
    st_ = json.loads(Path(state_path).read_text(encoding="utf-8"))       # read only, never written
    day_uae = day_uae or f"{now.astimezone(app.LOCAL_TZ):%Y-%m-%d}"
    want = []
    for key, a in st_.get("alerts", {}).items():
        pair, style, side = key.split("|")
        at = parse_t(a["time"])
        if f"{at.astimezone(app.LOCAL_TZ):%Y-%m-%d}" == day_uae and style in app.STYLES:
            want.append((pair, style, side, a))
    report = dict(checked=[], opened=[])
    if not want:
        return report
    res = app.scan_pairs(sorted({w[0] for w in want}), sorted({w[1] for w in want}, key=list(app.STYLES).index),
                         COLLATERAL, [], workers=4, leverage=LEVERAGE)
    by = {(r["pair"], r["style"]): r for r in res["results"]}
    trades_doc, learning = ledger.load()
    for pair, style, side, a in want:
        r = by.get((pair, style))
        pkey = f"{pair}|{style}|{side}|seed:{a['time']}"
        ok = r is not None and r.get("ok") and r["status"] == f"GO {side}"
        item = dict(pair=pair, style=style, side=side, alert_uae=uae(a["time"]), alert_price=a["price"],
                    status_now=(r or {}).get("status"), still_valid=bool(ok))
        report["checked"].append(item)
        if not ok or pkey in trades_doc["processed_signals"]:
            item["opened"] = False
            item["why"] = "already seeded" if ok else "no longer a GO"
            continue
        if any(t["status"] == "open" and t["pair"] == pair and t["style"] == style for t in trades_doc["trades"]):
            item["opened"], item["why"] = False, "already open"
            continue
        prot = protection_state(trades_doc, now)
        if prot and not prot["new_trades_allowed"]:
            item["opened"], item["why"] = False, prot["blocked_by"][0]["reason"]
            continue
        if not has_free_cash(trades_doc, now):
            item["opened"], item["why"] = False, "not enough free cash"
            continue
        s = r["long"] if side == "LONG" else r["short"]
        sig = dict(pair=pair, style=style, side=side, confidence=s["confidence"], entry=s["entry"], stop=s["stop"],
                   target=s["target"], backtest_expectancy_r=s["bt_exp_r"], backtest_trades=s["bt_n"],
                   backtest_win_rate=s["bt_win_rate"], opened_at=now.isoformat(), signal_time=a["time"],
                   funding_rate_8h=r.get("funding_rate"))
        feats = compute_features(sig)
        tags = entry_tags(feats)
        t = open_trade(sig, now, feats, tags, funding=(r.get("funding_rate"), "scan", False), required=BASE_MIN_CONF,
                       note=f"Seeded late from the {uae(a['time'])} alert (alert price {a['price']}); "
                            f"entry is the price when the paper trade was opened, stop/target re-worked from it.")
        if get_rules(trades_doc).get("let_winners_run"):
            init_mgmt(t, now, get_rules(trades_doc)["let_winners_run"])
        trades_doc["trades"].append(t)
        trades_doc["processed_signals"][pkey] = dict(time=now.isoformat(), passed=True, reason="seed")
        item.update(opened=True, entry=t["entry"], stop=t["stop"], target=t["target"], opened_uae=uae(now))
        report["opened"].append(opened_view(t))
    ledger.save(trades_doc, learning)
    return report


# ---------------------------------------------------------------- summary

def fmtp(x):
    return "-" if x is None else app.fmt_price(float(x))


def protection_lines(prot: dict) -> list[str]:
    L = ["Protection: " + prot["status_text"]]
    if prot.get("today"):
        d = prot["today"]
        L.append(f"  Today (UAE): ${d['realised_pnl_usd']:+,.2f} realised vs daily loss limit ${d['loss_limit_usd']:,.2f} "
                 f"(-{d['loss_limit_pct']:g}% of ${d['start_of_day_value_usd']:,.2f})"
                 + (f" - HIT at {d['limit_hit_uae']}" if d["limit_hit"] else f" - ${d['room_left_usd']:,.2f} room left"))
        L.append(f"  Daily goal ${d['daily_goal_usd']:,.0f}: " + (
            ("profit lock HIT at " + d["profit_lock_uae"]) if d["profit_lock_hit"] else
            (f"reached, peak ${d['peak_usd']:+,.2f}; lock if it falls to ${d['profit_lock_level_usd']:+,.2f}")
            if d["goal_reached"] else f"not reached (peak ${d['peak_usd']:+,.2f})"))
    if prot.get("week"):
        w = prot["week"]
        L.append(f"  Week: ${w['realised_pnl_usd']:+,.2f} realised vs weekly limit ${w['loss_limit_usd']:,.2f} "
                 f"(-{w['loss_limit_pct']:g}% of ${w['start_of_week_value_usd']:,.2f})" + (" - HIT" if w["limit_hit"] else ""))
    o = prot["open"]
    L.append(f"  Open trades: {o['open_trades']} of max {o['max_open']}" + (" - at the cap" if o["at_cap"] else ""))
    for b in prot["blocked_by"]:
        if b.get("until_uae"):
            L.append(f"  Paused by {b['reason']} until {b['until_uae']}")
    return L


def summary_text(ledger: Ledger, now=None) -> str:
    now = now or utcnow()
    trades_doc, learning = ledger.load()
    tot = totals(trades_doc, now)
    L = [f"PRACTICE TRADING (paper money, $5,000 at 5x per trade) · {uae(now)}", ""]
    acc = account_state(trades_doc, now)
    if acc:
        L.append(f"Account: ${acc['account_value_usd']:,.2f} ({acc['return_pct']:+.2f}% on "
                 f"${acc['starting_balance_usd']:,.0f} since {acc['start_uae']}) · free cash ${acc['free_cash_usd']:,.2f} · "
                 f"in trades ${acc['collateral_in_use_usd']:,.0f} ({acc['open_trades']} open) · equity "
                 f"${acc['equity_usd']:,.2f} ({acc['equity_return_pct']:+.2f}%)")
        if not acc["can_open_new_trade"]:
            L.append("  New trades are paused until $1,000 is free.")
        L.append("")
    else:
        L += ["Account: no starting balance set (use --starting-balance).", ""]
    prot = protection_state(trades_doc, now)
    if prot:
        L += protection_lines(prot) + [""]
    if tot["closed_trades"]:
        L.append(f"Closed trades: {tot['closed_trades']} · won {tot['wins']} · lost {tot['losses']} · "
                 f"win rate {tot['win_rate_pct']:.0f}% · total P&L ${tot['total_pnl_usd']:+,.2f} after costs "
                 f"(avg {tot['avg_r']:+.2f}R)")
    else:
        L.append("Closed trades: none yet.")
    opens = [t for t in trades_doc["trades"] if t["status"] == "open"]
    L.append(f"Open trades: {len(opens)} · unrealised ${tot['open_unrealised_pnl_usd'] or 0:+,.2f}")
    for t in opens:
        v = open_view(t, now)
        u = "n/a yet" if v["unrealised_pnl_usd"] is None else f"${v['unrealised_pnl_usd']:+,.2f}"
        mg = ""
        if t.get("mgmt"):
            m = t["mgmt"]
            if m["partial_done"]:
                mg = f" · half booked ${booked_partial_usd(t):+,.2f} at target, rest trailing"
            elif m["be_moved"]:
                mg = " · stop at break-even"
        L.append(f"  • {t['pair']} {t['style']} {t['side']} from {fmtp(t['entry'])} (stop {fmtp(t['stop'])}, "
                 f"target {fmtp(t['target'])}) · now {fmtp(t.get('last_price'))} · {u}{mg} · open {v['held']}, "
                 f"closes by {v['closes_by_uae']}")
    closed = sorted([t for t in trades_doc["trades"] if t["status"] == "closed"], key=lambda t: t["exit_time"])
    if closed:
        L.append("")
        L.append("Last closed:")
        for t in closed[-10:]:
            L.append(f"  • {t['pair']} {t['style']} {t['side']} {fmtp(t['entry'])} -> {fmtp(t['exit_price'])} "
                     f"{t['reason']}{' (half at target)' if t.get('partials') else ''} · ${t['pnl_usd']:+,.2f} ({t['pnl_pct_collateral']:+.1f}%) · held "
                     f"{dur(t['held_hours'])} · closed {uae(t['exit_time'])}"
                     + (f" · why: {', '.join(TAG_WORDS.get(x, x) for x in t['loss_tags'][:2])}" if t.get("loss_tags") else ""))
    L.append("")
    les = lessons(learning, trades_doc)
    L.append("What it has learned:" if les else "What it has learned: nothing yet (needs at least 5 closed trades in a group).")
    L += [f"  • {x}" for x in les]
    if learning["changes"]:
        L.append("")
        L.append("Recent rule changes:")
        for c in learning["changes"][-5:]:
            L.append(f"  • {c['time_uae']}: {c['label']} - {c['change']} ({c['reason']})")
    L.append("")
    L.append("Practice only - no real money. Not financial advice.")
    return "\n".join(L)


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Paper (practice) trading on the scanner's GO signals.")
    ap.add_argument("--ledger", default=DEFAULT_LEDGER, help=f"ledger folder (default {DEFAULT_LEDGER})")
    ap.add_argument("--run", action="store_true", help="update open trades, learn, open trades for NEW signals")
    ap.add_argument("--scan", help="scanner JSON (last_run.json); its paper section is added in place")
    ap.add_argument("--out", help="with --run and no --scan: write the paper JSON here")
    ap.add_argument("--summary", action="store_true", help="print a plain summary")
    ap.add_argument("--seed-from-state", help="open paper trades for today's alerts in this state.json "
                                              "if still GO (state file is only read)")
    ap.add_argument("--starting-balance", type=float, help="set the practice account's starting balance (USD), "
                    "counted from the first practice trade unless --account-start is given")
    ap.add_argument("--account-start", help="ISO time the account starts (default: first practice trade)")
    ap.add_argument("--enable-protection", action="store_true",
                    help="switch on account protection (daily/weekly loss limits, profit lock, max 6 open) and "
                         "let-winners-run (break-even at +1R, half at target, ATR trail) for this ledger")
    ap.add_argument("--daily-goal", type=float, help="with --enable-protection: daily goal for the profit lock (USD)")
    ap.add_argument("--allow-no-dedupe", action="store_true", help="open trades even if the scan used --no-state (tests)")
    a = ap.parse_args(argv)
    led = Ledger(a.ledger)
    if a.starting_balance is not None:
        led.lock()
        try:
            print(json.dumps(set_starting_balance(led, a.starting_balance, a.account_start), indent=2, default=str))
        finally:
            led.unlock()
        if not (a.run or a.seed_from_state or a.summary or a.enable_protection):
            return 0
    if a.enable_protection or a.daily_goal is not None:
        led.lock()
        try:
            print(json.dumps(enable_protection(led, a.daily_goal), indent=2, default=str))
        finally:
            led.unlock()
        if not (a.run or a.seed_from_state or a.summary):
            return 0
    if a.summary and not (a.run or a.seed_from_state):
        print(summary_text(led))
        return 0
    led.lock()
    try:
        if a.seed_from_state:
            print(json.dumps(seed_from_state(led, Path(a.seed_from_state)), indent=2, default=str))
        if a.run:
            scan = None
            if a.scan:
                try:
                    scan = json.loads(Path(a.scan).read_text(encoding="utf-8"))
                except Exception as e:
                    print(f"warning: could not read scan {a.scan}: {e}", file=sys.stderr)
            try:
                paper = run(led, scan, allow_without_dedupe=a.allow_no_dedupe)
            except Exception as e:
                paper = dict(error=f"{type(e).__name__}: {e}"[:300])
                print(f"paper trader error: {paper['error']}", file=sys.stderr)
            (led.root / "last_paper.json").write_text(
                json.dumps({k: v for k, v in paper.items() if k != "_passed_signals"}, indent=2, default=str))
            if scan is not None and a.scan:
                merge_into_scan(Path(a.scan), scan, paper)
            else:
                paper.pop("_passed_signals", None)
                if a.out:
                    Path(a.out).write_text(json.dumps(paper, indent=2, default=str))
                else:
                    print(json.dumps(paper, indent=2, default=str))
        if a.summary:
            print(summary_text(led))
    finally:
        led.unlock()
    return 0


if __name__ == "__main__":
    sys.exit(main())
