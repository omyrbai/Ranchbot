from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl
import html
import re

from pydantic import BaseModel, Field
import pymysql
import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.config import FRONTEND_DIR, GENERATED_DIR, settings
from app.services.dataset_store import atomic_write_json, load_json
from app.services.excel_processor import (
    WorkbookValidationError,
    parse_contracts,
    parse_credentials,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = FRONTEND_DIR / "index.html"
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_TG_ID = int(os.getenv("ADMIN_TG_ID", "0"))
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_NAME = os.getenv("DB_NAME", "ranchbot_db")
DB_USER = os.getenv("DB_USER", "ranchbot_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
WEB_APP_URL = os.getenv("WEB_APP_URL", "https://regofis.shabyte.uz").strip().rstrip("/")
GROUP_TGID = int(os.getenv("GR_TG_ID", "0"))
PHONE_CHANGE_TOPIC_ID = int(os.getenv("PHONE_CHANGE_TOPIC_ID", "0"))
DISCR_TOPIC_ID = int(os.getenv("DISCR_TOPIC_ID", "0"))
TELEGRAM_INIT_DATA_MAX_AGE_SECONDS = int(
    os.getenv("TELEGRAM_INIT_DATA_MAX_AGE_SECONDS", "86400")
)
STUDENT_ID_RE = re.compile(r"^[A-Z]{2}\d{7}$")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ranchbot")
GENERATED_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Database: reuse ranchbot_db.users; do not create a second users table.
# ---------------------------------------------------------------------------

class PhoneChangeRequest(BaseModel):
    id_number: str = Field(min_length=9, max_length=9)
    new_phone: str = Field(min_length=12, max_length=13)

def get_db_connection():
    if not DB_PASSWORD:
        raise RuntimeError("DB_PASSWORD is missing from .env")
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
        autocommit=True,
    )


def initialize_database():
    """Check the existing users table; create it only if it does not exist."""
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                    tgid BIGINT NOT NULL UNIQUE,
                    phone VARCHAR(20) NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                  COLLATE=utf8mb4_unicode_ci
                """
            )
            cursor.execute("SHOW COLUMNS FROM users")
            columns = {row["Field"] for row in cursor.fetchall()}
            required = {"tgid", "phone"}
            if not required.issubset(columns):
                raise RuntimeError(
                    "The existing users table must contain tgid and phone columns. "
                    f"Found columns: {', '.join(sorted(columns))}"
                )
    finally:
        connection.close()


def get_registered_user(tgid: int):
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT tgid, phone FROM users WHERE tgid = %s LIMIT 1", (tgid,)
            )
            return cursor.fetchone()
    finally:
        connection.close()


def register_user(tgid: int, phone: str):
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO users (tgid, phone) VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE phone = VALUES(phone)
                """,
                (tgid, phone),
            )
    finally:
        connection.close()

# ---------------------------------------------------------------------------
# Telegram keyboards and handlers
# ---------------------------------------------------------------------------


def registration_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Share my phone number", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def contract_menu_keyboard():
    return ReplyKeyboardMarkup(
        [["🔎 Find"], ["📝 Discrepancy Report"], ["⬅️ Main Menu"]],
        resize_keyboard=True,
    )


def mini_app_keyboard():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🚀 Open the bot", web_app=WebAppInfo(url=WEB_APP_URL))]]
    )


async def send_registered_menu(message):
    await message.reply_text(
        "✅ Your phone number is registered. Open the Mini App to use student services:",
        reply_markup=ReplyKeyboardRemove(),
    )
    await message.reply_text("🚀 Tap below to open the Mini app:", reply_markup=mini_app_keyboard())

# ---------------------------------------------------------------------------
# Admin Excel upload handlers
# ---------------------------------------------------------------------------

def is_admin(update: Update) -> bool:
    """Allow uploads only from the configured Telegram admin in private chat."""
    user = update.effective_user
    chat = update.effective_chat

    return (
        user is not None
        and chat is not None
        and chat.type == "private"
        and ADMIN_TG_ID != 0
        and user.id == ADMIN_TG_ID
    )


async def upload_hemis_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if not message:
        return

    if not is_admin(update):
        await message.reply_text("⛔ You are not authorized to upload files.")
        return

    context.user_data["awaiting_admin_upload"] = "hemis"

    await message.reply_text(
        "📄 HEMIS upload selected.\n\n"
        "Please send the HEMIS Excel file as a document (.xlsx).\n"
        "The first sheet must contain student login and password data."
    )


