"""
middleware/auth.py — Access control and rate limiting middleware.

Every bot command must pass through check_authorized() before executing.
Unauthorized attempts are logged to denied_access_log.
Rate limit: max 10 commands per minute per telegram_id.

Owner: 1386285936 — the only user who can manage other admins/users.
"""
import functools
import logging
from typing import Callable, Optional

from telegram import Update
from telegram.ext import ContextTypes

from db import queries

logger = logging.getLogger(__name__)

# ── Hard-coded owner ──────────────────────────────────────────────────────────
# Only this Telegram ID can add/remove users and promote/revoke admins.
OWNER_ID: int = 1386285936

DENIED_MESSAGE       = "⛔ You are not authorized to use this bot. Contact the admin."
OWNER_ONLY_MESSAGE   = "⛔ This command is restricted to the bot owner only."
ADMIN_ONLY_MESSAGE   = "⛔ This command is restricted to admins."
RATE_LIMITED_MESSAGE = "⚠️ Too many commands. Please wait a moment."


def _get_command_text(update: Update) -> Optional[str]:
    """Safely extract command text from update."""
    if update.message:
        return update.message.text
    return None


def authorized(func: Callable) -> Callable:
    """
    Decorator: check access control before running any handler.
    The OWNER is always authorized regardless of DB state.

    Usage:
        @authorized
        async def start(update, context):
            ...
    """
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user:
            return

        telegram_id = user.id
        username = user.username
        command = _get_command_text(update)

        # ── Rate limit check ──────────────────────────────────────────────────
        within_limit = await queries.check_and_increment_rate(telegram_id, max_per_minute=10)
        if not within_limit:
            logger.warning("Rate limit hit: telegram_id=%s", telegram_id)
            if update.effective_message:
                await update.effective_message.reply_text(RATE_LIMITED_MESSAGE)
            return

        # ── Owner always passes ───────────────────────────────────────────────
        if telegram_id == OWNER_ID:
            return await func(update, context)

        # ── Authorization check ───────────────────────────────────────────────
        user_record = await queries.get_authorized_user(telegram_id)
        if not user_record:
            logger.warning(
                "Unauthorized access: telegram_id=%s command=%s", telegram_id, command
            )
            await queries.log_denied_access(telegram_id, username, command)
            if update.callback_query:
                try:
                    await update.callback_query.answer(DENIED_MESSAGE, show_alert=True)
                except Exception:
                    pass
            elif update.effective_message:
                await update.effective_message.reply_text(DENIED_MESSAGE)
            return

        return await func(update, context)

    return wrapper


def owner_only(func: Callable) -> Callable:
    """
    Decorator: passes ONLY if caller is the hard-coded OWNER_ID.
    Use on user/admin management commands.
    """
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user:
            return

        telegram_id = user.id
        username = user.username
        command = _get_command_text(update)

        # ── Rate limit ────────────────────────────────────────────────────────
        within_limit = await queries.check_and_increment_rate(telegram_id, max_per_minute=10)
        if not within_limit:
            if update.effective_message:
                await update.effective_message.reply_text(RATE_LIMITED_MESSAGE)
            return

        # ── Owner check ───────────────────────────────────────────────────────
        if telegram_id != OWNER_ID:
            logger.warning(
                "Non-owner tried owner command: telegram_id=%s command=%s", telegram_id, command
            )
            await queries.log_denied_access(telegram_id, username, command, reason="not_owner")
            if update.callback_query:
                try:
                    await update.callback_query.answer(OWNER_ONLY_MESSAGE, show_alert=True)
                except Exception:
                    pass
            elif update.effective_message:
                await update.effective_message.reply_text(OWNER_ONLY_MESSAGE)
            return

        return await func(update, context)

    return wrapper


def admin_only(func: Callable) -> Callable:
    """
    Decorator: passes if caller is OWNER or in admin_users table.
    Rate-limits before checking.
    """
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user:
            return

        telegram_id = user.id
        username = user.username
        command = _get_command_text(update)

        # ── Rate limit ────────────────────────────────────────────────────────
        within_limit = await queries.check_and_increment_rate(telegram_id, max_per_minute=10)
        if not within_limit:
            if update.effective_message:
                await update.effective_message.reply_text(RATE_LIMITED_MESSAGE)
            return

        # ── Owner always passes as admin ──────────────────────────────────────
        if telegram_id == OWNER_ID:
            return await func(update, context)

        # ── Admin check ───────────────────────────────────────────────────────
        is_admin = await queries.is_admin(telegram_id)
        if not is_admin:
            logger.warning(
                "Non-admin tried admin command: telegram_id=%s command=%s", telegram_id, command
            )
            await queries.log_denied_access(telegram_id, username, command, reason="not_admin")
            if update.callback_query:
                try:
                    await update.callback_query.answer(ADMIN_ONLY_MESSAGE, show_alert=True)
                except Exception:
                    pass
            elif update.effective_message:
                await update.effective_message.reply_text(ADMIN_ONLY_MESSAGE)
            return

        return await func(update, context)

    return wrapper


async def check_authorized_inline(update: Update) -> bool:
    """
    Non-decorator version for use inside ConversationHandlers.
    Owner always returns True. Returns True if authorized.
    """
    user = update.effective_user
    if not user:
        return False

    if user.id == OWNER_ID:
        return True

    user_record = await queries.get_authorized_user(user.id)
    if not user_record:
        await queries.log_denied_access(user.id, user.username, "inline_check")
        return False
    return True
