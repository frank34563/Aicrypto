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
# Global trading configuration (stored in context.bot_data)
# -----------------------
TRADING_DEFAULTS = {
    'daily_payout_percent': 1.5,      # Default daily profit percentage
    'trades_per_day': 10,              # Default number of trades per day
    'percent_per_trade': 0.15,         # Default percentage per trade (1.5% / 10 = 0.15%)
    'trading_enabled': False,          # Trading on/off
    'use_binance': USE_BINANCE_BY_DEFAULT,  # Use real Binance prices
}

# Price cache with TTL
price_cache: Dict[str, tuple] = {}  # {symbol: (price, timestamp)}

# -----------------------
# Binance price fetching with httpx
# -----------------------
async def fetch_binance_price(symbol: str) -> Optional[float]:
    """Fetch real-time price from Binance API using httpx."""
    try:
        now = datetime.now(timezone.utc)
        if symbol in price_cache:
            price, timestamp = price_cache[symbol]
            if (now - timestamp).total_seconds() < BINANCE_CACHE_TTL:
                return price
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
            price = float(data['price'])
            price_cache[symbol] = (price, now)
            return price
    except Exception as e:
        logger.warning("Failed to fetch Binance price for %s: %s", symbol, e)
        return None

def generate_simulated_price(symbol: str, base_price: float) -> float:
    """Generate a simulated price with small random variation."""
    variation = random.uniform(-0.02, 0.02)  # ±2% variation
    return base_price * (1 + variation)

# Simulated base prices for common pairs
SIMULATED_PRICES = {
    'BTCUSDT': 45000.0,
    'ETHUSDT': 2500.0,
    'BNBUSDT': 300.0,
    'XRPUSDT': 0.5,
    'ADAUSDT': 0.4,
    'SOLUSDT': 100.0,
    'DOGEUSDT': 0.08,
    'DOTUSDT': 6.0,
    'MATICUSDT': 0.8,
    'LTCUSDT': 70.0,
}

async def get_crypto_price(symbol: str, use_binance: bool = True) -> float:
    """Get crypto price from Binance or simulated."""
    if use_binance:
        price = await fetch_binance_price(symbol)
        if price is not None:
            return price
        logger.info("Binance fetch failed for %s, falling back to simulation", symbol)
    
    base_price = SIMULATED_PRICES.get(symbol, 100.0)
    return generate_simulated_price(symbol, base_price)

# -----------------------
# Admin / tx helpers
# -----------------------
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

async def notify_admin(context: ContextTypes.DEFAULT_TYPE, message: str, reply_markup=None):
    """Send notification to admin."""
    if not ADMIN_ID or ADMIN_ID == 0:
        return
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=message, reply_markup=reply_markup, parse_mode='HTML')
    except Exception as e:
        logger.error("Failed to notify admin: %s", e)

def format_price(price: float, decimals: int = 8) -> str:
    """Format price to avoid scientific notation."""
    d = Decimal(str(price))
    quantize_str = '0.' + '0' * decimals
    formatted = d.quantize(Decimal(quantize_str), rounding=ROUND_HALF_UP)
    return f"{formatted:.{decimals}f}".rstrip('0').rstrip('.')

# -----------------------
# Trading simulation job
# -----------------------
async def execute_single_trade(session: AsyncSession, user_id: int, balance: Decimal, 
                               trade_percent: float, crypto_pairs: List[str], use_binance: bool) -> Decimal:
    """Execute a single simulated trade for a user."""
    if balance <= 0:
        return Decimal('0')
    
    # Select random crypto pair
    pair = random.choice(crypto_pairs) if crypto_pairs else 'BTCUSDT'
    
    # Get price
    price = await get_crypto_price(pair, use_binance)
    
    # Calculate profit/loss (random outcome with profit bias)
    outcome = random.uniform(-0.3, 1.0)  # Range gives ~77% profit probability
    profit_multiplier = Decimal(str(outcome * trade_percent / 100))
    trade_profit = balance * profit_multiplier
    
    # Log trade transaction
    await log_transaction(
        session,
        user_id=user_id,
        type='trade',
        amount=float(trade_profit),
        status='completed',
        proof=f"{pair}@{format_price(price)}",
        wallet=None,
        network=None
    )
    
    return trade_profit

