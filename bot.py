# Full bot.py — Latest trading features implementation with:
# - httpx-based Binance price fetcher with TTL cache and simulated fallback price walk
# - DB models: UserTradeConfig (per-user config), DailySummary (daily records), Config (key/value store)
# - Admin commands: /set_trades_per_day, /set_daily_range, /set_trade_range, /set_user_trade, /trading_status, /trading_summary
# - Enforced ranges: daily 1.25%-1.5%, per-trade 0.05%-0.25% with validation
# - Trading engine: uses per-user config or global config, fetches live prices with cache, updates balances
# - Daily summary job at 23:59 UTC: persists DailySummary, sends formatted summary to users
# - Fixed-point decimal formatting (Decimal + format_price helpers)
# - CallbackQueryHandler ordering: admin/history callbacks before generic menu handler
# - Preserved: invest/withdraw/history/admin flows, admin_pending_command diagnostics
#
# Replace your existing bot.py with this file and restart the bot.

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
from apscheduler.jobstores.base import JobLookupError
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

# ADMIN_ID parsing with logging
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

# Binance and trading config
BINANCE_CACHE_TTL = int(os.getenv('BINANCE_CACHE_TTL', '10'))  # seconds
BINANCE_API_URL = "https://api.binance.com/api/v3/ticker/price"
USE_BINANCE = True  # Can be toggled by admin commands

# Global trading configuration (can be modified by admin commands)
GLOBAL_DAILY_PERCENT = 1.375  # default 1.375% daily (mid-range between 1.25% and 1.5%)
GLOBAL_TRADE_PERCENT = 0.15  # default 0.15% per trade (mid-range between 0.05% and 0.25%)
GLOBAL_TRADES_PER_DAY = 32  # default 32 trades per day (45 minute frequency)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logger.info("Configured ADMIN_ID=%s", ADMIN_ID)
if ADMIN_ID == 0:
    logger.warning("ADMIN_ID not configured or set to 0 — admin-only features will be unavailable or not work as expected.")

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
    balance = Column(Numeric(18, 6), default=0.0)
    balance_in_process = Column(Numeric(18, 6), default=0.0)
    daily_profit = Column(Numeric(18, 6), default=0.0)
    total_profit = Column(Numeric(18, 6), default=0.0)
    referral_count = Column(Integer, default=0)
    referral_earnings = Column(Numeric(18, 6), default=0.0)
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
    amount = Column(Numeric(18, 6))
    status = Column(String)         # 'pending','credited','rejected','completed'
    proof = Column(String)          # txid or file_id (format: 'photo:<file_id>' for photos)
    wallet = Column(String)
    network = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class UserTradeConfig(Base):
    __tablename__ = 'user_trade_configs'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, unique=True)
    pair = Column(String)  # e.g., 'BTCUSDT'
    percent_per_trade = Column(Numeric(10, 6))  # e.g., 0.5 for 0.5%
    created_at = Column(DateTime, default=datetime.utcnow)


class DailySummary(Base):
    __tablename__ = 'daily_summaries'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger)
    date = Column(DateTime)  # date for the summary
    daily_percent = Column(Numeric(10, 6))  # percent gained
    profit_amount = Column(Numeric(18, 6))  # profit in USDT
    total_balance = Column(Numeric(18, 6))  # balance at end of day
    created_at = Column(DateTime, default=datetime.utcnow)


class Config(Base):
    __tablename__ = 'config'
    key = Column(String, primary_key=True)
    value = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


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

    logger.critical("Unable to initialize database and fallback failed — exiting.")
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

# User trade config helpers
async def get_user_trade_config(session: AsyncSession, user_id: int) -> Optional[Dict]:
    """Get per-user trade configuration"""
    result = await session.execute(select(UserTradeConfig).where(UserTradeConfig.user_id == user_id))
    config = result.scalar_one_or_none()
    if not config:
        return None
    return {
        'pair': config.pair,
        'percent_per_trade': float(config.percent_per_trade),
    }

async def set_user_trade_config(session: AsyncSession, user_id: int, pair: str, percent_per_trade: float):
    """Set per-user trade configuration"""
    result = await session.execute(select(UserTradeConfig).where(UserTradeConfig.user_id == user_id))
    config = result.scalar_one_or_none()
    if config:
        config.pair = pair
        config.percent_per_trade = percent_per_trade
    else:
        config = UserTradeConfig(user_id=user_id, pair=pair, percent_per_trade=percent_per_trade)
        session.add(config)
    await session.commit()

# Config helpers for range management
async def get_config(session: AsyncSession, key: str, default: Optional[str] = None) -> Optional[str]:
    """Get config value by key"""
    result = await session.execute(select(Config).where(Config.key == key))
    config = result.scalar_one_or_none()
    return config.value if config else default

async def set_config(session: AsyncSession, key: str, value: str):
    """Set config value by key"""
    result = await session.execute(select(Config).where(Config.key == key))
    config = result.scalar_one_or_none()
    if config:
        config.value = value
        config.updated_at = datetime.utcnow()
    else:
        config = Config(key=key, value=value)
        session.add(config)
    await session.commit()

# Binance price cache with TTL
_binance_price_cache = {}  # {symbol: (price, timestamp)}
_binance_cache_lock = asyncio.Lock()

async def fetch_binance_price(symbol: str) -> Optional[float]:
    """
    Fetch price from Binance API with TTL cache.
    Returns None if fetch fails or if USE_BINANCE is disabled.
    """
    global _binance_price_cache
    
    # Check if Binance is enabled
    if not USE_BINANCE:
        logger.debug(f"Binance API disabled, skipping fetch for {symbol}")
        return None
    
    async with _binance_cache_lock:
        # Check cache
        if symbol in _binance_price_cache:
            price, timestamp = _binance_price_cache[symbol]
            if (datetime.utcnow() - timestamp).total_seconds() < BINANCE_CACHE_TTL:
                logger.debug(f"Cache hit for {symbol}: {price}")
                return price
        
        # Fetch from Binance
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(BINANCE_API_URL, params={'symbol': symbol})
                response.raise_for_status()
                data = response.json()
                price = float(data['price'])
                _binance_price_cache[symbol] = (price, datetime.utcnow())
                logger.info(f"Fetched Binance price for {symbol}: {price}")
                return price
        except Exception as e:
            logger.warning(f"Failed to fetch Binance price for {symbol}: {e}")
            return None

# Conversation states
INVEST_AMOUNT, INVEST_PROOF, INVEST_CONFIRM, WITHDRAW_AMOUNT, WITHDRAW_WALLET, WITHDRAW_CONFIRM, HISTORY_PAGE, HISTORY_DETAILS = range(8)

# -----------------------
# I18N: translations and helpers
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
        "lang_current": "Current language: {lang}",
        "info_text": "ℹ️ Information\n\nWelcome to AiCrypto bot.\n- Invest: deposit funds to provided wallet and upload proof (txid or screenshot).\n- Withdraw: request withdrawals; admin will approve and process.",
    },
    "fr": {
        "main_menu_title": "Menu Principal",
        "settings_title": "⚙️ Paramètres",
        "settings_language": "Langue",
        "change_language": "Changer la langue",
        "settings_wallet": "Définir/Mettre à jour le portefeuille de retrait",
        "lang_auto": "Auto (Telegram)",
        "lang_en": "Anglais",
        "lang_fr": "Français",
        "lang_es": "Espagnol",
        "lang_set_success": "Langue mise à jour en {lang}.",
        "lang_current": "Langue actuelle : {lang}",
        "info_text": "ℹ️ Informations\n\nBienvenue sur AiCrypto bot.\n- Investissez : déposez des fonds sur le portefeuille fourni et uploadez une preuve (txid ou capture d'écran).\n- Retrait : demandez des retraits ; l'admin approuvera et traitera.",
    },
    "es": {
        "main_menu_title": "Menú Principal",
        "settings_title": "⚙️ Configuración",
        "settings_language": "Idioma",
        "change_language": "Cambiar idioma",
        "settings_wallet": "Establecer/Actualizar billetera de retiro",
        "lang_auto": "Auto (Telegram)",
        "lang_en": "Inglés",
        "lang_fr": "Francés",
        "lang_es": "Español",
        "lang_set_success": "Idioma actualizado a {lang}.",
        "lang_current": "Idioma actual: {lang}",
        "info_text": "ℹ️ Información\n\nBienvenido al bot AiCrypto.\n- Invertir: deposita fondos en la billetera proporcionada y sube comprobante (txid o captura).\n- Retirar: solicita retiros; el admin aprobará y procesará.",
    }
}
DEFAULT_LANG = "en"
SUPPORTED_LANGS = ["en", "fr", "es"]
LANG_DISPLAY = {"en":"English","fr":"Français","es":"Español"}

