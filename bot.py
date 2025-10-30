import os
import logging
import sqlite3
import psycopg2
from datetime import datetime
from typing import Dict, Any
from dotenv import load_dotenv

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from telegram import (
    Update, ReplyKeyboardMarkup, InlineKeyboardMarkup,
    InlineKeyboardButton, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes, ConversationHandler
)

load_dotenv()

# === CONFIG ===
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set!")

ADMIN_ID = int(os.getenv('ADMIN_ID', 0))
MASTER_WALLET = os.getenv('MASTER_WALLET', 'TAbcDeF123...XyZ789')
SUPPORT_USER = '@AiCrypto_Support1'

DATABASE_URL = os.getenv('DATABASE_URL')  # Railway Postgres: postgres://user:pass@host/db

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# === STATES ===
(AMOUNT, TX_PROOF, WITHDRAW_AMOUNT, WALLET_ADDR, NETWORK) = range(5)

# === DATABASE SETUP ===
DB_CONN = None
CURSOR = None

def init_db():
    global DB_CONN, CURSOR
    if DATABASE_URL:
        # Postgres for Railway/Production
        DB_CONN = psycopg2.connect(DATABASE_URL)
        CURSOR = DB_CONN.cursor()
        CURSOR.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id BIGINT PRIMARY KEY,
            balance DECIMAL(15,2) DEFAULT 0.00,
            balance_in_process DECIMAL(15,2) DEFAULT 0.00,
            daily_profit DECIMAL(15,2) DEFAULT 0.00,
            total_profit DECIMAL(15,2) DEFAULT 0.00,
            referral_count INTEGER DEFAULT 0,
            referral_earnings DECIMAL(15,2) DEFAULT 0.00,
            referrer_id BIGINT,
            wallet_address TEXT,
            network TEXT,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        ''')
        CURSOR.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            type TEXT,
            amount DECIMAL(15,2),
            status TEXT,
            txid TEXT,
            wallet TEXT,
            network TEXT,
            screenshot_path TEXT,
            admin_note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        ''')
        DB_CONN.commit()
    else:
        # SQLite fallback for local
        DB_CONN = sqlite3.connect('bot.db', check_same_thread=False)
        CURSOR = DB_CONN.cursor()
        CURSOR.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            balance REAL DEFAULT 0.0,
            balance_in_process REAL DEFAULT 0.0,
            daily_profit REAL DEFAULT 0.0,
            total_profit REAL DEFAULT 0.0,
            referral_count INTEGER DEFAULT 0,
            referral_earnings REAL DEFAULT 0.0,
            referrer_id INTEGER,
            wallet_address TEXT,
            network TEXT,
            joined_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        ''')
        CURSOR.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            type TEXT,
            amount REAL,
            status TEXT,
            txid TEXT,
            wallet TEXT,
            network TEXT,
            screenshot_path TEXT,
            admin_note TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        ''')
        DB_CONN.commit()

init_db()

# === HELPERS ===
def get_user(user_id: int) -> Dict[str, Any]:
    CURSOR.execute('SELECT * FROM users WHERE id = %s', (user_id,))
    row = CURSOR.fetchone()
    if not row:
        CURSOR.execute('INSERT INTO users (id) VALUES (%s)', (user_id,))
        DB_CONN.commit()
        return get_user(user_id)
    columns = ['id', 'balance', 'balance_in_process', 'daily_profit', 'total_profit', 'referral_count', 'referral_earnings', 'referrer_id', 'wallet_address', 'network', 'joined_at']
    return dict(zip(columns, row))

def update_user(user_id: int, **kwargs):
    set_clause = ', '.join([f"{k} = %({k})s" for k in kwargs])
    kwargs['id'] = user_id
    query = f'UPDATE users SET {set_clause} WHERE id = %(id)s'
    CURSOR.execute(query, kwargs)
    DB_CONN.commit()

def log_transaction(user_id: int, trans_type: str, amount: float, status: str, **extra):
    extra['user_id'] = user_id
    extra['type'] = trans_type
    extra['amount'] = amount
    extra['status'] = status
    columns = ', '.join(extra.keys())
    placeholders = ', '.join(['%(' + k + ')s' for k in extra.keys()])
    CURSOR.execute(f'INSERT INTO transactions ({columns}) VALUES ({placeholders})', extra)
    DB_CONN.commit()

def get_history(user_id: int, limit: int = 5):
    CURSOR.execute('SELECT * FROM transactions WHERE user_id = %s ORDER BY created_at DESC LIMIT %s', (user_id, limit))
    return CURSOR.fetchall()

def handle_referral(user_id: int, ref_code: str = None):
    if ref_code:
        try:
            referrer_id = int(ref_code)
            if referrer_id != user_id:
                update_user(user_id, referrer_id=referrer_id)
                update_user(referrer_id, referral_count=get_user(referrer_id)['referral_count'] + 1)
                return True
        except ValueError:
            pass
    return False

