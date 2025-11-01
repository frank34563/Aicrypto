# Full final bot.py — Reply keyboard on /start removed.
# History buttons show only DATE | AMOUNT | TYPE and details include Back/Exit.
# Referral "Copy Link" uses switch_inline_query_current_chat and leaves the referral link message in chat.
#
# Environment variables required:
# - BOT_TOKEN (required)
# - ADMIN_ID (required, numeric)
# - MASTER_WALLET (recommended)
# - MASTER_NETWORK (recommended)
# - DATABASE_URL (optional; if not set, sqlite will be used)
# - ADMIN_LOG_CHAT_ID (optional) — chat id for admin audit logs

import os
import logging
import random
import re
from datetime import datetime
from typing import Dict, Optional, List, Tuple
from dotenv import load_dotenv

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime,
    BigInteger, select, func, Numeric, text, update as sa_update
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

# === CONFIG ===
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set!")

ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
ADMIN_LOG_CHAT_ID = os.getenv('ADMIN_LOG_CHAT_ID')  # optional
MASTER_WALLET = os.getenv('MASTER_WALLET', 'TAbc...')
MASTER_NETWORK = os.getenv('MASTER_NETWORK', 'TRC20')
SUPPORT_USER = os.getenv('SUPPORT_USER', '@AiCrypto_Support1')
SUPPORT_URL = os.getenv('SUPPORT_URL') or (f"https://t.me/{SUPPORT_USER.lstrip('@')}" if SUPPORT_USER else None)

MENU_FULL_WIDTH = os.getenv('MENU_FULL_WIDTH', 'false').strip().lower() in ('1', 'true', 'yes', 'on')
DATABASE_URL = os.getenv('DATABASE_URL')

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === DATABASE ===
Base = declarative_base()

if DATABASE_URL:
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
    elif DATABASE_URL.startswith("postgresql://") and "+psycopg" not in DATABASE_URL:
        DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
else:
    DATABASE_URL = "sqlite+aiosqlite:///bot.db"

engine = create_async_engine(DATABASE_URL, echo=False, future=True)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# === MODELS ===
class User(Base):
    __tablename__ = 'users'
    id = Column(BigInteger, primary_key=True)
    balance = Column(Numeric(15, 2), default=0.0)
    balance_in_process = Column(Numeric(15, 2), default=0.0)
    daily_profit = Column(Numeric(15, 2), default=0.0)
    total_profit = Column(Numeric(15, 2), default=0.0)
    referral_count = Column(Integer, default=0)
    referral_earnings = Column(Numeric(15, 2), default=0.0)
    referrer_id = Column(BigInteger, nullable=True)
    wallet_address = Column(String)
    wallet_network = Column(String)
    joined_at = Column(DateTime, default=datetime.utcnow)


class Transaction(Base):
    __tablename__ = 'transactions'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger)
    ref = Column(String)            # random 5-digit reference
    type = Column(String)           # 'invest' or 'withdraw' or 'profit'
    amount = Column(Numeric(15, 2))
    status = Column(String)         # 'pending','credited','rejected','completed'
    proof = Column(String)          # txid or file_id (format: 'photo:<file_id>' for photos)
    wallet = Column(String)
    network = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


# DB init helpers
import asyncio, sys, time

async def _create_all_with_timeout(engine_to_use):
    async with engine_to_use.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def ensure_columns():
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS wallet_network VARCHAR"))
        await conn.execute(text("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS proof VARCHAR"))
        await conn.execute(text("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS wallet VARCHAR"))
        await conn.execute(text("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS network VARCHAR"))
        await conn.execute(text("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS ref VARCHAR"))

async def init_db(retries: int = 5, backoff: float = 2.0, fallback_to_sqlite: bool = True):
    global engine, async_session, DATABASE_URL
    last_exc = None
    attempt = 0
    current_engine = engine
    while attempt < retries:
        attempt += 1
        try:
            await _create_all_with_timeout(current_engine)
            try:
                await ensure_columns()
            except Exception as col_exc:
                logger.warning("ensure_columns warning: %s", col_exc)
            logger.info("Database initialized successfully.")
            return
        except Exception as e:
            last_exc = e
            logger.warning("Database init attempt %d/%d failed: %s", attempt, retries, e)
            await asyncio.sleep(backoff * (2 ** (attempt - 1)))

    logger.error("All %d database init attempts failed. Last error: %s", retries, last_exc)
    if fallback_to_sqlite:
        try:
            sqlite_url = "sqlite+aiosqlite:///bot_fallback.db"
            logger.warning("Falling back to sqlite DB at %s", sqlite_url)
            from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine
            DATABASE_URL = sqlite_url
            engine = _create_async_engine(DATABASE_URL, echo=False, future=True)
            async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            await _create_all_with_timeout(engine)
            try:
                await ensure_columns()
            except Exception as col_exc:
                logger.warning("ensure_columns fallback warning: %s", col_exc)
            logger.info("Fallback sqlite DB initialized.")
            return
        except Exception as e2:
            logger.exception("Fallback to sqlite failed: %s", e2)

    logger.critical("Unable to initialize database and fallback failed — exiting.")
    await asyncio.sleep(0.1)
    sys.exit(1)

