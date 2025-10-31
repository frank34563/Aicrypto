# Final updated bot.py
# - Fixes admin approve/reject handling and sends user notifications on approve/reject
# - Adds a random 5-digit transaction reference ("ref") for deposit/withdrawal numbers shown to user
# - Ensures DB columns (users.wallet_network, transactions.proof, wallet, network, ref) are created at startup
# - Conversation flows: invest/withdraw as before, with improved notifications
# - Ready to copy/paste and deploy. Set BOT_TOKEN, ADMIN_ID (integer), MASTER_WALLET, MASTER_NETWORK, DATABASE_URL (optional).

import os
import logging
import random
from datetime import datetime
from typing import Dict, Optional, List
from dotenv import load_dotenv

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
    create_engine, Column, Integer, String, DateTime,
    BigInteger, select, update, func, Numeric, text
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

# === CONFIG ===
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set!")

ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))  # numeric Telegram id for admin
MASTER_WALLET = os.getenv('MASTER_WALLET', 'TAbc...')
MASTER_NETWORK = os.getenv('MASTER_NETWORK', 'TRC20')
SUPPORT_USER = os.getenv('SUPPORT_USER', '@AiCrypto_Support1')
SUPPORT_URL = os.getenv('SUPPORT_URL') or (f"https://t.me/{SUPPORT_USER.lstrip('@')}" if SUPPORT_USER else None)

MENU_FULL_WIDTH = os.getenv('MENU_FULL_WIDTH', 'false').strip().lower() in ('1', 'true', 'yes', 'on')
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
    balance = Column(Numeric(15, 2), default=0.0)
    balance_in_process = Column(Numeric(15, 2), default=0.0)
    daily_profit = Column(Numeric(15, 2), default=0.0)
    total_profit = Column(Numeric(15, 2), default=0.0)
    referral_count = Column(Integer, default=0)
    referral_earnings = Column(Numeric(15, 2), default=0.0)
    referrer_id = Column(BigInteger, nullable=True)
    wallet_address = Column(String)
    wallet_network = Column(String)  # ensure_columns will add if missing
    joined_at = Column(DateTime, default=datetime.utcnow)


class Transaction(Base):
    __tablename__ = 'transactions'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger)
    ref = Column(String)            # random 5-digit reference shown to users
    type = Column(String)           # 'invest' or 'withdraw'
    amount = Column(Numeric(15, 2))
    status = Column(String)         # 'pending','in_process','requested','credited','rejected','completed'
    proof = Column(String)          # txid or file_id
    wallet = Column(String)         # withdrawal wallet or deposit wallet used
    network = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


# DB init helpers
import asyncio, sys, time

async def _create_all_with_timeout(engine_to_use):
    async with engine_to_use.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def ensure_columns():
    """
    Ensure nullable columns that may be missing on older schemas exist.
    Uses ALTER TABLE ... ADD COLUMN IF NOT EXISTS for idempotence.
    """
    async with engine.begin() as conn:
        # users
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS wallet_network VARCHAR"))
        # transactions
        await conn.execute(text("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS proof VARCHAR"))
        await conn.execute(text("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS wallet VARCHAR"))
        await conn.execute(text("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS network VARCHAR"))
        await conn.execute(text("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS ref VARCHAR"))

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
            wait = backoff * (2 ** (attempt - 1))
            await asyncio.sleep(wait)

    logger.error("All %d database init attempts failed. Last error: %s", retries, last_exc)

    if fallback_to_sqlite:
        try:
            sqlite_url = "sqlite+aiosqlite:///bot_fallback.db"
            logger.warning("Falling back to sqlite DB at %s for local/dev.", sqlite_url)
            from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine
            DATABASE_URL = sqlite_url
            engine = _create_async_engine(DATABASE_URL, echo=False, future=True)
            async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            await _create_all_with_timeout(engine)
            try:
                await ensure_columns()
            except Exception as col_exc:
                logger.warning("ensure_columns on sqlite fallback warning: %s", col_exc)
            logger.info("Fallback sqlite database initialized.")
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
    await session.execute(update(User).where(User.id == user_id).values(**kwargs))
    await session.commit()

