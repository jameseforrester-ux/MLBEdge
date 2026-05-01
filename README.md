# 🌤️ Weather Prediction Telegram Bot

A high-accuracy temperature forecast bot that combines an **ensemble of 8 of
the world's best numerical weather prediction models** — ECMWF IFS, ECMWF AIFS
(AI), UK Met Office, DWD ICON, NOAA GFS, JMA, Météo-France, and Environment
Canada GEM — with an inter-model uncertainty estimate, calibrated probabilities
per integer temperature, METAR-station current conditions, and tracking alerts.

---

## ✨ Features

- 🔍 **City search** → returns nearby airports as tappable buttons
- ✈️ **Airport forecast** by ICAO (e.g. `KJFK`) or IATA (e.g. `LAX`)
- 🧠 **8-model weighted ensemble** with ECMWF IFS getting the highest weight
- 🎯 **Confidence score** on every prediction (high-confidence flagged 🟢)
- 📊 **Probability per integer temperature** (e.g. `72°F: 45% · 71°F: 25%`)
- 📡 **Live METAR observations** from the airport's actual weather station
- 🎲 **Polymarket integration** — for supported cities, shows the top 3 daily
  high-temperature buckets by YES probability with a ✅ next to the one our
  model agrees with, plus a hedge band around our prediction with [Trade]
  deep-links into Polymarket. Auto-detects °F vs °C per market.
- 🔔 **Tracking** — alerts you if the predicted max changes by ≥2°F or ≥1°C;
  alert includes Polymarket data when the model's bucket shifts.
- 🌡️ Temperatures shown in both **°F and °C, always whole numbers**
- 📱 **Bottom-left commands menu** + persistent reply keyboard for fast access

---

## 🧪 Methodology (how the prediction works)

1. **Multi-model fetch.** For each query, we fetch daily max-temperature
   forecasts from **8 NWP models** in parallel via Open-Meteo's free API.
2. **Weighted ensemble.** Each model's prediction is combined using weights
   based on long-term skill rankings:
   - ECMWF IFS — 0.22 (global accuracy leader)
   - ECMWF AIFS — 0.18 (ECMWF's AI-based model, peer to Google's WeatherNext)
   - DWD ICON — 0.15
   - UK Met Office — 0.13
   - NOAA GFS — 0.10
   - Météo-France — 0.09
   - JMA — 0.08
   - Environment Canada GEM — 0.05
3. **Spread → uncertainty.** We compute the standard deviation σ across
   models. When the world's best models agree (small σ), the prediction is
   highly reliable. Empirical rule: σ ≤ 1°C → outcome usually within ±2°.
4. **Confidence score.** `confidence = clamp(1 − σ/3, 0, 1)`. Mapped to
   🟢 HIGH (≥ 75%), 🟡 MEDIUM (50–75%), 🔴 LOW (< 50%).
5. **Per-temperature probability.** We treat the ensemble mean as μ and the
   spread as σ in a Gaussian, then integrate over `[T−0.5, T+0.5]` for each
   integer T near μ. That gives `P(round(temp) == T)`.
6. **METAR.** For "current conditions" we hit `aviationweather.gov` for the
   airport's actual observation; if unavailable, we fall back to Open-Meteo's
   nearest grid cell.
