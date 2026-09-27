
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import json
import base64
import csv
import io
import tempfile
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional, List

from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, FSInputFile, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest

from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, Text, ForeignKey, select, func
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.exc import IntegrityError

from cryptography.fernet import Fernet

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    PhoneCodeInvalidError, PhoneCodeExpiredError,
    SessionPasswordNeededError, PasswordHashInvalidError,
    FloodWaitError
)

import qrcode

# ==================== CONFIGURATION ====================
@dataclass
class Config:
    BOT_TOKEN: str = "BOT TOKEN"
    ADMIN_IDS: list = field(default_factory=lambda: [USERID, USERID ])
    CHANNEL_USERNAME: str = "@ZeRoPiNg_oTp"
    CHANNEL_ID: int = ID
    DATABASE_URL: str = "sqlite+aiosqlite:///numsellbot.db"
    UPI_ID: str = "ENTER UR"
    UPI_NAME: str = "ENTER UR"
    MIN_DEPOSIT: int = 20
    ENCRYPTION_KEY: str = "Tgaccountsellbymytgbothere"
    API_ID: int = ENTER  API ID
    API_HASH: str = "ENTER HASH"
    QR_CLEANUP_MINUTES: int = 12

config = Config()

# ==================== LOGGING ====================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ==================== HTML ESCAPE HELPER ====================
def e(text) -> str:
    """Escape dynamic values for HTML parse_mode. FIX3."""
    return html.escape(str(text)) if text is not None else ""

# ==================== DATABASE MODELS ====================
Base = declarative_base()

def get_cipher():
    key = base64.urlsafe_b64encode(config.ENCRYPTION_KEY.encode().ljust(32)[:32])
    return Fernet(key)

def encrypt_value(value: str) -> str:
    if value:
        return get_cipher().encrypt(value.encode()).decode()
    return None

def decrypt_value(value: str) -> str:
    if value:
        try:
            return get_cipher().decrypt(value.encode()).decode()
        except Exception:
            return None
    return None

class User(Base):
    __tablename__ = "users"
    user_id = Column(Integer, primary_key=True)
    username = Column(String)
    wallet_balance = Column(Float, default=0.0)
    total_deposited = Column(Float, default=0.0)
    join_date = Column(DateTime, default=datetime.utcnow)
    is_banned = Column(Boolean, default=False)
    transactions = relationship("Transaction", back_populates="user")

class Account(Base):
    __tablename__ = "accounts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    phone_number = Column(String, unique=True)
    _session_string = Column(Text, name="session_string")
    country = Column(String)
    price = Column(Float)
    status = Column(String, default="available")
    added_date = Column(DateTime, default=datetime.utcnow)
    sold_to = Column(Integer, nullable=True)
    sold_date = Column(DateTime, nullable=True)
    _twofa_password = Column(Text, name="twofa_password", nullable=True)
    note = Column(Text, nullable=True)

    @property
    def session_string(self):
        return decrypt_value(self._session_string)

    @session_string.setter
    def session_string(self, value):
        self._session_string = encrypt_value(value)

    @property
    def twofa_password(self):
        return decrypt_value(self._twofa_password)

    @twofa_password.setter
    def twofa_password(self, value):
        self._twofa_password = encrypt_value(value) if value else None

class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.user_id"))
    type = Column(String)
    amount = Column(Float)
    status = Column(String)
    timestamp = Column(DateTime, default=datetime.utcnow)
    _metadata = Column(Text, name="metadata")
    user = relationship("User", back_populates="transactions")

    def set_metadata(self, data):
        self._metadata = json.dumps(data)

    def get_metadata(self):
        return json.loads(self._metadata) if self._metadata else {}

class Setting(Base):
    __tablename__ = "settings"
    key = Column(String, primary_key=True)
    value = Column(Text)

class BanLog(Base):
    __tablename__ = "ban_logs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer)
    action = Column(String)
    admin_id = Column(Integer)
    reason = Column(Text)
    timestamp = Column(DateTime, default=datetime.utcnow)

class AdminLog(Base):
    __tablename__ = "admin_logs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    admin_id = Column(Integer)
    action = Column(String)
    details = Column(Text)
    timestamp = Column(DateTime, default=datetime.utcnow)