async def trading_job(context: ContextTypes.DEFAULT_TYPE):
    """Execute trading simulation for all users."""
    try:
        config = context.bot_data.get('trading_config', TRADING_DEFAULTS.copy())
        
        if not config.get('trading_enabled', False):
            logger.info("Trading is disabled, skipping job")
            return
        
        trades_per_day = config.get('trades_per_day', 10)
        percent_per_trade = config.get('percent_per_trade', 0.15)
        use_binance = config.get('use_binance', True)
        
        async with async_session() as session:
            # Get all users with balance
            result = await session.execute(
                select(User).where(User.balance > 0)
            )
            users = result.scalars().all()
            
            for user in users:
                # Check if user has custom settings
                user_config = context.bot_data.get(f'user_config_{user.id}', {})
                user_pairs = user_config.get('crypto_pairs', ['BTCUSDT', 'ETHUSDT', 'BNBUSDT'])
                user_percent = user_config.get('percent_per_trade', percent_per_trade)
                
                # Execute trade
                trade_profit = await execute_single_trade(
                    session, user.id, user.balance, user_percent, user_pairs, use_binance
                )
                
                # Update user balance and daily profit
                new_balance = user.balance + trade_profit
                new_daily_profit = user.daily_profit + trade_profit
                new_total_profit = user.total_profit + trade_profit
                
                await update_user(
                    session,
                    user.id,
                    balance=float(new_balance),
                    daily_profit=float(new_daily_profit),
                    total_profit=float(new_total_profit)
                )
                
                logger.info("Trade executed for user %d: profit=%s, new_balance=%s", 
                           user.id, trade_profit, new_balance)
        
        logger.info("Trading job completed successfully")
    except Exception as e:
        logger.exception("Error in trading job: %s", e)

async def daily_profit_job(context: ContextTypes.DEFAULT_TYPE):
    """Reset daily profit at end of day and send summaries."""
    try:
        async with async_session() as session:
            # Get all users
            result = await session.execute(select(User))
            users = result.scalars().all()
            
            for user in users:
                if user.daily_profit != 0:
                    # Send end-of-day summary to user
                    try:
                        profit_percent = (float(user.daily_profit) / float(user.balance) * 100) if user.balance > 0 else 0
                        summary = (
                            f"📊 <b>End of Day Trading Summary</b>\n\n"
                            f"💰 Daily Profit: ${format_price(float(user.daily_profit), 2)}\n"
                            f"📈 Profit Percent: {format_price(profit_percent, 2)}%\n"
                            f"💵 Total Balance: ${format_price(float(user.balance), 2)}\n"
                            f"🎯 Total All-Time Profit: ${format_price(float(user.total_profit), 2)}"
                        )
                        await context.bot.send_message(chat_id=user.id, text=summary, parse_mode='HTML')
                    except Exception as e:
                        logger.warning("Failed to send summary to user %d: %s", user.id, e)
                    
                    # Reset daily profit
                    await update_user(session, user.id, daily_profit=0.0)
        
        logger.info("Daily profit reset completed")
    except Exception as e:
        logger.exception("Error in daily profit job: %s", e)

# -----------------------
# Command handlers
# -----------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    user = update.effective_user
    async with async_session() as session:
        await get_user(session, user.id)
        lang = await get_user_language(session, user.id, update)
    
    welcome = (
        f"👋 Welcome to <b>AiCrypto Trading Bot</b>!\n\n"
        f"🤖 I help you trade cryptocurrencies automatically.\n\n"
        f"Use /menu to access all features."
    )
    await update.message.reply_html(welcome)

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show main menu."""
    async with async_session() as session:
        lang = await get_user_language(session, update.effective_user.id, update)
    
    keyboard = build_main_menu_keyboard(lang=lang)
    text = t(lang, "main_menu_title")
    
    if update.message:
        await update.message.reply_text(text, reply_markup=keyboard)
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)

async def balance_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show balance information."""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    async with async_session() as session:
        user_data = await get_user(session, user_id)
    
    text = (
        f"💰 <b>Your Balance</b>\n\n"
        f"Available: ${format_price(float(user_data['balance']), 2)}\n"
        f"In Process: ${format_price(float(user_data['balance_in_process']), 2)}\n"
        f"Today's Profit: ${format_price(float(user_data['daily_profit']), 2)}\n"
        f"Total Profit: ${format_price(float(user_data['total_profit']), 2)}"
    )
    
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="menu_back")]])
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')