7. **Whole numbers.** `°C` and `°F` are each independently rounded to the
   nearest integer (so 22°C ↔ 72°F is normal — they're rounded separately).

---

## 📦 What's in this package

```
weather-bot/
├── bot.py                  # Main bot — all handlers & UI
├── weather.py              # Ensemble + METAR forecasting
├── airports.py             # Global airport DB & city geocoding
├── tracking.py             # SQLite tracking store
├── config.py               # Env-driven config
├── requirements.txt        # Python deps
├── .env.example            # Copy to .env and edit
├── .gitignore              # Keeps secrets out of git
├── install.sh              # One-command VPS installer
├── weather-bot.service     # systemd unit template
├── data/                   # airports.csv lands here on first run
└── README.md               # This file
```

---

## 🚀 Deploy to a VPS

### Step 1 — Create the GitHub repo (one-time, on your laptop)

```bash
# Unzip the package
unzip weather-bot.zip
cd weather-bot

# Initialize git and push to GitHub
git init -b main
git add .
git commit -m "Initial commit"

# Create an empty repo on github.com first (call it weather-bot), then:
git remote add origin https://github.com/<your-username>/weather-bot.git
git push -u origin main
```

> ⚠️ **Important.** `.gitignore` deliberately excludes `.env` so your bot
> token does **not** end up on GitHub. Don't override that.

---

### Step 2 — On the VPS (over PuTTY/SSH)

```bash
# Pick a directory you own. Home is fine.
cd ~

# Pull the code from your GitHub repo
git clone https://github.com/<your-username>/weather-bot.git
cd weather-bot
```

If your repo is **private**, generate a Personal Access Token on GitHub
(Settings → Developer settings → Tokens) and clone with:

```bash
git clone https://<token>@github.com/<your-username>/weather-bot.git
```

---

### Step 3 — Install (one command)

```bash
bash install.sh
```

The installer will:
1. Install system packages (`python3`, `python3-venv`, `python3-pip`)
2. Create a virtualenv in `./venv`
3. Install all Python dependencies
4. Create `.env` from `.env.example` if it doesn't exist
5. Generate a systemd unit substituting your user and install path
6. Enable + start the service

The bot will be **running and will auto-start on every reboot**.

---

### Step 4 — Verify

```bash
sudo systemctl status weather-bot
```

You should see `active (running)`. Watch live logs with:

```bash
sudo journalctl -u weather-bot -f
# or
tail -f bot.log
```

Open Telegram, find your bot, hit `/start`, and try:
- `/search Toronto`
- `/forecast KJFK`
- Tap the **menu button** in the bottom-left

---

## 🔁 Updating the bot

Anytime you push new commits to GitHub:

```bash
cd ~/weather-bot
git pull
sudo systemctl restart weather-bot
```

Done.

---

## 🛠️ Useful commands

```bash
# Check status
sudo systemctl status weather-bot

# Start / stop / restart
sudo systemctl start  weather-bot
sudo systemctl stop   weather-bot
sudo systemctl restart weather-bot

# Live logs
sudo journalctl -u weather-bot -f

# Disable autostart on reboot
sudo systemctl disable weather-bot

# Run manually for debugging (without systemd)
cd ~/weather-bot
source venv/bin/activate
python bot.py
```

When you log out of PuTTY, the systemd service keeps running in the
background — that's the whole point of running it as a service.

---

## 🔐 Security note about your bot token

Your token (`8687484106:AAEh_...`) is in `.env.example` for convenience, but
`.env.example` does **not** contain anything secret on its own — it's only a
secret because of what *you* put in it.

For real production use:
1. Edit `.env` on the VPS to set your real token.
2. Never commit `.env` (it's already in `.gitignore`).
3. If you accidentally leak the token (e.g. paste it into a public chat), open
   a chat with **@BotFather** in Telegram and run `/revoke` → `/token` to
   regenerate it. Then update `.env` and `sudo systemctl restart weather-bot`.

---

## 🐛 Troubleshooting

**"`Conflict: terminated by other getUpdates request`"** — Another instance
of the bot is running with the same token. Stop the other one, or run
`sudo systemctl restart weather-bot`.

**"`Forbidden: bot was blocked by the user`"** — Just means a user blocked
your bot. Not an error you need to fix.

**Tracking alerts never fire** — They only fire after the *forecast* changes
between two consecutive checks (every 30 min by default). Set
`TRACKING_INTERVAL_MINUTES=10` in `.env` to check more often.

**Airport not found** — The bot uses the OurAirports global dataset
(~80k airports). Make sure you're using a valid ICAO (4-letter) or IATA
(3-letter) code. Heliports and seaplane bases are deliberately excluded.

**"`No module named 'telegram.ext.JobQueue'`"** — You forgot the
`[job-queue]` extra. Run:
```bash
source venv/bin/activate
pip install -U "python-telegram-bot[job-queue]"
sudo systemctl restart weather-bot
```

---

## 📜 License & data attribution

- Airport data: [OurAirports](https://ourairports.com/data/) (public domain).
- Weather models served via [Open-Meteo](https://open-meteo.com/) (free for
  non-commercial use).
- METAR observations: [aviationweather.gov](https://aviationweather.gov)
  (NOAA, public domain).
