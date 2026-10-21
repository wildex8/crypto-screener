#!/usr/bin/env python3
"""
Multi-pair scanner for the Leveraged Crypto Trade Screener.

Scans many pairs for one or more trade styles, evaluating BOTH long and short
with exactly the same logic as the app (multi-timeframe score, Supertrend gate,
direction-specific backtest, liquidation checks, learning multiplier), and
reports NEW confirmed GO LONG / GO SHORT signals.

Usage:
  python scanner.py [--styles Day,Swing] [--pairs BTCUSDT,ETHUSDT,...] [--collateral 100]
                    [--state /workspace/scanner/state.json] [--json] [--all] [--no-state]

A signal is NEW if the same pair + style + side was not alerted in the last
12 hours, or if since then it went to NO-GO / the opposite side and came back.

NOT FINANCIAL ADVICE.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Quieten Streamlit's "no runtime" warnings - we only use the plain functions.
warnings.filterwarnings("ignore")
import streamlit.logger as _st_logger  # noqa: E402
_st_logger.set_log_level("error")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import app  # noqa: E402  (core functions; the Streamlit UI only runs under `streamlit run`)

for _name in list(logging.root.manager.loggerDict):      # Streamlit sets up its loggers on import
    if _name.startswith("streamlit"):
        logging.getLogger(_name).setLevel(logging.ERROR)

DEFAULT_STATE = "/workspace/scanner/state.json"
REALERT_HOURS = 12


# ---------------------------------------------------------------- state / dedupe

def load_state(path: Path) -> dict:
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(st, dict):
            st.setdefault("alerts", {})
            st.setdefault("status", {})
            return st
    except Exception:
        pass
    return {"alerts": {}, "status": {}}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def find_new_signals(results: list[dict], state: dict | None, now: datetime) -> list[dict]:
    """Pick NEW GO signals and update the state in place (if a state is given)."""
    new = []
    for r in results:
        if not r.get("ok"):
            continue                       # skipped pairs don't change the remembered status
        key_ps = f"{r['pair']}|{r['style']}"
        status = r["status"]               # "GO LONG", "GO SHORT" or "NO-GO"
        prev = (state or {}).get("status", {}).get(key_ps, {}).get("status")
        if status.startswith("GO"):
            side = status.split()[1]
            key = f"{key_ps}|{side}"
            last = (state or {}).get("alerts", {}).get(key)
            recent = False
            if last:
                try:
                    recent = now - datetime.fromisoformat(last["time"]) < timedelta(hours=REALERT_HOURS)
                except Exception:
                    recent = False
            came_back = prev is not None and prev != status      # was NO-GO / opposite side last run
            if state is None or not recent or came_back:
                new.append(r)
                if state is not None:
                    state["alerts"][key] = {"time": now.isoformat(), "confidence": r["best_confidence"],
                                            "price": r["price"]}
        if state is not None:
            state["status"][key_ps] = {"status": status, "time": now.isoformat()}
    return new


# ---------------------------------------------------------------- formatting

def signal_record(r: dict) -> dict:
    side = r["status"].split()[1]
    s = r["long"] if side == "LONG" else r["short"]
    return dict(pair=r["pair"], style=r["style"], side=side, confidence=s["confidence"],
                leverage=r["leverage"], collateral=r["collateral"], entry=s["entry"], stop=s["stop"],
                target=s["target"], liquidation=s["liquidation"], rr_after_costs=s["rr_after_costs"],
                reasons=s["reasons"], backtest_expectancy_r=s["bt_exp_r"],
                backtest_expectancy_pct=s["bt_exp_pct"], backtest_trades=s["bt_n"],
                backtest_win_rate=s["bt_win_rate"], source=r["source"], timeframe=r["primary_tf"],
                candle_time_utc=r["candle_time_utc"], candle_time_local=r["candle_time_local"])


def short_source(src: str) -> str:
    """'Binance (public data mirror) (BTCUSDT)' -> 'Binance mirror'."""
    names = []
    for part in (src or "").split(", "):
        n = part.split(" (")[0] + (" mirror" if "mirror" in part else "")
        if n not in names:
            names.append(n)
    return "+".join(names)


def fp(x) -> str:
    return app.fmt_price(float(x)) if x is not None else "-"


def text_summary(out: dict, show_all: bool) -> str:
    L = [f"Crypto screener scan · {out['run_time_local']} · {out['seconds']}s",
         f"Styles: {', '.join(out['styles'])} · scanned {out['counts']['pairs_scanned']} pairs "
         f"({out['counts']['evaluations']} pair/style checks) · GO now: {out['counts']['go_now']} · "
         f"NEW: {out['counts']['new_signals']} · skipped: {out['counts']['skipped']}"]
    if out["new_signals"]:
        L.append("")
        L.append("NEW SIGNALS")
        for s in out["new_signals"]:
            L.append(f"• {s['pair']} {s['style']} GO {s['side']} {s['confidence']}% at {s['leverage']}x")
            L.append(f"  entry {fp(s['entry'])} · stop {fp(s['stop'])} · target {fp(s['target'])} · "
                     f"liq {fp(s['liquidation'])} · R:R {s['rr_after_costs']:.2f} after costs")
            exp = "n/a" if s["backtest_expectancy_r"] is None else f"{s['backtest_expectancy_r']:+.2f}R"
            L.append(f"  backtest ({s['side'].lower()} only): {exp} over {s['backtest_trades']} trades")
            for reason in s["reasons"]:
                L.append(f"  - {reason}")
            L.append(f"  {s['source']} · {s['timeframe']} candle {s['candle_time_local']}")
    else:
        L.append("No new GO signals.")
    if out["skipped"]:
        L.append("Skipped: " + ", ".join(out["skipped"]))
    if show_all:
        L.append("")
        L.append(f"{'PAIR':<10} {'STYLE':<6} {'LONG':>8} {'SHORT':>8} {'SCORE':>6}  {'VERDICT':<9} SOURCE")
        for r in out["all"]:
            if not r["ok"]:
                L.append(f"{r['pair']:<10} {r['style']:<6} {'error: ' + (r.get('error') or '')[:50]}")
                continue
            lg, sh = f"{r['long']['confidence']}%", f"{r['short']['confidence']}%"
            L.append(f"{r['pair']:<10} {r['style']:<6} {lg:>8} {sh:>8} {r['score']:+6.0f}  {r['status']:<9} "
                     f"{short_source(r['source'])}")
    L.append("Not financial advice.")
    return "\n".join(L)


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Scan many crypto pairs for confirmed GO LONG / GO SHORT signals.")
    ap.add_argument("--styles", default="Day,Swing", help="comma list of Swing, Day, Scalp (default Day,Swing)")
    ap.add_argument("--pairs", default=",".join(app.SCAN_PAIRS), help="comma list of pairs (default: 20 liquid pairs)")
    ap.add_argument("--collateral", type=float, default=100.0, help="collateral in USDT (default 100)")
    ap.add_argument("--leverage", type=float, default=None, help="leverage for every style (default: each style's own)")
    ap.add_argument("--state", default=DEFAULT_STATE, help=f"state file for dedupe (default {DEFAULT_STATE})")
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    ap.add_argument("--all", action="store_true", help="also show every pair's long/short confidence")
    ap.add_argument("--no-state", action="store_true", help="ignore and don't update the state file")
    ap.add_argument("--workers", type=int, default=4, help="pairs scanned at once (default 4)")
    a = ap.parse_args(argv)

    styles = []
    for s in a.styles.split(","):
        s = s.strip().title()
        if s not in app.STYLES:
            ap.error(f"unknown style {s!r} (use Swing, Day, Scalp)")
        if s not in styles:
            styles.append(s)
    pairs = []
    for p in a.pairs.split(","):
        if not p.strip():
            continue
        try:
            b, q = app.parse_pair(p)
            if b + q not in pairs:
                pairs.append(b + q)
        except Exception:
            print(f"ignoring bad pair {p!r}", file=sys.stderr)
    if not pairs:
        ap.error("no valid pairs")
    if a.collateral <= 0:
        ap.error("--collateral must be positive")

    entries = app.load_log()
    state_path = Path(a.state)
    state = None if a.no_state else load_state(state_path)
    now = datetime.now(timezone.utc)

    res = app.scan_pairs(pairs, styles, a.collateral, entries, workers=a.workers, leverage=a.leverage,
                         progress=lambda d, n: print(f"  scanned {d}/{n}", file=sys.stderr) if not a.json else None)
    new = find_new_signals(res["results"], state, now)
    if state is not None:
        state["last_run"] = now.isoformat()
        try:
            save_state(state_path, state)
        except Exception as e:
            print(f"warning: could not save state: {e}", file=sys.stderr)

    ok = [r for r in res["results"] if r["ok"]]
    out = dict(
        run_time_utc=now.isoformat(), run_time_local=f"{now.astimezone(app.LOCAL_TZ):%d %b %Y %H:%M} {app.LOCAL_TZ_LABEL}",
        seconds=res["seconds"], styles=styles, pairs=pairs,
        leverage={s: (a.leverage if a.leverage else app.default_leverage(s)) for s in styles}, collateral=a.collateral,
        learning_log_entries=len(entries), dedupe=("off" if state is None else f"{REALERT_HOURS}h via {state_path}"),
        counts=dict(pairs_scanned=len({r["pair"] for r in ok}), evaluations=len(ok),
                    go_now=sum(r["status"] != "NO-GO" for r in ok), new_signals=len(new),
                    skipped=len(res["skipped"]), failed_checks=len(res["results"]) - len(ok)),
        new_signals=[signal_record(r) for r in new],
        go_now=[f"{r['pair']} {r['style']} {r['status']} {r['best_confidence']}%" for r in ok if r["status"] != "NO-GO"],
        skipped=res["skipped"], errors=res["errors"],
        top=[dict(pair=r["pair"], style=r["style"], side=r["best_side"], confidence=r["best_confidence"],
                  status=r["status"]) for r in sorted(ok, key=lambda r: -r["best_confidence"])[:10]],
        disclaimer="Not financial advice. Leveraged trading can lose your entire collateral.",
    )
    if a.all or a.json:
        out["all"] = [{k: v for k, v in r.items() if k not in ("problems",)} for r in res["results"]]
    if a.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(text_summary(out, a.all))
    return 0


if __name__ == "__main__":
    sys.exit(main())
