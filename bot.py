# Full bot.py — httpx-based Binance integration, admin commands, trading simulation, invest/withdraw flows.
# This file includes:
# - Async SQLAlchemy models and DB init with fallback to sqlite
# - Conversation handlers for invest/withdraw flows
# - Admin notification flow with approve/reject and "Commands" button
# - Trading simulation job that fetches Binance prices via httpx.AsyncClient by default with TTL cache and falls back to simulated prices
# - Price formatting to avoid scientific notation (fixed-point)
# - Admin commands: /trade_on, /trade_off, /trade_freq, /trade_now, /trade_status, /use_binance_on, /use_binance_off, /binance_status, /admin_cmds
# - Scheduler with APScheduler to run trading_job and daily_profit_job
#
# Requirements: python-telegram-bot >= 20, sqlalchemy, aiosqlite, apscheduler, httpx, python-dotenv
# Environment variables required: BOT_TOKEN, ADMIN_ID (numeric)
# Optional env vars: ADMIN_LOG_CHAT_ID, DATABASE_URL, MASTER_WALLET, MASTER_NETWORK, SUPPORT_USER, BINANCE_CACHE_TTL
#
# Replace your existing bot.py with this content and restart the bot.

import os
import logging
import random
import re
import asyncio
import sys
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, List
from dotenv import load_dotenv
from decimal import Decimal, ROUND_HALF_UP

import httpx

from decimal import Decimal, ROUND_HALF_UP

import httpx

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
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
    Column, Integer, String, DateTime,
    BigInteger, select, Numeric, text, update as sa_update
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

# === CONFIG ===
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set!")

# ADMIN_ID parsing
try:
    ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
except Exception:
    ADMIN_ID = 0

ADMIN_LOG_CHAT_ID = os.getenv('ADMIN_LOG_CHAT_ID')  # optional admin log chat id
MASTER_WALLET = os.getenv('MASTER_WALLET', 'TAbc...')
MASTER_NETWORK = os.getenv('MASTER_NETWORK', 'TRC20')
SUPPORT_USER = os.getenv('SUPPORT_USER', '@AiCrypto_Support1')
SUPPORT_URL = os.getenv('SUPPORT_URL') or (f"https://t.me/{SUPPORT_USER.lstrip('@')}" if SUPPORT_USER else "https://t.me/")

MENU_FULL_TWO_COLUMN = os.getenv('MENU_FULL_TWO_COLUMN', 'true').lower() in ('1','true','yes','on')
DATABASE_URL = os.getenv('DATABASE_URL')

# Binance settings
USE_BINANCE_BY_DEFAULT = True
BINANCE_CACHE_TTL = int(os.getenv('BINANCE_CACHE_TTL', '10'))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logger.info("Configured ADMIN_ID=%s", ADMIN_ID)
if ADMIN_ID == 0:
    logger.warning("ADMIN_ID not configured or set to 0 — admin-only features may not work.")

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
    balance = Column(Numeric(28, 12), default=0.0)
    balance_in_process = Column(Numeric(28, 12), default=0.0)
    daily_profit = Column(Numeric(28, 12), default=0.0)
    total_profit = Column(Numeric(28, 12), default=0.0)
    referral_count = Column(Integer, default=0)
    referral_earnings = Column(Numeric(28, 12), default=0.0)
    referrer_id = Column(BigInteger, nullable=True)
    wallet_address = Column(String)
    wallet_network = Column(String)
    preferred_language = Column(String, nullable=True)
    joined_at = Column(DateTime, default=datetime.utcnow)


class Transaction(Base):
    __tablename__ = 'transactions'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger)
    ref = Column(String)            # random 5-digit reference
    type = Column(String)           # 'invest' or 'withdraw' or 'profit' or 'trade'
    amount = Column(Numeric(28, 12))
    status = Column(String)         # 'pending','credited','rejected','completed'
    proof = Column(String)          # txid or file_id
    wallet = Column(String)
    network = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

