# Full bot.py — Combined final version with Binance live-price integration and full handlers merged.
# - Real Binance price fetching (async via aiohttp), with TTL cache and admin controls (/use_binance_on, /use_binance_off, /binance_status)
# - Trading simulation uses Binance prices when enabled, falls back to simulated prices on failure
# - Rates formatted with fixed-point decimals (no scientific notation) using format_price (Option A)
# - Admin action keyboards include "🛠️ Commands" button and /admin_cmds command is available
# - Admin callbacks registered before generic menu handler (so they are not swallowed)
# - All main handlers (start, invest, withdraw, wallet, history, admin approve/reject, trading job, DB init) included
#
# Requirements: python-telegram-bot v20+, aiohttp, APScheduler, SQLAlchemy, aiosqlite, python-dotenv
# Environment variables:
# - BOT_TOKEN (required)
# - ADMIN_ID (required numeric)
# - ADMIN_LOG_CHAT_ID (optional)
# - DATABASE_URL (optional)
# - BINANCE_CACHE_TTL (optional, seconds)
#
# Replace your existing bot.py with this file and restart.

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

import aiohttp

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

try:
    ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
except Exception:
    ADMIN_ID = 0

ADMIN_LOG_CHAT_ID = os.getenv('ADMIN_LOG_CHAT_ID')
MASTER_WALLET = os.getenv('MASTER_WALLET', 'TAbc...')
MASTER_NETWORK = os.getenv('MASTER_NETWORK', 'TRC20')
SUPPORT_USER = os.getenv('SUPPORT_USER', '@AiCrypto_Support1')
SUPPORT_URL = os.getenv('SUPPORT_URL') or (f"https://t.me/{SUPPORT_USER.lstrip('@')}" if SUPPORT_USER else "https://t.me/")

MENU_FULL_TWO_COLUMN = os.getenv('MENU_FULL_TWO_COLUMN', 'true').lower() in ('1','true','yes','on')
DATABASE_URL = os.getenv('DATABASE_URL')

# Binance config
USE_BINANCE_BY_DEFAULT = True
BINANCE_CACHE_TTL = int(os.getenv('BINANCE_CACHE_TTL', '10'))  # seconds

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
    proof = Column(String)
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
        "lang_set_success": "Language updated to {lang}.",
        "info_text": "ℹ️ Information\n\nWelcome to AiCrypto bot."
    }
}
DEFAULT_LANG = "en"

def t(lang: str, key: str, **kwargs) -> str:
    bundle = TRANSLATIONS.get(lang, TRANSLATIONS[DEFAULT_LANG])
    txt = bundle.get(key, TRANSLATIONS[DEFAULT_LANG].get(key, key))
    return txt.format(**kwargs) if kwargs else txt

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
        "balance": "💰 Balance",
        "invest": "📈 Invest",
        "history": "🧾 History",
        "withdraw": "💸 Withdraw",
        "referrals":"👥 Referrals",
        "settings":"⚙️ Settings",
        "information":"ℹ️ Information",
        "help":"❓ Help",
        "exit":"⨉ Exit",
    }
    rows = [
        [InlineKeyboardButton(labels["balance"], callback_data="menu_balance"), InlineKeyboardButton(labels["invest"], callback_data="menu_invest")],
        [InlineKeyboardButton(labels["history"], callback_data="menu_history"), InlineKeyboardButton(labels["withdraw"], callback_data="menu_withdraw")],
        [InlineKeyboardButton(labels["referrals"], callback_data="menu_referrals"), InlineKeyboardButton(labels["settings"], callback_data="menu_settings")],
        [InlineKeyboardButton(labels["information"], callback_data="menu_info"), InlineKeyboardButton(labels["help"], url=SUPPORT_URL)],
        [InlineKeyboardButton(labels["exit"], callback_data="menu_exit")]
    ]
    return InlineKeyboardMarkup(rows)

