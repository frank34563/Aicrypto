import os
import logging
import sqlite3
from datetime import datetime
from typing import Dict, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

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
    raise ValueError("BOT_TOKEN not set in environment!")

ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
MASTER_WALLET = os.getenv('MASTER_WALLET', 'TAbcDeF123...XyZ789')
SUPPORT_USER = '@AiCrypto_Support1'

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# === STATES ===
(AMOUNT, TX_PROOF, WITHDRAW_AMOUNT, WALLET_ADDR, NETWORK) = range(5)

# === DATABASE ===
conn = sqlite3.connect('bot.db', check_same_thread=False)
cursor = conn.cursor()

cursor.execute('''
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    balance REAL DEFAULT 0.0,
    balance_in_process REAL DEFAULT 0.0,
    daily_profit REAL DEFAULT 0.0,
    total_profit REAL DEFAULT 0.0,
    wallet_address TEXT,
    network TEXT,
    joined_at TEXT DEFAULT CURRENT_TIMESTAMP
)
''')

cursor.execute('''
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
)
''')
conn.commit()

# === HELPERS ===
def get_user(user_id: int) -> Dict[str, Any]:
    cursor.execute('SELECT * FROM users WHERE id = ?', (user_id,))
    row = cursor.fetchone()
    if not row:
        cursor.execute('INSERT INTO users (id) VALUES (?)', (user_id,))
        conn.commit()
        return get_user(user_id)
    return {
        'id': row[0], 'balance': row[1], 'balance_in_process': row[2],
        'daily_profit': row[3], 'total_profit': row[4],
        'wallet_address': row[5], 'network': row[6], 'joined_at': row[7]
    }

def update_user(user_id: int, **kwargs):
    set_clause = ', '.join([f"{k} = ?" for k in kwargs])
    values = list(kwargs.values()) + [user_id]
    cursor.execute(f'UPDATE users SET {set_clause} WHERE id = ?', values)
    conn.commit()

