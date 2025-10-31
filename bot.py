# Full patched file: adds /pending admin command to list pending transactions with inline approve/reject buttons
# (Includes previous flows: persistent menu, invest/withdraw flows, confirm buttons, admin approve/reject)
import os
import logging
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
    BigInteger, select, update, func, Numeric
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

# === CONFIG ===
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set!")

ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))  # set to admin Telegram user id
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
    wallet_network = Column(String)
    joined_at = Column(DateTime, default=datetime.utcnow)


class Transaction(Base):
    __tablename__ = 'transactions'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger)
    type = Column(String)         # 'invest' or 'withdraw'
    amount = Column(Numeric(15, 2))
    status = Column(String)       # 'pending','in_process','requested','credited','rejected','completed'
    proof = Column(String)        # txid or file_id
    wallet = Column(String)       # withdrawal wallet (for withdraws)
    network = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


# DB init helpers
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
            await asyncio.sleep(wait)
    logger.error("All %d database init attempts failed. Last error: %s", retries, last_exc)
    if fallback_to_sqlite:
        try:
            sqlite_url = "sqlite+aiosqlite:///bot_fallback.db"
            from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine
            DATABASE_URL = sqlite_url
            engine = _create_async_engine(DATABASE_URL, echo=False, future=True)
            async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            await _create_all_with_timeout(engine)
            logger.info("Fallback sqlite database initialized.")
            return
        except Exception as e2:
            logger.exception("Fallback to sqlite failed: %s", e2)
    await asyncio.sleep(0.1)
    sys.exit(1)


# Helpers for DB access
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
    tx = Transaction(**data)
    session.add(tx)
    await session.commit()
    return tx.id

# Conversation states
INVEST_AMOUNT, INVEST_PROOF, WITHDRAW_AMOUNT, WITHDRAW_WALLET, WITHDRAW_CONFIRM = range(5)

# Menu builder
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

# Confirm/Cancel keyboard for user flows
def user_cancel_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⨉ Cancel", callback_data="user_cancel")]])

# Admin action keyboard for a specific tx
def admin_action_kb(tx_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Approve", callback_data=f"admin_approve_{tx_id}"),
         InlineKeyboardButton("❌ Reject", callback_data=f"admin_reject_{tx_id}")]
    ])

# Daily profit job (unchanged)
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
                              balance=float(user.balance or 0) + profit)
            await log_transaction(session, user_id=user.id, type='profit', amount=profit, status='credited')

# Menu callback handling
async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data if query else None
    if query:
        await query.answer()
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
    elif data == "menu_invest":
        await query.message.reply_text("📈 Enter the amount you want to invest (numbers only, e.g., 100.50). Send /cancel to abort.")
        return
    elif data == "menu_withdraw":
        await query.message.reply_text("💸 Enter the amount you want to withdraw (numbers only, e.g., 50.00). Send /cancel to abort.")
        return
    elif data == "menu_history":
        await query.edit_message_text("🧾 History (coming soon)", reply_markup=build_inline_menu(full_width=MENU_FULL_WIDTH, support_url=SUPPORT_URL))
    elif data == "menu_help":
        await query.edit_message_text(f"❓ Help\nContact support: {SUPPORT_USER}", reply_markup=build_inline_menu(full_width=MENU_FULL_WIDTH, support_url=SUPPORT_URL))
    else:
        return

