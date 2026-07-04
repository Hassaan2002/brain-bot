from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
from io import BytesIO

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

# Keep Claude input bounded so one very large article does not create a costly
# or over-limit API request.
MAX_CLAUDE_INPUT_CHARS = 8000

# Claude must choose exactly one of these categories for weekly summaries.
CATEGORIES = [
    "AI/Tech",
    "Career",
    "Money/Finance",
    "Learning",
    "Ideas/Inspiration",
    "Personal",
]


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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                summary TEXT,
                category TEXT
            )
            """
        )
        add_missing_item_columns(connection)


def add_missing_item_columns(connection: sqlite3.Connection) -> None:
    """Add newer nullable columns when an older database is reused."""
    # PRAGMA table_info tells us the current columns. SQLite can add columns,
    # but this compatibility check avoids trying to add the same column twice.
    existing_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(items)").fetchall()
    }

    if "summary" not in existing_columns:
        connection.execute("ALTER TABLE items ADD COLUMN summary TEXT")

    if "category" not in existing_columns:
        connection.execute("ALTER TABLE items ADD COLUMN category TEXT")


def get_db_connection() -> sqlite3.Connection:
    """Open a database connection that returns rows by column name."""
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def save_item(item_type: str, content: str) -> None:
    """Insert one detected message item into the SQLite database."""
    with get_db_connection() as connection:
        # The question marks are SQL parameters. They safely pass values into
        # the query without building SQL by string concatenation.
        connection.execute(
            "INSERT INTO items (type, content) VALUES (?, ?)",
            (item_type, content),
        )


def get_all_items() -> list[sqlite3.Row]:
    """Read all saved items from the SQLite database."""
    with get_db_connection() as connection:
        # Return the oldest items first so the debug output reads like history.
        return connection.execute(
            """
            SELECT id, type, content, created_at
            FROM items
            ORDER BY id
            """
        ).fetchall()


def get_items_from_last_week() -> list[sqlite3.Row]:
    """Read saved items whose SQLite timestamp is within the last 7 days."""
    with get_db_connection() as connection:
        return connection.execute(
            """
            SELECT id, type, content, created_at, summary, category
            FROM items
            WHERE created_at >= datetime('now', '-7 days')
            ORDER BY id
            """
        ).fetchall()


def get_item_by_id(item_id: int) -> sqlite3.Row | None:
    """Find one saved item by id."""
    with get_db_connection() as connection:
        return connection.execute(
            """
            SELECT id, type, content, created_at, summary, category
            FROM items
            WHERE id = ?
            """,
            (item_id,),
        ).fetchone()


def update_item_summary(item_id: int, summary: str, category: str) -> None:
    """Store the cached short summary and category for an item."""
    with get_db_connection() as connection:
        connection.execute(
            """
            UPDATE items
            SET summary = ?, category = ?
            WHERE id = ?
            """,
            (summary, category, item_id),
        )


def truncate_content(content: str, max_length: int = 50) -> str:
    """Shorten long content so the debug message stays readable."""
    if len(content) <= max_length:
        return content

    return content[:max_length] + "..."


def extract_first_url(text: str) -> str | None:
    """Find the first http:// or https:// URL inside saved message text."""
    match = re.search(r"https?://\S+", text)
    if not match:
        return None

    return match.group(0).rstrip(".,)")


def source_reference(item: sqlite3.Row) -> str:
    """Create a compact source label for summaries."""
    if item["type"] == "link":
        return extract_first_url(item["content"]) or item["content"]

    if item["type"] == "photo":
        return "[screenshot]"

    return "[note]"


async def extract_content(
    item: sqlite3.Row, context: ContextTypes.DEFAULT_TYPE | None = None
) -> str | None:
    """Return raw text that Claude can summarize for one saved item."""
    if item["type"] == "text":
        return item["content"]

    if item["type"] == "link":
        # trafilatura fetches and extracts the main readable article text,
        # which is usually cleaner than sending menus, ads, and sidebars.
        import trafilatura

        url = extract_first_url(item["content"])
        if not url:
            return None

        downloaded = await asyncio.to_thread(trafilatura.fetch_url, url)
        if not downloaded:
            return None

        extracted = await asyncio.to_thread(trafilatura.extract, downloaded)
        return extracted.strip() if extracted else None

    if item["type"] == "photo":
        # Photos are stored as Telegram file_ids, so we ask Telegram for the
        # actual file bytes before OCR can read text from the image.
        if context is None:
            return None

        from PIL import Image
        import pytesseract

        telegram_file = await context.bot.get_file(item["content"])
        image_bytes = await telegram_file.download_as_bytearray()

        with Image.open(BytesIO(image_bytes)) as image:
            extracted = await asyncio.to_thread(pytesseract.image_to_string, image)

        return extracted.strip() if extracted else None

    return None


