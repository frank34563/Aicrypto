# https://github.com/Princegluck/A/blob/main/bot.py
# Full patched file: default paired (two-column) menu layout, with env control to switch to full-width stacked
import os
import logging
from datetime import datetime
from typing import Dict
from dotenv import load_dotenv

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)

from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime,
    BigInteger, select, update, func, Numeric
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

# === CONFIG ===
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set!")

ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
MASTER_WALLET = os.getenv('MASTER_WALLET', 'TAbc...')
SUPPORT_USER = os.getenv('SUPPORT_USER', '@AiCrypto_Support1')
SUPPORT_URL = os.getenv('SUPPORT_URL') or (f"https://t.me/{SUPPORT_USER.lstrip('@')}" if SUPPORT_USER else None)

# MENU_FULL_WIDTH controls stacked full-width layout when true.
# For "pairs" (two-column) layout set MENU_FULL_WIDTH=false (this file defaults to pairs)
MENU_FULL_WIDTH = os.getenv('MENU_FULL_WIDTH', 'false').strip().lower() in ('1', 'true', 'yes', 'on')

DATABASE_URL = os.getenv('DATABASE_URL')

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === DATABASE ===
Base = declarative_base()

# === FORCE psycopg DRIVER ===
DATABASE_URL = os.getenv('DATABASE_URL')
if DATABASE_URL:
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
    elif DATABASE_URL.startswith("postgresql://") and "+psycopg" not in DATABASE_URL:
        DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
else:
    DATABASE_URL = "sqlite+aiosqlite:///bot.db"

engine = create_async_engine(DATABASE_URL, echo=False, future=True)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

class User(Base):
    __tablename__ = 'users'
    id = Column(BigInteger, primary_key=True)
    balance = Column(Numeric(15,2), default=0.0)
    balance_in_process = Column(Numeric(15,2), default=0.0)
    daily_profit = Column(Numeric(15,2), default=0.0)
    total_profit = Column(Numeric(15,2), default=0.0)
    referral_count = Column(Integer, default=0)
    referral_earnings = Column(Numeric(15,2), default=0.0)
    referrer_id = Column(BigInteger, nullable=True)
    wallet_address = Column(String)
    network = Column(String)
    joined_at = Column(DateTime, default=datetime.utcnow)

class Transaction(Base):
    __tablename__ = 'transactions'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger)
    type = Column(String)
    amount = Column(Numeric(15,2))
    status = Column(String)
    txid = Column(String)
    wallet = Column(String)
    network = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

# Init DB helpers and resilient init
import asyncio, sys, time

async def _create_all_with_timeout(engine_to_use):
    async with engine_to_use.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def init_db(retries: int = 5, backoff: float = 2.0, fallback_to_sqlite: bool = True):
    global engine, async_session, DATABASE_URL

    last_exc = None
    attempt = 0
    current_engine = engine

    while attempt < retries:
        attempt += 1
        try:
            await _create_all_with_timeout(current_engine)
            logger.info("Database initialized successfully.")
            return
        except Exception as e:
            last_exc = e
            logger.warning("Database init attempt %d/%d failed: %s", attempt, retries, e)
            wait = backoff * (2 ** (attempt - 1))
            logger.info("Waiting %.1f seconds before next DB init attempt...", wait)
            await asyncio.sleep(wait)

    logger.error("All %d database init attempts failed. Last error: %s", retries, last_exc)

    if fallback_to_sqlite:
        try:
            sqlite_url = "sqlite+aiosqlite:///bot_fallback.db"
            logger.warning("Falling back to sqlite DB at %s for local/dev (NOT recommended for production).", sqlite_url)
            from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine
            DATABASE_URL = sqlite_url
            engine = _create_async_engine(DATABASE_URL, echo=False, future=True)
            async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            await _create_all_with_timeout(engine)
            logger.info("Fallback sqlite database initialized. Bot will continue using sqlite.")
            return
        except Exception as e2:
            logger.exception("Fallback to sqlite failed: %s", e2)

    logger.critical("Unable to initialize database and fallback failed — exiting.")
    await asyncio.sleep(0.1)
    sys.exit(1)

