import uuid
import asyncio
import logging
from telegram import Update
from telegram import ReplyKeyboardMarkup
from telegram.ext import (
  Application,
  CommandHandler,
  ContextTypes,
  MessageHandler, 
  filters
)
from yookassa import Configuration, Payment

# ===================================================
# НАСТРОЙКИ
# ===================================================
TELEGRAM_BOT_TOKEN = "8714406422:AAG9J7tsZUHYrlcj23XDM9qc9vtQgbjBDdM"
YOOKASSA_SHOP_ID = "1317214"
YOOKASSA_SECRET_KEY = "test_cDBfWTHLt2nWaBRcINnbSq9Hq3Iu9SggYdsUfD_kdbU"
BOT_USERNAME = "BarashekBgdBot"

ALLOWED_USERS = [925771354]  # Telegram ID сотрудников
# ===================================================

Configuration.account_id = YOOKASSA_SHOP_ID
Configuration.secret_key = YOOKASSA_SECRET_KEY

logging.basicConfig(
  format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
  level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ——— Проверка доступа ———
def is_allowed(user_id: int) -> bool:
  return user_id in ALLOWED_USERS

def get_menu():
  return ReplyKeyboardMarkup(
    [
      ["💳 Создать платёж"],
      ["ℹ️ Помощь"],
    ],
    resize_keyboard=True
  )

# ——— Команда /start ———
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
  if not is_allowed(update.effective_user.id):
    await update.message.reply_text("⛔ Нет доступа")
    return

  await update.message.reply_text(
    "👋 Бот для создания ссылок оплаты\n\n"
    "Выбери действие ниже 👇",
    reply_markup=get_menu()
  )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
  if not is_allowed(update.effective_user.id):
    return

  await update.message.reply_text(
    "📌 Команды:\n\n"
    "/pay 500 — создать платёж\n"
    "/pay 1200 Пицца — с описанием\n\n"
    "Или нажми кнопку 👇",
    reply_markup=get_menu()
  )

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
  if not is_allowed(update.effective_user.id):
    return

  text = update.message.text

  if text == "💳 Создать платёж":
    await update.message.reply_text(
      "Введи сумму:\n\nНапример:\n/pay 500"
    )

  elif text == "ℹ️ Помощь":
    await help_command(update, context)

# ——— Фоновая проверка оплаты ———
async def check_payment(payment_id: str, chat_id: int, amount: float, bot):
  for _ in range(60):  # 5 минут
    try:
      payment = Payment.find_one(payment_id)

      if payment.status == "succeeded":
        await bot.send_message(
          chat_id=chat_id,
          text=(
            f"✅ Оплата прошла\n\n"
            f"💰 {amount:.2f} ₽\n"
            f"🆔 {payment_id}"
          ),
        )
        return

      if payment.status == "canceled":
        await bot.send_message(
          chat_id=chat_id,
          text="❌ Оплата отменена",
        )
        return

    except Exception as e:
      logger.error(f"Ошибка проверки: {e}")

    await asyncio.sleep(5)

  await bot.send_message(
    chat_id=chat_id,
    text="⏰ Платёж не завершён (5 минут)",
  )


# ——— Команда /pay ———
async def pay_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
  user_id = update.effective_user.id

  if not is_allowed(user_id):
    await update.message.reply_text("⛔ Нет доступа")
    return

  if not context.args:
    await update.message.reply_text("❗ Используй: /pay 500")
    return

  try:
    amount = float(context.args[0].replace(",", "."))
    if amount < 1:
      raise ValueError
  except ValueError:
    await update.message.reply_text("❌ Некорректная сумма")
    return

  description = " ".join(context.args[1:]) if len(context.args) > 1 else "Оплата"

  chat_id = update.effective_chat.id

  try:
    payment = Payment.create(
      {
        "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
        "confirmation": {
          "type": "redirect",
          "return_url": f"https://t.me/{BOT_USERNAME}",
        },
        "capture": True,
        "description": description,
      },
      str(uuid.uuid4()),
    )

    payment_url = payment.confirmation.confirmation_url

    await update.message.reply_text(
      f"🔗 Ссылка на оплату:\n{payment_url}\n\n"
      f"💰 {amount:.2f} ₽\n"
      f"📝 {description}\n\n"
      f"Отправь клиенту 👆"
    )

    logger.info(f"Payment created: {payment.id}")

    asyncio.create_task(
      check_payment(payment.id, chat_id, amount, context.bot)
    )

  except Exception as e:
    logger.error(f"Ошибка создания: {e}")
    await update.message.reply_text(f"❌ Ошибка: {e}")


# ——— Запуск ———
def main():
  app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

  app.add_handler(CommandHandler("start", start))
  app.add_handler(CommandHandler("pay", pay_command))
  app.add_handler(CommandHandler("help", help_command))
  app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))

  logger.info("🤖 Бот запущен")
  app.run_polling()


if __name__ == "__main__":
  main()