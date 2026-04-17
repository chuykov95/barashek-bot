from telegram.ext import ApplicationBuilder
import httpx
import uuid
import asyncio
import logging
import sqlite3
import os
import signal
import weakref
from datetime import datetime, timedelta
from contextlib import contextmanager, asynccontextmanager
from typing import Dict, Set, Optional

from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from yookassa import Configuration, Payment

# ===================================================
# НАСТРОЙКИ
# ===================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TOKEN")
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "YOUR_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "YOUR_SECRET_KEY")
BOT_USERNAME = os.getenv("BOT_USERNAME", "your_bot")
TELEGRAM_PROXY_URL = os.getenv("TELEGRAM_PROXY_URL", "")

# Админ — может добавлять/удалять сотрудников
ADMIN_ID = 925771354

# Путь к базе данных
DB_PATH = "payments.db"

# Таймаут ожидания оплаты (секунды)
PAYMENT_TIMEOUT = 600  # 10 минут
PAYMENT_CHECK_INTERVAL = 5  # секунд между проверками

# Максимальная сумма платежа
MAX_AMOUNT = 500_000.0
MIN_AMOUNT = 1.0
# ===================================================

Configuration.account_id = YOOKASSA_SHOP_ID
Configuration.secret_key = YOOKASSA_SECRET_KEY

# ——— Логирование ———
os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("PaymentBot")


# ===================================================
# УПРАВЛЕНИЕ ЗАДАЧАМИ И РЕСУРСАМИ
# ===================================================
class TaskManager:
    """Управляет фоновыми задачами для предотвращения утечек и зависаний."""
    
    def __init__(self):
        self._active_tasks: Set[asyncio.Task] = set()
        self._payment_watchers: Dict[str, asyncio.Task] = {}
        self._max_concurrent_watchers = 50
        self._api_semaphore = asyncio.Semaphore(10)  # Ограничение одновременных API запросов
        
    def create_payment_watcher(self, payment_id: str, chat_id: int, bot):
        """Создает задачу отслеживания платежа с контролем ресурсов."""
        if payment_id in self._payment_watchers:
            # Проверяем, не завершена ли старая задача
            old_task = self._payment_watchers[payment_id]
            if old_task.done():
                del self._payment_watchers[payment_id]
            else:
                logger.warning(f"Payment watcher for {payment_id} already exists")
                return None
        
        if len(self._payment_watchers) >= self._max_concurrent_watchers:
            logger.error("Maximum concurrent payment watchers reached")
            return None
            
        task = asyncio.create_task(
            check_payment_loop_safe(payment_id, chat_id, bot, self)
        )
        self._payment_watchers[payment_id] = task
        self._active_tasks.add(task)
        
        # Очистка завершенных задач
        task.add_done_callback(lambda t: self._cleanup_task(t, payment_id))
        
        return task
    
    def _cleanup_task(self, task: asyncio.Task, payment_id: str = None):
        """Очищает завершенные задачи."""
        self._active_tasks.discard(task)
        if payment_id and payment_id in self._payment_watchers:
            del self._payment_watchers[payment_id]
    
    async def cancel_payment_watcher(self, payment_id: str):
        """Отменяет задачу отслеживания платежа."""
        if payment_id in self._payment_watchers:
            task = self._payment_watchers[payment_id]
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._cleanup_task(task, payment_id)
            return True
        return False
    
    async def shutdown(self):
        """Завершает все фоновые задачи."""
        logger.info("Shutting down task manager...")
        
        # Отменяем все задачи
        for task in list(self._active_tasks):
            task.cancel()
        
        # Ждем завершения с таймаутом
        if self._active_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._active_tasks, return_exceptions=True),
                    timeout=10.0
                )
            except asyncio.TimeoutError:
                logger.warning("Some tasks didn't finish gracefully")
        
        self._active_tasks.clear()
        self._payment_watchers.clear()
        logger.info("Task manager shutdown complete")
    
    async def with_api_semaphore(self, coro):
        """Выполняет корутину с ограничением одновременных API запросов."""
        async with self._api_semaphore:
            return await coro
    
    def get_stats(self):
        """Возвращает статистику по задачам."""
        return {
            "active_tasks": len(self._active_tasks),
            "payment_watchers": len(self._payment_watchers),
            "max_watchers": self._max_concurrent_watchers
        }


# Глобальный менеджер задач
task_manager = TaskManager()


