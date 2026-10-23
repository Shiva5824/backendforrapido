import os
import time
import sqlite3
import logging
import threading

from flask import Flask, request, jsonify
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ============================================================
# CONFIGURATION
# ============================================================

PORT = int(os.getenv("PORT", "8080"))

DATABASE_FILE = os.getenv(
    "DATABASE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "allowed_users.db"),
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

ADMIN_USER_IDS = [
    int(user_id.strip())
    for user_id in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",")
    if user_id.strip().isdigit()
]

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger(__name__)

# ============================================================
# DATABASE
# ============================================================

def get_db_connection():
    """Create a SQLite connection with a reasonable timeout."""
    return sqlite3.connect(DATABASE_FILE, timeout=10)


def init_db():
    """Create the database directory and table if they do not exist."""
    database_directory = os.path.dirname(
        os.path.abspath(DATABASE_FILE)
    )

    os.makedirs(database_directory, exist_ok=True)

    with get_db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS allowed_users (
                phone TEXT PRIMARY KEY,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

    logger.info("Database initialized.")


def clean_phone_number(phone: str) -> str:
    """Keep digits only."""
    return "".join(filter(str.isdigit, str(phone)))


def is_valid_phone(phone: str) -> bool:
    """Validate a 10-digit phone number."""
    return len(clean_phone_number(phone)) == 10


def is_phone_allowed(phone: str) -> bool:
    """Check whether a phone number is authorized."""
    clean_phone = clean_phone_number(phone)

    if len(clean_phone) != 10:
        return False

    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM allowed_users WHERE phone = ?",
            (clean_phone,),
        ).fetchone()

    return row is not None


def add_phone_user(phone: str) -> bool:
    """Add or update an authorized phone number."""
    clean_phone = clean_phone_number(phone)

    if len(clean_phone) != 10:
        return False

    try:
        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO allowed_users (phone)
                VALUES (?)
                ON CONFLICT(phone) DO NOTHING
                """,
                (clean_phone,),
            )

        logger.info("Authorized phone number added or already present.")
        return True

    except sqlite3.Error:
        logger.exception("Failed to authorize phone number.")
        return False


def remove_phone_user(phone: str) -> bool:
    """Remove an authorized phone number."""
    clean_phone = clean_phone_number(phone)

    if len(clean_phone) != 10:
        return False

    try:
        with get_db_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM allowed_users WHERE phone = ?",
                (clean_phone,),
            )

            return cursor.rowcount > 0

    except sqlite3.Error:
        logger.exception("Failed to remove phone number.")
        return False


def get_all_users():
    """Return authorized phone numbers and their creation timestamps."""
    with get_db_connection() as conn:
        return conn.execute(
            """
            SELECT phone, added_at
            FROM allowed_users
            ORDER BY added_at DESC
            """
        ).fetchall()


# Initialize the database when the application starts.
init_db()

# ============================================================
# FLASK API
# ============================================================

app = Flask(__name__)


@app.route("/", methods=["GET"])
def home():
    """Basic service health check."""
    return jsonify({
        "status": "online",
        "service": "Phone Authorization API",
    })


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint for deployment monitoring."""
    return jsonify({"status": "healthy"}), 200


@app.route("/check", methods=["GET"])
def check_user():
    """Check whether a phone number is authorized."""
    phone = request.args.get("phone", "").strip()
    allowed = is_phone_allowed(phone)

    logger.info("Authorization check completed: allowed=%s", allowed)

    return jsonify({
        "status": "success",
        "phone": phone,
        "allowed": allowed,
    })


# ============================================================
# TELEGRAM ADMIN AUTHORIZATION
# ============================================================

def is_admin(update: Update) -> bool:
    """Return True only for configured Telegram administrators."""
    user = update.effective_user

    return (
        user is not None
        and user.id in ADMIN_USER_IDS
    )


async def reject_non_admin(update: Update) -> bool:
    """Reject requests from users who are not administrators."""
    if not is_admin(update):
        if update.effective_message:
            await update.effective_message.reply_text(
                "⛔ Unauthorized user."
            )
        return True

    return False