async def log_transaction(session: AsyncSession, **data):
    """
    Creates a transaction row. Generates a random 5-digit ref (string) if not provided.
    Returns the database id and the ref string.
    """
    if 'ref' not in data or not data.get('ref'):
        data['ref'] = f"{random.randint(10000,99999)}"
    tx = Transaction(**data)
    session.add(tx)
    await session.commit()
    await session.refresh(tx)
    return tx.id, data['ref']

# Conversation states
INVEST_AMOUNT, INVEST_PROOF, INVEST_CONFIRM, WITHDRAW_AMOUNT, WITHDRAW_WALLET, WITHDRAW_CONFIRM = range(6)

# UI helpers
def build_inline_menu(full_width: bool, support_url: Optional[str]):
    exit_row = [InlineKeyboardButton("⨉ Exit", callback_data="menu_exit")]
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
        rows.append(exit_row)
    else:
        rows = [
            [InlineKeyboardButton("💰 Balance", callback_data="menu_balance"),
             InlineKeyboardButton("📈 Invest", callback_data="menu_invest")],
            [InlineKeyboardButton("🧾 History", callback_data="menu_history"),
             InlineKeyboardButton("💸 Withdraw", callback_data="menu_withdraw")],
            [InlineKeyboardButton("👥 Referrals", callback_data="menu_referrals"),
             InlineKeyboardButton("⚙️ Settings", callback_data="menu_settings")],
            [InlineKeyboardButton("ℹ️ Information", callback_data="menu_info"),
             InlineKeyboardButton("❓ Help", url=support_url if support_url else "https://t.me/")],
            exit_row,
        ]
    return InlineKeyboardMarkup(rows)

def user_confirm_kb(prefix: str):
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ I sent the exact amount", callback_data=f"{prefix}_confirm_yes"),
                                 InlineKeyboardButton("❌ I did NOT send / Cancel", callback_data=f"{prefix}_confirm_no")]])

def admin_action_kb(tx_db_id: int):
    # admin buttons reference the DB id to allow unambiguous lookup
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve", callback_data=f"admin_approve_{tx_db_id}"),
                                  InlineKeyboardButton("❌ Reject", callback_data=f"admin_reject_{tx_db_id}")]])

# Scheduled job
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
                              balance=float(user.balance or 0) + profit)
            await log_transaction(session, user_id=user.id, type='profit', amount=profit, status='credited')

# Menu callback
async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data if query else None
    if query:
        await query.answer()
    if data in ("menu_invest", "menu_withdraw") or (data and data.startswith("admin_")):
        # let ConversationHandler or admin handler handle these
        logger.debug("menu_callback ignoring %s", data)
        return
    if data == "menu_exit":
        await cancel_conv(update, context)
        try:
            await query.message.edit_text("Main Menu", reply_markup=build_inline_menu(full_width=MENU_FULL_WIDTH, support_url=SUPPORT_URL))
        except Exception:
            await query.message.reply_text("Main Menu", reply_markup=build_inline_menu(full_width=MENU_FULL_WIDTH, support_url=SUPPORT_URL))
        return
    if data == "menu_balance":
        async with async_session() as session:
            await send_balance_message(query, session, query.from_user.id)
    elif data == "menu_history":
        await query.edit_message_text("🧾 History (coming soon)", reply_markup=build_inline_menu(full_width=MENU_FULL_WIDTH, support_url=SUPPORT_URL))
    elif data == "menu_referrals":
        user_id = query.from_user.id
        async with async_session() as session:
            ref = await get_user(session, user_id)
        ref_count = ref.get('referral_count', 0)
        ref_earn = float(ref.get('referral_earnings') or 0)
        bot_username = (await context.bot.get_me()).username
        referral_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
        text = (f"👥 Referrals\nCount: {ref_count}\nEarnings: {ref_earn:.2f}$\nShare your referral link:\n{referral_link}")
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=build_inline_menu(full_width=MENU_FULL_WIDTH, support_url=SUPPORT_URL))
    elif data == "menu_settings":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Set/Update Withdrawal Wallet", callback_data="settings_set_wallet")],
                                   [InlineKeyboardButton("Back to Main Menu", callback_data="menu_exit")]])
        await query.edit_message_text("⚙️ Settings\nChoose an action:", reply_markup=kb)
    elif data == "settings_set_wallet":
        await query.message.reply_text("Send your withdrawal wallet address and optional network (e.g., <code>0xabc... ERC20</code>).", parse_mode="HTML")
        return
    elif data == "menu_info":
        info_text = ("ℹ️ Information\n\nWelcome to AiCrypto bot.\n- Invest: deposit funds to provided wallet and upload proof (txid or screenshot). Admin will approve.\n- Withdraw: request withdrawals; admin will approve and process.")
        await query.edit_message_text(info_text, parse_mode="HTML", reply_markup=build_inline_menu(full_width=MENU_FULL_WIDTH, support_url=SUPPORT_URL))
    elif data == "menu_help":
        await query.edit_message_text(f"❓ Help\nContact support: {SUPPORT_USER}", reply_markup=build_inline_menu(full_width=MENU_FULL_WIDTH, support_url=SUPPORT_URL))
    else:
        return