# DB helpers
async def get_user(session: AsyncSession, user_id: int) -> Dict:
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        user = User(id=user_id)
        session.add(user)
        await session.commit()
        return await get_user(session, user_id)
    return {c.name: getattr(user, c.name) for c in user.__table__.columns}

async def update_user(session: AsyncSession, user_id: int, **kwargs):
    await session.execute(sa_update(User).where(User.id == user_id).values(**kwargs))
    await session.commit()

async def log_transaction(session: AsyncSession, **data):
    if 'ref' not in data or not data.get('ref'):
        data['ref'] = f"{random.randint(10000,99999)}"
    tx = Transaction(**data)
    session.add(tx)
    await session.commit()
    await session.refresh(tx)
    return tx.id, data['ref']

# Conversation states
INVEST_AMOUNT, INVEST_PROOF, INVEST_CONFIRM, WITHDRAW_AMOUNT, WITHDRAW_WALLET, WITHDRAW_CONFIRM, HISTORY_PAGE, HISTORY_DETAILS = range(8)

# UI helpers and validation
def build_main_menu_keyboard(full_width: bool = MENU_FULL_WIDTH) -> InlineKeyboardMarkup:
    rows = []
    rows.append([InlineKeyboardButton("💰 Balance", callback_data="menu_balance"),
                 InlineKeyboardButton("📈 Invest", callback_data="menu_invest")])
    rows.append([InlineKeyboardButton("🧾 History", callback_data="menu_history"),
                 InlineKeyboardButton("💸 Withdraw", callback_data="menu_withdraw")])
    rows.append([InlineKeyboardButton("👥 Referrals", callback_data="menu_referrals"),
                 InlineKeyboardButton("⚙️ Settings", callback_data="menu_settings")])
    rows.append([InlineKeyboardButton("ℹ️ Information", callback_data="menu_info"),
                 InlineKeyboardButton("❓ Help", url=SUPPORT_URL if SUPPORT_URL else "https://t.me/")])
    rows.append([InlineKeyboardButton("⨉ Exit", callback_data="menu_exit")])
    return InlineKeyboardMarkup(rows)

def build_reply_shortcuts():
    # function kept for backward-compatibility but not used on /start (keyboard removed per request)
    kb = [
        [KeyboardButton("/balance"), KeyboardButton("/invest")],
        [KeyboardButton("/withdraw"), KeyboardButton("/history")],
    ]
    return ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=False)

def is_probable_wallet(address: str) -> bool:
    address = address.strip()
    if address.startswith("0x") and len(address) >= 40 and re.match(r"^0x[0-9a-fA-F]+$", address):
        return True
    if address.startswith("T") and 25 <= len(address) <= 35:
        return True
    if re.match(r"^[13][a-km-zA-HJ-NP-Z1-9]{25,34}$", address):
        return True
    if 20 <= len(address) <= 100:
        return True
    return False

def tx_card_text(tx: Transaction) -> str:
    emoji = "📥" if (tx.type == 'invest') else ("💸" if tx.type == 'withdraw' else "💰")
    created = tx.created_at.strftime("%Y-%m-%d %H:%M:%S") if tx.created_at else "-"
    return (f"{emoji} <b>Ref {tx.ref}</b>\n"
            f"Type: <b>{(tx.type or '').upper()}</b>\n"
            f"Amount: <b>{float(tx.amount):.2f}$</b>\n"
            f"User: <code>{tx.user_id}</code>\n"
            f"Status: <b>{(tx.status or '').upper()}</b>\n"
            f"Created: {created}\n")

def admin_confirm_kb(action: str, tx_db_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes", callback_data=f"admin_confirm_{action}_{tx_db_id}"),
         InlineKeyboardButton("❌ No", callback_data=f"admin_cancel_{tx_db_id}")]
    ])

def admin_action_kb(tx_db_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Approve", callback_data=f"admin_start_approve_{tx_db_id}"),
         InlineKeyboardButton("❌ Reject", callback_data=f"admin_start_reject_{tx_db_id}")]
    ])

# Messaging helpers
async def send_admin_tx_notification(bot, tx: Transaction, proof_file_id: Optional[str] = None):
    text = tx_card_text(tx)
    try:
        if proof_file_id and proof_file_id.startswith("photo:"):
            file_id = proof_file_id.split(":",1)[1]
            await bot.send_photo(chat_id=ADMIN_ID, photo=file_id, caption=text, parse_mode="HTML", reply_markup=admin_action_kb(tx.id))
        else:
            if proof_file_id:
                text = text + f"\nProof: <code>{proof_file_id}</code>"
            await bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML", reply_markup=admin_action_kb(tx.id))
    except Exception:
        logger.exception("Failed to notify admin for transaction %s", tx.id)

async def post_admin_log(bot, message: str):
    if ADMIN_LOG_CHAT_ID:
        try:
            await bot.send_message(chat_id=ADMIN_LOG_CHAT_ID, text=message)
        except Exception:
            logger.exception("Failed to post admin log")

