# 📈 Leveraged Crypto Trade Screener

## What this is

A small web app that looks at a crypto pair (for example **BTCUSDT**) and tells you, in plain words, whether a leveraged trade looks sensible right now:

- a big **green GO LONG / GO SHORT** or a **red NO-GO**, with a **confidence %**
- a live, zoomable price chart with the **Supertrend** line and moving averages (EMAs)
- your **position size**, **stop-loss**, **take-profit**, estimated **liquidation price** and **all the costs** (fees, slippage, funding)
- an automatic **backtest**: it checks how the same rules would have done over roughly the last 12 months
- a simple **learning log**: tell it whether your trades won or lost and it gently adjusts future confidence

It's built to be used on your **phone**.

## ⚠️ Disclaimer

**Not financial advice. Leveraged trading can lose your entire collateral.**
This app is an educational tool. It can be wrong, data can be delayed, and past results do not predict the future. Only trade money you can afford to lose.

---

## Option A: Put it on your phone for free (Streamlit Community Cloud)

You'll end up with your own web link (like `https://my-crypto-screener.streamlit.app`) that opens on any phone. It takes about 15 minutes. These steps are easiest on a computer, but they also work on a phone browser.

### Step 1: Sign in to GitHub

Go to **github.com** and sign in with your existing account. GitHub is where your app's files will live.

### Step 2: Create a new public repository (a folder for the app)

1. At the top right, tap the **+** button, then **New repository**.
2. **Repository name:** type `crypto-screener`.
3. Choose **Public**. (The free Streamlit plan needs a public repository.)
4. You **don't** need to tick "Add a README file".
5. Tap the green **Create repository** button.

### Step 3: Upload the app files

1. On your new repository's page, tap the link **uploading an existing file**, or use **Add file → Upload files**.
2. Drag in (or choose) these files from the folder I gave you:
   - `app.py`
   - `requirements.txt`
   - `README.md` (optional)
3. Scroll down and tap the green **Commit changes** button.

**The `.streamlit` folder (optional):** it only sets the dark, iOS-style colour theme. Folders whose names start with a dot are hidden on many computers and phones, so they can be hard to upload. **You can skip it, the app works fine without it.** If you'd like it: tap **Add file → Create new file**, type the name `.streamlit/config.toml` (typing the `/` makes the folder), paste in the contents of `config.toml` from the folder I gave you, then tap **Commit changes**.

Don't upload the `screenshot_*.png` files. They're only preview pictures.

### Step 4: Deploy on Streamlit Community Cloud

1. Go to **share.streamlit.io**.
2. Tap **Continue to sign-in** → **Continue with GitHub**, and allow Streamlit to access your GitHub (tap **Authorize streamlit** if asked). If it's your first time, fill in the short form and tap **I accept**.
3. At the top right, tap **Create app**.
4. When asked "Do you already have an app?", tap **Yup, I have an app**.
5. Fill in:
   - **Repository:** `your-github-name/crypto-screener`
   - **Branch:** `main`
   - **Main file path:** `app.py`
   - **App URL (optional):** choose a name you'll remember, e.g. `my-crypto-screener`
6. (Optional) Under **Advanced settings** you can pick a Python version. The default (3.12) is fine.
7. Tap **Deploy**.

### Step 5: Open it on your phone and add it to your home screen

1. Wait a few minutes while it installs. You'll see a "baking" screen the first time.
2. Open your link on your phone.
3. Add it to your home screen so it works like an app:
   - **iPhone (Safari):** tap the **Share** button (square with an arrow) → **Add to Home Screen** → **Add**.
   - **Android (Chrome):** tap the **⋮** menu → **Add to Home screen** (or **Install app**) → **Add**.

### Step 6: Good to know

- **It goes to sleep.** If nobody uses it for a while, the free app sleeps. Just tap the button saying something like **"Yes, get this app back up!"** and wait about a minute.
- **The learning log gets wiped.** On the free plan, saved results are deleted whenever the app restarts, sleeps or is updated. After logging results, tap **Download backup (CSV)** in the **Journal** tab, and use **Restore from backup** to bring them back later.
- **Updating the app:** on GitHub, open the file (e.g. `app.py`), tap the ✏️ pencil to edit (or upload a new version), then **Commit changes**. The app updates itself within a minute or two.
- **Logs and settings:** when you're signed in, the app page has a **Manage app** button at the bottom right. That's where you can see logs, reboot the app or change settings.

