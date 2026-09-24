"""
handlers/wallet_flow.py — Self-service wallet import conversation.

Flow (4 steps):
  1. /importwallet → Chain selection
  2. Private key input (message auto-deleted for security)
  3. Optional wallet label
  4. Confirmation

Security: Private key message is deleted immediately after reading.
         Keys are encrypted with Fernet before DB storage.
"""
import logging

from eth_account import Account
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from db import queries
from middleware.auth import authorized, check_authorized_inline
from utils.crypto import encrypt_private_key, validate_private_key_format, mask_address
from utils.formatters import CHAIN_DISPLAY, CHAIN_EMOJI

logger = logging.getLogger(__name__)

# ── Conversation states ───────────────────────────────────────────────────────
WF_KEY   = 11
WF_LABEL = 12


# ── Step 1: /importwallet → Prompt for Private Key ───────────────────────────

@authorized
async def start_wallet_import(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()

    await update.message.reply_text(
        "🔐 *Import Wallet*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔑 *Send your private key*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ *Your message will be deleted immediately* after reading.\n\n"
        "• Include or omit the `0x` prefix\n"
        "• Must be 64 hex characters (32 bytes)\n\n"
        "_Paste your private key now, or /cancel to abort:_",
        parse_mode="Markdown",
    )
    return WF_KEY


# ── Step 2: Receive private key → Prompt for Label ────────────────────────────

async def handle_wf_private_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_inline(update):
        return ConversationHandler.END

    raw_key = update.message.text.strip()

    # Delete the message immediately to avoid it sitting in chat history
    try:
        await update.message.delete()
    except Exception:
        pass  # May fail if bot lacks delete permission; not fatal

    if raw_key.startswith("/cancel"):
        await update.effective_chat.send_message("❌ Wallet import cancelled.")
        return ConversationHandler.END

    if not validate_private_key_format(raw_key):
        await update.effective_chat.send_message(
            "⚠️ Invalid private key format.\n"
            "Must be 64 hex characters (32 bytes), with or without `0x` prefix.\n\n"
            "Try again or use /cancel:",
            parse_mode="Markdown",
        )
        return WF_KEY

    # Derive Ethereum address from private key
    try:
        key_normalized = raw_key.strip()
        if not key_normalized.startswith("0x"):
            key_normalized = "0x" + key_normalized
        acct = Account.from_key(key_normalized)
        derived_address = acct.address.lower()
    except Exception as e:
        await update.effective_chat.send_message(
            f"⚠️ Could not derive address from key: {e}\n"
            "Please check the key and try again, or /cancel:"
        )
        return WF_KEY

    # Check duplicate
    user_id = update.effective_user.id
    already_exists = await queries.wallet_address_exists(user_id, derived_address)
    if already_exists:
        await update.effective_chat.send_message(
            f"⚠️ Wallet `{mask_address(derived_address)}` is already registered.\n"
            "Use /cancel or import a different key:",
            parse_mode="Markdown",
        )
        return WF_KEY

    # Encrypt and store temporarily in user_data
    try:
        encrypted = encrypt_private_key(key_normalized)
    except Exception as e:
        await update.effective_chat.send_message(f"❌ Encryption failed: {e}")
        return ConversationHandler.END

    context.user_data["wf_address"] = derived_address
    context.user_data["wf_encrypted_key"] = encrypted

    keyboard = [[
        InlineKeyboardButton("⏭ Skip Name", callback_data="wf_skip_label"),
        InlineKeyboardButton("❌ Cancel", callback_data="wf_cancel"),
    ]]

    await update.effective_chat.send_message(
        f"✅ Key validated!\n"
        f"📍 Derived address: `{mask_address(derived_address)}`\n\n"
        "📝 *Enter a name for this wallet* (e.g. 'main', 'sniper 1')\n"
        "or tap *Skip Name*:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )
    return WF_LABEL


# ── Step 3: Name / Label → confirm & save ────────────────────────────────────

async def handle_wf_label_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_inline(update):
        return ConversationHandler.END

    label = update.message.text.strip()
    if label.startswith("/cancel"):
        await update.message.reply_text("❌ Wallet import cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    context.user_data["wf_label"] = label[:64]  # Truncate at 64 chars
    return await _do_save_wallet(update.effective_chat, update.effective_user.id, context)


async def handle_wf_label_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_inline(update):
        return ConversationHandler.END

    query = update.callback_query
    await query.answer()

    if query.data == "wf_cancel":
        await query.edit_message_text("❌ Wallet import cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    if query.data == "wf_skip_label":
        context.user_data["wf_label"] = None
        return await _do_save_wallet(query.message.chat, update.effective_user.id, context)


async def _do_save_wallet(chat, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Persist the wallet to DB and send confirmation."""
    address   = context.user_data["wf_address"]
    encrypted = context.user_data["wf_encrypted_key"]
    label     = context.user_data.get("wf_label")

    try:
        wallet_id = await queries.add_wallet(
            telegram_id=user_id,
            wallet_address=address,
            wallet_label=label,
            chain="evm",
            private_key_encrypted=encrypted,
        )
        await chat.send_message(
            f"✅ *Wallet Imported Successfully!*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🏷 Name: *{label or 'No name'}*\n"
            f"📍 Address: `{mask_address(address)}`\n"
            f"🆔 Wallet ID: #{wallet_id}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"When you use /mint, you can select which network/chain to use for this wallet.",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error("Wallet import DB error: %s", e)
        await chat.send_message(f"❌ Failed to save wallet: {e}")
    finally:
        context.user_data.clear()

    return ConversationHandler.END


# ── Cancel & Timeout ──────────────────────────────────────────────────────────

async def wf_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Wallet import cancelled.")
    return ConversationHandler.END


async def wf_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if update.effective_message:
        await update.effective_message.reply_text(
            "⏱ Wallet import timed out. Use /importwallet to start again."
        )
    return ConversationHandler.END


def get_wallet_conversation_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("importwallet", start_wallet_import)],
        states={
            WF_KEY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_wf_private_key),
                CommandHandler("cancel", wf_cancel),
            ],
            WF_LABEL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_wf_label_text),
                CallbackQueryHandler(
                    handle_wf_label_callback, pattern=r"^(wf_skip_label|wf_cancel)$"
                ),
                CommandHandler("cancel", wf_cancel),
            ],
            ConversationHandler.TIMEOUT: [
                MessageHandler(filters.ALL, wf_timeout),
            ],
        },
        fallbacks=[CommandHandler("cancel", wf_cancel)],
        conversation_timeout=180,  # 3 minutes
    )