# ==================== DATABASE MANAGER ====================
class DatabaseManager:
    def __init__(self, database_url: str):
        self.engine = create_async_engine(database_url, echo=False)
        self.async_session = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

    async def init_db(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self.async_session() as session:
            defaults = {
                "bot_locked": "false",
                "min_deposit": str(config.MIN_DEPOSIT),
                "upi_id": config.UPI_ID,
                "support_username": "@TG_ZeRoPiNg",
                "force_join_channel": config.CHANNEL_USERNAME,
            }
            for k, v in defaults.items():
                res = await session.execute(select(Setting).where(Setting.key == k))
                if not res.scalar_one_or_none():
                    session.add(Setting(key=k, value=v))
            await session.commit()

    async def get_user(self, user_id: int) -> Optional[User]:
        async with self.async_session() as session:
            result = await session.execute(select(User).where(User.user_id == user_id))
            return result.scalar_one_or_none()

    async def create_user(self, user_id: int, username: str) -> User:
        async with self.async_session() as session:
            existing = await session.execute(select(User).where(User.user_id == user_id))
            u = existing.scalar_one_or_none()
            if u:
                return u
            user = User(user_id=user_id, username=username)
            session.add(user)
            try:
                await session.commit()
                await session.refresh(user)
                return user
            except IntegrityError:
                await session.rollback()
                r2 = await session.execute(select(User).where(User.user_id == user_id))
                return r2.scalar_one_or_none()

    async def add_balance(self, user_id: int, amount: float, transaction_type: str = "deposit"):
        async with self.async_session() as session:
            result = await session.execute(select(User).where(User.user_id == user_id))
            user = result.scalar_one_or_none()
            if user:
                user.wallet_balance += amount
                if transaction_type == "deposit":
                    user.total_deposited += amount
                tx = Transaction(user_id=user_id, type=transaction_type, amount=amount, status="completed")
                session.add(tx)
                await session.commit()
                return True
        return False

    async def create_deposit_request(self, user_id: int, amount: float, screenshot_file_id: str) -> Transaction:
        async with self.async_session() as session:
            tx = Transaction(user_id=user_id, type="deposit", amount=amount, status="pending")
            tx.set_metadata({"screenshot": screenshot_file_id})
            session.add(tx)
            await session.commit()
            await session.refresh(tx)
            return tx

    async def get_pending_deposits(self) -> List[Transaction]:
        async with self.async_session() as session:
            result = await session.execute(
                select(Transaction)
                .where(Transaction.type == "deposit", Transaction.status == "pending")
                .order_by(Transaction.timestamp.desc())
            )
            return result.scalars().all()

    async def approve_deposit(self, transaction_id: int, admin_id: int) -> Optional[dict]:
        async with self.async_session() as session:
            result = await session.execute(select(Transaction).where(Transaction.id == transaction_id))
            tx = result.scalar_one_or_none()
            if tx and tx.status == "pending":
                tx.status = "completed"
                user_res = await session.execute(select(User).where(User.user_id == tx.user_id))
                user = user_res.scalar_one_or_none()
                if user:
                    user.wallet_balance += tx.amount
                    user.total_deposited += tx.amount
                session.add(AdminLog(admin_id=admin_id, action="approve_deposit", details=f"Approved #{transaction_id}"))
                await session.commit()
                return {"user_id": tx.user_id, "amount": tx.amount, "id": tx.id}
        return None

    async def reject_deposit(self, transaction_id: int, admin_id: int) -> Optional[dict]:
        async with self.async_session() as session:
            result = await session.execute(select(Transaction).where(Transaction.id == transaction_id))
            tx = result.scalar_one_or_none()
            if tx and tx.status == "pending":
                tx.status = "rejected"
                session.add(AdminLog(admin_id=admin_id, action="reject_deposit", details=f"Rejected #{transaction_id}"))
                await session.commit()
                return {"user_id": tx.user_id, "amount": tx.amount, "id": tx.id}
        return None

    async def get_available_accounts_by_country(self) -> dict:
        async with self.async_session() as session:
            result = await session.execute(
                select(Account.country, func.count(Account.id), func.min(Account.price))
                .where(Account.status == "available")
                .group_by(Account.country)
            )
            return {c: {"count": cnt, "price": p} for c, cnt, p in result}

    async def buy_account(self, user_id: int, country: str) -> Optional[Account]:
        async with self.async_session() as session:
            acc_res = await session.execute(
                select(Account).where(Account.country == country, Account.status == "available").limit(1)
            )
            account = acc_res.scalar_one_or_none()
            if not account:
                return None
            user_res = await session.execute(select(User).where(User.user_id == user_id))
            user = user_res.scalar_one_or_none()
            if not user or user.wallet_balance < account.price:
                return None
            user.wallet_balance -= account.price
            account.status = "sold"
            account.sold_to = user_id
            account.sold_date = datetime.utcnow()
            tx = Transaction(user_id=user_id, type="purchase", amount=account.price, status="completed")
            tx.set_metadata({"account_id": account.id, "country": country, "phone": account.phone_number})
            session.add(tx)
            await session.commit()
            await session.refresh(account)
            return account

    async def get_user_purchased_accounts(self, user_id: int) -> List[Account]:
        async with self.async_session() as session:
            result = await session.execute(
                select(Account).where(Account.sold_to == user_id).order_by(Account.sold_date.desc())
            )
            return result.scalars().all()

    async def account_exists(self, phone_number: str) -> bool:
        """FIX1: Pre-check for duplicates before insert."""
        async with self.async_session() as session:
            result = await session.execute(select(Account.id).where(Account.phone_number == phone_number))
            return result.scalar_one_or_none() is not None

    async def add_account(self, phone_number: str, session_string: str, country: str, price: float,
                          twofa_password: str = None, note: str = None) -> tuple:
        """FIX1: Returns (account, error_str). Never raises to caller."""
        if await self.account_exists(phone_number):
            return None, "already_exists"
        async with self.async_session() as session:
            try:
                account = Account(phone_number=phone_number, country=country, price=price,
                                  status="available", note=note)
                account.session_string = session_string
                if twofa_password:
                    account.twofa_password = twofa_password
                session.add(account)
                await session.commit()
                await session.refresh(account)
                return account, None
            except IntegrityError:
                await session.rollback()
                return None, "already_exists"
            except Exception as ex:
                await session.rollback()
                logger.error(f"add_account db error: {ex}")
                return None, "db_error"

    async def bulk_add_accounts(self, accounts_data: List[dict]) -> tuple:
        added = skipped = 0
        for data in accounts_data:
            async with self.async_session() as session:
                try:
                    existing = await session.execute(
                        select(Account.id).where(Account.phone_number == data["phone_number"])
                    )
                    if existing.scalar_one_or_none():
                        skipped += 1
                        continue
                    account = Account(
                        phone_number=data["phone_number"],
                        country=data["country"],
                        price=float(data["price"]),
                        status="available",
                        note=data.get("note") or None
                    )
                    account.session_string = data["session_string"]
                    if data.get("twofa_password"):
                        account.twofa_password = data["twofa_password"]
                    session.add(account)
                    await session.commit()
                    added += 1
                except IntegrityError:
                    await session.rollback()
                    skipped += 1
                except Exception as ex:
                    await session.rollback()
                    logger.error(f"Bulk row error: {ex}")
                    skipped += 1
        return added, skipped

    async def get_all_accounts(self) -> List[Account]:
        async with self.async_session() as session:
            result = await session.execute(select(Account).where(Account.status == "available"))
            return result.scalars().all()

    async def remove_dead_accounts(self, dead_ids: List[int]) -> int:
        async with self.async_session() as session:
            count = 0
            for aid in dead_ids:
                res = await session.execute(select(Account).where(Account.id == aid))
                acc = res.scalar_one_or_none()
                if acc:
                    await session.delete(acc)
                    count += 1
            await session.commit()
            return count

    async def get_analytics(self) -> dict:
        async with self.async_session() as session:
            total_users = (await session.execute(select(func.count(User.user_id)))).scalar() or 0
            total_revenue = (await session.execute(
                select(func.sum(Transaction.amount))
                .where(Transaction.type == "purchase", Transaction.status == "completed")
            )).scalar() or 0
            today = datetime.utcnow().date()
            today_revenue = (await session.execute(
                select(func.sum(Transaction.amount))
                .where(Transaction.type == "purchase", Transaction.status == "completed",
                       func.date(Transaction.timestamp) == today)
            )).scalar() or 0
            available = (await session.execute(
                select(func.count(Account.id)).where(Account.status == "available")
            )).scalar() or 0
            sold = (await session.execute(
                select(func.count(Account.id)).where(Account.status == "sold")
            )).scalar() or 0
            top_buyers_raw = (await session.execute(
                select(Transaction.user_id, func.count(Transaction.id).label("cnt"))
                .where(Transaction.type == "purchase")
                .group_by(Transaction.user_id)
                .order_by(func.count(Transaction.id).desc())
                .limit(5)
            )).all()
            country_sales_raw = (await session.execute(
                select(Account.country, func.count(Account.id).label("cnt"))
                .where(Account.status == "sold")
                .group_by(Account.country)
                .order_by(func.count(Account.id).desc())
            )).all()
            return {
                "total_users": total_users,
                "total_revenue": total_revenue,
                "today_revenue": today_revenue,
                "available_stock": available,
                "sold_accounts": sold,
                "top_buyers": top_buyers_raw,
                "country_sales": country_sales_raw,
            }

    async def get_setting(self, key: str, default=None):
        async with self.async_session() as session:
            result = await session.execute(select(Setting).where(Setting.key == key))
            s = result.scalar_one_or_none()
            return s.value if s else default

    async def set_setting(self, key: str, value: str):
        async with self.async_session() as session:
            result = await session.execute(select(Setting).where(Setting.key == key))
            setting = result.scalar_one_or_none()
            if setting:
                setting.value = value
            else:
                session.add(Setting(key=key, value=value))
            await session.commit()

    async def ban_user(self, user_id: int, admin_id: int, reason: str = "") -> bool:
        async with self.async_session() as session:
            result = await session.execute(select(User).where(User.user_id == user_id))
            user = result.scalar_one_or_none()
            if user:
                user.is_banned = True
                session.add(BanLog(user_id=user_id, action="ban", admin_id=admin_id, reason=reason))
                await session.commit()
                return True
        return False

    async def unban_user(self, user_id: int, admin_id: int) -> bool:
        async with self.async_session() as session:
            result = await session.execute(select(User).where(User.user_id == user_id))
            user = result.scalar_one_or_none()
            if user:
                user.is_banned = False
                session.add(BanLog(user_id=user_id, action="unban", admin_id=admin_id))
                await session.commit()
                return True
        return False

    async def get_all_users(self) -> List[User]:
        async with self.async_session() as session:
            result = await session.execute(select(User))
            return result.scalars().all()

    async def log_admin_action(self, admin_id: int, action: str, details: str):
        async with self.async_session() as session:
            session.add(AdminLog(admin_id=admin_id, action=action, details=details))
            await session.commit()

# ==================== STATES ====================
class DepositStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_screenshot = State()

class AddAccountStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_otp = State()
    waiting_for_2fa = State()
    waiting_for_price = State()
    waiting_for_country = State()
    waiting_for_note = State()

class BroadcastStates(StatesGroup):
    waiting_for_message = State()
    confirm_broadcast = State()

class UserManagementStates(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_balance_amount = State()
    waiting_for_ban_reason = State()

class BuyStates(StatesGroup):
    confirming_wallet = State()

class SettingsStates(StatesGroup):
    waiting_for_upi = State()
    waiting_for_support = State()
    waiting_for_channel = State()
    waiting_for_min_deposit = State()

# ==================== KEYBOARDS ====================
# FIX4: style="primary" (blue), style="success" (green), style="danger" (red)

class KB:
    @staticmethod
    def flag(country: str) -> str:
        return {"India":"🇮🇳","USA":"🇺🇸","UK":"🇬🇧","Canada":"🇨🇦","Australia":"🇦🇺",
                "Thailand":"🇹🇭","Algeria":"🇩🇿","Nepal":"🇳🇵","Indonesia":"🇮🇩","Brazil":"🇧🇷"}.get(country,"🌍")

    @staticmethod
    def back(cb: str = "back_to_main"):
        b = InlineKeyboardBuilder()
        b.button(text="🔙 Back", callback_data=cb, style="danger")
        return b.as_markup()

    @staticmethod
    def force_join(channel: str):
        ch = channel.replace("@","")
        b = InlineKeyboardBuilder()
        b.button(text="📢 Join Channel", url=f"https://t.me/{ch}")
        b.button(text="✅ Check", callback_data="check_join", style="success")
        b.adjust(1)
        return b.as_markup()

    @staticmethod
    def main_menu(user_id: int = None):
        b = InlineKeyboardBuilder()
        b.button(text="🛒 Buy Accounts",  callback_data="buy_accounts", style="primary")
        b.button(text="💰 Deposit Money", callback_data="deposit",      style="success")
        b.button(text="📊 My Stats",      callback_data="stats",        style="primary")
        b.button(text="📱 My Numbers",    callback_data="my_numbers",   style="primary")
        b.button(text="📞 Support",       callback_data="support",      style="primary")
        if user_id and user_id in config.ADMIN_IDS:
            b.button(text="🔧 Admin Panel", callback_data="admin_panel", style="danger")
            b.adjust(2,2,2)
        else:
            b.adjust(2,2,1)
        return b.as_markup()

    @staticmethod
    def countries(data: dict):
        b = InlineKeyboardBuilder()
        for country, info in data.items():
            if info["count"] > 0:
                b.button(text=f"{KB.flag(country)} {country} ({info['count']}) - ₹{info['price']}",
                         callback_data=f"country_{country}", style="primary")
        b.button(text="🔙 Back", callback_data="back_to_main", style="danger")
        b.adjust(1)
        return b.as_markup()

    @staticmethod
    def wallet_prompt():
        b = InlineKeyboardBuilder()
        b.button(text="✅ Yes, Use Wallet", callback_data="use_wallet_yes", style="success")
        b.button(text="❌ No",              callback_data="use_wallet_no",  style="danger")
        b.adjust(2)
        return b.as_markup()

    @staticmethod
    def after_purchase(account_id: int):
        b = InlineKeyboardBuilder()
        b.button(text="🔐 Get OTP",   callback_data=f"get_otp_{account_id}", style="primary")
        b.button(text="🔙 Main Menu", callback_data="back_to_main",          style="danger")
        b.adjust(1)
        return b.as_markup()

    @staticmethod
    def otp_buttons(account_id: int, otp: str = None):
        b = InlineKeyboardBuilder()
        if otp:
            b.button(text="📋 Copy OTP",   callback_data=f"copy_otp_{otp}",       style="success")
        b.button(text="✅ Login Done",      callback_data="login_done",             style="success")
        b.button(text="🔄 Get New OTP",    callback_data=f"get_otp_{account_id}",  style="primary")
        b.adjust(1)
        return b.as_markup()

    @staticmethod
    def deposit_amounts():
        b = InlineKeyboardBuilder()
        for amt in [50, 100, 200, 500, 1000]:
            b.button(text=f"💰 ₹{amt}", callback_data=f"deposit_amount_{amt}", style="primary")
        b.button(text="✏️ Custom Amount", callback_data="deposit_custom", style="success")
        b.button(text="🔙 Back",          callback_data="back_to_main",   style="danger")
        b.adjust(3, 2, 1)
        return b.as_markup()

    @staticmethod
    def admin_panel():
        b = InlineKeyboardBuilder()
        b.button(text="📊 Analytics",        callback_data="admin_analytics",    style="primary")
        b.button(text="➕ Add Account",      callback_data="admin_add_account",  style="success")
        b.button(text="📦 Bulk Upload",      callback_data="admin_bulk_upload",  style="primary")
        b.button(text="🩺 Health Checker",   callback_data="admin_health_check", style="primary")
        b.button(text="💰 Pending Deposits", callback_data="admin_deposits",     style="danger")
        b.button(text="👥 User Management",  callback_data="admin_users",        style="primary")
        b.button(text="📢 Broadcast",        callback_data="admin_broadcast",    style="primary")
        b.button(text="🔒 Lock/Unlock Bot",  callback_data="admin_toggle_lock",  style="danger")
        b.button(text="⚙️ Settings",         callback_data="admin_settings",     style="primary")
        b.button(text="❌ Close",             callback_data="back_to_main",       style="danger")
        b.adjust(2,2,2,2,2)
        return b.as_markup()

    @staticmethod
    def settings_menu():
        b = InlineKeyboardBuilder()
        b.button(text="💳 Change UPI ID",             callback_data="setting_upi",         style="primary")
        b.button(text="👤 Change Support Username",   callback_data="setting_support",     style="primary")
        b.button(text="📢 Change Force Join Channel", callback_data="setting_channel",     style="primary")
        b.button(text="💰 Change Minimum Deposit",    callback_data="setting_min_deposit", style="primary")
        b.button(text="🔙 Back",                      callback_data="admin_panel",         style="danger")
        b.adjust(1)
        return b.as_markup()

    @staticmethod
    def deposit_approval(tid: int):
        b = InlineKeyboardBuilder()
        b.button(text="✅ Approve", callback_data=f"approve_deposit_{tid}", style="success")
        b.button(text="❌ Reject",  callback_data=f"reject_deposit_{tid}",  style="danger")
        b.adjust(2)
        return b.as_markup()

    @staticmethod
    def user_actions(user_id: int, is_banned: bool):
        b = InlineKeyboardBuilder()
        b.button(text="💰 Add Balance", callback_data=f"add_balance_{user_id}", style="success")
        if is_banned:
            b.button(text="✅ Unban User", callback_data=f"unban_{user_id}", style="success")
        else:
            b.button(text="🚫 Ban User",   callback_data=f"ban_{user_id}",   style="danger")
        b.button(text="📋 View Details", callback_data=f"view_user_{user_id}", style="primary")
        b.button(text="🔙 Back",         callback_data="admin_users",          style="danger")
        b.adjust(2,1,1)
        return b.as_markup()

    @staticmethod
    def my_numbers(accounts: list):
        b = InlineKeyboardBuilder()
        for acc in accounts:
            b.button(
                text=f"{KB.flag(acc.country)} {acc.phone_number} | {acc.sold_date.strftime('%d/%m/%y') if acc.sold_date else 'N/A'}",
                callback_data=f"mynumber_{acc.id}", style="primary"
            )
        b.button(text="🔙 Back", callback_data="back_to_main", style="danger")
        b.adjust(1)
        return b.as_markup()

    @staticmethod
    def health_result(dead_ids: list):
        b = InlineKeyboardBuilder()
        if dead_ids:
            b.button(text=f"🗑 Remove {len(dead_ids)} Dead Accounts",
                     callback_data="remove_dead_accounts", style="danger")
        b.button(text="🔙 Back", callback_data="admin_panel", style="danger")
        b.adjust(1)
        return b.as_markup()

    @staticmethod
    def skip_note():
        b = InlineKeyboardBuilder()
        b.button(text="⏭ Skip",  callback_data="skip_note",   style="primary")
        b.button(text="🔙 Back", callback_data="admin_panel",  style="danger")
        b.adjust(2)
        return b.as_markup()

    @staticmethod
    def skip_2fa():
        b = InlineKeyboardBuilder()
        b.button(text="⏭ Skip (No 2FA)", callback_data="skip_2fa",    style="primary")
        b.button(text="🔙 Back",          callback_data="admin_panel", style="danger")
        b.adjust(2)
        return b.as_markup()

    @staticmethod
    def broadcast_confirm():
        b = InlineKeyboardBuilder()
        b.button(text="✅ Yes, Send", callback_data="confirm_broadcast", style="success")
        b.button(text="❌ Cancel",    callback_data="admin_panel",       style="danger")
        b.adjust(2)
        return b.as_markup()

# ==================== UTILITIES ====================
def fmt(num: float) -> str:
    return f"{num:,.2f}"

def is_admin(uid: int) -> bool:
    return uid in config.ADMIN_IDS

def make_qr(data: str) -> str:
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    img.save(tmp.name)
    return tmp.name

async def fetch_otp(session_string: str) -> Optional[str]:
    try:
        client = TelegramClient(StringSession(session_string), config.API_ID, config.API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return None
        async for msg in client.iter_messages(777000, limit=10):
            if msg.text and "login code" in msg.text.lower():
                m = re.search(r"\b(\d{5,6})\b", msg.text)
                if m:
                    await client.disconnect()
                    return m.group(1)
        await client.disconnect()
        return None
    except Exception as ex:
        logger.error(f"fetch_otp error: {ex}")
        return None

async def check_health(session_string: str) -> dict:
    res = {"alive": False, "authorized": False, "twofa": False, "error": None}
    client = None
    try:
        client = TelegramClient(StringSession(session_string), config.API_ID, config.API_HASH)
        await asyncio.wait_for(client.connect(), timeout=15)
        auth = await client.is_user_authorized()
        res["alive"] = True
        res["authorized"] = auth
        if auth:
            try:
                from telethon.tl.functions.account import GetPasswordRequest
                pw = await client(GetPasswordRequest())
                res["twofa"] = pw.has_password
            except Exception:
                pass
    except asyncio.TimeoutError:
        res["error"] = "timeout"
    except Exception as ex:
        res["error"] = str(ex)
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
    return res

# ==================== GLOBALS ====================
db = DatabaseManager(config.DATABASE_URL)
_qr_files: dict = {}
DISCLAIMER = (
    "\n\n\u26a0\ufe0f <b>Account delivered successfully.</b> Any future ban, freeze, logout, "
    "recovery, or restriction on the account will not be eligible for replacement or refund."
)

async def qr_cleanup(path: str):
    _qr_files[path] = datetime.utcnow() + timedelta(minutes=config.QR_CLEANUP_MINUTES)
    await asyncio.sleep(config.QR_CLEANUP_MINUTES * 60)
    try:
        if os.path.exists(path):
            os.remove(path)
        _qr_files.pop(path, None)
    except Exception as ex:
        logger.error(f"QR cleanup: {ex}")

async def is_banned(uid: int) -> bool:
    u = await db.get_user(uid)
    return bool(u and u.is_banned)

async def in_channel(bot, uid: int) -> bool:
    try:
        ch = await db.get_setting("force_join_channel", config.CHANNEL_USERNAME)
        m = await bot.get_chat_member(chat_id=ch, user_id=uid)
        return m.status not in ["left", "kicked"]
    except Exception as ex:
        logger.error(f"channel check: {ex}")
        return False

# FIX2: safe_send - always answer() for Message, edit_text() for callback message
async def safe_send(obj, text: str, markup=None, pm: str = "HTML"):
    try:
        if isinstance(obj, Message):
            await obj.answer(text, reply_markup=markup, parse_mode=pm)
        else:
            await obj.edit_text(text, reply_markup=markup, parse_mode=pm)
    except TelegramBadRequest as ex:
        logger.warning(f"safe_send fallback: {ex}")
        try:
            await obj.answer(text, reply_markup=markup, parse_mode=pm)
        except Exception:
            pass

def main_menu_text(user: User, username: str = None) -> str:
    """FIX6: Exact PRD start menu text."""
    display = f"@{e(username)}" if username else f"User {user.user_id}"
    return (
        f"👋 <b>Welcome to Account Store Bot!</b>\n\n"
        f"💳 Your Balance: ₹{fmt(user.wallet_balance)}\n"
        f"👤 {display}\n\n"
        f"🆔 Your ID: <code>{user.user_id}</code>\n\n"
        f"Select an option from the menu below:"
    )

def user_detail_text(user: User) -> str:
    uname = f"@{e(user.username)}" if user.username else "No username"
    return (
        f"👤 <b>User Details</b>\n\n"
        f"🆔 ID: <code>{user.user_id}</code>\n"
        f"👤 Username: {uname}\n"
        f"💰 Balance: ₹{fmt(user.wallet_balance)}\n"
        f"💵 Total Deposited: ₹{fmt(user.total_deposited)}\n"
        f"📅 Joined: {user.join_date.strftime('%Y-%m-%d %H:%M')}\n"
        f"🚫 Banned: {'Yes' if user.is_banned else 'No'}"
    )

# ==================== USER ROUTER ====================
UR = Router()

@UR.message(Command("start"))
async def cmd_start(msg: Message):
    try:
        uid = msg.from_user.id
        if await is_banned(uid):
            await msg.answer("❌ You are banned from using this bot.")
            return
        locked = await db.get_setting("bot_locked", "false")
        if locked == "true" and not is_admin(uid):
            await msg.answer("🔒 Bot is currently under maintenance. Please try again later.")
            return
        if not await in_channel(msg.bot, uid):
            ch = await db.get_setting("force_join_channel", config.CHANNEL_USERNAME)
            await msg.answer(
                "🔒 <b>Access Restricted</b>\n\nPlease join our official channel to use this bot.\nAfter joining, click ✅ Check below.",
                reply_markup=KB.force_join(ch), parse_mode="HTML"
            )
            return
        user = await db.get_user(uid) or await db.create_user(uid, msg.from_user.username)
        await msg.answer(main_menu_text(user, msg.from_user.username),
                         reply_markup=KB.main_menu(uid), parse_mode="HTML")
    except Exception as ex:
        logger.error(f"cmd_start: {ex}")
        await msg.answer("❌ Something went wrong. Please try /start again.")

@UR.callback_query(F.data == "check_join")
async def cb_check_join(cb: CallbackQuery):
    try:
        uid = cb.from_user.id
        if await in_channel(cb.bot, uid):
            user = await db.get_user(uid) or await db.create_user(uid, cb.from_user.username)
            await cb.message.edit_text(main_menu_text(user, cb.from_user.username),
                                       reply_markup=KB.main_menu(uid), parse_mode="HTML")
        else:
            await cb.answer("❌ You haven't joined the channel yet!", show_alert=True)
    except TelegramBadRequest as ex:
        logger.warning(f"check_join: {ex}")
    except Exception as ex:
        logger.error(f"check_join: {ex}")
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

@UR.callback_query(F.data == "back_to_main")
async def cb_back_main(cb: CallbackQuery):
    try:
        uid = cb.from_user.id
        user = await db.get_user(uid) or await db.create_user(uid, cb.from_user.username)
        await cb.message.edit_text(main_menu_text(user, cb.from_user.username),
                                   reply_markup=KB.main_menu(uid), parse_mode="HTML")
    except TelegramBadRequest as ex:
        logger.warning(f"back_to_main: {ex}")
    except Exception as ex:
        logger.error(f"back_to_main: {ex}")
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

@UR.callback_query(F.data == "my_numbers")
async def cb_my_numbers(cb: CallbackQuery):
    try:
        accs = await db.get_user_purchased_accounts(cb.from_user.id)
        if not accs:
            await cb.message.edit_text(
                "📱 <b>My Numbers</b>\n\n😔 You haven't purchased any accounts yet!\n\nGo to 🛒 Buy Accounts to purchase.",
                reply_markup=KB.back(), parse_mode="HTML")
        else:
            await cb.message.edit_text(
                f"📱 <b>Your Purchased Numbers ({len(accs)})</b>\n\nTap any number to view details!",
                reply_markup=KB.my_numbers(accs), parse_mode="HTML")
    except TelegramBadRequest as ex:
        logger.warning(f"my_numbers: {ex}")
    except Exception as ex:
        logger.error(f"my_numbers: {ex}")
        await cb.answer("❌ Error loading numbers.", show_alert=True)
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

@UR.callback_query(F.data.startswith("mynumber_"))
async def cb_mynumber(cb: CallbackQuery):
    try:
        uid = cb.from_user.id
        aid = int(cb.data.replace("mynumber_", ""))
        async with db.async_session() as s:
            r = await s.execute(select(Account).where(Account.id == aid))
            acc = r.scalar_one_or_none()
        if not acc or acc.sold_to != uid:
            await cb.answer("❌ Account not found!", show_alert=True)
            return
        twofa = f"🔑 2FA Password: <code>{e(acc.twofa_password)}</code>" if acc.twofa_password else "🔑 2FA Password: No 2FA Password Set"
        note = f"\n📝 Note: {e(acc.note)}" if acc.note else ""
        await cb.message.edit_text(
            f"📱 <b>Account Details</b>\n\n"
            f"📞 Phone: <code>{e(acc.phone_number)}</code>\n"
            f"{KB.flag(acc.country)} Country: {e(acc.country)}\n"
            f"{twofa}{note}\n"
            f"📅 Purchased: {acc.sold_date.strftime('%Y-%m-%d %H:%M') if acc.sold_date else 'N/A'}\n\n"
            f"Click below to get OTP:",
            reply_markup=KB.after_purchase(acc.id), parse_mode="HTML")
    except TelegramBadRequest as ex:
        logger.warning(f"mynumber: {ex}")
    except Exception as ex:
        logger.error(f"mynumber: {ex}")
        await cb.answer("❌ Error.", show_alert=True)
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

@UR.callback_query(F.data == "deposit")
async def cb_deposit(cb: CallbackQuery):
    try:
        mn = await db.get_setting("min_deposit", str(config.MIN_DEPOSIT))
        await cb.message.edit_text(
            f"💰 <b>Deposit Money</b>\n\nMinimum deposit: ₹{mn}\nSelect amount or enter custom:",
            reply_markup=KB.deposit_amounts(), parse_mode="HTML")
    except TelegramBadRequest as ex:
        logger.warning(f"deposit: {ex}")
    except Exception as ex:
        logger.error(f"deposit: {ex}")
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

@UR.callback_query(F.data.startswith("deposit_amount_"))
async def cb_deposit_amount(cb: CallbackQuery, state: FSMContext):
    try:
        amount = float(cb.data.split("_")[2])
        await do_deposit(cb.message, amount, state)
    except Exception as ex:
        logger.error(f"deposit_amount: {ex}")
        await cb.answer("❌ Error.", show_alert=True)
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

@UR.callback_query(F.data == "deposit_custom")
async def cb_deposit_custom(cb: CallbackQuery, state: FSMContext):
    try:
        mn = await db.get_setting("min_deposit", str(config.MIN_DEPOSIT))
        await cb.message.edit_text(
            f"💰 <b>Custom Deposit</b>\n\nPlease enter the amount (Minimum ₹{mn}):",
            reply_markup=KB.back(), parse_mode="HTML")
        await state.set_state(DepositStates.waiting_for_amount)
    except TelegramBadRequest as ex:
        logger.warning(f"deposit_custom: {ex}")
    except Exception as ex:
        logger.error(f"deposit_custom: {ex}")
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

@UR.message(DepositStates.waiting_for_amount)
async def msg_deposit_amount(msg: Message, state: FSMContext):
    try:
        mn = int(await db.get_setting("min_deposit", str(config.MIN_DEPOSIT)))
        amount = float(msg.text)
        if amount < mn:
            await msg.answer(f"❌ Minimum deposit amount is ₹{mn}", reply_markup=KB.back())
            return
        await do_deposit(msg, amount, state)
    except ValueError:
        await msg.answer("❌ Please enter a valid number.", reply_markup=KB.back())
    except Exception as ex:
        logger.error(f"msg_deposit_amount: {ex}")
        await msg.answer("❌ Something went wrong.", reply_markup=KB.back())

async def do_deposit(msg_obj: Message, amount: float, state: FSMContext):
    try:
        upi = await db.get_setting("upi_id", config.UPI_ID)
        qr_path = make_qr(f"upi://pay?pa={upi}&pn={config.UPI_NAME}&am={amount}&cu=INR")
        asyncio.create_task(qr_cleanup(qr_path))
        await state.update_data(amount=amount)
        await state.set_state(DepositStates.waiting_for_screenshot)
        await msg_obj.answer_photo(
            photo=FSInputFile(qr_path),
            caption=(
                f"💳 <b>Deposit Amount: ₹{fmt(amount)}</b>\n\n"
                f"📱 UPI ID: <code>{e(upi)}</code>\n"
                f"👤 Name: {e(config.UPI_NAME)}\n\n"
                f"1️⃣ Scan QR code or use UPI ID to pay\n"
                f"2️⃣ After payment, send screenshot here\n"
                f"3️⃣ Admin will verify and add balance"
            ),
            reply_markup=KB.back(), parse_mode="HTML"
        )
    except Exception as ex:
        logger.error(f"do_deposit: {ex}")
        await msg_obj.answer("❌ Error generating payment QR. Please try again.")

@UR.message(DepositStates.waiting_for_screenshot, F.photo)
async def msg_screenshot(msg: Message, state: FSMContext):
    try:
        uid = msg.from_user.id
        data = await state.get_data()
        amount = data["amount"]
        photo = msg.photo[-1]
        tx = await db.create_deposit_request(uid, amount, photo.file_id)
        user = await db.get_user(uid)
        await msg.answer(
            f"✅ <b>Payment Screenshot Received!</b>\n\n"
            f"Amount: ₹{fmt(amount)}\nTransaction ID: #{tx.id}\n\n"
            f"⏳ Please wait for admin verification. You will be notified once approved.",
            reply_markup=KB.main_menu(uid), parse_mode="HTML"
        )
        uname = e(msg.from_user.username) if msg.from_user.username else str(uid)
        for aid in config.ADMIN_IDS:
            try:
                await msg.bot.send_photo(
                    chat_id=aid, photo=photo.file_id,
                    caption=(
                        f"💰 <b>New Deposit Request</b>\n\n"
                        f"🆔 User ID: <code>{uid}</code>\n"
                        f"👤 Username: @{uname}\n"
                        f"💵 Amount: ₹{fmt(amount)}\n"
                        f"📝 Transaction ID: #{tx.id}"
                    ),
                    reply_markup=KB.deposit_approval(tx.id), parse_mode="HTML"
                )
            except Exception as ex:
                logger.error(f"Admin notify {aid}: {ex}")
        await state.clear()
    except Exception as ex:
        logger.error(f"msg_screenshot: {ex}")
        await msg.answer("❌ Error processing screenshot. Please try again.")

@UR.callback_query(F.data == "buy_accounts")
async def cb_buy(cb: CallbackQuery):
    try:
        data = await db.get_available_accounts_by_country()
        if not data:
            await cb.message.edit_text(
                "😔 <b>No Accounts Available</b>\n\nCurrently there are no accounts in stock.\nPlease check back later.",
                reply_markup=KB.back(), parse_mode="HTML")
        else:
            lines = "\n".join(
                f"{KB.flag(c)} {e(c)}: {info['count']} available - ₹{fmt(info['price'])}"
                for c, info in data.items()
            )
            await cb.message.edit_text(
                f"🌍 <b>Select Country</b>\n\nAvailable accounts by country:\n\n{lines}",
                reply_markup=KB.countries(data), parse_mode="HTML")
    except TelegramBadRequest as ex:
        logger.warning(f"cb_buy: {ex}")
    except Exception as ex:
        logger.error(f"cb_buy: {ex}")
        await cb.answer("❌ Error loading countries.", show_alert=True)
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

@UR.callback_query(F.data == "back_to_countries")
async def cb_back_countries(cb: CallbackQuery):
    await cb_buy(cb)

@UR.callback_query(F.data.startswith("country_"))
async def cb_country(cb: CallbackQuery, state: FSMContext):
    try:
        country = cb.data.replace("country_", "")
        data = await db.get_available_accounts_by_country()
        info = data.get(country)
        if not info:
            await cb.answer("❌ Country not available!", show_alert=True)
            return
        user = await db.get_user(cb.from_user.id)
        balance = user.wallet_balance if user else 0.0
        await state.update_data(country=country, price=info["price"])
        await state.set_state(BuyStates.confirming_wallet)
        await cb.message.edit_text(
            f"🛒 <b>Buy Account - {KB.flag(country)} {e(country)}</b>\n\n"
            f"💵 Price: ₹{fmt(info['price'])}\n"
            f"💳 Your Balance: ₹{fmt(balance)}\n\n"
            f"Would you like to use your wallet balance?",
            reply_markup=KB.wallet_prompt(), parse_mode="HTML")
    except TelegramBadRequest as ex:
        logger.warning(f"cb_country: {ex}")
    except Exception as ex:
        logger.error(f"cb_country: {ex}")
        await cb.answer("❌ Error.", show_alert=True)
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

@UR.callback_query(F.data == "use_wallet_yes", BuyStates.confirming_wallet)
async def cb_wallet_yes(cb: CallbackQuery, state: FSMContext):
    try:
        uid = cb.from_user.id
        d = await state.get_data()
        country, price = d["country"], d["price"]
        user = await db.get_user(uid)
        if not user or user.wallet_balance < price:
            needed = price - (user.wallet_balance if user else 0)
            await cb.message.edit_text(
                f"❌ <b>Insufficient Balance</b>\n\n"
                f"💵 Price: ₹{fmt(price)}\n"
                f"💳 Your Balance: ₹{fmt(user.wallet_balance if user else 0)}\n"
                f"⚠️ You need ₹{fmt(needed)} more.\n\nPlease deposit to continue.",
                reply_markup=KB.back("back_to_countries"), parse_mode="HTML")
            await state.clear()
            return
        acc = await db.buy_account(uid, country)
        await state.clear()
        if not acc:
            await cb.answer("❌ Purchase failed! Check balance or stock.", show_alert=True)
            return
        user = await db.get_user(uid)
        twofa = f"🔑 2FA Password: <code>{e(acc.twofa_password)}</code>" if acc.twofa_password else "🔑 2FA Password: No 2FA Password Set"
        note = f"\n📝 Note: {e(acc.note)}" if acc.note else ""
        await cb.message.edit_text(
            f"✅ <b>Purchase Successful!</b>\n\n"
            f"📞 Phone: <code>{e(acc.phone_number)}</code>\n"
            f"{KB.flag(acc.country)} Country: {e(acc.country)}\n"
            f"{twofa}{note}\n\n"
            f"💰 New Balance: ₹{fmt(user.wallet_balance)}\n\n"
            f"Click below to get OTP for login:",
            reply_markup=KB.after_purchase(acc.id), parse_mode="HTML")
    except TelegramBadRequest as ex:
        logger.warning(f"wallet_yes: {ex}")
    except Exception as ex:
        logger.error(f"wallet_yes: {ex}")
        await cb.answer("❌ Purchase error.", show_alert=True)
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

@UR.callback_query(F.data == "use_wallet_no", BuyStates.confirming_wallet)
async def cb_wallet_no(cb: CallbackQuery, state: FSMContext):
    try:
        await state.clear()
        await cb.message.edit_text(
            "❌ Purchase cancelled.\n\nPlease deposit money and try again.",
            reply_markup=KB.back("back_to_countries"), parse_mode="HTML")
    except TelegramBadRequest as ex:
        logger.warning(f"wallet_no: {ex}")
    except Exception as ex:
        logger.error(f"wallet_no: {ex}")
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

@UR.callback_query(F.data.startswith("get_otp_"))
async def cb_get_otp(cb: CallbackQuery):
    try:
        uid = cb.from_user.id
        aid = int(cb.data.replace("get_otp_", ""))
        async with db.async_session() as s:
            r = await s.execute(select(Account).where(Account.id == aid))
            acc = r.scalar_one_or_none()
        if not acc or acc.sold_to != uid:
            await cb.answer("❌ Account not found!", show_alert=True)
            return
        await cb.message.edit_text("🔄 <b>Fetching OTP...</b>\n\nPlease wait, this may take up to 30 seconds.", parse_mode="HTML")
        await cb.answer()
        otp = await fetch_otp(acc.session_string)
        if otp:
            twofa = f"\n🔑 2FA Password: <code>{e(acc.twofa_password)}</code>" if acc.twofa_password else "\n🔑 2FA Password: No 2FA Password Set"
            note = f"\n📝 Note: {e(acc.note)}" if acc.note else ""
            await cb.message.edit_text(
                f"✅ <b>OTP Received!</b>\n\n"
                f"📱 Phone: <code>{e(acc.phone_number)}</code>\n"
                f"🔐 OTP: <code>{otp}</code>"
                f"{twofa}{note}\n\n"
                f"Use this code to log in. Valid for a short time."
                f"{DISCLAIMER}",
                reply_markup=KB.otp_buttons(aid, otp), parse_mode="HTML")
        else:
            await cb.message.edit_text(
                "❌ <b>Failed to fetch OTP</b>\n\n"
                "Possible reasons:\n• No OTP received yet\n• Session expired\n• Network issue\n\n"
                "Please try again in a few seconds.",
                reply_markup=KB.otp_buttons(aid), parse_mode="HTML")
    except TelegramBadRequest as ex:
        logger.warning(f"get_otp edit: {ex}")
    except Exception as ex:
        logger.error(f"get_otp: {ex}")
        try:
            await cb.answer("❌ Error fetching OTP.", show_alert=True)
        except Exception:
            pass

@UR.callback_query(F.data.startswith("copy_otp_"))
async def cb_copy_otp(cb: CallbackQuery):
    try:
        otp = cb.data.replace("copy_otp_", "")
        await cb.answer(f"OTP: {otp}", show_alert=True)
    except Exception as ex:
        logger.error(f"copy_otp: {ex}")

@UR.callback_query(F.data == "login_done")
async def cb_login_done(cb: CallbackQuery):
    try:
        await cb.message.edit_text(
            "✅ <b>Login Successful!</b>\n\nThank you for your purchase. Enjoy using the account!",
            reply_markup=KB.back(), parse_mode="HTML")
    except TelegramBadRequest as ex:
        logger.warning(f"login_done: {ex}")
    except Exception as ex:
        logger.error(f"login_done: {ex}")
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

@UR.callback_query(F.data == "stats")
async def cb_stats(cb: CallbackQuery):
    try:
        uid = cb.from_user.id
        user = await db.get_user(uid)
        if not user:
            await cb.answer("❌ User not found!", show_alert=True)
            return
        cnt = len(await db.get_user_purchased_accounts(uid))
        await cb.message.edit_text(
            f"📊 <b>Your Statistics</b>\n\n"
            f"🆔 User ID: <code>{uid}</code>\n"
            f"💳 Current Balance: ₹{fmt(user.wallet_balance)}\n"
            f"💰 Total Deposited: ₹{fmt(user.total_deposited)}\n"
            f"📱 Accounts Purchased: {cnt}\n"
            f"📅 Joined: {user.join_date.strftime('%Y-%m-%d')}",
            reply_markup=KB.back(), parse_mode="HTML")
    except TelegramBadRequest as ex:
        logger.warning(f"stats: {ex}")
    except Exception as ex:
        logger.error(f"stats: {ex}")
        await cb.answer("❌ Error.", show_alert=True)
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

@UR.callback_query(F.data == "support")
async def cb_support(cb: CallbackQuery):
    try:
        sup = await db.get_setting("support_username", "@TG_ZeRoPiNg")
        await cb.message.edit_text(
            f"📞 <b>Support</b>\n\nNeed help? Contact our support team:\n\n"
            f"💬 Telegram: {e(sup)}\n\n⏰ Response Time: 2-4 hours",
            reply_markup=KB.back(), parse_mode="HTML")
    except TelegramBadRequest as ex:
        logger.warning(f"support: {ex}")
    except Exception as ex:
        logger.error(f"support: {ex}")
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

# ==================== ADMIN ROUTER ====================
AR = Router()

@AR.callback_query(F.data == "admin_panel")
async def cb_admin_panel(cb: CallbackQuery):
    try:
        if not is_admin(cb.from_user.id):
            await cb.answer("⛔ Access Denied!", show_alert=True)
            return
        await cb.message.edit_text("🔧 <b>Admin Panel</b>\n\nSelect an option:", reply_markup=KB.admin_panel(), parse_mode="HTML")
    except TelegramBadRequest as ex:
        logger.warning(f"admin_panel: {ex}")
    except Exception as ex:
        logger.error(f"admin_panel: {ex}")
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

# ---- ANALYTICS ----
@AR.callback_query(F.data == "admin_analytics")
async def cb_analytics(cb: CallbackQuery):
    try:
        if not is_admin(cb.from_user.id):
            await cb.answer("⛔ Access Denied!", show_alert=True)
            return
        d = await db.get_analytics()
        buyers = ""
        for uid2, cnt in d["top_buyers"]:
            u2 = await db.get_user(uid2)
            uname = f"@{e(u2.username)}" if u2 and u2.username else str(uid2)
            buyers += f"  • {uname}: {cnt} purchase(s)\n"
        cw = ""
        for c, cnt in d["country_sales"]:
            cw += f"  {KB.flag(c)} {e(c)}: {cnt}\n"
        await cb.message.edit_text(
            f"📊 <b>Analytics</b>\n\n"
            f"👥 Total Users: {d['total_users']}\n"
            f"💰 Total Revenue: ₹{fmt(d['total_revenue'])}\n"
            f"📅 Today Revenue: ₹{fmt(d['today_revenue'])}\n"
            f"📦 Available Stock: {d['available_stock']}\n"
            f"✅ Sold Accounts: {d['sold_accounts']}\n\n"
            f"🏆 <b>Top Buyers:</b>\n{buyers or '  None yet'}\n"
            f"🌍 <b>Country Wise Sales:</b>\n{cw or '  None yet'}",
            reply_markup=KB.back("admin_panel"), parse_mode="HTML")
    except TelegramBadRequest as ex:
        logger.warning(f"analytics: {ex}")
    except Exception as ex:
        logger.error(f"analytics: {ex}")
        await cb.answer("❌ Error loading analytics.", show_alert=True)
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

# ---- SETTINGS ----
@AR.callback_query(F.data == "admin_settings")
async def cb_settings(cb: CallbackQuery):
    try:
        if not is_admin(cb.from_user.id):
            await cb.answer("⛔ Access Denied!", show_alert=True)
            return
        upi = await db.get_setting("upi_id", config.UPI_ID)
        sup = await db.get_setting("support_username", "@TG_ZeRoPiNg")
        ch = await db.get_setting("force_join_channel", config.CHANNEL_USERNAME)
        mn = await db.get_setting("min_deposit", str(config.MIN_DEPOSIT))
        await cb.message.edit_text(
            f"⚙️ <b>Settings</b>\n\n"
            f"💳 UPI ID: <code>{e(upi)}</code>\n"
            f"👤 Support: {e(sup)}\n"
            f"📢 Force Join: {e(ch)}\n"
            f"💰 Min Deposit: ₹{mn}",
            reply_markup=KB.settings_menu(), parse_mode="HTML")
    except TelegramBadRequest as ex:
        logger.warning(f"settings: {ex}")
    except Exception as ex:
        logger.error(f"settings: {ex}")
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

@AR.callback_query(F.data == "setting_upi")
async def cb_setting_upi(cb: CallbackQuery, state: FSMContext):
    try:
        if not is_admin(cb.from_user.id): return
        await cb.message.edit_text("💳 Enter new UPI ID:", reply_markup=KB.back("admin_settings"), parse_mode="HTML")
        await state.set_state(SettingsStates.waiting_for_upi)
    except TelegramBadRequest as ex:
        logger.warning(f"setting_upi: {ex}")
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

@AR.message(SettingsStates.waiting_for_upi)
async def msg_upi(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    await db.set_setting("upi_id", msg.text.strip())
    await db.log_admin_action(msg.from_user.id, "setting_upi", msg.text.strip())
    await msg.answer("✅ UPI ID updated!", reply_markup=KB.back("admin_settings"), parse_mode="HTML")
    await state.clear()

@AR.callback_query(F.data == "setting_support")
async def cb_setting_support(cb: CallbackQuery, state: FSMContext):
    try:
        if not is_admin(cb.from_user.id): return
        await cb.message.edit_text("👤 Enter new support username (e.g. @username):", reply_markup=KB.back("admin_settings"), parse_mode="HTML")
        await state.set_state(SettingsStates.waiting_for_support)
    except TelegramBadRequest as ex:
        logger.warning(f"setting_support: {ex}")
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

@AR.message(SettingsStates.waiting_for_support)
async def msg_support(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    await db.set_setting("support_username", msg.text.strip())
    await db.log_admin_action(msg.from_user.id, "setting_support", msg.text.strip())
    await msg.answer("✅ Support username updated!", reply_markup=KB.back("admin_settings"), parse_mode="HTML")
    await state.clear()

@AR.callback_query(F.data == "setting_channel")
async def cb_setting_channel(cb: CallbackQuery, state: FSMContext):
    try:
        if not is_admin(cb.from_user.id): return
        await cb.message.edit_text("📢 Enter new force join channel (e.g. @channel):", reply_markup=KB.back("admin_settings"), parse_mode="HTML")
        await state.set_state(SettingsStates.waiting_for_channel)
    except TelegramBadRequest as ex:
        logger.warning(f"setting_channel: {ex}")
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

@AR.message(SettingsStates.waiting_for_channel)
async def msg_channel(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    await db.set_setting("force_join_channel", msg.text.strip())
    await db.log_admin_action(msg.from_user.id, "setting_channel", msg.text.strip())
    await msg.answer("✅ Force join channel updated!", reply_markup=KB.back("admin_settings"), parse_mode="HTML")
    await state.clear()

@AR.callback_query(F.data == "setting_min_deposit")
async def cb_setting_mindep(cb: CallbackQuery, state: FSMContext):
    try:
        if not is_admin(cb.from_user.id): return
        await cb.message.edit_text("💰 Enter new minimum deposit amount (numbers only):", reply_markup=KB.back("admin_settings"), parse_mode="HTML")
        await state.set_state(SettingsStates.waiting_for_min_deposit)
    except TelegramBadRequest as ex:
        logger.warning(f"setting_mindep: {ex}")
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

@AR.message(SettingsStates.waiting_for_min_deposit)
async def msg_mindep(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    try:
        val = int(msg.text.strip())
        await db.set_setting("min_deposit", str(val))
        await db.log_admin_action(msg.from_user.id, "setting_min_deposit", str(val))
        await msg.answer(f"✅ Minimum deposit set to ₹{val}!", reply_markup=KB.back("admin_settings"), parse_mode="HTML")
    except ValueError:
        await msg.answer("❌ Enter a valid number.", reply_markup=KB.back("admin_settings"))
    await state.clear()

# ---- HEALTH CHECKER ----
@AR.callback_query(F.data == "admin_health_check")
async def cb_health(cb: CallbackQuery):
    try:
        if not is_admin(cb.from_user.id):
            await cb.answer("⛔ Access Denied!", show_alert=True)
            return
        await cb.message.edit_text("🩺 <b>Running health check...</b>\n\nThis may take a while for large inventories.", parse_mode="HTML")
        await cb.answer()
        accounts = await db.get_all_accounts()
        if not accounts:
            await cb.message.edit_text("📭 No accounts to check.", reply_markup=KB.back("admin_panel"), parse_mode="HTML")
            return
        total = len(accounts)
        alive = dead = twofa_on = twofa_off = 0
        dead_ids = []
        for acc in accounts:
            if not acc.session_string:
                dead += 1
                dead_ids.append(acc.id)
                continue
            h = await check_health(acc.session_string)
            if h["alive"] and h["authorized"]:
                alive += 1
                if h["twofa"]: twofa_on += 1
                else: twofa_off += 1
            else:
                dead += 1
                dead_ids.append(acc.id)
        await db.set_setting("_dead_ids", json.dumps(dead_ids))
        await cb.message.edit_text(
            f"🩺 <b>Health Check Results</b>\n\n"
            f"📦 Total Accounts: {total}\n"
            f"✅ Alive Accounts: {alive}\n"
            f"💀 Dead Accounts: {dead}\n"
            f"🔐 2FA Enabled: {twofa_on}\n"
            f"🔓 2FA Disabled: {twofa_off}",
            reply_markup=KB.health_result(dead_ids), parse_mode="HTML")
    except TelegramBadRequest as ex:
        logger.warning(f"health: {ex}")
    except Exception as ex:
        logger.error(f"health: {ex}")
        await cb.answer("❌ Health check failed.", show_alert=True)

@AR.callback_query(F.data == "remove_dead_accounts")
async def cb_remove_dead(cb: CallbackQuery):
    try:
        if not is_admin(cb.from_user.id):
            await cb.answer("⛔ Access Denied!", show_alert=True)
            return
        raw = await db.get_setting("_dead_ids", "[]")
        dead_ids = json.loads(raw)
        if not dead_ids:
            await cb.answer("No dead accounts to remove!", show_alert=True)
            return
        cnt = await db.remove_dead_accounts(dead_ids)
        await db.set_setting("_dead_ids", "[]")
        await db.log_admin_action(cb.from_user.id, "remove_dead", f"Removed {cnt}")
        await cb.message.edit_text(f"🗑 Removed <b>{cnt}</b> dead account(s) successfully.",
                                   reply_markup=KB.back("admin_panel"), parse_mode="HTML")
    except TelegramBadRequest as ex:
        logger.warning(f"remove_dead: {ex}")
    except Exception as ex:
        logger.error(f"remove_dead: {ex}")
        await cb.answer("❌ Error.", show_alert=True)
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

# ---- ADD ACCOUNT (In-bot OTP flow) ----
@AR.callback_query(F.data == "admin_add_account")
async def cb_add_account(cb: CallbackQuery, state: FSMContext):
    try:
        if not is_admin(cb.from_user.id):
            await cb.answer("⛔ Access Denied!", show_alert=True)
            return
        await cb.message.edit_text(
            "➕ <b>Add New Account</b>\n\nEnter the phone number (with country code):\nExample: +919876543210",
            reply_markup=KB.back("admin_panel"), parse_mode="HTML")
        await state.set_state(AddAccountStates.waiting_for_phone)
    except TelegramBadRequest as ex:
        logger.warning(f"add_account: {ex}")
    except Exception as ex:
        logger.error(f"add_account: {ex}")
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

@AR.message(AddAccountStates.waiting_for_phone)
async def msg_phone(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    phone = msg.text.strip()
    # FIX1: pre-check
    if await db.account_exists(phone):
        await msg.answer(
            f"❌ Account already exists in database.\n\n<code>{e(phone)}</code> is already added.",
            reply_markup=KB.back("admin_panel"), parse_mode="HTML")
        await state.clear()
        return
    await msg.answer(f"📱 Sending OTP to <code>{e(phone)}</code>...", parse_mode="HTML")
    try:
        client = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
        await client.connect()
        result = await client.send_code_request(phone)
        sess = client.session.save()
        pch = result.phone_code_hash
        await client.disconnect()
        await state.update_data(phone=phone, session_str=sess, phone_code_hash=pch)
        await state.set_state(AddAccountStates.waiting_for_otp)
        await msg.answer(f"✅ OTP sent to <code>{e(phone)}</code>!\n\nPlease enter the OTP:",
                         reply_markup=KB.back("admin_panel"), parse_mode="HTML")
    except FloodWaitError as ex:
        await msg.answer(f"❌ Flood wait: please wait {ex.seconds}s and retry.", reply_markup=KB.back("admin_panel"))
        await state.clear()
    except Exception as ex:
        await msg.answer("❌ Failed to send OTP. Check the phone number and try again.", reply_markup=KB.back("admin_panel"))
        logger.error(f"msg_phone send_code: {ex}")
        await state.clear()

@AR.message(AddAccountStates.waiting_for_otp)
async def msg_otp(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    d = await state.get_data()
    try:
        client = TelegramClient(StringSession(d["session_str"]), config.API_ID, config.API_HASH)
        await client.connect()
        await client.sign_in(phone=d["phone"], code=msg.text.strip(), phone_code_hash=d["phone_code_hash"])
        final = client.session.save()
        await client.disconnect()
        await state.update_data(session_str=final, twofa_password=None)
        await state.set_state(AddAccountStates.waiting_for_price)
        await msg.answer("✅ Login successful! No 2FA detected.\n\nEnter the price for this account (₹):",
                         reply_markup=KB.back("admin_panel"), parse_mode="HTML")
    except SessionPasswordNeededError:
        await state.update_data(session_str=d["session_str"])
        await state.set_state(AddAccountStates.waiting_for_2fa)
        await msg.answer("🔐 This account has 2FA enabled.\n\nEnter the 2FA password, or skip if unavailable:",
                         reply_markup=KB.skip_2fa(), parse_mode="HTML")
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        await msg.answer("❌ Invalid or expired OTP. Process stopped.", reply_markup=KB.back("admin_panel"))
        await state.clear()
    except Exception as ex:
        await msg.answer("❌ Login failed. Please try again.", reply_markup=KB.back("admin_panel"))
        logger.error(f"msg_otp: {ex}")
        await state.clear()

@AR.message(AddAccountStates.waiting_for_2fa)
async def msg_2fa(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    d = await state.get_data()
    try:
        client = TelegramClient(StringSession(d["session_str"]), config.API_ID, config.API_HASH)
        await client.connect()
        await client.sign_in(password=msg.text.strip())
        final = client.session.save()
        await client.disconnect()
        await state.update_data(session_str=final, twofa_password=msg.text.strip())
        await state.set_state(AddAccountStates.waiting_for_price)
        await msg.answer("✅ 2FA login successful!\n\nEnter the price for this account (₹):",
                         reply_markup=KB.back("admin_panel"), parse_mode="HTML")
    except PasswordHashInvalidError:
        await msg.answer("❌ Incorrect 2FA password. Process stopped.", reply_markup=KB.back("admin_panel"))
        await state.clear()
    except Exception as ex:
        await msg.answer("❌ 2FA login failed.", reply_markup=KB.back("admin_panel"))
        logger.error(f"msg_2fa: {ex}")
        await state.clear()

@AR.callback_query(F.data == "skip_2fa", AddAccountStates.waiting_for_2fa)
async def cb_skip_2fa(cb: CallbackQuery, state: FSMContext):
    try:
        if not is_admin(cb.from_user.id): return
        await state.update_data(twofa_password=None)
        await state.set_state(AddAccountStates.waiting_for_price)
        await cb.message.edit_text("⏭ 2FA skipped.\n\nEnter the price for this account (₹):",
                                   reply_markup=KB.back("admin_panel"), parse_mode="HTML")
    except TelegramBadRequest as ex:
        logger.warning(f"skip_2fa: {ex}")
    except Exception as ex:
        logger.error(f"skip_2fa: {ex}")
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

@AR.message(AddAccountStates.waiting_for_price)
async def msg_price(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    try:
        price = float(msg.text.strip())
        if price <= 0: raise ValueError
        await state.update_data(price=price)
        await state.set_state(AddAccountStates.waiting_for_country)
        await msg.answer("💰 Price saved.\n\nEnter country (e.g., India, USA, UK):", reply_markup=KB.back("admin_panel"), parse_mode="HTML")
    except ValueError:
        await msg.answer("❌ Enter a valid price!", reply_markup=KB.back("admin_panel"))

@AR.message(AddAccountStates.waiting_for_country)
async def msg_country(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    await state.update_data(country=msg.text.strip())
    await state.set_state(AddAccountStates.waiting_for_note)
    await msg.answer(
        "🌍 Country saved.\n\nEnter a note for this account (optional).\n"
        "Examples: Fresh Account, Aged Account, Premium Username",
        reply_markup=KB.skip_note(), parse_mode="HTML")

@AR.callback_query(F.data == "skip_note", AddAccountStates.waiting_for_note)
async def cb_skip_note(cb: CallbackQuery, state: FSMContext):
    try:
        if not is_admin(cb.from_user.id): return
        await state.update_data(note=None)
        await finalize_account(cb.message, state, cb.from_user.id, from_cb=True)
    except Exception as ex:
        logger.error(f"skip_note: {ex}")
        await cb.answer("❌ Error.", show_alert=True)
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

@AR.message(AddAccountStates.waiting_for_note)
async def msg_note(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    await state.update_data(note=msg.text.strip())
    await finalize_account(msg, state, msg.from_user.id, from_cb=False)

async def finalize_account(obj, state: FSMContext, admin_id: int, from_cb: bool):
    """FIX1 + FIX2: Safe final account save with duplicate guard and correct send method."""
    d = await state.get_data()
    phone = d["phone"]
    # FIX1: final pre-check before DB call
    if await db.account_exists(phone):
        txt = f"❌ Account already exists in database.\n\n<code>{e(phone)}</code> is already added."
        if from_cb:
            try:
                await obj.edit_text(txt, reply_markup=KB.back("admin_panel"), parse_mode="HTML")
            except TelegramBadRequest:
                await obj.answer(txt, reply_markup=KB.back("admin_panel"), parse_mode="HTML")
        else:
            await obj.answer(txt, reply_markup=KB.back("admin_panel"), parse_mode="HTML")
        await state.clear()
        return

    account, err = await db.add_account(
        phone, d["session_str"], d["country"], d["price"],
        d.get("twofa_password"), d.get("note")
    )
    if err == "already_exists":
        txt = f"❌ Account already exists in database.\n\n<code>{e(phone)}</code> is already added."
    elif err:
        txt = "❌ Failed to save account due to a database error. Please try again."
        logger.error(f"finalize_account err: {err}")
    else:
        await db.log_admin_action(admin_id, "add_account", f"{phone} ({d['country']}) ₹{d['price']}")
        note_line = f"\n📝 Note: {e(d['note'])}" if d.get("note") else ""
        txt = (
            f"✅ <b>Account Added Successfully!</b>\n\n"
            f"📱 Phone: <code>{e(phone)}</code>\n"
            f"🌍 Country: {e(d['country'])}\n"
            f"💰 Price: ₹{fmt(d['price'])}\n"
            f"🔐 2FA: {'✅ With 2FA' if d.get('twofa_password') else '❌ No 2FA'}"
            f"{note_line}"
        )
    # FIX2: correct send method
    if from_cb:
        try:
            await obj.edit_text(txt, reply_markup=KB.back("admin_panel"), parse_mode="HTML")
        except TelegramBadRequest:
            await obj.answer(txt, reply_markup=KB.back("admin_panel"), parse_mode="HTML")
    else:
        await obj.answer(txt, reply_markup=KB.back("admin_panel"), parse_mode="HTML")
    await state.clear()

# ---- BULK UPLOAD ----
@AR.callback_query(F.data == "admin_bulk_upload")
async def cb_bulk(cb: CallbackQuery):
    try:
        if not is_admin(cb.from_user.id):
            await cb.answer("⛔ Access Denied!", show_alert=True)
            return
        await cb.message.edit_text(
            "📦 <b>Bulk Upload</b>\n\n"
            "Send a CSV file with columns:\n"
            "<code>phone_number, session_string, country, price, twofa_password (opt), note (opt)</code>\n\n"
            "Duplicate phone numbers will be skipped automatically.",
            reply_markup=KB.back("admin_panel"), parse_mode="HTML")
    except TelegramBadRequest as ex:
        logger.warning(f"bulk: {ex}")
    except Exception as ex:
        logger.error(f"bulk: {ex}")
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

@AR.message(F.document)
async def msg_csv(msg: Message):
    if not is_admin(msg.from_user.id): return
    if not msg.document.file_name.endswith(".csv"):
        await msg.answer("❌ Please send a CSV file!")
        return
    try:
        f = await msg.bot.get_file(msg.document.file_id)
        raw = await msg.bot.download_file(f.file_path)
        reader = csv.DictReader(io.StringIO(raw.read().decode("utf-8")))
        rows = []
        for row in reader:
            rows.append({
                "phone_number": row.get("phone_number","").strip(),
                "session_string": row.get("session_string","").strip(),
                "country": row.get("country","").strip(),
                "price": row.get("price","0").strip(),
                "twofa_password": row.get("twofa_password","").strip() or None,
                "note": row.get("note","").strip() or None,
            })
        added, skipped = await db.bulk_add_accounts(rows)
        await db.log_admin_action(msg.from_user.id, "bulk_upload", f"Added {added}, Skipped {skipped}")
        await msg.answer(
            f"✅ <b>Bulk Upload Complete!</b>\n\n✅ Added: {added}\n⏭ Skipped Duplicates: {skipped}",
            reply_markup=KB.back("admin_panel"), parse_mode="HTML")
    except Exception as ex:
        logger.error(f"msg_csv: {ex}")
        await msg.answer("❌ Error processing file. Check format and try again.", reply_markup=KB.back("admin_panel"))

# ---- BROADCAST ----
@AR.callback_query(F.data == "admin_broadcast")
async def cb_broadcast(cb: CallbackQuery, state: FSMContext):
    try:
        if not is_admin(cb.from_user.id):
            await cb.answer("⛔ Access Denied!", show_alert=True)
            return
        await cb.message.edit_text(
            "📢 <b>Broadcast Message</b>\n\nSend the message to broadcast to all users.",
            reply_markup=KB.back("admin_panel"), parse_mode="HTML")
        await state.set_state(BroadcastStates.waiting_for_message)
    except TelegramBadRequest as ex:
        logger.warning(f"broadcast: {ex}")
    except Exception as ex:
        logger.error(f"broadcast: {ex}")
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

@AR.message(BroadcastStates.waiting_for_message)
async def msg_broadcast(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    await state.update_data(message_id=msg.message_id, chat_id=msg.chat.id)
    # FIX2: source is Message -> answer()
    await msg.answer("📢 <b>Confirm Broadcast</b>\n\nThis will be sent to ALL users. Confirm?",
                     reply_markup=KB.broadcast_confirm(), parse_mode="HTML")
    await state.set_state(BroadcastStates.confirm_broadcast)

@AR.callback_query(F.data == "confirm_broadcast", BroadcastStates.confirm_broadcast)
async def cb_confirm_broadcast(cb: CallbackQuery, state: FSMContext):
    try:
        if not is_admin(cb.from_user.id):
            await cb.answer("⛔ Access Denied!", show_alert=True)
            return
        d = await state.get_data()
        users = await db.get_all_users()
        ok = fail = 0
        await cb.message.edit_text(f"📤 Sending to {len(users)} users...", parse_mode="HTML")
        await cb.answer()
        for u in users:
            try:
                await cb.bot.copy_message(chat_id=u.user_id, from_chat_id=d["chat_id"], message_id=d["message_id"])
                ok += 1
            except Exception:
                fail += 1
            await asyncio.sleep(0.05)
        await db.log_admin_action(cb.from_user.id, "broadcast", f"Sent {ok}, failed {fail}")
        await cb.message.edit_text(
            f"✅ <b>Broadcast Complete!</b>\n\n✅ Sent: {ok}\n❌ Failed: {fail}",
            reply_markup=KB.back("admin_panel"), parse_mode="HTML")
        await state.clear()
    except TelegramBadRequest as ex:
        logger.warning(f"confirm_broadcast: {ex}")
    except Exception as ex:
        logger.error(f"confirm_broadcast: {ex}")
        await cb.answer("❌ Broadcast error.", show_alert=True)

# ---- LOCK/UNLOCK ----
@AR.callback_query(F.data == "admin_toggle_lock")
async def cb_toggle_lock(cb: CallbackQuery):
    try:
        if not is_admin(cb.from_user.id):
            await cb.answer("⛔ Access Denied!", show_alert=True)
            return
        cur = await db.get_setting("bot_locked", "false")
        new = "false" if cur == "true" else "true"
        await db.set_setting("bot_locked", new)
        await db.log_admin_action(cb.from_user.id, "toggle_lock", f"Bot {'locked' if new=='true' else 'unlocked'}")
        await cb.message.edit_text(
            f"🔒 Bot is now <b>{'LOCKED' if new=='true' else 'UNLOCKED'}</b>",
            reply_markup=KB.back("admin_panel"), parse_mode="HTML")
    except TelegramBadRequest as ex:
        logger.warning(f"toggle_lock: {ex}")
    except Exception as ex:
        logger.error(f"toggle_lock: {ex}")
        await cb.answer("❌ Error.", show_alert=True)
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

# ---- PENDING DEPOSITS ----
@AR.callback_query(F.data == "admin_deposits")
async def cb_deposits(cb: CallbackQuery):
    try:
        if not is_admin(cb.from_user.id):
            await cb.answer("⛔ Access Denied!", show_alert=True)
            return
        deps = await db.get_pending_deposits()
        if not deps:
            await cb.message.edit_text("📭 No pending deposits.", reply_markup=KB.back("admin_panel"), parse_mode="HTML")
        else:
            txt = "💰 <b>Pending Deposits</b>\n\n"
            for dep in deps[:10]:
                txt += (f"🆔 #{dep.id} - User: <code>{dep.user_id}</code>\n"
                        f"💵 Amount: ₹{fmt(dep.amount)}\n"
                        f"📅 {dep.timestamp.strftime('%Y-%m-%d %H:%M')}\n\n")
            await cb.message.edit_text(txt, reply_markup=KB.back("admin_panel"), parse_mode="HTML")
    except TelegramBadRequest as ex:
        logger.warning(f"deposits: {ex}")
    except Exception as ex:
        logger.error(f"deposits: {ex}")
        await cb.answer("❌ Error.", show_alert=True)
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

@AR.callback_query(F.data.startswith("approve_deposit_"))
async def cb_approve_dep(cb: CallbackQuery):
    try:
        if not is_admin(cb.from_user.id):
            await cb.answer("⛔ Access Denied!", show_alert=True)
            return
        tid = int(cb.data.replace("approve_deposit_", ""))
        info = await db.approve_deposit(tid, cb.from_user.id)
        if info:
            try:
                await cb.bot.send_message(info["user_id"],
                    f"✅ <b>Deposit Approved!</b>\n\nAmount: ₹{fmt(info['amount'])}\nYour balance has been updated.",
                    parse_mode="HTML")
            except Exception: pass
            try:
                await cb.message.edit_caption(caption=f"{cb.message.caption}\n\n✅ APPROVED", reply_markup=None)
            except TelegramBadRequest: pass
            await cb.answer("✅ Deposit approved!")
        else:
            await cb.answer("❌ Failed to approve deposit!", show_alert=True)
    except Exception as ex:
        logger.error(f"approve_dep: {ex}")
        await cb.answer("❌ Error.", show_alert=True)

@AR.callback_query(F.data.startswith("reject_deposit_"))
async def cb_reject_dep(cb: CallbackQuery):
    try:
        if not is_admin(cb.from_user.id):
            await cb.answer("⛔ Access Denied!", show_alert=True)
            return
        tid = int(cb.data.replace("reject_deposit_", ""))
        info = await db.reject_deposit(tid, cb.from_user.id)
        if info:
            try:
                await cb.bot.send_message(info["user_id"],
                    f"❌ <b>Deposit Rejected</b>\n\nAmount: ₹{fmt(info['amount'])}\nPlease contact support.",
                    parse_mode="HTML")
            except Exception: pass
            try:
                await cb.message.edit_caption(caption=f"{cb.message.caption}\n\n❌ REJECTED", reply_markup=None)
            except TelegramBadRequest: pass
            await cb.answer("❌ Deposit rejected!")
        else:
            await cb.answer("❌ Failed to reject deposit!", show_alert=True)
    except Exception as ex:
        logger.error(f"reject_dep: {ex}")
        await cb.answer("❌ Error.", show_alert=True)

# ---- USER MANAGEMENT ----
@AR.callback_query(F.data == "admin_users")
async def cb_users(cb: CallbackQuery, state: FSMContext):
    try:
        if not is_admin(cb.from_user.id):
            await cb.answer("⛔ Access Denied!", show_alert=True)
            return
        await cb.message.edit_text("👥 <b>User Management</b>\n\nEnter user ID to manage:",
                                   reply_markup=KB.back("admin_panel"), parse_mode="HTML")
        await state.set_state(UserManagementStates.waiting_for_user_id)
    except TelegramBadRequest as ex:
        logger.warning(f"users: {ex}")
    except Exception as ex:
        logger.error(f"users: {ex}")
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

@AR.message(UserManagementStates.waiting_for_user_id)
async def msg_user_id(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    try:
        uid = int(msg.text)
        user = await db.get_user(uid)
        if not user:
            await msg.answer("❌ User not found!", reply_markup=KB.back("admin_panel"))
            await state.clear()
            return
        # FIX2: Message source -> answer()
        await msg.answer(user_detail_text(user), reply_markup=KB.user_actions(user.user_id, user.is_banned), parse_mode="HTML")
        await state.clear()
    except ValueError:
        await msg.answer("❌ Please enter a valid user ID!", reply_markup=KB.back("admin_panel"))
    except Exception as ex:
        logger.error(f"msg_user_id: {ex}")
        await msg.answer("❌ Error.", reply_markup=KB.back("admin_panel"))

@AR.callback_query(F.data.startswith("view_user_"))
async def cb_view_user(cb: CallbackQuery):
    try:
        if not is_admin(cb.from_user.id):
            await cb.answer("⛔ Access Denied!", show_alert=True)
            return
        uid = int(cb.data.replace("view_user_",""))
        user = await db.get_user(uid)
        if not user:
            await cb.answer("❌ User not found!", show_alert=True)
            return
        await cb.message.edit_text(user_detail_text(user), reply_markup=KB.user_actions(user.user_id, user.is_banned), parse_mode="HTML")
    except TelegramBadRequest as ex:
        logger.warning(f"view_user: {ex}")
    except Exception as ex:
        logger.error(f"view_user: {ex}")
        await cb.answer("❌ Error.", show_alert=True)
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

@AR.callback_query(F.data.startswith("add_balance_"))
async def cb_add_balance(cb: CallbackQuery, state: FSMContext):
    try:
        if not is_admin(cb.from_user.id):
            await cb.answer("⛔ Access Denied!", show_alert=True)
            return
        uid = int(cb.data.replace("add_balance_",""))
        await state.update_data(target_user=uid)
        await state.set_state(UserManagementStates.waiting_for_balance_amount)
        await cb.message.edit_text(
            f"💰 <b>Add Balance</b>\n\nUser ID: <code>{uid}</code>\n\nEnter amount to add:",
            reply_markup=KB.back(f"view_user_{uid}"), parse_mode="HTML")
    except TelegramBadRequest as ex:
        logger.warning(f"add_balance: {ex}")
    except Exception as ex:
        logger.error(f"add_balance: {ex}")
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

@AR.message(UserManagementStates.waiting_for_balance_amount)
async def msg_balance_amount(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    try:
        amount = float(msg.text)
        if amount <= 0: raise ValueError
        d = await state.get_data()
        uid = d["target_user"]
        await db.add_balance(uid, amount, "admin_add")
        await db.log_admin_action(msg.from_user.id, "add_balance", f"₹{amount} -> {uid}")
        # FIX2: Message -> answer()
        await msg.answer(f"✅ Added ₹{fmt(amount)} to user <code>{uid}</code>",
                         reply_markup=KB.back("admin_panel"), parse_mode="HTML")
        try:
            await msg.bot.send_message(uid, f"💰 <b>Balance Updated</b>\n\n₹{fmt(amount)} added by admin.", parse_mode="HTML")
        except Exception: pass
        await state.clear()
    except ValueError:
        await msg.answer("❌ Please enter a valid amount!", reply_markup=KB.back("admin_panel"))
    except Exception as ex:
        logger.error(f"msg_balance_amount: {ex}")
        await msg.answer("❌ Error.", reply_markup=KB.back("admin_panel"))

@AR.callback_query(F.data.startswith("ban_"))
async def cb_ban(cb: CallbackQuery, state: FSMContext):
    try:
        if not is_admin(cb.from_user.id):
            await cb.answer("⛔ Access Denied!", show_alert=True)
            return
        uid = int(cb.data.replace("ban_",""))
        await state.update_data(ban_user=uid)
        await state.set_state(UserManagementStates.waiting_for_ban_reason)
        await cb.message.edit_text(
            f"🚫 <b>Ban User</b>\n\nUser ID: <code>{uid}</code>\n\nEnter reason (or type 'skip'):",
            reply_markup=KB.back(f"view_user_{uid}"), parse_mode="HTML")
    except TelegramBadRequest as ex:
        logger.warning(f"ban: {ex}")
    except Exception as ex:
        logger.error(f"ban: {ex}")
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

@AR.message(UserManagementStates.waiting_for_ban_reason)
async def msg_ban_reason(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    try:
        d = await state.get_data()
        uid = d["ban_user"]
        reason = msg.text if msg.text.lower() != "skip" else ""
        await db.ban_user(uid, msg.from_user.id, reason)
        await db.log_admin_action(msg.from_user.id, "ban_user", f"Banned {uid}: {reason}")
        # FIX2: Message -> answer()
        await msg.answer(f"✅ User <code>{uid}</code> has been banned.",
                         reply_markup=KB.back("admin_panel"), parse_mode="HTML")
        try:
            await msg.bot.send_message(uid, f"🚫 <b>Account Banned</b>\n\nReason: {e(reason) if reason else 'Violation of terms'}", parse_mode="HTML")
        except Exception: pass
        await state.clear()
    except Exception as ex:
        logger.error(f"msg_ban_reason: {ex}")
        await msg.answer("❌ Error banning user.", reply_markup=KB.back("admin_panel"))

@AR.callback_query(F.data.startswith("unban_"))
async def cb_unban(cb: CallbackQuery):
    try:
        if not is_admin(cb.from_user.id):
            await cb.answer("⛔ Access Denied!", show_alert=True)
            return
        uid = int(cb.data.replace("unban_",""))
        await db.unban_user(uid, cb.from_user.id)
        await db.log_admin_action(cb.from_user.id, "unban_user", f"Unbanned {uid}")
        await cb.message.edit_text(f"✅ User <code>{uid}</code> has been unbanned.",
                                   reply_markup=KB.back(f"view_user_{uid}"), parse_mode="HTML")
        try:
            await cb.bot.send_message(uid, "✅ <b>Account Unbanned</b>\n\nYour account has been unbanned.", parse_mode="HTML")
        except Exception: pass
    except TelegramBadRequest as ex:
        logger.warning(f"unban: {ex}")
    except Exception as ex:
        logger.error(f"unban: {ex}")
        await cb.answer("❌ Error.", show_alert=True)
    finally:
        try:
            await cb.answer()
        except Exception:
            pass

# ==================== MAIN ====================
async def main():
    await db.init_db()
    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(UR)
    dp.include_router(AR)
    logger.info("Bot started (v2 patched - all fixes applied)")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())