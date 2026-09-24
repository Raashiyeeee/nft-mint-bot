"""
handlers/admin_ui.py — Interactive owner-only admin management panel.

Commands:
  /admin — Open the bot admin panel (owner-only)

Features (all via inline buttons):
  • View paginated list of all authorized users with role badges
  • Per-user detail view: role, status, wallet count, expiry
  • Add a new user (prompts for Telegram ID and optional username)
  • Toggle admin / user role
  • Toggle active / inactive status
  • Delete a user (with private key wipe confirmation)
  • Cancel / back navigation at every step

Access: OWNER_ID (1386285936) only.
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

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
from middleware.auth import owner_only, OWNER_ID
from utils.formatters import format_utc

logger = logging.getLogger(__name__)

# ── Conversation states ───────────────────────────────────────────────────────
AU_LIST        = 100
AU_USER_VIEW   = 101
AU_ADD_ID      = 102
AU_ADD_NAME    = 103
AU_ADD_DAYS    = 104
AU_CONFIRM_DEL = 105

USERS_PER_PAGE = 8


# ── Helpers ───────────────────────────────────────────────────────────────────

def _role_badge(u: dict, owner_id: int) -> str:
    if u["telegram_id"] == owner_id:
        return "👑 Owner"
    if u.get("is_admin"):
        return "🔧 Admin"
    return "👤 User"


async def _owner_check_inline(update: Update) -> bool:
    user = update.effective_user
    if not user or user.id != OWNER_ID:
        if update.effective_message:
            await update.effective_message.reply_text("⛔ Owner only.")
        return False
    return True


async def _show_user_list(target, context, page: int, edit: bool = True):
    offset = (page - 1) * USERS_PER_PAGE
    users  = await queries.list_authorized_users_with_roles(offset=offset, limit=USERS_PER_PAGE)
    total  = await queries.count_authorized_users()
    total_pages = max(1, (total + USERS_PER_PAGE - 1) // USERS_PER_PAGE)

    lines = [f"👥 *Bot Users* — Page {page}/{total_pages} ({total} total)\n"]
    keyboard = []

    for u in users:
        u = dict(u)
        role   = _role_badge(u, OWNER_ID)
        status = "✅" if u["is_active"] else "❌"
        name   = u.get("telegram_username") or u.get("display_name") or str(u["telegram_id"])
        lines.append(f"{status} {role} | @{name} `({u['telegram_id']})`")
        keyboard.append([InlineKeyboardButton(
            f"{status} {name} [{role}]",
            callback_data=f"au_view:{u['telegram_id']}",
        )])

    # Pagination row
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"au_list:{page - 1}"))
    if page * USERS_PER_PAGE < total:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"au_list:{page + 1}"))
    if nav:
        keyboard.append(nav)

    keyboard.append([
        InlineKeyboardButton("➕ Add User", callback_data="au_add_start"),
        InlineKeyboardButton("❌ Close", callback_data="au_close"),
    ])

    text   = "\n".join(lines)
    markup = InlineKeyboardMarkup(keyboard)

    if edit and hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await target.reply_text(text, reply_markup=markup, parse_mode="Markdown")


async def _show_user_detail(target, context, telegram_id: int, edit: bool = True):
    u = await queries.get_authorized_user_with_role(telegram_id)
    if not u:
        text   = "⚠️ User not found."
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="au_back_list")]])
        if edit and hasattr(target, "edit_message_text"):
            await target.edit_message_text(text, reply_markup=markup)
        else:
            await target.reply_text(text, reply_markup=markup)
        return

    u = dict(u)
    role      = _role_badge(u, OWNER_ID)
    status    = "✅ Active" if u["is_active"] else "❌ Inactive"
    expiry    = format_utc(u.get("expires_at")) if u.get("expires_at") else "Permanent"
    name      = u.get("telegram_username") or u.get("display_name") or "N/A"
    is_owner  = (telegram_id == OWNER_ID)

    text = (
        f"👤 *User Detail*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 ID: `{telegram_id}`\n"
        f"🔖 Name: @{name}\n"
        f"🎭 Role: {role}\n"
        f"📊 Status: {status}\n"
        f"📅 Expiry: {expiry}\n"
        f"👛 Wallets: {u.get('wallet_count', 0)}\n"
        f"📆 Added: {format_utc(u.get('added_at'))}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )

    keyboard = []
    if not is_owner:
        # Role toggle
        if u.get("is_admin"):
            keyboard.append([InlineKeyboardButton("⬇️ Demote to User", callback_data=f"au_demote:{telegram_id}")])
        else:
            keyboard.append([InlineKeyboardButton("⬆️ Promote to Admin", callback_data=f"au_promote:{telegram_id}")])

        # Active toggle
        if u["is_active"]:
            keyboard.append([InlineKeyboardButton("🔒 Deactivate User", callback_data=f"au_deactivate:{telegram_id}")])
        else:
            keyboard.append([InlineKeyboardButton("🔓 Reactivate User", callback_data=f"au_reactivate:{telegram_id}")])

        # Delete
        keyboard.append([InlineKeyboardButton("🗑 Delete User (+ wallets & keys)", callback_data=f"au_del_ask:{telegram_id}")])

    keyboard.append([InlineKeyboardButton("⬅️ Back to List", callback_data="au_back_list")])

    markup = InlineKeyboardMarkup(keyboard)
    if edit and hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await target.reply_text(text, reply_markup=markup, parse_mode="Markdown")


# ── Entry: /admin ─────────────────────────────────────────────────────────────

@owner_only
async def cmd_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await _show_user_list(update.message, context, page=1, edit=False)
    return AU_LIST


# ── User list state ───────────────────────────────────────────────────────────

async def handle_au_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "au_close":
        await query.edit_message_text("✅ Admin panel closed.")
        context.user_data.clear()
        return ConversationHandler.END

    if data == "au_add_start":
        await query.edit_message_text(
            "➕ *Add New User*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Send the Telegram ID of the user to add:\n"
            "_(e.g. `123456789`)_\n\n"
            "Or type /cancel to abort.",
            parse_mode="Markdown",
        )
        return AU_ADD_ID

    if data == "au_back_list":
        await _show_user_list(query, context, page=1, edit=True)
        return AU_LIST

    if data.startswith("au_list:"):
        page = int(data.split(":")[1])
        await _show_user_list(query, context, page=page, edit=True)
        return AU_LIST

    if data.startswith("au_view:"):
        tid = int(data.split(":")[1])
        context.user_data["au_target_id"] = tid
        await _show_user_detail(query, context, tid)
        return AU_USER_VIEW

    return AU_LIST


# ── Per-user detail state ─────────────────────────────────────────────────────

async def handle_au_user_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "au_back_list":
        await _show_user_list(query, context, page=1, edit=True)
        return AU_LIST

    if data == "au_close":
        await query.edit_message_text("✅ Admin panel closed.")
        context.user_data.clear()
        return ConversationHandler.END

    if data.startswith("au_promote:"):
        tid = int(data.split(":")[1])
        if tid == OWNER_ID:
            await query.answer("⛔ Cannot change owner's role.", show_alert=True)
            return AU_USER_VIEW
        await queries.make_admin(tid)
        await query.answer("✅ Promoted to Admin.", show_alert=False)
        await _show_user_detail(query, context, tid)
        return AU_USER_VIEW

    if data.startswith("au_demote:"):
        tid = int(data.split(":")[1])
        if tid == OWNER_ID:
            await query.answer("⛔ Cannot change owner's role.", show_alert=True)
            return AU_USER_VIEW
        await queries.revoke_admin(tid)
        await query.answer("✅ Demoted to User.", show_alert=False)
        await _show_user_detail(query, context, tid)
        return AU_USER_VIEW

    if data.startswith("au_deactivate:"):
        tid = int(data.split(":")[1])
        if tid == OWNER_ID:
            await query.answer("⛔ Cannot deactivate the owner.", show_alert=True)
            return AU_USER_VIEW
        await queries.remove_authorized_user(tid)
        await query.answer("✅ User deactivated.", show_alert=False)
        await _show_user_detail(query, context, tid)
        return AU_USER_VIEW

    if data.startswith("au_reactivate:"):
        tid = int(data.split(":")[1])
        pool = await queries.get_pool()
        await pool.execute(
            "UPDATE authorized_users SET is_active = TRUE WHERE telegram_id = $1",
            tid,
        )
        await query.answer("✅ User reactivated.", show_alert=False)
        await _show_user_detail(query, context, tid)
        return AU_USER_VIEW

    if data.startswith("au_del_ask:"):
        tid = int(data.split(":")[1])
        if tid == OWNER_ID:
            await query.answer("⛔ Cannot delete the owner.", show_alert=True)
            return AU_USER_VIEW
        context.user_data["au_target_id"] = tid
        u = await queries.get_authorized_user_with_role(tid)
        name = (u.get("telegram_username") or u.get("display_name") or str(tid)) if u else str(tid)
        await query.edit_message_text(
            f"⚠️ *Confirm Delete User*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"You are about to *permanently delete*:\n"
            f"👤 @{name} (`{tid}`)\n\n"
            f"This will remove:\n"
            f"• Their access to the bot\n"
            f"• All their imported wallets\n"
            f"• All associated private keys\n\n"
            f"⛔ This action cannot be undone!",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑 Yes, Delete", callback_data=f"au_del_confirm:{tid}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"au_view:{tid}"),
            ]]),
            parse_mode="Markdown",
        )
        return AU_CONFIRM_DEL

    if data.startswith("au_view:"):
        tid = int(data.split(":")[1])
        context.user_data["au_target_id"] = tid
        await _show_user_detail(query, context, tid)
        return AU_USER_VIEW

    return AU_USER_VIEW


# ── Delete confirmation state ─────────────────────────────────────────────────

async def handle_au_confirm_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("au_del_confirm:"):
        tid = int(data.split(":")[1])
        if tid == OWNER_ID:
            await query.answer("⛔ Cannot delete the owner.", show_alert=True)
            await _show_user_list(query, context, page=1, edit=True)
            return AU_LIST

        # Hard delete — CASCADE removes wallets + encrypted keys
        pool = await queries.get_pool()
        await pool.execute("DELETE FROM authorized_users WHERE telegram_id = $1", tid)
        await queries.revoke_admin(tid)

        await query.edit_message_text(
            f"🗑 *User `{tid}` permanently deleted.*\n"
            f"All wallets and private keys have been wiped.",
            parse_mode="Markdown",
        )
        context.user_data.clear()
        await _show_user_list(
            await query.message.reply_text("↩️ Back to user list:"),
            context, page=1, edit=False,
        )
        return AU_LIST

    if data.startswith("au_view:"):
        tid = int(data.split(":")[1])
        await _show_user_detail(query, context, tid)
        return AU_USER_VIEW

    await _show_user_list(query, context, page=1, edit=True)
    return AU_LIST


# ── Add user: Telegram ID ─────────────────────────────────────────────────────

async def handle_au_add_id_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _owner_check_inline(update):
        return ConversationHandler.END

    text = update.message.text.strip()
    if text.lower() in ("/cancel", "cancel"):
        await update.message.reply_text("❌ Cancelled.")
        await _show_user_list(update.message, context, page=1, edit=False)
        return AU_LIST

    if not text.isdigit():
        await update.message.reply_text(
            "⚠️ Please send a valid Telegram ID (numbers only).\n"
            "Or type /cancel to abort."
        )
        return AU_ADD_ID

    context.user_data["au_new_id"] = int(text)
    await update.message.reply_text(
        f"✅ ID: `{text}`\n\n"
        "📝 Send a *username* for this user (without @).\n"
        "Or type `skip` to leave blank.",
        parse_mode="Markdown",
    )
    return AU_ADD_NAME


async def handle_au_add_name_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _owner_check_inline(update):
        return ConversationHandler.END

    text = update.message.text.strip()
    if text.lower() in ("/cancel", "cancel"):
        await update.message.reply_text("❌ Cancelled.")
        await _show_user_list(update.message, context, page=1, edit=False)
        return AU_LIST

    username = None if text.lower() == "skip" else text.lstrip("@")[:64]
    context.user_data["au_new_username"] = username

    await update.message.reply_text(
        "📅 How many *days* should access last?\n"
        "Send a number (e.g. `30`) or type `permanent` for no expiry.",
        parse_mode="Markdown",
    )
    return AU_ADD_DAYS


async def handle_au_add_days_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _owner_check_inline(update):
        return ConversationHandler.END

    text = update.message.text.strip()
    if text.lower() in ("/cancel", "cancel"):
        await update.message.reply_text("❌ Cancelled.")
        await _show_user_list(update.message, context, page=1, edit=False)
        return AU_LIST

    expires_at: Optional[datetime] = None
    if text.lower() not in ("permanent", "0", "none", "skip"):
        if not text.isdigit():
            await update.message.reply_text(
                "⚠️ Please enter a number of days, or type `permanent`.",
                parse_mode="Markdown",
            )
            return AU_ADD_DAYS
        expires_at = datetime.now(timezone.utc) + timedelta(days=int(text))

    new_id   = context.user_data["au_new_id"]
    username = context.user_data.get("au_new_username")

    try:
        await queries.add_authorized_user(
            telegram_id=new_id,
            telegram_username=username,
            display_name=username or str(new_id),
            added_by=OWNER_ID,
            expires_at=expires_at,
        )
        expiry_str = format_utc(expires_at) if expires_at else "Permanent"
        await update.message.reply_text(
            f"✅ *User Added*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 ID: `{new_id}`\n"
            f"🔖 Username: @{username or 'N/A'}\n"
            f"📅 Expiry: {expiry_str}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to add user: {e}")

    context.user_data.pop("au_new_id", None)
    context.user_data.pop("au_new_username", None)
    await _show_user_list(update.message, context, page=1, edit=False)
    return AU_LIST


# ── Cancel / fallback ─────────────────────────────────────────────────────────

async def au_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if update.message:
        await update.message.reply_text("❌ Admin panel closed.")
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("❌ Admin panel closed.")
    return ConversationHandler.END


# ── ConversationHandler factory ───────────────────────────────────────────────

def get_admin_ui_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("admin", cmd_admin_panel)],
        states={
            AU_LIST: [
                CallbackQueryHandler(
                    handle_au_list,
                    pattern=r"^(au_list:\d+|au_view:\d+|au_add_start|au_back_list|au_close)$",
                ),
            ],
            AU_USER_VIEW: [
                CallbackQueryHandler(
                    handle_au_user_view,
                    pattern=(
                        r"^(au_promote:\d+|au_demote:\d+|au_deactivate:\d+"
                        r"|au_reactivate:\d+|au_del_ask:\d+|au_view:\d+"
                        r"|au_back_list|au_close)$"
                    ),
                ),
            ],
            AU_CONFIRM_DEL: [
                CallbackQueryHandler(
                    handle_au_confirm_del,
                    pattern=r"^(au_del_confirm:\d+|au_view:\d+|au_back_list|au_close)$",
                ),
            ],
            AU_ADD_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_au_add_id_text),
                CommandHandler("cancel", au_cancel),
            ],
            AU_ADD_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_au_add_name_text),
                CommandHandler("cancel", au_cancel),
            ],
            AU_ADD_DAYS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_au_add_days_text),
                CommandHandler("cancel", au_cancel),
            ],
            ConversationHandler.TIMEOUT: [
                MessageHandler(filters.ALL, au_cancel),
            ],
        },
        fallbacks=[CommandHandler("cancel", au_cancel)],
        conversation_timeout=300,
    )
