# AiCrypto Investment Bot

Telegram bot with 1.5% daily profit, admin approvals, SQLite.

## Features
- 2-column main menu
- Deposit & Withdraw with admin approval
- 1.5% daily profit (auto)
- Full history
- Wallet settings
- Support button

## Setup
1. Copy `.env.example` → `.env`
2. Fill `BOT_TOKEN`, `ADMIN_ID`, `MASTER_WALLET`
3. `pip install -r requirements.txt`
4. `python bot.py`

## Deploy on Railway
1. Push to GitHub
2. Connect in Railway
3. Add env vars
4. Auto-deploy!