# DAILY PROFIT JOB (ensure defined)
async def daily_profit_job():
    PROFIT_RATE = 0.015  # 1.5% daily
    async with async_session() as session:
        result = await session.execute(select(User))
        users = result.scalars().all()
        for user in users:
            try:
                total = float(user.balance or 0) + float(user.balance_in_process or 0)
                if total <= 0:
                    continue
                profit = round(total * PROFIT_RATE, 2)
                new_balance = float(user.balance or 0) + profit
                new_total_profit = float(user.total_profit or 0) + profit
                await update_user(session, user.id, balance=new_balance, daily_profit=profit, total_profit=new_total_profit)
                await log_transaction(session, user_id=user.id, ref=None, type='profit', amount=profit, status='credited', proof='', wallet='', network='', created_at=datetime.utcnow())
                logger.info("Credited daily profit %.2f to user %s", profit, user.id)
            except Exception:
                logger.exception("daily_profit_job: failed for user %s", getattr(user, "id", "<unknown>"))

# Menu callback handler
async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data if query else None
    if query:
        await query.answer()
    if not data:
        return
    # Let ConversationHandler handle invest/withdraw entrypoints and admin_* callbacks
    if data in ("menu_invest", "menu_withdraw") or data.startswith("admin_"):
        return
    if data == "menu_exit":
        await cancel_conv(update, context)
        try:
            await query.message.edit_text("Main Menu", reply_markup=build_main_menu_keyboard())
        except Exception:
            await query.message.reply_text("Main Menu", reply_markup=build_main_menu_keyboard())
        return
    if data == "menu_balance":
        async with async_session() as session:
            await send_balance_message(query, session, query.from_user.id)
    elif data == "menu_history":
        await history_command(update, context)
    elif data == "menu_referrals":
        # Send a separate message containing the referral link (so it remains in chat)
        # and provide a single "Copy Link" button that inserts the link into the current chat input.
        user_id = query.from_user.id
        bot_username = (await context.bot.get_me()).username
        referral_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
        # Message text with code block of the link so user can copy later
        text = (f"👥 Referrals\n\nShare this link to invite friends and earn rewards:\n\n"
                f"<code>{referral_link}</code>\n\nUse the button below to paste the link into your input field for quick copying/sharing.")
        # Use switch_inline_query_current_chat so Telegram inserts the link into the user's input field when they tap the button.
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 Copy Link", switch_inline_query_current_chat=referral_link)],
            [InlineKeyboardButton("Back to Main Menu", callback_data="menu_exit")]
        ])
        # Send a new message (reply to the menu message) so it stays in chat. Then leave the menu message unchanged; the Back button will edit the menu message back to Main Menu.
        try:
            await query.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await query.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
    elif data == "menu_settings":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Set/Update Withdrawal Wallet", callback_data="settings_set_wallet")],
            [InlineKeyboardButton("Back to Main Menu", callback_data="menu_exit")]
        ])
        await query.edit_message_text("⚙️ Settings\nChoose an action:", reply_markup=kb)
    elif data == "menu_info":
        info_text = ("ℹ️ Information\n\nWelcome to AiCrypto bot.\n- Invest: deposit funds to provided wallet and upload proof (txid or screenshot).\n- Withdraw: request withdrawals; admin will approve and process.")
        await query.edit_message_text(info_text, reply_markup=build_main_menu_keyboard())
    else:
        return

# Balance helper
async def send_balance_message(query_or_message, session: AsyncSession, user_id: int):
    user = await get_user(session, user_id)
    msg = (f"💎 <b>Your Balance</b>\n"
           f"Available: <b>{float(user['balance']):.2f}$</b>\n"
           f"In Process: <b>{float(user['balance_in_process']):.2f}$</b>\n"
           f"Daily Profit: <b>{float(user['daily_profit']):.2f}$</b>\n"
           f"Total Profit: <b>{float(user['total_profit']):.2f}$</b>\n\nManager: {SUPPORT_USER}")
    try:
        await query_or_message.edit_message_text(msg, parse_mode="HTML", reply_markup=build_main_menu_keyboard())
    except Exception:
        await query_or_message.reply_text(msg, parse_mode="HTML", reply_markup=build_main_menu_keyboard())

async def balance_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        await send_balance_message(update.effective_message, session, update.effective_user.id)

# INVEST FLOW
async def invest_cmd_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("📈 Enter the amount you want to invest (numbers only, e.g., 100.50). Send /cancel to abort.", reply_markup=None)
    return INVEST_AMOUNT

async def invest_start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text("📈 Enter the amount you want to invest (numbers only, e.g., 100.50).")
    return INVEST_AMOUNT

async def invest_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    text = (msg.text or "").strip()
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError()
    except Exception:
        await msg.reply_text("Invalid amount. Send a positive number like 100 or 50.50, or /cancel.")
        return INVEST_AMOUNT
    amount = round(amount, 2)
    context.user_data['invest_amount'] = amount
    wallet_msg = (f"📥 Deposit {amount:.2f}$\nSend to wallet:\nWallet: <code>{MASTER_WALLET}</code>\nNetwork: <b>{MASTER_NETWORK}</b>\n\n"
                  "After sending, upload a screenshot OR send the transaction hash (txid).")
    await msg.reply_text(wallet_msg, parse_mode="HTML")
    return INVEST_PROOF

