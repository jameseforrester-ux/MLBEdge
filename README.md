# Polymarket MLB Value Bot

A Telegram bot that scans **Polymarket** baseball markets — moneylines, run lines, and totals — and surfaces high‑confidence value plays with proper Kelly‑Criterion position sizing.

## What it does

- Pulls live MLB events from Polymarket's **Gamma API** and live mid‑prices from the **CLOB API**
- Computes a **fair probability** for each market using:
  - Team season win % blended with Pythagorean expectation (Bill James, exp 1.83)
  - Log5 head‑to‑head matchup math
  - Home‑field advantage (~4 pp)
  - Wind speed and temperature for totals (via Open‑Meteo, no key required)
  - Probable starting pitchers (via MLB Stats API, no key required)
- Detects **value** as `fair − market` in percentage points
- Recommends **position size** as `¼-Kelly × confidence-haircut`, hard‑capped at 5 % of bankroll
- Buckets plays into **tiers** S / A / B / C so the UI can show the best ones first
- Sends background **alerts** when prices on tracked games move ≥ 5 pp, or when a new tier‑S/A play appears

## Bot UI

A polished menu‑driven interface built with inline keyboards — every action is reachable either by tapping a button or typing a slash command.

### Slash commands

| Command | What it does |
|---|---|
| `/start` | Main menu |
| `/games` | All MLB games with current Polymarket prices |
| `/value` | Top value plays (sorted by tier) |
| `/tracked` | Games you're tracking |
| `/settings` | Edit thresholds and bankroll |
| `/track <slug>` | Track a specific game's price movement |
| `/untrack <slug>` | Stop tracking |
| `/alerts on\|off` | Toggle background alerts |
| `/bankroll <amount>` | Set bankroll for position sizing |
| `/edge <pp>` | Set min edge in percentage points (default 4.0) |
| `/conf <0-100>` | Set min confidence score (default 55) |
| `/help` | How the model works |

### What a play card looks like

```
🌟 Tier S  ·  📏 Run Line
⚾ Yankees @ Red Sox
Thu Apr 30 · 23:05 UTC

🎯 PICK: Yankees -1.5 @ 36.0%  (+178)

📊 Analysis
  Implied prob:    36.0%
  Fair prob:       50.8%
  Edge:            +14.82pp
  Expected Value:  +41.18%

💵 Position Sizing (¼-Kelly)
  Full Kelly:      23.16% of bankroll
  Recommended:     4.86% of bankroll
  At $2.5k bankroll: $122

🎲 Confidence: 84/100
  ██████████░░

📝 Rationale
  • Market mid 36.0%, fair est 50.8% → +14.8pp edge
  • Liquidity $8,000
  • Yankees 20-10 @ Red Sox 14-16
  • SP: Gerrit Cole vs Brayan Bello

→ Open on Polymarket
```

---

## Local setup

```bash
git clone https://github.com/YOUR_USERNAME/polymarket-mlb-bot.git
cd polymarket-mlb-bot

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env and set TELEGRAM_BOT_TOKEN (get one from @BotFather)

python main.py
```

That's it for local testing. Open Telegram, find your bot, send `/start`.

### Optional: restrict to specific users

Set `ALLOWED_USER_IDS` in `.env` to a comma‑separated list of Telegram user IDs to lock the bot down. Get your ID from `@userinfobot`.

---

## Deploy to a VPS via PuTTY

Tested on Ubuntu 22.04 / 24.04. Should work on any modern Debian/Ubuntu derivative.

### 1 — Connect to your VPS with PuTTY

1. Open **PuTTY**
2. **Host Name**: your server's IP (e.g. `203.0.113.42`)
3. **Port**: 22
4. **Connection type**: SSH
5. Click **Open**, log in as `root` (or your sudo user)

### 2 — Create a dedicated user and install dependencies

Paste these into the PuTTY terminal:

```bash
# As root (or with sudo):
adduser --disabled-password --gecos "" botuser
usermod -aG sudo botuser
# (the `sudo` group line is only needed if you want the update.sh helper to work)

apt update
apt install -y python3 python3-venv python3-pip git
```

### 3 — Push the bot to GitHub from your local machine

If you haven't already created the repo on GitHub:

```bash
# On your local machine, in the unzipped folder:
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/polymarket-mlb-bot.git
git push -u origin main
```

> **Public vs private repo:** Either is fine. `.env` is gitignored so your token stays out of the repo. If your repo is private, the `git clone` step below needs a Personal Access Token — see GitHub → *Settings → Developer settings → Personal access tokens*. Use the URL `https://USERNAME:TOKEN@github.com/USERNAME/polymarket-mlb-bot.git`.

### 4 — Clone, configure, and install on the VPS

Back in PuTTY:

```bash
# Switch to the bot user
sudo -iu botuser

# Clone — replace with your actual repo URL
git clone https://github.com/YOUR_USERNAME/polymarket-mlb-bot.git
cd polymarket-mlb-bot

# Create venv and install deps
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# Configure
cp .env.example .env
nano .env
```

In the editor, paste your Telegram token. Save with **Ctrl+O**, **Enter**, then exit with **Ctrl+X**.

Quick smoke test before installing the service:

```bash
.venv/bin/python main.py
```

If you see `Application started` and no errors, send `/start` to your bot from Telegram. If it replies, hit **Ctrl+C** to stop it and continue.

### 5 — Install as a systemd service (auto‑start on boot, auto‑restart on crash)

Exit the bot user shell and install the service file as root:

