import logging
import os
import sqlite3

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# Load environment variables from the .env file.
# This lets you keep your Telegram bot token out of the Python code.
load_dotenv()


# Set up basic console logging so you can see what the bot is doing.
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# Store saved items in a SQLite database file. DB_PATH can point to a custom
# file, while the default keeps local testing simple.
DB_PATH = os.getenv("DB_PATH", "brainbot.db")


def create_items_table() -> None:
    """Create the SQLite table once when the bot starts."""
    # sqlite3.connect creates the database file automatically if it does not
    # already exist.
    with sqlite3.connect(DB_PATH) as connection:
        # The items table stores one row for each thing the user sends.
        # created_at uses SQLite's CURRENT_TIMESTAMP so Python does not need to
        # calculate the time manually.
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT,
                content TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def save_item(item_type: str, content: str) -> None:
    """Insert one detected message item into the SQLite database."""
    with sqlite3.connect(DB_PATH) as connection:
        # The question marks are SQL parameters. They safely pass values into
        # the query without building SQL by string concatenation.
        connection.execute(
            "INSERT INTO items (type, content) VALUES (?, ?)",
            (item_type, content),
        )


def get_all_items() -> list[tuple[int, str, str, str]]:
    """Read all saved items from the SQLite database."""
    with sqlite3.connect(DB_PATH) as connection:
        # Return the oldest items first so the debug output reads like history.
        return connection.execute(
            """
            SELECT id, type, content, created_at
            FROM items
            ORDER BY id
            """
        ).fetchall()


def truncate_content(content: str, max_length: int = 50) -> str:
    """Shorten long content so the debug message stays readable."""
    if len(content) <= max_length:
        return content

    return content[:max_length] + "..."


def detect_message_item(message) -> tuple[str, str]:
    """Detect whether a Telegram message is a photo, link, or plain text."""
    # Check for photos first. Telegram sends multiple sizes of the same photo,
    # and the largest size is the last item in message.photo.
    if message.photo:
        largest_photo = message.photo[-1]
        return "photo", largest_photo.file_id

    # For text messages, keep the complete message text as the saved content.
    # If Telegram sends a non-text message that is not a photo, this becomes an
    # empty note instead of crashing.
    text = message.text or ""

    # After photos, detect links by looking for either common URL prefix.
    if "http://" in text or "https://" in text:
        return "link", text

    # Anything else is treated as a plain note.
    return "text", text


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply when a user sends the /start command."""
    await update.message.reply_text(
        "Welcome! Send me any content and I will save it for you. "
        "Later, I will help summarize what you share."
    )


async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with a readable list of all saved database rows."""
    items = get_all_items()

    # Give a clear response when the database table exists but has no rows yet.
    if not items:
        await update.message.reply_text("No saved items yet.")
        return

    # Build one readable block per row. Content is truncated to avoid very long
    # Telegram messages when a note or URL is large.
    lines = ["Saved items:"]
    for item_id, item_type, content, created_at in items:
        short_content = truncate_content(content)
        lines.append(
            f"{item_id}. type: {item_type}\n"
            f"   content: {short_content}\n"
            f"   created_at: {created_at}"
        )

    await update.message.reply_text("\n\n".join(lines))


async def handle_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Detect, save, and reply to any non-command message the bot receives."""
    user = update.effective_user
    message = update.effective_message

    # Log who sent the message. Some users may not have a username,
    # so we also include their numeric Telegram user ID.
    logger.info(
        "Received message from %s %s (@%s, id=%s)",
        user.first_name if user else "Unknown",
        user.last_name if user and user.last_name else "",
        user.username if user and user.username else "no_username",
        user.id if user else "unknown",
    )

    # Figure out what kind of content the user sent, then save it locally.
    item_type, content = detect_message_item(message)
    save_item(item_type, content)

    # Send a friendly confirmation that matches the detected item type.
    replies = {
        "link": "Saved a link \U0001f517",
        "photo": "Saved a screenshot \U0001f4f8",
        "text": "Saved a note \U0001f4dd",
    }
    await message.reply_text(replies[item_type])


def main() -> None:
    """Create and run the Telegram bot using polling."""
    bot_token = os.getenv("BOT_TOKEN")

    # Stop early with a helpful error if BOT_TOKEN is missing.
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is missing. Add it to your .env file.")

    # Make sure the database table exists before Telegram updates arrive.
    create_items_table()

    # Build the async python-telegram-bot application.
    application = Application.builder().token(bot_token).build()

    # /start has its own welcome response.
    application.add_handler(CommandHandler("start", start))

    # /debug shows all saved rows from the SQLite database.
    application.add_handler(CommandHandler("debug", debug))

    # This catches every non-command message, including text, photos,
    # documents, stickers, voice messages, and more.
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_any_message))

    logger.info("Bot is starting with polling...")

    # Polling means the bot asks Telegram for new updates repeatedly.
    # This is simpler than webhooks for local development and learning.
    application.run_polling()


if __name__ == "__main__":
    main()