# Send balance message helper
async def send_balance_message(query_or_message, session: AsyncSession, user_id: int):
    user = await get_user(session, user_id)
    msg = (
        f"💎 <b>Balance</b>\n"
        f"Your Balance: <b>{float(user['balance']):.2f}$</b>\n"
        f"In Process: <b>{float(user['balance_in_process']):.2f}$</b>\n"
        f"Daily Profit: <b>{float(user['daily_profit']):.2f}$</b>\n"
        f"Total Profit: <b>{float(user['total_profit']):.2f}$</b>\n\n"
        f"Manager: {SUPPORT_USER}"
    )
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

    wallet_msg = f"📥 Deposit {amount:.2f}$\nSend to wallet:\nWallet: <code>{MASTER_WALLET}</code>\nNetwork: <b>{MASTER_NETWORK}</b>\n\nAfter sending, upload a screenshot or send the txid."
    await update.message.reply_text(wallet_msg, parse_mode="HTML", reply_markup=user_cancel_kb())
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

    async with async_session() as session:
        tx_id = await log_transaction(session,
                                      user_id=user_id,
                                      type='invest',
                                      amount=amount,
                                      status='pending',
                                      proof=str(proof_label),
                                      wallet=MASTER_WALLET,
                                      network=MASTER_NETWORK,
                                      created_at=datetime.utcnow())
    await update.message.reply_text(f"✅ Deposit proof received. Investment request #{tx_id} is pending admin approval.", reply_markup=build_inline_menu(full_width=MENU_FULL_WIDTH, support_url=SUPPORT_URL))

    admin_text = f"New INVEST request #{tx_id}\nUser: {user_id}\nAmount: {amount:.2f}$\nWallet: {MASTER_WALLET}\nNetwork: {MASTER_NETWORK}\nProof: {proof_label}"
    try:
        app = context.application
        if ADMIN_ID and ADMIN_ID != 0:
            await app.bot.send_message(chat_id=ADMIN_ID, text=admin_text, reply_markup=admin_action_kb(tx_id))
    except Exception:
        logger.exception("Failed to notify admin for invest")

    context.user_data.pop('invest_amount', None)
    return ConversationHandler.END

# ---- WITHDRAW FLOW ----
async def withdraw_cmd_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💸 Enter the amount you want to withdraw (numbers only, e.g., 50.00). Send /cancel to abort.")
    return WITHDRAW_AMOUNT

async def withdraw_start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text("💸 Enter the amount you want to withdraw (numbers only, e.g., 50.00). Send /cancel to abort.")
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
        await update.message.reply_text(f"Your saved wallet:\nWallet: <code>{saved_wallet}</code>\nNetwork: <b>{saved_network}</b>\n\nSend 'yes' to use it or send a new wallet and optional network.", parse_mode="HTML", reply_markup=user_cancel_kb())
        return WITHDRAW_WALLET
    else:
        await update.message.reply_text("No saved wallet. Send wallet address and optional network (e.g., <code>0xabc... ERC20</code>).", parse_mode="HTML", reply_markup=user_cancel_kb())
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
            await update.message.reply_text("No saved wallet found. Please send a wallet address and optional network.", reply_markup=user_cancel_kb())
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
        tx_id = await log_transaction(session,
                                      user_id=user_id,
                                      type='withdraw',
                                      amount=amount,
                                      status='pending',
                                      proof='',
                                      wallet=wallet,
                                      network=network,
                                      created_at=datetime.utcnow())
    await query.message.reply_text(f"✅ Withdrawal request #{tx_id} submitted and is pending admin approval.", reply_markup=build_inline_menu(full_width=MENU_FULL_WIDTH, support_url=SUPPORT_URL))

    admin_text = f"New WITHDRAW request #{tx_id}\nUser: {user_id}\nAmount: {amount:.2f}$\nWallet: {wallet}\nNetwork: {network}"
    try:
        app = context.application
        if ADMIN_ID and ADMIN_ID != 0:
            await app.bot.send_message(chat_id=ADMIN_ID, text=admin_text, reply_markup=admin_action_kb(tx_id))
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
    if data.startswith("admin_approve_") or data.startswith("admin_reject_"):
        parts = data.split("_")
        action = parts[1]
        tx_id = int(parts[2])
        async with async_session() as session:
            result = await session.execute(select(Transaction).where(Transaction.id == tx_id))
            tx = result.scalar_one_or_none()
            if not tx:
                await query.message.reply_text("Transaction not found.")
                return
            if action == 'approve':
                if tx.type == 'invest':
                    await session.execute(update(Transaction).where(Transaction.id == tx_id).values(status='in_process'))
                elif tx.type == 'withdraw':
                    await session.execute(update(Transaction).where(Transaction.id == tx_id).values(status='completed'))
                    user = await get_user(session, tx.user_id)
                    new_in_process = max(0.0, float(user['balance_in_process'] or 0) - float(tx.amount or 0))
                    await update_user(session, tx.user_id, balance_in_process=new_in_process)
                await session.commit()
                await query.message.reply_text(f"Transaction #{tx_id} approved.")
                try:
                    await context.application.bot.send_message(chat_id=tx.user_id, text=f"Your transaction #{tx_id} has been approved by the admin.")
                except Exception:
                    pass
            else:
                await session.execute(update(Transaction).where(Transaction.id == tx_id).values(status='rejected'))
                if tx.type == 'withdraw':
                    user = await get_user(session, tx.user_id)
                    new_in_process = max(0.0, float(user['balance_in_process'] or 0) - float(tx.amount or 0))
                    new_balance = float(user['balance'] or 0) + float(tx.amount or 0)
                    await update_user(session, tx.user_id, balance=new_balance, balance_in_process=new_in_process)
                await session.commit()
                await query.message.reply_text(f"Transaction #{tx_id} rejected.")
                try:
                    await context.application.bot.send_message(chat_id=tx.user_id, text=f"Your transaction #{tx_id} was rejected by the admin.")
                except Exception:
                    pass