# Balance helper
async def send_balance_message(query_or_message, session: AsyncSession, user_id: int):
    user = await get_user(session, user_id)
    msg = (f"💎 <b>Balance</b>\nYour Balance: <b>{float(user['balance']):.2f}$</b>\n"
           f"In Process: <b>{float(user['balance_in_process']):.2f}$</b>\n"
           f"Daily Profit: <b>{float(user['daily_profit']):.2f}$</b>\n"
           f"Total Profit: <b>{float(user['total_profit']):.2f}$</b>\n\nManager: {SUPPORT_USER}")
    try:
        await query_or_message.edit_message_text(msg, reply_markup=build_inline_menu(full_width=MENU_FULL_WIDTH, support_url=SUPPORT_URL), parse_mode="HTML")
    except Exception:
        await query_or_message.reply_text(msg, reply_markup=build_inline_menu(full_width=MENU_FULL_WIDTH, support_url=SUPPORT_URL), parse_mode="HTML")

# ---- INVEST FLOW ----
async def invest_cmd_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📈 Enter the amount you want to invest (numbers only, e.g., 100.50). Send /cancel to abort.")
    return INVEST_AMOUNT

async def invest_start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text("📈 Enter the amount you want to invest (numbers only, e.g., 100.50). Send /cancel to abort.")
    logger.info("Invest conversation started for user %s", update.effective_user.id)
    return INVEST_AMOUNT

async def invest_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError()
    except Exception:
        await update.message.reply_text("Invalid amount. Send a positive number like 100 or 50.50, or /cancel.")
        return INVEST_AMOUNT
    amount = round(amount, 2)
    context.user_data['invest_amount'] = amount

    wallet_msg = (f"📥 Deposit {amount:.2f}$\nSend to wallet:\nWallet: <code>{MASTER_WALLET}</code>\nNetwork: <b>{MASTER_NETWORK}</b>\n\n"
                  "After sending, upload a screenshot of the transaction OR send the transaction hash (txid).")
    await update.message.reply_text(wallet_msg, parse_mode="HTML")
    return INVEST_PROOF

async def invest_proof_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    amount = context.user_data.get('invest_amount')
    if amount is None:
        await update.message.reply_text("No pending invest amount found. Start again with Invest.")
        return ConversationHandler.END

    proof_label = None
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        proof_label = f"photo:{file_id}"
    else:
        text = (update.message.text or "").strip()
        if text:
            proof_label = text

    if not proof_label:
        await update.message.reply_text("Please upload a screenshot or send the txid, or /cancel.")
        return INVEST_PROOF

    context.user_data['invest_proof'] = proof_label
    await update.message.reply_text(
        f"Proof received: <code>{proof_label}</code>\nIf you have sent exactly {amount:.2f}$ to the provided wallet, press Confirm. Otherwise press Cancel.",
        parse_mode="HTML",
        reply_markup=user_confirm_kb("invest")
    )
    return INVEST_CONFIRM