# ===================================================
# БАЗА ДАННЫХ С УЛУЧШЕННОЙ ОБРАБОТКОЙ
# ===================================================
class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._connection_pool = []
        self._pool_size = 5
        self._lock = asyncio.Lock()
        self._init_db()
        
    async def get_connection(self):
        """Получает соединение из пула."""
        async with self._lock:
            if self._connection_pool:
                return self._connection_pool.pop()
            else:
                # Создаем новое соединение в отдельном потоке
                loop = asyncio.get_running_loop()
                conn = await loop.run_in_executor(None, self._create_connection)
                return conn
    
    async def return_connection(self, conn):
        """Возвращает соединение в пул."""
        async with self._lock:
            if len(self._connection_pool) < self._pool_size:
                self._connection_pool.append(conn)
            else:
                await self._close_connection(conn)
    
    def _create_connection(self):
        """Создает новое соединение с базой данных."""
        conn = sqlite3.connect(self.db_path, timeout=30.0)  # Увеличенный таймаут
        conn.row_factory = sqlite3.Row
        # Включаем WAL режим для лучшей concurrency
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=10000")
        conn.execute("PRAGMA temp_store=MEMORY")
        return conn
    
    async def _close_connection(self, conn):
        """Закрывает соединение."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, conn.close)

    @contextmanager
    def _connect(self):
        """Синхронный контекст для обратной совместимости."""
        conn = self._create_connection()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
    
    @asynccontextmanager
    async def async_connect(self):
        """Асинхронный контекст для работы с БД."""
        conn = await self.get_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            await self.return_connection(conn)

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payment_id TEXT UNIQUE NOT NULL,
                    amount REAL NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_by INTEGER NOT NULL,
                    created_by_name TEXT NOT NULL DEFAULT '',
                    chat_id INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    payment_url TEXT NOT NULL DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS allowed_users (
                    user_id INTEGER PRIMARY KEY,
                    added_by INTEGER NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Добавляем админа если его нет
            conn.execute(
                "INSERT OR IGNORE INTO allowed_users (user_id, added_by, name) VALUES (?, ?, ?)",
                (ADMIN_ID, ADMIN_ID, "Админ"),
            )

    # ——— Пользователи ———
    def is_allowed(self, user_id: int) -> bool:
        if user_id == ADMIN_ID:
            return True
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM allowed_users WHERE user_id = ?", (user_id,)
            ).fetchone()
            return row is not None

    def add_user(self, user_id: int, added_by: int, name: str):
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO allowed_users (user_id, added_by, name) VALUES (?, ?, ?)",
                (user_id, added_by, name),
            )

    def remove_user(self, user_id: int) -> bool:
        if user_id == ADMIN_ID:
            return False
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM allowed_users WHERE user_id = ?", (user_id,)
            )
            return cursor.rowcount > 0

    def list_users(self) -> list:
        with self._connect() as conn:
            return conn.execute(
                "SELECT user_id, name, added_at FROM allowed_users ORDER BY added_at"
            ).fetchall()

    # ——— Платежи ———
    def save_payment(
        self,
        payment_id: str,
        amount: float,
        description: str,
        created_by: int,
        created_by_name: str,
        chat_id: int,
        payment_url: str,
    ):
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO payments 
                   (payment_id, amount, description, created_by, created_by_name, chat_id, payment_url)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (payment_id, amount, description, created_by, created_by_name, chat_id, payment_url),
            )

    def update_payment_status(self, payment_id: str, status: str):
        with self._connect() as conn:
            conn.execute(
                "UPDATE payments SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE payment_id = ?",
                (status, payment_id),
            )

    def get_payment(self, payment_id: str):
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM payments WHERE payment_id = ?", (payment_id,)
            ).fetchone()

    def get_recent_payments(self, limit: int = 10, user_id: int = None):
        with self._connect() as conn:
            if user_id:
                return conn.execute(
                    "SELECT * FROM payments WHERE created_by = ? ORDER BY created_at DESC LIMIT ?",
                    (user_id, limit),
                ).fetchall()
            return conn.execute(
                "SELECT * FROM payments ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()

    def get_today_stats(self):
        with self._connect() as conn:
            today = datetime.now().strftime("%Y-%m-%d")
            row = conn.execute(
                """SELECT 
                     COUNT(*) as total,
                     COALESCE(SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END), 0) as paid,
                     COALESCE(SUM(CASE WHEN status='succeeded' THEN amount ELSE 0 END), 0) as paid_sum,
                     COALESCE(SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END), 0) as pending,
                     COALESCE(SUM(CASE WHEN status='canceled' THEN 1 ELSE 0 END), 0) as canceled
                   FROM payments WHERE DATE(created_at) = ?""",
                (today,),
            ).fetchone()
            return row

    def get_pending_payments(self):
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM payments WHERE status = 'pending' ORDER BY created_at DESC"
            ).fetchall()


db = Database(DB_PATH)


# ===================================================
# КЛАВИАТУРЫ
# ===================================================
def get_main_menu():
    return ReplyKeyboardMarkup(
        [
            ["💳 Создать платёж"],
            ["📋 История", "📊 Статистика"],
            ["⏳ Активные платежи", "ℹ️ Помощь"],
        ],
        resize_keyboard=True,
    )


def get_admin_menu():
    return ReplyKeyboardMarkup(
        [
            ["💳 Создать платёж"],
            ["📋 История", "📊 Статистика"],
            ["⏳ Активные платежи", "ℹ️ Помощь"],
            ["👥 Сотрудники"],
        ],
        resize_keyboard=True,
    )


def menu_for(user_id: int):
    return get_admin_menu() if user_id == ADMIN_ID else get_main_menu()


# ===================================================
# УТИЛИТЫ
# ===================================================
STATUS_EMOJI = {
    "pending": "⏳",
    "succeeded": "✅",
    "canceled": "❌",
    "expired": "⏰",
}


def format_payment_short(p) -> str:
    emoji = STATUS_EMOJI.get(p["status"], "❓")
    dt = p["created_at"][:16].replace("T", " ")
    desc = p["description"]
    if len(desc) > 30:
        desc = desc[:27] + "..."
    return f"{emoji} {p['amount']:.2f}₽ — {desc} ({dt})"


def format_payment_detail(p) -> str:
    emoji = STATUS_EMOJI.get(p["status"], "❓")
    status_text = {
        "pending": "Ожидает оплаты",
        "succeeded": "Оплачен",
        "canceled": "Отменён",
        "expired": "Истёк",
    }.get(p["status"], p["status"])

    return (
        f"{emoji} <b>{status_text}</b>\n\n"
        f"💰 Сумма: <b>{p['amount']:.2f} ₽</b>\n"
        f"📝 Описание: {p['description']}\n"
        f"👤 Создал: {p['created_by_name']}\n"
        f"🕐 Создан: {p['created_at'][:19]}\n"
        f"🆔 <code>{p['payment_id']}</code>"
    )


def get_user_display_name(user) -> str:
    """Получить отображаемое имя пользователя Telegram."""
    parts = []
    if user.first_name:
        parts.append(user.first_name)
    if user.last_name:
        parts.append(user.last_name)
    name = " ".join(parts) if parts else "Неизвестный"
    if user.username:
        name += f" (@{user.username})"
    return name


# ===================================================
# ПРОВЕРКА ОПЛАТЫ (улучшенная версия)
# ===================================================
async def check_payment_with_timeout(func, *args, timeout=30):
    """Выполняет функцию с таймаутом."""
    try:
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(None, func, *args),
            timeout=timeout
        )
    except asyncio.TimeoutError:
        logger.warning(f"API call timeout after {timeout}s")
        raise
    except Exception as e:
        logger.error(f"API call error: {e}")
        raise


async def check_payment_loop_safe(payment_id: str, chat_id: int, bot, task_mgr: TaskManager):
    """Безопасная проверка статуса платежа с управлением ошибками."""
    checks = PAYMENT_TIMEOUT // PAYMENT_CHECK_INTERVAL
    consecutive_errors = 0
    max_consecutive_errors = 3

    for attempt in range(checks):
        try:
            # Проверяем, не была ли задача отменена
            if asyncio.current_task().cancelled():
                logger.info(f"Payment watcher for {payment_id} was cancelled")
                return

            # Используем семафор для ограничения одновременных запросов
            async with task_mgr._api_semaphore:
                yoo_payment = await check_payment_with_timeout(Payment.find_one, payment_id)
                status = yoo_payment.status
                consecutive_errors = 0  # Сбрасываем счетчик ошибок

            if status == "succeeded":
                db.update_payment_status(payment_id, "succeeded")
                p = db.get_payment(payment_id)

                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "✅ <b>Оплата получена!</b>\n\n"
                        f"💰 Сумма: <b>{p['amount']:.2f} ₽</b>\n"
                        f"📝 Описание: {p['description']}\n"
                        f"👤 Создал: {p['created_by_name']}\n"
                        f"🕐 {datetime.now().strftime('%H:%M:%S')}\n"
                        f"🆔 <code>{payment_id}</code>"
                    ),
                    parse_mode="HTML",
                )
                logger.info(f"Payment {payment_id} succeeded — {p['amount']}₽ ({p['description']})")
                return

            if status == "canceled":
                db.update_payment_status(payment_id, "canceled")
                p = db.get_payment(payment_id)

                reason = ""
                if hasattr(yoo_payment, "cancellation_details") and yoo_payment.cancellation_details:
                    reason = f"\n📎 Причина: {yoo_payment.cancellation_details.reason}"

                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "❌ <b>Платёж отменён</b>\n\n"
                        f"💰 Сумма: {p['amount']:.2f} ₽\n"
                        f"📝 {p['description']}"
                        f"{reason}\n"
                        f"🆔 <code>{payment_id}</code>"
                    ),
                    parse_mode="HTML",
                )
                logger.info(f"Payment {payment_id} canceled")
                return

        except asyncio.CancelledError:
            logger.info(f"Payment watcher for {payment_id} cancelled")
            return
        except Exception as e:
            consecutive_errors += 1
            logger.warning(f"Check error for {payment_id} (attempt {attempt+1}, error {consecutive_errors}/{max_consecutive_errors}): {e}")
            
            # Если слишком много ошибок подряд, прекращаем попытки
            if consecutive_errors >= max_consecutive_errors:
                logger.error(f"Too many consecutive errors for {payment_id}, stopping watcher")
                db.update_payment_status(payment_id, "canceled")
                try:
                    p = db.get_payment(payment_id)
                    await bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"⚠️ <b>Проблемы с проверкой платежа</b>\n\n"
                            f"💰 {p['amount']:.2f} ₽ — {p['description']}\n"
                            f"Проверьте статус вручную: /status {payment_id}\n"
                            f"🆔 <code>{payment_id}</code>"
                        ),
                        parse_mode="HTML",
                    )
                except Exception as send_error:
                    logger.error(f"Failed to send error message: {send_error}")
                return

        # Используем asyncio.wait_for для прерываемого сна
        try:
            await asyncio.wait_for(asyncio.sleep(PAYMENT_CHECK_INTERVAL), timeout=PAYMENT_CHECK_INTERVAL + 1)
        except asyncio.CancelledError:
            return

    # Таймаут
    db.update_payment_status(payment_id, "expired")
    p = db.get_payment(payment_id)

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"⏰ <b>Платёж не оплачен</b> (прошло {PAYMENT_TIMEOUT // 60} мин)\n\n"
                f"💰 {p['amount']:.2f} ₽ — {p['description']}\n"
                f"🆔 <code>{payment_id}</code>"
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Failed to send timeout message: {e}")
    
    logger.info(f"Payment {payment_id} expired")


async def check_payment_loop(payment_id: str, chat_id: int, bot):
    """Обратная совместимость - используем новый безопасный метод."""
    task = task_manager.create_payment_watcher(payment_id, chat_id, bot)
    return task


# ===================================================
# ОБРАБОТЧИКИ КОМАНД
# ===================================================

# ——— /start ———
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not db.is_allowed(user.id):
        await update.message.reply_text("⛔ Нет доступа. Обратитесь к администратору.")
        logger.warning(f"Unauthorized access attempt: {user.id} ({get_user_display_name(user)})")
        return

    await update.message.reply_text(
      f"👋 Привет, {user.first_name}!\n\n"
      "Я бот для создания ссылок на оплату.\n\n"
      "📌 <b>Быстрый старт:</b>\n"
      "• Нажми «💳 Создать платёж»\n"
      "• Или введи: /pay 500 Пицца Маргарита\n\n"
      "Когда клиент оплатит — я пришлю уведомление ✅",
      parse_mode="HTML",
      reply_markup=menu_for(user.id),
    )


# ——— /help ———
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not db.is_allowed(user.id):
        return

    text = (
        "📖 <b>Справка по командам</b>\n\n"
        "<b>Основные:</b>\n"
        "/pay <code>сумма</code> <code>описание</code> — создать платёж\n"
        "/pay <code>500</code> — платёж на 500₽\n"
        "/pay <code>1200 Доставка еды</code> — с описанием\n\n"
        "<b>Информация:</b>\n"
        "/history — последние 10 платежей\n"
        "/stats — статистика за сегодня\n"
        "/pending — активные (неоплаченные) платежи\n"
        "/status <code>ID</code> — статус конкретного платежа\n\n"
    )

    if user.id == ADMIN_ID:
        text += (
            "<b>Админ-команды:</b>\n"
            "/adduser <code>ID</code> <code>Имя</code> — добавить сотрудника\n"
            "/removeuser <code>ID</code> — удалить сотрудника\n"
            "/users — список сотрудников\n"
        )

    await update.message.reply_text(text, parse_mode="HTML", reply_markup=menu_for(user.id))


# ——— /pay ———
async def cmd_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not db.is_allowed(user.id):
        await update.message.reply_text("⛔ Нет доступа")
        return

    if not context.args:
        await update.message.reply_text(
            "💳 <b>Создание платежа</b>\n\n"
            "Формат: /pay <code>сумма</code> <code>описание</code>\n\n"
            "Примеры:\n"
            "• /pay 500\n"
            "• /pay 1200 Пицца и напитки\n"
            "• /pay 3500.50 Заказ №142",
            parse_mode="HTML",
        )
        return

    # Парсим сумму
    try:
        amount = float(context.args[0].replace(",", "."))
        if amount < MIN_AMOUNT:
            await update.message.reply_text(f"❌ Минимальная сумма: {MIN_AMOUNT:.2f} ₽")
            return
        if amount > MAX_AMOUNT:
            await update.message.reply_text(f"❌ Максимальная сумма: {MAX_AMOUNT:.0f} ₽")
            return
    except ValueError:
        await update.message.reply_text(
            "❌ Некорректная сумма. Введите число.\n"
            "Пример: /pay 500"
        )
        return

    # Описание
    description = " ".join(context.args[1:]).strip() if len(context.args) > 1 else ""
    if not description:
        description = f"Оплата {amount:.2f}₽"

    if len(description) > 128:
        description = description[:125] + "..."

    chat_id = update.effective_chat.id
    user_name = get_user_display_name(user)
    idempotency_key = str(uuid.uuid4())

    # Отправляем "печатает..."
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        loop = asyncio.get_running_loop()
        yoo_payment = await loop.run_in_executor(
            None,
            lambda: Payment.create(
                {
                    "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
                    "confirmation": {
                        "type": "redirect",
                        "return_url": f"https://t.me/{BOT_USERNAME}",
                    },
                    "capture": True,
                    "description": description,
                    "metadata": {
                        "created_by": str(user.id),
                        "created_by_name": user_name,
                        "bot": BOT_USERNAME,
                    },
                },
                idempotency_key,
            )
        )

        payment_url = yoo_payment.confirmation.confirmation_url
        payment_id = yoo_payment.id

        # Сохраняем в БД
        db.save_payment(
            payment_id=payment_id,
            amount=amount,
            description=description,
            created_by=user.id,
            created_by_name=user_name,
            chat_id=chat_id,
            payment_url=payment_url,
        )

        # Кнопка со ссылкой
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔗 Ссылка для клиента", url=payment_url)],
                [InlineKeyboardButton("📋 Копировать ссылку", callback_data=f"copy_{payment_id}")],
            ]
        )

        await update.message.reply_text(
            f"💳 <b>Платёж создан</b>\n\n"
            f"💰 Сумма: <b>{amount:.2f} ₽</b>\n"
            f"📝 Описание: {description}\n"
            f"🕐 {datetime.now().strftime('%H:%M:%S')}\n\n"
            f"🔗 <code>{payment_url}</code>\n\n"
            f"Отправь ссылку клиенту. Я сообщу, когда оплата пройдёт 👆",
            parse_mode="HTML",
            reply_markup=keyboard,
        )

        logger.info(
            f"Payment created: {payment_id} | {amount}₽ | '{description}' | by {user.id} ({user_name})"
        )

        # Запускаем фоновую проверку
        asyncio.create_task(check_payment_loop(payment_id, chat_id, context.bot))

    except Exception as e:
        logger.error(f"Payment creation error: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ Ошибка при создании платежа.\n\n"
            f"Попробуйте ещё раз или обратитесь к администратору.\n"
            f"<code>{e}</code>",
            parse_mode="HTML",
        )


# ——— /history ———
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not db.is_allowed(user.id):
        return

    # Админ видит все, сотрудник — только свои
    if user.id == ADMIN_ID:
        payments = db.get_recent_payments(limit=15)
        title = "📋 <b>Последние платежи (все)</b>\n\n"
    else:
        payments = db.get_recent_payments(limit=15, user_id=user.id)
        title = "📋 <b>Ваши последние платежи</b>\n\n"

    if not payments:
        await update.message.reply_text("📋 Платежей пока нет", reply_markup=menu_for(user.id))
        return

    lines = [title]
    for p in payments:
        lines.append(format_payment_short(p))

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=menu_for(user.id),
    )


# ——— /stats ———
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not db.is_allowed(user.id):
        return

    s = db.get_today_stats()
    today = datetime.now().strftime("%d.%m.%Y")

    await update.message.reply_text(
        f"📊 <b>Статистика за {today}</b>\n\n"
        f"📦 Всего создано: <b>{s['total']}</b>\n"
        f"✅ Оплачено: <b>{s['paid']}</b>\n"
        f"💰 Сумма оплат: <b>{s['paid_sum']:.2f} ₽</b>\n"
        f"⏳ Ожидают: <b>{s['pending']}</b>\n"
        f"❌ Отменено: <b>{s['canceled']}</b>",
        parse_mode="HTML",
        reply_markup=menu_for(user.id),
    )


# ——— /pending ———
async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not db.is_allowed(user.id):
        return

    payments = db.get_pending_payments()

    if not payments:
        await update.message.reply_text(
            "✨ Нет активных платежей",
            reply_markup=menu_for(user.id),
        )
        return

    lines = ["⏳ <b>Активные платежи (ожидают оплаты)</b>\n"]
    for p in payments:
        lines.append(
            f"• <b>{p['amount']:.2f}₽</b> — {p['description']}\n"
            f"  👤 {p['created_by_name']} | {p['created_at'][:16]}\n"
            f"  🔗 <code>{p['payment_url']}</code>\n"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=menu_for(user.id),
    )


# ——— /status <payment_id> ———
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not db.is_allowed(user.id):
        return

    if not context.args:
        await update.message.reply_text(
            "Формат: /status <code>payment_id</code>",
            parse_mode="HTML",
        )
        return

    payment_id = context.args[0].strip()
    p = db.get_payment(payment_id)

    if not p:
        await update.message.reply_text("❌ Платёж не найден в базе")
        return

    # Дополнительно проверим актуальный статус в ЮKassa
    try:
        loop = asyncio.get_running_loop()
        yoo = await loop.run_in_executor(None, Payment.find_one, payment_id)
        if yoo.status != p["status"]:
            db.update_payment_status(payment_id, yoo.status)
            p = db.get_payment(payment_id)
    except Exception as e:
        logger.warning(f"Could not refresh status for {payment_id}: {e}")

    await update.message.reply_text(
        format_payment_detail(p),
        parse_mode="HTML",
        reply_markup=menu_for(user.id),
    )


# ===================================================
# АДМИН-КОМАНДЫ
# ===================================================

# ——— /adduser ———
async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Только для администратора")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Формат: /adduser <code>telegram_id</code> <code>Имя</code>\n\n"
            "Пример: /adduser 123456789 Иван Продавец",
            parse_mode="HTML",
        )
        return

    try:
        new_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом")
        return

    name = " ".join(context.args[1:])
    db.add_user(new_user_id, ADMIN_ID, name)

    await update.message.reply_text(
        f"✅ Сотрудник добавлен:\n\n"
        f"🆔 <code>{new_user_id}</code>\n"
        f"👤 {name}",
        parse_mode="HTML",
        reply_markup=menu_for(ADMIN_ID),
    )
    logger.info(f"User added: {new_user_id} ({name}) by admin")


# ——— /removeuser ———
async def cmd_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Только для администратора")
        return

    if not context.args:
        await update.message.reply_text(
            "Формат: /removeuser <code>telegram_id</code>",
            parse_mode="HTML",
        )
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом")
        return

    if target_id == ADMIN_ID:
        await update.message.reply_text("❌ Нельзя удалить администратора")
        return

    if db.remove_user(target_id):
        await update.message.reply_text(
            f"✅ Сотрудник <code>{target_id}</code> удалён",
            parse_mode="HTML",
        )
        logger.info(f"User removed: {target_id} by admin")
    else:
        await update.message.reply_text("❌ Пользователь не найден")


# ——— /users ———
async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Только для администратора")
        return

    users = db.list_users()

    if not users:
        await update.message.reply_text("👥 Список пуст")
        return

    lines = ["👥 <b>Список сотрудников</b>\n"]
    for u in users:
        is_admin = " 👑" if u["user_id"] == ADMIN_ID else ""
        lines.append(
            f"• <code>{u['user_id']}</code> — {u['name']}{is_admin}"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=menu_for(ADMIN_ID),
    )


# ===================================================
# ОБРАБОТКА КНОПОК МЕНЮ И CALLBACK
# ===================================================

async def handle_menu_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий на кнопки ReplyKeyboard."""
    user = update.effective_user
    if not db.is_allowed(user.id):
        await update.message.reply_text("⛔ Нет доступа")
        return

    text = update.message.text

    if text == "💳 Создать платёж":
        await update.message.reply_text(
            "💳 <b>Создание платежа</b>\n\n"
            "Введите команду в формате:\n"
            "/pay <code>сумма</code> <code>описание</code>\n\n"
            "Примеры:\n"
            "• /pay 500\n"
            "• /pay 1200 Пицца Маргарита\n"
            "• /pay 3500.50 Заказ №142\n"
            "• /pay 850 Доставка цветов",
            parse_mode="HTML",
        )

    elif text == "📋 История":
        await cmd_history(update, context)

    elif text == "📊 Статистика":
        await cmd_stats(update, context)

    elif text == "⏳ Активные платежи":
        await cmd_pending(update, context)

    elif text == "ℹ️ Помощь":
        await cmd_help(update, context)

    elif text == "👥 Сотрудники":
        await cmd_users(update, context)

    else:
        # Попытка распознать сумму из свободного ввода
        await handle_free_input(update, context)