def parse_claude_json(text: str) -> dict:
    """Parse Claude JSON, including responses wrapped in markdown fences."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.removeprefix("json").strip()

    return json.loads(cleaned)


def call_claude(text: str, mode: str) -> dict:
    """Ask Claude for either a cached short summary or a detailed summary."""
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is missing from the environment.")

    # Truncate the source text before sending it to control cost and avoid
    # exceeding model input limits on very long articles or OCR output.
    safe_text = text[:MAX_CLAUDE_INPUT_CHARS]

    if mode == "short":
        prompt = (
            "Summarize the content in 1-2 sentences and choose exactly one "
            "category from this fixed list: "
            f"{', '.join(CATEGORIES)}.\n\n"
            'Return only valid JSON shaped like: {"summary": "...", '
            '"category": "..."}.\n\n'
            f"Content:\n{safe_text}"
        )
        max_tokens = 500
    elif mode == "long":
        prompt = (
            "Write a thorough summary of roughly 300-500 words covering the "
            "key points, arguments, and important details. Use plain, clear "
            'prose.\n\nReturn only valid JSON shaped like: {"summary": "..."}.\n\n'
            f"Content:\n{safe_text}"
        )
        max_tokens = 1400
    else:
        raise ValueError(f"Unsupported Claude mode: {mode}")

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
        max_tokens=max_tokens,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )

    # Anthropic returns message content as blocks. We join text blocks before
    # parsing the JSON the prompt requested.
    response_text = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    )
    parsed = parse_claude_json(response_text)

    if mode == "short" and parsed.get("category") not in CATEGORIES:
        parsed["category"] = "Personal"

    return parsed


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
    for item in items:
        short_content = truncate_content(item["content"])
        lines.append(
            f"{item['id']}. type: {item['type']}\n"
            f"   content: {short_content}\n"
            f"   created_at: {item['created_at']}"
        )

    await update.message.reply_text("\n\n".join(lines))


async def weekly(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Summarize saves from the last week, grouped by Claude category."""
    items = get_items_from_last_week()

    if not items:
        await update.message.reply_text("No saves in the last week \U0001f440")
        return

    processed_items = []
    for item in items:
        summary = item["summary"]
        category = item["category"]

        # Only call extraction and Claude for items that have not already been
        # processed. This keeps /weekly faster and avoids repeated API cost.
        if not summary or not category:
            raw_text = await extract_content(item, context)

            if raw_text is None:
                summary = "Could not extract content"
                category = "Personal"
            else:
                try:
                    claude_result = await asyncio.to_thread(
                        call_claude, raw_text, mode="short"
                    )
                except Exception as error:
                    logger.exception("Claude short summary failed for item %s", item["id"])
                    await update.message.reply_text(f"Claude summary failed: {error}")
                    return

                summary = claude_result.get("summary", "").strip()
                category = claude_result.get("category", "Personal").strip()

                if category not in CATEGORIES:
                    category = "Personal"

            update_item_summary(item["id"], summary, category)

        processed_items.append(
            {
                "id": item["id"],
                "summary": summary,
                "category": category,
                "source": source_reference(item),
            }
        )

    lines = ["Weekly saves:"]
    for category in CATEGORIES:
        category_items = [
            item for item in processed_items if item["category"] == category
        ]
        if not category_items:
            continue

        lines.append(f"\n{category}")
        for item in category_items:
            lines.append(
                f"[{item['id']}] {item['summary']}\n"
                f"Source: {item['source']}"
            )

    await update.message.reply_text("\n".join(lines))


async def details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with a fresh detailed summary for one saved item."""
    if not context.args:
        await update.message.reply_text("Use /details <id>")
        return

    try:
        item_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Use /details <id>")
        return

    item = get_item_by_id(item_id)
    if item is None:
        await update.message.reply_text("Couldn't find that item")
        return

    raw_text = await extract_content(item, context)
    if raw_text is None:
        await update.message.reply_text(
            f"Could not extract content from {source_reference(item)}"
        )
        return

    try:
        claude_result = await asyncio.to_thread(call_claude, raw_text, mode="long")
    except Exception as error:
        logger.exception("Claude detailed summary failed for item %s", item["id"])
        await update.message.reply_text(f"Claude summary failed: {error}")
        return

    summary = claude_result.get("summary", "").strip()
    await update.message.reply_text(
        f"Details for [{item['id']}]\n"
        f"Source: {source_reference(item)}\n\n"
        f"{summary}"
    )


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

    # /weekly summarizes recent saves by category.
    application.add_handler(CommandHandler("weekly", weekly))

    # /details <id> gives a fresh detailed summary for one saved item.
    application.add_handler(CommandHandler("details", details))

    # This catches every non-command message, including text, photos,
    # documents, stickers, voice messages, and more.
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_any_message))

    logger.info("Bot is starting with polling...")

    # Polling means the bot asks Telegram for new updates repeatedly.
    # This is simpler than webhooks for local development and learning.
    application.run_polling()


if __name__ == "__main__":
    main()
