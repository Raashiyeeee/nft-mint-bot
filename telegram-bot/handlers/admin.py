"""
handlers/admin.py — Admin-only Telegram commands.

Access levels:
  @owner_only   — Only Telegram ID 1386285936 (the bot owner)
  @admin_only   — Owner OR users in admin_users table

User management commands (/adduser, /removeuser, /makeadmin, /revokeadmin)
are owner-only so that only you decide who can use the bot.
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

from db import queries
from middleware.auth import admin_only, owner_only, OWNER_ID
from utils.crypto import encrypt_private_key, validate_private_key_format, validate_address_format, mask_address
from utils.formatters import format_utc, CHAIN_DISPLAY
from engine.client import get_engine_client

logger = logging.getLogger(__name__)


# ── /adduser — OWNER ONLY ─────────────────────────────────────────────────────

@owner_only
async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /adduser <telegram_id> <username> [days]
    Adds a user to the authorized list.
    Optional days = expiry. No days = permanent.
    """
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: `/adduser <telegram_id> <username> [days]`",
            parse_mode="Markdown"
        )
        return

    try:
        telegram_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid telegram_id — must be a number.")
        return

    username = args[1].lstrip("@")
    expires_at: Optional[datetime] = None

    if len(args) >= 3:
        try:
            days = int(args[2])
            expires_at = datetime.now(timezone.utc) + timedelta(days=days)
        except ValueError:
            await update.message.reply_text("❌ Invalid days value.")
            return

    await queries.add_authorized_user(
        telegram_id=telegram_id,
        telegram_username=username,
        display_name=username,
        added_by=update.effective_user.id,
        expires_at=expires_at,
    )

    expiry_str = f"Expires: {format_utc(expires_at)}" if expires_at else "Access: Permanent"
    await update.message.reply_text(
        f"✅ User added\n"
        f"ID: {telegram_id}\n"
        f"Username: @{username}\n"
        f"{expiry_str}"
    )
    logger.info("Owner %s added user %s", update.effective_user.id, telegram_id)


# ── /removeuser — OWNER ONLY ──────────────────────────────────────────────────

@owner_only
async def cmd_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /removeuser <telegram_id>
    Deactivates a user (sets is_active = FALSE).
    """
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: `/removeuser <telegram_id>`", parse_mode="Markdown")
        return

    try:
        telegram_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid telegram_id.")
        return

    if telegram_id == OWNER_ID:
        await update.message.reply_text("⛔ Cannot remove the bot owner.")
        return

    success = await queries.remove_authorized_user(telegram_id)
    if success:
        await update.message.reply_text(f"✅ User `{telegram_id}` deactivated.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ User `{telegram_id}` not found.", parse_mode="Markdown")


# ── /makeadmin — OWNER ONLY ───────────────────────────────────────────────────

@owner_only
async def cmd_makeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /makeadmin <telegram_id>
    Promotes an existing authorized user to admin.
    """
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: `/makeadmin <telegram_id>`", parse_mode="Markdown")
        return

    try:
        telegram_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid telegram_id.")
        return

    # User must be authorized first
    user_record = await queries.get_authorized_user(telegram_id)
    if not user_record:
        await update.message.reply_text(
            f"❌ User `{telegram_id}` is not in the authorized list.\n"
            f"Add them first with /adduser.",
            parse_mode="Markdown",
        )
        return

    await queries.make_admin(telegram_id)
    await update.message.reply_text(
        f"✅ User `{telegram_id}` is now an admin.", parse_mode="Markdown"
    )
    logger.info("Owner promoted %s to admin", telegram_id)


# ── /revokeadmin — OWNER ONLY ─────────────────────────────────────────────────

