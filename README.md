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

**The `.streamlit` folder (optional):** it only sets the dark colour theme. Folders whose names start with a dot are hidden on many computers and phones, so they can be hard to upload. **You can skip it, the app works fine without it.** If you'd like it: tap **Add file → Create new file**, type the name `.streamlit/config.toml` (typing the `/` makes the folder), paste in the contents of `config.toml` from the folder I gave you, then tap **Commit changes**.

Don't upload `screenshot_mobile.png`. It's only a preview picture.

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
- **The learning log gets wiped.** On the free plan, saved results are deleted whenever the app restarts, sleeps or is updated. After logging results, tap **Download backup (CSV)** in the **Learning** tab, and use **Restore from CSV** to bring them back later.
- **Updating the app:** on GitHub, open the file (e.g. `app.py`), tap the ✏️ pencil to edit (or upload a new version), then **Commit changes**. The app updates itself within a minute or two.
- **Logs and settings:** when you're signed in, the app page has a **Manage app** button at the bottom right. That's where you can see logs, reboot the app or change settings.

### Troubleshooting

| Problem | What to do |
|---|---|
| "Could not load enough data" / "data source blocked" | Streamlit's servers are in the USA, and some exchanges (Binance's main site, Bybit) block US traffic. The app tries other sources automatically (Binance's public data mirror, OKX, KuCoin, Coinbase, Kraken) and shows which one it used. If they all fail, wait a minute and tap **Refresh data**. |
| "Could not load ... for XYZUSDT" | Check the pair name. Use the coin + USDT, e.g. `SOLUSDT`, `PEPEUSDT`. Very new or tiny coins may not be on the free data sources. |
| `ModuleNotFoundError` on the deploy screen | Make sure `requirements.txt` is uploaded, sits next to `app.py` (not in a sub-folder) and is spelt exactly like that. Then use **Manage app → Reboot app**. |
| App shows an error after you edited it | Undo your change on GitHub (open the file → History) or upload the original `app.py` again. |
| Backtest is slow the first time | It downloads up to a year of prices. Results are remembered for 30 minutes, so later runs are quick. |
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

**Settings (top of the page).** Tap to open. Choose the **pair**, your **collateral** (the USDT you put in), the **leverage** (1-25x) and the **trade style**. Then tap **Analyse**.

| Style | Charts it looks at | Typical hold | Suggested leverage |
|---|---|---|---|
| Swing | 4-hour, daily, weekly | days | 2-5x |
| Day | 15-min, 1-hour, 4-hour | hours | 3-8x |
| Scalp | 5-min, 15-min, 1-hour | minutes to an hour | 5-15x |

If your leverage is outside the suggested range, you'll see a warning.

**Show side (Auto / Long / Short).** A switch just under the settings.
- **Auto (best side)**: the big banner shows whichever side, long or short, has the higher confidence. If both are NO-GO, it simply says NO-GO.
- **Long** or **Short**: the big banner, the Position tab, the stop/target lines on the chart and the "Log a result" direction all follow the side you picked.

**🎯 Decision.** The big banner, then two smaller cards, **LONG** and **SHORT**, side by side. Each side is checked separately and gets its own verdict and confidence. The side the banner is showing has a yellow outline and a ★.
- **GO LONG** means the signals point up: a bet the price rises.
- **GO SHORT** means the signals point down: a bet the price falls.
- **NO-GO** means don't trade that side now. The reasons are listed on its card.
- **Confidence %** shows how strongly everything agrees for that side. Under 55% is always NO-GO, and a NO-GO never shows more than 49%. Even 80% is **not** a guarantee.

Usually one side looks reasonable and the other looks poor: if the charts lean up, the long side scores higher and the short side scores lower. Often both are NO-GO, which simply means "no good trade right now".

A side can only be GO if all of these are true:
1. the overall score clearly points its way
2. the **Supertrend** on the main chart agrees (the key check)
3. the biggest chart doesn't strongly disagree
4. past trades **in that direction** made money after costs in the backtest
5. you wouldn't be liquidated before your stop
6. the reward is bigger than the risk after costs

**📊 Chart.** Candles with the **Supertrend** (green = up-trend, red = down-trend) and the EMA lines. Volume is in the middle and **RSI** at the bottom (above 70 = stretched up, below 30 = stretched down). Pinch to zoom. Times are Sri Lanka time.

**💰 Position.** Shows the side chosen with the **Show side** switch (in Auto, the stronger side). A long's stop sits below the price and a short's above it, so the numbers change with the side.
- Your position size (collateral × leverage) and the number of coins.
- The suggested stop-loss and take-profit.
- The estimated **liquidation price**: the price at which the exchange closes your trade and you lose the collateral.
- If liquidation would come **before** your stop, you'll get a red warning and the highest **safe leverage**.
- Costs: 0.04% fee in and out, slippage, and funding for the typical hold time.

**🔍 Analysis.** Every indicator on every timeframe, explained in one line each, with the points it added or took away.

**🧪 Backtest.** How the same rules would have done in the past, at your leverage and with all costs included. Long trades and short trades are also shown separately, because each side's decision uses only its own past trades:
- number of trades and win rate
- average win and loss
- **expectancy**: the average result per trade (above 0 is good)
- profit factor
- biggest drawdown
- an account-value chart

Scalp tests only the last few weeks of 5-minute candles to stay quick. The exact dates tested are shown.

**🧠 Learning.** Log **Win** or **Loss** after each trade, with its direction. The app shrinks confidence a little for styles, pairs and directions (long or short) that keep losing for you, and raises it slightly (up to +15%) for ones that win. Remember to **download the CSV backup**.

**⚠️ Warnings.** The important risks, in one place.

---

## Files in this folder

| File | What it is |
|---|---|
| `app.py` | The whole app |
| `requirements.txt` | The list of parts Streamlit installs |
| `.streamlit/config.toml` | Dark theme (optional) |
| `README.md` | This guide |
| `screenshot_mobile.png` | What it looks like on a phone (don't upload) |

`learning_log.json` is created automatically when you log your first result.

*If you ever need a new GitHub account: sign-up and verification emails sent to Hotmail/Outlook.com addresses sometimes arrive late or land in the Junk folder. Check Junk, or sign up with a free Gmail address instead.*
