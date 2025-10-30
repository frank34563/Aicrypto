import os
import logging
from datetime import datetime
from typing import Dict, Any
from dotenv import load_dotenv

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from telegram import (
    Update, ReplyKeyboardMarkup, InlineKeyboardMarkup,
    InlineKeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes, ConversationHandler
)

from sqlalchemy import (
    create_engine, Column, Integer, Float, String, DateTime,
    BigInteger, select, update, insert, func
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.dialects.postgresql import DECIMAL

load_dotenv()

# === CONFIG ===
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set!")

ADMIN_ID = int(os.getenv('ADMIN_ID', 0))
MASTER_WALLET = os.getenv('MASTER_WALLET', 'TAbcDeF123...XyZ789')
SUPPORT_USER = '@AiCrypto_Support1'
DATABASE_URL = os.getenv('DATABASE_URL')  # Railway: postgres://... → use asyncpg

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === DATABASE ===
Base = declarative_base()

if DATABASE_URL:
    # Railway Postgres → use asyncpg
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://")
    engine = create_async_engine(DATABASE_URL, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
else:
    # Local fallback: SQLite
    engine = create_async_engine("sqlite+aiosqlite:///bot.db", echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

class User(Base):
    __tablename__ = 'users'
    id = Column(BigInteger, primary_key=True)
    balance = Column(DECIMAL(15,2), default=0.0)
    balance_in_process = Column(DECIMAL(15,2), default=0.0)
    daily_profit = Column(DECIMAL(15,2), default=0.0)
    total_profit = Column(DECIMAL(15,2), default=0.0)
    referral_count = Column(Integer, default=0)
    referral_earnings = Column(DECIMAL(15,2), default=0.0)
    referrer_id = Column(BigInteger, nullable=True)
    wallet_address = Column(String, nullable=True)
    network = Column(String, nullable=True)
    joined_at = Column(DateTime, default=datetime.utcnow)

class Transaction(Base):
    __tablename__ = 'transactions'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger)
    type = Column(String)
    amount = Column(DECIMAL(15,2))
    status = Column(String)
    txid = Column(String, nullable=True)
    wallet = Column(String, nullable=True)
    network = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

# Create tables
import asyncio
async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

asyncio.run(init_db())

# === HELPERS ===
async def get_user(session: AsyncSession, user_id: int) -> Dict:
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        user = User(id=user_id)
        session.add(user)
        await session.commit()
    return {
        'id': user.id,
        'balance': float(user.balance or 0),
        'balance_in_process': float(user.balance_in_process or 0),
        'daily_profit': float(user.daily_profit or 0),
        'total_profit': float(user.total_profit or 0),
        'referral_count': user.referral_count or 0,
        'referral_earnings': float(user.referral_earnings or 0),
        'referrer_id': user.referrer_id,
        'wallet_address': user.wallet_address,
        'network': user.network
    }

async def update_user(session: AsyncSession, user_id: int, **kwargs):
    stmt = update(User).where(User.id == user_id).values(**kwargs)
    await session.execute(stmt)
    await session.commit()

async def log_transaction(session: AsyncSession, user_id: int, trans_type: str, amount: float, status: str, **extra):
    tx = Transaction(user_id=user_id, type=trans_type, amount=amount, status=status, **extra)
    session.add(tx)
    await session.commit()

# === STATES ===
AMOUNT, TX_PROOF, WITHDRAW_AMOUNT, WALLET_ADDR, NETWORK = range(5)

# === MENUS ===
def main_menu():
    return ReplyKeyboardMarkup([
        ['Balance', 'Invest'],
        ['Withdraw', 'History'],
        ['Referrals', 'Settings'],
        ['Information', 'Help Up Arrow']
    ], resize_keyboard=True)

# === DAILY PROFIT + REFERRALS ===
async def daily_profit_job():
    async with async_session() as session:
        result = await session.execute(select(User))
        users = result.scalars().all()
        for user in users:
            total = float(user.balance or 0) + float(user.balance_in_process or 0)
            if total <= 0:
                continue
            profit = round(total * 0.015, 2)
            await update_user(session, user.id,
                daily_profit=profit,
                total_profit=float(user.total_profit or 0) + profit,
                balance=float(user.balance or 0) + profit
            )
            await log_transaction(session, user.id, 'profit', profit, 'credited')
            # Referral bonus
            if user.referrer_id:
                ref_bonus = round(profit * 0.01, 2)
                referrer = await get_user(session, user.referrer_id)
                await update_user(session, user.referrer_id,
                    referral_earnings=referrer['referral_earnings'] + ref_bonus,
                    balance=referrer['balance'] + ref_bonus
                )
                await log_transaction(session, user.referrer_id, 'referral_profit', ref_bonus, 'credited')

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
                    await update_user(session, ref_id, referral_count=func.coalesce(User.referral_count, 0) + 1)
                    await update.message.reply_text("Welcome! You joined via referral – bonus activated!")
            except:
                pass
        else:
            await update.message.reply_text("Welcome to AiCrypto!")
        await update.message.reply_text("Use the menu:", reply_markup=main_menu())

async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        user = await get_user(session, update.effective_user.id)
        msg = f"""Balance

Your Balance: {user['balance']:.2f}$
In Process: {user['balance_in_process']:.2f}$
Daily Profit: {user['daily_profit']:.2f}$
Total Profit: {user['total_profit']:.2f}$
Referral Earnings: {user['referral_earnings']:.2f}$

Manager: {SUPPORT_USER}"""
        await update.message.reply_text(msg)

# === CONVERSATION HANDLERS (simplified for brevity) ===
# (Invest, Withdraw, Settings, Referrals, etc. – same logic, just use async session)

# === MAIN ===
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Add all handlers...
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^Balance$"), show_balance))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(daily_profit_job, 'cron', hour=0, minute=0)
    scheduler.start()

    print("AiCrypto Bot STARTED – Python 3.13 + asyncpg + Referrals")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