def log_transaction(user_id: int, trans_type: str, amount: float, status: str, **extra):
    cursor.execute('''
    INSERT INTO transactions
    (user_id, type, amount, status, txid, wallet, network, screenshot_path, admin_note)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        user_id, trans_type, amount, status,
        extra.get('txid'), extra.get('wallet'), extra.get('network'),
        extra.get('screenshot_path'), extra.get('admin_note')
    ))
    conn.commit()

def get_history(user_id: int, limit: int = 5):
    cursor.execute('''
    SELECT * FROM transactions WHERE user_id = ? ORDER BY created_at DESC LIMIT ?
    ''', (user_id, limit))
    return cursor.fetchall()

# === DAILY PROFIT ===
async def daily_profit_job():
    cursor.execute('SELECT id, balance, balance_in_process FROM users')
    for user_id, balance, in_process in cursor.fetchall():
        total = balance + in_process
        if total <= 0:
            continue
        profit = round(total * 0.015, 2)
        user = get_user(user_id)
        update_user(
            user_id,
            daily_profit=profit,
            total_profit=user['total_profit'] + profit,
            balance=balance + profit
        )
        log_transaction(user_id, 'profit', profit, 'credited')

# === MENUS ===
def main_menu():
    return ReplyKeyboardMarkup([
        ['Balance', 'Invest'],
        ['Withdraw', 'History'],
        ['Settings', 'Information'],
        ['Help Up Arrow', 'Back Arrow']
    ], resize_keyboard=True)

# === HANDLERS ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Welcome to AiCrypto!', reply_markup=main_menu())

async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    msg = f"""Balance

Your Balance: {user['balance']:.2f}$
Balance in process: {user['balance_in_process']:.2f}$
Daily Profit: {user['daily_profit']:.2f}$
Total Profit: {user['total_profit']:.2f}$

Your personal manager: {SUPPORT_USER}"""
    await update.message.reply_text(msg)

async def invest_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Enter amount to invest (min 10 USDT):')
    return AMOUNT

async def invest_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

Send screenshot or TxID after payment."""
        await update.message.reply_text(msg)
        log_transaction(update.effective_user.id, 'deposit', amount, 'pending', wallet=MASTER_WALLET)
        return TX_PROOF
    except ValueError:
        await update.message.reply_text('Invalid number.')
        return AMOUNT

async def tx_proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    amount = context.user_data['invest_amount']
    proof = update.message.text or "Screenshot"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton('Approve', callback_data=f'approve_deposit_{user_id}_{amount}'),
         InlineKeyboardButton('Reject', callback_data=f'reject_deposit_{user_id}_{amount}')]
    ])
    await context.bot.send_message(
        ADMIN_ID,
        f"NEW DEPOSIT\nUser: {user_id}\nAmount: {amount:.2f}\nProof: {proof}",
        reply_markup=keyboard
    )
    if update.message.photo:
        await context.bot.forward_message(ADMIN_ID, user_id, update.message.message_id)
    await update.message.reply_text('Request sent to admin.')
    context.user_data.clear()
    return ConversationHandler.END

async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if not user['wallet_address']:
        await update.message.reply_text('Set wallet in Settings first.')
        return ConversationHandler.END
    await update.message.reply_text(f'Enter amount (Available: {user["balance"]:.2f}$):')
    return WITHDRAW_AMOUNT

async def withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text)
        user = get_user(update.effective_user.id)
        if amount > user['balance']:
            await update.message.reply_text('Not enough balance.')
            return WITHDRAW_AMOUNT
        update_user(
            user['id'],
            balance=user['balance'] - amount,
            balance_in_process=user['balance_in_process'] + amount
        )
        log_transaction(user['id'], 'withdraw', -amount, 'pending', wallet=user['wallet_address'])
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton('Pay & Approve', callback_data=f'approve_withdraw_{user["id"]}_{amount}'),
             InlineKeyboardButton('Reject', callback_data=f'reject_withdraw_{user["id"]}_{amount}')]
        ])
        await context.bot.send_message(
            ADMIN_ID,
            f"WITHDRAW\nUser: {user['id']}\nAmount: {amount:.2f}\nWallet: {user['wallet_address']}",
            reply_markup=keyboard
        )
        await update.message.reply_text('Withdrawal request sent.')
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text('Invalid amount.')
        return WITHDRAW_AMOUNT

async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    await update.message.reply_text(
        f"Settings\n\nWallet: {user['wallet_address'] or 'Not set'}",
        reply_markup=ReplyKeyboardMarkup([['Set Wallet']], resize_keyboard=True)
    )

async def set_wallet_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Send wallet address:')
    return WALLET_ADDR

async def wallet_addr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['wallet'] = update.message.text.strip()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton('TRC20', callback_data='net_TRC20')],
        [InlineKeyboardButton('ERC20', callback_data='net_ERC20')],
        [InlineKeyboardButton('BEP20', callback_data='net_BEP20')]
    ])
    await update.message.reply_text('Select network:', reply_markup=keyboard)
    return NETWORK

async def network_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    net = {'net_TRC20': 'TRC20', 'net_ERC20': 'ERC20', 'net_BEP20': 'BEP20'}[query.data]
    wallet = context.user_data['wallet']
    update_user(query.from_user.id, wallet_address=wallet, network=net)
    await query.edit_message_text(f"Wallet set: {wallet} ({net})")
    return ConversationHandler.END

async def information(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "AiCrypto Bot\n\n1.5% Daily Profit\nMin Deposit: 10 USDT\nContact: @AiCrypto_Support1"
    )

async def help_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton('Support', url=f't.me/{SUPPORT_USER[1:]}')]]
    await update.message.reply_text('Need help?', reply_markup=InlineKeyboardMarkup(keyboard))

async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_history(update.effective_user.id)
    if not rows:
        await update.message.reply_text('No history.')
        return
    msg = 'History\n\n'
    for r in rows:
        sign = '+' if r[3] > 0 else '-'
        msg += f"{sign}{abs(r[3]):.2f}$ | {r[2]} | {r[4]}\n"
    await update.message.reply_text(msg)

async def admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split('_')
    action, typ, uid, amt = data[0], data[1], int(data[2]), float(data[3])
    user = get_user(uid)

    if action == 'approve':
        if typ == 'deposit':
            update_user(uid, balance_in_process=user['balance_in_process'] + amt)
            log_transaction(uid, 'deposit', amt, 'approved')
            await context.bot.send_message(uid, f'Deposit {amt:.2f}$ approved.')
        else:
            update_user(uid, balance_in_process=user['balance_in_process'] - amt)
            log_transaction(uid, 'withdraw', -amt, 'paid')
            await context.bot.send_message(uid, f'Withdrawal {amt:.2f}$ sent!')
    else:
        if typ == 'deposit':
            await context.bot.send_message(uid, 'Deposit rejected.')
        else:
            update_user(uid, balance=user['balance'] + amt, balance_in_process=user['balance_in_process'] - amt)
            await context.bot.send_message(uid, 'Withdrawal rejected.')
    await query.edit_message_text(f'{action.title()}d {typ}')

# === MAIN ===
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex('^(Invest|Withdraw|Set Wallet)$'),
                           lambda u, c: invest_start(u, c) if 'Invest' in u.message.text
                           else withdraw_start(u, c) if 'Withdraw' in u.message.text
                           else set_wallet_start(u, c))
        ],
        states={
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, invest_amount)],
            TX_PROOF: [MessageHandler(filters.ALL & ~filters.COMMAND, tx_proof)],
            WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount)],
            WALLET_ADDR: [MessageHandler(filters.TEXT & ~filters.COMMAND, wallet_addr)],
            NETWORK: [CallbackQueryHandler(network_select)],
        },
        fallbacks=[]
    )

    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.Regex('^Balance$'), show_balance))
    app.add_handler(MessageHandler(filters.Regex('^History$'), show_history))
    app.add_handler(MessageHandler(filters.Regex('^Settings$'), settings_menu))
    app.add_handler(MessageHandler(filters.Regex('^Information$'), information))
    app.add_handler(MessageHandler(filters.Regex('^Help Up Arrow$'), help_btn))
    app.add_handler(MessageHandler(filters.Regex('^Back Arrow$'), lambda u, c: u.message.reply_text('Back', reply_markup=main_menu())))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(admin_action, pattern=r'^(approve|reject)_(deposit|withdraw)_'))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(daily_profit_job, 'cron', hour=0, minute=0)
    scheduler.start()

    print("AiCrypto Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