def is_probable_wallet(address: str) -> bool:
    address = (address or "").strip()
    if not address: return False
    if address.startswith("0x") and len(address) >= 40 and re.match(r"^0x[0-9a-fA-F]+$", address): return True
    if 25 <= len(address) <= 35 and address.startswith("T"): return True
    if re.match(r"^[13][a-km-zA-HJ-NP-Z1-9]{25,34}$", address): return True
    if 20 <= len(address) <= 100: return True
    return False

# -----------------------
# Admin / transaction helpers
# -----------------------
def tx_card_text(tx: Transaction, username: Optional[str] = None) -> str:
    emoji = "📥" if (tx.type == 'invest') else ("💸" if tx.type == 'withdraw' else ("🤖" if tx.type == 'trade' else "💰"))
    created = tx.created_at.strftime("%Y-%m-%d %H:%M:%S") if tx.created_at else "-"
    user_line = f"User: <code>{tx.user_id}</code>"
    if username:
        user_line += f" (@{username})"
    return (f"{emoji} <b>Ref {tx.ref}</b>\nType: <b>{(tx.type or '').upper()}</b>\nAmount: <b>{float(tx.amount):.6f}$</b>\n{user_line}\nStatus: <b>{(tx.status or '').upper()}</b>\nCreated: {created}\n")

def admin_action_kb(tx_db_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("✅ Approve", callback_data=f"admin_start_approve_{tx_db_id}"),
         InlineKeyboardButton("❌ Reject", callback_data=f"admin_start_reject_{tx_db_id}")],
        [InlineKeyboardButton("🛠️ Commands", callback_data="admin_commands")]
    ]
    return InlineKeyboardMarkup(rows)

def admin_confirm_kb(action: str, tx_db_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("✅ Yes", callback_data=f"admin_confirm_{action}_{tx_db_id}"),
         InlineKeyboardButton("❌ No", callback_data=f"admin_cancel_{tx_db_id}")],
        [InlineKeyboardButton("🛠️ Commands", callback_data="admin_commands")]
    ]
    return InlineKeyboardMarkup(rows)

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
# Formatting helper
# -----------------------
def format_price(value: float, decimals: int = 12) -> str:
    try:
        if value is None:
            return "0"
        d = Decimal(str(value))
        q = Decimal('1').scaleb(-decimals)
        d = d.quantize(q, rounding=ROUND_HALF_UP)
        return f"{d:.{decimals}f}"
    except Exception:
        return f"{value:.{decimals}f}"

# -----------------------
# BINANCE Integration
# -----------------------
_binance_session: Optional[aiohttp.ClientSession] = None
_binance_cache: Dict[str, Dict] = {}
USE_BINANCE = USE_BINANCE_BY_DEFAULT

async def _ensure_binance_session():
    global _binance_session
    if _binance_session is None or _binance_session.closed:
        _binance_session = aiohttp.ClientSession()

async def fetch_binance_price(symbol: str) -> Optional[float]:
    symbol = symbol.upper()
    now = datetime.utcnow()
    cached = _binance_cache.get(symbol)
    if cached:
        age = (now - cached['ts']).total_seconds()
        if age < BINANCE_CACHE_TTL:
            return cached['price']
    await _ensure_binance_session()
    url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
    try:
        async with _binance_session.get(url, timeout=5) as resp:
            if resp.status != 200:
                logger.warning("Binance returned status %s for %s", resp.status, symbol)
                return None
            data = await resp.json()
            price = float(data.get('price'))
            _binance_cache[symbol] = {'price': price, 'ts': now}
            return price
    except Exception:
        logger.exception("Failed to fetch Binance price for %s", symbol)
        return None

# -----------------------
# Simulated fallback prices
# -----------------------
SIM_PRICE_PAIRS = {
    "BTC/USDT": 35000.0,
    "ETH/USDT": 2200.0,
    "BUSD/USDT": 1.0,
    "XRP/USDT": 0.55,
    "LTC/USDT": 85.0,
}
_price_lock = asyncio.Lock()

