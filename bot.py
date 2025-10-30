import os
import logging
from datetime import datetime
from typing import Dict
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
    BigInteger, select, update, func, text
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.dialects.postgresql import DECIMAL

load_dotenv()

# === CONFIG ===
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN missing!")

ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
MASTER_WALLET = os.getenv('MASTER_WALLET', 'TAbcDeF...')
SUPPORT_USER = '@AiCrypto_Support1'
DATABASE_URL = os.getenv('DATABASE_URL')

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === DATABASE ===
Base = declarative_base()

# Fix for Railway: postgres:// → postgresql+psycopg://
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)

engine = create_async_engine(DATABASE_URL or "sqlite+aiosqlite:///bot.db")
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
    created_at = Column(DateTime, default=datetime.utcnow)

# Init DB
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
        return await get_user(session, user_id)
    return {col.name: getattr(user, col.name) for col in user.__table__.columns}

async def update_user(session: AsyncSession, user_id: int, **kwargs):
    await session.execute(update(User).where(User.id == user_id).values(**kwargs))
    await session.commit()

async def log_transaction(session: AsyncSession, **data):
    session.add(Transaction(**data))
    await session.commit()

# === STATES ===
(AMOUNT, TX_PROOF, WITHDRAW_AMOUNT, WALLET_ADDR, NETWORK) = range(5)

# === MENUS ===
def main_menu():
    return ReplyKeyboardMarkup([
        ['Balance', 'Invest'], ['Withdraw', 'History'],
        ['Referrals', 'Settings'], ['Information', 'Help']
    ], resize_keyboard=True)

# === DAILY PROFIT ===
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
            # Referral bonus
            if user.referrer_id:
                bonus = round(profit * 0.01, 2)
                ref = await get_user(session, user.referrer_id)
                await update_user(session, user.referrer_id,
                    referral_earnings=ref['referral_earnings'] + bonus,
                    balance=ref['balance'] + bonus
                )
                await log_transaction(session, user_id=user.referrer_id, type='referral', amount=bonus, status='credited')

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
            except: pass
        else:
            await update.message.reply_text("Welcome to AiCrypto!")
        await update.message.reply_text("Choose:", reply_markup=main_menu())

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        user = await get_user(session, update.effective_user.id)
        msg = f"""
Balance: {user['balance']:.2f}$
In Process: {user['balance_in_process']:.2f}$
Daily Profit: {user['daily_profit']:.2f}$
Total Profit: {user['total_profit']:.2f}$
Referral Earnings: {user['referral_earnings']:.2f}$
        """.strip()
        await update.message.reply_text(msg)

# === MAIN ===
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^Balance$"), balance))
    # Add other handlers...

    scheduler = AsyncIOScheduler()
    scheduler.add_job(daily_profit_job, 'cron', hour=0, minute=0)
    scheduler.start()

    print("AiCrypto Bot STARTED – psycopg[binary] + Python 3.13")
    app.run_polling()

if __name__ == '__main__':
    main()
