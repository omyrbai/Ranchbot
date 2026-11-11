from __future__ import annotations

import io
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import unicodedata
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl
import html
import re

import urllib.parse
import urllib.request
import urllib.error
from threading import Lock

from pydantic import BaseModel, Field
import pymysql
import uvicorn
from dotenv import load_dotenv
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    UploadFile,
)
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
    CallbackQueryHandler,
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
RECEIPT_TOPIC_ID = int(os.getenv("RECEIPT_TOPIC_ID", "0"))
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
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
NOMINATIM_USER_AGENT = "Ranchbot/1.0 (contact: aydaxo@bk.ru)"

_nominatim_lock = Lock()
_last_nominatim_request = 0.0

address_lookup_queue: asyncio.Queue | None = None
address_lookup_worker_task: asyncio.Task | None = None


async def address_lookup_worker():
    while True:
        latitude, longitude, future = await address_lookup_queue.get()

        try:
            if future.cancelled():
                continue
            result = await asyncio.to_thread(
                get_full_address,
                latitude,
                longitude,
            )

            if not future.done():
                future.set_result(result)

        except Exception as exc:
            if not future.done():
                future.set_exception(exc)

        finally:
            address_lookup_queue.task_done()


def get_full_address(latitude: float, longitude: float) -> dict:
    global _last_nominatim_request

    params = urllib.parse.urlencode({
        "lat": latitude,
        "lon": longitude,
        "format": "jsonv2",
        "addressdetails": 1,
        "accept-language": "kaa",
    })

    request = urllib.request.Request(
        f"{NOMINATIM_URL}?{params}",
        headers={
            "User-Agent": NOMINATIM_USER_AGENT,
            "Accept-Language": "kaa",
        },
    )

    with _nominatim_lock:
        elapsed = time.monotonic() - _last_nominatim_request

        if elapsed < 1.1:
            time.sleep(1.1 - elapsed)

        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))

        except urllib.error.URLError:
            logger.exception("Nominatim request failed")
            raise HTTPException(
                status_code=503,
                detail="Address lookup is temporarily unavailable."
            )

        finally:
            _last_nominatim_request = time.monotonic()

    address = data.get("address", {})

    city = (
        address.get("city")
        or address.get("town")
        or address.get("village")
        or address.get("municipality")
    )

    neighborhood = (
        address.get("neighbourhood")
        or address.get("suburb")
        or address.get("quarter")
    )

    street = (
            address.get("road")
            or address.get("pedestrian")
            or address.get("residential")
            or "Ati belgilenbegen koshe"
    )

    house_number = address.get("house_number") or "nomersiz jay"

    full_address = ", ".join(
        part for part in [city, neighborhood, street, house_number]
        if part
    )

    if not full_address:
        raise HTTPException(
            status_code=422,
            detail="No readable address found for this location."
        )

    return {
        "city": city,
        "neighborhood": neighborhood,
        "street": street,
        "house_number": house_number,
        "full_address": full_address,
    }# ---------------------------------------------------------------------------
# Database: reuse ranchbot_db.users; do not create a second users table.
# ---------------------------------------------------------------------------

class PhoneChangeRequest(BaseModel):
    id_number: str = Field(min_length=9, max_length=9)
    new_phone: str = Field(min_length=12, max_length=13)


class AddressLocationRequest(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    full_address: str | None = Field(default=None, max_length=500)


class SaveAddressRequest(BaseModel):
    address_type: Literal["OWN", "RENT", "NOT_RENT"]
    current_address: AddressLocationRequest
    permanent_address: AddressLocationRequest | None = None
    landlord_fullname: str | None = Field(default=None, max_length=150)
    landlord_phone: str | None = Field(default=None, max_length=13)

def normalize_landlord_fullname(value: str) -> str:
    name = value.strip()

    allowed_punctuation = {" ", "-", "'", "’", "‘", "ʻ", "ʼ"}

    if not name:
        raise HTTPException(
            status_code=422,
            detail="Please enter the landlord's full name."
        )

    for character in name:
        is_latin_letter = (
            unicodedata.category(character).startswith("L")
            and "LATIN" in unicodedata.name(character, "")
        )

        if not is_latin_letter and character not in allowed_punctuation:
            raise HTTPException(
                status_code=422,
                detail="Landlord's name must use Latin letters only. Cyrillic letters and numbers are not allowed."
            )

    return name.upper()

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
    phone = phone.strip()

    # Normalize Uzbek phone numbers to +998XXXXXXXXX.
    digits = re.sub(r"\D", "", phone)

    if digits.startswith("998") and len(digits) == 12:
        phone = f"+{digits}"

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
        [[KeyboardButton("📱 Nómerimdi jiberiw", request_contact=True)]],
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
        "✅ Siziń akkauntıńız tastıyıqlanǵan. Mini appqa ótip studentlerge kórsetiletuǵın xızmetler menen tanıssańız boladı:",
        reply_markup=ReplyKeyboardRemove(),
    )
    await message.reply_text("🚀 Tómendegi túymeni basıp Mini apptı ashıń:", reply_markup=mini_app_keyboard())

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