# === HELPERS ===
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
    await session.execute(update(User).where(User.id == user_id).values(**kwargs))
    await session.commit()

async def log_transaction(session: AsyncSession, **data):
    session.add(Transaction(**data))
    await session.commit()

# === STATES ===
(AMOUNT, TX_PROOF, WITHDRAW_AMOUNT, WALLET_ADDR, NETWORK) = range(5)

# === MENUS ===
def build_inline_menu(full_width: bool, support_url: str | None):
    """
    Build a menu either in full-width stacked rows (if full_width True)
    or in paired two-column layout (if full_width False).
    This file defaults to paired layout (MENU_FULL_WIDTH=False).
    """
    if full_width:
        rows = [
            [InlineKeyboardButton("💰 Balance", callback_data="menu_balance")],
            [InlineKeyboardButton("📈 Invest", callback_data="menu_invest")],
            [InlineKeyboardButton("🧾 History", callback_data="menu_history")],
            [InlineKeyboardButton("💸 Withdraw", callback_data="menu_withdraw")],
            [InlineKeyboardButton("👥 Referrals", callback_data="menu_referrals")],
            [InlineKeyboardButton("⚙️ Settings", callback_data="menu_settings")],
            [InlineKeyboardButton("ℹ️ Information", callback_data="menu_info")],
        ]
        if support_url:
            rows.append([InlineKeyboardButton("❓ Help", url=support_url)])
        else:
            rows.append([InlineKeyboardButton("❓ Help", callback_data="menu_help")])
    else:
        # Paired two-column layout (menu should be in pairs)
        rows = [
            [InlineKeyboardButton("💰 Balance", callback_data="menu_balance"),
             InlineKeyboardButton("📈 Invest", callback_data="menu_invest")],
            [InlineKeyboardButton("🧾 History", callback_data="menu_history"),
             InlineKeyboardButton("💸 Withdraw", callback_data="menu_withdraw")],
            [InlineKeyboardButton("👥 Referrals", callback_data="menu_referrals"),
             InlineKeyboardButton("⚙️ Settings", callback_data="menu_settings")],
            [InlineKeyboardButton("ℹ️ Information", callback_data="menu_info"),
             InlineKeyboardButton("❓ Help", url=support_url if support_url else "https://t.me/")],
        ]
    return InlineKeyboardMarkup(rows)

# === DAILY PROFIT + REFERRALS ===
async def daily_profit_job():
    async with async_session() as session:
        result = await session.execute(select(User))
        for user in result.scalars():
            total = float(user.balance or 0) + float(user.balance_in_process or 0)
            if total <= 0:
                continue
            profit = round(total * 0.015, 2)
            await update_user(session, user.id,
                daily_profit=profit,
                total_profit=float(user.total_profit or 0) + profit,
                balance=float(user.balance or 0) + profit
            )
            await log_transaction(session, user_id=user.id, type='profit', amount=profit, status='credited')
            if user.referrer_id:
                bonus = round(profit * 0.01, 2)
                ref = await get_user(session, user.referrer_id)
                await update_user(session, user.referrer_id,
                    referral_earnings=ref['referral_earnings'] + bonus,
                    balance=ref['balance'] + bonus
                )
                await log_transaction(session, user_id=user.referrer_id, type='referral_profit', amount=bonus, status='credited')

# === HANDLERS ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    async with async_session() as session:
        if args and args[0].startswith('ref_'):
            try:
                ref_id = int(args[0][4:])
                if ref_id != user_id:
                    await update_user(session, user_id, referrer_id=ref_id)
                    ref = await get_user(session, ref_id)
                    await update_user(session, ref_id, referral_count=ref['referral_count'] + 1)
                    await update.message.reply_text("Welcome! Referral bonus activated!")
            except Exception:
                pass
        else:
            await update.message.reply_text("Welcome to AiCrypto!")

        kb = build_inline_menu(full_width=MENU_FULL_WIDTH, support_url=SUPPORT_URL)
        await update.message.reply_text("Choose:", reply_markup=kb)