```bash
exit  # back to root/sudo user

sudo cp /home/botuser/polymarket-mlb-bot/deploy/polymarket-mlb-bot.service \
        /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable polymarket-mlb-bot
sudo systemctl start polymarket-mlb-bot
```

Verify it's running:

```bash
sudo systemctl status polymarket-mlb-bot
```

You should see `active (running)`. To follow live logs:

```bash
sudo journalctl -u polymarket-mlb-bot -f
```

### 6 — Updating the bot

Whenever you push new code to GitHub, just SSH in and run:

```bash
sudo -iu botuser
cd polymarket-mlb-bot
./deploy/update.sh
```

This pulls `main`, reinstalls any new dependencies, and restarts the service.

---

## Common operations

```bash
# Restart
sudo systemctl restart polymarket-mlb-bot

# Stop
sudo systemctl stop polymarket-mlb-bot

# Disable auto-start on boot
sudo systemctl disable polymarket-mlb-bot

# Recent logs (last 200 lines)
sudo journalctl -u polymarket-mlb-bot -n 200 --no-pager

# Live tail
sudo journalctl -u polymarket-mlb-bot -f

# Backup the SQLite DB (preferences, tracked games, alert dedup state)
cp /home/botuser/polymarket-mlb-bot/bot.db ~/bot.db.backup
```

---

## Project structure

```
polymarket-mlb-bot/
├── main.py                       # Entry point — wires everything up
├── config.py                     # Environment configuration
├── storage.py                    # SQLite persistence
├── polymarket/
│   ├── client.py                 # Async Gamma + CLOB client
│   └── parser.py                 # Raw events → Game/Market dataclasses
├── analysis/
│   ├── enrichment.py             # MLB Stats API + Open-Meteo weather
│   └── value.py                  # Fair-prob math, Kelly sizing, tiers
├── handlers/
│   ├── commands.py               # Slash commands + callback routing
│   ├── keyboards.py              # All inline keyboard layouts
│   ├── ui.py                     # HTML rendering (cards, summaries)
│   └── jobs.py                   # Background scan/alert jobs
├── deploy/
│   ├── polymarket-mlb-bot.service  # systemd unit
│   └── update.sh                   # Pull-and-restart helper
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

---

## How the model works (technical detail)

For every binary market on Polymarket we know the **market‑implied probability** `p_market` directly — it's the YES‑token midpoint, $0..$1.

We compute `p_fair` per market type:

**Moneyline** — Each team gets a "true strength" prob equal to a 50/50 blend of season win % and Pythagorean win % (with the Pythagorean weighted higher when sample is small). We then run **log5** to convert two strengths into a head‑to‑head matchup probability:

> P(A beats B) = (pₐ − pₐpᵦ) / (pₐ + pᵦ − 2pₐpᵦ)

Then we apply +4 pp for the home team.

**Run line** — Empirically, MLB favorites cover -1.5 about `p^1.6` of the time when their moneyline win prob is `p`. Symmetric on the dog side: P(+1.5 covers) = 1 − (1−p)^1.6.

**Totals** — We compute an expected total runs from each team's runs‑scored and runs‑allowed per game, average them, then nudge ±0.3 for high wind / cold temperature. A logistic curve maps the gap between expected total and the line to P(over).

**Edge** = (p_fair − p_market) × 100, in pp.

**Expected value** per dollar staked = (p_fair / p_market) − 1.

**Position sizing** uses the Kelly Criterion:

> f* = (b·p − q) / b   where b = (1−p_market) / p_market  and q = 1 − p_fair

We then apply two haircuts:
1. **¼-Kelly** — multiply by 0.25 to control variance (standard professional practice)
2. **Confidence haircut** — multiply by `confidence/100` so weakly‑justified plays get smaller stakes
3. Hard cap at **5 % of bankroll**, hard floor at **0.5 %** (below that we recommend skipping)

**Confidence (0..100)** sums:
- Liquidity score (0..35) — `7 × log10(liquidity + 1)`
- Spread score (0..20) — how close YES + NO is to 1.00
- Edge score (0..25) — sweet spot 4–12 pp; very large edges get penalized as suspicious
- Data score (0..20) — bumps for ≥20-game samples, weather data, named pitchers

**Tiers:**

| Tier | Edge | Confidence |
|---|---|---|
| 🌟 S | ≥ 7 pp | ≥ 75 |
| 💪 A | ≥ 5 pp | ≥ 65 |
| ✓ B | ≥ 3.5 pp | ≥ 55 |
| · C | anything passing your filters | |

---

## Limitations & caveats

- **No starting‑pitcher quality model.** We pull probable pitchers and show them in the rationale, but ERA/FIP isn't fed into the fair‑prob estimate. That's the highest‑leverage future improvement.
- **Polymarket liquidity is shallow** for many MLB markets. The bot enforces a `MIN_LIQUIDITY_USDC` floor (default $2k) to avoid recommending plays you can't actually fill.
- **Weather model is coarse.** Wind direction relative to ballpark orientation is ignored — it's just a wind‑magnitude bump. Wrigley‑specific logic would help.
- **Polymarket is a prediction market**, not a regulated sportsbook. Liquidity, slippage, and resolution rules differ. This tool is informational; it doesn't place trades.

---

## License

MIT.

- Airport data: [OurAirports](https://ourairports.com/data/) (public domain).
- Weather models served via [Open-Meteo](https://open-meteo.com/) (free for
  non-commercial use).
- METAR observations: [aviationweather.gov](https://aviationweather.gov)
  (NOAA, public domain).