async def invest_proof_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    amount = context.user_data.get('invest_amount')
    if amount is None:
        await msg.reply_text("No pending invest amount. Start again with /invest.")
        return ConversationHandler.END
    proof_label = None
    if msg.photo:
        file_id = msg.photo[-1].file_id
        proof_label = f"photo:{file_id}"
    else:
        text = (msg.text or "").strip()
        if text:
            proof_label = text
    if not proof_label:
        await msg.reply_text("Please upload a screenshot or send the txid, or /cancel.")
        return INVEST_PROOF
    context.user_data['invest_proof'] = proof_label
    await msg.reply_text(
        f"Proof received: <code>{proof_label}</code>\nIf you sent exactly {amount:.2f}$, press confirm. Otherwise Cancel.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ I sent the exact amount", callback_data="invest_confirm_yes"),
                                           InlineKeyboardButton("❌ Cancel", callback_data="invest_confirm_no")]])
    )
    return INVEST_CONFIRM

async def invest_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    if data == "invest_confirm_no":
        await query.message.reply_text("Investment cancelled.", reply_markup=build_main_menu_keyboard())
        context.user_data.pop('invest_amount', None)
        context.user_data.pop('invest_proof', None)
        return ConversationHandler.END
    amount = context.user_data.get('invest_amount')
    proof = context.user_data.get('invest_proof')
    if amount is None or proof is None:
        await query.message.reply_text("Missing data. Restart invest flow.", reply_markup=build_main_menu_keyboard())
        return ConversationHandler.END
    async with async_session() as session:
        tx_db_id, tx_ref = await log_transaction(session, user_id=user_id, ref=None, type='invest', amount=amount, status='pending', proof=str(proof), wallet=MASTER_WALLET, network=MASTER_NETWORK, created_at=datetime.utcnow())
    steps = ("1️⃣ Proof received — processing on blockchain\n"
             "2️⃣ Admin review pending\n"
             "3️⃣ On approval your investment will start")
    await query.message.reply_text(f"✅ Proof received. Reference: <b>{tx_ref}</b>\n\n{steps}", parse_mode="HTML", reply_markup=build_main_menu_keyboard())
    async with async_session() as session:
        result = await session.execute(select(Transaction).where(Transaction.id == tx_db_id))
        tx = result.scalar_one_or_none()
    await send_admin_tx_notification(context.application.bot, tx, proof_file_id=proof)
    await post_admin_log(context.application.bot, f"New INVEST #{tx_db_id} ref {tx_ref} user {user_id} amount {amount:.2f}$")
    context.user_data.pop('invest_amount', None)
    context.user_data.pop('invest_proof', None)
    return ConversationHandler.END

# WITHDRAW FLOW
async def withdraw_cmd_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("💸 Enter the amount you want to withdraw (numbers only). Send /cancel to abort.")
    return WITHDRAW_AMOUNT

async def withdraw_start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text("💸 Enter the amount you want to withdraw (numbers only).")
    return WITHDRAW_AMOUNT

async def withdraw_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    text = (msg.text or "").strip()
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError()
    except Exception:
        await msg.reply_text("Invalid amount. Send a positive number like 50 or 25.75, or /cancel.")
        return WITHDRAW_AMOUNT
    amount = round(amount, 2)
    user_id = update.effective_user.id
    context.user_data['withdraw_amount'] = amount
    async with async_session() as session:
        user = await get_user(session, user_id)
    balance = float(user.get('balance') or 0)
    if amount > balance:
        await msg.reply_text(f"Insufficient balance. Available: {balance:.2f}$. Enter smaller amount or /cancel.")
        context.user_data.pop('withdraw_amount', None)
        return WITHDRAW_AMOUNT
    saved_wallet = user.get('wallet_address')
    saved_network = user.get('wallet_network')
    if saved_wallet:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Use saved wallet", callback_data="withdraw_use_saved")]])
        await msg.reply_text(f"Your saved wallet:\n<code>{saved_wallet}</code>\nNetwork: <b>{saved_network}</b>\n\nOr send a new wallet and optional network.", parse_mode="HTML", reply_markup=kb)
    else:
        await msg.reply_text("No saved wallet. Send wallet address and optional network (e.g., 0xabc... ERC20).")
    return WITHDRAW_WALLET

async def withdraw_wallet_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user_id = update.effective_user.id

    # handle callback (use saved)
    if update.callback_query and update.callback_query.data == "withdraw_use_saved":
        await update.callback_query.answer()
        async with async_session() as session:
            user = await get_user(session, user_id)
        wallet_address = user.get('wallet_address')
        wallet_network = user.get('wallet_network')
        if not wallet_address:
            await msg.reply_text("No saved wallet found. Please send wallet address.")
            return WITHDRAW_WALLET
    else:
        text = (msg.text or "").strip()
        if not text:
            await msg.reply_text("Please send wallet address and optional network.")
            return WITHDRAW_WALLET
        parts = text.split()
        wallet_address = parts[0]
        wallet_network = parts[1] if len(parts) > 1 else ''
        if wallet_address.startswith('/'):
            await msg.reply_text("Looks like a command. Send only the wallet address and optional network.")
            return WITHDRAW_WALLET
        if not is_probable_wallet(wallet_address):
            await msg.reply_text("This address doesn't look valid. Send 'yes' to save anyway or send correct address.")
            context.user_data['pending_wallet_candidate'] = (wallet_address, wallet_network)
            return WITHDRAW_WALLET
        async with async_session() as session:
            await update_user(session, user_id, wallet_address=wallet_address, wallet_network=wallet_network)

    context.user_data['withdraw_wallet'] = wallet_address
    context.user_data['withdraw_network'] = wallet_network
    amount = context.user_data.get('withdraw_amount')
    if amount:
        await msg.reply_text(f"Confirm withdrawal:\nAmount: {amount:.2f}$\nWallet: <code>{wallet_address}</code>\nNetwork: <b>{wallet_network}</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm", callback_data="withdraw_confirm_yes"), InlineKeyboardButton("❌ Cancel", callback_data="withdraw_confirm_no")]]))
        return WITHDRAW_CONFIRM
    else:
        await msg.reply_text(f"✅ Wallet saved:\n<code>{wallet_address}</code>\nNetwork: {wallet_network}", parse_mode="HTML", reply_markup=build_main_menu_keyboard())
        context.user_data.pop('withdraw_wallet', None)
        context.user_data.pop('withdraw_network', None)
        return ConversationHandler.END