@owner_only
async def cmd_revokeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /revokeadmin <telegram_id>
    Removes a user from the admin_users table.
    """
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: `/revokeadmin <telegram_id>`", parse_mode="Markdown")
        return

    try:
        telegram_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid telegram_id.")
        return

    if telegram_id == OWNER_ID:
        await update.message.reply_text("⛔ Cannot revoke owner's admin status.")
        return

    success = await queries.revoke_admin(telegram_id)
    if success:
        await update.message.reply_text(
            f"✅ Admin rights revoked from `{telegram_id}`.", parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"❌ `{telegram_id}` was not an admin.", parse_mode="Markdown"
        )


# ── /listusers — ADMIN ────────────────────────────────────────────────────────

@admin_only
async def cmd_listusers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /listusers [page]
    Paginated table of all users.
    """
    args = context.args or []
    page  = int(args[0]) if args and args[0].isdigit() else 1
    limit = 10
    offset = (page - 1) * limit

    users       = await queries.list_authorized_users(offset=offset, limit=limit)
    total       = await queries.count_authorized_users()
    total_pages = (total + limit - 1) // limit

    if not users:
        await update.message.reply_text("No users found.")
        return

    lines = [f"👥 Authorized Users (Page {page}/{total_pages})\n"]
    for u in users:
        status = "✅" if u["is_active"] else "❌"
        expiry = format_utc(u["expires_at"]) if u["expires_at"] else "Permanent"
        wallets = u.get("wallet_count", 0)
        is_admin = "👑" if u["telegram_id"] == OWNER_ID else ""
        lines.append(
            f"{status}{is_admin} {u['telegram_id']} | @{u['telegram_username'] or 'N/A'} "
            f"| 👛 {wallets} | {expiry}"
        )

    await update.message.reply_text("\n".join(lines))


# ── /addwallet — ADMIN ────────────────────────────────────────────────────────