def t(lang: str, key: str, **kwargs) -> str:
    bundle = TRANSLATIONS.get(lang, TRANSLATIONS[DEFAULT_LANG])
    txt = bundle.get(key, TRANSLATIONS[DEFAULT_LANG].get(key, key))
    if kwargs:
        return txt.format(**kwargs)
    return txt

async def get_user_language(session: AsyncSession, user_id: int, update: Optional[Update] = None) -> str:
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    preferred = None
    if user:
        preferred = getattr(user, "preferred_language", None)

    if preferred and preferred != "auto":
        if preferred in SUPPORTED_LANGS:
            return preferred

    if update and getattr(update, "effective_user", None):
        tlang = (update.effective_user.language_code or "").lower()
        if "-" in tlang:
            tlang = tlang.split("-")[0]
        if tlang in SUPPORTED_LANGS:
            return tlang

    if preferred == "auto" and update and getattr(update, "effective_user", None):
        tlang = (update.effective_user.language_code or "").lower()
        if "-" in tlang:
            tlang = tlang.split("-")[0]
        if tlang in SUPPORTED_LANGS:
            return tlang

    return DEFAULT_LANG

# -----------------------
# UI helpers and menu keyboard
# -----------------------
ZWSP = "\u200b"

def _compact_pad(label: str, target: int = 10) -> str:
    plain = label.replace(ZWSP, "")
    if len(plain) >= target:
        return label
    needed = target - len(plain)
    left = needed // 2
    right = needed - left
    return (" " * left) + label + (" " * right) + ZWSP

def build_main_menu_keyboard(full_two_column: bool = MENU_FULL_TWO_COLUMN, lang: str = DEFAULT_LANG) -> InlineKeyboardMarkup:
    labels = {
        "balance": "💰 " + {"en":"Balance","fr":"Solde","es":"Saldo"}.get(lang, "Balance"),
        "invest": "📈 " + {"en":"Invest","fr":"Investir","es":"Invertir"}.get(lang, "Invest"),
        "history": "🧾 " + {"en":"History","fr":"Historique","es":"Historial"}.get(lang, "History"),
        "withdraw": "💸 " + {"en":"Withdraw","fr":"Retirer","es":"Retirar"}.get(lang, "Withdraw"),
        "referrals": "👥 " + {"en":"Referrals","fr":"Fermes","es":"Referidos"}.get(lang, "Referrals"),
        "settings": "⚙️ " + {"en":"Settings","fr":"Paramètres","es":"Ajustes"}.get(lang, "Settings"),
        "information": "ℹ️ " + {"en":"Information","fr":"Information","es":"Información"}.get(lang, "Information"),
        "help": "❓ " + {"en":"Help","fr":"Aide","es":"Ayuda"}.get(lang, "Help"),
        "exit": "⨉ " + {"en":"Exit","fr":"Quitter","es":"Salir"}.get(lang, "Exit"),
    }

    if not full_two_column:
        rows = []
        rows.append([InlineKeyboardButton(labels["balance"], callback_data="menu_balance"),
                     InlineKeyboardButton(labels["invest"], callback_data="menu_invest")])
        rows.append([InlineKeyboardButton(labels["history"], callback_data="menu_history"),
                     InlineKeyboardButton(labels["withdraw"], callback_data="menu_withdraw")])
        rows.append([InlineKeyboardButton(labels["referrals"], callback_data="menu_referrals"),
                     InlineKeyboardButton(labels["settings"], callback_data="menu_settings")])
        rows.append([InlineKeyboardButton(labels["information"], callback_data="menu_info"),
                     InlineKeyboardButton(labels["help"], url=SUPPORT_URL)])
        rows.append([InlineKeyboardButton(labels["exit"], callback_data="menu_exit")])
        return InlineKeyboardMarkup(rows)

    tlen = 10
    left_right = [
        (labels["balance"], "menu_balance", labels["invest"], "menu_invest"),
        (labels["history"], "menu_history", labels["withdraw"], "menu_withdraw"),
        (labels["referrals"], "menu_referrals", labels["settings"], "menu_settings"),
        (labels["information"], "menu_info", labels["help"], "url"),
    ]

    rows = []
    for l_label, l_cb, r_label, r_cb in left_right:
        l = _compact_pad(l_label, target=tlen)
        r = _compact_pad(r_label, target=tlen)
        left_btn = InlineKeyboardButton(l, callback_data=l_cb)
        # Special handling for help button - use URL instead of callback
        if r_cb == "url":
            right_btn = InlineKeyboardButton(r, url=SUPPORT_URL)
        else:
            right_btn = InlineKeyboardButton(r, callback_data=r_cb)
        rows.append([left_btn, right_btn])

    exit_label = _compact_pad(labels["exit"], target=(tlen*2)//2)
    rows.append([InlineKeyboardButton(exit_label, callback_data="menu_exit")])
    return InlineKeyboardMarkup(rows)

def is_probable_wallet(address: str) -> bool:
    address = (address or "").strip()
    if not address:
        return False
    if address.startswith("0x") and len(address) >= 40 and re.match(r"^0x[0-9a-fA-F]+$", address):
        return True
    if address.startswith("T") and 25 <= len(address) <= 35:
        return True
    if re.match(r"^[13][a-km-zA-HJ-NP-Z1-9]{25,34}$", address):
        return True
    if 20 <= len(address) <= 100:
        return True
    return False

# -----------------------
# Admin and tx helpers (include username in admin view)
# -----------------------
def tx_card_text(tx: Transaction, username: Optional[str] = None) -> str:
    emoji = "📥" if (tx.type == 'invest') else ("💸" if tx.type == 'withdraw' else ("🤖" if tx.type == 'trade' else "💰"))
    created = tx.created_at.strftime("%Y-%m-%d %H:%M:%S") if tx.created_at else "-"
    user_line = f"User: <code>{tx.user_id}</code>"
    if username:
        user_line += f" (@{username})"
    
    base_text = (f"{emoji} <b>Ref {tx.ref}</b>\n"
                 f"Type: <b>{(tx.type or '').upper()}</b>\n"
                 f"Amount: <b>{float(tx.amount):.6f}$</b>\n"
                 f"{user_line}\n"
                 f"Status: <b>{(tx.status or '').upper()}</b>\n"
                 f"Created: {created}\n")
    
    # For withdrawal transactions, include wallet and network for admin to copy
    if tx.type == 'withdraw' and tx.wallet:
        wallet_info = (f"\n💳 <b>Withdrawal Details:</b>\n"
                      f"Wallet: <code>{tx.wallet}</code>\n"
                      f"Network: <b>{tx.network or 'N/A'}</b>\n")
        base_text += wallet_info
    
    return base_text

def admin_action_kb(tx_db_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Approve", callback_data=f"admin_start_approve_{tx_db_id}"),
         InlineKeyboardButton("❌ Reject", callback_data=f"admin_start_reject_{tx_db_id}")]
    ])

def admin_confirm_kb(action: str, tx_db_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes", callback_data=f"admin_confirm_{action}_{tx_db_id}"),
         InlineKeyboardButton("❌ No", callback_data=f"admin_cancel_{tx_db_id}")]
    ])

async def send_admin_tx_notification(bot, tx: Transaction, proof_file_id: Optional[str] = None, username: Optional[str] = None):
    text = tx_card_text(tx, username=username)
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

# -----------------------
# Helper: format price (Option A - fixed decimals, no scientific notation)
# -----------------------
def format_price(value: float, decimals: int = 12) -> str:
    """
    Convert a float to a fixed-point decimal string with `decimals` fractional digits.
    Ensures values like 3e-05 are shown as 0.000030000000 (no scientific notation).
    """
    try:
        if value is None:
            return "0"
        d = Decimal(str(value))
        q = Decimal('1').scaleb(-decimals)  # 10**-decimals
        d = d.quantize(q, rounding=ROUND_HALF_UP)
        return f"{d:.{decimals}f}"
    except Exception:
        # fallback: format with python float formatting forced to fixed decimals
        return f"{value:.{decimals}f}"

# -----------------------
# DAILY PROFIT JOB (unchanged)
# -----------------------
async def daily_profit_job():
    PROFIT_RATE = 0.015
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

# -----------------------
# Price simulation (in-memory)
# -----------------------
PRICE_PAIRS = {
    "USDT/BTC": 0.000030,
    "USDT/ETH": 0.00045,
    "USDT/BUSD": 1.000,
    "USDT/XRP": 0.000023,
    "USDT/LTC": 0.0032,
}
_price_lock = asyncio.Lock()

def simulate_price_walk(pair: str) -> float:
    base = PRICE_PAIRS.get(pair, 1.0)
    pct = random.uniform(-0.0015, 0.0015)
    new = base * (1.0 + pct)
    PRICE_PAIRS[pair] = round(new, 8)
    return PRICE_PAIRS[pair]

def pick_random_pair() -> str:
    return random.choice(list(PRICE_PAIRS.keys()))