# DB init helpers
async def _create_all_with_timeout(engine_to_use):
    async with engine_to_use.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def ensure_columns():
    async with engine.begin() as conn:
        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS wallet_network VARCHAR"))
            await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS preferred_language VARCHAR"))
            await conn.execute(text("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS proof VARCHAR"))
            await conn.execute(text("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS wallet VARCHAR"))
            await conn.execute(text("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS network VARCHAR"))
            await conn.execute(text("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS ref VARCHAR"))
        except Exception:
            pass

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
    raise SystemExit(1)

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

# -----------------------
# I18N & UI helpers
# -----------------------
TRANSLATIONS = {
    "en": {
        "main_menu_title": "Main Menu",
        "settings_title": "⚙️ Settings",
        "settings_language": "Language",
        "change_language": "Change Language",
        "settings_wallet": "Set/Update Withdrawal Wallet",
        "lang_auto": "Auto (Telegram)",
        "lang_en": "English",
        "lang_fr": "Français",
        "lang_es": "Español",
        "lang_set_success": "Language updated to {lang}.",
        "info_text": "ℹ️ Information\n\nWelcome to AiCrypto bot.\n- Invest: deposit funds to provided wallet and upload proof (txid or screenshot).\n- Withdraw: request withdrawals; admin will approve and process.",
    }
}
DEFAULT_LANG = "en"
SUPPORTED_LANGS = ["en"]
LANG_DISPLAY = {"en":"English"}

def t(lang: str, key: str, **kwargs) -> str:
    bundle = TRANSLATIONS.get(lang, TRANSLATIONS[DEFAULT_LANG])
    txt = bundle.get(key, TRANSLATIONS[DEFAULT_LANG].get(key, key))
    if kwargs:
        return txt.format(**kwargs)
    return txt

async def get_user_language(session: AsyncSession, user_id: int, update: Optional[Update] = None) -> str:
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    preferred = getattr(user, "preferred_language", None) if user else None
    if preferred and preferred != "auto" and preferred in SUPPORTED_LANGS:
        return preferred
    if update and getattr(update, "effective_user", None):
        tlang = (update.effective_user.language_code or "").split("-")[0].lower()
        if tlang in SUPPORTED_LANGS:
            return tlang
    return DEFAULT_LANG

ZWSP = "\u200b"
def _compact_pad(label: str, target: int = 10) -> str:
    plain = label.replace(ZWSP, "")
    if len(plain) >= target: return label
    needed = target - len(plain)
    left = needed//2
    right = needed-left
    return (" "*left) + label + (" "*right) + ZWSP

