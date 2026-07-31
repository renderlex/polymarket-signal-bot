# Polymarket Signal Scalp Bot

A trading bot for Polymarket UP/DOWN markets using the trend-following
"Signal Scalp" strategy. It tracks the 1-hour price trend (moving averages +
RSI) and opens positions on the asset's rise/fall markets, protecting profits
with a trailing stop from the peak.

Control via Telegram, charts in a separate window, and continuous trading
(reacts to the market within seconds, not every few minutes).

---

## Features

- **Entries:** buys UP/DOWN tokens based on the 1h MA(12)/MA(48) gap and RSI.
- **Exits:** take-profit +30%, trailing −16% from the peak, locks 65% of peak
  profit on a pullback, sells hopeless positions near window end, trend reversal.
- **Demo by default:** runs on a virtual balance with no real-money risk.
- **Live mode:** real orders through the Polymarket CLOB (requires a wallet +
  API keys).
- **Telegram control:** status, balance, and entry/exit/settle notifications
  arrive in your chat.

## Requirements

- Python **3.10+**
- Windows / Linux / macOS
- Internet access (Binance + Polymarket APIs)

---

## 1. Installation

### 1.1 Download and unpack

Place the folder contents anywhere on disk where you can open a console.

### 1.2 Create a virtual environment and install dependencies

**Windows (PowerShell):**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

**Linux / macOS:**

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