async def withdraw_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data != "withdraw_confirm_yes":
        await query.message.reply_text("Withdrawal cancelled.", reply_markup=build_main_menu_keyboard())
        context.user_data.pop('withdraw_amount', None)
        context.user_data.pop('withdraw_wallet', None)
        return ConversationHandler.END
    user_id = query.from_user.id
    amount = context.user_data.get('withdraw_amount')
    wallet = context.user_data.get('withdraw_wallet')
    network = context.user_data.get('withdraw_network', '')
    if amount is None or wallet is None:
        await query.message.reply_text("Missing data. Start withdraw again.", reply_markup=build_main_menu_keyboard())
        return ConversationHandler.END
    async with async_session() as session:
        user = await get_user(session, user_id)
        balance = float(user.get('balance') or 0)
        if amount > balance:
            await query.message.reply_text(f"Insufficient balance ({balance:.2f}$).", reply_markup=build_main_menu_keyboard())
            context.user_data.pop('withdraw_amount', None)
            return ConversationHandler.END
        new_balance = balance - amount
        new_in_process = float(user.get('balance_in_process') or 0) + amount
        await update_user(session, user_id, balance=new_balance, balance_in_process=new_in_process)
        tx_db_id, tx_ref = await log_transaction(session, user_id=user_id, ref=None, type='withdraw', amount=amount, status='pending', proof='', wallet=wallet, network=network, created_at=datetime.utcnow())
    await query.message.reply_text(f"✅ Withdrawal request {tx_ref} submitted and is pending admin approval.", reply_markup=build_main_menu_keyboard())
    async with async_session() as session:
        result = await session.execute(select(Transaction).where(Transaction.id == tx_db_id))
        tx = result.scalar_one_or_none()
    await send_admin_tx_notification(context.application.bot, tx, proof_file_id=None)
    await post_admin_log(context.application.bot, f"New WITHDRAW #{tx_db_id} ref {tx_ref} user {user_id} amount {amount:.2f}$")
    context.user_data.pop('withdraw_amount', None)
    context.user_data.pop('withdraw_wallet', None)
    context.user_data.pop('withdraw_network', None)
    return ConversationHandler.END

# ADMIN flows with confirmation steps
async def admin_start_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split("_")
    if len(parts) < 4:
        await query.message.reply_text("Invalid admin action callback.")
        return
    action = parts[2]
    tx_db_id = int(parts[3])
    await query.message.reply_text(f"Are you sure you want to {action} transaction {tx_db_id}?", reply_markup=admin_confirm_kb(action, tx_db_id))