async def upload_contracts_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if not message:
        return

    if not is_admin(update):
        await message.reply_text("⛔ You are not authorized to upload files.")
        return

    context.user_data["awaiting_admin_upload"] = "contracts"

    await message.reply_text(
        "📄 Contract upload selected.\n\n"
        "Please send the contract Excel file as a document (.xlsx).\n"
        "The first sheet must contain student ID, total amount, "
        "paid amount, and payment percentage."
    )


async def receive_admin_excel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if not message or not is_admin(update):
        return

    upload_type = context.user_data.get("awaiting_admin_upload")

    # Do not process documents unless an upload command was issued first.
    if upload_type not in {"hemis", "contracts"}:
        return

    document = message.document

    if document is None:
        return

    filename = document.file_name or ""

    if not filename.lower().endswith(".xlsx"):
        await message.reply_text(
            "❌ Please send an Excel file with the .xlsx extension."
        )
        return

    max_bytes = settings.max_upload_mb * 1024 * 1024

    if document.file_size is not None and document.file_size > max_bytes:
        await message.reply_text(
            f"❌ File is too large. Maximum size: "
            f"{settings.max_upload_mb} MB."
        )
        return

    await message.reply_text("⏳ Downloading and validating your Excel file...")

    try:
        telegram_file = await context.bot.get_file(document.file_id)
        content = bytes(await telegram_file.download_as_bytearray())

        if not content:
            await message.reply_text("❌ The uploaded file is empty.")
            return

        if len(content) > max_bytes:
            await message.reply_text(
                f"❌ File exceeds the {settings.max_upload_mb} MB limit."
            )
            return

        # Parse and validate BEFORE replacing the existing dataset.
        if upload_type == "hemis":
            dataset = await asyncio.to_thread(parse_credentials, content)
            destination = GENERATED_DIR / "login.json"
            dataset_name = "HEMIS credentials"

        else:
            dataset = await asyncio.to_thread(parse_contracts, content)
            destination = GENERATED_DIR / "contract.json"
            dataset_name = "contract payment data"

        if not dataset:
            await message.reply_text(
                "❌ No valid records were found. "
                "The existing dataset has not been changed."
            )
            return

        # Atomically replace the dataset only after successful validation.
        await asyncio.to_thread(atomic_write_json, destination, dataset)

        context.user_data.pop("awaiting_admin_upload", None)

        record_count = (
            len(dataset["students"])
            if upload_type == "contracts"
            else len(dataset)
        )

        confirmation = (
            f"✅ {dataset_name.capitalize()} uploaded successfully!\n\n"
            f"📊 Records imported: {record_count}\n"
            f"📁 Dataset: {destination.name}"
        )

        if upload_type == "contracts":
            confirmation += f"\n📅 Data updated on: {dataset['update_date']}"

        await message.reply_text(confirmation)


        logger.info(
            "Admin %s uploaded %s records to %s",
            update.effective_user.id,
            len(dataset),
            destination.name,
        )

    except WorkbookValidationError as exc:
        logger.warning("Admin Excel validation failed: %s", exc)
        await message.reply_text(
            f"❌ Excel validation failed:\n{exc}\n\n"
            "The existing dataset has not been changed. "
            "Correct the file and send it again."
        )

    except Exception:
        logger.exception("Admin Excel upload failed")
        await message.reply_text(
            "❌ Upload failed because of an internal error. "
            "The server log contains more details. "
            "Please try again."
        )



async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    try:
        registered_user = await asyncio.to_thread(get_registered_user, user.id)
    except Exception:
        logger.exception("Database error while checking registration")
        await message.reply_text("The service is temporarily unavailable. Please try again later.")
        return
    if registered_user and registered_user.get("phone"):
        await send_registered_menu(message)
    else:
        await message.reply_text(
            "Welcome to the bot! To unlock the Mini App and student services, "
            "please share your own phone number using the button below.",
            reply_markup=registration_keyboard(),
        )


