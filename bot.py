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
SUPPORT_USER = '@AiCrypto_Support1'
DATABASE_URL = os.getenv('DATABASE_URL')

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === DATABASE ===
Base = declarative_base()

# === FORCE psycopg DRIVER ===
DATABASE_URL = os.getenv('DATABASE_URL')

if DATABASE_URL:
    # Railway gives: postgres://user:pass@host:port/db
    if DATABASE_URL.startswith("postgres://"):
        # CRITICAL: Use +psycopg:// (NOT psycopg2)
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
    elif DATABASE_URL.startswith("postgresql://"):
        # If already postgresql://, force +psycopg
        if "+psycopg" not in DATABASE_URL:
            DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
else:
    DATABASE_URL = "sqlite+aiosqlite:///bot.db"

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    future=True
)
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

# Init DB
import asyncio
async def _create_all_with_timeout(engine, timeout=10):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def init_db(retries: int = 5, backoff: float = 2.0, fallback_to_sqlite: bool = True):
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
            global engine, async_session, DATABASE_URL
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
    import sys
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
def build_inline_menu(full_width: bool = False) -> InlineKeyboardMarkup:
    if full_width:
        rows = [
            [InlineKeyboardButton("Balance", callback_data="menu_balance")],
            [InlineKeyboardButton("Invest", callback_data="menu_invest")],
            [InlineKeyboardButton("Withdraw", callback_data="menu_withdraw")],
            [InlineKeyboardButton("History", callback_data="menu_history")],
            [InlineKeyboardButton("Referrals", callback_data="menu_referrals")],
            [InlineKeyboardButton("Settings", callback_data="menu_settings")],
            [InlineKeyboardButton("Information", callback_data="menu_info")],
            [InlineKeyboardButton("Help", callback_data="menu_help")],
        ]
    else:
        rows = [
            [InlineKeyboardButton("Balance", callback_data="menu_balance"),
             InlineKeyboardButton("Invest", callback_data="menu_invest")],
            [InlineKeyboardButton("Withdraw", callback_data="menu_withdraw"),
             InlineKeyboardButton("History", callback_data="menu_history")],
            [InlineKeyboardButton("Referrals", callback_data="menu_referrals"),
             InlineKeyboardButton("Settings", callback_data="menu_settings")],
            [InlineKeyboardButton("Information", callback_data="menu_info"),
             InlineKeyboardButton("Help", callback_data="menu_help")],
        ]
    return InlineKeyboardMarkup(rows)

# === DAILY PROFIT + REFERRALS ===
async def daily_profit_job():
    async with async_session() as session:
        result = await session.execute(select(User))
        for user in result.scalars():
            total = float(user.balance or 0) + float(user.balance_in_process or 0)
            if total <= 0: continue
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
                await log_transaction(session, user.referrer_id, type='referral_profit', amount=bonus, status='credited')

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
        kb = build_inline_menu(full_width=False)
        await update.message.reply_text("Choose:", reply_markup=kb)

async def send_balance_message(query_or_message, session: AsyncSession, user_id: int):
    user = await get_user(session, user_id)
    msg = f"""
Balance: {user['balance']:.2f}$
In Process: {user['balance_in_process']:.2f}$
Daily Profit: {user['daily_profit']:.2f}$
Total Profit: {user['total_profit']:.2f}$
Referral Earnings: {user['referral_earnings']:.2f}$

Manager: {SUPPORT_USER}
    """.strip()
    try:
        await query_or_message.edit_message_text(msg, reply_markup=build_inline_menu(full_width=True))
    except Exception:
        await query_or_message.reply_text(msg, reply_markup=build_inline_menu(full_width=True))

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    async with async_session() as session:
        if data == "menu_balance":
            await send_balance_message(query, session, query.from_user.id)
        elif data == "menu_invest":
            await query.edit_message_text("Invest menu: choose a plan or enter amount.")
        elif data == "menu_withdraw":
            await query.edit_message_text("Withdraw menu: enter amount to withdraw.")
        elif data == "menu_history":
            await query.edit_message_text("Your history:\n(coming soon)")
        elif data == "menu_referrals":
            ref = await get_user(session, query.from_user.id)
            await query.edit_message_text(f"Referral count: {ref['referral_count']}\nReferral earnings: {ref['referral_earnings']:.2f}$")
        elif data == "menu_settings":
            await query.edit_message_text("Settings menu (profile, wallet address, network).")
        elif data == "menu_info":
            await query.edit_message_text("Information: bot rules, plans, and FAQ.")
        elif data == "menu_help":
            await query.edit_message_text(f"Contact support: {SUPPORT_USER}")
        else:
            await query.edit_message_text("Unknown action", reply_markup=build_inline_menu())

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
    # Add other handlers as needed...

    # Create and set an event loop for this thread (fixes RuntimeError on Python >=3.10)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Create AsyncIOScheduler and, for clarity, pass the loop explicitly if supported
    try:
        # Newer versions accept event_loop parameter name; keep fallback to default ctor
        scheduler = AsyncIOScheduler(event_loop=loop)
    except TypeError:
        scheduler = AsyncIOScheduler()

    scheduler.add_job(daily_profit_job, 'cron', hour=0, minute=0)
    scheduler.start()

    print("AiCrypto Bot STARTED – SQLAlchemy 2.0 + Python 3.13 (using InlineKeyboard)")
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
    # Initialize DB before starting the bot
    try:
        asyncio.run(init_db(retries=5, backoff=2.0, fallback_to_sqlite=True))
    except SystemExit:
        raise
    except Exception as e:
        logger.exception("Unexpected error during DB initialization: %s", e)
        raise

    main()