async def invest_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    logger.info("invest_confirm_callback for user %s data=%s", user_id, data)

    if data == "invest_confirm_no":
        await query.message.reply_text("Investment cancelled.", reply_markup=build_inline_menu(full_width=MENU_FULL_WIDTH, support_url=SUPPORT_URL))
        context.user_data.pop('invest_amount', None)
        context.user_data.pop('invest_proof', None)
        return ConversationHandler.END

    amount = context.user_data.get('invest_amount')
    proof = context.user_data.get('invest_proof')
    if amount is None or proof is None:
        await query.message.reply_text("Missing data. Restart invest flow.", reply_markup=build_inline_menu(full_width=MENU_FULL_WIDTH, support_url=SUPPORT_URL))
        context.user_data.pop('invest_amount', None)
        context.user_data.pop('invest_proof', None)
        return ConversationHandler.END

    async with async_session() as session:
        tx_db_id, tx_ref = await log_transaction(session,
                                      user_id=user_id,
                                      ref=None,  # log_transaction will fill random ref
                                      type='invest',
                                      amount=amount,
                                      status='pending',
                                      proof=str(proof),
                                      wallet=MASTER_WALLET,
                                      network=MASTER_NETWORK,
                                      created_at=datetime.utcnow())

    # Notify user: show the 5-digit ref to them
    await query.message.reply_text(
        f"✅ Deposit proof received. Your deposit reference number is <b>{tx_ref}</b>.\nYour deposit #{tx_ref} is processing — please wait for blockchain confirmation and admin approval.",
        parse_mode="HTML",
        reply_markup=build_inline_menu(full_width=MENU_FULL_WIDTH, support_url=SUPPORT_URL)
    )

    admin_text = (f"New INVEST request #{tx_db_id} (ref {tx_ref})\nUser: {user_id}\nAmount: {amount:.2f}$\nWallet: {MASTER_WALLET}\nNetwork: {MASTER_NETWORK}\nProof: {proof}")
    try:
        if ADMIN_ID and ADMIN_ID != 0:
            await context.application.bot.send_message(chat_id=ADMIN_ID, text=admin_text, reply_markup=admin_action_kb(tx_db_id))
    except Exception:
        logger.exception("Failed notifying admin for invest")

    context.user_data.pop('invest_amount', None)
    context.user_data.pop('invest_proof', None)
    return ConversationHandler.END

# ---- WITHDRAW FLOW ----
async def withdraw_cmd_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💸 Enter the amount you want to withdraw (numbers only, e.g., 50.00). Send /cancel to abort.")
    return WITHDRAW_AMOUNT

async def withdraw_start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text("💸 Enter the amount you want to withdraw (numbers only, e.g., 50.00). Send /cancel to abort.")
    logger.info("Withdraw conversation started for user %s", update.effective_user.id)
    return WITHDRAW_AMOUNT

async def withdraw_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError()
    except Exception:
        await update.message.reply_text("Invalid amount. Send a positive number like 50 or 25.75, or /cancel.")
        return WITHDRAW_AMOUNT
    amount = round(amount, 2)
    user_id = update.effective_user.id
    async with async_session() as session:
        user = await get_user(session, user_id)
    balance = float(user['balance'] or 0)
    if amount > balance:
        await update.message.reply_text(f"Insufficient balance. Your available balance is {balance:.2f}$. Enter a smaller amount or /cancel.")
        return WITHDRAW_AMOUNT

    context.user_data['withdraw_amount'] = amount
    saved_wallet = user.get('wallet_address')
    saved_network = user.get('wallet_network')
    if saved_wallet:
        await update.message.reply_text(f"Your saved wallet:\nWallet: <code>{saved_wallet}</code>\nNetwork: <b>{saved_network}</b>\n\nSend 'yes' to use it or send a new wallet and optional network.", parse_mode="HTML")
        return WITHDRAW_WALLET
    else:
        await update.message.reply_text("No saved wallet. Send wallet address and optional network (e.g., <code>0xabc... ERC20</code>).", parse_mode="HTML")
        return WITHDRAW_WALLET