# ============================================================
# TELEGRAM BOT COMMANDS
# ============================================================

async def start_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if await reject_non_admin(update):
        return

    msg = (
        "🤖 *Auto Driver Assistance User Access Manager*\n\n"
        "Available Commands:\n"
        "• `/add <10-digit-phone>` - Allow a user\n"
        "• `/remove <10-digit-phone>` - Revoke access\n"
        "• `/list` - Show authorized numbers\n"
        "• `/check <10-digit-phone>` - Check authorization"
    )

    await update.effective_message.reply_text(
        msg,
        parse_mode="Markdown",
    )


async def add_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if await reject_non_admin(update):
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /add <10-digit-phone>"
        )
        return

    phone = context.args[0]

    if not is_valid_phone(phone):
        await update.effective_message.reply_text(
            "❌ Invalid phone number. Enter exactly 10 digits."
        )
        return

    if add_phone_user(phone):
        await update.effective_message.reply_text(
            f"✅ Mobile number {clean_phone_number(phone)} authorized."
        )
    else:
        await update.effective_message.reply_text(
            "❌ Could not authorize the number. Check server logs."
        )


async def remove_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if await reject_non_admin(update):
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /remove <10-digit-phone>"
        )
        return

    phone = context.args[0]

    if not is_valid_phone(phone):
        await update.effective_message.reply_text(
            "❌ Invalid phone number. Enter exactly 10 digits."
        )
        return

    if remove_phone_user(phone):
        await update.effective_message.reply_text(
            f"🗑️ Access revoked for {clean_phone_number(phone)}."
        )
    else:
        await update.effective_message.reply_text(
            "⚠️ Number is invalid, unauthorized, or could not be removed."
        )


async def list_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if await reject_non_admin(update):
        return

    users = get_all_users()

    if not users:
        await update.effective_message.reply_text(
            "📱 No authorized numbers found."
        )
        return

    lines = ["📋 Authorized Users:\n"]

    for phone, added_at in users:
        lines.append(f"• {phone} (Added: {added_at})")

    # Telegram messages have a maximum length. Split long lists.
    message = ""

    for line in lines:
        if len(message) + len(line) + 1 > 3500:
            await update.effective_message.reply_text(message)
            message = ""

        message += line + "\n"

    if message:
        await update.effective_message.reply_text(message)


async def check_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if await reject_non_admin(update):
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /check <10-digit-phone>"
        )
        return

    phone = context.args[0]

    if not is_valid_phone(phone):
        await update.effective_message.reply_text(
            "❌ Invalid phone number. Enter exactly 10 digits."
        )
        return

    allowed = is_phone_allowed(phone)

    status = (
        "✅ AUTHORIZED"
        if allowed
        else "❌ NOT AUTHORIZED"
    )

    await update.effective_message.reply_text(
        f"Result for {clean_phone_number(phone)}: {status}"
    )


# ============================================================
# TELEGRAM BOT STARTUP
# ============================================================

def run_telegram_bot():
    """Start the Telegram bot using long polling."""
    if not TELEGRAM_BOT_TOKEN:
        logger.warning(
            "TELEGRAM_BOT_TOKEN is not configured. Bot is disabled."
        )
        return

    if not ADMIN_USER_IDS:
        logger.warning(
            "ADMIN_TELEGRAM_IDS is empty. No Telegram user can administer the bot."
        )

    bot_app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    bot_app.add_handler(CommandHandler("start", start_cmd))
    bot_app.add_handler(CommandHandler("add", add_cmd))
    bot_app.add_handler(CommandHandler("remove", remove_cmd))
    bot_app.add_handler(CommandHandler("list", list_cmd))
    bot_app.add_handler(CommandHandler("check", check_cmd))

    logger.info("Starting Telegram bot polling...")

    bot_app.run_polling()


# ============================================================
# LOCAL WINDOWS STARTUP
# ============================================================

if __name__ == "__main__":
    logger.info("Starting local Flask API and Telegram bot.")

    if TELEGRAM_BOT_TOKEN:
        bot_thread = threading.Thread(
            target=run_telegram_bot,
            daemon=True,
        )
        bot_thread.start()

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False,
    )