# ---- NEW: /pending ADMIN COMMAND ----
def _is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID and ADMIN_ID != 0

async def admin_pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        await update.message.reply_text("Forbidden: admin only.")
        return
    # Query pending transactions
    async with async_session() as session:
        result = await session.execute(select(Transaction).where(Transaction.status == 'pending').order_by(Transaction.created_at.asc()))
        pending: List[Transaction] = result.scalars().all()
        if not pending:
            await update.message.reply_text("No pending transactions.")
            return
        # For each pending transaction, send a compact message with approve/reject buttons
        for tx in pending:
            text = (f"Pending #{tx.id}\nType: {tx.type.upper()}\nUser: {tx.user_id}\nAmount: {float(tx.amount):.2f}$\n"
                    f"Wallet: {tx.wallet or '-'}\nNetwork: {tx.network or '-'}\nProof: {tx.proof or '-'}\nCreated: {tx.created_at}")
            try:
                await update.message.reply_text(text, reply_markup=admin_action_kb(tx.id))
            except Exception:
                logger.exception("Failed to send pending tx to admin for tx %s", tx.id)

# Cancel conversation helper
async def cancel_conv(update: Optional[Update], context: ContextTypes.DEFAULT_TYPE):
    if context and getattr(context, "user_data", None):
        context.user_data.pop('invest_amount', None)
        context.user_data.pop('withdraw_amount', None)
        context.user_data.pop('withdraw_wallet', None)
        context.user_data.pop('withdraw_network', None)
    if update and getattr(update, "callback_query", None):
        await update.callback_query.answer()
    return ConversationHandler.END

# Fallback: typed "Balance"
async def balance_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        await send_balance_message(update.message, session, update.effective_user.id)

# === MAIN & handler wiring ===
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
            INVEST_PROOF: [
                MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), invest_proof_received),
                CallbackQueryHandler(lambda u,c: cancel_conv(u,c), pattern='^user_cancel$'),
            ],
            WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount_received)],
            WITHDRAW_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_wallet_received),
                              CallbackQueryHandler(lambda u,c: cancel_conv(u,c), pattern='^user_cancel$')],
            WITHDRAW_CONFIRM: [CallbackQueryHandler(withdraw_confirm_callback, pattern='^withdraw_confirm_yes$'),
                               CallbackQueryHandler(withdraw_confirm_callback, pattern='^withdraw_confirm_no$')],
        },
        fallbacks=[CommandHandler('cancel', lambda u,c: cancel_conv(u,c))],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CallbackQueryHandler(menu_callback))
    app.add_handler(conv_handler)
    app.add_handler(MessageHandler(filters.Regex("^Balance$"), balance_text_handler))
    app.add_handler(CallbackQueryHandler(admin_callback_handler, pattern='^admin_(approve|reject)_\\d+$'))

    # NEW: /pending admin command
    app.add_handler(CommandHandler("pending", admin_pending_command))

    # Admin convenience commands (legacy)
    app.add_handler(CommandHandler("approve_withdraw", admin_approve_withdraw_cmd))
    app.add_handler(CommandHandler("credit_invest", admin_credit_invest_cmd))

    # quick start commands
    app.add_handler(CommandHandler("invest", invest_cmd_handler))
    app.add_handler(CommandHandler("withdraw", withdraw_cmd_handler))

    # Scheduler
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        scheduler = AsyncIOScheduler(event_loop=loop)
    except TypeError:
        scheduler = AsyncIOScheduler()
    scheduler.add_job(daily_profit_job, 'cron', hour=0, minute=0)
    scheduler.start()

    print("AiCrypto Bot STARTED")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

# Start & admin legacy handlers used above
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