async def handle_free_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Попытка распознать сумму или сумму + описание из свободного текста."""
    user = update.effective_user
    if not db.is_allowed(user.id):
        return

    text = update.message.text.strip()
    parts = text.split(maxsplit=1)

    if not parts:
        return

    # Пробуем распарсить первое слово как сумму
    try:
        amount = float(parts[0].replace(",", "."))
        if MIN_AMOUNT <= amount <= MAX_AMOUNT:
            # Имитируем вызов /pay
            description = parts[1] if len(parts) > 1 else ""
            if description:
                context.args = [parts[0], *parts[1].split()]
            else:
                context.args = [parts[0]]
            await cmd_pay(update, context)
            return
    except ValueError:
        pass

    await update.message.reply_text(
        "🤔 Не понял команду.\n\n"
        "Используйте /pay <code>сумма</code> или нажмите кнопку меню 👇",
        parse_mode="HTML",
        reply_markup=menu_for(user.id),
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка inline-кнопок."""
    query = update.callback_query
    await query.answer()

    data = query.data

    if data.startswith("copy_"):
        payment_id = data.replace("copy_", "")
        p = db.get_payment(payment_id)

        if p:
            await query.message.reply_text(
                f"📋 <b>Ссылка для отправки клиенту:</b>\n\n"
                f"{p['payment_url']}\n\n"
                f"💰 {p['amount']:.2f} ₽ — {p['description']}",
                parse_mode="HTML",
            )
        else:
            await query.message.reply_text("❌ Платёж не найден")

    elif data.startswith("refresh_"):
        payment_id = data.replace("refresh_", "")

        try:
            yoo = Payment.find_one(payment_id)
            db.update_payment_status(payment_id, yoo.status)
            p = db.get_payment(payment_id)

            if p:
                await query.message.reply_text(
                    f"🔄 <b>Обновлённый статус:</b>\n\n{format_payment_detail(p)}",
                    parse_mode="HTML",
                )
            else:
                await query.message.reply_text("❌ Платёж не найден в базе")

        except Exception as e:
            logger.error(f"Refresh error for {payment_id}: {e}")
            await query.message.reply_text(f"❌ Ошибка обновления: {e}")