async def admin_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split("_")
    if len(parts) < 4:
        await query.message.reply_text("Invalid confirmation data.")
        return
    action = parts[2]
    tx_db_id = int(parts[3])
    async with async_session() as session:
        result = await session.execute(select(Transaction).where(Transaction.id == tx_db_id))
        tx = result.scalar_one_or_none()
        if not tx:
            await query.message.reply_text("Transaction not found.")
            return
        if tx.status != 'pending':
            await query.message.reply_text(f"Transaction already processed (status: {tx.status}).")
            return
        if action == 'approve':
            if tx.type == 'invest':
                user = await get_user(session, tx.user_id)
                new_balance = float(user.get('balance') or 0) + float(tx.amount or 0)
                await update_user(session, tx.user_id, balance=new_balance)
                await session.execute(sa_update(Transaction).where(Transaction.id == tx_db_id).values(status='credited'))
                await session.commit()
                await query.message.reply_text(f"Invest #{tx_db_id} credited.")
                try:
                    await context.application.bot.send_message(chat_id=tx.user_id, text=f"✅ Your deposit (ref {tx.ref}) approved. Amount credited: {float(tx.amount):.2f}$.")
                except Exception:
                    logger.exception("Notify user fail invest approve")
                await post_admin_log(context.application.bot, f"Admin approved INVEST #{tx_db_id} ref {tx.ref}")
            elif tx.type == 'withdraw':
                user = await get_user(session, tx.user_id)
                new_in_process = max(0.0, float(user.get('balance_in_process') or 0) - float(tx.amount or 0))
                await update_user(session, tx.user_id, balance_in_process=new_in_process)
                await session.execute(sa_update(Transaction).where(Transaction.id == tx_db_id).values(status='completed'))
                await session.commit()
                await query.message.reply_text(f"Withdraw #{tx_db_id} completed.")
                try:
                    await context.application.bot.send_message(chat_id=tx.user_id, text=f"✅ Your withdrawal (ref {tx.ref}) completed by admin.")
                except Exception:
                    logger.exception("Notify user fail withdraw complete")
                await post_admin_log(context.application.bot, f"Admin approved WITHDRAW #{tx_db_id} ref {tx.ref}")
        else:
            if tx.type == 'invest':
                await session.execute(sa_update(Transaction).where(Transaction.id == tx_db_id).values(status='rejected'))
                await session.commit()
                await query.message.reply_text(f"Invest #{tx_db_id} rejected.")
                try:
                    await context.application.bot.send_message(chat_id=tx.user_id, text=f"❌ Your deposit (ref {tx.ref}) was rejected by admin.")
                except Exception:
                    logger.exception("Notify user invest reject fail")
                await post_admin_log(context.application.bot, f"Admin rejected INVEST #{tx_db_id} ref {tx.ref}")
            elif tx.type == 'withdraw':
                user = await get_user(session, tx.user_id)
                new_in_process = max(0.0, float(user.get('balance_in_process') or 0) - float(tx.amount or 0))
                new_balance = float(user.get('balance') or 0) + float(tx.amount or 0)
                await update_user(session, tx.user_id, balance=new_balance, balance_in_process=new_in_process)
                await session.execute(sa_update(Transaction).where(Transaction.id == tx_db_id).values(status='rejected'))
                await session.commit()
                await query.message.reply_text(f"Withdraw #{tx_db_id} rejected and funds restored.")
                try:
                    await context.application.bot.send_message(chat_id=tx.user_id, text=f"❌ Your withdrawal (ref {tx.ref}) was rejected by admin. Funds restored.")
                except Exception:
                    logger.exception("Notify user withdraw reject fail")
                await post_admin_log(context.application.bot, f"Admin rejected WITHDRAW #{tx_db_id} ref {tx.ref}")
    return

async def admin_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Action cancelled.")

# /pending
def _is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID and ADMIN_ID != 0

async def admin_pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ef_msg = update.effective_message
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        await ef_msg.reply_text("Forbidden: admin only.")
        return
    async with async_session() as session:
        result = await session.execute(select(Transaction).where(Transaction.status == 'pending').order_by(Transaction.created_at.asc()))
        pending: List[Transaction] = result.scalars().all()
    if not pending:
        await ef_msg.reply_text("No pending transactions.")
        return
    for tx in pending:
        proof = tx.proof or ""
        try:
            if proof.startswith("photo:"):
                file_id = proof.split(":",1)[1]
                await context.application.bot.send_photo(chat_id=user_id, photo=file_id, caption=tx_card_text(tx), parse_mode="HTML", reply_markup=admin_action_kb(tx.id))
            else:
                caption = tx_card_text(tx) + (f"\nProof: <code>{proof}</code>" if proof else "")
                await context.application.bot.send_message(chat_id=user_id, text=caption, parse_mode="HTML", reply_markup=admin_action_kb(tx.id))
        except Exception:
            logger.exception("Failed to send pending tx %s to admin", tx.id)

# -------------------------
# HISTORY: buttons show only DATE | AMOUNT | TYPE (one button per tx)
# Details view includes Back to History and Exit to Main Menu
# -------------------------

def history_list_item_text(tx: Transaction) -> str:
    """Return compact single-line text for a history list item: DATE | AMOUNT | TYPE"""
    created = tx.created_at.strftime("%Y-%m-%d") if tx.created_at else "-"
    ttype_raw = (tx.type or "").lower()
    if ttype_raw.startswith("with"):
        ttype = "WITHDRAW"
    elif ttype_raw.startswith("invest") or ttype_raw == "profit" or ttype_raw == "deposit":
        ttype = "DEPOSIT"
    else:
        ttype = (tx.type or "UNKNOWN").upper()
    amount = f"{float(tx.amount):.2f}$" if tx.amount is not None else "-"
    return f"{created}  |  {amount}  |  {ttype}"

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ef_msg = update.effective_message
    user_id = update.effective_user.id
    args = context.args if hasattr(context, "args") else []
    is_admin = _is_admin(user_id)

    # admin 'all' behavior unchanged
    if args and args[0].lower() == "all":
        if not is_admin:
            await ef_msg.reply_text("Forbidden: admin only.")
            return
        limit = 200
        async with async_session() as session:
            result = await session.execute(select(Transaction).order_by(Transaction.created_at.desc()).limit(limit))
            txs: List[Transaction] = result.scalars().all()
        if not txs:
            await ef_msg.reply_text("No transactions found.")
            return
        lines = []
        for tx in txs:
            created = tx.created_at.strftime("%Y-%m-%d %H:%M:%S") if tx.created_at else "-"
            lines.append(f"DB:{tx.id} Ref:{tx.ref} {tx.type.upper()} {float(tx.amount):.2f}$ {tx.status} {created}")
        for i in range(0, len(lines), 50):
            await ef_msg.reply_text("\n".join(lines[i:i+50]))
        return

    # Default: user's transactions (paginated)
    page = 1
    if args and args[0].isdigit():
        page = max(1, int(args[0]))
    per_page = 10
    async with async_session() as session:
        result = await session.execute(select(Transaction).where(Transaction.user_id == user_id).order_by(Transaction.created_at.desc()))
        txs: List[Transaction] = result.scalars().all()
    if not txs:
        await ef_msg.reply_text("🧾 History: no transactions found.")
        return
    total = len(txs)
    total_pages = (total + per_page - 1) // per_page
    page = min(page, total_pages)
    start = (page-1)*per_page
    page_items = txs[start:start+per_page]

    # Build only buttons (each button label = "DATE | AMOUNT | TYPE")
    kb_rows = []
    for tx in page_items:
        # callback_data includes tx.id, page, owner_id so details view can provide Back button
        kb_rows.append([InlineKeyboardButton(history_list_item_text(tx), callback_data=f"history_details_{tx.id}_{page}_{user_id}")])

    # Prev/Next navigation row + Exit (back to main menu)
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅ Prev", callback_data=f"history_page_{page-1}_{user_id}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("Next ➡", callback_data=f"history_page_{page+1}_{user_id}"))
    # Exit to main menu
    nav.append(InlineKeyboardButton("Exit ❌", callback_data="menu_exit"))
    if nav:
        kb_rows.append(nav)

    header = f"🧾 Transactions (page {page}/{total_pages}) — Tap an item for details\n\n"
    # send header + keyboard only (no long textual list)
    await ef_msg.reply_text(header, reply_markup=InlineKeyboardMarkup(kb_rows))

