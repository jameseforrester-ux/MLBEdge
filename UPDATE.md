# Polymarket Update — Drop-in Files

This update adds Polymarket prediction-market integration to your bot. **No
new dependencies, no reinstall, no DB rebuild needed** — the tracking schema
auto-migrates on startup.

## What's in this zip

| File              | Status      | Purpose                                            |
| ----------------- | ----------- | -------------------------------------------------- |
| `polymarket.py`   | **NEW**     | Polymarket Gamma API + bucket parsing + selectors  |
| `bot.py`          | **CHANGED** | Renders the Polymarket section + alert integration |
| `tracking.py`     | **CHANGED** | Adds `last_bucket` column (auto-migrates on boot)  |
| `README.md`       | **CHANGED** | Updated feature list                               |

## How to deploy

### Step 1 — drop into your local repo & push

In the `weather-bot` folder on your laptop (the one wired up to GitHub):

```bash
# Copy the 4 files from this update zip on top of your local repo, overwriting
# the old bot.py, tracking.py, README.md, and adding the new polymarket.py.

git add polymarket.py bot.py tracking.py README.md
git commit -m "Add Polymarket integration"
git push
```

### Step 2 — pull & restart on the VPS (PuTTY)

```bash
cd ~/weather-bot
git pull
sudo systemctl restart weather-bot
sudo systemctl status weather-bot --no-pager
```

That's it. No `pip install`, no recreating the venv, no touching `.env`. The
tracking DB migrates itself the first time the bot starts up.

### Step 3 — verify in Telegram

Watch live logs in PuTTY:

```bash
sudo journalctl -u weather-bot -f
```

Then in Telegram:
1. `/forecast KJFK` → you should see a 🎲 *Polymarket — NYC (F°)* section
   under today's forecast with the top 3 buckets and a ✅ next to the one
   the model agrees with. Each bucket has a `[Trade]` deep link.
2. `/forecast EDDF` (Frankfurt) → no Polymarket section (silently hidden,
   per your preference) since Frankfurt isn't covered.
3. Toronto, London, Paris, Tokyo render in °C; US cities + LA/Miami/etc. in °F.

## Supported cities

NYC (KJFK/KLGA/KEWR), Los Angeles, Chicago, Miami, Philadelphia, Austin,
Denver, Houston, Atlanta, Dallas, Seattle, San Francisco, Toronto (CYYZ),
London (EGLL etc.), Paris (LFPG), Tokyo (RJTT/RJAA).

Unsupported airports just hide the section — no error, no fallback to a
different city's market.

## How tracking alerts work now

Tracking still fires on a temperature delta (≥2°F / ≥1°C) as before. **In
addition**, when the model's predicted bucket changes (e.g. 54-55°F → 56-57°F
on NYC, or 22°C → 23°C on Toronto), the alert message includes the full
Polymarket section showing top 3 + hedges + ✅ + [Trade] links — exactly the
behavior you asked for.

If the bot can't find a Polymarket event for a tracked airport's city, the
alert just looks like before (no market data appended).