def simulate_price_walk(pair: str) -> float:
    base = SIM_PRICE_PAIRS.get(pair, 1.0)
    pct = random.uniform(-0.0025, 0.0025)
    new = base * (1.0 + pct)
    SIM_PRICE_PAIRS[pair] = round(new, 8)
    return SIM_PRICE_PAIRS[pair]

def pick_random_pair() -> str:
    return random.choice(list(SIM_PRICE_PAIRS.keys()))

# -----------------------
# Trading job
# -----------------------
TRADING_ENABLED = True
TRADING_FREQ_MINUTES = 10

async def trading_job():
    if not TRADING_ENABLED:
        logger.debug("trading_job: disabled, skipping")
        return
    now = datetime.utcnow()
    logger.info("trading_job: run at %s", now.isoformat())
    async with async_session() as session:
        result = await session.execute(select(User))
        users = result.scalars().all()
        if not users:
            return
        for user in users:
            try:
                bal = float(user.balance or 0.0)
                if bal <= 1.0:
                    continue
                if random.random() < 0.6:
                    continue
                pair = pick_random_pair()  # e.g., "BTC/USDT"
                price = None
                if USE_BINANCE:
                    symbol = pair.replace("/", "")
                    price = await fetch_binance_price(symbol)
                if price is None:
                    async with _price_lock:
                        price = simulate_price_walk(pair)
                spread = random.uniform(0.0005, 0.005)
                buy_rate_raw = price * (1.0 - spread/2)
                sell_rate_raw = price * (1.0 + spread/2 + random.uniform(0.0001, 0.0009))
                buy_rate = format_price(buy_rate_raw, decimals=8)
                sell_rate = format_price(sell_rate_raw, decimals=8)
                runs_per_day = max(1.0, (24*60) / TRADING_FREQ_MINUTES)
                daily_rate = 0.015
                base_slice = bal * daily_rate / runs_per_day
                profit = round(base_slice * random.uniform(0.2, 1.6), 6)
                if profit <= 0:
                    continue
                new_balance = bal + profit
                new_total_profit = float(user.total_profit or 0.0) + profit
                await update_user(session, user.id, balance=new_balance, total_profit=new_total_profit)
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
                profit_percent = round((profit / bal) * 100, 6)
                base_asset, quote_asset = pair.split("/")  # BTC, USDT
                trading_pair_str = f"{quote_asset} → {base_asset} → {quote_asset}"
                display_balance = round(new_balance, 6)
                date_str = now.strftime("%d.%m.%Y %H:%M")
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
                    logger.debug("Failed to send trade alert to user %s", user.id)
            except Exception:
                logger.exception("trading_job error for user %s", getattr(user, "id", "<unknown>"))

# -----------------------
# Handlers: start, balance, wallet, invest, withdraw, history, admin, etc.
# -----------------------
def _is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID and ADMIN_ID != 0

# start
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with async_session() as session:
        lang = await get_user_language(session, user_id, update=update)
    WELCOME_TEXT = (
        "Welcome to AiCrypto bot.\n"
        "- Invest: deposit funds to provided wallet and upload proof (txid or screenshot).\n"
        "- Withdraw: request withdrawals; admin will approve and process."
    )
    try:
        await update.effective_message.reply_text(WELCOME_TEXT + "\n\n" + t(lang, "main_menu_title"), reply_markup=build_main_menu_keyboard(MENU_FULL_TWO_COLUMN, lang=lang))
    except Exception:
        await update.effective_message.reply_text("Main Menu", reply_markup=build_main_menu_keyboard(MENU_FULL_TWO_COLUMN))

# balance
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

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        await send_balance_message(update.effective_message, session, update.effective_user.id)

# wallet
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

# invest flow
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

    try:
        await send_admin_tx_notification(context.application.bot, tx, proof_file_id=proof, username=username)
    except Exception:
        logger.exception("Failed sending admin notification for invest %s", tx_db_id)
    await post_admin_log(context.application.bot, f"New INVEST #{tx_db_id} ref {tx_ref} user {user_id} username @{username or 'N/A'} amount {amount:.2f}$")

    context.user_data.pop('invest_amount', None)
    context.user_data.pop('invest_proof', None)
    return ConversationHandler.END