async def referrals_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show referral information."""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    async with async_session() as session:
        user_data = await get_user(session, user_id)
    
    ref_link = f"https://t.me/{context.bot.username}?start={user_id}"
    text = (
        f"👥 <b>Referral Program</b>\n\n"
        f"Your referrals: {user_data['referral_count']}\n"
        f"Earnings: ${format_price(float(user_data['referral_earnings']), 2)}\n\n"
        f"🔗 Your link:\n<code>{ref_link}</code>"
    )
    
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="menu_back")]])
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')

async def info_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show information."""
    query = update.callback_query
    await query.answer()
    
    async with async_session() as session:
        lang = await get_user_language(session, query.from_user.id, update)
    
    text = t(lang, "info_text")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="menu_back")]])
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')

async def menu_exit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exit menu."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("👋 Menu closed. Use /menu to open again.")

async def invest_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show invest instructions."""
    query = update.callback_query
    await query.answer()
    
    text = (
        f"📈 <b>Investment Instructions</b>\n\n"
        f"1. Send USDT to the wallet below\n"
        f"2. Send us the transaction ID (TXID)\n"
        f"3. Wait for admin approval\n\n"
        f"💳 <b>Wallet Address:</b>\n"
        f"<code>{MASTER_WALLET}</code>\n\n"
        f"🌐 <b>Network:</b> {MASTER_NETWORK}\n\n"
        f"📝 After sending, use /invest command to submit your proof."
    )
    
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="menu_back")]])
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')

async def withdraw_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show withdraw instructions."""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    async with async_session() as session:
        user_data = await get_user(session, user_id)
    
    text = (
        f"💸 <b>Withdrawal</b>\n\n"
        f"Available Balance: ${format_price(float(user_data['balance']), 2)}\n\n"
        f"To withdraw, use the /withdraw command.\n"
        f"Admin will review and process your request."
    )
    
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="menu_back")]])
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')