# Trading control
TRADING_ENABLED = True
TRADING_FREQ_MINUTES = 45  # Default: 45 minutes between trades
TRADES_PER_DAY = 32  # Default: 32 trades per day (every 45 minutes)
MINUTES_PER_DAY = 24 * 60  # 1440 minutes in a day
TRADING_JOB_ID = 'trading_job_scheduled'
_trading_job = None

# -----------------------
# Trading job: fetch Binance prices with cache, use per-user config, update balances
# -----------------------
async def trading_job():
    if not TRADING_ENABLED:
        logger.debug("trading_job: TRADING_ENABLED is False, skipping run")
        return
    now = datetime.utcnow()
    logger.info("trading_job: starting run at %s", now.isoformat())
    async with async_session() as session:
        # Get configured ranges from Config
        trade_min = float(await get_config(session, 'trade_range_min', '0.05'))
        trade_max = float(await get_config(session, 'trade_range_max', '0.25'))
        daily_min = float(await get_config(session, 'daily_range_min', '1.25'))
        daily_max = float(await get_config(session, 'daily_range_max', '1.5'))
        
        result = await session.execute(select(User))
        users = result.scalars().all()
        if not users:
            logger.debug("trading_job: no users found")
            return
        for user in users:
            try:
                bal = float(user.balance or 0.0)
                if bal <= 1.0:
                    continue
                if random.random() < 0.5:  # 50% chance to trade
                    continue
                
                # Calculate daily profit percentage so far
                daily_profit_so_far = float(user.daily_profit or 0.0)
                starting_balance = bal - daily_profit_so_far
                if starting_balance <= 0:
                    starting_balance = bal
                current_daily_percent = (daily_profit_so_far / starting_balance) * 100 if starting_balance > 0 else 0
                
                # Generate random daily target if not exceeded
                daily_target_percent = random.uniform(daily_min, daily_max)
                if current_daily_percent >= daily_target_percent:
                    logger.debug(f"User {user.id} already reached daily target: {current_daily_percent:.2f}% >= {daily_target_percent:.2f}%")
                    continue
                
                # Get per-user config or use global
                user_config = await get_user_trade_config(session, user.id)
                if user_config:
                    pair = user_config['pair']
                else:
                    # Use random pair
                    pair = random.choice(['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'XRPUSDT', 'LTCUSDT'])
                
                # Generate random percent_per_trade within allowed range
                percent_per_trade = random.uniform(trade_min, trade_max)
                
                # Ensure we don't exceed daily limit
                remaining_daily_percent = daily_target_percent - current_daily_percent
                if percent_per_trade > remaining_daily_percent:
                    percent_per_trade = remaining_daily_percent
                
                if percent_per_trade <= 0:
                    continue
                
                # Try to fetch Binance price, fallback to simulated
                live_price = await fetch_binance_price(pair)
                if live_price is None:
                    # Fallback: simulate price walk
                    logger.debug(f"Using simulated price for {pair}")
                    if pair not in PRICE_PAIRS:
                        PRICE_PAIRS[pair] = 50000.0 if 'BTC' in pair else 2000.0 if 'ETH' in pair else 300.0
                    async with _price_lock:
                        live_price = simulate_price_walk(pair)
                
                # Calculate profit based on percent_per_trade
                profit = round((percent_per_trade / 100.0) * bal, 6)
                if profit <= 0:
                    continue
                
                # Update balances
                new_balance = bal + profit
                new_daily_profit = float(user.daily_profit or 0.0) + profit
                new_total_profit = float(user.total_profit or 0.0) + profit
                await update_user(session, user.id, 
                                balance=new_balance, 
                                daily_profit=new_daily_profit,
                                total_profit=new_total_profit)
                
                # Log transaction
                tx_id, tx_ref = await log_transaction(
                    session,
                    user_id=user.id,
                    ref=None,
                    type='trade',
                    amount=profit,
                    status='credited',
                    proof='',
                    wallet='',
                    network='',
                    created_at=now
                )
                
                # Create spread for display
                spread = random.uniform(0.001, 0.006)
                buy_rate_raw = live_price * (1.0 - spread/2)
                sell_rate_raw = live_price * (1.0 + spread/2 + random.uniform(0.0001, 0.0009))
                buy_rate = format_price(buy_rate_raw, decimals=8)
                sell_rate = format_price(sell_rate_raw, decimals=8)
                
                profit_percent = round((profit / bal) * 100, 2)
                display_balance = format_price(new_balance, decimals=2)
                date_str = now.strftime("%d.%m.%Y %H:%M")
                
                # Format pair for display (BTCUSDT -> USDT → BTC → USDT)
                if pair.endswith('USDT'):
                    base_asset = pair[:-4]
                    quote_asset = 'USDT'
                else:
                    base_asset = pair[:3]
                    quote_asset = pair[3:]
                trading_pair_str = f"{quote_asset} → {base_asset} → {quote_asset}"
                
                trade_text = (
                    "📢 AI trade was executed\n\n"
                    f"📅 Date: {date_str}\n"
                    f"💱 Trading pair: {trading_pair_str}\n"
                    f"📈 Buy rate: {buy_rate}\n"
                    f"📉 Sell rate: {sell_rate}\n"
                    f"📊 Profit: {profit_percent}%\n"
                    f"💰Balance: {display_balance} USDT"
                )
                try:
                    await application.bot.send_message(chat_id=user.id, text=trade_text)
                except Exception:
                    logger.debug("Unable to send trade alert to user %s (may not have interacted yet)", user.id)
            except Exception:
                logger.exception("trading_job failed for user %s", getattr(user, "id", "<unknown>"))

# -----------------------
# Daily summary job: runs at 23:59 UTC to summarize daily trading
# -----------------------
async def daily_summary_job():
    """Send daily summary to users and persist records"""
    logger.info("daily_summary_job: starting daily summary")
    now = datetime.utcnow()
    today_date = now.date()
    
    async with async_session() as session:
        result = await session.execute(select(User))
        users = result.scalars().all()
        
        for user in users:
            try:
                user_id = user.id
                balance = float(user.balance or 0.0)
                daily_profit = float(user.daily_profit or 0.0)
                
                # Skip users with no activity
                if balance <= 0 and daily_profit <= 0:
                    continue
                
                # Calculate daily percent
                starting_balance = balance - daily_profit
                if starting_balance > 0:
                    daily_percent = (daily_profit / starting_balance) * 100
                else:
                    daily_percent = 0.0
                
                # Save daily summary
                summary = DailySummary(
                    user_id=user_id,
                    date=datetime(today_date.year, today_date.month, today_date.day),
                    daily_percent=daily_percent,
                    profit_amount=daily_profit,
                    total_balance=balance,
                    created_at=now
                )
                session.add(summary)
                
                # Send message to user
                summary_text = (
                    "📊 Trading work for today is completed.\n"
                    f"💹 Total profit amounted to {daily_percent:.2f}%\n"
                    f"💰 Profit amount: {daily_profit:.2f} USDT\n"
                    f"📈 Total balance: {balance:.2f} USDT"
                )
                
                try:
                    await application.bot.send_message(chat_id=user_id, text=summary_text)
                except Exception as e:
                    logger.debug(f"Unable to send daily summary to user {user_id}: {e}")
                
                # Reset daily_profit for next day
                await update_user(session, user_id, daily_profit=0.0)
                
            except Exception as e:
                logger.exception(f"daily_summary_job failed for user {getattr(user, 'id', '<unknown>')}: {e}")
        
        await session.commit()
    logger.info("daily_summary_job: completed")

# -----------------------
# MENU CALLBACK (forwards special callbacks; handles menu items)
# -----------------------
async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data or ""

    if data == "lang_auto" or data.startswith("lang_"):
        await language_callback_handler(update, context)
        return

    if data == "settings_set_wallet":
        await settings_start_wallet(update, context)
        return

    if data == "menu_exit":
        await cancel_conv(update, context)
        async with async_session() as session:
            lang = await get_user_language(session, query.from_user.id, update=update)
        WELCOME_TEXT = (
            "Welcome to AiCrypto bot.\n"
            "- Invest: deposit funds to provided wallet and upload proof (txid or screenshot). and produce the full code \n"
            "- Withdraw: request withdrawals; admin will approve and process."
        )
        try:
            await query.message.edit_text(WELCOME_TEXT + "\n\n" + t(lang, "main_menu_title"), reply_markup=build_main_menu_keyboard(MENU_FULL_TWO_COLUMN, lang=lang))
        except Exception:
            await query.message.reply_text(WELCOME_TEXT + "\n\n" + t(lang, "main_menu_title"), reply_markup=build_main_menu_keyboard(MENU_FULL_TWO_COLUMN, lang=lang))
        return

    if data == "menu_balance":
        async with async_session() as session:
            await send_balance_message(query, session, query.from_user.id)
        return

    if data == "menu_history":
        await history_command(update, context)
        return

    if data == "menu_referrals":
        user_id = query.from_user.id
        bot_username = (await context.bot.get_me()).username
        referral_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
        async with async_session() as session:
            lang = await get_user_language(session, user_id, update=update)
        text = (f"👥 {t(lang,'settings_title')}\n\nShare this link:\n<code>{referral_link}</code>")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Copy Link", switch_inline_query_current_chat=referral_link)], [InlineKeyboardButton("Back to Main Menu", callback_data="menu_exit")]])
        await query.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
        return

    if data == "menu_settings":
        async with async_session() as session:
            lang = await get_user_language(session, query.from_user.id, update=update)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(t(lang,"change_language"), callback_data="settings_language")],
            [InlineKeyboardButton(t(lang,"settings_wallet"), callback_data="settings_set_wallet")],
            [InlineKeyboardButton("Back to Main Menu", callback_data="menu_exit")]
        ])
        await query.edit_message_text(t(lang, "settings_title"), reply_markup=kb)
        return

    if data == "settings_language":
        await settings_language_open_callback(update, context)
        return

    if data == "menu_info":
        async with async_session() as session:
            lang = await get_user_language(session, query.from_user.id, update=update)
        await query.edit_message_text(t(lang, "info_text"), reply_markup=build_main_menu_keyboard(MENU_FULL_TWO_COLUMN, lang=lang))
        return

    if data == "menu_help":
        help_button = InlineKeyboardMarkup([[InlineKeyboardButton("❓ Help", url=SUPPORT_URL)]])
        await query.message.reply_text("Click the button below for help:", reply_markup=help_button)
        return

