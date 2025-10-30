import os
import logging
import sqlite3
from datetime import datetime
from typing import Dict, Any
import asyncio

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
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))
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

# Users table
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

# Transactions table
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

# === DAILY PROFIT JOB ===
async def daily_profit_job():
    cursor.execute('SELECT id, balance, balance_in_process FROM users')
    for user_id, balance, in_process in cursor.fetchall():
        total = balance + in_process
        if total <= 0:
            continue
        profit = round(total * 0.015, 2)  # 1.5%
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
            await update.message.reply_text('Minimum 10 USDT. Try again.')
            return AMOUNT
        context.user_data['invest_amount'] = amount
        memo = f"MEMO_{update.effective_user.id}_{int(datetime.now().timestamp())}"
        msg = f"""Deposit {amount:.2f} USDT to:

Wallet (TRC20): {MASTER_WALLET} (Memo: {memo})
Network: TRON (TRC20)
Expires in 30 minutes

After payment, send:
• Screenshot of transaction OR
• Transaction Hash (TxID)"""
        await update.message.reply_text(msg)
        log_transaction(update.effective_user.id, 'deposit', amount, 'pending', wallet=MASTER_WALLET)
        return TX_PROOF
    except ValueError:
        await update.message.reply_text('Please enter a valid number.')
        return AMOUNT

async def tx_proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    amount = context.user_data['invest_amount']
    proof = update.message.text or "Screenshot received"
    screenshot_path = update.message.photo[-1].file_id if update.message.photo else None

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton('Approve', callback_data=f'approve_deposit_{user_id}_{amount}'),
         InlineKeyboardButton('Reject', callback_data=f'reject_deposit_{user_id}_{amount}')]
    ])
    await context.bot.send_message(
        ADMIN_ID,
        f"NEW DEPOSIT\nUser: {user_id}\nAmount: {amount:.2f} USDT\nProof: {proof}",
        reply_markup=keyboard
    )
    if screenshot_path:
        await context.bot.forward_message(ADMIN_ID, user_id, update.message.message_id)

    await update.message.reply_text('Deposit request sent for approval.')
    context.user_data.clear()
    return ConversationHandler.END

async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if not user['wallet_address']:
        await update.message.reply_text('Set your wallet in Settings first.')
        return ConversationHandler.END
    await update.message.reply_text(f'Enter withdrawal amount:\nAvailable: {user["balance"]:.2f}$')
    return WITHDRAW_AMOUNT

async def withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text)
        user = get_user(update.effective_user.id)
        if amount > user['balance']:
            await update.message.reply_text('Amount exceeds available balance.')
            return WITHDRAW_AMOUNT
        context.user_data['withdraw_amount'] = amount
        update_user(
            user['id'],
            balance=user['balance'] - amount,
            balance_in_process=user['balance_in_process'] + amount
        )
        log_transaction(user['id'], 'withdraw', -amount, 'pending', wallet=user['wallet_address'], network=user['network'])

        msg = f"""Withdrawal Request Submitted
• Amount: {amount:.2f} USDT
• Wallet: {user['wallet_address']} ({user['network']})
• Status: Pending Approval

Funds moved to "Balance in process" """
        await update.message.reply_text(msg)

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton('Pay & Approve', callback_data=f'approve_withdraw_{user["id"]}_{amount}'),
             InlineKeyboardButton('Reject', callback_data=f'reject_withdraw_{user["id"]}_{amount}')]
        ])
        await context.bot.send_message(
            ADMIN_ID,
            f"WITHDRAWAL REQUEST\nUser: {user['id']}\nAmount: {amount:.2f} USDT\nWallet: {user['wallet_address']}",
            reply_markup=keyboard
        )
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text('Invalid amount.')
        return WITHDRAW_AMOUNT

async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    wallet = user['wallet_address'] or 'Not set'
    keyboard = [['Set Wallet', 'Change Wallet']]
    await update.message.reply_text(
        f"Settings\n\nWithdrawal Wallet: {wallet}",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
    )

async def set_wallet_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Send your withdrawal wallet address:\nExample: TAbc123...xyz789')
    return WALLET_ADDR

async def wallet_addr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['wallet_address'] = update.message.text.strip()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton('TRON (TRC20)', callback_data='net_TRC20')],
        [InlineKeyboardButton('Ethereum (ERC20)', callback_data='net_ERC20')],
        [InlineKeyboardButton('BSC (BEP20)', callback_data='net_BEP20')]
    ])
    await update.message.reply_text('Select network:', reply_markup=keyboard)
    return NETWORK

