# SF Apartment Alert — Setup Guide

## Quick start

```bash
git clone <this-repo>
cd Weiss-Dashboard
pip install -r requirements.txt
cp .env.example .env
# fill in .env (steps below), then:
python run.py --dry-run    # test fetch + filter, no email
python run.py              # full run: fetch, score, email
```

---

## §1 — Anthropic API key

1. Go to **https://console.anthropic.com** and sign in (or create an account).
2. Click **API Keys** in the left sidebar → **Create Key**.
3. Copy the key (starts with `sk-ant-`).
4. Paste it into `.env`:
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```

Cost estimate: each run scores up to 25 listings × 4 photos each.
At current pricing (~$0.003/image input), that's about **$0.30 max per run**,
usually much less once the database fills with seen listings.

---

## §2 — Gmail App Password

Gmail requires an *App Password* when using SMTP — not your real password.

1. **Enable 2-Step Verification** on your Google account if it isn't already:
   https://myaccount.google.com/security → 2-Step Verification → Turn on.

2. Go to **https://myaccount.google.com/apppasswords** (you must have 2FA on).

3. In the "App name" field type `apartment-alert` → click **Create**.

4. Google shows you a 16-character password like `xxxx xxxx xxxx xxxx`.
   Copy it exactly (spaces are fine — Gmail ignores them).

5. Paste into `.env`:
   ```
   GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
   GMAIL_FROM=adam.jacob.weiss@gmail.com
   GMAIL_TO=adam.jacob.weiss@gmail.com
   ```

---

## §3 — First run (get a real test batch)

```bash
# Full pipeline — fetches, scores with Claude, emails you:
python run.py

# Or with verbose logging to see everything:
python run.py -v

# Dry-run — fetch + filter only, no Claude API calls, no email:
python run.py --dry-run

# Check what's in the database:
python run.py --stats

# Start fresh (clear the seen-listings database):
python run.py --reset-db
```

The first run fetches 3 pages × 120 = up to 360 listings. Expect 5-30 to
pass filters (depending on market conditions), and those will be scored and
emailed. Future runs are much faster since the database skips already-seen IDs.

---

## §4 — Scheduling: local cron vs. cloud VM

### Option A — Local cron (free, your machine must be running)

Edit your crontab:
```bash
crontab -e
```

Add this line (adjust path):
```
0 * * * * cd /path/to/Weiss-Dashboard && /usr/bin/python3 run.py >> ~/apartment_alert.log 2>&1
```

This fires at the top of every hour. Works great if your machine is usually on;
misses runs when it's off or sleeping.

### Option B — $5/mo cloud VM (recommended for reliability) ✓

A 1GB VPS from DigitalOcean, Linode, or Vultr runs ~$5/mo and stays on 24/7.
**Important:** Pick a datacenter in the US (San Francisco or New York preferred) —
fresh residential-region IPs are rarely blocked by Craigslist.

Setup on a fresh Ubuntu VM:
```bash
sudo apt update && sudo apt install -y python3 python3-pip git
git clone <your-repo-url>
cd Weiss-Dashboard
pip3 install -r requirements.txt
cp .env.example .env && nano .env   # fill in your secrets
python3 run.py --dry-run            # verify it works
crontab -e
# Add: 0 * * * * cd ~/Weiss-Dashboard && python3 run.py >> ~/apartment_alert.log 2>&1
```

**Why the VM wins:** apartment searches are time-sensitive — Tier 1 listings in
Noe Valley / Pacific Heights at ≤$5k get rented within hours. Reliable hourly
polling from a always-on VM means you won't miss the window because your laptop
was asleep.

### A note on Craigslist and datacenter IPs

Craigslist blocks HTTP requests from well-known cloud datacenter CIDR ranges
(AWS, GCP, Azure) with 403. A fresh DigitalOcean/Linode/Vultr VM in a US
region usually works fine. If you get persistent 403s from your VM, try:
- Choosing a different datacenter region
- Switching providers (Vultr's residential-ISP IPs have good success rates)

---

## §5 — Architecture overview

```
run.py
└── apartment_alert/main.py          orchestrates the pipeline
    ├── fetcher.py                   Craigslist HTML scraper (swap in other sources here)
    ├── filter.py                    price / neighborhood / room-share filtering
    ├── scorer.py                    Claude vision API scoring
    ├── database.py                  SQLite dedup (seen IDs + fuzzy title matching)
    └── emailer.py                   Gmail SMTP HTML email
```

**Adding a new listing source:** implement a class with `fetch_listings()` and
`fetch_listing_detail()` matching `CraigslistFetcher`'s signatures, then call
it alongside `CraigslistFetcher` in `main.py`.

---

## §6 — Tuning

Edit `apartment_alert/config.py` for runtime constants:

| Variable | Default | Meaning |
|---|---|---|
| `MAX_PRICE` | 5000 | Hard price ceiling |
| `MIN_BEDROOMS` | 1 | Minimum bedrooms |
| `REQUEST_DELAY` | 2.5 | Seconds between CL requests |
| `MAX_PHOTOS_PER_LISTING` | 4 | Photos sent to Claude per listing |
| `MAX_LISTINGS_TO_SCORE` | 25 | Cap on Claude calls per run |
| `MIN_SCORE_TO_EMAIL` | 5 | Minimum Claude score to include in email |

Neighborhood tiers and excluded areas are in `apartment_alert/filter.py` —
edit `TIER_1`, `TIER_2`, and `EXCLUDED` sets to adjust.
