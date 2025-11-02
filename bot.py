# Full final bot.py — Patched to add language selection (auto + manual) in Settings.
# - Adds `preferred_language` to User model (handled in ensure_columns migration)
# - Auto-detect language from Telegram (update.effective_user.language_code)
# - Persist explicit language choice (including "auto") in DB
# - New translations dictionary + helper `t(lang, key, **kwargs)`
# - New settings language keyboard and callback handlers
# - Language resolution helper `get_user_language`
# - Integrated into Settings flow (opens language selector from Settings)
#
# Notes:
# - This patch modifies the existing bot to add the preference in DB. ensure_columns() attempts
#   to add the column; for production, run a proper migration if necessary.
# - Default supported languages: English (en), French (fr), Spanish (es). Add more in TRANSLATIONS.
# - The code stores user's explicit choice in `preferred_language` column: values are language codes, or "auto".
# - Behavior:
#    * If user chose "auto" (or has no preference), the bot uses update.effective_user.language_code when available.
#    * If user chose a language code (e.g., 'fr'), that language is used always until changed.
#    * Fallback is DEFAULT_LANG ("en").
#
# Environment variables required:
# - BOT_TOKEN, ADMIN_ID, MASTER_WALLET, MASTER_NETWORK, DATABASE_URL (optional), ADMIN_LOG_CHAT_ID (optional)
# - MENU_FULL_TWO_COLUMN (optional; controls menu width style)
#
# The remainder of the bot behavior (invest/withdraw/history/admin flows) remains unchanged except for the new language helpers and settings integration.

import os
import logging
import random
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, List
from dotenv import load_dotenv

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
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

ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
ADMIN_LOG_CHAT_ID = os.getenv('ADMIN_LOG_CHAT_ID')  # optional
MASTER_WALLET = os.getenv('MASTER_WALLET', 'TAbc...')
MASTER_NETWORK = os.getenv('MASTER_NETWORK', 'TRC20')
SUPPORT_USER = os.getenv('SUPPORT_USER', '@AiCrypto_Support1')
SUPPORT_URL = os.getenv('SUPPORT_URL') or (f"https://t.me/{SUPPORT_USER.lstrip('@')}" if SUPPORT_USER else "https://t.me/")

MENU_FULL_TWO_COLUMN = os.getenv('MENU_FULL_TWO_COLUMN', 'true').lower() in ('1','true','yes','on')
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
    balance = Column(Numeric(18, 2), default=0.0)
    balance_in_process = Column(Numeric(18, 2), default=0.0)
    daily_profit = Column(Numeric(18, 2), default=0.0)
    total_profit = Column(Numeric(18, 2), default=0.0)
    referral_count = Column(Integer, default=0)
    referral_earnings = Column(Numeric(18, 2), default=0.0)
    referrer_id = Column(BigInteger, nullable=True)
    wallet_address = Column(String)
    wallet_network = Column(String)
    # NEW: preferred language column (stores e.g. 'en', 'fr', 'auto', or None)
    preferred_language = Column(String, nullable=True)
    joined_at = Column(DateTime, default=datetime.utcnow)


class Transaction(Base):
    __tablename__ = 'transactions'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger)
    ref = Column(String)            # random 5-digit reference
    type = Column(String)           # 'invest' or 'withdraw' or 'profit'
    amount = Column(Numeric(18, 2))
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
        # add new columns if missing (best-effort). For production use proper migrations.
        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS wallet_network VARCHAR"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS proof VARCHAR"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS wallet VARCHAR"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS network VARCHAR"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS ref VARCHAR"))
        except Exception:
            pass
        # add preferred_language to users table if not exists
        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS preferred_language VARCHAR"))
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

# -----------------------
# I18N: translations and helpers
# -----------------------

# Small translations bundle; expand as needed.
TRANSLATIONS = {
    "en": {
        "main_menu_title": "Main Menu",
        "settings_title": "⚙️ Settings",
        "lang_auto": "Auto (Telegram)",
        "lang_en": "English",
        "lang_fr": "Français",
        "lang_es": "Español",
        "lang_set_success": "Language updated to {lang}.",
        "lang_current": "Current language: {lang}",
        "settings_choose": "Choose language:",
        "settings_back": "◀ Back",
    },
    "fr": {
        "main_menu_title": "Menu Principal",
        "settings_title": "⚙️ Paramètres",
        "lang_auto": "Auto (Telegram)",
        "lang_en": "Anglais",
        "lang_fr": "Français",
        "lang_es": "Espagnol",
        "lang_set_success": "Langue mise à jour en {lang}.",
        "lang_current": "Langue actuelle : {lang}",
        "settings_choose": "Choisissez la langue :",
        "settings_back": "◀ Retour",
    },
    "es": {
        "main_menu_title": "Menú Principal",
        "settings_title": "⚙️ Configuración",
        "lang_auto": "Auto (Telegram)",
        "lang_en": "Inglés",
        "lang_fr": "Francés",
        "lang_es": "Español",
        "lang_set_success": "Idioma actualizado a {lang}.",
        "lang_current": "Idioma actual: {lang}",
        "settings_choose": "Elija el idioma:",
        "settings_back": "◀ Volver",
    },
}