async def history_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show transaction history."""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    async with async_session() as session:
        result = await session.execute(
            select(Transaction).where(Transaction.user_id == user_id).order_by(Transaction.created_at.desc()).limit(10)
        )
        transactions = result.scalars().all()
    
    if not transactions:
        text = "🧾 <b>Transaction History</b>\n\nNo transactions yet."
    else:
        text = "🧾 <b>Transaction History</b>\n\n"
        for tx in transactions:
            status_emoji = {"pending": "⏳", "completed": "✅", "credited": "✅", "rejected": "❌"}.get(tx.status, "❓")
            text += f"{status_emoji} {tx.type.upper()} - ${format_price(float(tx.amount), 2)} ({tx.status})\n"
    
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="menu_back")]])
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')

async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show settings menu."""
    query = update.callback_query
    await query.answer()
    
    async with async_session() as session:
        lang = await get_user_language(session, query.from_user.id, update)
    
    text = t(lang, "settings_title") + "\n\n" + t(lang, "settings_wallet")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang, "change_language"), callback_data="settings_language")],
        [InlineKeyboardButton("« Back", callback_data="menu_back")]
    ])
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle menu callbacks."""
    query = update.callback_query
    data = query.data
    
    if data == "menu_back":
        await menu(update, context)
    elif data == "menu_balance":
        await balance_menu(update, context)
    elif data == "menu_invest":
        await invest_menu(update, context)
    elif data == "menu_withdraw":
        await withdraw_menu(update, context)
    elif data == "menu_history":
        await history_menu(update, context)
    elif data == "menu_settings":
        await settings_menu(update, context)
    elif data == "menu_referrals":
        await referrals_menu(update, context)
    elif data == "menu_info":
        await info_menu(update, context)
    elif data == "menu_exit":
        await menu_exit(update, context)
    elif data == "settings_language":
        await query.answer("Language settings coming soon!")
        await settings_menu(update, context)

# -----------------------
# Admin commands
# -----------------------
async def admin_set_daily_payout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to set daily payout percentage."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    
    if not context.args or len(context.args) != 1:
        await update.message.reply_text("Usage: /set_daily_payout <percentage>\nExample: /set_daily_payout 2.0")
        return
    
    try:
        payout = float(context.args[0])
        if payout < 0 or payout > 100:
            await update.message.reply_text("❌ Percentage must be between 0 and 100")
            return
        
        config = context.bot_data.get('trading_config', TRADING_DEFAULTS.copy())
        config['daily_payout_percent'] = payout
        context.bot_data['trading_config'] = config
        
        await update.message.reply_text(f"✅ Daily payout set to {payout}%")
    except ValueError:
        await update.message.reply_text("❌ Invalid number")

async def admin_set_trades_per_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to set number of trades per day."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    
    if not context.args or len(context.args) != 1:
        await update.message.reply_text("Usage: /set_trades_per_day <number>\nExample: /set_trades_per_day 20")
        return
    
    try:
        trades = int(context.args[0])
        if trades < 1 or trades > 1000:
            await update.message.reply_text("❌ Number must be between 1 and 1000")
            return
        
        config = context.bot_data.get('trading_config', TRADING_DEFAULTS.copy())
        config['trades_per_day'] = trades
        # Recalculate percent per trade
        daily_payout = config.get('daily_payout_percent', 1.5)
        config['percent_per_trade'] = daily_payout / trades
        context.bot_data['trading_config'] = config
        
        await update.message.reply_text(
            f"✅ Trades per day set to {trades}\n"
            f"📊 Percent per trade: {config['percent_per_trade']:.4f}%"
        )
    except ValueError:
        await update.message.reply_text("❌ Invalid number")

async def admin_set_percent_per_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to set percentage per trade globally."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    
    if not context.args or len(context.args) != 1:
        await update.message.reply_text("Usage: /set_percent_per_trade <percentage>\nExample: /set_percent_per_trade 0.25")
        return
    
    try:
        percent = float(context.args[0])
        if percent < 0 or percent > 10:
            await update.message.reply_text("❌ Percentage must be between 0 and 10")
            return
        
        config = context.bot_data.get('trading_config', TRADING_DEFAULTS.copy())
        config['percent_per_trade'] = percent
        context.bot_data['trading_config'] = config
        
        await update.message.reply_text(f"✅ Percent per trade set to {percent}%")
    except ValueError:
        await update.message.reply_text("❌ Invalid number")

async def admin_assign_user_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to assign crypto pairs to a user."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /assign_pairs <user_id> <pair1> <pair2> ...\n"
            "Example: /assign_pairs 12345 BTCUSDT ETHUSDT BNBUSDT"
        )
        return
    
    try:
        user_id = int(context.args[0])
        pairs = [p.upper() for p in context.args[1:]]
        
        user_config = context.bot_data.get(f'user_config_{user_id}', {})
        user_config['crypto_pairs'] = pairs
        context.bot_data[f'user_config_{user_id}'] = user_config
        
        await update.message.reply_text(
            f"✅ Assigned pairs to user {user_id}:\n" + 
            "\n".join(f"• {pair}" for pair in pairs)
        )
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID")

async def admin_assign_user_percent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to assign per-trade percentage to a user."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    
    if not context.args or len(context.args) != 2:
        await update.message.reply_text(
            "Usage: /assign_percent <user_id> <percentage>\n"
            "Example: /assign_percent 12345 0.25"
        )
        return
    
    try:
        user_id = int(context.args[0])
        percent = float(context.args[1])
        
        if percent < 0 or percent > 10:
            await update.message.reply_text("❌ Percentage must be between 0 and 10")
            return
        
        user_config = context.bot_data.get(f'user_config_{user_id}', {})
        user_config['percent_per_trade'] = percent
        context.bot_data[f'user_config_{user_id}'] = user_config
        
        await update.message.reply_text(f"✅ User {user_id} percent per trade set to {percent}%")
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID or percentage")

async def admin_trading_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to show trading day statistics."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    
    async with async_session() as session:
        # Get all users with activity
        result = await session.execute(select(User))
        users = result.scalars().all()
        
        total_users = len(users)
        active_users = sum(1 for u in users if u.balance > 0)
        total_balance = sum(float(u.balance) for u in users)
        total_daily_profit = sum(float(u.daily_profit) for u in users)
        total_all_time_profit = sum(float(u.total_profit) for u in users)
        
        # Get today's trade count
        today = datetime.now(timezone.utc).date()
        trade_result = await session.execute(
            select(Transaction).where(
                Transaction.type == 'trade',
                Transaction.created_at >= datetime.combine(today, datetime.min.time())
            )
        )
        trades_today = len(trade_result.scalars().all())
        
        config = context.bot_data.get('trading_config', TRADING_DEFAULTS.copy())
        
        summary = (
            f"📊 <b>Trading Day Summary</b>\n\n"
            f"👥 Total Users: {total_users}\n"
            f"✅ Active Traders: {active_users}\n"
            f"💰 Total Balance: ${format_price(total_balance, 2)}\n"
            f"📈 Today's Profit: ${format_price(total_daily_profit, 2)}\n"
            f"🎯 All-Time Profit: ${format_price(total_all_time_profit, 2)}\n"
            f"🔄 Trades Today: {trades_today}\n\n"
            f"<b>Current Settings:</b>\n"
            f"• Trading: {'ON' if config.get('trading_enabled') else 'OFF'}\n"
            f"• Daily Payout: {config.get('daily_payout_percent', 1.5)}%\n"
            f"• Trades/Day: {config.get('trades_per_day', 10)}\n"
            f"• Percent/Trade: {config.get('percent_per_trade', 0.15):.4f}%\n"
            f"• Binance: {'ON' if config.get('use_binance') else 'OFF'}"
        )
        
        await update.message.reply_text(summary, parse_mode='HTML')

async def trade_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to enable trading."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    
    config = context.bot_data.get('trading_config', TRADING_DEFAULTS.copy())
    config['trading_enabled'] = True
    context.bot_data['trading_config'] = config
    
    await update.message.reply_text("✅ Trading enabled")

async def trade_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to disable trading."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    
    config = context.bot_data.get('trading_config', TRADING_DEFAULTS.copy())
    config['trading_enabled'] = False
    context.bot_data['trading_config'] = config
    
    await update.message.reply_text("🛑 Trading disabled")

async def trade_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to show trading status."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    
    config = context.bot_data.get('trading_config', TRADING_DEFAULTS.copy())
    
    status = (
        f"📊 <b>Trading Status</b>\n\n"
        f"Status: {'🟢 ON' if config.get('trading_enabled') else '🔴 OFF'}\n"
        f"Daily Payout: {config.get('daily_payout_percent', 1.5)}%\n"
        f"Trades/Day: {config.get('trades_per_day', 10)}\n"
        f"Percent/Trade: {config.get('percent_per_trade', 0.15):.4f}%\n"
        f"Binance: {'ON' if config.get('use_binance') else 'OFF'}"
    )
    
    await update.message.reply_text(status, parse_mode='HTML')

async def use_binance_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to enable Binance price fetching."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    
    config = context.bot_data.get('trading_config', TRADING_DEFAULTS.copy())
    config['use_binance'] = True
    context.bot_data['trading_config'] = config
    
    await update.message.reply_text("✅ Binance price fetching enabled")

async def use_binance_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to disable Binance price fetching."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    
    config = context.bot_data.get('trading_config', TRADING_DEFAULTS.copy())
    config['use_binance'] = False
    context.bot_data['trading_config'] = config
    
    await update.message.reply_text("🛑 Binance price fetching disabled (using simulation)")

async def admin_cmds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all admin commands."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    
    commands = """