async def receive_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if not user or not message or not message.contact:
        return

    # Telegram's request_contact button should send the sender's own contact.
    if message.contact.user_id != user.id:
        await message.reply_text(
            "Please use the button to share your own phone number, not someone else's.",
            reply_markup=registration_keyboard(),
        )
        return

    phone = message.contact.phone_number.strip()
    if not phone:
        await message.reply_text("Telegram did not provide a phone number. Please try again.", reply_markup=registration_keyboard())
        return

    try:
        await asyncio.to_thread(register_user, user.id, phone)
    except Exception:
        logger.exception("Could not register user")
        await message.reply_text("Registration failed temporarily. Please try again later.", reply_markup=registration_keyboard())
        return

    await send_registered_menu(message)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if not user or not message or not message.text:
        return
    try:
        registered_user = await asyncio.to_thread(get_registered_user, user.id)
    except Exception:
        logger.exception("Database error while checking user")
        await message.reply_text("The service is temporarily unavailable. Please try again later.")
        return
    if not registered_user or not registered_user.get("phone"):
        await message.reply_text(
            "Please register first by sharing your own phone number.",
            reply_markup=registration_keyboard(),
        )
        return

    text = message.text.strip()
    if text == "🔐 HEMIS Login":
        context.user_data["awaiting_login_id"] = True
        await message.reply_text("Enter your student ID (for example, AB1234567):", reply_markup=ReplyKeyboardRemove())
    elif context.user_data.get("awaiting_login_id"):
        context.user_data.pop("awaiting_login_id", None)
        student_id = text.upper()
        if not STUDENT_ID_RE.fullmatch(student_id):
            await message.reply_text("Student ID must be two English letters followed by seven digits.", reply_markup=main_menu_keyboard())
        else:
            dataset = load_json(GENERATED_DIR / "login.json")
            record = dataset.get(student_id) if dataset else None
            if record:
                await message.reply_text(f"Login: {record['login']}\nPassword: {record['password']}", reply_markup=main_menu_keyboard())
            else:
                await message.reply_text("No HEMIS record was found for that student ID.", reply_markup=main_menu_keyboard())
    elif text == "💳 Contract":
        await message.reply_text("Choose a contract service:", reply_markup=contract_menu_keyboard())
    elif text == "🔎 Find":
        context.user_data["awaiting_contract_id"] = True
        await message.reply_text("Enter your student ID:", reply_markup=ReplyKeyboardRemove())
    elif context.user_data.get("awaiting_contract_id"):
        context.user_data.pop("awaiting_contract_id", None)
        student_id = text.upper()
        if not STUDENT_ID_RE.fullmatch(student_id):
            await message.reply_text("Student ID must be two English letters followed by seven digits.", reply_markup=main_menu_keyboard())
        else:
            dataset = load_json(GENERATED_DIR / "contract.json")
            record = dataset.get(student_id) if dataset else None
            if record:
                await message.reply_text(
                    f"Total: {record['total_amount']}\nPaid: {record['paid_amount']}\n"
                    f"Remaining: {record['remaining_amount']}\nPaid: {record['payment_percentage']}%",
                    reply_markup=main_menu_keyboard(),
                )
            else:
                await message.reply_text("No contract record was found for that student ID.", reply_markup=main_menu_keyboard())
    elif text == "📝 Discrepancy Report":
        await message.reply_text("Please open the Mini App to continue with student services.", reply_markup=mini_app_keyboard())
    elif text == "⬅️ Main Menu":
        await send_registered_menu(message)
    elif text == "ℹ️ Help":
        await message.reply_text("Use HEMIS Login to look up credentials, Contract to check payment details, or open the Mini App for the full interface.", reply_markup=main_menu_keyboard())
    else:
        await message.reply_text("Please choose an option from the menu.", reply_markup=main_menu_keyboard())

# ---------------------------------------------------------------------------
# Telegram Mini App initData authentication
# ---------------------------------------------------------------------------