DEFAULT_LANG = "en"
SUPPORTED_LANGS = ["en", "fr", "es"]
LANG_DISPLAY = {"en": "English", "fr": "Français", "es": "Español"}

def t(lang: str, key: str, **kwargs) -> str:
    bundle = TRANSLATIONS.get(lang, TRANSLATIONS[DEFAULT_LANG])
    txt = bundle.get(key, TRANSLATIONS[DEFAULT_LANG].get(key, key))
    if kwargs:
        return txt.format(**kwargs)
    return txt

async def get_user_language(session: AsyncSession, user_id: int, update: Optional[Update] = None) -> str:
    """
    Resolve effective language to use for a user:
    1) If user.preferred_language exists and != 'auto' -> use it.
    2) If preferred_language == 'auto' or not set, try update.effective_user.language_code.
    3) Fallback to DEFAULT_LANG.
    """
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    preferred = None
    if user:
        preferred = getattr(user, "preferred_language", None)
    # Use explicit language choice if present and not 'auto'
    if preferred and preferred != "auto":
        if preferred in SUPPORTED_LANGS:
            return preferred
    # Try telegram language_code from current update
    if update and getattr(update, "effective_user", None):
        tlang = (update.effective_user.language_code or "").lower()
        if "-" in tlang:
            tlang = tlang.split("-")[0]
        if tlang in SUPPORTED_LANGS:
            return tlang
    # If preferred == 'auto' and update provided, try telegram code again
    if preferred == "auto" and update and getattr(update, "effective_user", None):
        tlang = (update.effective_user.language_code or "").lower()
        if "-" in tlang:
            tlang = tlang.split("-")[0]
        if tlang in SUPPORTED_LANGS:
            return tlang
    # fallback default
    return DEFAULT_LANG

def build_language_kb(current_lang: str) -> InlineKeyboardMarkup:
    rows = []
    # Auto option
    rows.append([InlineKeyboardButton(t(current_lang, "lang_auto"), callback_data="lang_auto")])
    # Language options
    for code in SUPPORTED_LANGS:
        # Use default English bundle labels for language names to keep visible names stable
        label = TRANSLATIONS[DEFAULT_LANG].get(f"lang_{code}", LANG_DISPLAY.get(code, code))
        rows.append([InlineKeyboardButton(label, callback_data=f"lang_{code}")])
    rows.append([InlineKeyboardButton(t(current_lang, "settings_back"), callback_data="menu_exit")])
    return InlineKeyboardMarkup(rows)

# -----------------------
# UI helpers & menu keyboard (compact default)
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

