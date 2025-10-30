# AiCrypto Investment Bot (v2.0 – Fixed + Postgres + Referrals)

## New Features
- **Postgres Support:** Production-ready DB (Railway auto-provision).
- **Referrals:** Unique links, 5% first deposit bonus, 1% daily profit share.
- **Fixed for Python 3.13:** Uses python-telegram-bot v21+.

## Setup
1. Copy `.env.example` → `.env` (local) or add vars in Railway.
2. For Postgres: Provision in Railway → Copy DATABASE_URL.
3. `pip install -r requirements.txt`
4. `python bot.py`

## Deploy on Railway
1. Push to GitHub.
2. New Project → GitHub Repo → Provision Postgres (free tier).
3. Add vars: BOT_TOKEN, ADMIN_ID, MASTER_WALLET, DATABASE_URL.
4. Deploy – Bot live!