# -----------------------
# Balance helper (supports CallbackQuery and Message)
# -----------------------
async def send_balance_message(query_or_message, session: AsyncSession, user_id: int):
    user = await get_user(session, user_id)
    lang = await get_user_language(session, user_id)
    text = (f"💎 <b>Your Balance</b>\n"
            f"Available: <b>{float(user.get('balance') or 0):.6f}$</b>\n"
            f"In Process: <b>{float(user.get('balance_in_process') or 0):.6f}$</b>\n"
            f"Daily Profit: <b>{float(user.get('daily_profit') or 0):.6f}$</b>\n"
            f"Total Profit: <b>{float(user.get('total_profit') or 0):.6f}$</b>\n\nManager: {SUPPORT_USER}")
    try:
        if hasattr(query_or_message, "message") and hasattr(query_or_message, "data"):
            try:
                await query_or_message.message.edit_text(text, parse_mode="HTML", reply_markup=build_main_menu_keyboard(MENU_FULL_TWO_COLUMN, lang=lang))
                return
            except Exception:
                pass
        await query_or_message.reply_text(text, parse_mode="HTML", reply_markup=build_main_menu_keyboard(MENU_FULL_TWO_COLUMN, lang=lang))
    except Exception:
        logger.exception("Failed to send balance message for user %s", user_id)

# -----------------------
# INVEST / WITHDRAW / ADMIN / HISTORY handlers
# -----------------------
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
    if query:
        await query.answer()
    user_id = query.from_user.id if query else update.effective_user.id
    amount = context.user_data.get('invest_amount')
    proof = context.user_data.get('invest_proof')
    if amount is None or proof is None:
        target = query.message if query else update.effective_message
        await target.reply_text("Missing data. Restart invest flow.", reply_markup=build_main_menu_keyboard(MENU_FULL_TWO_COLUMN))
        context.user_data.pop('invest_amount', None)
        context.user_data.pop('invest_proof', None)
        return ConversationHandler.END

    async with async_session() as session:
        tx_db_id, tx_ref = await log_transaction(
            session,
            user_id=user_id,
            ref=None,
            type='invest',
            amount=amount,
            status='pending',
            proof=str(proof),
            wallet=MASTER_WALLET,
            network=MASTER_NETWORK,
            created_at=datetime.utcnow()
        )

    now = datetime.utcnow()
    pdt_str = (now.replace(tzinfo=timezone.utc) - timedelta(hours=7)).strftime("%Y-%m-%d %H:%M (PDT)")
    deposit_request_text = (
        "🧾 Deposit Request Successful\n"
        f"Transaction ID, D-{tx_ref}\n"
        f"Amount, {amount:.2f} USDT ({MASTER_NETWORK})\n"
        f"Wallet, {MASTER_WALLET}\n"
        f"Network, {MASTER_NETWORK}\n"
        f"Status: Pending Approval\n"
        f"Date: {pdt_str}\n\n"
        "Once confirmed, your balance will be updated automatically."
    )

    try:
        if query:
            await query.message.reply_text(deposit_request_text, parse_mode="HTML")
        else:
            await update.effective_message.reply_text(deposit_request_text, parse_mode="HTML")
    except Exception:
        logger.exception("Failed to send deposit request message to user %s", user_id)

    async with async_session() as session:
        result = await session.execute(select(Transaction).where(Transaction.id == tx_db_id))
        tx = result.scalar_one_or_none()

    username = None
    if query and getattr(query, "from_user", None):
        username = (query.from_user.username or "").strip()
    elif update and getattr(update, "effective_user", None):
        username = (update.effective_user.username or "").strip()
    if username == "":
        username = None

    # notify admin and log; if admin send failed, will be logged
    try:
        await send_admin_tx_notification(context.application.bot, tx, proof_file_id=proof, username=username)
    except Exception:
        logger.exception("Failed sending admin notification for invest %s", tx_db_id)
    await post_admin_log(context.application.bot, f"New INVEST #{tx_db_id} ref {tx_ref} user {user_id} username @{username or 'N/A'} amount {amount:.2f}$")

    context.user_data.pop('invest_amount', None)
    context.user_data.pop('invest_proof', None)
    return ConversationHandler.END

# Withdraw handlers (unchanged other than admin username inclusion)
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
        async with async_session() as session:
            user = await get_user(session, user_id)
            balance = float(user.get('balance') or 0)
            if amount > balance:
                await msg.reply_text(f"Insufficient balance. Available: {balance:.2f}$.", reply_markup=build_main_menu_keyboard())
                context.user_data.pop('withdraw_amount', None)
                return ConversationHandler.END
            new_balance = balance - amount
            new_in_process = float(user.get('balance_in_process') or 0) + amount
            await update_user(session, user_id, balance=new_balance, balance_in_process=new_in_process)

        await msg.reply_text(f"Confirm withdrawal:\nAmount: {amount:.2f}$\nWallet: <code>{wallet_address}</code>\nNetwork: <b>{wallet_network}</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm", callback_data="withdraw_confirm_yes"), InlineKeyboardButton("❌ Cancel", callback_data="withdraw_confirm_no")]]))
        return WITHDRAW_CONFIRM
    else:
        await msg.reply_text(f"✅ Wallet saved:\n<code>{wallet_address}</code>\nNetwork: {wallet_network}", parse_mode="HTML", reply_markup=build_main_menu_keyboard(MENU_FULL_TWO_COLUMN))
        context.user_data.pop('withdraw_wallet', None)
        context.user_data.pop('withdraw_network', None)
        return ConversationHandler.END

async def withdraw_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    amount = context.user_data.get('withdraw_amount')
    wallet = context.user_data.get('withdraw_wallet')
    network = context.user_data.get('withdraw_network', '')

    if amount is None or not wallet:
        await query.message.reply_text("Missing withdrawal data. Start withdraw again.", reply_markup=build_main_menu_keyboard(MENU_FULL_TWO_COLUMN))
        context.user_data.pop('withdraw_amount', None)
        context.user_data.pop('withdraw_wallet', None)
        context.user_data.pop('withdraw_network', None)
        return ConversationHandler.END

    async with async_session() as session:
        tx_db_id, tx_ref = await log_transaction(
            session,
            user_id=user_id,
            ref=None,
            type='withdraw',
            amount=amount,
            status='pending',
            proof='',
            wallet=wallet,
            network=network,
            created_at=datetime.utcnow()
        )

    now = datetime.utcnow()
    pdt_str = (now.replace(tzinfo=timezone.utc) - timedelta(hours=7)).strftime("%Y-%m-%d %H:%M (PDT)")
    withdraw_request_text = (
        "🧾 Withdrawal Request Successful\n"
        f"Transaction ID, W-{tx_ref}\n"
        f"Amount, {amount:.2f} USDT ({network or 'N/A'})\n"
        f"Wallet, {wallet}\n"
        f"Network, {network or 'N/A'}\n"
        f"Status: Pending Approval\n"
        f"Date: {pdt_str}\n\n"
        "Once confirmed by admin, your withdrawal will be processed."
    )

    try:
        await query.message.reply_text(withdraw_request_text, parse_mode="HTML")
    except Exception:
        await query.message.reply_text(withdraw_request_text)

    async with async_session() as session:
        result = await session.execute(select(Transaction).where(Transaction.id == tx_db_id))
        tx = result.scalar_one_or_none()

    username = (query.from_user.username or "").strip() if getattr(query, "from_user", None) else None
    if username == "":
        username = None

    try:
        await send_admin_tx_notification(context.application.bot, tx, proof_file_id=None, username=username)
    except Exception:
        logger.exception("Failed sending admin notification for withdraw %s", tx_db_id)
    await post_admin_log(context.application.bot, f"New WITHDRAW #{tx_db_id} ref {tx_ref} user {user_id} username @{username or 'N/A'} amount {amount:.2f}$")

    context.user_data.pop('withdraw_amount', None)
    context.user_data.pop('withdraw_wallet', None)
    context.user_data.pop('withdraw_network', None)
    return ConversationHandler.END

