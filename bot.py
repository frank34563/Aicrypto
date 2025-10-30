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

# CONFIG
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
MASTER_WALLET = os.getenv('MASTER_WALLET', 'TAbcDeF123...XyZ789')
SUPPORT_USER = '@AiCrypto_Support1'
DATABASE_URL = os.getenv('DATABASE_URL')  # Railway Postgres

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# STATES
AMOUNT, TX_PROOF, WITHDRAW_AMOUNT, WALLET_ADDR, NETWORK = range(5)

#DATABASE
Base = declarative_base()

if DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql", "postgresql+asyncpg")
    engine = create_async_engine(DATABASE_URL, echo=False)
else:
    engine = create_async_engine("sqlite+aiosqlite:///bot.db", echo=False)

async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

class User(Base):
    __tablename__ = 'users'
    id = Column(BigInteger, primary_key=True)
    balance = Column(DECIMAL(15,2), default=0.00)
    balance_in_process = Column(DECIMAL(15,2), default=0.00)
    daily_profit = Column(DECIMAL(15,2), default=0.00)
    total_profit = Column(DECIMAL(15,2), default=0.00)
    referral_count = Column(Integer, default=0)
    referral_earnings = Column(DECIMAL(15,2), default=0.00)
    referrer_id = Column(BigInteger)
    wallet_address = Column(String)
    network = Column(String)
    joined_at = Column(DateTime, default=datetime.utcnow)

class Transaction(Base):
    __tablename__ = 'transactions'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger)
    type = Column(String)
    amount = Column(DECIMAL(15,2))
    status = Column(String)
    txid = Column(String)
    wallet = Column(String)
    network = Column(String)
    screenshot_path = Column(String)
    admin_note = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

# Create tables
async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

import asyncio
asyncio.run(init_db())

# HELPERS
async def get_user(session: AsyncSession, user_id: int) -> Dict:
    stmt = select(User).where(User.id == user_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    if not user:
        user = User(id=user_id)
        session.add(user)
        await session.commit()
        return await get_user(session, user_id)
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

async def get_history(session: AsyncSession, user_id: int, limit: int = 5):
    stmt = select(Transaction).where(Transaction.user_id == user_id).order_by(Transaction.created_at.desc()).limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()

# DAILY PROFIT
async def daily_profit_job():
    async with async_session() as session:
        stmt = select(User)
        result = await session.execute(stmt)
        users = result.scalars().all()
        for user in users:
            total = float(user.balance or 0) + float(user.balance_in_process or 0)
            if total <= 0:
                continue
            profit = round(total * 0.015, 2)
            user.daily_profit = profit
            user.total_profit = float(user.total_profit or 0) + profit
            user.balance = float(user.balance or 0) + profit
            await session.merge(user)
            await session.commit()
            await log_transaction(session, user.id, 'profit', profit, 'credited')
            # Referral bonus
            if user.referrer_id:
                ref_bonus = round(profit * 0.01, 2)
                referrer = await get_user(session, user.referrer_id)
                await update_user(session, user.referrer_id, referral_earnings=referrer['referral_earnings'] + ref_bonus, balance=referrer['balance'] + ref_bonus)
                await log_transaction(session, user.referrer_id, 'referral_profit', ref_bonus, 'credited')

# MENUS
def main_menu():
    return ReplyKeyboardMarkup([
        ['Balance', 'Invest'],
        ['Withdraw', 'History'],
        ['Referrals', 'Settings'],
        ['Information', 'Help Up Arrow']
    ], resize_keyboard=True)

# HANDLERS
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    async with async_session() as session:
        if args and args[0].startswith('ref_'):
            ref_id = int(args[0][4:])
            if ref_id != user_id:
                await update_user(session, user_id, referrer_id=ref_id)
                referrer = await get_user(session, ref_id)
                await update_user(session, ref_id, referral_count=referrer['referral_count'] + 1)
                await update.message.reply_text('Welcome! Joined via referral - bonus activated!')
        await update.message.reply_text('Welcome to AiCrypto!', reply_markup=main_menu())

# Add other handlers similarly, using async with async_session() as session for DB operations.

# MAIN
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Handlers...
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^Balance$"), show_balance))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(daily_profit_job, 'cron', hour=0, minute=0)
    scheduler.start()

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