📋 <b>Admin Commands</b>

<b>Trading Configuration:</b>
/set_daily_payout &lt;%&gt; - Set daily profit percentage
/set_trades_per_day &lt;n&gt; - Set number of trades per day
/set_percent_per_trade &lt;%&gt; - Set percent per trade
/trade_on - Enable trading
/trade_off - Disable trading
/trade_status - Show trading status

<b>User Management:</b>
/assign_pairs &lt;user_id&gt; &lt;pairs&gt; - Assign crypto pairs to user
/assign_percent &lt;user_id&gt; &lt;%&gt; - Assign percent per trade to user

<b>Reports:</b>
/trading_summary - Show trading day statistics
/user_summary &lt;user_id&gt; - Show user trading summary

<b>Binance:</b>
/use_binance_on - Enable Binance prices
/use_binance_off - Disable Binance prices

<b>Other:</b>
/admin_cmds - Show this help
"""
    await update.message.reply_text(commands, parse_mode='HTML')

async def user_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to show user trading summary."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    
    if not context.args or len(context.args) != 1:
        await update.message.reply_text("Usage: /user_summary <user_id>")
        return
    
    try:
        user_id = int(context.args[0])
        
        async with async_session() as session:
            user_data = await get_user(session, user_id)
            
            # Get user's custom config
            user_config = context.bot_data.get(f'user_config_{user_id}', {})
            pairs = user_config.get('crypto_pairs', ['Default'])
            percent = user_config.get('percent_per_trade', 'Default')
            
            summary = (
                f"📊 <b>User {user_id} Summary</b>\n\n"
                f"💰 Balance: ${format_price(float(user_data['balance']), 2)}\n"
                f"📈 Daily Profit: ${format_price(float(user_data['daily_profit']), 2)}\n"
                f"🎯 Total Profit: ${format_price(float(user_data['total_profit']), 2)}\n"
                f"👥 Referrals: {user_data['referral_count']}\n\n"
                f"<b>Custom Settings:</b>\n"
                f"• Crypto Pairs: {', '.join(pairs) if isinstance(pairs, list) else pairs}\n"
                f"• Percent/Trade: {percent if isinstance(percent, str) else f'{percent:.4f}%'}"
            )
            
            await update.message.reply_text(summary, parse_mode='HTML')
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID")

# -----------------------
# Main function
# -----------------------
async def post_init(application: Application):
    """Initialize bot data and scheduler."""
    # Initialize trading config
    if 'trading_config' not in application.bot_data:
        application.bot_data['trading_config'] = TRADING_DEFAULTS.copy()
    
    # Initialize database
    await init_db()
    
    # Setup scheduler
    scheduler = AsyncIOScheduler(timezone='UTC')
    
    # Schedule trading job
    config = application.bot_data.get('trading_config', TRADING_DEFAULTS.copy())
    trades_per_day = config.get('trades_per_day', 10)
    
    # Calculate interval between trades (in minutes)
    interval_minutes = (24 * 60) // trades_per_day
    
    scheduler.add_job(
        trading_job,
        'interval',
        minutes=interval_minutes,
        args=[application],
        id='trading_job'
    )
    
    # Schedule daily profit reset at midnight UTC
    scheduler.add_job(
        daily_profit_job,
        'cron',
        hour=0,
        minute=0,
        args=[application],
        id='daily_profit_job'
    )
    
    scheduler.start()
    application.bot_data['scheduler'] = scheduler
    
    logger.info("Bot initialized. Trading interval: %d minutes", interval_minutes)

def main():
    """Start the bot."""
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", menu))
    
    # Admin commands
    application.add_handler(CommandHandler("set_daily_payout", admin_set_daily_payout))
    application.add_handler(CommandHandler("set_trades_per_day", admin_set_trades_per_day))
    application.add_handler(CommandHandler("set_percent_per_trade", admin_set_percent_per_trade))
    application.add_handler(CommandHandler("assign_pairs", admin_assign_user_pairs))
    application.add_handler(CommandHandler("assign_percent", admin_assign_user_percent))
    application.add_handler(CommandHandler("trading_summary", admin_trading_summary))
    application.add_handler(CommandHandler("user_summary", user_summary))
    application.add_handler(CommandHandler("trade_on", trade_on))
    application.add_handler(CommandHandler("trade_off", trade_off))
    application.add_handler(CommandHandler("trade_status", trade_status))
    application.add_handler(CommandHandler("use_binance_on", use_binance_on))
    application.add_handler(CommandHandler("use_binance_off", use_binance_off))
    application.add_handler(CommandHandler("admin_cmds", admin_cmds))
    
    # Menu callbacks
    application.add_handler(CallbackQueryHandler(menu_callback))
    
    # Start bot
    logger.info("Starting bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