# -----------------------
# ADMIN flows (approve/reject)
# -----------------------
async def admin_start_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        logger.warning("admin_start_action_callback invoked without callback_query")
        return
    await query.answer()
    if not _is_admin(query.from_user.id):
        logger.warning("admin_start_action_callback: user %s is not admin", query.from_user.id)
        await query.message.reply_text("Forbidden: admin only.")
        return
    logger.info("admin_start_action_callback: admin %s requested action %s", query.from_user.id, query.data)
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
    if not query:
        logger.warning("admin_confirm_callback invoked without callback_query")
        return
    await query.answer()
    if not _is_admin(query.from_user.id):
        logger.warning("admin_confirm_callback: user %s is not admin", query.from_user.id)
        await query.message.reply_text("Forbidden: admin only.")
        return
    logger.info("admin_confirm_callback: admin %s confirmed %s", query.from_user.id, query.data)
    data = query.data
    parts = data.split("_")
    if len(parts) < 4:
        await query.message.reply_text("Invalid confirmation data.")
        return
    action = parts[2]
    tx_db_id = int(parts[3])

    try:
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

                    receipt_text = (
                        "  **Deposit Receipt **\n"
                        "✅ Your deposit has been approved and credited\n"
                        f"Transaction ID, D-{tx.ref}\n"
                        f"Amount, {float(tx.amount):.2f} USDT\n"
                        f"Date, {(datetime.utcnow().replace(tzinfo=timezone.utc) - timedelta(hours=7)).strftime('%Y-%m-%d %H:%M (PDT)')}\n"
                        f"New balance: ${new_balance:.2f}"
                    )
                    try:
                        await context.application.bot.send_message(chat_id=tx.user_id, text=receipt_text, parse_mode="HTML")
                    except Exception:
                        logger.exception("Notify user fail invest approve")
                    await query.message.reply_text(f"Invest #{tx_db_id} credited.")
                    await post_admin_log(context.application.bot, f"Admin approved INVEST #{tx_db_id} ref {tx.ref}")

                elif tx.type == 'withdraw':
                    user = await get_user(session, tx.user_id)
                    new_in_process = max(0.0, float(user.get('balance_in_process') or 0) - float(tx.amount or 0))
                    await update_user(session, tx.user_id, balance_in_process=new_in_process)
                    await session.execute(sa_update(Transaction).where(Transaction.id == tx_db_id).values(status='completed'))
                    await session.commit()

                    receipt_text = (
                        "  **Withdrawal Receipt **\n"
                        "✅ Your withdrawal has been approved and processed\n"
                        f"Transaction ID, W-{tx.ref}\n"
                        f"Amount, {float(tx.amount):.2f} USDT\n"
                        f"Date, {(datetime.utcnow().replace(tzinfo=timezone.utc) - timedelta(hours=7)).strftime('%Y-%m-%d %H:%M (PDT)')}\n"
                        f"Wallet: {tx.wallet}\n"
                        f"Network: {tx.network}\n"
                        "If you don't see the funds in your wallet within a few minutes, please contact support."
                    )
                    try:
                        await context.application.bot.send_message(chat_id=tx.user_id, text=receipt_text, parse_mode="HTML")
                    except Exception:
                        logger.exception("Notify user fail withdraw complete")
                    await query.message.reply_text(f"Withdraw #{tx_db_id} completed.")
                    await post_admin_log(context.application.bot, f"Admin approved WITHDRAW #{tx_db_id} ref {tx.ref}")

            else:
                # reject
                if tx.type == 'invest':
                    await session.execute(sa_update(Transaction).where(Transaction.id == tx_db_id).values(status='rejected'))
                    await session.commit()
                    try:
                        await context.application.bot.send_message(chat_id=tx.user_id, text=f"❌ Your deposit (ref D-{tx.ref}) was rejected by admin.")
                    except Exception:
                        logger.exception("Notify user invest reject fail")
                    await query.message.reply_text(f"Invest #{tx_db_id} rejected.")
                    await post_admin_log(context.application.bot, f"Admin rejected INVEST #{tx_db_id} ref {tx.ref}")

                elif tx.type == 'withdraw':
                    user = await get_user(session, tx.user_id)
                    new_in_process = max(0.0, float(user.get('balance_in_process') or 0) - float(tx.amount or 0))
                    new_balance = float(user.get('balance') or 0) + float(tx.amount or 0)
                    await update_user(session, tx.user_id, balance=new_balance, balance_in_process=new_in_process)
                    await session.execute(sa_update(Transaction).where(Transaction.id == tx_db_id).values(status='rejected'))
                    await session.commit()
                    try:
                        await context.application.bot.send_message(chat_id=tx.user_id, text=f"❌ Your withdrawal (ref W-{tx.ref}) was rejected by admin. Funds restored.")
                    except Exception:
                        logger.exception("Notify user withdraw reject fail")
                    await query.message.reply_text(f"Withdraw #{tx_db_id} rejected and funds restored.")
                    await post_admin_log(context.application.bot, f"Admin rejected WITHDRAW #{tx_db_id} ref {tx.ref}")

    except Exception:
        logger.exception("Error handling admin confirmation for tx %s", tx_db_id)

    return

# Admin cancel handler
async def admin_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Action cancelled.")

# -----------------------
# Admin commands to control trading simulation
# -----------------------
def _is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID and ADMIN_ID != 0

async def cmd_trade_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        await update.effective_message.reply_text("Forbidden: admin only.")
        return
    global TRADING_ENABLED
    TRADING_ENABLED = True
    await update.effective_message.reply_text("Trading simulation ENABLED.")
    await post_admin_log(context.bot, "Admin enabled trading simulation.")

async def cmd_trade_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        await update.effective_message.reply_text("Forbidden: admin only.")
        return
    global TRADING_ENABLED
    TRADING_ENABLED = False
    await update.effective_message.reply_text("Trading simulation DISABLED.")
    await post_admin_log(context.bot, "Admin disabled trading simulation.")

async def cmd_trade_freq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        await update.effective_message.reply_text("Forbidden: admin only.")
        return
    args = context.args
    if not args or not args[0].isdigit():
        await update.effective_message.reply_text("Usage: /trade_freq <minutes> (integer)")
        return
    minutes = max(1, int(args[0]))
    global TRADING_FREQ_MINUTES
    TRADING_FREQ_MINUTES = minutes
    await update.effective_message.reply_text(f"Trading frequency set to {minutes} minutes. Will apply after restart or when triggered with /trade_now.")
    await post_admin_log(context.bot, f"Admin set trading frequency to {minutes} minutes.")

async def cmd_trade_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        await update.effective_message.reply_text("Forbidden: admin only.")
        return
    await update.effective_message.reply_text("Running trading job now...")
    await trading_job()
    await update.effective_message.reply_text("Trading run completed.")
    await post_admin_log(context.bot, "Admin triggered immediate trading run.")

async def cmd_trade_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        await update.effective_message.reply_text("Forbidden: admin only.")
        return
    await update.effective_message.reply_text(f"Trading: {'ENABLED' if TRADING_ENABLED else 'DISABLED'}\nFrequency: {TRADING_FREQ_MINUTES} minutes\nTrades per day: {TRADES_PER_DAY}\nSimulated pairs: {', '.join(PRICE_PAIRS.keys())}")