async def get_addresses_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if not message:
        return

    if not is_admin(update):
        await message.reply_text(
            "⛔ You are not authorized to access address records."
        )
        return

    await message.reply_text("⏳ Preparing the addresses Excel file...")

    try:
        def generate_addresses_excel():
            from openpyxl import Workbook

            # Load HEMIS student information.
            dataset = load_json(GENERATED_DIR / "login.json") or {}

            students_by_hemis_id = {}
            for student in dataset.values():
                if not isinstance(student, dict):
                    continue

                hemis_id = str(student.get("HemisID", "")).strip().upper()
                if hemis_id:
                    students_by_hemis_id[hemis_id] = student

            # Normalize Uzbek phone numbers for comparison.
            def normalize_phone(value):
                digits = re.sub(r"\D", "", str(value or ""))
                return digits[-9:] if len(digits) >= 9 else digits

            connection = get_db_connection()

            try:
                with connection.cursor() as cursor:
                    # Get registered phone numbers from the existing users table.
                    cursor.execute(
                        """
                        SELECT phone
                        FROM users
                        WHERE phone IS NOT NULL AND phone <> ''
                        """
                    )
                    registered_phones = {
                        normalize_phone(row["phone"])
                        for row in cursor.fetchall()
                        if normalize_phone(row["phone"])
                    }

                    # Get only active address records.
                    cursor.execute(
                        """
                        SELECT *
                        FROM addresses
                        WHERE is_current = 1
                        ORDER BY hemis_id, address_role, address_id
                        """
                    )
                    address_rows = cursor.fetchall()
                    address_headers = [
                        column[0] for column in cursor.description
                    ]

                # Group current and permanent addresses by student.
                students = {}

                for row in address_rows:
                    hemis_id = str(row.get("hemis_id") or "").strip().upper()

                    if not hemis_id:
                        continue

                    if hemis_id not in students:
                        students[hemis_id] = {
                            "CURRENT": {},
                            "PERMANENT": {},
                        }

                    role = str(row.get("address_role") or "").upper()

                    if role in ("CURRENT", "PERMANENT"):
                        students[hemis_id][role] = row
                # Calculate export statistics.
                student_count = len(students_by_hemis_id)

                confirmed_count = sum(
                    1
                    for student in students_by_hemis_id.values()
                    if normalize_phone(student.get("Phone", ""))
                    and normalize_phone(student.get("Phone", "")) in registered_phones
                )

                address_entered_count = sum(
                    1
                    for hemis_id in students_by_hemis_id
                    if hemis_id in students
                )

                workbook = Workbook()
                worksheet = workbook.active
                worksheet.title = "Addresses"

                # Build the Excel column order.
                export_headers = [
                    "status",
                    "student_group",
                    "tutor_name",
                    "hemis_id",
                    "student_fullname",
                    "student_phone",
                ]

                address_fields = [
                    header for header in address_headers
                    if header not in {
                        "address_id",
                        "hemis_id",
                        "address_role",
                        "is_current",
                    }
                ]

                for role in ("CURRENT", "PERMANENT"):
                    prefix = role.lower()

                    for field in address_fields:
                        export_headers.append(f"{prefix}_{field}")

                        if field == "longitude":
                            export_headers.append(
                                f"{prefix}_google_maps_link"
                            )

                worksheet.append(export_headers)

                # One Excel row per student.
                # One Excel row for EVERY student in the HEMIS dataset.
                for hemis_id, student in students_by_hemis_id.items():

                    # Get addresses if available; otherwise use empty records.
                    addresses = students.get(
                        hemis_id,
                        {
                            "CURRENT": {},
                            "PERMANENT": {},
                        }
                    )
                    student_phone = student.get("Phone", "")

                    status = (
                        "confirmed"
                        if normalize_phone(student_phone) in registered_phones
                        and normalize_phone(student_phone)
                        else "not_confirmed"
                    )

                    values = {
                        "status": status,
                        "student_group": student.get("Group", ""),
                        "tutor_name": student.get("Tutor", ""),
                        "hemis_id": hemis_id,
                        "student_fullname": student.get("Fullname", ""),
                        "student_phone": student_phone,
                    }

                    for role in ("CURRENT", "PERMANENT"):
                        prefix = role.lower()
                        address = addresses[role]

                        for field in address_fields:
                            values[f"{prefix}_{field}"] = (
                                address.get(field, "") if address else ""
                            )

                            if field == "longitude":
                                latitude = (
                                    address.get("latitude") if address else None
                                )
                                longitude = (
                                    address.get("longitude") if address else None
                                )

                                maps_link = ""
                                if latitude is not None and longitude is not None:
                                    maps_link = (
                                        f"https://www.google.com/maps"
                                        f"?q={latitude},{longitude}"
                                    )

                                values[f"{prefix}_google_maps_link"] = maps_link

                    worksheet.append([
                        values.get(header, "")
                        for header in export_headers
                    ])

                    # Make both map links clickable.
                    current_row = worksheet.max_row

                    for role in ("current", "permanent"):
                        link_header = f"{role}_google_maps_link"
                        if link_header not in export_headers:
                            continue

                        column_number = export_headers.index(link_header) + 1
                        cell = worksheet.cell(
                            row=current_row,
                            column=column_number,
                        )

                        if cell.value:
                            cell.hyperlink = cell.value
                            cell.style = "Hyperlink"

                worksheet.freeze_panes = "A2"
                worksheet.auto_filter.ref = worksheet.dimensions

                for column in worksheet.columns:
                    max_length = max(
                        (
                            len(str(cell.value))
                            if cell.value is not None else 0
                        )
                        for cell in column
                    )

                    worksheet.column_dimensions[
                        column[0].column_letter
                    ].width = min(max_length + 2, 50)

                output = io.BytesIO()
                workbook.save(output)
                output.seek(0)

                return (
                    output,
                    student_count,
                    confirmed_count,
                    address_entered_count,
                )

            finally:
                connection.close()

        (
            excel_file,
            student_count,
            confirmed_count,
            address_entered_count,
        ) = await asyncio.to_thread(generate_addresses_excel)

        await message.reply_document(
            document=excel_file,
            filename="addresses.xlsx",
            caption=(
                "📍 Addresses"
            ),
        )
        await message.reply_text(
            f"Student sani: {student_count}\n"
            f"Tastiyiqlanganlar sani: {confirmed_count}\n"
            f"Manzilin kiritkenler sani: {address_entered_count}"
        )
        def generate_statistics_chart():
            import matplotlib
            matplotlib.use("Agg")

            import matplotlib.pyplot as plt
            from matplotlib.patches import Patch

            # Students who have not confirmed their accounts.
            unconfirmed_count = max(
                student_count - confirmed_count, 0
            )

            fig, ax = plt.subplots(
                figsize=(9, 6),
                facecolor="white"
            )

            if student_count > 0:
                # Two complementary portions of the total.
                ax.pie(
                    [confirmed_count, unconfirmed_count],
                    colors=["#269B62", "#D9DEE7"],
                    startangle=90,
                    counterclock=False,
                    autopct=lambda pct: (
                        f"{pct:.1f}%" if pct > 0 else ""
                    ),
                    pctdistance=0.78,
                    wedgeprops={
                        "width": 0.38,
                        "edgecolor": "white",
                        "linewidth": 2,
                    },
                    textprops={
                        "color": "#263238",
                        "fontsize": 11,
                        "weight": "bold",
                    },
                )
            else:
                ax.pie(
                    [1],
                    colors=["#D9DEE7"],
                    startangle=90,
                    wedgeprops={"width": 0.38},
                )

            # Total number in the center.
            ax.text(
                0, 0.10,
                f"{student_count:,}",
                ha="center",
                va="center",
                fontsize=25,
                fontweight="bold",
                color="#172B4D",
            )

            ax.text(
                0, -0.18,
                "Sani",
                ha="center",
                va="center",
                fontsize=11,
                color="#667085",
            )

            ax.set_title(
                "Student Statistics",
                fontsize=17,
                fontweight="bold",
                color="#172B4D",
                pad=18,
            )

            # Legend: exactly the three requested statistics.
            legend_items = [
                Patch(
                    facecolor="#269B62",
                    label=f"Sani: {student_count:,}",
                ),
                Patch(
                    facecolor="#269B62",
                    label=f"Tastiyiqlandi: {confirmed_count:,}",
                ),
                Patch(
                    facecolor="#D9DEE7",
                    label=f"Manzil: {address_entered_count:,}",
                ),
            ]

            ax.legend(
                handles=legend_items,
                loc="center left",
                bbox_to_anchor=(1.02, 0.5),
                frameon=False,
                fontsize=11,
                labelspacing=1.5,
            )

            ax.set_aspect("equal")
            fig.tight_layout()

            chart_output = io.BytesIO()

            fig.savefig(
                chart_output,
                format="png",
                dpi=180,
                bbox_inches="tight",
                facecolor="white",
            )

            plt.close(fig)
            chart_output.seek(0)

            return chart_output
        chart_file = await asyncio.to_thread(
            generate_statistics_chart
        )

        await message.reply_photo(
            photo=chart_file,
            caption="📊 Student statistics",
        )

    except Exception:
        logger.exception("Could not export addresses to Excel")
        await message.reply_text(
            "❌ Failed to generate the addresses Excel file. "
            "Please check the server logs."
        )