# === DAILY PROFIT (INCLUDES REFERRAL BONUS) ===
async def daily_profit_job():
    CURSOR.execute('SELECT * FROM users')
    users = CURSOR.fetchall()
    for user_row in users:
        user = dict(zip(['id', 'balance', 'balance_in_process', 'total_profit', 'referrer_id'], user_row[:5] + [user_row[7]]))
        total = user['balance'] + user['balance_in_process']
        if total <= 0:
            continue
        profit = round(total * 0.015, 2)  # 1.5%
        update_user(
            user['id'],
            daily_profit=profit,
            total_profit=user['total_profit'] + profit,
            balance=user['balance'] + profit
        )
        log_transaction(user['id'], 'profit', profit, 'credited')
        # Referral bonus: 1% of profit to referrer
        if user['referrer_id']:
            ref_bonus = round(profit * 0.01, 2)
            referrer = get_user(user['referrer_id'])
            update_user(user['referrer_id'], referral_earnings=referrer['referral_earnings'] + ref_bonus, balance=referrer['balance'] + ref_bonus)
            log_transaction(user['referrer_id'], 'referral_profit', ref_bonus, 'credited')

# === MENUS ===
def main_menu():
    return ReplyKeyboardMarkup([
        ['Balance', 'Invest'],
        ['Withdraw', 'History'],
        ['Referrals', 'Settings'],
        ['Information', 'Help Up Arrow']
    ], resize_keyboard=True)

def referrals_menu():
    return ReplyKeyboardMarkup([['My Referral Link', 'Referral Stats']], resize_keyboard=True, one_time_keyboard=True)

# === HANDLERS ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    ref_code = args[0] if args else None
    if handle_referral(user_id, ref_code):
        await update.message.reply_text('Welcome! You joined via referral – bonus activated!')
    else:
        await update.message.reply_text('Welcome to AiCrypto!')
    await update.message.reply_text('Use the menu to get started.', reply_markup=main_menu())

async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    msg = f"""Balance

Your Balance: {user['balance']:.2f}$
Balance in process: {user['balance_in_process']:.2f}$
Daily Profit: {user['daily_profit']:.2f}$
Total Profit: {user['total_profit']:.2f}$
Referral Earnings: {user['referral_earnings']:.2f}$

Your personal manager: {SUPPORT_USER}"""
    await update.message.reply_text(msg)

async def invest_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (same as before, but add 5% bonus to referrer on first deposit approval)
    # In admin approve: if first deposit, add 5% to referrer
    try:
        amount = float(update.message.text)
        if amount < 10:
            await update.message.reply_text('Minimum 10 USDT.')
            return AMOUNT
        context.user_data['invest_amount'] = amount
        memo = f"MEMO_{update.effective_user.id}_{int(datetime.now().timestamp())}"
        msg = f"""Deposit {amount:.2f} USDT to:

Wallet (TRC20): {MASTER_WALLET} (Memo: {memo})
Network: TRON (TRC20)
Expires in 30 min

Send screenshot or TxID."""
        await update.message.reply_text(msg)
        log_transaction(update.effective_user.id, 'deposit', amount, 'pending', wallet=MASTER_WALLET)
        return TX_PROOF
    except ValueError:
        await update.message.reply_text('Invalid number.')
        return AMOUNT

# Update admin approve for deposit to include referral bonus
async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (same as before)
    if action == 'approve' and req_type == 'deposit':
        # ... approve logic
        user = get_user(user_id)
        if user['referrer_id'] and user['balance'] == 0:  # First deposit
            bonus = round(amount * 0.05, 2)
            referrer = get_user(user['referrer_id'])
            update_user(user['referrer_id'], referral_earnings=referrer['referral_earnings'] + bonus, balance=referrer['balance'] + bonus)
            log_transaction(user['referrer_id'], 'referral_bonus', bonus, 'credited')
            await context.bot.send_message(user['referrer_id'], f'Referral bonus: +{bonus:.2f}$ from new user!')

# Referrals handler
async def referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    ref_link = f"https://t.me/{context.bot.username}?start=ref_{user['id']}"
    msg = f"""Referrals

Your Link: {ref_link}
Referred Users: {user['referral_count']}
Earnings: {user['referral_earnings']:.2f}$

Share to earn 5% on first deposits + 1% daily profits!"""
    await update.message.reply_text(msg, reply_markup=referrals_menu())

async def my_ref_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    ref_link = f"https://t.me/{context.bot.username}?start=ref_{user['id']}"
    await update.message.reply_text(f'Your Referral Link: {ref_link}')

async def ref_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    await update.message.reply_text(f'Referred: {user["referral_count"]}\nEarnings: {user["referral_earnings"]:.2f}$')

# ... (Other handlers like withdraw, settings, history, etc. remain the same as previous version)

# === MAIN ===
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # ConversationHandler (same as before, with added referrals entry if needed)

    # Add referral handlers
    app.add_handler(MessageHandler(filters.Regex('^Referrals$'), referrals))
    app.add_handler(MessageHandler(filters.Regex('^My Referral Link$'), my_ref_link))
    app.add_handler(MessageHandler(filters.Regex('^Referral Stats$'), ref_stats))

    # ... (All other handlers)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(daily_profit_job, 'cron', hour=0, minute=0)
    scheduler.start()

    print("AiCrypto Bot running with Postgres + Referrals...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