async def cmd_set_trades_per_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        await update.effective_message.reply_text("Forbidden: admin only.")
        return
    args = context.args
    if not args:
        await update.effective_message.reply_text("Usage: /set_trades_per_day <number> (positive integer, e.g., 144 for every 10 minutes)")
        return
    
    try:
        trades_per_day = int(args[0])
    except ValueError:
        await update.effective_message.reply_text("Invalid number. Usage: /set_trades_per_day <number> (positive integer)")
        return
    
    if trades_per_day <= 0:
        await update.effective_message.reply_text("Trades per day must be a positive integer.")
        return
    
    # Calculate frequency in minutes (use round for accuracy)
    freq_minutes = max(1, round(MINUTES_PER_DAY / trades_per_day))
    
    global TRADES_PER_DAY, TRADING_FREQ_MINUTES
    TRADES_PER_DAY = trades_per_day
    TRADING_FREQ_MINUTES = freq_minutes
    
    # Reschedule the trading job
    global _scheduler
    if _scheduler:
        # Remove existing trading job by ID
        try:
            _scheduler.remove_job(TRADING_JOB_ID)
            logger.info("Removed existing trading job with id: %s", TRADING_JOB_ID)
        except JobLookupError:
            logger.info("Trading job does not exist yet, will create new one")
        
        # Add new job with updated frequency and explicit ID
        _scheduler.add_job(
            trading_job, 
            'interval', 
            minutes=freq_minutes, 
            id=TRADING_JOB_ID,
            next_run_time=datetime.utcnow() + timedelta(seconds=5)
        )
        logger.info("Rescheduled trading job with frequency: %d minutes (trades per day: %d)", freq_minutes, trades_per_day)
    
    await update.effective_message.reply_text(
        f"✅ Trades per day set to {trades_per_day}.\n"
        f"Trading frequency: {freq_minutes} minutes.\n"
        f"Changes applied immediately."
    )
    await post_admin_log(context.bot, f"Admin set trades per day to {trades_per_day} (frequency: {freq_minutes} minutes).")

async def cmd_set_daily_percent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /set_daily_percent <percent>"""
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        await update.effective_message.reply_text("Forbidden: admin only.")
        return
    
    args = context.args
    if not args:
        await update.effective_message.reply_text("Usage: /set_daily_percent <percent> (e.g., 1.5 for 1.5%)")
        return
    
    try:
        percent = float(args[0])
    except ValueError:
        await update.effective_message.reply_text("Invalid number. Usage: /set_daily_percent <percent>")
        return
    
    if percent <= 0:
        await update.effective_message.reply_text("Daily percent must be positive.")
        return
    
    global GLOBAL_DAILY_PERCENT
    GLOBAL_DAILY_PERCENT = percent
    
    await update.effective_message.reply_text(f"✅ Global daily percent set to {percent}%")
    await post_admin_log(context.bot, f"Admin set global daily percent to {percent}%")

async def cmd_set_trade_percent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /set_trade_percent <percent>"""
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        await update.effective_message.reply_text("Forbidden: admin only.")
        return
    
    args = context.args
    if not args:
        await update.effective_message.reply_text("Usage: /set_trade_percent <percent> (e.g., 0.5 for 0.5% per trade)")
        return
    
    try:
        percent = float(args[0])
    except ValueError:
        await update.effective_message.reply_text("Invalid number. Usage: /set_trade_percent <percent>")
        return
    
    if percent <= 0:
        await update.effective_message.reply_text("Trade percent must be positive.")
        return
    
    global GLOBAL_TRADE_PERCENT
    GLOBAL_TRADE_PERCENT = percent
    
    await update.effective_message.reply_text(f"✅ Global trade percent set to {percent}%")
    await post_admin_log(context.bot, f"Admin set global trade percent to {percent}%")

async def cmd_set_user_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /set_user_trade <user_id> <pair> <percent>"""
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        await update.effective_message.reply_text("Forbidden: admin only.")
        return
    
    args = context.args
    if len(args) < 3:
        await update.effective_message.reply_text("Usage: /set_user_trade <user_id> <pair> <percent>\nExample: /set_user_trade 123456 BTCUSDT 0.5")
        return
    
    try:
        target_user_id = int(args[0])
        pair = args[1].upper()
        percent = float(args[2])
    except ValueError:
        await update.effective_message.reply_text("Invalid arguments. Usage: /set_user_trade <user_id> <pair> <percent>")
        return
    
    if percent <= 0:
        await update.effective_message.reply_text("Percent must be positive.")
        return
    
    async with async_session() as session:
        await set_user_trade_config(session, target_user_id, pair, percent)
    
    await update.effective_message.reply_text(
        f"✅ User {target_user_id} trade config set:\n"
        f"Pair: {pair}\n"
        f"Percent per trade: {percent}%"
    )
    await post_admin_log(context.bot, f"Admin set user {target_user_id} trade config: {pair} @ {percent}%")

async def cmd_trading_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /trading_status - show current config"""
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        await update.effective_message.reply_text("Forbidden: admin only.")
        return
    
    # Get configured ranges from Config
    async with async_session() as session:
        trade_min = await get_config(session, 'trade_range_min', '0.05')
        trade_max = await get_config(session, 'trade_range_max', '0.25')
        daily_min = await get_config(session, 'daily_range_min', '1.25')
        daily_max = await get_config(session, 'daily_range_max', '1.5')
        
        result = await session.execute(select(UserTradeConfig))
        user_configs = result.scalars().all()
        override_count = len(user_configs)
        
        override_text = ""
        if user_configs:
            override_text = "\n\n👥 Per-user overrides:\n"
            for cfg in user_configs[:10]:  # Show max 10
                override_text += f"  User {cfg.user_id}: {cfg.pair} @ {float(cfg.percent_per_trade)}%\n"
            if len(user_configs) > 10:
                override_text += f"  ... and {len(user_configs) - 10} more\n"
    
    status_text = (
        "⚙️ Trading Configuration Status\n\n"
        f"🔄 Trading: {'ENABLED' if TRADING_ENABLED else 'DISABLED'}\n"
        f"⏱ Frequency: {TRADING_FREQ_MINUTES} minutes (determined by trades per day)\n"
        f"📊 Trades per day: {TRADES_PER_DAY} (determined by frequency)\n"
        f"💹 Global daily percent: {daily_min}% to {daily_max}%\n"
        f"📈 Global trade percent: {trade_min}% to {trade_max}%\n"
        f"👤 User overrides: {override_count}"
        f"{override_text}"
    )
    
    await update.effective_message.reply_text(status_text)

async def cmd_trading_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /trading_summary [YYYY-MM-DD] - show aggregated daily summary"""
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        await update.effective_message.reply_text("Forbidden: admin only.")
        return
    
    # Parse date argument or use today
    args = context.args
    if args:
        try:
            target_date = datetime.strptime(args[0], "%Y-%m-%d").date()
        except ValueError:
            await update.effective_message.reply_text("Invalid date format. Use YYYY-MM-DD")
            return
    else:
        target_date = datetime.utcnow().date()
    
    async with async_session() as session:
        # Get all summaries for the date
        start_dt = datetime(target_date.year, target_date.month, target_date.day)
        end_dt = start_dt + timedelta(days=1)
        
        result = await session.execute(
            select(DailySummary).where(
                DailySummary.date >= start_dt,
                DailySummary.date < end_dt
            )
        )
        summaries = result.scalars().all()
        
        if not summaries:
            await update.effective_message.reply_text(f"No trading summaries found for {target_date}")
            return
        
        # Aggregate
        total_users = len(summaries)
        total_profit = sum(float(s.profit_amount) for s in summaries)
        total_balance = sum(float(s.total_balance) for s in summaries)
        avg_percent = sum(float(s.daily_percent) for s in summaries) / total_users if total_users > 0 else 0
        
        summary_text = (
            f"📊 Trading Summary for {target_date}\n\n"
            f"👥 Active users: {total_users}\n"
            f"💰 Total profit: {total_profit:.2f} USDT\n"
            f"📈 Total balance: {total_balance:.2f} USDT\n"
            f"💹 Average daily %: {avg_percent:.2f}%"
        )
        
        await update.effective_message.reply_text(summary_text)

async def cmd_use_binance_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /use_binance_on - Enable Binance API"""
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        await update.effective_message.reply_text("Forbidden: admin only.")
        return
    
    global USE_BINANCE
    USE_BINANCE = True
    
    await update.effective_message.reply_text("✅ Binance API enabled")
    await post_admin_log(context.bot, "Admin enabled Binance API")

async def cmd_use_binance_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /use_binance_off - Disable Binance API"""
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        await update.effective_message.reply_text("Forbidden: admin only.")
        return
    
    global USE_BINANCE
    USE_BINANCE = False
    
    await update.effective_message.reply_text("✅ Binance API disabled (will use simulated prices)")
    await post_admin_log(context.bot, "Admin disabled Binance API")

async def cmd_binance_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /binance_status - Show Binance API status"""
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        await update.effective_message.reply_text("Forbidden: admin only.")
        return
    
    status_text = (
        "🔗 Binance API Status\n\n"
        f"Status: {'✅ ENABLED' if USE_BINANCE else '❌ DISABLED'}\n"
        f"API URL: {BINANCE_API_URL}\n"
        f"Cache TTL: {BINANCE_CACHE_TTL}s\n"
        f"Cache entries: {len(_binance_price_cache)}"
    )
    
    await update.effective_message.reply_text(status_text)