async def send_balance_message(query_or_message, session: AsyncSession, user_id: int):
    user = await get_user(session, user_id)
    msg = (
        f"💎 <b>Balance</b>\n"
        f"Your Balance: <b>{user['balance']:.2f}$</b>\n"
        f"Balance in process: <b>{user['balance_in_process']:.2f}$</b>\n"
        f"Daily Profit: <b>{user['daily_profit']:.2f}$</b>\n"
        f"Total Profit: <b>{user['total_profit']:.2f}$</b>\n\n"
        f"Your personal manager: <b>{SUPPORT_USER}</b>"
    )
    try:
        await query_or_message.edit_message_text(msg, reply_markup=build_inline_menu(full_width=MENU_FULL_WIDTH, support_url=SUPPORT_URL), parse_mode="HTML")
    except Exception:
        await query_or_message.reply_text(msg, reply_markup=build_inline_menu(full_width=MENU_FULL_WIDTH, support_url=SUPPORT_URL), parse_mode="HTML")

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    async with async_session() as session:
        if data == "menu_balance":
            await send_balance_message(query, session, query.from_user.id)
        elif data == "menu_invest":
            await query.edit_message_text("📈 <b>Invest</b>\nChoose a plan or enter an amount.", parse_mode="HTML")
        elif data == "menu_withdraw":
            await query.edit_message_text("💸 <b>Withdraw</b>\nEnter amount to withdraw.", parse_mode="HTML")
        elif data == "menu_history":
            await query.edit_message_text("🧾 <b>History</b>\nYour operations history (coming soon).", parse_mode="HTML")
        elif data == "menu_referrals":
            ref = await get_user(session, query.from_user.id)
            await query.edit_message_text(f"👥 <b>Referrals</b>\nCount: {ref['referral_count']}\nEarnings: {ref['referral_earnings']:.2f}$", parse_mode="HTML")
        elif data == "menu_settings":
            await query.edit_message_text("⚙️ <b>Settings</b>\nProfile, wallet address, network.", parse_mode="HTML")
        elif data == "menu_info":
            await query.edit_message_text("ℹ️ <b>Information</b>\nBot rules, plans, and FAQ.", parse_mode="HTML")
        elif data == "menu_help":
            # Fallback when Help is created as a callback (should be URL in paired layout)
            await query.edit_message_text(f"❓ <b>Help</b>\nContact support: {SUPPORT_USER}", parse_mode="HTML")
        else:
            await query.edit_message_text("Unknown action", reply_markup=build_inline_menu(full_width=MENU_FULL_WIDTH, support_url=SUPPORT_URL))

# Keep a text handler fallback (for typed commands like "Balance")
async def balance_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        await send_balance_message(update.message, session, update.effective_user.id)

# === MAIN ===
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_callback))
    app.add_handler(MessageHandler(filters.Regex("^Balance$"), balance_text_handler))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        scheduler = AsyncIOScheduler(event_loop=loop)
    except TypeError:
        scheduler = AsyncIOScheduler()

    scheduler.add_job(daily_profit_job, 'cron', hour=0, minute=0)
    scheduler.start()

    print("AiCrypto Bot STARTED – SQLAlchemy 2.0 + Python 3.13 (paired menu layout)")
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        try:
            loop.stop()
            loop.close()
        except Exception:
            pass

if __name__ == '__main__':
    try:
        asyncio.run(init_db(retries=5, backoff=2.0, fallback_to_sqlite=True))
    except SystemExit:
        raise
    except Exception as e:
        logger.exception("Unexpected error during DB initialization: %s", e)
        raise

    main()