async def network_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    net_map = {'net_TRC20': 'TRC20', 'net_ERC20': 'ERC20', 'net_BEP20': 'BEP20'}
    network = net_map[query.data]
    wallet = context.user_data['wallet_address']
    user_id = query.from_user.id
    update_user(user_id, wallet_address=wallet, network=network)
    await query.edit_message_text(f"Wallet saved: {wallet} ({network})")
    await query.message.reply_text('Back to main menu.', reply_markup=main_menu())
    context.user_data.clear()
    return ConversationHandler.END

async def information(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = """AiCrypto Investment Bot

Earn passive income with AI-powered crypto strategies.

Minimum Deposit: 10 USDT
Daily Profit: 1.5% on all deposited amounts
Funds Secured via Smart Contracts
24/7 Support

Start small, grow big. Your financial future begins now.

Contact: @AiCrypto_Support1"""
    await update.message.reply_text(msg)

async def help_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton('Message @AiCrypto_Support1', url='https://t.me/AiCrypto_Support1')]]
    await update.message.reply_text('Need help? Chat with your manager:', reply_markup=InlineKeyboardMarkup(keyboard))

async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_history(update.effective_user.id)
    if not rows:
        await update.message.reply_text('No transactions yet.')
        return
    msg = 'Transaction History\n\n'
    for row in rows:
        sign = '+' if row[3] > 0 else '-'
        date = row[10][:16]
        msg += f'[{sign}] {date} | {row[2].capitalize()} | {abs(row[3]):.2f}$ | {row[4].capitalize()}\n'
    await update.message.reply_text(msg)

# === ADMIN CALLBACK ===
async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split('_')
    action, req_type, user_id, amount = data[0], data[1], int(data[2]), float(data[3])
    user = get_user(user_id)

    if action == 'approve':
        if req_type == 'deposit':
            update_user(user_id, balance_in_process=user['balance_in_process'] + amount)
            log_transaction(user_id, 'deposit', amount, 'approved')
            await context.bot.send_message(user_id, f'Deposit of {amount:.2f}$ approved.')
        else:  # withdraw
            update_user(user_id, balance_in_process=user['balance_in_process'] - amount)
            log_transaction(user_id, 'withdraw', -amount, 'paid')
            await context.bot.send_message(user_id, f'Withdrawal of {amount:.2f}$ paid!')
    else:  # reject
        if req_type == 'deposit':
            log_transaction(user_id, 'deposit', amount, 'rejected')
            await context.bot.send_message(user_id, 'Deposit rejected. Contact support.')
        else:
            update_user(user_id, balance=user['balance'] + amount, balance_in_process=user['balance_in_process'] - amount)
            log_transaction(user_id, 'withdraw', -amount, 'rejected')
            await context.bot.send_message(user_id, 'Withdrawal rejected.')

    await query.edit_message_text(f'{action.capitalize()}d {req_type} for user {user_id}')

# === MAIN ===
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Conversation handler
    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex('^(Invest|Withdraw|Set Wallet|Change Wallet)$'), 
                           lambda u, c: invest_start(u, c) if u.message.text == 'Invest' else
                           withdraw_start(u, c) if u.message.text == 'Withdraw' else set_wallet_start(u, c))
        ],
        states={
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, invest_amount)],
            TX_PROOF: [MessageHandler(filters.ALL, tx_proof)],
            WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount)],
            WALLET_ADDR: [MessageHandler(filters.TEXT & ~filters.COMMAND, wallet_addr)],
            NETWORK: [CallbackQueryHandler(network_select)],
        },
        fallbacks=[CommandHandler('cancel', lambda u, c: u.message.reply_text('Cancelled.', reply_markup=main_menu()))]
    )

    # Commands
    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.Regex('^Balance$'), show_balance))
    app.add_handler(MessageHandler(filters.Regex('^History$'), show_history))
    app.add_handler(MessageHandler(filters.Regex('^Settings$'), settings_menu))
    app.add_handler(MessageHandler(filters.Regex('^Information$'), information))
    app.add_handler(MessageHandler(filters.Regex('^Help Up Arrow$'), help_support))
    app.add_handler(MessageHandler(filters.Regex('^Back Arrow$'), lambda u, c: u.message.reply_text('Back', reply_markup=main_menu())))
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r'^(approve|reject)_(deposit|withdraw)_'))

    # Daily profit
    scheduler = AsyncIOScheduler()
    scheduler.add_job(daily_profit_job, 'cron', hour=0, minute=0)
    scheduler.start()

    print("Bot is running...")
    app.run_polling()

if __name__ == '__main__':
    main()
