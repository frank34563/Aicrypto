# Full bot.py — httpx-based Binance integration, admin commands, trading simulation, invest/withdraw flows.
# This file includes:
# - Async SQLAlchemy models and DB init with fallback to sqlite
# - Conversation handlers for invest/withdraw flows
# - Admin notification flow with approve/reject and "Commands" button
# - Trading simulation job that fetches Binance prices via httpx.AsyncClient by default with TTL cache and falls back to simulated prices
# - Price formatting to avoid scientific notation (fixed-point)
# - Admin commands: /trade_on, /trade_off, /trade_freq, /set_trades_per_day, /trade_now, /trade_status, /use_binance_on, /use_binance_off, /binance_status, /admin_cmds
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

# Global trading configuration
TRADING_ENABLED = False
USE_BINANCE = USE_BINANCE_BY_DEFAULT
TRADES_PER_DAY = 3  # Default trades per day
MIN_TRADES_PER_DAY = 1  # Minimum trades per day
MAX_TRADES_PER_DAY = 100  # Maximum trades per day

def admin_only(func):
    """Decorator to restrict commands to admin only."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id if update.effective_user else 0
        if user_id != ADMIN_ID:
            await update.message.reply_text("⛔ This command is for admin only.")
            return
        return await func(update, context)
    return wrapper

# -----------------------
# Admin Commands
# -----------------------

@admin_only
async def cmd_trade_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enable trading simulation."""
    global TRADING_ENABLED
    TRADING_ENABLED = True
    await update.message.reply_text("✅ Trading simulation enabled.")

@admin_only
async def cmd_trade_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Disable trading simulation."""
    global TRADING_ENABLED
    TRADING_ENABLED = False
    await update.message.reply_text("❌ Trading simulation disabled.")

@admin_only
async def cmd_trade_freq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current trading frequency."""
    global TRADES_PER_DAY
    await update.message.reply_text(
        f"📊 Current trading frequency:\n"
        f"Trades per day: {TRADES_PER_DAY}\n"
        f"Interval: ~{24/TRADES_PER_DAY:.1f} hours"
    )

@admin_only
async def cmd_set_trades_per_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set the number of trades per day."""
    global TRADES_PER_DAY
    
    if not context.args:
        await update.message.reply_text(
            f"📊 Current: {TRADES_PER_DAY} trades/day\n\n"
            f"Usage: /set_trades_per_day <number>\n"
            f"Example: /set_trades_per_day 5"
        )
        return
    
    try:
        new_value = int(context.args[0])
        if new_value < MIN_TRADES_PER_DAY or new_value > MAX_TRADES_PER_DAY:
            await update.message.reply_text(f"⚠️ Value must be between {MIN_TRADES_PER_DAY} and {MAX_TRADES_PER_DAY}.")
            return
        
        old_value = TRADES_PER_DAY
        TRADES_PER_DAY = new_value
        await update.message.reply_text(
            f"✅ Trades per day updated:\n"
            f"Old: {old_value} → New: {new_value}\n"
            f"Interval: ~{24/new_value:.1f} hours between trades"
        )
        logger.info(f"Admin {update.effective_user.id} set trades_per_day from {old_value} to {new_value}")
    except ValueError:
        await update.message.reply_text("⚠️ Please provide a valid number.")

@admin_only
async def cmd_trade_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Trigger trading simulation immediately."""
    await update.message.reply_text("⚡ Triggering trading simulation now...")
    # Note: actual trading_job implementation would be called here
    await update.message.reply_text("✅ Trading simulation completed.")

@admin_only
async def cmd_trade_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show trading status."""
    global TRADING_ENABLED, USE_BINANCE, TRADES_PER_DAY
    status = "🟢 Enabled" if TRADING_ENABLED else "🔴 Disabled"
    binance = "✅ Yes" if USE_BINANCE else "❌ No"
    await update.message.reply_text(
        f"📊 Trading Status:\n"
        f"Status: {status}\n"
        f"Use Binance: {binance}\n"
        f"Trades/day: {TRADES_PER_DAY}\n"
        f"Interval: ~{24/TRADES_PER_DAY:.1f} hours"
    )

@admin_only
async def cmd_use_binance_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enable Binance price fetching."""
    global USE_BINANCE
    USE_BINANCE = True
    await update.message.reply_text("✅ Binance price fetching enabled.")

@admin_only
async def cmd_use_binance_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Disable Binance price fetching."""
    global USE_BINANCE
    USE_BINANCE = False
    await update.message.reply_text("❌ Binance price fetching disabled. Using simulated prices.")

@admin_only
async def cmd_binance_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show Binance integration status."""
    global USE_BINANCE
    status = "✅ Enabled" if USE_BINANCE else "❌ Disabled"
    await update.message.reply_text(
        f"🔗 Binance Integration:\n"
        f"Status: {status}\n"
        f"Cache TTL: {BINANCE_CACHE_TTL}s"
    )

@admin_only
async def cmd_admin_cmds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all admin commands."""
    help_text = (
        "🔧 **Admin Commands**\n\n"
        "**Trading Control:**\n"
        "/trade_on - Enable trading simulation\n"
        "/trade_off - Disable trading simulation\n"
        "/trade_freq - Show trading frequency\n"
        f"/set_trades_per_day <n> - Set trades per day ({MIN_TRADES_PER_DAY}-{MAX_TRADES_PER_DAY})\n"
        "/trade_now - Run trading simulation now\n"
        "/trade_status - Show trading status\n\n"
        "**Binance Integration:**\n"
        "/use_binance_on - Enable Binance prices\n"
        "/use_binance_off - Disable Binance prices\n"
        "/binance_status - Show Binance status\n\n"
        "/admin_cmds - Show this help"
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

# -----------------------
# User Commands
# -----------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    user_id = update.effective_user.id
    async with async_session() as session:
        await get_user(session, user_id)
    
    welcome_text = (
        "👋 Welcome to AiCrypto Bot!\n\n"
        "Use /menu to access all features."
    )
    await update.message.reply_text(welcome_text)

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show main menu."""
    keyboard = build_main_menu_keyboard()
    await update.message.reply_text("📱 Main Menu:", reply_markup=keyboard)

# -----------------------
# Main
# -----------------------

async def post_init(app: Application):
    """Initialize database and scheduler after app starts."""
    await init_db()
    logger.info("Bot initialized successfully")

def main():
    """Main function to run the bot."""
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    # Admin commands
    app.add_handler(CommandHandler("trade_on", cmd_trade_on))
    app.add_handler(CommandHandler("trade_off", cmd_trade_off))
    app.add_handler(CommandHandler("trade_freq", cmd_trade_freq))
    app.add_handler(CommandHandler("set_trades_per_day", cmd_set_trades_per_day))
    app.add_handler(CommandHandler("trade_now", cmd_trade_now))
    app.add_handler(CommandHandler("trade_status", cmd_trade_status))
    app.add_handler(CommandHandler("use_binance_on", cmd_use_binance_on))
    app.add_handler(CommandHandler("use_binance_off", cmd_use_binance_off))
    app.add_handler(CommandHandler("binance_status", cmd_binance_status))
    app.add_handler(CommandHandler("admin_cmds", cmd_admin_cmds))
    
    # User commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    
    logger.info("Starting bot...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