def validate_telegram_init_data(init_data: str) -> dict:
    if not BOT_TOKEN:
        raise HTTPException(status_code=503, detail="Telegram authentication is not configured.")
    if not init_data or len(init_data) > 8192:
        raise HTTPException(status_code=401, detail="Missing or invalid Telegram authentication data.")

    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        data = dict(pairs)
    except ValueError:
        raise HTTPException(status_code=401, detail="Malformed Telegram authentication data.")

    received_hash = data.pop("hash", None)
    if not received_hash or "auth_date" not in data or "user" not in data:
        raise HTTPException(status_code=401, detail="Incomplete Telegram authentication data.")

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(status_code=401, detail="Telegram authentication could not be verified.")

    try:
        auth_date = int(data["auth_date"])
        telegram_user = json.loads(data["user"])
        telegram_user_id = int(telegram_user["id"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="Invalid Telegram user data.")

    now = int(time.time())
    if auth_date > now + 60 or now - auth_date > TELEGRAM_INIT_DATA_MAX_AGE_SECONDS:
        raise HTTPException(status_code=401, detail="Telegram session expired. Close and reopen the Mini App from the bot.")
    return {"tgid": telegram_user_id, "user": telegram_user, "auth_date": auth_date}


def require_registered_telegram_user(
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    auth = validate_telegram_init_data(x_telegram_init_data or "")
    try:
        user = get_registered_user(auth["tgid"])
    except Exception:
        logger.exception("Database error during Mini App authentication")
        raise HTTPException(status_code=503, detail="Registration service is temporarily unavailable.")
    if not user or not user.get("phone"):
        raise HTTPException(status_code=403, detail="Phone verification is required. Return to the bot and share your own phone number.")
    return {"tgid": auth["tgid"], "phone": user["phone"], "telegram_user": auth["user"]}

# ---------------------------------------------------------------------------
# FastAPI application: web server and bot share this single entry point.
# ---------------------------------------------------------------------------

telegram_app = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app
    await asyncio.to_thread(initialize_database)
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Set it in .env before starting the bot.")


    telegram_app = Application.builder().token(BOT_TOKEN).build()

    # User registration
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(MessageHandler(filters.CONTACT, receive_contact))

    # Admin Excel uploads
    telegram_app.add_handler(
        CommandHandler("upload_hemis", upload_hemis_command)
    )
    telegram_app.add_handler(
        CommandHandler("upload_contracts", upload_contracts_command)
    )
    telegram_app.add_handler(
        MessageHandler(filters.Document.ALL, receive_admin_excel)
    )

    # Existing text-message handling
    telegram_app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    await telegram_app.initialize()
    await telegram_app.start()
    if telegram_app.updater is None:
        raise RuntimeError("Telegram polling is unavailable.")
    await telegram_app.updater.start_polling()
    logger.info("Telegram bot polling started")
    logger.info("Bot web server started")
    try:
        yield
    finally:
        if telegram_app.updater and telegram_app.updater.running:
            await telegram_app.updater.stop()
        if telegram_app.running:
            await telegram_app.stop()
        await telegram_app.shutdown()
        logger.info("Bot stopped")


app = FastAPI(
    title="Regofis Bot",
    description="Telegram bot and authenticated student Mini App",
    version="1.0.0",
    lifespan=lifespan,
)

@app.get("/", include_in_schema=False)
async def homepage():
    if not INDEX_FILE.is_file():
        raise HTTPException(status_code=500, detail="frontend/index.html was not found.")
    return FileResponse(INDEX_FILE)


@app.get("/api/health")
async def health_check():
    try:
        await asyncio.to_thread(initialize_database)
        return {
            "status": "ok",
            "database": "connected",
            "datasets": {
                "hemis_credentials": (GENERATED_DIR / "login.json").exists(),
                "contracts": (GENERATED_DIR / "contract.json").exists(),
            },
        }
    except Exception:
        logger.exception("Health check failed")
        raise HTTPException(status_code=503, detail="Database is unavailable.")


@app.get("/api/auth/me")
async def auth_me(current_user: dict = Depends(require_registered_telegram_user)):
    return {
        "ok": True,
        "telegram_user_id": current_user["tgid"],
        "phone_verified": True,
    }


def require_admin(x_admin_token: str | None) -> None:
    configured = settings.admin_upload_token
    if not configured or configured == "CHANGE_ME_BEFORE_RUNNING":
        raise HTTPException(status_code=503, detail="Set ADMIN_UPLOAD_TOKEN in .env before using admin upload endpoints.")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, configured):
        raise HTTPException(status_code=403, detail="Not authorized to upload datasets.")


async def read_upload(file: UploadFile) -> bytes:
    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Please upload an .xlsx file.")
    max_bytes = settings.max_upload_mb * 1024 * 1024
    content = await file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail=f"File exceeds {settings.max_upload_mb} MB limit.")
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    return content