# ===================================================
# ОБРАБОТКА ОШИБОК
# ===================================================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Глобальный обработчик ошибок."""
    logger.error(f"Exception while handling update: {context.error}", exc_info=context.error)

    if update and isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ Произошла внутренняя ошибка. Попробуйте ещё раз.",
            )
        except Exception:
            pass


# ===================================================
# POST-INIT: восстановление незавершённых платежей
# ===================================================
async def post_init(application: Application):
    """
    При перезапуске бота проверяем все pending-платежи
    и возобновляем отслеживание.
    """
    logger.info("Post-init: restoring pending payment watchers...")

    pending = db.get_pending_payments()
    restored = 0

    for p in pending:
        # Проверяем, не устарел ли платёж (старше PAYMENT_TIMEOUT)
        try:
            created = datetime.strptime(p["created_at"][:19], "%Y-%m-%d %H:%M:%S")
            age = (datetime.now() - created).total_seconds()

            if age > PAYMENT_TIMEOUT:
                # Проверим финальный статус в ЮKassa
                try:
                    yoo = Payment.find_one(p["payment_id"])
                    db.update_payment_status(p["payment_id"], yoo.status)
                    logger.info(f"Expired payment {p['payment_id']} finalized as {yoo.status}")
                except Exception as e:
                    db.update_payment_status(p["payment_id"], "expired")
                    logger.warning(f"Could not check expired payment {p['payment_id']}: {e}")
                continue

            # Ещё в пределах таймаута — возобновляем отслеживание
            asyncio.create_task(
                check_payment_loop(
                    p["payment_id"],
                    p["chat_id"],
                    application.bot,
                )
            )
            restored += 1

        except Exception as e:
            logger.error(f"Error restoring payment {p['payment_id']}: {e}")

    logger.info(f"Post-init complete: {restored} payment watchers restored, "
                f"{len(pending) - restored} finalized/expired")


