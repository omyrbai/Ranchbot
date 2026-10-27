import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from dotenv import load_dotenv

import pymysql
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from telegram import (
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
INDEX_FILE = WEB_DIR / "index.html"
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_TG_ID = int(os.getenv("ADMIN_TG_ID", "0"))

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_NAME = os.getenv("DB_NAME", "ranchbot_db")
DB_USER = os.getenv("DB_USER", "ranchbot_user")
DB_PASSWORD = os.getenv("DB_PASSWORD")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --------------------------------------------------
# DATABASE
# --------------------------------------------------


def get_db_connection():
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
                )
                ENGINE=InnoDB
                DEFAULT CHARSET=utf8mb4
                COLLATE=utf8mb4_unicode_ci
                """
            )
    finally:
        connection.close()


def get_registered_user(tgid: int):
    connection = get_db_connection()

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT user_id, tgid, phone FROM users WHERE tgid = %s",
                (tgid,),
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
                INSERT INTO users (tgid, phone)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE phone = VALUES(phone)
                """,
                (tgid, phone),
            )
    finally:
        connection.close()


# --------------------------------------------------
# TELEGRAM KEYBOARDS
# --------------------------------------------------


def registration_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Share phone number", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def main_menu_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["🔐 HEMIS Login"],
            ["💳 Contract"],
            ["ℹ️ Help"],
        ],
        resize_keyboard=True,
    )


def contract_menu_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["🔎 Find"],
            ["📝 Discrepancy Report"],
            ["⬅️ Main Menu"],
        ],
        resize_keyboard=True,
    )


# --------------------------------------------------
# TELEGRAM HANDLERS
# --------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Require registration before allowing bot services."""

    user = update.effective_user
    if not user or not update.message:
        return

    try:
        registered_user = await asyncio.to_thread(
            get_registered_user, user.id
        )
    except Exception:
        logger.exception("Database error while checking registration")
        await update.message.reply_text(
            "The service is temporarily unavailable. Please try again later."
        )
        return

    if registered_user:
        await update.message.reply_text(
            "Welcome! Choose a service:",
            reply_markup=main_menu_keyboard(),
        )
    else:
        await update.message.reply_text(
            "To use the bot, please register by sharing your phone number "
            "using the button below.",
            reply_markup=registration_keyboard(),
        )


async def receive_contact(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Accept only the contact belonging to the current Telegram user."""

    user = update.effective_user
    message = update.message

    if not user or not message or not message.contact:
        return

    if message.contact.user_id != user.id:
        await message.reply_text(
            "Please share your own phone number using the button below.",
            reply_markup=registration_keyboard(),
        )
        return

    phone = message.contact.phone_number.strip()

    try:
        await asyncio.to_thread(register_user, user.id, phone)
    except Exception:
        logger.exception("Could not register user")
        await message.reply_text(
            "Registration failed temporarily. Please try again later.",
            reply_markup=registration_keyboard(),
        )
        return

    await message.reply_text(
        "Registration successful! Choose a service:",
        reply_markup=main_menu_keyboard(),
    )


async def handle_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Handle menu buttons, blocking all services for unregistered users."""

    user = update.effective_user
    message = update.message

    if not user or not message or not message.text:
        return

    try:
        registered_user = await asyncio.to_thread(
            get_registered_user, user.id
        )
    except Exception:
        logger.exception("Database error while checking user")
        await message.reply_text(
            "The service is temporarily unavailable. Please try again later."
        )
        return

    if not registered_user:
        await message.reply_text(
            "Please register first by sharing your own phone number.",
            reply_markup=registration_keyboard(),
        )
        return

    text = message.text.strip()

    if text == "🔐 HEMIS Login":
        context.user_data["awaiting_login_id"] = True
        await message.reply_text(
            "Enter your student ID (for example, AB1234567):",
            reply_markup=ReplyKeyboardRemove(),
        )

    elif context.user_data.get("awaiting_login_id"):
        context.user_data.pop("awaiting_login_id", None)

        # HEMIS dataset lookup will be added next.
        await message.reply_text(
            "HEMIS lookup is not connected yet. We will add the dataset "
            "lookup next.",
            reply_markup=main_menu_keyboard(),
        )

    elif text == "💳 Contract":
        await message.reply_text(
            "Choose a contract service:",
            reply_markup=contract_menu_keyboard(),
        )

    elif text == "🔎 Find":
        context.user_data["awaiting_contract_id"] = True
        await message.reply_text(
            "Enter your student ID:",
            reply_markup=ReplyKeyboardRemove(),
        )

    elif context.user_data.get("awaiting_contract_id"):
        context.user_data.pop("awaiting_contract_id", None)

        # Contract dataset lookup will be added next.
        await message.reply_text(
            "Contract lookup is not connected yet. We will add the dataset "
            "lookup next.",
            reply_markup=main_menu_keyboard(),
        )

    elif text == "📝 Discrepancy Report":
        await message.reply_text(
            "Discrepancy reporting will be added next.",
            reply_markup=main_menu_keyboard(),
        )

    elif text == "⬅️ Main Menu":
        await message.reply_text(
            "Main menu:",
            reply_markup=main_menu_keyboard(),
        )

    elif text == "ℹ️ Help":
        await message.reply_text(
            "Use HEMIS Login to look up login information, Contract to "
            "check contract details, or Discrepancy Report to report "
            "an issue.",
            reply_markup=main_menu_keyboard(),
        )

    else:
        await message.reply_text(
            "Please choose an option from the menu.",
            reply_markup=main_menu_keyboard(),
        )


# --------------------------------------------------
# FASTAPI WEB BACKEND
# --------------------------------------------------

telegram_app = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app

    # Ensure the required database table exists.
    await asyncio.to_thread(initialize_database)

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing. Set it as an environment variable."
        )

    telegram_app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(
        MessageHandler(filters.CONTACT, receive_contact)
    )
    telegram_app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    await telegram_app.initialize()
    await telegram_app.start()

    if telegram_app.updater is None:
        raise RuntimeError("Telegram polling is unavailable.")

    await telegram_app.updater.start_polling()

    logger.info("Telegram bot started")
    logger.info("Web backend started")

    try:
        yield
    finally:
        if telegram_app.updater and telegram_app.updater.running:
            await telegram_app.updater.stop()

        if telegram_app.running:
            await telegram_app.stop()

        await telegram_app.shutdown()
        logger.info("Application stopped")


app = FastAPI(
    title="Ranchbot",
    description="University student services",
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
async def homepage():
    if not INDEX_FILE.is_file():
        return {
            "error": "web/index.html was not found."
        }

    return FileResponse(INDEX_FILE)


@app.get("/api/health")
async def health_check():
    """Basic health endpoint for testing the backend."""

    try:
        await asyncio.to_thread(
            lambda: (
                get_db_connection().close()
            )
        )
        return {"status": "ok", "database": "connected"}
    except Exception:
        logger.exception("Health check database error")
        return {
            "status": "error",
            "database": "unavailable",
        }


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
    )