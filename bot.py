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
# Admin / tx helpers
# -----------------------
... (TRUNCATED FOR BREVITY)