# ===================================================
# HEALTH CHECK И МОНИТОРИНГ
# ===================================================
async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Улучшенная проверка состояния бота."""
    if not db.is_allowed(update.effective_user.id):
        return

    stats = db.get_today_stats()
    pending = db.get_pending_payments()
    task_stats = task_manager.get_stats()

    # Проверяем работоспособность БД
    try:
        db_health = "✅"
        with db._connect() as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception:
        db_health = "❌"

    await update.message.reply_text(
        f"🏓 <b>Статус бота</b>\n\n"
        f"⏱ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"🗄️ База данных: {db_health}\n"
        f"⏳ Активных платежей: {len(pending)}\n"
        f"📊 Статистика задач:\n"
        f"  • Активные: {task_stats['active_tasks']}\n"
        f"  • Отслеживание платежей: {task_stats['payment_watchers']}/{task_stats['max_watchers']}\n"
        f"✅ Оплачено сегодня: {stats['paid']}\n"
        f"💰 Сумма оплат: {stats['paid_sum']:.2f} ₽",
        parse_mode="HTML",
    )


async def cmd_system(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Расширенная системная информация (только для админа)."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Только для администратора")
        return

    import psutil
    import sys
    
    # Системная информация
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    
    task_stats = task_manager.get_stats()
    
    # Проверяем долгие задачи
    long_running_tasks = []
    for payment_id, task in task_manager._payment_watchers.items():
        if not task.done():
            # Здесь можно добавить логику отслеживания времени выполнения
            long_running_tasks.append(f"• {payment_id}")
    
    system_info = (
        f"🖥️ <b>Системная информация</b>\n\n"
        f"💾 RAM: {memory.percent}% ({memory.used // 1024 // 1024}MB/{memory.total // 1024 // 1024}MB)\n"
        f"💽 Диск: {disk.percent}% ({disk.used // 1024 // 1024 // 1024}GB/{disk.total // 1024 // 1024 // 1024}GB)\n"
        f"🐍 Python: {sys.version.split()[0]}\n"
        f"⚡ Процессов: {len(psutil.pids())}\n\n"
        f"📊 <b>Задачи бота</b>\n"
        f"• Активные задачи: {task_stats['active_tasks']}\n"
        f"• Отслеживание платежей: {task_stats['payment_watchers']}\n"
        f"• Максимум отслеживания: {task_stats['max_watchers']}\n"
    )
    
    if long_running_tasks:
        system_info += f"\n🔄 <b>Долгие задачи</b>:\n" + "\n".join(long_running_tasks[:5])
        if len(long_running_tasks) > 5:
            system_info += f"\n... и еще {len(long_running_tasks) - 5}"

    await update.message.reply_text(system_info, parse_mode="HTML")


# ===================================================
# ГРАЦИОЗНЫЙ ЗАВЕРШЕНИЕ
# ===================================================
class BotShutdown:
    """Управляет gracious shutdown бота."""
    
    def __init__(self):
        self.shutdown_requested = False
        
    async def signal_handler(self, signum, frame):
        """Обработчик сигналов для graceful shutdown."""
        logger.info(f"Received signal {signum}, initiating graceful shutdown...")
        self.shutdown_requested = True
        await self.shutdown()
    
    async def shutdown(self):
        """Выполняет graceful shutdown всех компонентов."""
        logger.info("🔄 Starting graceful shutdown...")
        
        try:
            # 1. Останавливаем task manager
            await task_manager.shutdown()
            
            # 2. Закрываем соединения с БД
            if hasattr(db, '_connection_pool'):
                for conn in db._connection_pool:
                    try:
                        conn.close()
                    except Exception:
                        pass
                db._connection_pool.clear()
            
            logger.info("✅ Graceful shutdown completed")
            
        except Exception as e:
            logger.error(f"❌ Error during shutdown: {e}")


shutdown_manager = BotShutdown()


# ===================================================
# ЗАПУСК С УЛУЧШЕННОЙ ОБРАБОТКОЙ
# ===================================================
def main():
    logger.info("=" * 50)
    logger.info("Starting Payment Bot...")
    logger.info(f"Admin ID: {ADMIN_ID}")
    logger.info(f"Database: {DB_PATH}")
    logger.info(f"Payment timeout: {PAYMENT_TIMEOUT}s")
    if TELEGRAM_PROXY_URL:
        logger.info(f"Proxy: {TELEGRAM_PROXY_URL}")
    logger.info("=" * 50)

    # Устанавливаем обработчики сигналов
    import signal
    signal.signal(signal.SIGINT, lambda s, f: asyncio.create_task(shutdown_manager.signal_handler(s, f)))
    signal.signal(signal.SIGTERM, lambda s, f: asyncio.create_task(shutdown_manager.signal_handler(s, f)))

    # ✅ Настройка с прокси
    builder = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init)

    if TELEGRAM_PROXY_URL:
        builder = builder.proxy(TELEGRAM_PROXY_URL).get_updates_proxy(TELEGRAM_PROXY_URL)

    app = builder.build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("pay", cmd_pay))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("system", cmd_system))

    # Админ-команды
    app.add_handler(CommandHandler("adduser", cmd_adduser))
    app.add_handler(CommandHandler("removeuser", cmd_removeuser))
    app.add_handler(CommandHandler("users", cmd_users))

    # Inline-кнопки
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Текстовые сообщения
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_buttons))

    # Ошибки
    app.add_error_handler(error_handler)

    logger.info("🤖 Bot is running. Press Ctrl+C to stop.")
    
    try:
        app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    except KeyboardInterrupt:
        logger.info("🛑 Received interrupt signal")
    finally:
        # Гарантированный shutdown
        if not shutdown_manager.shutdown_requested:
            logger.info("🔄 Running final shutdown...")
            asyncio.run(shutdown_manager.shutdown())


if __name__ == "__main__":
    main()