async def history_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split("_")
    if len(parts) < 4:
        await query.message.reply_text("Invalid pagination data.")
        return
    page = int(parts[2])
    uid = int(parts[3])
    if update.effective_user.id != uid and not _is_admin(update.effective_user.id):
        await query.message.reply_text("Forbidden: cannot view other user's history.")
        return
    context.args = [str(page)]
    await history_command(update, context)

async def history_details_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Expects callback_data format: history_details_{tx_id}_{page}_{user_id}
    So the details view can include a Back button returning to that page for that user.
    """
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split("_")
    if len(parts) < 5:
        await query.message.reply_text("Invalid details callback.")
        return
    try:
        tx_db_id = int(parts[2])
        page = int(parts[3])
        owner_id = int(parts[4])
    except Exception:
        await query.message.reply_text("Invalid callback format.")
        return

    # authorize: only owner or admin may view
    if update.effective_user.id != owner_id and not _is_admin(update.effective_user.id):
        await query.message.reply_text("Forbidden: cannot view other user's transaction.")
        return

    async with async_session() as session:
        result = await session.execute(select(Transaction).where(Transaction.id == tx_db_id))
        tx = result.scalar_one_or_none()
    if not tx:
        await query.message.reply_text("Transaction not found.")
        return

    created = tx.created_at.strftime("%Y-%m-%d %H:%M:%S") if tx.created_at else ""
    amount = f"{float(tx.amount):.2f}$" if tx.amount is not None else ""
    tx_type = (tx.type or "").upper()
    status = (tx.status or "").upper()
    ref = tx.ref or ""
    proof = tx.proof or ""
    wallet = tx.wallet or ""
    network = tx.network or "-"

    detail_text = (
        f"📄 <b>Transaction Details</b>\n\n"
        f"Ref: <code>{ref}</code>\n"
        f"Type: <b>{tx_type}</b>\n"
        f"Amount: <b>{amount}</b>\n"
        f"Status: <b>{status}</b>\n"
        f"Date: {created}\n"
        f"Wallet: <code>{wallet}</code>\n"
        f"Network: {network}\n"
    )

    # Build Back button to return to the same page, and Exit to main menu.
    back_cb = f"history_back_{page}_{owner_id}"
    kb = []
    # For admins on pending tx provide action buttons as well
    if _is_admin(query.from_user.id) and tx.status == 'pending':
        kb.append([InlineKeyboardButton("✅ Approve", callback_data=f"admin_start_approve_{tx.id}"),
                   InlineKeyboardButton("❌ Reject", callback_data=f"admin_start_reject_{tx.id}")])
    # Back and Exit row
    kb.append([InlineKeyboardButton("◀ Back to History", callback_data=back_cb),
               InlineKeyboardButton("Exit ❌", callback_data="menu_exit")])

    # If proof is a photo file id (stored as 'photo:<file_id>') show as photo with caption
    if proof and proof.startswith("photo:"):
        file_id = proof.split(":", 1)[1]
        try:
            await context.application.bot.send_photo(chat_id=query.from_user.id, photo=file_id, caption=detail_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
            return
        except Exception:
            pass

    if proof and proof != "-":
        detail_text += f"\nProof: <code>{proof}</code>"

    await query.message.reply_text(detail_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def history_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Callback: history_back_{page}_{user_id} -> displays that page of history.
    """
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split("_")
    if len(parts) < 4:
        await query.message.reply_text("Invalid back callback.")
        return
    page = int(parts[2])
    uid = int(parts[3])
    if update.effective_user.id != uid and not _is_admin(update.effective_user.id):
        await query.message.reply_text("Forbidden: cannot view other user's history.")
        return
    context.args = [str(page)]
    await history_command(update, context)