### Troubleshooting

| Problem | What to do |
|---|---|
| "Could not load enough data" / "data source blocked" | Streamlit's servers are in the USA, and some exchanges (Binance's main site, Bybit) block US traffic. The app tries other sources automatically (Binance's public data mirror, OKX, KuCoin, Coinbase, Kraken) and shows which one it used. If they all fail, wait a minute and tap **Refresh prices**. |
| "Could not load ... for XYZUSDT" | Check the pair name. Use the coin + USDT, e.g. `SOLUSDT`, `PEPEUSDT`. Very new or tiny coins may not be on the free data sources. |
| `ModuleNotFoundError` on the deploy screen | Make sure `requirements.txt` is uploaded, sits next to `app.py` (not in a sub-folder) and is spelt exactly like that. Then use **Manage app → Reboot app**. |
| App shows an error after you edited it | Undo your change on GitHub (open the file → History) or upload the original `app.py` again. |
| First load is slow | It downloads up to a year of prices (a few seconds). The history is kept for an hour and only the newest candles are added after that, so later taps are quick. If the app was asleep, Streamlit needs 30-60 seconds to wake it up first. |
| Can't find the app link again | Go to share.streamlit.io → **My apps**. |

---

## Option B: Run it on your own computer (backup)

1. Install **Python 3.11 or newer** from **python.org/downloads**.
   - **Windows:** on the first installer screen, **tick "Add Python to PATH"**, then click **Install Now**.
   - **Mac:** run the downloaded installer.
2. Put the `crypto-screener` folder somewhere easy, such as your Desktop.
3. Open a terminal in that folder:
   - **Windows:** open the folder, click the address bar, type `cmd` and press Enter.
   - **Mac:** open **Terminal**, type `cd ` (with a space), drag the folder into the window and press Enter.
4. Install the parts it needs (only once):
   ```
   pip install -r requirements.txt
   ```
   (On a Mac, if `pip` isn't found, use `pip3` and `python3`.)
5. Start the app:
   ```
   streamlit run app.py
   ```
   Your browser opens **http://localhost:8501**.
6. **To use it on your phone at home:** make sure the phone is on the same Wi-Fi, then type the **Network URL** shown in the terminal (like `http://192.168.1.20:8501`) into your phone's browser.
7. To stop it, press **Ctrl + C** in the terminal.

---

## What each part of the app means

The app is laid out like a phone app. From top to bottom:

**Settings card.** The bar showing something like `BTCUSDT · Day · 100 USDT · 3x`. It's open on your first visit and folds away after you tap **Analyse**. Tap it to change:
- the **pair**
- the **trade style** (Swing / Day / Scalp)
- your **collateral** (the USDT you put in)
- the **leverage** (1-25x)
- **Show side** (Auto / Long / Short), which switches instantly with no need to tap Analyse

| Style | Charts it looks at | Typical hold | Suggested leverage |
|---|---|---|---|
| Swing | 4-hour, daily, weekly | days | 2-5x |
| Day | 15-min, 1-hour, 4-hour | hours | 3-8x |
| Scalp | 5-min, 15-min, 1-hour | minutes to an hour | 5-15x |

If your leverage is outside the suggested range, an orange note appears under the settings card.

**Show side.**
- **Auto (best side)**: the big card shows whichever side, long or short, has the higher confidence. If both are NO-GO, it simply says NO-GO.
- **Long** or **Short**: the big card, the four key numbers, the Details tab, the stop/target lines on the chart and the Journal's direction all follow the side you picked.

**The big decision card.** The verdict (**GO LONG**, **GO SHORT** or **NO-GO**), a ring showing the **confidence %**, and one line saying the main reason.
- **GO LONG** means the signals point up: a bet the price rises.
- **GO SHORT** means the signals point down: a bet the price falls.
- **NO-GO** means don't trade that side now.
- **Confidence %** shows how strongly everything agrees for that side. Under 55% is always NO-GO, and a NO-GO never shows more than 49%. Even 80% is **not** a guarantee.

**Long and Short cards.** Two small cards side by side, one per direction, each with its own verdict and confidence bar. They always show both sides. The one with the blue outline is the side the big card is showing. Usually one side looks reasonable and the other poor: if the charts lean up, the long side scores higher and the short side lower. Often both are NO-GO, which simply means "no good trade right now".

Under the cards you'll see two more reasons. Tap **Show all reasons** for the full list for both sides. Green dots are checks that passed, red dots are what blocks a GO, orange dots are cautions, and grey dots are just information.

A side can only be GO if all of these are true:
1. the overall score clearly points its way
2. the **Supertrend** on the main chart agrees (the key check)
3. the biggest chart doesn't strongly disagree
4. past trades **in that direction** made money after costs in the backtest
5. you wouldn't be liquidated before your stop
6. the reward is bigger than the risk after costs

**Four key numbers.** **Entry** (price now), **Stop-loss**, **Target** and **Liquidation** for the side being shown, each with its distance from the price. A long's stop sits below the price and a short's above it. If you'd be liquidated before your stop, a red warning tells you the highest safe leverage. **Refresh prices** fetches the newest data (prices are otherwise remembered for 60 seconds).

Below that are four tabs:

- **Chart**: candles with the **Supertrend** line (green = up-trend, red = down-trend) and the EMA lines, plus dashed lines for entry, stop and target. Volume is in the middle and **RSI** at the bottom (above 70 = stretched up, below 30 = stretched down). Pinch to zoom. Times are UAE time (UTC+4).
- **Details**:
  - your position size, the max safe leverage, what you'd lose at the stop and gain at the target
  - distances to the stop and to liquidation
  - a clear list of costs (0.04% fee in and out, slippage, and funding for the typical hold time)
  - **Analysis**: every indicator on every timeframe, explained in one line each
  - **Risks & disclaimer**: the important warnings in one place
- **Backtest**: how the same rules would have done in the past, at your leverage and with all costs included:
  - trades and win rate
  - **expectancy**: the average result per trade (above 0 is good)
  - profit factor and biggest drawdown
  - an account-value chart
  - long trades vs short trades shown separately, because each side's decision uses only its own past trades

  Scalp tests only the last few weeks of 5-minute candles to stay quick. The exact dates tested are shown.
- **Journal**: log **Win** or **Loss** after each trade, with its direction. The app shrinks confidence a little for styles, pairs and directions that keep losing for you, and raises it slightly (up to +15%) for ones that win. Tap **Download backup (CSV)** afterwards, because free Streamlit Cloud wipes the journal on restarts. Use **Restore from backup** to bring it back.

---

## Scanner: checking many pairs at once

**In the app:** open the **Scan** tab and tap **Scan pairs**. It checks the 12 most-traded pairs for your chosen style (tick **Scan all 28 pairs** for the full list, which is slower). It uses the style's suggested leverage (Swing 3x, Day 5x) and exactly the same long and short checks as the main screen. You get a sorted list showing each pair's best side and confidence, with any **GO** highlighted in green. Results are kept for 10 minutes.

**Phone alerts:** these come from **Grok Bot**, not from the Streamlit app. Grok Bot runs the scanner on its own computer every hour and messages you when a pair gets a **new** confirmed GO LONG or GO SHORT for the Day or Swing style. "New" means it hasn't alerted you about the same pair, style and side in the last 12 hours, unless the signal went away and came back. Each alert includes:
- entry, stop, target and liquidation (at the suggested leverage)
- reward-to-risk after costs
- the top reasons
- how that side did in the backtest

Pairs scanned by default: BTC, ETH, SOL, BNB, XRP, DOGE, ADA, AVAX, LINK, TON, DOT, LTC, BCH, NEAR, SUI, APT, ARB, OP, INJ, TRX, PENGU, ZEC, HYPE, PUMP, PEPE, WIF, ENA and TAO (all against USDT). If a coin isn't on Binance, or Binance only has a short history for it, the app automatically uses Bybit, OKX, KuCoin, Coinbase or Kraken instead. Newer coins with less than 12 months of history get the usual "low reliability" handling.

For the technically curious, the scanner is the `scanner.py` file:
```
python scanner.py --styles Day,Swing --all --no-state
```
Useful options:
- `--pairs BTCUSDT,ETHUSDT`
- `--collateral 100`
- `--json`
- `--state FILE` (sets the file used to remember what has already been alerted)

**Scalps on a slower chart:** `--styles Scalp --scalp-tf 15m,30m,1h` scans three extra scalp styles, `Scalp-15m` (trend checked on 1h/4h), `Scalp-30m` (1h/4h) and `Scalp-1h` (4h/daily). They use the same Scalp rules as the 5-minute scalp: a stop at 2 x ATR but never closer than 0.35%, a 2R target, and a stop that must be bigger than the round-trip costs. Each one is backtested on its own chart: about 3 months for 15m, 6 months for 30m and 12 months for 1h. Repeat alerts are held back for 2h, 3h and 6h respectively, instead of 12h. In the app, pick **Scalp** and then the **Scalp chart** (5m, 15m, 30m or 1h).

As always: **not financial advice.** A GO is a set of conditions lining up, not a promise.

---

## Practice trading bot (paper money)

`paper_trader.py` trades every new GO signal with **pretend money**. It uses no exchange account and no keys. The aim is to see whether the signals actually make money before any real funds are involved.

- **Opening trades:** each new GO the scanner alerts on becomes a paper trade of **$5,000 at 5x** (1,000 USDT collateral). It enters at the signal's price and uses the signal's stop and target. Only one trade per pair and style can be open at a time.
- **Checking trades:** every scanner run replays the 5-minute candles (15m or 1h if 5m isn't available) since the last check, using the same free data sources as the app. A trade closes at the **stop**, the **target** or **liquidation**, whichever is reached first. If the stop and target are both inside one candle, the stop is assumed. Because the candles are replayed, nights when the scanner doesn't run are handled exactly.
- **Maximum hold:** the same as the backtest, so the results can be compared. Swing 7 days, Day 24h, Scalp (5m) 3h, Scalp-15m 4h, Scalp-30m 8h, Scalp-1h 16h. After that the trade closes at the market price.
- **Costs:** a 0.04% fee on entry and on exit, the style's slippage each way, and funding for every 8h held (the live rate at entry, or 0.01% per 8h). Funding is always counted as paid. A liquidation loses the whole 1,000 USDT.
- **Learning:** every closed trade keeps a snapshot of the setup at entry: confidence, ADX, RSI, Supertrend distance, EMA alignment, how many charts agreed, backtest edge, volatility, hour (UAE) and funding. Losses also get "why" tags, such as a quick stop-out, a weak trend or trading against the bigger chart. Results are grouped (by pair + style + side, by style + side, and by style + side + setup type).
  - A group with **5 or more** results and a negative average needs more confidence before the next trade: +5, +10 or at most +15 points.
  - A group that is clearly losing after **8 or more** trades (-0.5R or worse on average, 30% wins or fewer) is **blocked for 14 days**. One in every 4 of its signals is still traded as a test.
  - Groups that recover are relaxed again step by step.
  - Every change is written to `learning_changes.txt`. The strategy itself is never changed automatically.
  - Signals the learning skips are not sent as alerts. They are listed under `paper.learning.skipped`.

```
python paper_trader.py --summary                     # plain summary + lessons learned
python paper_trader.py --run --scan last_run.json    # what the scanner job runs after each scan
```
### Account protection and letting winners run

Switched on per ledger with `python paper_trader.py --enable-protection [--daily-goal 500]` (stored in `trades.json` under `rules`). All limits use **realised** P&L after costs (closed trades plus half-positions already booked), with days and weeks in UAE time (the week starts Monday 00:00 UAE). Limits are a % of the account, so they grow with it, and once hit they hold for the rest of the day or week.

- **Daily loss limit:** once today is down 3% of the account value at 00:00 UAE, no new trades until the next UAE day.
- **Profit lock:** once today reaches the daily goal (+$500), falling back to half of the day's peak stops new trades for the day.
- **Weekly limit:** once the week is down 8% of the value at Monday 00:00 UAE, no new trades until next Monday.
- **Open trade cap:** at most 6 open trades.
- Open trades are never force-closed by these rules. Skipped signals appear under `paper.not_opened` with the reason; status is in `paper.protection` (`just_triggered` lists a limit the first time it is hit).
- **Let winners run:** at +1R the stop moves to break-even plus costs. At the original target half is closed and booked, and the rest is trailed with an ATR(14) x Supertrend-multiplier stop on the trade's own timeframe (only ever tightens; may run up to 2x the normal max hold). A trade is still one result: its P&L and R include the booked half, R on the full position.

### Trading sessions and the high-confidence exception

Switched on per ledger with `python paper_trader.py --enable-sessions [--override-min-confidence 75]` (stored under `rules.sessions` and `rules.high_conf_override`). These only decide whether a practice trade opens; signals and alerts are computed as before. Times are UAE; the NY open is 09:30 New York time converted each day, so US daylight saving is automatic.

- **Prime** (Mon-Fri, NY open -75 min to +3h: 16:15-20:30 UAE in US summer time, 17:15-21:30 in winter): 55% on every style. A 15m/30m signal from the candle that opens at the NY open is skipped; the next bar can enter.
- **Normal** (other weekday hours, 08:00-21:00): 55%, but 15m scalps need 65% during 08:00-11:00 and 13:00 until prime.
- **Quiet** (weekends, weekday 21:00 or prime end until 08:00): 70% and the style's highest chart Supertrend agrees (`higher_tf_agrees`: 4h for Day/15m/30m scalps, 1d for 1h scalps, 1w for Swing). At most 2 open trades opened in quiet hours.
- Outside prime, new trades only while fewer than 4 are open (2 of the 6 slots kept for prime).
- Every trade is tagged `session` (prime, normal or quiet).
- **High-confidence exception** (75%+): may open despite the profit lock, the 6-trade cap or the prime slots at full size; despite the daily loss limit once per UAE day at half size ($500 collateral); never despite the weekly limit or the quiet-hours cap. Session rules, the NY-open-candle skip and free cash still apply. Tagged `override.kind = "high_confidence"`.

### Entry filters and cleanup (23 Oct 2026)

Switched on with `python paper_trader.py --enable-filters` (stored under `rules.entry_filters`; it overrides the session values above at run time without changing them):

- **Bigger chart must agree on every new trade.** This applies to every style and session, and the 75%+ exception can't bypass it. A blocked trade shows the reason `bigger chart disagrees`.
- **55% minimum in prime and normal hours, 70% in quiet hours.** The 65% rule for 15m scalps at set hours and the NY-open-candle skip are frozen. Their code is kept but not applied.
- **75%+ exception paused** (code kept) until the confidence score is recalibrated at about 60 trades. Every limit, cap and reserved slot applies to every signal. When it returns, it will have no half-size trade during a daily-loss pause, so the daily loss limit stays absolute.
- **Learning filter frozen** until 100 closed trades. It keeps recording but never blocks or adds penalties.
- **No new real Scalp-1h trades** (Day covers 1h) **or Swing trades** (shadow mode). Open ones keep running.
- **Shadow (log only):** "would be half size: Swing probation" and "would skip: stretched RSI" tags. Every signal that does not open becomes a hypothetical trade in `shadow_trades.json` (plain stop / target / time limit). It is also logged to `skipped_signals.jsonl` with relative volume, funding, OKX open interest, BTC 1h/4h trend, RSI and session.
- **Tracking:** a close with |P&L| under $1 counts as a scratch, not a win or a loss, and win rate = wins / (wins + losses). New trades also store BTC trend, session and max favourable/adverse excursion.
- **Confidence score (app):** Supertrend ±60, EMA 21 vs 50 ±20 and RSI ±20. Confidence = 40 + 0.35 × the aligned score (was 0.55), so a fully aligned setup is about 75%, 55% needs a score of about +44 and 70% about +86. MACD, ADX, the other EMA checks and volume are shown but score nothing. There is no confidence cut for a thin backtest or for a disagreeing bigger chart. A backtest that lost money still blocks.

The ledger lives in `/workspace/scanner/paper/` on Grok Bot's computer (`trades.json`, `learning.json`, with backups before every write).

---

## Files in this folder

| File | What it is |
|---|---|
| `app.py` | The whole app |
| `scanner.py` | The multi-pair scanner (used for Grok Bot's alerts; optional on Streamlit Cloud) |
| `paper_trader.py` | The practice (paper money) trading bot that runs after each scan |
| `requirements.txt` | The list of parts Streamlit installs |
| `.streamlit/config.toml` | Dark theme (optional) |
| `README.md` | This guide |
| `screenshot_*.png` | What it looks like on a phone (don't upload) |

`learning_log.json` is created automatically when you log your first result.

*If you ever need a new GitHub account: sign-up and verification emails sent to Hotmail/Outlook.com addresses sometimes arrive late or land in the Junk folder. Check Junk, or sign up with a free Gmail address instead.*