async def get_address_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if not message:
        return

    if not is_admin(update):
        await message.reply_text(
            "⛔ You are not authorized to access address records."
        )
        return

    await message.reply_text("⏳ Preparing the address records Excel file...")

    try:
        def generate_address_excel():
            from openpyxl import Workbook

            # Load HEMIS student information.
            dataset = load_json(GENERATED_DIR / "login.json") or {}

            students_by_hemis_id = {}

            for student in dataset.values():
                if not isinstance(student, dict):
                    continue

                hemis_id = str(
                    student.get("HemisID", "")
                ).strip().upper()

                if hemis_id:
                    students_by_hemis_id[hemis_id] = student

            connection = get_db_connection()

            try:
                with connection.cursor() as cursor:
                    # Select every active address record.
                    cursor.execute(
                        """
                        SELECT *
                        FROM addresses
                        WHERE is_current = 1
                        ORDER BY hemis_id, address_role, address_id
                        """
                    )

                    address_rows = cursor.fetchall()

                    address_headers = [
                        column[0]
                        for column in cursor.description
                    ]

                workbook = Workbook()
                worksheet = workbook.active
                worksheet.title = "Address Records"

                # Student information comes first.
                export_headers = [
                    "student_group",
                    "student_fullname",
                    "hemis_id",
                ]

                # Include all address columns except address_id
                # and the duplicate hemis_id.
                address_fields = [
                    header
                    for header in address_headers
                    if header not in {"address_id", "hemis_id"}
                ]

                export_headers.extend(address_fields)

                # Add Google Maps link after longitude.
                if "longitude" in address_fields:
                    longitude_index = (
                        export_headers.index("longitude") + 1
                    )
                    export_headers.insert(
                        longitude_index,
                        "google_maps_link",
                    )

                worksheet.append(export_headers)

                # LEFT JOIN equivalent:
                # Keep every address record, even if no HEMIS
                # student record matches its hemis_id.
                for address in address_rows:
                    hemis_id = str(
                        address.get("hemis_id") or ""
                    ).strip().upper()

                    student = students_by_hemis_id.get(
                        hemis_id, {}
                    )

                    values = {
                        "student_group": student.get("Group", ""),
                        "student_fullname": student.get("Fullname", ""),
                        "hemis_id": hemis_id,
                    }

                    # Copy address information.
                    for field in address_fields:
                        values[field] = address.get(field, "")

                    # Build Google Maps link.
                    latitude = address.get("latitude")
                    longitude = address.get("longitude")

                    maps_link = ""

                    if latitude is not None and longitude is not None:
                        maps_link = (
                            f"https://www.google.com/maps"
                            f"?q={latitude},{longitude}"
                        )

                    if "google_maps_link" in export_headers:
                        values["google_maps_link"] = maps_link

                    worksheet.append([
                        values.get(header, "")
                        for header in export_headers
                    ])

                    # Make Google Maps link clickable.
                    if "google_maps_link" in export_headers:
                        link_column = (
                            export_headers.index("google_maps_link") + 1
                        )

                        cell = worksheet.cell(
                            row=worksheet.max_row,
                            column=link_column,
                        )

                        if cell.value:
                            cell.hyperlink = cell.value
                            cell.style = "Hyperlink"

                worksheet.freeze_panes = "A2"
                worksheet.auto_filter.ref = worksheet.dimensions

                for column in worksheet.columns:
                    max_length = max(
                        (
                            len(str(cell.value))
                            if cell.value is not None else 0
                        )
                        for cell in column
                    )

                    worksheet.column_dimensions[
                        column[0].column_letter
                    ].width = min(max_length + 2, 50)

                output = io.BytesIO()
                workbook.save(output)
                output.seek(0)

                return output, len(address_rows)

            finally:
                connection.close()

        excel_file, record_count = await asyncio.to_thread(
            generate_address_excel
        )

        await message.reply_document(
            document=excel_file,
            filename="address_records.xlsx",
            caption="📍 Active address records",
        )

        await message.reply_text(
            f"📊 Active address records exported: {record_count}"
        )

    except Exception:
        logger.exception("Could not export address records to Excel")

        await message.reply_text(
            "❌ Failed to generate the address records Excel file. "
            "Please check the server logs."
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
        "The first sheet must contain these columns in order: HemisID, Fullname, PINFL, Idnumber, Phone, Group, Form, Tutor.\n"
        "The first row is treated as a header and ignored."
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

        if upload_type == "hemis":
            destination = GENERATED_DIR / "login.json"
            # Clear the previous HEMIS JSON before rebuilding from this in-memory upload.
            # The Excel workbook is never saved to the server.

            dataset = await asyncio.to_thread(parse_credentials, content)
            dataset_name = "HEMIS student data"

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
            "Botqa xosh keldińiz! Studentlerge kórsetiletuǵın xızmetlerden tolıq paydalanıw ushın iltimas tómendegi túymeni basıp telefonıńızdı jiberiń",
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
            "Iltimas birewdiń telefon nomerin emes ózińizdiń telefon nomerińizdi tómendegi túymeni basıw arqalı jiberiń",
            reply_markup=registration_keyboard(),
        )
        return

    phone = message.contact.phone_number.strip()
    if not phone:
        await message.reply_text("Telegram arqalı telefon nomerin jiberiw múmkin bolmadı. Iltimas qaytadan urınıp kóriń.", reply_markup=registration_keyboard())
        return

    try:
        await asyncio.to_thread(register_user, user.id, phone)
    except Exception:
        logger.exception("Paydalanıwshını tastıyıqlaw múmkin bolmadı.")
        await message.reply_text("Házirshe tastıyıqlawdıń ilajı joq. Iltimas keyinirek qaytadan urınıp kóriń.", reply_markup=registration_keyboard())
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
            "Dáslep ózińizdiń telefon nomerińizdi jiberiw arqalı akkauntıńızdı tastıyıqlań",
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
                await message.reply_text(
                    f"HEMIS ID: {record.get('HemisID', record.get('login', ''))}\n"
                    f"Full name: {record.get('Fullname', '')}\n"
                    f"PINFL: {record.get('PINFL', '')}\n"
                    f"Student ID: {record.get('Idnumber', student_id)}\n"
                    f"Phone: {record.get('Phone', '')}\n"
                    f"Group: {record.get('Group', '')}\n"
                    f"Form: {record.get('Form', '')}\n"
                    f"Tutor: {record.get('Tutor', '')}",
                    reply_markup=main_menu_keyboard(),
                )
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
    global telegram_app, address_lookup_queue, address_lookup_worker_task
    await asyncio.to_thread(initialize_database)
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Set it in .env before starting the bot.")


    telegram_app = Application.builder().token(BOT_TOKEN).build()

    # User registration
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(MessageHandler(filters.CONTACT, receive_contact))
    telegram_app.add_handler(
        CallbackQueryHandler(
            handle_phone_change_callback,
            pattern=r"^phone_change:(changed|cancel)$",
        )
    )
    # Admin Excel uploads
    telegram_app.add_handler(
        CommandHandler("upload_hemis", upload_hemis_command)
    )
    telegram_app.add_handler(
        CommandHandler("upload_contracts", upload_contracts_command)
    )
    telegram_app.add_handler(
        CommandHandler("get_addresses", get_addresses_command)
    )
    telegram_app.add_handler(
        CommandHandler("get_address", get_address_command)
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
    address_lookup_queue = asyncio.Queue(maxsize=100)
    address_lookup_worker_task = asyncio.create_task(
        address_lookup_worker()
    )

    try:
        yield
    finally:
        if telegram_app.updater and telegram_app.updater.running:
            await telegram_app.updater.stop()
        if telegram_app.running:
            await telegram_app.stop()
        await telegram_app.shutdown()
        logger.info("Bot stopped")
        if address_lookup_worker_task:
            address_lookup_worker_task.cancel()
            await asyncio.gather(
                address_lookup_worker_task,
                return_exceptions=True,
            )

        address_lookup_worker_task = None
        address_lookup_queue = None


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


@app.get("/translations.json", include_in_schema=False)
async def translation_file():
    """Serve the Mini App language dictionaries to the frontend."""
    translations_file = FRONTEND_DIR / "translations.json"
    if not translations_file.is_file():
        raise HTTPException(status_code=500, detail="frontend/translations.json was not found.")
    return FileResponse(translations_file, media_type="application/json; charset=utf-8")


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

@app.get("/api/student/me")
def get_my_student_profile(
    current_user: dict = Depends(require_registered_telegram_user)
):
    """Return the HEMIS profile matching the user's registered phone."""

    dataset = load_json(GENERATED_DIR / "login.json")

    if not dataset:
        raise HTTPException(
            status_code=503,
            detail="HEMIS student data has not been uploaded yet."
        )

    def normalize_phone(value):
        # Ignore +, spaces, hyphens, and other non-digit characters.
        digits = re.sub(r"\D", "", str(value or ""))

        # Compare the last 9 digits of Uzbek phone numbers.
        return digits[-9:] if len(digits) >= 9 else digits

    registered_phone = normalize_phone(current_user.get("phone"))

    if not registered_phone:
        raise HTTPException(
            status_code=403,
            detail="Your registered phone number is missing."
        )

    matches = [
        record
        for record in dataset.values()
        if isinstance(record, dict)
        and normalize_phone(
            record.get("Phone", record.get("phone", ""))
        ) == registered_phone
    ]

    if not matches:
        raise HTTPException(
            status_code=404,
            detail="No HEMIS student record matches your registered phone number."
        )

    if len(matches) > 1:
        raise HTTPException(
            status_code=409,
            detail="Multiple student records match your phone number. Please contact the administrator."
        )

    student = matches[0]

    return {
        "fullname": student.get("Fullname", ""),
        "hemis_id": student.get("HemisID", ""),
        "idnumber": student.get("Idnumber", ""),
        "pinfl": student.get("PINFL", ""),
        "phone": student.get("Phone", ""),
        "group": student.get("Group", ""),
        "form": student.get("Form", ""),
        "tutor": student.get("Tutor", ""),
    }


@app.post("/api/address/lookup")
async def lookup_address(
    request: AddressLocationRequest,
    current_user: dict = Depends(require_registered_telegram_user)
):
    if address_lookup_queue is None:
        raise HTTPException(
            status_code=503,
            detail="Address lookup service is unavailable.",
        )

    loop = asyncio.get_running_loop()
    future = loop.create_future()

    try:
        address_lookup_queue.put_nowait((
            request.latitude,
            request.longitude,
            future,
        ))

    except asyncio.QueueFull:
        raise HTTPException(
            status_code=503,
            detail="Address lookup queue is currently full. Please try again shortly.",
        )

    try:
        return await asyncio.wait_for(future, timeout=180)

    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail="Address lookup took too long. Please try again.",
        )

