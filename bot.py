import uuid
import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from yookassa import Configuration, Payment

# ===================================================
# НАСТРОЙКИ
# ===================================================
TELEGRAM_BOT_TOKEN = "8714406422:AAG9J7tsZUHYrlcj23XDM9qc9vtQgbjBDdM"
YOOKASSA_SHOP_ID = "1317214"
YOOKASSA_SECRET_KEY = "test_cDBfWTHLt2nWaBRcINnbSq9Hq3Iu9SggYdsUfD_kdbU"
BOT_USERNAME = "BarashekBgdBot"
# ===================================================

Configuration.account_id = YOOKASSA_SHOP_ID
Configuration.secret_key = YOOKASSA_SECRET_KEY

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ——— Команда /start ———
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет!\n\n"
        "Отправь мне сумму в рублях — я создам ссылку на оплату.\n\n"
        "Например: 500"
    )


# ——— Пользователь отправил сумму ———
async def handle_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", ".")

    try:
        amount = float(text)
        if amount < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введи корректную сумму (минимум 1 ₽)")
        return

    context.user_data["amount"] = amount

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"💳 Оплатить {amount:.2f} ₽", callback_data="pay")]]
    )

    await update.message.reply_text(
        f"💰 Сумма к оплате: **{amount:.2f} ₽**\n\n"
        f"Нажми кнопку ниже:",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


# ——— Фоновая проверка статуса платежа ———
async def check_payment(payment_id: str, chat_id: int, amount: float, bot):
    """Проверяем статус каждые 5 секунд, максимум 5 минут"""

    for i in range(60):  # 60 * 5 сек = 5 минут
        try:
            payment = Payment.find_one(payment_id)

            if payment.status == "succeeded":
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"✅ **Оплата прошла успешно!**\n\n"
                        f"💰 Сумма: {amount:.2f} ₽\n"
                        f"🆔 Платёж: `{payment_id}`\n\n"
                        f"Спасибо! 🎉"
                    ),
                    parse_mode="Markdown",
                )
                logger.info(f"✅ Payment {payment_id} succeeded")
                return

            if payment.status == "canceled":
                await bot.send_message(
                    chat_id=chat_id,
                    text="❌ Оплата отменена.",
                )
                logger.info(f"❌ Payment {payment_id} canceled")
                return

            logger.info(
                f"⏳ Payment {payment_id} status: {payment.status} "
                f"(проверка {i + 1}/60)"
            )

        except Exception as e:
            logger.error(f"Ошибка проверки платежа: {e}")

        await asyncio.sleep(5)

    # Время вышло
    await bot.send_message(
        chat_id=chat_id,
        text="⏰ Время на оплату истекло (5 минут). Отправь сумму заново.",
    )
    logger.info(f"⏰ Payment {payment_id} expired")


# ——— Нажатие кнопки "Оплатить" ———
async def pay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    amount = context.user_data.get("amount")
    if not amount:
        await query.edit_message_text("❌ Сумма не найдена. Отправь заново.")
        return

    chat_id = query.message.chat_id

    try:
        # Создаём платёж в ЮKassa
        payment = Payment.create(
            {
                "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
                "confirmation": {
                    "type": "redirect",
                    "return_url": f"https://t.me/{BOT_USERNAME}",
                },
                "capture": True,
                "description": f"Оплата {amount:.2f} ₽ от пользователя {chat_id}",
            },
            str(uuid.uuid4()),
        )

        payment_url = payment.confirmation.confirmation_url

        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("💳 Перейти к оплате", url=payment_url)]]
        )

        await query.edit_message_text(
            f"🔗 Ссылка на оплату **{amount:.2f} ₽** создана!\n\n"
            f"После оплаты я пришлю подтверждение ✅\n"
            f"Ожидание — до 5 минут.",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

        logger.info(f"🔗 Payment created: {payment.id} | {amount:.2f} ₽")

        # Запускаем фоновую проверку
        asyncio.create_task(
            check_payment(payment.id, chat_id, amount, context.bot)
        )

    except Exception as e:
        logger.error(f"Ошибка создания платежа: {e}")
        await query.edit_message_text(f"❌ Ошибка: {e}")


# ——— Запуск ———
def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(pay_callback, pattern="pay"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount))

    logger.info("🤖 Бот запущен!")
    app.run_polling()


if __name__ == "__main__":
    main()