@admin_only
async def cmd_addwallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /addwallet <telegram_id> <chain> <wallet_address> <private_key> [label]
    Encrypts key and stores in DB.
    """
    args = context.args or []
    if len(args) < 4:
        await update.message.reply_text(
            "Usage: `/addwallet <telegram_id> <chain> <wallet_address> <private_key> [label]`",
            parse_mode="Markdown"
        )
        return

    try:
        telegram_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid telegram_id.")
        return

    chain          = args[1].lower()
    wallet_address = args[2].lower()
    private_key    = args[3]
    label          = " ".join(args[4:]) if len(args) > 4 else None

    # Validate chain
    if chain not in CHAIN_DISPLAY:
        valid = ", ".join(CHAIN_DISPLAY.keys())
        await update.message.reply_text(f"❌ Unknown chain `{chain}`. Valid: {valid}", parse_mode="Markdown")
        return

    # Validate address
    if not validate_address_format(wallet_address):
        await update.message.reply_text("❌ Invalid wallet address format.")
        return

    # Validate private key
    if not validate_private_key_format(private_key):
        await update.message.reply_text("❌ Invalid private key format (must be 32 bytes hex).")
        return

    # Encrypt
    try:
        encrypted = encrypt_private_key(private_key)
    except Exception as e:
        await update.message.reply_text(f"❌ Encryption failed: {e}")
        return

    # Store
    try:
        wallet_id = await queries.add_wallet(
            telegram_id=telegram_id,
            wallet_address=wallet_address,
            wallet_label=label,
            chain=chain,
            private_key_encrypted=encrypted,
        )
        await update.message.reply_text(
            f"✅ Wallet added\n"
            f"Chain: {CHAIN_DISPLAY.get(chain, chain)}\n"
            f"Address: `{mask_address(wallet_address)}`\n"
            f"Label: {label or 'None'}\n"
            f"Wallet ID: #{wallet_id}",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error("addwallet error: %s", e)
        await update.message.reply_text(f"❌ DB error: {e}")

    # Delete the command message to avoid exposing private key in chat history
    try:
        await update.message.delete()
    except Exception:
        pass


# ── /adminremovewallet — ADMIN ────────────────────────────────────────────────

@admin_only
async def cmd_admin_removewallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /adminremovewallet <telegram_id> <wallet_address>
    """
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text("Usage: `/adminremovewallet <telegram_id> <wallet_address>`", parse_mode="Markdown")
        return

    try:
        telegram_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid telegram_id.")
        return

    wallet_address = args[1].lower()
    success = await queries.remove_wallet(telegram_id, wallet_address)
    if success:
        await update.message.reply_text(f"✅ Wallet `{mask_address(wallet_address)}` deleted.", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Wallet not found or already deleted.")


# ── /broadcast — ADMIN ────────────────────────────────────────────────────────

@admin_only
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /broadcast <message>
    Sends message to all active authorized users.
    """
    if not context.args:
        await update.message.reply_text("Usage: `/broadcast <message>`", parse_mode="Markdown")
        return

    message = " ".join(context.args)
    telegram_ids = await queries.get_all_active_telegram_ids()

    sent   = 0
    failed = 0
    for tid in telegram_ids:
        try:
            await context.bot.send_message(
                chat_id=tid,
                text=f"📢 *Admin Broadcast*\n\n{message}",
                parse_mode="Markdown",
            )
            sent += 1
        except Exception as e:
            logger.warning("Broadcast failed for %s: %s", tid, e)
            failed += 1

    await update.message.reply_text(
        f"📢 Broadcast complete\n✅ Sent: {sent}\n❌ Failed: {failed}"
    )


# ── /botstatus — ADMIN ────────────────────────────────────────────────────────

@admin_only
async def cmd_botstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Shows engine status, pending sessions, chain connectivity.
    """
    engine = get_engine_client()

    engine_ok = await engine.health_check()
    if not engine_ok:
        await update.message.reply_text("⚠️ Rust engine is *not responding* on port 7878!", parse_mode="Markdown")
        return

    try:
        status = await engine.get_status()
        chains = await engine.ping_pipeline()

        chain_lines = []
        for name, info in chains.items():
            emoji = "✅" if info.get("ok") else "❌"
            chain_lines.append(f"  {emoji} {name}: `{info.get('rpc', 'N/A')[:40]}`")

        text = (
            f"🤖 *Bot Status*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Engine: {'✅ Running' if engine_ok else '❌ Down'}\n"
            f"Chains: {status.get('chain_count', '?')}\n"
            f"Active sessions: {status.get('pending_sessions', '?')}\n"
            f"Owner ID: `{OWNER_ID}`\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"*Chain RPC Status:*\n" +
            "\n".join(chain_lines)
        )
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Engine error: {e}")


# ── /ping — ADMIN smoke test ──────────────────────────────────────────────────

@admin_only
async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Hidden command: /ping
    Runs pipeline smoke test via Rust engine.
    """
    await update.message.reply_text("🔍 Running pipeline smoke test...")
    engine = get_engine_client()
    try:
        results  = await engine.ping_pipeline()
        ok_count = sum(1 for v in results.values() if v.get("ok"))
        total    = len(results)
        await update.message.reply_text(
            f"✅ Ping complete: {ok_count}/{total} chains responding."
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ping failed: {e}")


# ── /adminhelp — ADMIN ────────────────────────────────────────────────────────

@admin_only
async def cmd_adminhelp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show admin command list."""
    from middleware.auth import OWNER_ID as OID
    is_owner = (update.effective_user.id == OID)
    owner_cmds = (
        "\n👑 *Owner-Only Commands*\n"
        "/adduser `<id>` `<username>` `[days]` — Add authorized user\n"
        "/removeuser `<id>` — Remove a user\n"
        "/makeadmin `<id>` — Promote user to admin\n"
        "/revokeadmin `<id>` — Revoke admin rights\n"
    ) if is_owner else ""

    await update.message.reply_text(
        "🔧 *Admin Commands*\n\n"
        "/listusers `[page]` — List all users\n"
        "/addwallet `<id>` `<chain>` `<addr>` `<key>` `[label]` — Add wallet\n"
        "/adminremovewallet `<id>` `<addr>` — Remove wallet\n"
        "/broadcast `<msg>` — Message all users\n"
        "/botstatus — Engine & chain status\n"
        "/ping — Smoke test\n"
        f"{owner_cmds}",
        parse_mode="Markdown",
    )


# ── Handler registry ──────────────────────────────────────────────────────────

def get_admin_handlers():
    return [
        # Owner-only
        CommandHandler("adduser",           cmd_adduser),
        CommandHandler("removeuser",        cmd_removeuser),
        CommandHandler("makeadmin",         cmd_makeadmin),
        CommandHandler("revokeadmin",       cmd_revokeadmin),
        # Admin-level
        CommandHandler("listusers",         cmd_listusers),
        CommandHandler("addwallet",         cmd_addwallet),
        CommandHandler("adminremovewallet", cmd_admin_removewallet),
        CommandHandler("broadcast",         cmd_broadcast),
        CommandHandler("botstatus",         cmd_botstatus),
        CommandHandler("ping",              cmd_ping),
        CommandHandler("adminhelp",         cmd_adminhelp),
    ]
