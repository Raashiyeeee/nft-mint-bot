"""
telegram-bot/main.py — Application entrypoint for the NFT Mint Telegram Bot.
Initializes PTB application, registers handlers, starts database pool,
runs internal notification server, and manages graceful shutdown.
"""
import asyncio
import logging
import os
import sys
from dotenv import load_dotenv
from aiohttp import web
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes

from db import queries
from engine.client import get_engine_client
from handlers.admin import get_admin_handlers
from handlers.admin_ui import get_admin_ui_handler
from handlers.user import get_user_handlers
from handlers.mint_flow import get_mint_conversation_handler
from handlers.wallet_flow import get_wallet_conversation_handler
from notifications.sender import NotificationSender, create_notification_server
from middleware.auth import OWNER_ID

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("telegram_bot")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log unhandled exceptions during update processing."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ An unexpected error occurred while processing your request. Please try again later."
            )
        except Exception:
            pass


async def post_init(application) -> None:
    """Hook called after application is initialized, before updates start."""
    logger.info("Initializing database connection pool...")
    database_url = os.environ.get(
        "DATABASE_URL",
        "postgres://nftbot:nftbot_secret@localhost:5432/nftbot_db"
    )
    await queries.init_pool(database_url)
    logger.info("Database pool established.")

    # Bootstrap owner (hard-coded OWNER_ID) as admin + authorized user on every start.
    # This is idempotent — safe to run repeatedly.
    try:
        pool = await queries.get_pool()
        await pool.execute(
            """
            INSERT INTO authorized_users (telegram_id, display_name, is_active)
            VALUES ($1, 'Owner', TRUE)
            ON CONFLICT (telegram_id) DO UPDATE SET is_active = TRUE
            """,
            OWNER_ID,
        )
        await pool.execute(
            """
            INSERT INTO admin_users (telegram_id)
            VALUES ($1)
            ON CONFLICT DO NOTHING
            """,
            OWNER_ID,
        )
        logger.info("Owner bootstrapped: telegram_id=%s", OWNER_ID)

        # Enforce: only the owner should be in admin_users.
        # Demote any other entries that may have been left from previous versions.
        demoted = await queries.revoke_non_owner_admins(OWNER_ID)
        if demoted:
            logger.warning("Demoted %d non-owner admin(s) to regular user on startup.", demoted)
    except Exception as e:
        logger.warning("Could not bootstrap owner: %s", e)

    # Start notification HTTP server on port 7879 to receive events from Rust engine
    port = int(os.environ.get("NOTIFICATION_PORT", "7879"))
    logger.info("Starting notification receiver server on port %s...", port)
    sender = NotificationSender(application.bot)
    aio_app = create_notification_server(sender, port=port)
    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    application.bot_data["notification_runner"] = runner
    logger.info("Notification receiver listening on 0.0.0.0:%s", port)


async def post_shutdown(application) -> None:
    """Hook called when application is shutting down."""
    logger.info("Shutting down bot services...")

    # Stop notification server
    runner = application.bot_data.get("notification_runner")
    if runner:
        logger.info("Stopping notification receiver server...")
        await runner.cleanup()

    # Close engine client
    engine = get_engine_client()
    await engine.close()

    # Close database pool
    logger.info("Closing database pool...")
    await queries.close_pool()
    logger.info("Shutdown complete.")


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN")
    if not token:
        logger.error("Missing TELEGRAM_BOT_TOKEN or BOT_TOKEN environment variable!")
        sys.exit(1)

    fernet_key = os.environ.get("FERNET_KEY")
    if not fernet_key:
        logger.warning(
            "FERNET_KEY is not set in environment! Wallet encryption/decryption will fail."
        )

    # Build Application with optimized HTTPX request settings
    from telegram.request import HTTPXRequest
    request = HTTPXRequest(
        connection_pool_size=20,
        read_timeout=15.0,
        write_timeout=15.0,
        connect_timeout=10.0,
        pool_timeout=10.0,
    )
    builder = ApplicationBuilder().token(token).request(request)
    builder.post_init(post_init)
    builder.post_shutdown(post_shutdown)
    application = builder.build()

    # Register handlers (ConversationHandlers must come before plain command handlers)
    application.add_handler(get_mint_conversation_handler())
    application.add_handler(get_wallet_conversation_handler())
    application.add_handler(get_admin_ui_handler())   # interactive owner admin panel

    for handler in get_admin_handlers():
        application.add_handler(handler)

    for handler in get_user_handlers():
        application.add_handler(handler)

    application.add_error_handler(error_handler)

    logger.info("Starting Telegram bot polling...")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