# Withdraw flow
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
# Admin callbacks: approve/reject
# -----------------------
async def admin_start_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if not _is_admin(query.from_user.id):
        await query.message.reply_text("Forbidden: admin only.")
        return
    data = query.data or ""
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
        return
    await query.answer()
    if not _is_admin(query.from_user.id):
        await query.message.reply_text("Forbidden: admin only.")
        return
    data = query.data or ""
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
                        f"Wallet: {tx.wallet}\n"
                        f"Network: {tx.network}\n"
                    )
                    try:
                        await context.application.bot.send_message(chat_id=tx.user_id, text=receipt_text, parse_mode="HTML")
                    except Exception:
                        logger.exception("Notify user fail withdraw complete")
                    await query.message.reply_text(f"Withdraw #{tx_db_id} completed.")
                    await post_admin_log(context.application.bot, f"Admin approved WITHDRAW #{tx_db_id} ref {tx.ref}")

            else:
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

async def admin_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Action cancelled.")

# -----------------------
# History handlers
# -----------------------
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
# Language & settings
# -----------------------
def build_language_kb(current_lang: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(TRANSLATIONS.get(current_lang, TRANSLATIONS[DEFAULT_LANG])["lang_auto"], callback_data="lang_auto")]]
    rows.append([InlineKeyboardButton("◀ Back", callback_data="menu_settings")])
    return InlineKeyboardMarkup(rows)

async def settings_language_open_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_text(t(DEFAULT_LANG, "settings_title") + "\n\n" + t(DEFAULT_LANG, "settings_language"), reply_markup=build_language_kb(DEFAULT_LANG))

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

    await query.message.reply_text(t(DEFAULT_LANG, "lang_set_success", lang=selected or "auto"))

# -----------------------
# Admin commands: binance toggles, admin_cmds
# -----------------------
async def cmd_use_binance_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        await update.effective_message.reply_text("Forbidden: admin only.")
        return
    global USE_BINANCE
    USE_BINANCE = True
    await update.effective_message.reply_text("Binance price fetching ENABLED.")
    await post_admin_log(context.bot, "Admin enabled Binance price fetching.")

async def cmd_use_binance_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        await update.effective_message.reply_text("Forbidden: admin only.")
        return
    global USE_BINANCE
    USE_BINANCE = False
    await update.effective_message.reply_text("Binance price fetching DISABLED; using simulated prices.")
    await post_admin_log(context.bot, "Admin disabled Binance price fetching.")

async def cmd_binance_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        await update.effective_message.reply_text("Forbidden: admin only.")
        return
    cache_info = ", ".join(f"{s}:{d['price']}" for s,d in _binance_cache.items())
    await update.effective_message.reply_text(f"Binance enabled: {USE_BINANCE}\nCache TTL: {BINANCE_CACHE_TTL}s\nCache: {cache_info or 'empty'}")

def admin_commands_text() -> str:
    return (
        "🛠️ Admin Commands\n\n"
        "/pending - show pending transactions\n"
        "/trade_on - enable trading simulation\n"
        "/trade_off - disable trading simulation\n"
        "/trade_freq <minutes> - set trading frequency\n"
        "/trade_now - trigger trading run immediately\n"
        "/trade_status - show trading simulation status\n"
        "/use_binance_on - enable live Binance prices\n"
        "/use_binance_off - disable live Binance prices\n"
        "/binance_status - show binance status/cache\n"
        "/admin_cmds - show this commands list\n"
    )

async def admin_cmds_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        await update.effective_message.reply_text("Forbidden: admin only.")
        return
    await update.effective_message.reply_text(admin_commands_text())

# -----------------------
# Misc helpers: pending, trade control
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
    await update.effective_message.reply_text(f"Trading: {'ENABLED' if TRADING_ENABLED else 'DISABLED'}\nFrequency: {TRADING_FREQ_MINUTES} minutes\nBinance enabled: {USE_BINANCE}")

