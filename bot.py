import logging
import os

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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply when a user sends the /start command."""
    await update.message.reply_text(
        "Welcome! Send me any content and I will save it for you. "
        "Later, I will help summarize what you share."
    )


async def handle_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply to any non-command message the bot receives."""
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

    # Reply to the message, whether it was text, a photo, a document, etc.
    await message.reply_text("Got it 👍")


def main() -> None:
    """Create and run the Telegram bot using polling."""
    bot_token = os.getenv("BOT_TOKEN")

    # Stop early with a helpful error if BOT_TOKEN is missing.
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is missing. Add it to your .env file.")

    # Build the async python-telegram-bot application.
    application = Application.builder().token(bot_token).build()

    # /start has its own welcome response.
    application.add_handler(CommandHandler("start", start))

    # This catches every non-command message, including text, photos,
    # documents, stickers, voice messages, and more.
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_any_message))

    logger.info("Bot is starting with polling...")

    # Polling means the bot asks Telegram for new updates repeatedly.
    # This is simpler than webhooks for local development and learning.
    application.run_polling()


if __name__ == "__main__":
    main()