# Cancel helper
async def cancel_conv(update: Optional[Update], context: ContextTypes.DEFAULT_TYPE):
    if context and getattr(context, "user_data", None):
        context.user_data.clear()
    if update and getattr(update, "callback_query", None):
        await update.callback_query.answer()
    return ConversationHandler.END

# Wallet/history/help commands
async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        await send_balance_message(update.effective_message, session, update.effective_user.id)

async def wallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if args:
        wallet_address = args[0]
        wallet_network = args[1] if len(args) > 1 else ''
        async with async_session() as session:
            await update_user(session, user_id, wallet_address=wallet_address, wallet_network=wallet_network)
        await update.effective_message.reply_text(f"Wallet saved:\n<code>{wallet_address}</code>\nNetwork: {wallet_network}", parse_mode="HTML")
    else:
        async with async_session() as session:
            user = await get_user(session, user_id)
        wallet_address = user.get('wallet_address')
        wallet_network = user.get('wallet_network')
        if wallet_address:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Use this wallet for next withdrawal", callback_data="withdraw_use_saved")]])
            await update.effective_message.reply_text(f"Saved wallet:\n<code>{wallet_address}</code>\nNetwork: {wallet_network}", parse_mode="HTML", reply_markup=kb)
        else:
            await update.effective_message.reply_text("No withdrawal wallet saved. Set it with /wallet <address> [network]")

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await history_command(update, context)

async def information_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("ℹ️ Information\nSee menu for actions.", reply_markup=build_main_menu_keyboard())

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = ("/start - main menu\n/balance\n/invest\n/withdraw\n/wallet\n/history\n/history all (admin)\n/information\n/help")
    await update.effective_message.reply_text(help_text)

# Settings entry
async def settings_start_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text("Send your withdrawal wallet address and optional network (e.g., 0xabc... ERC20).")
    else:
        await update.effective_message.reply_text("Send your withdrawal wallet address and optional network (e.g., 0xabc... ERC20).")
    return WITHDRAW_WALLET

# Start handler — removed reply keyboard so no keyboard appears in chat bottom
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Show main menu inline keyboard only (no reply keyboard)
        await update.effective_message.reply_text("Main Menu", reply_markup=build_main_menu_keyboard())
    except Exception:
        await update.effective_message.reply_text("Main Menu", reply_markup=build_main_menu_keyboard())

# MAIN wiring
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('invest', invest_cmd_handler),
            CommandHandler('withdraw', withdraw_cmd_handler),
            CallbackQueryHandler(invest_start_cmd, pattern='^menu_invest$'),
            CallbackQueryHandler(withdraw_start_cmd, pattern='^menu_withdraw$'),
            CallbackQueryHandler(settings_start_wallet, pattern='^settings_set_wallet$'),
        ],
        states={
            INVEST_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, invest_amount_received)],
            INVEST_PROOF: [MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), invest_proof_received)],
            INVEST_CONFIRM: [CallbackQueryHandler(invest_confirm_callback, pattern='^invest_confirm_yes$'),
                             CallbackQueryHandler(invest_confirm_callback, pattern='^invest_confirm_no$')],
            WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount_received)],
            WITHDRAW_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_wallet_received),
                              CallbackQueryHandler(withdraw_wallet_received, pattern='^withdraw_use_saved$')],
            WITHDRAW_CONFIRM: [CallbackQueryHandler(withdraw_confirm_callback, pattern='^withdraw_confirm_yes$'),
                               CallbackQueryHandler(withdraw_confirm_callback, pattern='^withdraw_confirm_no$')],
        },
        fallbacks=[CommandHandler('cancel', lambda u,c: cancel_conv(u,c))],
        allow_reentry=True,
    )

    app.add_handler(conv_handler)

    # admin handlers
    app.add_handler(CallbackQueryHandler(admin_start_action_callback, pattern='^admin_start_(approve|reject)_\\d+$'))
    app.add_handler(CallbackQueryHandler(admin_confirm_callback, pattern='^admin_confirm_(approve|reject)_\\d+$'))
    app.add_handler(CallbackQueryHandler(admin_cancel_callback, pattern='^admin_cancel_\\d+$'))

    # history callbacks
    app.add_handler(CallbackQueryHandler(history_page_callback, pattern='^history_page_\\d+_\\d+$'))
    # details expects history_details_{tx}_{page}_{owner}
    app.add_handler(CallbackQueryHandler(history_details_callback, pattern='^history_details_\\d+_\\d+_\\d+$'))
    app.add_handler(CallbackQueryHandler(history_back_callback, pattern='^history_back_\\d+_\\d+$'))

    app.add_handler(CallbackQueryHandler(menu_callback))
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("balance", balance_command))
    app.add_handler(CommandHandler("wallet", wallet_command))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("information", information_command))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("pending", admin_pending_command))

    app.add_handler(MessageHandler(filters.Regex("^Balance$"), balance_text_handler))

    # scheduler
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        scheduler = AsyncIOScheduler(event_loop=loop)
    except TypeError:
        scheduler = AsyncIOScheduler()
    scheduler.add_job(daily_profit_job, 'cron', hour=0, minute=0)
    scheduler.start()

    logger.info("AiCrypto Bot STARTED")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    try:
        asyncio.run(init_db(retries=5, backoff=2.0, fallback_to_sqlite=True))
    except Exception as e:
        logger.exception("DB init failed: %s", e)
        raise
    main()