def build_main_menu_keyboard(full_two_column: bool = MENU_FULL_TWO_COLUMN, lang: str = DEFAULT_LANG) -> InlineKeyboardMarkup:
    labels = {
        "balance": "💰 " + {"en":"Balance"}.get(lang,"Balance"),
        "invest": "📈 " + {"en":"Invest"}.get(lang,"Invest"),
        "history": "🧾 " + {"en":"History"}.get(lang,"History"),
        "withdraw": "💸 " + {"en":"Withdraw"}.get(lang,"Withdraw"),
        "referrals":"👥 " + {"en":"Referrals"}.get(lang,"Referrals"),
        "settings":"⚙️ " + {"en":"Settings"}.get(lang,"Settings"),
        "information":"ℹ️ " + {"en":"Information"}.get(lang,"Information"),
        "help":"❓ " + {"en":"Help"}.get(lang,"Help"),
        "exit":"⨉ " + {"en":"Exit"}.get(lang,"Exit"),
    }
    if not full_two_column:
        rows = [
            [InlineKeyboardButton(labels["balance"], callback_data="menu_balance"), InlineKeyboardButton(labels["invest"], callback_data="menu_invest")],
            [InlineKeyboardButton(labels["history"], callback_data="menu_history"), InlineKeyboardButton(labels["withdraw"], callback_data="menu_withdraw")],
            [InlineKeyboardButton(labels["referrals"], callback_data="menu_referrals"), InlineKeyboardButton(labels["settings"], callback_data="menu_settings")],
            [InlineKeyboardButton(labels["information"], callback_data="menu_info"), InlineKeyboardButton(labels["help"], url=SUPPORT_URL)],
            [InlineKeyboardButton(labels["exit"], callback_data="menu_exit")]
        ]
        return InlineKeyboardMarkup(rows)
    tlen=10
    left_right = [
        (labels["balance"], "menu_balance", labels["invest"], "menu_invest"),
        (labels["history"], "menu_history", labels["withdraw"], "menu_withdraw"),
        (labels["referrals"], "menu_referrals", labels["settings"], "menu_settings"),
        (labels["information"], "menu_info", labels["help"], "menu_help_url"),
    ]
    rows=[]
    for l_label,l_cb,r_label,r_cb in left_right:
        l=_compact_pad(l_label,target=tlen); r=_compact_pad(r_label,target=tlen)
        left_btn = InlineKeyboardButton(l, callback_data=l_cb)
        if r_cb=="menu_help_url":
            right_btn = InlineKeyboardButton(r, url=SUPPORT_URL)
        else:
            right_btn = InlineKeyboardButton(r, callback_data=r_cb)
        rows.append([left_btn,right_btn])
    exit_label=_compact_pad(labels["exit"], target=(tlen*2)//2)
    rows.append([InlineKeyboardButton(exit_label, callback_data="menu_exit")])
    return InlineKeyboardMarkup(rows)

def is_probable_wallet(address: str) -> bool:
    address = (address or "").strip()
    if not address: return False
    if address.startswith("0x") and len(address)>=40 and re.match(r"^0x[0-9a-fA-F]+$", address): return True
    if address.startswith("T") and 25<=len(address)<=35: return True
    if re.match(r"^[13][a-km-zA-HJ-NP-Z1-9]{25,34}$", address): return True
    if 20<=len(address)<=100: return True
    return False

# -----------------------
# Admin / tx helpers
# -----------------------

async def send_admin_notification(context: ContextTypes.DEFAULT_TYPE, message: str):
    """Send notification to admin with error handling."""
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=message, parse_mode='Markdown')
        logger.info("Admin notification sent successfully")
    except Exception as e:
        logger.error(f"Failed to send admin notification: {e}", exc_info=True)

# -----------------------
# Admin Commands
# -----------------------