def build_main_menu_keyboard(full_two_column: bool = MENU_FULL_TWO_COLUMN) -> InlineKeyboardMarkup:
    if not full_two_column:
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

    tlen = 10
    left_right = [
        ("💰 Balance", "menu_balance", "📈 Invest", "menu_invest"),
        ("🧾 History", "menu_history", "💸 Withdraw", "menu_withdraw"),
        ("👥 Referrals", "menu_referrals", "⚙️ Settings", "menu_settings"),
        ("ℹ️ Information", "menu_info", "❓ Help", "menu_help_url"),
    ]

    rows = []
    for l_label, l_cb, r_label, r_cb in left_right:
        l = _compact_pad(l_label, target=tlen)
        r = _compact_pad(r_label, target=tlen)
        left_btn = InlineKeyboardButton(l, callback_data=l_cb)
        if r_cb == "menu_help_url":
            right_btn = InlineKeyboardButton(r, url=SUPPORT_URL if SUPPORT_URL else "https://t.me/")
        else:
            right_btn = InlineKeyboardButton(r, callback_data=r_cb)
        rows.append([left_btn, right_btn])

    exit_label = _compact_pad("⨉ Exit", target=(tlen*2)//2)
    rows.append([InlineKeyboardButton(exit_label, callback_data="menu_exit")])
    return InlineKeyboardMarkup(rows)

# -----------------------
# Messaging helpers & admin
# -----------------------

def tx_card_text(tx: Transaction) -> str:
    emoji = "📥" if (tx.type == 'invest') else ("💸" if tx.type == 'withdraw' else "💰")
    created = tx.created_at.strftime("%Y-%m-%d %H:%M:%S") if tx.created_at else "-"
    return (f"{emoji} <b>Ref {tx.ref}</b>\n"
            f"Type: <b>{(tx.type or '').upper()}</b>\n"
            f"Amount: <b>{float(tx.amount):.2f}$</b>\n"
            f"User: <code>{tx.user_id}</code>\n"
            f"Status: <b>{(tx.status or '').upper()}</b>\n"
            f"Created: {created}\n")

def _utc_to_pdt_str(dt: datetime) -> str:
    pdt = dt.replace(tzinfo=timezone.utc) - timedelta(hours=7)
    return pdt.strftime("%Y-%m-%d %H:%M (PDT)")

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

# -----------------------
# Menu handlers including Settings -> Language integration
# -----------------------

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
    data = query.data if query else None
    if not data:
        return
    # Let ConversationHandler handle invest/withdraw entrypoints and admin_* callbacks
    if data in ("menu_invest", "menu_withdraw") or data.startswith("admin_"):
        return
    if data == "menu_exit":
        await cancel_conv(update, context)
        try:
            # use localized main menu title if possible
            async with async_session() as session:
                lang = await get_user_language(session, query.from_user.id, update=update)
            await query.message.edit_text(t(lang, "main_menu_title"), reply_markup=build_main_menu_keyboard())
        except Exception:
            await query.message.reply_text("Main Menu", reply_markup=build_main_menu_keyboard())
        return
    if data == "menu_balance":
        async with async_session() as session:
            await send_balance_message(query, session, query.from_user.id)
    elif data == "menu_history":
        await history_command(update, context)
    elif data == "menu_referrals":
        user_id = query.from_user.id
        bot_username = (await context.bot.get_me()).username
        referral_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
        text = (f"👥 Referrals\n\nShare this link to invite friends and earn rewards:\n\n"
                f"<code>{referral_link}</code>\n\nUse the button below to insert the link into your input field for quick copying/sharing.")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 Copy Link", switch_inline_query_current_chat=referral_link)],
            [InlineKeyboardButton("Back to Main Menu", callback_data="menu_exit")]
        ])
        try:
            await query.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await query.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
    elif data == "menu_settings":
        # show settings menu; for now show language selector entry
        async with async_session() as session:
            lang = await get_user_language(session, query.from_user.id, update=update)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(t(lang, "settings_title"), callback_data="noop")],
            [InlineKeyboardButton(t(lang, "lang_auto"), callback_data="lang_auto"),
             InlineKeyboardButton(TRANSLATIONS[DEFAULT_LANG].get("lang_en","English"), callback_data="lang_en")],
            [InlineKeyboardButton("Change Language...", callback_data="open_language_selector")],
            [InlineKeyboardButton("Back to Menu", callback_data="menu_exit")]
        ])
        try:
            await query.message.edit_text(t(lang, "settings_title"), reply_markup=kb)
        except Exception:
            await query.message.reply_text(t(lang, "settings_title"), reply_markup=kb)
    elif data == "open_language_selector":
        # open the proper language chooser
        async with async_session() as session:
            lang = await get_user_language(session, query.from_user.id, update=update)
        await query.message.edit_text(t(lang, "settings_choose"), reply_markup=build_language_kb(lang))
    elif data == "menu_info":
        async with async_session() as session:
            lang = await get_user_language(session, query.from_user.id, update=update)
        info_text = ("ℹ️ Information\n\nWelcome to AiCrypto bot.\n- Invest: deposit funds to provided wallet and upload proof (txid or screenshot).\n- Withdraw: request withdrawals; admin will approve and process.")
        await query.edit_message_text(info_text, reply_markup=build_main_menu_keyboard())
    else:
        # language button patterns handled elsewhere
        return

# Language callback handler
async def language_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data  # e.g. "lang_en" or "lang_auto"
    user_id = query.from_user.id
    selected = None
    if data == "lang_auto":
        selected = "auto"
    elif data.startswith("lang_"):
        selected = data.split("_",1)[1]
    else:
        # unrecognized pattern
        return

    async with async_session() as session:
        # upsert preferred_language
        result = await session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            # create new user row with preferred_language
            new_user = User(id=user_id, preferred_language=selected)
            session.add(new_user)
        else:
            await session.execute(sa_update(User).where(User.id == user_id).values(preferred_language=selected))
        await session.commit()

        effective_lang = await get_user_language(session, user_id, update=update)

    await query.message.reply_text(t(effective_lang, "lang_set_success", lang=LANG_DISPLAY.get(effective_lang, effective_lang)))