> If PowerShell blocks scripts (`Activate.ps1`), run once:
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`

> The bundled `run_telegram.ps1` script does all of this automatically on the
> first run (and restarts the bot if it crashes).

---

## 2. Configuration

### 2.1 Create .env from the template

```powershell
Copy-Item .env.example .env
```

### 2.2 Telegram (required -- it is your control panel)

1. Open [@BotFather](https://t.me/BotFather), run `/newbot`, pick a name.
   You will receive a token like `1234567890:AAH...`.
2. Put it in `.env`:
   ```
   TELEGRAM_BOT_TOKEN=1234567890:AAH...
   ```
3. Send your bot any message (e.g. `/start`).
4. Find your `chat_id`: open in a browser
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
   and look for `"chat":{"id":123456789}` in the JSON -- that number is your
   `TELEGRAM_CHAT_ID`.
5. Put it in `.env`:
   ```
   TELEGRAM_CHAT_ID=123456789
   ```

### 2.3 (Live mode only) Polymarket

These fields can stay empty for demo mode. For real trading:

1. Set up a wallet on [Polymarket](https://polymarket.com) (Deposit Wallet).
2. Get the wallet private key and address (`PRIVATE_KEY`, `FUNDER_ADDRESS`).
3. Generate CLOB keys (API section on the site) and fill in
   `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE`.
4. Check `SIGNATURE_TYPE`:
   - `3` -- Deposit Wallet (EIP-1271), typical.
   - `1` -- Poly Proxy.
5. **Never share any of these values** -- keys give full access to funds.

### 2.4 Main parameters (quick reference)

| Parameter | Default | What it does |
|---|---|---|
| `STAKE` | `1` | Stake per position ($) |
| `WINDOW` | `14400` | Market window: `900` (15m), `14400` (4h), `86400` (1d) |
| `SIG_POLL` | `5` | Seconds between market checks (bot reaction speed) |
| `SIG_MAX_POS` | `6` | Max simultaneous positions |
| `SIG_TP` | `0.30` | Full take-profit (+30%) |
| `SIG_TRAIL_PCT` | `0.16` | Exit when price drops 16% from the peak |
| `SIG_LOCK_ARM_PCT` | `0.06` | Trail arms at +6% from entry |
| `SIG_LOCK_GIVE` | `0.65` | Locks 65% of peak profit on a pullback |
| `DRY_RUN` | `1` | `1` = orders are not sent (safe) |
| `LIVE_TRADING` | `0` | `0` = blocked. `1` = allow (with `DRY_RUN=0`) |

Every parameter is documented in the comments of `.env.example` itself.

---

## 3. Running

**Easy way (Windows):**

```powershell
.\run_telegram.ps1
```

The script sets up the virtual environment (if needed), installs dependencies,
and starts the bot. If the bot crashes, it restarts it automatically.

**Manually:**

```powershell
.\.venv\Scripts\pythonw.exe telegram_bot.py
```

or in a terminal:

```powershell
.\.venv\Scripts\python.exe telegram_bot.py
```

> The bot starts **automatically** right after launch, in **demo mode**.
> The chart window opens on its own -- nothing else to do.

---

## 4. Telegram control

Open your chat with the bot:

| Command / button | What it does |
|---|---|
| `Status` | Balance, open positions, WIN/LOSS, mode |
| `Balance` | Demo balance vs real USDC |
| `Mode` | Switch demo / live |
| `Start` / `Stop` | Start / stop the trading loop |
| `Help` | Command reference |

Notifications arrive automatically:
- `ENTRY ...` -- a position was opened;
- `EXIT ...` -- position sold via trail/take-profit;
- `SETTLE ...` -- position resolved by market outcome;
- `STOP by risk limits` -- drawdown protection triggered.

---

## 5. Real trading (LIVE)

Enabling live requires **two** simultaneous changes (protection against accidents):

```ini
DRY_RUN=0
LIVE_TRADING=1
```

1. Fill in `PRIVATE_KEY`, `FUNDER_ADDRESS` and CLOB keys in `.env`.
2. Deposit USDC on the wallet you plan to trade with.
3. Set `DRY_RUN=0` and `LIVE_TRADING=1`.
4. Restart the bot. It will show mode `LIVE` in Telegram.
5. After `/start`, press `Mode` to switch from DEMO to LIVE.

**Risk:** real-money trading. Run a week in demo first.

---

## 6. Files and their purpose

| File | Purpose |
|---|---|
| `telegram_bot.py` | Control panel: Telegram, lifecycle, state, autostart |
| `whale_agent.py` | Strategy engine: signals, entries/exits, trailing |
| `chart_gui.py` | Separate window with live Binance vs Polymarket charts |
| `.env` | Your settings and keys (never committed, never shared) |
| `chart_data.json` | Chart data (created while running) |
| `whale_agent.log` | Trading engine log |
| `telegram_bot.log` | Telegram control log |
| `telegram_state.json` | Bot state (balance, bets) -- restored after restart |
| `chart_history/` | Chart archives and trade logs per window |

---

## 7. Safety

- **Never publish `.env`** -- it contains wallet private keys.
- Demo mode (`DRY_RUN=1`) is a safe simulation; test strategies on it.
- `MAX_DRAWDOWN_PCT` / `MAX_DAILY_LOSS_PCT` stop the bot on large drawdowns.
- Live orders are capped by `MAX_REAL_STAKE` and `MAX_WINDOW_EXPOSURE`.

## 8. Troubleshooting

| Symptom | Solution |
|---|---|
| "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env" | Fill in `.env` (section 2.2) |
| "Another instance is already running (port 50517 busy)" | Bot is already running; close the old process |
| No Telegram messages | Check token and `chat_id`; message your bot /start |
| Bot "does not react" to signals | Check `whale_agent.log`: `SKIP-ENTRY` lines explain why an entry was skipped |
| Python error on startup | Run `pip install -r requirements.txt` |

---

*This project is not financial advice. Crypto derivatives carry high risk.*


I am committed to making this entire project accessible, meaning I will be sharing all my ongoing results, future updates, and the full program itself completely for free. Institutional trading firms charge thousands of dollars a month for access to algorithms with a fraction of this transparency, but I firmly believe in leveling the playing field for independent builders and researchers. Since I am dedicating countless hours to refining this code and covering the server and data costs entirely out of pocket, I have set up a voluntary donation jar to support the project's continued development. If this research sparks a new idea for your own strategy, or if you simply want to see this bot reach its final, highly profitable form, consider sending over a small contribution. Just think of it as buying me a coffee or donating the equivalent of just one single winning trade from the demo logs above, which makes it practically effortless to support the work. I will leave my Binance deposit details below for anyone who wants to fuel the next major update.
<img width="1008" height="1391" alt="image" src="https://github.com/user-attachments/assets/a44e8614-4a6b-4bbc-9e3b-1f511c2a76c4" />

0x093D19B90337aFa2F6826b051A991a7b8Cb983D2

 