@app.post("/api/admin/upload/hemis")
async def upload_hemis(file: UploadFile = File(...), x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")):
    require_admin(x_admin_token)
    content = await read_upload(file)
    try:
        dataset = parse_credentials(content)
    except WorkbookValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    atomic_write_json(GENERATED_DIR / "login.json", dataset)
    return {"ok": True, "message": "HEMIS credentials dataset updated.", "records": len(dataset)}


@app.post("/api/admin/upload/contracts")
async def upload_contracts(file: UploadFile = File(...), x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")):
    require_admin(x_admin_token)
    content = await read_upload(file)
    try:
        dataset = parse_contracts(content)
    except WorkbookValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    atomic_write_json(GENERATED_DIR / "contract.json", dataset)
    return {"ok": True, "message": "Contract dataset updated.", "records": len(dataset)}


@app.get("/api/hemis/credentials/{student_id}")
def get_credentials(student_id: str, _current_user: dict = Depends(require_registered_telegram_user)):
    student_id = student_id.upper()
    if not STUDENT_ID_RE.fullmatch(student_id):
        raise HTTPException(status_code=422, detail="Student ID must contain two English letters followed by seven digits.")
    dataset = load_json(GENERATED_DIR / "login.json")
    if dataset is None:
        raise HTTPException(status_code=503, detail="HEMIS credentials have not been uploaded yet.")
    record = dataset.get(student_id)
    if record is None:
        raise HTTPException(status_code=404, detail="No HEMIS credentials were found for this student ID.")
    return {"student_id": student_id, **record}

@app.post("/api/hemis/phone-change")
async def request_phone_change(
    request: PhoneChangeRequest,
    current_user: dict = Depends(require_registered_telegram_user),
):
    # 1. Check the configured Telegram destination
    if GROUP_TGID == 0:
        raise HTTPException(
            status_code=500,
            detail="Phone-change group is not configured.",
        )

    # 2. Validate the identification number
    student_id = request.id_number.strip().upper()

    if not STUDENT_ID_RE.fullmatch(student_id):
        raise HTTPException(
            status_code=400,
            detail="Invalid identification number format.",
        )

    # 3. Normalize and validate the new phone number
    phone_digits = re.sub(r"\D", "", request.new_phone)

    if phone_digits.startswith("998") and len(phone_digits) == 12:
        new_phone = phone_digits[3:]
    elif len(phone_digits) == 9:
        new_phone = phone_digits
    else:
        raise HTTPException(
            status_code=400,
            detail="Invalid phone number. Enter 9 digits.",
        )

    # 4. Get information about the registered Telegram user
    telegram_id = current_user["tgid"]
    current_phone = current_user.get("phone") or "Not provided"

    telegram_user = current_user.get("telegram_user") or {}

    first_name = telegram_user.get("first_name", "")
    last_name = telegram_user.get("last_name", "")
    full_name = f"{first_name} {last_name}".strip()

    if not full_name:
        full_name = "Not provided"

    # 5. Escape values before inserting them into HTML
    safe_name = html.escape(full_name)
    safe_current_phone = html.escape(str(current_phone))
    safe_telegram_id = html.escape(str(telegram_id))
    safe_student_id = html.escape(student_id)
    safe_new_phone = html.escape(new_phone)

    # 6. Build the Telegram message
    message = (
        "<b>📱 Telefon nomerin ózgertiw ushın arza</b>\n\n"
        f"📞 Házirgi telefon nomeri: <code>{safe_current_phone}</code>\n"
        f"🆔 Telegram ID: <code>{safe_telegram_id}</code>\n"
        f"👤 Tolıq atı: {safe_name}\n"
        f"🪪 ID karta nomeri: <code>{safe_student_id}</code>\n\n"
        f"Arza beriwshi <code>{safe_student_id}</code> telefon nomerin "
        f"<code>{safe_new_phone}</code> nomerine ózgertiwge arza berdi."
    )

    # 7. Send the request to the Telegram group and topic
    try:
        if telegram_app is None:
            raise HTTPException(
                status_code=503,
                detail="Telegram bot is not initialized.",
            )

        send_options = {
            "chat_id": GROUP_TGID,
            "text": message,
            "parse_mode": "HTML",
        }

        if PHONE_CHANGE_TOPIC_ID > 0:
            send_options["message_thread_id"] = PHONE_CHANGE_TOPIC_ID

        await telegram_app.bot.send_message(**send_options)

    except HTTPException:
        raise

    except Exception:
        logger.exception("Failed to send phone-change request to Telegram.")

        raise HTTPException(
            status_code=502,
            detail="Could not send the request to Telegram. Please try again.",
        )
    # 8. Tell the frontend that the request was sent successfully
    return {
        "ok": True,
        "message": "Phone-change request sent successfully.",
    }



@app.get("/api/contracts/{student_id}")
def get_contract(
    student_id: str,
    _current_user: dict = Depends(require_registered_telegram_user),
):
    student_id = student_id.upper()

    if not STUDENT_ID_RE.fullmatch(student_id):
        raise HTTPException(
            status_code=422,
            detail="Student ID must contain two English letters followed by seven digits.",
        )

    dataset = load_json(GENERATED_DIR / "contract.json")

    if dataset is None:
        raise HTTPException(
            status_code=503,
            detail="Contract payment data has not been uploaded yet.",
        )

    students = dataset.get("students", {})
    record = students.get(student_id)

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="No contract payment record was found for this student ID.",
        )

    return {
        "student_id": student_id,
        "update_date": dataset.get("update_date"),
        **record,
    }



if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