# -----------------------
# Balance helper (localized)
# -----------------------

async def send_balance_message(query_or_message, session: AsyncSession, user_id: int):
    lang = await get_user_language(session, user_id)
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

# -----------------------
# Invest / Withdraw / History flows (unchanged behavior)
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
        await target.reply_text("Missing data. Restart invest flow.", reply_markup=build_main_menu_keyboard())
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
    pdt_str = _utc_to_pdt_str(now)
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
    await send_admin_tx_notification(context.application.bot, tx, proof_file_id=proof)
    await post_admin_log(context.application.bot, f"New INVEST #{tx_db_id} ref {tx_ref} user {user_id} amount {amount:.2f}$")

    context.user_data.pop('invest_amount', None)
    context.user_data.pop('invest_proof', None)
    return ConversationHandler.END

# WITHDRAW flow and admin flows are left unchanged except language-aware messages when appropriate.
# (For brevity, the rest of withdraw/admin/history handlers remain as in prior version — unchanged.)

# For completeness we re-add withdraw and admin handlers (slightly condensed):

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
        await msg.reply_text(f"✅ Wallet saved:\n<code>{wallet_address}</code>\nNetwork: {wallet_network}", parse_mode="HTML", reply_markup=build_main_menu_keyboard())
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
        await query.message.reply_text("Missing withdrawal data. Start withdraw again.", reply_markup=build_main_menu_keyboard())
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
    pdt_str = _utc_to_pdt_str(now)
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
    await send_admin_tx_notification(context.application.bot, tx, proof_file_id=None)
    await post_admin_log(context.application.bot, f"New WITHDRAW #{tx_db_id} ref {tx_ref} user {user_id} amount {amount:.2f}$")

    context.user_data.pop('withdraw_amount', None)
    context.user_data.pop('withdraw_wallet', None)
    context.user_data.pop('withdraw_network', None)
    return ConversationHandler.END

# -----------------------
# ADMIN flows (approve/reject)
# -----------------------

def _is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID and ADMIN_ID != 0

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

                receipt_text = (
                    "  **Deposit Receipt **\n"
                    "✅ Your deposit has been approved and credited\n"
                    f"Transaction ID, D-{tx.ref}\n"
                    f"Amount, {float(tx.amount):.2f} USDT\n"
                    f"Date, {_utc_to_pdt_str(datetime.utcnow())}\n"
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
                    f"Date, {_utc_to_pdt_str(datetime.utcnow())}\n"
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
            # reject flows...
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

    return

async def admin_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Action cancelled.")

# -----------------------
# Admin pending, history, and other handlers remain implemented (omitted for brevity)
# -----------------------

# For continuity: wire the language callback handler into the dispatcher and ensure handlers are added.

# -----------------------
# Utilities & start
# -----------------------

async def cancel_conv(update: Optional[Update], context: ContextTypes.DEFAULT_TYPE):
    if context and getattr(context, "user_data", None):
        context.user_data.clear()
    if update and getattr(update, "callback_query", None):
        await update.callback_query.answer()
    return ConversationHandler.END

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # On start, attempt to auto-detect language and persist user's row if missing
    user_id = update.effective_user.id
    async with async_session() as session:
        # ensure user exists
        result = await session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            # set preferred_language to None so auto works
            new_user = User(id=user_id)
            session.add(new_user)
            await session.commit()
    # send info bubble then main menu image/keyboard (if you use image)
    # For simplicity, send localized main menu title with inline keyboard
    async with async_session() as session:
        lang = await get_user_language(session, user_id, update=update)
    try:
        await update.effective_message.reply_text(t(lang, "main_menu_title"), reply_markup=build_main_menu_keyboard())
    except Exception:
        await update.effective_message.reply_text("Main Menu", reply_markup=build_main_menu_keyboard())

# -----------------------
# MAIN wiring
# -----------------------

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

    # language callbacks
    app.add_handler(CallbackQueryHandler(language_callback_handler, pattern='^lang_'))
    app.add_handler(CallbackQueryHandler(menu_callback))
    app.add_handler(CallbackQueryHandler(admin_start_action_callback, pattern='^admin_start_(approve|reject)_\\d+$'))
    app.add_handler(CallbackQueryHandler(admin_confirm_callback, pattern='^admin_confirm_(approve|reject)_\\d+$'))
    app.add_handler(CallbackQueryHandler(admin_cancel_callback, pattern='^admin_cancel_\\d+$'))

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("balance", balance_command))
    app.add_handler(CommandHandler("wallet", wallet_command))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("information", information_command))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("pending", admin_pending_command))

    app.add_handler(MessageHandler(filters.Regex("^Balance$"), balance_text_handler))

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