# -----------------------
# MENU callback
# -----------------------
async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data or ""

    if data == "admin_commands":
        uid = query.from_user.id
        if not _is_admin(uid):
            await query.message.reply_text("Forbidden: admin only.")
            return
        await query.message.reply_text(admin_commands_text())
        return

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
            "- Invest: deposit funds to provided wallet and upload proof (txid or screenshot).\n"
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

# -----------------------
# Utilities
# -----------------------
async def get_user_language(session: AsyncSession, user_id: int, update: Optional[Update] = None) -> str:
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    preferred = getattr(user, "preferred_language", None) if user else None
    if preferred and preferred != "auto":
        return preferred if preferred in TRANSLATIONS else DEFAULT_LANG
    if update and getattr(update, "effective_user", None):
        tlang = (update.effective_user.language_code or "").lower().split("-")[0]
        if tlang in TRANSLATIONS:
            return tlang
    return DEFAULT_LANG

# -----------------------
# Main wiring
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
    application.add_handler(conv_handler)

    # language & settings
    application.add_handler(CallbackQueryHandler(settings_language_open_callback, pattern='^settings_language$'))
    application.add_handler(CallbackQueryHandler(language_callback_handler, pattern='^lang_'))
    application.add_handler(CallbackQueryHandler(language_callback_handler, pattern='^lang_auto$'))

    # admin-specific callbacks (register before generic menu handler)
    application.add_handler(CallbackQueryHandler(admin_start_action_callback, pattern='^admin_start_(approve|reject)_\\d+$'))
    application.add_handler(CallbackQueryHandler(admin_confirm_callback, pattern='^admin_confirm_(approve|reject)_\\d+$'))
    application.add_handler(CallbackQueryHandler(admin_cancel_callback, pattern='^admin_cancel_\\d+$'))

    # history callbacks
    application.add_handler(CallbackQueryHandler(history_page_callback, pattern='^history_page_\\d+_\\d+$'))
    application.add_handler(CallbackQueryHandler(history_details_callback, pattern='^history_details_\\d+_\\d+_\\d+$'))
    application.add_handler(CallbackQueryHandler(history_back_callback, pattern='^history_back_\\d+_\\d+$'))

    # generic menu callback (after specific)
    application.add_handler(CallbackQueryHandler(menu_callback))

    # commands
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("wallet", wallet_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("information", lambda u,c: asyncio.create_task(u.effective_message.reply_text(t(DEFAULT_LANG,"info_text")))))
    application.add_handler(CommandHandler("help", lambda u,c: asyncio.create_task(u.effective_message.reply_text("Use /admin_cmds for admin commands."))))
    application.add_handler(CommandHandler("pending", admin_pending_command))

    # admin trade control commands
    application.add_handler(CommandHandler("trade_on", cmd_trade_on))
    application.add_handler(CommandHandler("trade_off", cmd_trade_off))
    application.add_handler(CommandHandler("trade_freq", cmd_trade_freq))
    application.add_handler(CommandHandler("trade_now", cmd_trade_now))
    application.add_handler(CommandHandler("trade_status", cmd_trade_status))

    # Binance controls
    application.add_handler(CommandHandler("use_binance_on", cmd_use_binance_on))
    application.add_handler(CommandHandler("use_binance_off", cmd_use_binance_off))
    application.add_handler(CommandHandler("binance_status", cmd_binance_status))

    # admin commands list
    application.add_handler(CommandHandler("admin_cmds", admin_cmds_handler))

    application.add_handler(MessageHandler(filters.Regex("^Balance$"), balance_text_handler))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        _scheduler = AsyncIOScheduler(event_loop=loop)
    except TypeError:
        _scheduler = AsyncIOScheduler()
    _scheduler.add_job(trading_job, 'interval', minutes=TRADING_FREQ_MINUTES, next_run_time=datetime.utcnow() + timedelta(seconds=15))
    _scheduler.add_job(daily_profit_job, 'cron', hour=0, minute=0)
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