async def admin_pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /pending command - show all pending transactions with detailed logging and robust error handling.
    Includes detailed logging for invocation, permission checks, DB results, and send success/failure.
    Per-transaction error handling with stack traces and admin notifications for failed sends.
    """
    # Log invocation
    user_id = update.effective_user.id if update.effective_user else None
    logger.info(f"admin_pending_command invoked by user_id={user_id}")
    
    # Permission check with logging
    if not ADMIN_ID or user_id != ADMIN_ID:
        logger.warning(f"Unauthorized access attempt to /pending by user_id={user_id}, ADMIN_ID={ADMIN_ID}")
        try:
            await update.message.reply_text("❌ Admin only.")
            logger.info(f"Sent unauthorized access message to user_id={user_id}")
        except Exception as e:
            logger.error(f"Failed to send unauthorized message to user_id={user_id}: {e}", exc_info=True)
        return
    
    logger.info(f"Permission check passed for user_id={user_id}")
    
    # Query pending transactions with logging
    try:
        async with async_session() as session:
            logger.info("Querying database for pending transactions")
            result = await session.execute(
                select(Transaction).where(Transaction.status == 'pending').order_by(Transaction.created_at.desc())
            )
            pending_txs = result.scalars().all()
            logger.info(f"Database query successful: found {len(pending_txs)} pending transaction(s)")
    except Exception as e:
        logger.error(f"Database query failed for pending transactions: {e}", exc_info=True)
        try:
            await update.message.reply_text(f"❌ Database error: {str(e)}")
            logger.info("Sent database error message to admin")
        except Exception as send_err:
            logger.error(f"Failed to send database error message: {send_err}", exc_info=True)
        return
    
    # Handle no pending transactions
    if not pending_txs:
        logger.info("No pending transactions found")
        try:
            await update.message.reply_text("✅ No pending transactions.")
            logger.info("Sent 'no pending transactions' message successfully")
        except Exception as e:
            logger.error(f"Failed to send 'no pending transactions' message: {e}", exc_info=True)
        return
    
    # Process and send each transaction with per-transaction error handling
    logger.info(f"Processing {len(pending_txs)} pending transaction(s)")
    success_count = 0
    failure_count = 0
    
    for idx, tx in enumerate(pending_txs, 1):
        try:
            logger.info(f"Processing transaction {idx}/{len(pending_txs)}: tx_id={tx.id}, user_id={tx.user_id}, type={tx.type}, amount={tx.amount}, ref={tx.ref}")
            
            # Build transaction message
            msg_parts = [
                f"📋 *Pending Transaction #{tx.id}*",
                f"👤 User: `{tx.user_id}`",
                f"🔖 Ref: `{tx.ref or 'N/A'}`",
                f"📌 Type: {tx.type}",
                f"💰 Amount: {tx.amount}",
                f"📅 Created: {tx.created_at.strftime('%Y-%m-%d %H:%M:%S') if tx.created_at else 'N/A'}",
            ]
            
            if tx.wallet:
                msg_parts.append(f"💼 Wallet: `{tx.wallet}`")
            if tx.network:
                msg_parts.append(f"🌐 Network: {tx.network}")
            if tx.proof:
                msg_parts.append(f"🔗 Proof: {tx.proof}")
            
            msg = "\n".join(msg_parts)
            
            # Send transaction details with robust error handling
            try:
                await update.message.reply_text(msg, parse_mode='Markdown')
                success_count += 1
                logger.info(f"Successfully sent transaction {idx}/{len(pending_txs)} (tx_id={tx.id}) to admin")
            except Exception as send_err:
                failure_count += 1
                logger.error(
                    f"Failed to send transaction {idx}/{len(pending_txs)} (tx_id={tx.id}) to admin: {send_err}",
                    exc_info=True
                )
                # Notify admin about the failed send via a fallback message
                try:
                    fallback_msg = f"⚠️ Failed to send transaction #{tx.id} details. Error: {str(send_err)}"
                    await update.message.reply_text(fallback_msg)
                    logger.info(f"Sent fallback error notification for tx_id={tx.id}")
                except Exception as fallback_err:
                    logger.error(f"Failed to send fallback error notification for tx_id={tx.id}: {fallback_err}", exc_info=True)
        
        except Exception as process_err:
            failure_count += 1
            logger.error(
                f"Failed to process transaction {idx}/{len(pending_txs)} (tx_id={tx.id}): {process_err}",
                exc_info=True
            )
            # Notify admin about the processing error
            try:
                error_msg = f"⚠️ Error processing transaction #{tx.id}: {str(process_err)}"
                await update.message.reply_text(error_msg)
                logger.info(f"Sent processing error notification for tx_id={tx.id}")
            except Exception as notify_err:
                logger.error(f"Failed to send processing error notification for tx_id={tx.id}: {notify_err}", exc_info=True)
    
    # Send summary
    summary_msg = f"✅ Pending transactions processed: {success_count} successful, {failure_count} failed"
    logger.info(summary_msg)
    try:
        await update.message.reply_text(summary_msg)
        logger.info("Sent summary message successfully")
    except Exception as e:
        logger.error(f"Failed to send summary message: {e}", exc_info=True)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    user = update.effective_user
    async with async_session() as session:
        await get_user(session, user.id)
        lang = await get_user_language(session, user.id, update)
    
    welcome_text = f"👋 Welcome to AiCrypto Bot!\n\nUse the menu below to get started."
    keyboard = build_main_menu_keyboard(lang=lang)
    await update.message.reply_text(welcome_text, reply_markup=keyboard)

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /menu command."""
    async with async_session() as session:
        lang = await get_user_language(session, update.effective_user.id, update)
    keyboard = build_main_menu_keyboard(lang=lang)
    await update.message.reply_text(t(lang, "main_menu_title"), reply_markup=keyboard)

async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /info command."""
    async with async_session() as session:
        lang = await get_user_language(session, update.effective_user.id, update)
    await update.message.reply_text(t(lang, "info_text"))

# -----------------------
# Main
# -----------------------

async def post_init(application: Application):
    """Initialize database after application is created."""
    await init_db()
    logger.info("Bot initialized successfully")

def main():
    """Start the bot."""
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    # Register command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("info", info_command))
    application.add_handler(CommandHandler("pending", admin_pending_command))
    
    logger.info("Starting bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
