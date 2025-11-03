# AiCrypto Bot – Production Ready

## Features

- **Automated Trading System**: Configurable daily profit percentage with scheduled trades
- **Binance Integration**: Real-time price fetching via httpx with fallback to simulated prices
- **User Management**: Custom crypto pairs and per-trade percentages for individual users
- **Comprehensive Reporting**: End-of-day summaries and admin trading statistics
- **Referral System**: 5% first deposit + 1% daily profit sharing
- **Admin Controls**: Full configuration and monitoring capabilities

## Tech Stack

- Python 3.13+ with asyncio
- SQLAlchemy 2.0 (Postgres/SQLite)
- python-telegram-bot 21.4+
- httpx for Binance API
- APScheduler for automated jobs

## Environment Variables

Required:
- `BOT_TOKEN` - Telegram bot token
- `ADMIN_ID` - Telegram user ID of admin (numeric)

Optional:
- `DATABASE_URL` - Database connection (defaults to SQLite)
- `MASTER_WALLET` - Master wallet address
- `MASTER_NETWORK` - Network type (e.g., TRC20)
- `SUPPORT_USER` - Support contact username
- `BINANCE_CACHE_TTL` - Price cache TTL in seconds (default: 10)

## Admin Commands

### Trading Configuration
- `/set_daily_payout <percent>` - Set daily profit percentage (e.g., `/set_daily_payout 2.0`)
- `/set_trades_per_day <number>` - Set number of trades per day (e.g., `/set_trades_per_day 20`)
- `/set_percent_per_trade <percent>` - Set percent per trade globally (e.g., `/set_percent_per_trade 0.25`)
- `/trade_on` - Enable automated trading
- `/trade_off` - Disable automated trading
- `/trade_status` - Show current trading configuration

### User Management
- `/assign_pairs <user_id> <pair1> <pair2> ...` - Assign specific crypto pairs to a user
  - Example: `/assign_pairs 12345 BTCUSDT ETHUSDT BNBUSDT`
- `/assign_percent <user_id> <percent>` - Assign custom per-trade percentage to a user
  - Example: `/assign_percent 12345 0.35`

### Reports & Monitoring
- `/trading_summary` - Show trading day statistics (total users, active traders, profits, trades count)
- `/user_summary <user_id>` - Show detailed user trading summary

### Binance Settings
- `/use_binance_on` - Enable real Binance price fetching
- `/use_binance_off` - Disable Binance (use simulated prices)

### Help
- `/admin_cmds` - Show all admin commands

## User Features

- `/start` - Start the bot
- `/menu` - Open main menu with:
  - 💰 Balance - View balance, profits, and in-process amounts
  - 📈 Invest - Deposit funds (with admin approval)
  - 🧾 History - View transaction history
  - 💸 Withdraw - Request withdrawals
  - 👥 Referrals - View referral stats and link
  - ⚙️ Settings - Update language and wallet
  - ℹ️ Information - Bot information
  - ❓ Help - Contact support

## Automated Features

### Trading Jobs
- Executes trades automatically based on configured frequency
- Calculates interval: `(24 * 60 minutes) / trades_per_day`
- Supports custom crypto pairs and percentages per user
- Logs all trades to database

### Daily Profit Reset
- Runs at midnight UTC
- Sends end-of-day summary to each user with:
  - Daily profit amount
  - Profit percentage
  - Total balance
  - All-time total profit
- Resets daily profit counter

## Deployment

### Railway (Recommended)
1. Push code to GitHub
2. Create new Railway project from GitHub repo
3. Add Postgres database addon
4. Set environment variables:
   - `BOT_TOKEN`
   - `ADMIN_ID`
   - `MASTER_WALLET` (optional)
   - `DATABASE_URL` (auto-set by Railway)
5. Deploy!

### Local Development
1. Clone repository
2. Install dependencies: `pip install -r requirements.txt`
3. Create `.env` file with required variables
4. Run: `python bot.py`

## Database Schema

### Users Table
- Balance tracking (available, in-process)
- Daily profit and total profit
- Referral system data
- Wallet information

### Transactions Table
- All investment, withdrawal, and trade records
- Status tracking (pending, completed, rejected)
- Proof of transaction (TXID or file ID)

## Notes

- Trading simulation generates realistic profit/loss with 70% profit probability
- Binance price fetching includes TTL caching to avoid rate limits
- All previous invest/withdraw/admin features are preserved
- Fallback to SQLite if PostgreSQL connection fails