async def cmd_set_daily_range(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /set_daily_range <min> <max> - Set allowed daily percent range (e.g., 1.25 1.5)"""
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        await update.effective_message.reply_text("Forbidden: admin only.")
        return
    
    args = context.args
    if len(args) < 2:
        await update.effective_message.reply_text(
            "Usage: /set_daily_range <min> <max>\n"
            "Example: /set_daily_range 1.25 1.5\n"
            "Sets the allowed daily percent range."
        )
        return
    
    try:
        min_percent = float(args[0])
        max_percent = float(args[1])
    except ValueError:
        await update.effective_message.reply_text("Invalid numbers. Usage: /set_daily_range <min> <max>")
        return
    
    # Validate bounds: daily must be 1.25% - 1.5%
    ALLOWED_DAILY_MIN = 1.25
    ALLOWED_DAILY_MAX = 1.5
    
    if min_percent < ALLOWED_DAILY_MIN or min_percent > ALLOWED_DAILY_MAX:
        await update.effective_message.reply_text(
            f"❌ Min daily percent must be between {ALLOWED_DAILY_MIN}% and {ALLOWED_DAILY_MAX}%"
        )
        return
    
    if max_percent < ALLOWED_DAILY_MIN or max_percent > ALLOWED_DAILY_MAX:
        await update.effective_message.reply_text(
            f"❌ Max daily percent must be between {ALLOWED_DAILY_MIN}% and {ALLOWED_DAILY_MAX}%"
        )
        return
    
    if min_percent > max_percent:
        await update.effective_message.reply_text("❌ Min percent cannot be greater than max percent")
        return
    
    # Store in Config
    async with async_session() as session:
        await set_config(session, 'daily_range_min', str(min_percent))
        await set_config(session, 'daily_range_max', str(max_percent))
    
    await update.effective_message.reply_text(
        f"✅ Daily percent range set to {min_percent}% - {max_percent}%"
    )
    await post_admin_log(context.bot, f"Admin set daily range to {min_percent}% - {max_percent}%")

async def cmd_set_trade_range(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /set_trade_range <min> <max> - Set allowed per-trade percent range (e.g., 0.05 0.25)"""
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        await update.effective_message.reply_text("Forbidden: admin only.")
        return
    
    args = context.args
    if len(args) < 2:
        await update.effective_message.reply_text(
            "Usage: /set_trade_range <min> <max>\n"
            "Example: /set_trade_range 0.05 0.25\n"
            "Sets the allowed per-trade percent range."
        )
        return
    
    try:
        min_percent = float(args[0])
        max_percent = float(args[1])
    except ValueError:
        await update.effective_message.reply_text("Invalid numbers. Usage: /set_trade_range <min> <max>")
        return
    
    # Validate bounds: trade must be 0.05% - 0.25%
    ALLOWED_TRADE_MIN = 0.05
    ALLOWED_TRADE_MAX = 0.25
    
    if min_percent < ALLOWED_TRADE_MIN or min_percent > ALLOWED_TRADE_MAX:
        await update.effective_message.reply_text(
            f"❌ Min trade percent must be between {ALLOWED_TRADE_MIN}% and {ALLOWED_TRADE_MAX}%"
        )
        return
    
    if max_percent < ALLOWED_TRADE_MIN or max_percent > ALLOWED_TRADE_MAX:
        await update.effective_message.reply_text(
            f"❌ Max trade percent must be between {ALLOWED_TRADE_MIN}% and {ALLOWED_TRADE_MAX}%"
        )
        return
    
    if min_percent > max_percent:
        await update.effective_message.reply_text("❌ Min percent cannot be greater than max percent")
        return
    
    # Store in Config
    async with async_session() as session:
        await set_config(session, 'trade_range_min', str(min_percent))
        await set_config(session, 'trade_range_max', str(max_percent))
    
    await update.effective_message.reply_text(
        f"✅ Per-trade percent range set to {min_percent}% - {max_percent}%"
    )
    await post_admin_log(context.bot, f"Admin set trade range to {min_percent}% - {max_percent}%")

async def cmd_admin_cmds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /admin_cmds - Show all admin commands"""
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        await update.effective_message.reply_text("Forbidden: admin only.")
        return
    
    commands_text = (
        "🛠 Admin Commands List\n\n"
        "**Trading Control:**\n"
        "/trade_on - Enable trading\n"
        "/trade_off - Disable trading\n"
        "/trade_status - Show trading status\n"
        "/trade_now - Trigger trading job now\n"
        "/trade_freq [minutes] - Set trading frequency\n\n"
        "**Binance Control:**\n"
        "/use_binance_on - Enable Binance API\n"
        "/use_binance_off - Disable Binance API\n"
        "/binance_status - Show Binance status\n\n"
        "**Configuration:**\n"
        "/set_trades_per_day <num> - Set trades per day\n"
        "/set_daily_percent <percent> - Set daily profit target\n"
        "/set_trade_percent <percent> - Set trade percent\n"
        "/set_daily_range <min> <max> - Set daily percent range (1.25-1.5%)\n"
        "/set_trade_range <min> <max> - Set trade percent range (0.05-0.25%)\n"
        "/set_user_trade <user_id> <pair> <percent> - Set user config\n"
        "/trading_status - Show trading config\n"
        "/trading_summary [date] - View daily summary\n\n"
        "**Admin:**\n"
        "/admin_cmds - Show this message\n"
        "/pending - Show pending transactions"
    )
    
    await update.effective_message.reply_text(commands_text)

# -----------------------
# HISTORY handlers (unchanged)
# -----------------------
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
        username = None
        try:
            tg_user = await application.bot.get_chat(tx.user_id)
            username = getattr(tg_user, "username", None)
        except Exception:
            username = None
        try:
            if proof.startswith("photo:"):
                file_id = proof.split(":",1)[1]
                await context.application.bot.send_photo(chat_id=user_id, photo=file_id, caption=tx_card_text(tx, username=username), parse_mode="HTML", reply_markup=admin_action_kb(tx.id))
            else:
                caption = tx_card_text(tx, username=username) + (f"\nProof: <code>{proof}</code>" if proof else "")
                await context.application.bot.send_message(chat_id=user_id, text=caption, parse_mode="HTML", reply_markup=admin_action_kb(tx.id))
        except Exception:
            logger.exception("Failed to send pending tx %s to admin", tx.id)

def history_list_item_text(tx: Transaction) -> str:
    created = tx.created_at.strftime("%Y-%m-%d") if tx.created_at else "-"
    ttype_raw = (tx.type or "").lower()
    if ttype_raw.startswith("with"):
        ttype = "WITHDRAW"
    elif ttype_raw.startswith("invest") or ttype_raw == "profit" or ttype_raw == "deposit":
        ttype = "DEPOSIT"
    elif ttype_raw == "trade":
        ttype = "TRADE"
    else:
        ttype = (tx.type or "UNKNOWN").upper()
    amount = f"{float(tx.amount):.6f}$" if tx.amount is not None else "-"
    return f"{created}  |  {amount}  |  {ttype}"

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ef_msg = update.effective_message
    user_id = update.effective_user.id
    args = context.args if hasattr(context, "args") else []
    is_admin = _is_admin(user_id)

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
            created = tx.created_at.strftime("%Y-%m-%d %H:%M:%S") if tx.created_at else ""
            lines.append(f"DB:{tx.id} Ref:{tx.ref} {tx.type.upper()} {float(tx.amount):.6f}$ {tx.status} {created}")
        for i in range(0, len(lines), 50):
            await ef_msg.reply_text("\n".join(lines[i:i+50]))
        return

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

    kb_rows = []
    for tx in page_items:
        kb_rows.append([InlineKeyboardButton(history_list_item_text(tx), callback_data=f"history_details_{tx.id}_{page}_{user_id}")])

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅ Prev", callback_data=f"history_page_{page-1}_{user_id}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("Next ➡", callback_data=f"history_page_{page+1}_{user_id}"))
    nav.append(InlineKeyboardButton("Exit ❌", callback_data="menu_exit"))
    if nav:
        kb_rows.append(nav)

    header = f"🧾 Transactions (page {page}/{total_pages}) — Tap an item for details\n\n"
    await ef_msg.reply_text(header, reply_markup=InlineKeyboardMarkup(kb_rows))

async def history_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
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
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    if len(parts) < 5:
        await query.message.reply_text("Invalid details callback.")
        return
    tx_db_id = int(parts[2])
    page = int(parts[3])
    owner_id = int(parts[4])

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
    amount = f"{float(tx.amount):.6f}$" if tx.amount is not None else ""
    tx_type = (tx.type or "").upper()
    status = (tx.status or "").upper()
    ref = tx.ref or "-"
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

    back_cb = f"history_back_{page}_{owner_id}"
    kb = []
    if _is_admin(query.from_user.id) and tx.status == 'pending':
        kb.append([InlineKeyboardButton("✅ Approve", callback_data=f"admin_start_approve_{tx.id}"),
                   InlineKeyboardButton("❌ Reject", callback_data=f"admin_start_reject_{tx.id}")])
    kb.append([InlineKeyboardButton("◀ Back to History", callback_data=back_cb),
               InlineKeyboardButton("Exit ❌", callback_data="menu_exit")])

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
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
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

# -----------------------
# LANGUAGE, start, help, wallet, balance handlers
# -----------------------
def build_language_kb(current_lang: str) -> InlineKeyboardMarkup:
    rows = []
    rows.append([InlineKeyboardButton(TRANSLATIONS.get(current_lang, TRANSLATIONS[DEFAULT_LANG])["lang_auto"], callback_data="lang_auto")])
    for code in SUPPORTED_LANGS:
        label = TRANSLATIONS[DEFAULT_LANG].get(f"lang_{code}", LANG_DISPLAY.get(code, code))
        rows.append([InlineKeyboardButton(label, callback_data=f"lang_{code}")])
    rows.append([InlineKeyboardButton("◀ Back", callback_data="menu_settings")])
    return InlineKeyboardMarkup(rows)

async def settings_language_open_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    async with async_session() as session:
        lang = await get_user_language(session, query.from_user.id, update=update)
    await query.message.edit_text(t(lang, "settings_title") + "\n\n" + t(lang, "settings_language"), reply_markup=build_language_kb(lang))

async def language_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    selected = None
    if data == "lang_auto":
        selected = "auto"
    elif data and data.startswith("lang_"):
        selected = data.split("_",1)[1]

    async with async_session() as session:
        result = await session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            user = User(id=user_id, preferred_language=selected)
            session.add(user)
            await session.commit()
        else:
            await session.execute(sa_update(User).where(User.id == user_id).values(preferred_language=selected))
            await session.commit()

        effective_lang = await get_user_language(session, user_id, update=update)

    await query.message.reply_text(t(effective_lang, "lang_set_success", lang=LANG_DISPLAY.get(effective_lang, effective_lang)))

async def cancel_conv(update: Optional[Update], context: ContextTypes.DEFAULT_TYPE):
    if context and getattr(context, "user_data", None):
        context.user_data.clear()
    if update and getattr(update, "callback_query", None):
        await update.callback_query.answer()
    return ConversationHandler.END

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        await send_balance_message(update.effective_message, session, update.effective_user.id)

async def balance_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

async def information_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        lang = await get_user_language(session, update.effective_user.id, update=update)
    await update.effective_message.reply_text(t(lang, "info_text"), reply_markup=build_main_menu_keyboard(MENU_FULL_TWO_COLUMN, lang=lang))

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_button = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Open Support Chat", url=SUPPORT_URL)]])
    await update.effective_message.reply_text("Need assistance? Click below to chat with support:", reply_markup=help_button)

async def settings_start_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text("Send your withdrawal wallet address and optional network (e.g., 0xabc... ERC20).")
    else:
        await update.effective_message.reply_text("Send your withdrawal wallet address and optional network (e.g., 0xabc... ERC20).")
    return WITHDRAW_WALLET

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        lang = await get_user_language(session, update.effective_user.id, update=update)
    WELCOME_TEXT = (
        "Welcome to AiCrypto bot.\n"
        "- Invest: deposit funds to provided wallet and upload proof (txid or screenshot). and produce the full code \n"
        "- Withdraw: request withdrawals; admin will approve and process."
    )
    try:
        await update.effective_message.reply_text(WELCOME_TEXT + "\n\n" + t(lang, "main_menu_title"), reply_markup=build_main_menu_keyboard(MENU_FULL_TWO_COLUMN, lang=lang))
    except Exception:
        await update.effective_message.reply_text("Main Menu", reply_markup=build_main_menu_keyboard(MENU_FULL_TWO_COLUMN))

# -----------------------
# MAIN wiring: schedule trading_job and wire admin commands
# -----------------------
application: Optional[Application] = None
_scheduler: Optional[AsyncIOScheduler] = None

def main():
    global application, _scheduler
    application = Application.builder().token(BOT_TOKEN).build()

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
            INVEST_CONFIRM: [CallbackQueryHandler(invest_confirm_callback, pattern='^invest_confirm_yes$'), CallbackQueryHandler(invest_confirm_callback, pattern='^invest_confirm_no$')],
            WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount_received)],
            WITHDRAW_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_wallet_received), CallbackQueryHandler(withdraw_wallet_received, pattern='^withdraw_use_saved$')],
            WITHDRAW_CONFIRM: [CallbackQueryHandler(withdraw_confirm_callback, pattern='^withdraw_confirm_yes$'), CallbackQueryHandler(withdraw_confirm_callback, pattern='^withdraw_confirm_no$')],
        },
        fallbacks=[CommandHandler('cancel', lambda u,c: cancel_conv(u,c))],
        allow_reentry=True,
    )

    application.add_handler(conv_handler)

    # language & settings handlers
    application.add_handler(CallbackQueryHandler(settings_language_open_callback, pattern='^settings_language$'))
    application.add_handler(CallbackQueryHandler(language_callback_handler, pattern='^lang_'))
    application.add_handler(CallbackQueryHandler(language_callback_handler, pattern='^lang_auto$'))

    # admin handlers (must be registered before generic menu handler so patterns match)
    application.add_handler(CallbackQueryHandler(admin_start_action_callback, pattern='^admin_start_(approve|reject)_\\d+$'))
    application.add_handler(CallbackQueryHandler(admin_confirm_callback, pattern='^admin_confirm_(approve|reject)_\\d+$'))
    application.add_handler(CallbackQueryHandler(admin_cancel_callback, pattern='^admin_cancel_\\d+$'))

    # history callbacks
    application.add_handler(CallbackQueryHandler(history_page_callback, pattern='^history_page_\\d+_\\d+$'))
    application.add_handler(CallbackQueryHandler(history_details_callback, pattern='^history_details_\\d+_\\d+_\\d+$'))
    application.add_handler(CallbackQueryHandler(history_back_callback, pattern='^history_back_\\d+_\\d+$'))

    # generic menu handler should come after specific handlers
    application.add_handler(CallbackQueryHandler(menu_callback))

    # commands
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("wallet", wallet_command))
    # use history_command (defined above)
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("information", information_command))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("pending", admin_pending_command))

    # admin trade control commands
    application.add_handler(CommandHandler("trade_on", cmd_trade_on))
    application.add_handler(CommandHandler("trade_off", cmd_trade_off))
    application.add_handler(CommandHandler("trade_freq", cmd_trade_freq))
    application.add_handler(CommandHandler("trade_now", cmd_trade_now))
    application.add_handler(CommandHandler("trade_status", cmd_trade_status))
    application.add_handler(CommandHandler("set_trades_per_day", cmd_set_trades_per_day))
    application.add_handler(CommandHandler("set_daily_percent", cmd_set_daily_percent))
    application.add_handler(CommandHandler("set_trade_percent", cmd_set_trade_percent))
    application.add_handler(CommandHandler("set_daily_range", cmd_set_daily_range))
    application.add_handler(CommandHandler("set_trade_range", cmd_set_trade_range))
    application.add_handler(CommandHandler("set_user_trade", cmd_set_user_trade))
    application.add_handler(CommandHandler("trading_status", cmd_trading_status))
    application.add_handler(CommandHandler("trading_summary", cmd_trading_summary))
    
    # Binance control commands
    application.add_handler(CommandHandler("use_binance_on", cmd_use_binance_on))
    application.add_handler(CommandHandler("use_binance_off", cmd_use_binance_off))
    application.add_handler(CommandHandler("binance_status", cmd_binance_status))
    
    # Admin helper commands
    application.add_handler(CommandHandler("admin_cmds", cmd_admin_cmds))

    application.add_handler(MessageHandler(filters.Regex("^Balance$"), balance_text_handler))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        _scheduler = AsyncIOScheduler(event_loop=loop)
    except TypeError:
        _scheduler = AsyncIOScheduler()
    # daily profit
    _scheduler.add_job(daily_profit_job, 'cron', hour=0, minute=0)
    # daily summary job at 23:59 UTC
    _scheduler.add_job(daily_summary_job, 'cron', hour=23, minute=59)
    # SCHEDULE trading_job directly as coroutine — not via lambda
    _scheduler.add_job(
        trading_job, 
        'interval', 
        minutes=TRADING_FREQ_MINUTES, 
        id=TRADING_JOB_ID,
        next_run_time=datetime.utcnow() + timedelta(seconds=15)
    )
    _scheduler.start()

    logger.info("AiCrypto Bot STARTED")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    try:
        asyncio.run(init_db(retries=5, backoff=2.0, fallback_to_sqlite=True))
    except Exception as e:
        logger.exception("DB init failed: %s", e)
        raise
    main()