async def withdraw_wallet_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    async with async_session() as session:
        user = await get_user(session, user_id)

    if text.lower() in ('yes','y'):
        wallet_address = user.get('wallet_address')
        wallet_network = user.get('wallet_network')
        if not wallet_address:
            await update.message.reply_text("No saved wallet found. Please send a wallet address and optional network.")
            return WITHDRAW_WALLET
    else:
        parts = text.split()
        wallet_address = parts[0]
        wallet_network = parts[1] if len(parts) > 1 else ''
        async with async_session() as session:
            await update_user(session, user_id, wallet_address=wallet_address, wallet_network=wallet_network)

    context.user_data['withdraw_wallet'] = wallet_address
    context.user_data['withdraw_network'] = wallet_network
    amount = context.user_data.get('withdraw_amount')
    await update.message.reply_text(f"Confirm withdrawal:\nAmount: {amount:.2f}$\nWallet: <code>{wallet_address}</code>\nNetwork: <b>{wallet_network}</b>", parse_mode="HTML",
                                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm", callback_data="withdraw_confirm_yes"), InlineKeyboardButton("❌ Cancel", callback_data="withdraw_confirm_no")]]))
    return WITHDRAW_CONFIRM

async def withdraw_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data != "withdraw_confirm_yes":
        await query.message.reply_text("Withdrawal cancelled.", reply_markup=build_inline_menu(full_width=MENU_FULL_WIDTH, support_url=SUPPORT_URL))
        context.user_data.pop('withdraw_amount', None)
        context.user_data.pop('withdraw_wallet', None)
        return ConversationHandler.END

    user_id = query.from_user.id
    amount = context.user_data.get('withdraw_amount')
    wallet = context.user_data.get('withdraw_wallet')
    network = context.user_data.get('withdraw_network', '')

    if amount is None or wallet is None:
        await query.message.reply_text("Missing data. Start withdrawal again.", reply_markup=build_inline_menu(full_width=MENU_FULL_WIDTH, support_url=SUPPORT_URL))
        return ConversationHandler.END

    async with async_session() as session:
        user = await get_user(session, user_id)
        balance = float(user['balance'] or 0)
        if amount > balance:
            await query.message.reply_text(f"Insufficient balance ({balance:.2f}$). Withdrawal aborted.", reply_markup=build_inline_menu(full_width=MENU_FULL_WIDTH, support_url=SUPPORT_URL))
            context.user_data.pop('withdraw_amount', None)
            context.user_data.pop('withdraw_wallet', None)
            return ConversationHandler.END
        new_balance = balance - amount
        new_in_process = float(user['balance_in_process'] or 0) + amount
        await update_user(session, user_id, balance=new_balance, balance_in_process=new_in_process)

        tx_db_id, tx_ref = await log_transaction(session,
                                      user_id=user_id,
                                      ref=None,
                                      type='withdraw',
                                      amount=amount,
                                      status='pending',
                                      proof='',
                                      wallet=wallet,
                                      network=network,
                                      created_at=datetime.utcnow())

    await query.message.reply_text(f"✅ Withdrawal request #{tx_ref} submitted and is pending admin approval.", reply_markup=build_inline_menu(full_width=MENU_FULL_WIDTH, support_url=SUPPORT_URL))

    admin_text = f"New WITHDRAW request #{tx_db_id} (ref {tx_ref})\nUser: {user_id}\nAmount: {amount:.2f}$\nWallet: {wallet}\nNetwork: {network}"
    try:
        if ADMIN_ID and ADMIN_ID != 0:
            await context.application.bot.send_message(chat_id=ADMIN_ID, text=admin_text, reply_markup=admin_action_kb(tx_db_id))
    except Exception:
        logger.exception("Failed to notify admin for withdraw")

    context.user_data.pop('withdraw_amount', None)
    context.user_data.pop('withdraw_wallet', None)
    context.user_data.pop('withdraw_network', None)
    return ConversationHandler.END

# ---- ADMIN CALLBACKS ----
async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if not data:
        return
    logger.info("admin_callback_handler data=%s", data)
    if data.startswith("admin_approve_") or data.startswith("admin_reject_"):
        parts = data.split("_")
        action = parts[1]
        tx_db_id = int(parts[2])
        async with async_session() as session:
            result = await session.execute(select(Transaction).where(Transaction.id == tx_db_id))
            tx = result.scalar_one_or_none()
            if not tx:
                await query.message.reply_text("Transaction not found.")
                return
            if action == 'approve':
                if tx.type == 'invest':
                    user = await get_user(session, tx.user_id)
                    new_balance = float(user['balance'] or 0) + float(tx.amount or 0)
                    await update_user(session, tx.user_id, balance=new_balance)
                    await session.execute(update(Transaction).where(Transaction.id == tx_db_id).values(status='credited'))
                    await session.commit()
                    await query.message.reply_text(f"Invest transaction #{tx_db_id} (ref {tx.ref}) credited.")
                    try:
                        await context.application.bot.send_message(chat_id=tx.user_id, text=f"✅ Your deposit (ref {tx.ref}) has been approved. Your investment has started. Amount credited: {float(tx.amount):.2f}$.")
                    except Exception:
                        logger.exception("Failed to notify user about invest credit")
                elif tx.type == 'withdraw':
                    user = await get_user(session, tx.user_id)
                    new_in_process = max(0.0, float(user['balance_in_process'] or 0) - float(tx.amount or 0))
                    await update_user(session, tx.user_id, balance_in_process=new_in_process)
                    await session.execute(update(Transaction).where(Transaction.id == tx_db_id).values(status='completed'))
                    await session.commit()
                    await query.message.reply_text(f"Withdraw transaction #{tx_db_id} (ref {tx.ref}) completed.")
                    try:
                        await context.application.bot.send_message(chat_id=tx.user_id, text=f"✅ Your withdrawal (ref {tx.ref}) has been completed by admin.")
                    except Exception:
                        logger.exception("Failed to notify user about withdraw completion")
                else:
                    await query.message.reply_text("Unknown transaction type.")
            else:
                # reject
                if tx.type == 'invest':
                    await session.execute(update(Transaction).where(Transaction.id == tx_db_id).values(status='rejected'))
                    await session.commit()
                    await query.message.reply_text(f"Invest transaction #{tx_db_id} (ref {tx.ref}) rejected.")
                    try:
                        await context.application.bot.send_message(chat_id=tx.user_id, text=f"❌ Your deposit (ref {tx.ref}) was rejected by admin.")
                    except Exception:
                        logger.exception("Failed to notify user about invest rejection")
                elif tx.type == 'withdraw':
                    user = await get_user(session, tx.user_id)
                    new_in_process = max(0.0, float(user['balance_in_process'] or 0) - float(tx.amount or 0))
                    new_balance = float(user['balance'] or 0) + float(tx.amount or 0)
                    await update_user(session, tx.user_id, balance=new_balance, balance_in_process=new_in_process)
                    await session.execute(update(Transaction).where(Transaction.id == tx_db_id).values(status='rejected'))
                    await session.commit()
                    await query.message.reply_text(f"Withdraw transaction #{tx_db_id} (ref {tx.ref}) rejected and funds restored.")
                    try:
                        await context.application.bot.send_message(chat_id=tx.user_id, text=f"❌ Your withdrawal (ref {tx.ref}) was rejected by admin. Funds restored to your balance.")
                    except Exception:
                        logger.exception("Failed to notify user about withdraw rejection")
                else:
                    await query.message.reply_text("Unknown transaction type.")
    return

# ---- /pending admin command
def _is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID and ADMIN_ID != 0

async def admin_pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        await update.message.reply_text("Forbidden: admin only.")
        return
    async with async_session() as session:
        result = await session.execute(select(Transaction).where(Transaction.status == 'pending').order_by(Transaction.created_at.asc()))
        pending: List[Transaction] = result.scalars().all()
        if not pending:
            await update.message.reply_text("No pending transactions.")
            return
        for tx in pending:
            text_msg = (f"Pending DB id: {tx.id}  Ref: {tx.ref}\nType: {tx.type.upper()}\nUser: {tx.user_id}\nAmount: {float(tx.amount):.2f}$\n"
                        f"Wallet: {tx.wallet or '-'}\nNetwork: {tx.network or '-'}\nProof: {tx.proof or '-'}\nCreated: {tx.created_at}")
            try:
                await update.message.reply_text(text_msg, reply_markup=admin_action_kb(tx.id))
            except Exception:
                logger.exception("Failed to send pending tx to admin for tx %s", tx.id)

# Cancel helper
async def cancel_conv(update: Optional[Update], context: ContextTypes.DEFAULT_TYPE):
    if context and getattr(context, "user_data", None):
        context.user_data.pop('invest_amount', None)
        context.user_data.pop('invest_proof', None)
        context.user_data.pop('withdraw_amount', None)
        context.user_data.pop('withdraw_wallet', None)
        context.user_data.pop('withdraw_network', None)
    if update and getattr(update, "callback_query", None):
        await update.callback_query.answer()
    return ConversationHandler.END

# Balance text fallback
async def balance_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        await send_balance_message(update.message, session, update.effective_user.id)

# === MAIN ===
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('invest', invest_cmd_handler),
            CommandHandler('withdraw', withdraw_cmd_handler),
            CallbackQueryHandler(invest_start_cmd, pattern='^menu_invest$'),
            CallbackQueryHandler(withdraw_start_cmd, pattern='^menu_withdraw$'),
        ],
        states={
            INVEST_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, invest_amount_received)],
            INVEST_PROOF: [MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), invest_proof_received)],
            INVEST_CONFIRM: [
                CallbackQueryHandler(invest_confirm_callback, pattern='^invest_confirm_yes$'),
                CallbackQueryHandler(invest_confirm_callback, pattern='^invest_confirm_no$'),
            ],
            WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount_received)],
            WITHDRAW_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_wallet_received)],
            WITHDRAW_CONFIRM: [
                CallbackQueryHandler(withdraw_confirm_callback, pattern='^withdraw_confirm_yes$'),
                CallbackQueryHandler(withdraw_confirm_callback, pattern='^withdraw_confirm_no$'),
            ],
        },
        fallbacks=[CommandHandler('cancel', lambda u,c: cancel_conv(u,c))],
        allow_reentry=True,
    )

    # register handlers in correct order
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(admin_callback_handler, pattern='^admin_(approve|reject)_\\d+$'))
    app.add_handler(CallbackQueryHandler(menu_callback))
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(MessageHandler(filters.Regex("^Balance$"), balance_text_handler))

    app.add_handler(CommandHandler("pending", admin_pending_command))

    app.add_handler(CommandHandler("invest", invest_cmd_handler))
    app.add_handler(CommandHandler("withdraw", withdraw_cmd_handler))

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

# Start helpers & legacy admin commands
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = build_inline_menu(full_width=MENU_FULL_WIDTH, support_url=SUPPORT_URL)
    await update.message.reply_text("Main Menu", reply_markup=kb)

async def admin_approve_withdraw_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID or ADMIN_ID == 0:
        await update.message.reply_text("Forbidden: admin only.")
        return
    await update.message.reply_text("Use the inline approve/reject buttons sent to the admin when a new request arrives.")

async def admin_credit_invest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID or ADMIN_ID == 0:
        await update.message.reply_text("Forbidden: admin only.")
        return
    await update.message.reply_text("Use inline approve buttons in admin notifications to credit/approve invest requests.")

if __name__ == '__main__':
    try:
        asyncio.run(init_db(retries=5, backoff=2.0, fallback_to_sqlite=True))
    except Exception as e:
        logger.exception("DB init failed: %s", e)
        raise
    main()
