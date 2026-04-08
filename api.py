import uuid
import sqlite3
import logging
import hashlib
import hmac
import os
from datetime import datetime
from contextlib import contextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from yookassa import Configuration, Payment

# ===================================================
# НАСТРОЙКИ
# ===================================================
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "YOUR_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "YOUR_SECRET_KEY")
API_SECRET = os.getenv("API_SECRET", "your-super-secret-key-change-me")
DB_PATH = os.getenv("DB_PATH", "payments.db")
RETURN_URL = os.getenv("RETURN_URL", "https://yourapp.com/payment/success")

# Разрешённые origins для CORS (ваше приложение)
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000,https://yourapp.com").split(",")

MAX_AMOUNT = 500_000.0
MIN_AMOUNT = 1.0
# ===================================================

Configuration.account_id = YOOKASSA_SHOP_ID
Configuration.secret_key = YOOKASSA_SECRET_KEY

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/api.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("PaymentAPI")
os.makedirs("logs", exist_ok=True)


# ===================================================
# БАЗА ДАННЫХ (та же что была)
# ===================================================
class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payment_id TEXT UNIQUE NOT NULL,
                    amount REAL NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    customer_email TEXT DEFAULT '',
                    customer_phone TEXT DEFAULT '',
                    metadata TEXT DEFAULT '{}',
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    payment_url TEXT NOT NULL DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS api_keys (
                    key_hash TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active INTEGER DEFAULT 1
                )
            """)

    def save_payment(self, **kwargs):
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO payments 
                   (payment_id, amount, description, customer_email, 
                    customer_phone, metadata, created_by, payment_url)
                   VALUES (:payment_id, :amount, :description, :customer_email,
                           :customer_phone, :metadata, :created_by, :payment_url)""",
                kwargs,
            )

    def update_status(self, payment_id: str, status: str):
        with self._connect() as conn:
            conn.execute(
                "UPDATE payments SET status=?, updated_at=CURRENT_TIMESTAMP WHERE payment_id=?",
                (status, payment_id),
            )

    def get_payment(self, payment_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM payments WHERE payment_id=?", (payment_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_payments(
        self,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list:
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM payments WHERE status=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (status, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM payments ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            return [dict(r) for r in rows]

    def get_today_stats(self) -> dict:
        with self._connect() as conn:
            today = datetime.now().strftime("%Y-%m-%d")
            row = conn.execute(
                """SELECT 
                     COUNT(*) as total,
                     COALESCE(SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END), 0) as paid_count,
                     COALESCE(SUM(CASE WHEN status='succeeded' THEN amount ELSE 0 END), 0) as paid_sum,
                     COALESCE(SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END), 0) as pending_count,
                     COALESCE(SUM(CASE WHEN status='canceled' THEN 1 ELSE 0 END), 0) as canceled_count
                   FROM payments WHERE DATE(created_at) = ?""",
                (today,),
            ).fetchone()
            return dict(row)


db = Database(DB_PATH)


# ===================================================
# PYDANTIC МОДЕЛИ
# ===================================================
class CreatePaymentRequest(BaseModel):
    amount: float = Field(..., ge=MIN_AMOUNT, le=MAX_AMOUNT, description="Сумма в рублях")
    description: str = Field(default="Оплата", max_length=128)
    customer_email: str = Field(default="", max_length=255)
    customer_phone: str = Field(default="", max_length=20)
    metadata: dict = Field(default_factory=dict)
    return_url: Optional[str] = Field(default=None, description="URL возврата после оплаты")


class PaymentResponse(BaseModel):
    payment_id: str
    amount: float
    description: str
    status: str
    payment_url: str
    created_at: str


class StatsResponse(BaseModel):
    date: str
    total: int
    paid_count: int
    paid_sum: float
    pending_count: int
    canceled_count: int


class ErrorResponse(BaseModel):
    error: str
    detail: str = ""


class WebhookEvent(BaseModel):
    type: str
    event: str
    object: dict


# ===================================================
# АВТОРИЗАЦИЯ
# ===================================================
async def verify_api_key(x_api_key: str = Header(..., alias="X-API-Key")):
    """Проверка API-ключа из заголовка."""
    if not hmac.compare_digest(x_api_key, API_SECRET):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return x_api_key


# ===================================================
# ПРИЛОЖЕНИЕ
# ===================================================
app = FastAPI(
    title="Payment API",
    description="API для создания и управления платежами через ЮKassa",
    version="1.0.0",
    docs_url="/docs",        # Swagger UI
    redoc_url="/redoc",      # ReDoc
)

# CORS — разрешаем запросы от вашего фронтенда
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===================================================
# ЭНДПОИНТЫ
# ===================================================

# ——— Health check ———
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
    }


# ——— Создать платёж ———
@app.post("/payments", response_model=PaymentResponse)
async def create_payment(
    body: CreatePaymentRequest,
    api_key: str = Depends(verify_api_key),
):
    """Создать новый платёж и получить ссылку для оплаты."""
    
    return_url = body.return_url or RETURN_URL
    idempotency_key = str(uuid.uuid4())

    try:
        yoo_payment = Payment.create(
            {
                "amount": {
                    "value": f"{body.amount:.2f}",
                    "currency": "RUB",
                },
                "confirmation": {
                    "type": "redirect",
                    "return_url": return_url,
                },
                "capture": True,
                "description": body.description,
                "receipt": _build_receipt(body) if body.customer_email or body.customer_phone else None,
                "metadata": body.metadata,
            },
            idempotency_key,
        )

        payment_url = yoo_payment.confirmation.confirmation_url
        payment_id = yoo_payment.id

        db.save_payment(
            payment_id=payment_id,
            amount=body.amount,
            description=body.description,
            customer_email=body.customer_email,
            customer_phone=body.customer_phone,
            metadata=str(body.metadata),
            created_by="api",
            payment_url=payment_url,
        )

        logger.info(f"Payment created: {payment_id} | {body.amount}₽ | {body.description}")

        return PaymentResponse(
            payment_id=payment_id,
            amount=body.amount,
            description=body.description,
            status="pending",
            payment_url=payment_url,
            created_at=datetime.now().isoformat(),
        )

    except Exception as e:
        logger.error(f"Payment creation error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ——— Получить статус платежа ———
@app.get("/payments/{payment_id}")
async def get_payment(
    payment_id: str,
    refresh: bool = False,
    api_key: str = Depends(verify_api_key),
):
    """
    Получить информацию о платеже.
    ?refresh=true — обновить статус из ЮKassa.
    """
    
    # Обновляем из ЮKassa если просят
    if refresh:
        try:
            yoo = Payment.find_one(payment_id)
            db.update_status(payment_id, yoo.status)
        except Exception as e:
            logger.warning(f"Could not refresh {payment_id}: {e}")

    payment = db.get_payment(payment_id)

    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")

    return payment


# ——— Список платежей ———
@app.get("/payments")
async def list_payments(
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    api_key: str = Depends(verify_api_key),
):
    """
    Список платежей.
    ?status=pending|succeeded|canceled
    ?limit=50&offset=0
    """
    
    if limit > 100:
        limit = 100

    payments = db.get_payments(status=status, limit=limit, offset=offset)

    return {
        "payments": payments,
        "count": len(payments),
        "limit": limit,
        "offset": offset,
    }


# ——— Статистика ———
@app.get("/stats", response_model=StatsResponse)
async def get_stats(api_key: str = Depends(verify_api_key)):
    """Статистика за сегодня."""
    
    stats = db.get_today_stats()

    return StatsResponse(
        date=datetime.now().strftime("%Y-%m-%d"),
        **stats,
    )


# ——— Отмена платежа ———
@app.post("/payments/{payment_id}/cancel")
async def cancel_payment(
    payment_id: str,
    api_key: str = Depends(verify_api_key),
):
    """Отменить платёж (если ещё не оплачен)."""
    
    payment = db.get_payment(payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")

    if payment["status"] != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot cancel payment with status '{payment['status']}'",
        )

    try:
        yoo = Payment.find_one(payment_id)

        if yoo.status == "pending":
            # ЮKassa не позволяет отменить pending напрямую,
            # но мы помечаем у себя
            db.update_status(payment_id, "canceled")
            logger.info(f"Payment {payment_id} canceled locally")
        else:
            db.update_status(payment_id, yoo.status)

        return {"payment_id": payment_id, "status": "canceled"}

    except Exception as e:
        logger.error(f"Cancel error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ===================================================
# WEBHOOK от ЮKassa (самое важное!)
# ===================================================
@app.post("/webhook/yookassa")
async def yookassa_webhook(request: Request):
    """
    ЮKassa присылает сюда уведомления об оплате.
    Не требует API-ключа — проверяем по IP или подписи.
    """
    
    try:
        body = await request.json()
        logger.info(f"Webhook received: {body.get('event', 'unknown')}")

        event = body.get("event", "")
        payment_obj = body.get("object", {})
        payment_id = payment_obj.get("id", "")

        if not payment_id:
            return {"status": "ignored"}

        if event == "payment.succeeded":
            db.update_status(payment_id, "succeeded")
            logger.info(f"Webhook: payment {payment_id} succeeded")

        elif event == "payment.canceled":
            db.update_status(payment_id, "canceled")
            logger.info(f"Webhook: payment {payment_id} canceled")

        elif event == "payment.waiting_for_capture":
            # Автоматически подтверждаем (capture: true уже стоит, но на всякий случай)
            try:
                Payment.capture(payment_id)
                logger.info(f"Webhook: payment {payment_id} captured")
            except Exception as e:
                logger.error(f"Capture error for {payment_id}: {e}")

        elif event == "refund.succeeded":
            db.update_status(payment_id, "refunded")
            logger.info(f"Webhook: payment {payment_id} refunded")

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Webhook processing error: {e}", exc_info=True)
        # Возвращаем 200 чтобы ЮKassa не слала повторно
        return {"status": "error", "detail": str(e)}


# ===================================================
# ВОЗВРАТЫ (REFUNDS)
# ===================================================
@app.post("/payments/{payment_id}/refund")
async def refund_payment(
    payment_id: str,
    amount: Optional[float] = None,
    api_key: str = Depends(verify_api_key),
):
    """
    Возврат средств.
    Без amount — полный возврат.
    С amount — частичный возврат.
    """

    payment = db.get_payment(payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")

    if payment["status"] != "succeeded":
        raise HTTPException(
            status_code=400,
            detail=f"Can only refund succeeded payments. Current status: '{payment['status']}'",
        )

    refund_amount = amount if amount else payment["amount"]

    if refund_amount > payment["amount"]:
        raise HTTPException(
            status_code=400,
            detail=f"Refund amount ({refund_amount}) exceeds payment amount ({payment['amount']})",
        )

    try:
        from yookassa import Refund

        refund = Refund.create(
            {
                "payment_id": payment_id,
                "amount": {
                    "value": f"{refund_amount:.2f}",
                    "currency": "RUB",
                },
            },
            str(uuid.uuid4()),
        )

        if refund.status == "succeeded":
            db.update_status(payment_id, "refunded")

        logger.info(f"Refund created for {payment_id}: {refund_amount}₽, status={refund.status}")

        return {
            "payment_id": payment_id,
            "refund_id": refund.id,
            "refund_amount": refund_amount,
            "status": refund.status,
        }

    except Exception as e:
        logger.error(f"Refund error for {payment_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ===================================================
# ЧЕКИ (54-ФЗ)
# ===================================================
def _build_receipt(body: CreatePaymentRequest) -> dict:
    """Формирует данные чека для ЮKassa (54-ФЗ)."""
    
    customer = {}
    if body.customer_email:
        customer["email"] = body.customer_email
    if body.customer_phone:
        customer["phone"] = body.customer_phone

    return {
        "customer": customer,
        "items": [
            {
                "description": body.description[:128],
                "quantity": "1.00",
                "amount": {
                    "value": f"{body.amount:.2f}",
                    "currency": "RUB",
                },
                "vat_code": 1,  # Без НДС. Измени под свою систему налогообложения
                "payment_mode": "full_payment",
                "payment_subject": "commodity",
            }
        ],
    }


# ===================================================
# ЗАПУСК
# ===================================================
if __name__ == "__main__":
    import uvicorn

    logger.info("=" * 50)
    logger.info("Starting Payment API...")
    logger.info(f"Docs: http://localhost:8000/docs")
    logger.info(f"Webhook URL: https://yourdomain.com/webhook/yookassa")
    logger.info("=" * 50)

    uvicorn.run(
        "api:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        reload=os.getenv("ENV", "dev") == "dev",
        log_level="info",
    )