@app.get("/api/address/me")
def get_my_addresses(
    current_user: dict = Depends(require_registered_telegram_user)
):
    # Reuse the existing authenticated student lookup.
    student = get_my_student_profile(current_user)
    hemis_id = student.get("hemis_id")

    if not hemis_id:
        raise HTTPException(
            status_code=404,
            detail="Your HEMIS ID could not be identified."
        )

    connection = get_db_connection()

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    address_id,
                    latitude,
                    longitude,
                    full_address,                    
                    address_type,
                    landlord_fullname,
                    landlord_phone,
                    address_role,
                    is_current,
                    created_at,
                    updated_at
                FROM addresses
                WHERE hemis_id = %s
                  AND is_current = 1
                ORDER BY address_role
                """,
                (hemis_id,)
            )

            addresses = cursor.fetchall()

        return {
            "addresses": addresses
        }

    except Exception:
        logger.exception("Could not retrieve student addresses")
        raise HTTPException(
            status_code=503,
            detail="Address service is temporarily unavailable."
        )

    finally:
        connection.close()


@app.post("/api/address/me")
def save_my_addresses(
    request: SaveAddressRequest,
    current_user: dict = Depends(require_registered_telegram_user)
):
    student = get_my_student_profile(current_user)
    hemis_id = student.get("hemis_id")

    if not hemis_id:
        raise HTTPException(
            status_code=404,
            detail="Your HEMIS ID could not be identified."
        )

    if request.address_type == "OWN" and request.permanent_address is not None:
        raise HTTPException(
            status_code=422,
            detail="A permanent address is not required for OWN."
        )

    if request.address_type in ("RENT", "NOT_RENT") and request.permanent_address is None:
        raise HTTPException(
            status_code=422,
            detail="A permanent address is required."
        )

    landlord_fullname = None
    landlord_phone = None

    if request.address_type == "RENT":
        if not request.landlord_fullname:
            raise HTTPException(
                status_code=422,
                detail="Please enter the landlord's full name."
            )

        landlord_fullname = normalize_landlord_fullname(
            request.landlord_fullname
        )

        landlord_phone = (request.landlord_phone or "").strip()

        if not re.fullmatch(r"\+998\d{9}", landlord_phone):
            raise HTTPException(
                status_code=422,
                detail="Landlord's phone number must use the format +998991231212."
            )
    current_full_address = (
            request.current_address.full_address or ""
    ).strip()

    if not current_full_address:
        raise HTTPException(
            status_code=422,
            detail="Please provide your current address."
        )

    permanent_full_address = None

    if request.permanent_address:
        permanent_full_address = (
                request.permanent_address.full_address or ""
        ).strip()

        if not permanent_full_address:
            raise HTTPException(
                status_code=422,
                detail="Please provide your permanent address."
            )

    connection = get_db_connection()

    try:
        connection.begin()

        with connection.cursor() as cursor:
            # Retire previous active CURRENT address.
            cursor.execute(
                """
                UPDATE addresses
                SET is_current = 0
                WHERE hemis_id = %s
                  AND address_role = 'CURRENT'
                  AND is_current = 1
                """,
                (hemis_id,)
            )

            # Insert new CURRENT address.
            cursor.execute(
                """
                INSERT INTO addresses (
                    hemis_id, latitude, longitude, full_address,
                    address_type, landlord_fullname, landlord_phone,
                    address_role, is_current
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'CURRENT', 1)
                """,
                (
                    hemis_id,
                    request.current_address.latitude,
                    request.current_address.longitude,
                    current_full_address,
                    request.address_type,
                    landlord_fullname,
                    landlord_phone
                )
            )

            # Retire previous active PERMANENT address.
            cursor.execute(
                """
                UPDATE addresses
                SET is_current = 0
                WHERE hemis_id = %s
                  AND address_role = 'PERMANENT'
                  AND is_current = 1
                """,
                (hemis_id,)
            )

            if request.address_type == "OWN":
                permanent_latitude = request.current_address.latitude
                permanent_longitude = request.current_address.longitude
                permanent_address_text = current_full_address
            else:
                permanent_latitude = request.permanent_address.latitude
                permanent_longitude = request.permanent_address.longitude
                permanent_address_text = permanent_full_address

            # Insert new PERMANENT address.
            cursor.execute(
                """
                INSERT INTO addresses (
                    hemis_id, latitude, longitude, full_address,
                    address_type, address_role, is_current
                )
                VALUES (%s, %s, %s, %s, 'OWN', 'PERMANENT', 1)
                """,
                (
                    hemis_id,
                    permanent_latitude,
                    permanent_longitude,
                    permanent_address_text
                )
            )

        connection.commit()

        return {
            "message": "Addresses saved successfully."
        }

    except HTTPException:
        connection.rollback()
        raise

    except Exception:
        connection.rollback()
        logger.exception("Could not save student addresses")
        raise HTTPException(
            status_code=503,
            detail="Could not save addresses. Please try again."
        )

    finally:
        connection.close()


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
    destination = GENERATED_DIR / "login.json"
    # Clear the previous HEMIS JSON before rebuilding from this in-memory upload.
    # The uploaded Excel file is never saved to disk.

    try:
        dataset = parse_credentials(content)
    except WorkbookValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    atomic_write_json(destination, dataset)
    return {"ok": True, "message": "HEMIS student dataset updated.", "records": len(dataset)}


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

@app.post("/api/contracts/discrepancy-report")
async def submit_discrepancy_report(
    student_id: str = Form(...),
    full_name: str = Form(...),
    pinfl : str =  Form(...),
    payment_date: str = Form(...),
    details: str = Form(...),
    receipt: UploadFile = File(...),
    id_front: UploadFile = File(...),
    id_back: UploadFile = File(...),
    current_user: dict = Depends(require_registered_telegram_user),
):
    # 1. Check Telegram configuration
    if GROUP_TGID == 0 or DISCR_TOPIC_ID <= 0:
        raise HTTPException(
            status_code=500,
            detail="The discrepancy-report Telegram topic is not configured.",
        )

    if telegram_app is None:
        raise HTTPException(
            status_code=503,
            detail="Telegram bot is not initialized.",
        )

    # 2. Validate student ID
    student_id = student_id.strip().upper()

    if not STUDENT_ID_RE.fullmatch(student_id):
        raise HTTPException(
            status_code=400,
            detail="Enter a valid student ID: two letters followed by seven digits.",
        )
    full_name = full_name.strip()
    pinfl = pinfl.strip()

    if not full_name:
        raise HTTPException(
            status_code=400,
            detail="Please enter your full name.",
        )

    if len(full_name) > 150:
        raise HTTPException(
            status_code=400,
            detail="Full name must not exceed 150 characters.",
        )

    if not re.fullmatch(r"\d{14}", pinfl):
        raise HTTPException(
            status_code=400,
            detail="PINFL must contain exactly 14 digits.",
        )

    # Validate the payment date and apply the business-day rule.
    try:
        parsed_payment_date = date.fromisoformat(payment_date)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail="Please select a valid payment date.",
        ) from exc

    today = datetime.now(ZoneInfo("Asia/Tashkent")).date()

    if parsed_payment_date > today:
        raise HTTPException(
            status_code=400,
            detail="Payment date cannot be in the future.",
        )
    # If payment was made on Saturday or Sunday, move the
    # effective receipt date forward to the following Monday.
    # If payment was made on Saturday or Sunday, move the
    # effective receipt date forward to the following Monday.
    receipt_date = parsed_payment_date

    while receipt_date.weekday() >= 5:
        receipt_date += timedelta(days=1)

    # Count elapsed business days, excluding Saturdays and Sundays.
    business_days_elapsed = 0
    current_date = receipt_date

    while current_date < today:
        current_date += timedelta(days=1)

        if current_date.weekday() < 5:
            business_days_elapsed += 1

    if business_days_elapsed < 4:
        raise HTTPException(
            status_code=400,
            detail=(
                "Discrepancy reports can only be submitted at least 4 business days after the payment date."
            ),
        )

    # 3. Validate report description
    details = details.strip()

    if not details:
        raise HTTPException(
            status_code=400,
            detail="Please describe the discrepancy.",
        )

    # 4. Require and validate all three files
    required_files = [
        ("Payment receipt", receipt),
        ("ID card — front side", id_front),
        ("ID card — back side", id_back),
    ]

    max_file_size = 20 * 1024 * 1024  # 20 MB per file
    uploaded_files = []

    for label, upload in required_files:
        if not upload.filename:
            raise HTTPException(
                status_code=400,
                detail=f"Please upload the required file: {label}.",
            )

        filename = Path(upload.filename).name
        suffix = Path(filename).suffix.lower()
        content_type = (upload.content_type or "").lower()

        if not (
            suffix == ".pdf"
            or content_type == "application/pdf"
            or content_type.startswith("image/")
        ):
            raise HTTPException(
                status_code=400,
                detail=f"{label} must be an image or PDF.",
            )

        content = await upload.read(max_file_size + 1)

        if not content:
            raise HTTPException(
                status_code=400,
                detail=f"The uploaded file is empty: {label}.",
            )

        if len(content) > max_file_size:
            raise HTTPException(
                status_code=413,
                detail=f"{label} exceeds the 20 MB file-size limit.",
            )

        uploaded_files.append((label, filename, content))

    # 5. Get the registered Telegram user's information
    telegram_id = current_user["tgid"]
    current_phone = current_user.get("phone") or "Not provided"
    telegram_user = current_user.get("telegram_user") or {}

    telegram_name = (
        f"{telegram_user.get('first_name', '')} "
        f"{telegram_user.get('last_name', '')}"
    ).strip() or "Not provided"

    # 6. Escape user-provided text before using Telegram HTML formatting
    safe_name = html.escape(full_name)
    safe_telegram_name = html.escape(telegram_name)
    safe_pinfl = html.escape(pinfl)
    safe_phone = html.escape(str(current_phone))
    safe_telegram_id = html.escape(str(telegram_id))
    safe_student_id = html.escape(student_id)
    safe_details = html.escape(details)

    report_message = (
        "🚨 <b>Pariqti xabarlaw</b>\n\n"
        f"👤 <b>Arza berdi:</b> {safe_name}\n"
        f"📱 <b>Tg akkaunt:</b> {safe_telegram_name}\n"
        f"🆔 <b>ID nomeri:</b> <code>{safe_student_id}</code>\n"
        f"🪪 <b>PINFL:</b> <code>{safe_pinfl}</code>\n"
        f"📅 <b>Tólem sánesi:</b><code>{receipt_date.isoformat()}</code>\n"
        f"📱 <b>Tg nomeri:</b> <code>{safe_phone}</code>\n"
        f"💬 <b>Tg ID:</b> <code>{safe_telegram_id}</code>\n\n"
        f"<b>Sipatlama:</b>\n{safe_details}"
    )

    # 7. Send the report and documents to the configured topic
    send_options = {
        "chat_id": GROUP_TGID,
        "message_thread_id": DISCR_TOPIC_ID,
    }

    try:
        await telegram_app.bot.send_message(
            **send_options,
            text=report_message,
            parse_mode="HTML",
        )

        for label, filename, content in uploaded_files:
            await telegram_app.bot.send_document(
                **send_options,
                document=io.BytesIO(content),
                filename=filename,
                caption=label,
            )

    except Exception as exc:
        logger.exception("Failed to send discrepancy report to Telegram")
        raise HTTPException(
            status_code=502,
            detail=(
                "Telegram could not complete the report delivery. "
                "Please contact support before submitting again."
            ),
        ) from exc

    # 8. Return success to the Mini App
    return {
        "ok": True,
        "message": "Your discrepancy report and all three documents were sent successfully.",
    }


@app.post("/api/contracts/receipt")
async def submit_contract_receipt(
    student_id: str = Form(...),
    full_name: str = Form(...),
    pinfl: str = Form(...),
    payment_date: str = Form(...),
    amount: str = Form(...),
    receipt: UploadFile = File(...),
    current_user: dict = Depends(require_registered_telegram_user),
):
    """Receive a payment receipt and send it to the existing discrepancy Telegram topic."""
    if GROUP_TGID == 0 or RECEIPT_TOPIC_ID <= 0:
        raise HTTPException(
            status_code=500,
            detail="The contract receipt Telegram topic is not configured.",
        )

    if telegram_app is None:
        raise HTTPException(status_code=503, detail="Telegram bot is not initialized.")

    student_id = student_id.strip().upper()
    full_name = full_name.strip()
    pinfl = pinfl.strip()

    if not STUDENT_ID_RE.fullmatch(student_id):
        raise HTTPException(
            status_code=400,
            detail="Enter a valid ID: two letters followed by seven digits.",
        )
    if not full_name:
        raise HTTPException(status_code=400, detail="Please enter your full name.")
    if len(full_name) > 150:
        raise HTTPException(status_code=400, detail="Full name must not exceed 150 characters.")
    if not re.fullmatch(r"\d{14}", pinfl):
        raise HTTPException(status_code=400, detail="PINFL must contain exactly 14 digits.")
    try:
        parsed_payment_date = date.fromisoformat(payment_date)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Please select a valid payment date.") from exc
    normalized_amount = re.sub(r"\s+", "", amount.strip()).replace(",", ".")
    try:
        payment_amount = Decimal(normalized_amount)
    except (InvalidOperation, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Please enter a valid payment amount.") from exc
    if not payment_amount.is_finite() or payment_amount <= 0 or payment_amount.as_tuple().exponent < -2:
        raise HTTPException(status_code=400, detail="Payment amount must be greater than zero and have no more than two decimal places.")
    if not receipt.filename:
        raise HTTPException(status_code=400, detail="Please upload the payment receipt.")

    filename = Path(receipt.filename).name
    suffix = Path(filename).suffix.lower()
    content_type = (receipt.content_type or "").lower()
    if not (suffix == ".pdf" or content_type == "application/pdf" or content_type.startswith("image/")):
        raise HTTPException(status_code=400, detail="Payment receipt must be an image or PDF.")

    max_file_size = 20 * 1024 * 1024
    content = await receipt.read(max_file_size + 1)
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded receipt is empty.")
    if len(content) > max_file_size:
        raise HTTPException(status_code=413, detail="Payment receipt exceeds the 20 MB file-size limit.")

    telegram_id = current_user["tgid"]
    current_phone = current_user.get("phone") or "Not provided"
    telegram_user = current_user.get("telegram_user") or {}
    telegram_name = (
        f"{telegram_user.get('first_name', '')} {telegram_user.get('last_name', '')}"
    ).strip() or "Not provided"

    receipt_message = (
        "🧾 <b>Kvitanciya</b>\n\n"
        f"👤 <b>FIO:</b> {html.escape(full_name)}\n"
        f"🆔 <b>ID nomeri:</b> <code>{html.escape(student_id)}</code>\n"
        f"🪪 <b>PINFL:</b> <code>{html.escape(pinfl)}</code>\n"
        f"📅 <b>Tólem sánesi:</b> <code>{parsed_payment_date.isoformat()}</code>\n"
        f"💰 <b>Tólem muǵdarı:</b> <code>{html.escape(format(payment_amount, ',.2f').replace(',', ' ').replace('.', ','))}</code>\n"
        f"📱 <b>Tg akkaunt ati:</b> {html.escape(telegram_name)}\n"
        f"📞 <b>Nomeri:</b> <code>{html.escape(str(current_phone))}</code>\n"
        f"💬 <b>Tg ID:</b> <code>{html.escape(str(telegram_id))}</code>"
    )
    send_options = {"chat_id": GROUP_TGID, "message_thread_id": RECEIPT_TOPIC_ID}

    try:
        await telegram_app.bot.send_message(
            **send_options, text=receipt_message, parse_mode="HTML"
        )
        await telegram_app.bot.send_document(
            **send_options,
            document=io.BytesIO(content),
            filename=filename,
            caption="Payment receipt",
        )
    except Exception as exc:
        logger.exception("Failed to send payment receipt to Telegram")
        raise HTTPException(
            status_code=502,
            detail="Telegram could not complete receipt delivery. Please contact support before submitting again.",
        ) from exc

    return {"ok": True, "message": "Your payment receipt and details were sent successfully."}


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

    # Get tutor information from HEMIS dataset
    dataset = load_json(GENERATED_DIR / "login.json")
    student_record = dataset.get(student_id, {}) if dataset else {}

    tutor_name = student_record.get("Tutor", "Not available") or "Not available"

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
    print("\n========== PHONE CHANGE DEBUG ==========")

    telegram_id = current_user["tgid"]
    current_phone = current_user.get("phone") or "Not provided"

    registered_phone_digits = re.sub(
        r"\D", "", str(current_phone)
    )[-9:]

    print("STEP 1: Endpoint reached")
    print("STEP 2: Telegram ID:", telegram_id)
    print("STEP 3: Registered phone available:", current_phone != "Not provided")
    print("STEP 4: Registered phone matches submitted phone:",
          registered_phone_digits == new_phone)

    if registered_phone_digits == new_phone:
        print("RESULT: DUPLICATE PHONE NUMBER DETECTED")
        print("=======================================\n")

        raise HTTPException(
            status_code=400,
            detail="This phone number is already registered to your Telegram account."
        )

    print("RESULT: Phone number is different. Continuing request.")
    print("=======================================\n")
    telegram_user = current_user.get("telegram_user") or {}

    first_name = telegram_user.get("first_name", "")
    last_name = telegram_user.get("last_name", "")

    telegram_name = f"{first_name} {last_name}".strip() or "Not provided"

    # 5. Escape values before inserting them into HTML
    safe_telegram_name = html.escape(telegram_name)
    safe_current_phone = html.escape(str(current_phone))
    safe_telegram_id = html.escape(str(telegram_id))
    safe_student_id = html.escape(student_id)
    safe_new_phone = html.escape(new_phone)
    safe_tutor_name = html.escape(str(tutor_name))

    #inline buttons
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Ózgertiw",
                callback_data="phone_change:changed"
            ),
            InlineKeyboardButton(
                "❌ Biykarlaw",
                callback_data="phone_change:cancel"
            ),
        ]
    ])

    # 6. Build the Telegram message
    message = (
        "<b>📱 Nomerin ózgertiw</b>\n\n"
        f"📞 Arzashı nomeri: <code>{safe_current_phone}</code>\n"
        f"🆔 Tg ID: <code>{safe_telegram_id}</code>\n"
        f"🎫 Akkaunt atı: {safe_telegram_name}\n"
        f"🪪 ID nomeri: <code>{safe_student_id}</code>\n"
        f"👤 <b>Juwapker: {safe_tutor_name}</b>\n\n"
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
            "reply_markup": keyboard,
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


async def handle_phone_change_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    message = query.message

    # 1. Verify the original group
    if not message or message.chat.id != GROUP_TGID:
        await query.answer(
            "This action is only available in the original request group.",
            show_alert=True,
        )
        return

    # 2. Verify that the clicking user is a group member
    try:
        member = await context.bot.get_chat_member(
            chat_id=GROUP_TGID,
            user_id=query.from_user.id,
        )

        if member.status not in ("creator", "administrator", "member"):
            await query.answer(
                "Only group members can process this request.",
                show_alert=True,
            )
            return

    except Exception:
        logger.exception(
            "Could not verify group membership for user %s",
            query.from_user.id,
        )
        await query.answer(
            "Could not verify your group membership.",
            show_alert=True,
        )
        return

    # 3. Get the original message
    original_text = message.text_html or ""

    # Prevent processing an already completed request
    if "📌 <b>Status:</b>" in original_text:
        await query.answer(
            "This request has already been processed.",
            show_alert=True,
        )
        return

    # 4. Determine the selected action
    try:
        action = query.data.split(":")[1]
    except (IndexError, AttributeError):
        await query.answer("Invalid action.", show_alert=True)
        return

    if action == "changed":
        status = "📌 <b>Status:</b> ✅ Ózgertildi"
    elif action == "cancel":
        status = "📌 <b>Status:</b> ❌ Biykarlandı"
    else:
        await query.answer("Unknown action.", show_alert=True)
        return

    # 5. Extract student's Telegram ID
    match = re.search(
        r"Tg ID:\s*<code>(\d+)</code>",
        original_text,
    )

    if not match:
        logger.error("Could not find student Telegram ID in request.")
        await query.answer(
            "Student Telegram ID was not found.",
            show_alert=True,
        )
        return

    student_telegram_id = int(match.group(1))
    # Extract requested new phone number
    phone_match = re.search(
        r"nomerin\s*<code>(.*?)</code>",
        original_text,
        re.IGNORECASE,
    )

    if not phone_match:
        logger.error("Could not find requested phone number in request.")
        await query.answer(
            "Requested phone number was not found.",
            show_alert=True,
        )
        return

    requested_phone = phone_match.group(1)
    if action == "changed":
        student_message = (
            f"📱 Telefon nomerińizdi <code>{requested_phone}</code> nomerine ózgertiw boyınsha arzańız <b>orınlandı</b>."
        )
    elif action == "cancel":
        student_message = (
            f"📱 Telefon nomerińizdi <code>{requested_phone}</code> nomerine ózgertiw boyınsha arzańız <b>biykarlandı</b>."
        )

    # 6. Update original group message and remove buttons
    try:
        await query.edit_message_text(
            text=f"{original_text}\n\n{status}",
            parse_mode="HTML",
            reply_markup=None,
        )

    except Exception:
        logger.exception("Could not update phone-change request message.")
        await query.answer(
            "Could not update the request message.",
            show_alert=True,
        )
        return

    # 7. Answer callback
    await query.answer("Request processed successfully.")

    # 8. Send private notification to student
    try:
        await context.bot.send_message(
            chat_id=student_telegram_id,
            text=student_message,
            parse_mode="HTML",
        )

    except Exception:
        logger.exception(
            "Could not send phone-change notification to student %s",
            student_telegram_id,
        )


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
