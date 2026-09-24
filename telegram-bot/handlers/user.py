"""
handlers/user.py — User-facing Telegram commands.
All gated by @authorized decorator.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler

from db import queries
from middleware.auth import authorized
from utils.formatters import (
    format_wallet_line, format_balance_line, format_utc,
    format_history_row, format_tx_log_line,
    format_in_user_tz, format_dual_time,
    CHAIN_DISPLAY, CHAIN_EMOJI, CHAIN_EXPLORER,
    CHAIN_DEFAULT_RPCS,
    GAS_PRESETS, format_gas_preset_info,
    mask_address, POPULAR_TIMEZONES,
)
from engine.client import get_engine_client

logger = logging.getLogger(__name__)


# ── Mandatory group + authorization gate ─────────────────────────────────────

REQUIRED_CHAT = "@onemintchat"
REQUIRED_CHAT_URL = "https://t.me/onemintchat"


async def _is_group_member(context: ContextTypes.DEFAULT_TYPE, telegram_id: int) -> bool:
    """Return True when the user is currently a member of the required group."""
    try:
        member = await context.bot.get_chat_member(REQUIRED_CHAT, telegram_id)

        return member.status in {
            "member",
            "administrator",
            "creator",
        }

    except Exception as exc:
        logger.warning(
            "Could not verify group membership: telegram_id=%s error=%s",
            telegram_id,
            exc,
        )
        return False


async def _send_access_gate(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Show the appropriate group/authorization requirement."""

    user = update.effective_user
    if not user:
        return

    telegram_id = user.id
    username = f"@{user.username}" if user.username else "No username set"

    # First requirement: membership in the official group.
    is_member = await _is_group_member(context, telegram_id)

    if not is_member:
        keyboard = [
            [
                InlineKeyboardButton(
                    "📢 Join @onemintchat",
                    url=REQUIRED_CHAT_URL,
                )
            ],
            [
                InlineKeyboardButton(
                    "✅ I've Joined",
                    callback_data="verify_required_group",
                )
            ],
        ]

        await update.effective_message.reply_text(
            "🔐 *Join Required*\n\n"
            "Please join our official group before using this bot.\n\n"
            "After joining, press *I've Joined* so I can verify your membership.\n\n"
            "Once you are a member, you must also send your Telegram ID "
            "and username to the developer/admin in the group to request bot access.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return

    # Second requirement: existing developer/admin authorization.
    from middleware.auth import OWNER_ID

    if telegram_id != OWNER_ID:
        user_record = await queries.get_authorized_user(telegram_id)

        if not user_record:
            await queries.log_denied_access(
                telegram_id,
                user.username,
                "/start",
                reason="joined_group_but_not_authorized",
            )

            await update.effective_message.reply_text(
                "🔐 *Access Required*\n\n"
                "✅ You have joined the official group.\n\n"
                "Your Telegram account has not been authorized yet.\n\n"
                "Please send the following information to the developer/admin "
                "in the group:\n\n"
                f"🆔 Telegram ID: `{telegram_id}`\n"
                f"👤 Username: `{username}`\n\n"
                "After the developer/admin approves your account, press "
                "/start again.\n\n"
                "⚠️ Access is manually approved.",
                parse_mode="Markdown",
            )
            return

    # Both requirements passed.
    await _show_existing_start_message(update, context)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Public /start gate.

    User must:
      1. Be a member of @onemintchat
      2. Be authorized by the existing bot authorization system

    Only then is the normal bot welcome screen displayed.
    """
    await _send_access_gate(update, context)


async def handle_required_group_verification(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Handle the 'I've Joined' button."""

    query = update.callback_query
    user = update.effective_user

    if not query or not user:
        return

    await query.answer()

    telegram_id = user.id

    is_member = await _is_group_member(context, telegram_id)

    if not is_member:
        keyboard = [
            [
                InlineKeyboardButton(
                    "📢 Join @onemintchat",
                    url=REQUIRED_CHAT_URL,
                )
            ],
            [
                InlineKeyboardButton(
                    "✅ I've Joined",
                    callback_data="verify_required_group",
                )
            ],
        ]

        await query.edit_message_text(
            "❌ *Membership not detected yet.*\n\n"
            "Please join @onemintchat first, then press "
            "*I've Joined* again.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return

    from middleware.auth import OWNER_ID

    if telegram_id != OWNER_ID:
        user_record = await queries.get_authorized_user(telegram_id)

        if not user_record:
            username = f"@{user.username}" if user.username else "No username set"

            await query.edit_message_text(
                "✅ *Group membership verified!*\n\n"
                "🔐 *Bot access is still pending.*\n\n"
                "Please send the following to the developer/admin in the group:\n\n"
                f"🆔 Telegram ID: `{telegram_id}`\n"
                f"👤 Username: `{username}`\n\n"
                "After your account is authorized, press /start again.",
                parse_mode="Markdown",
            )
            return

    # Both checks passed.
    await query.edit_message_text(
        "✅ *Verification successful!*\n\n"
        "Your group membership and bot authorization are confirmed.",
        parse_mode="Markdown",
    )

    await _show_existing_start_message(update, context)


async def _show_existing_start_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Original /start welcome screen."""

    user = update.effective_user
    telegram_id = user.id

    wallets = await queries.get_user_wallets(telegram_id)
    wallet_count = len(wallets)
    chains = list({w["chain"] for w in wallets})
    chain_str = ", ".join(CHAIN_DISPLAY.get(c, c) for c in chains) if chains else "None"
    user_tz = await queries.get_user_timezone(telegram_id)

    await update.effective_message.reply_text(
        f"👋 Welcome, *{user.first_name}*!\n\n"
        f"🤖 *NFT Mint Bot* — Multi-Chain Auto Minter\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👛 Wallets: {wallet_count}\n"
        f"⛓ Chains: {chain_str}\n"
        f"🕐 Timezone: `{user_tz}`\n\n"
        f"*Quick Actions:*\n"
        f"🔐 /importwallet — Import a wallet\n"
        f"🚀 /mint — Start guided mint setup\n"
        f"📅 /schedule — View pending sessions\n"
        f"💰 /balance — Wallet balances\n"
        f"🕐 /timezone — Set your display timezone\n"
        f"📋 /help — All commands",
        parse_mode="Markdown",
    )


# ── /help ─────────────────────────────────────────────────────────────────────

@authorized
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *All Commands*\n\n"
        "🔐 *Wallet*\n"
        "/importwallet — Import wallet via private key\n"
        "/mywallets — List your wallets\n"
        "/removewallet — Remove a connected wallet\n"
        "/balance — Check wallet balances\n\n"
        "🚀 *Minting*\n"
        "/mint — Start full 20-step mint wizard\n"
        "/schedule — View pending/active sessions\n"
        "/cancelmint `<id>` — Cancel a session\n"
        "/stopbot `<id>` — Stop/pause a session\n\n"
        "📊 *Monitoring*\n"
        "/txhash `<id>` — TX hash + explorer link\n"
        "/logs `[id]` — Transaction logs\n"
        "/history `[page]` — Mint history\n"
        "/status — Bot & engine status\n\n"
        "⛽ *Gas*\n"
        "/gas — Show current gas presets\n\n"
        "🕐 *Preferences*\n"
        "/timezone — Set your display timezone\n\n"
        "/help — Show this message",
        parse_mode="Markdown",
    )


# ── /mywallets ────────────────────────────────────────────────────────────────

@authorized
async def cmd_mywallets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    wallets = await queries.get_user_wallets(telegram_id)

    if not wallets:
        await update.message.reply_text(
            "You have no wallets registered.\n"
            "Use /importwallet to add one."
        )
        return

    lines = ["👛 *Your Wallets*\n"]
    for w in wallets:
        lines.append(format_wallet_line(dict(w)))

    keyboard = [
        [
            InlineKeyboardButton("➕ Import Wallet", callback_data="none_import_hint"),
            InlineKeyboardButton("🗑 Remove a Wallet", callback_data="rw_menu"),
        ]
    ]

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


# ── /removewallet ─────────────────────────────────────────────────────────────

@authorized
async def cmd_removewallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    wallets = await queries.get_user_wallets(telegram_id)

    if not wallets:
        await update.message.reply_text(
            "⚠️ You have no wallets to remove.\n"
            "Use /importwallet to add one."
        )
        return

    keyboard = []
    for w in wallets:
        label = w.get("wallet_label") or "Unnamed"
        addr = mask_address(w["wallet_address"])
        keyboard.append([InlineKeyboardButton(
            f"🗑 {label} ({addr})",
            callback_data=f"rw_sel:{w['id']}",
        )])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="rw_cancel")])

    await update.message.reply_text(
        "🗑 *Remove a Wallet*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Select the wallet you want to remove from the bot:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def handle_remove_wallet_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    telegram_id = update.effective_user.id

    if data == "rw_cancel":
        await query.edit_message_text("❌ Wallet removal cancelled.")
        return

    if data == "none_import_hint":
        await query.answer("Use /importwallet to import a new wallet.", show_alert=True)
        return

    if data == "rw_menu":
        wallets = await queries.get_user_wallets(telegram_id)
        if not wallets:
            await query.edit_message_text("⚠️ You have no wallets to remove.")
            return

        keyboard = []
        for w in wallets:
            label = w.get("wallet_label") or "Unnamed"
            addr = mask_address(w["wallet_address"])
            keyboard.append([InlineKeyboardButton(
                f"🗑 {label} ({addr})",
                callback_data=f"rw_sel:{w['id']}",
            )])
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="rw_cancel")])

        await query.edit_message_text(
            "🗑 *Remove a Wallet*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Select the wallet you want to remove from the bot:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return

    if data.startswith("rw_sel:"):
        wid = int(data.split(":")[1])
        w = await queries.get_wallet_by_id(telegram_id, wid)
        if not w:
            await query.edit_message_text("⚠️ Wallet not found or already removed.")
            return

        label = w.get("wallet_label") or "Unnamed"
        addr = w["wallet_address"]

        keyboard = [
            [
                InlineKeyboardButton("🗑 Yes, Remove", callback_data=f"rw_confirm:{wid}"),
                InlineKeyboardButton("❌ Cancel", callback_data="rw_cancel"),
            ]
        ]
        await query.edit_message_text(
            "⚠️ *Confirm Wallet & Key Deletion*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🏷 Name: *{label}*\n"
            f"📍 Address: `{mask_address(addr)}`\n\n"
            "Are you sure? This will **permanently delete** this wallet and its private key from the bot.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return

    if data.startswith("rw_confirm:"):
        wid = int(data.split(":")[1])
        w = await queries.get_wallet_by_id(telegram_id, wid)
        if not w:
            await query.edit_message_text("⚠️ Wallet not found or already removed.")
            return

        label = w.get("wallet_label") or "Wallet"
        addr = mask_address(w["wallet_address"])
        success = await queries.remove_wallet_by_id(telegram_id, wid)
        if success:
            await query.edit_message_text(
                f"✅ *Wallet & Private Key Deleted*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"*{label}* (`{addr}`) and its private key have been permanently deleted from the bot.",
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text("❌ Failed to delete wallet. Please try again.")
        return


# ── /balance ──────────────────────────────────────────────────────────────────

@authorized
async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    wallets = await queries.get_user_wallets(telegram_id)

    if not wallets:
        await update.message.reply_text(
            "You have no wallets.\nUse /importwallet to add one."
        )
        return

    status_msg = await update.message.reply_text("⏳ Fetching balances across all chains…")

    engine = get_engine_client()

    NATIVE_TOKEN = {
        "ethereum": "ETH", "base": "ETH", "optimism": "ETH",
        "arbitrum": "ETH", "zora": "ETH", "shape": "ETH",
        "abstract": "ETH", "robinhood": "ETH",
        "polygon": "MATIC", "avalanche": "AVAX",
        "bsc": "BNB", "ape": "APE", "arc": "USDC",
        "hyperevm": "HYPE", "ink": "ETH",
    }

    # Helper to query a single chain balance with engine + direct RPC fallback
    async def _get_chain_balance(session: aiohttp.ClientSession, address: str, chain: str) -> Optional[float]:
        # 1. Try engine first
        try:
            bal = await engine.get_wallet_balance(address, chain)
            if bal is not None:
                return bal
        except Exception:
            pass

        # 2. Direct RPC fallback with standard User-Agent (bypasses Cloudflare / timeout)
        rpc_url = CHAIN_DEFAULT_RPCS.get(chain)
        if not rpc_url:
            return None
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_getBalance",
            "params": [address, "latest"],
            "id": 1,
        }
        try:
            async with session.post(rpc_url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if "result" in data and data["result"]:
                        return int(data["result"], 16) / 1e18
        except Exception:
            pass
        return None

    # Deduplicate wallets by address
    seen_addresses = set()
    unique_wallets = []
    for w in wallets:
        addr = w["wallet_address"].lower()
        if addr not in seen_addresses:
            seen_addresses.add(addr)
            unique_wallets.append(dict(w))

    all_chains = list(CHAIN_DEFAULT_RPCS.keys())

    async with aiohttp.ClientSession() as session:
        sections = []
        for w in unique_wallets:
            addr = w["wallet_address"]
            label = w.get("wallet_label") or "main"
            addr_short = f"`{addr[:6]}…{addr[-4:]}`"

            # Query all chains in parallel for this wallet
            chain_tasks = [_get_chain_balance(session, addr, c) for c in all_chains]
            balances = await asyncio.gather(*chain_tasks)

            non_zero = []
            zero_or_err = []

            for chain, bal in zip(all_chains, balances):
                emoji = CHAIN_EMOJI.get(chain, "🔗")
                chain_display = CHAIN_DISPLAY.get(chain, chain.capitalize())
                token = NATIVE_TOKEN.get(chain, "ETH")

                if bal is not None:
                    if bal > 0.00000001:
                        non_zero.append(f"{emoji} *{chain_display}*: `{bal:.6f} {token}`")
                    else:
                        zero_or_err.append(f"{emoji} {chain_display}: `0.0000 {token}`")
                else:
                    zero_or_err.append(f"{emoji} {chain_display}: `— (offline)`")

            wallet_text = [f"👛 *Wallet:* {addr_short} _({label})_\n"]
            if non_zero:
                wallet_text.append("✨ *Funded Chains:*")
                wallet_text.extend(f"  {line}" for line in non_zero)
                wallet_text.append("\n💤 *Other Chains:*")
                wallet_text.extend(f"  {line}" for line in zero_or_err)
            else:
                wallet_text.append("ℹ️ *All Chains:*")
                wallet_text.extend(f"  {line}" for line in zero_or_err)

            sections.append("\n".join(wallet_text))

    header = "💰 *Wallet Balances*\n━━━━━━━━━━━━━━━━━━━━\n"
    footer = "\n━━━━━━━━━━━━━━━━━━━━"
    final_text = header + "\n\n━━━━━━━━━━━━━━━━━━━━\n\n".join(sections) + footer

    try:
        await status_msg.edit_text(final_text, parse_mode="Markdown")
    except Exception:
        await update.message.reply_text(final_text, parse_mode="Markdown")


# ── /txhash ───────────────────────────────────────────────────────────────────

@authorized
async def cmd_txhash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    telegram_id = update.effective_user.id

    if not args or not args[0].isdigit():
        latest_id = await queries.get_latest_session_id(telegram_id)
        if not latest_id:
            await update.message.reply_text(
                "Usage: `/txhash <session_id>`\n"
                "You have no recent mint sessions.",
                parse_mode="Markdown",
            )
            return
        session_id = latest_id
    else:
        session_id = int(args[0])

    tx_rows = await queries.get_tx_hashes_for_session(session_id)
    if not tx_rows:
        await update.message.reply_text(
            f"📭 No transaction hashes found for session #{session_id}.\n"
            "The mint may not have started yet, or transactions are still broadcasting.\n"
            f"Check pending status with `/schedule` or `/logs {session_id}`.",
            parse_mode="Markdown",
        )
        return

    lines = [f"🔗 *TX Hashes — Session #{session_id}*\n"]
    for row in tx_rows:
        wallet = row["wallet_address"]
        tx = row["tx_hash"]
        status = row["status"]
        url = row.get("explorer_url") or ""
        block = row.get("block_number")

        status_emoji = {"confirmed": "✅", "sent": "📤", "failed": "❌"}.get(status, "⏳")
        tx_str = f"`{tx[:10]}...{tx[-6:]}`" if tx and len(tx) > 20 else (f"`{tx}`" if tx else "N/A")
        block_str = f" | Block #{block}" if block else ""
        line = f"{status_emoji} `{wallet[:6]}...{wallet[-4:]}` | {tx_str}{block_str}"
        if url:
            line += f"\n   🔍 [View on Explorer]({url})"
        lines.append(line)

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /logs ─────────────────────────────────────────────────────────────────────

@authorized
async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    telegram_id = update.effective_user.id

    if args and args[0].isdigit():
        session_id = int(args[0])
        logs = await queries.get_tx_logs(session_id, limit=30)
        if not logs:
            await update.message.reply_text(
                f"📭 No logs for session #{session_id} yet."
            )
            return
        lines = [f"📋 *Transaction Logs — Session #{session_id}*\n"]
        for log in logs:
            lines.append(format_tx_log_line(dict(log)))
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    else:
        # All recent logs
        logs = await queries.get_user_tx_logs(telegram_id, limit=20)
        if not logs:
            await update.message.reply_text(
                "📭 No transaction logs yet.\nUse /mint to schedule a mint."
            )
            return
        lines = ["📋 *Recent Transaction Logs*\n"]
        for log in logs:
            chain = log.get("chain", "")
            drop = log.get("drop_name") or "Mint"
            lines.append(f"_{CHAIN_EMOJI.get(chain, '🔗')} {drop} (Session #{log['session_id']}):_")
            lines.append(format_tx_log_line(dict(log)))
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /schedule ─────────────────────────────────────────────────────────────────

@authorized
async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    sessions = await queries.get_pending_sessions(telegram_id)

    if not sessions:
        await update.message.reply_text("📅 No pending or active mint sessions.")
        return

    lines = ["📅 *Scheduled Mints*\n"]
    for s in sessions:
        emoji  = CHAIN_EMOJI.get(s["chain"], "🔗")
        chain  = CHAIN_DISPLAY.get(s["chain"], s["chain"])
        drop   = s.get("drop_name") or s.get("drop_slug", "Unknown")
        fire   = format_utc(s.get("scheduled_at"))
        wcount = s.get("wallet_count", "?")
        status_emoji = {"pending": "⏳", "active": "🔄", "paused": "⏸"}.get(s["status"], "❓")

        lines.append(
            f"{status_emoji} #{s['id']} | {emoji} {drop}\n"
            f"   Chain: {chain} | Wallets: {wcount}\n"
            f"   Fires: {fire}\n"
            f"   /stopbot {s['id']} — /txhash {s['id']}"
        )

    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")


# ── /cancelmint ───────────────────────────────────────────────────────────────

@authorized
async def cmd_cancelmint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    telegram_id = update.effective_user.id
    if not args or not args[0].isdigit():
        sessions = await queries.get_pending_sessions(telegram_id)
        if not sessions:
            await update.message.reply_text("📅 No pending or active mint sessions to cancel.")
            return
        lines = ["Usage: `/cancelmint <session_id>`\n\n*Your Active / Pending Sessions:*"]
        for s in sessions:
            drop = s.get("drop_name") or s.get("nft_contract")
            lines.append(f"• Session `#{s['id']}`: {drop}\n  👉 `/cancelmint {s['id']}`")
        await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")
        return

    session_id  = int(args[0])
    success = await queries.cancel_mint_session(session_id, telegram_id)

    if success:
        await update.message.reply_text(f"✅ Session #{session_id} cancelled.")
    else:
        await update.message.reply_text(
            f"❌ Cannot cancel session #{session_id}.\n"
            "It may not exist, not be yours, or already completed."
        )


# ── /stopbot ──────────────────────────────────────────────────────────────────

@authorized
async def cmd_stopbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop / pause a running or pending session."""
    args = context.args or []
    telegram_id = update.effective_user.id
    if not args or not args[0].isdigit():
        sessions = await queries.get_pending_sessions(telegram_id)
        if not sessions:
            await update.message.reply_text("📅 No pending or active mint sessions to stop.")
            return
        lines = ["Usage: `/stopbot <session_id>`\n\n*Your Active / Pending Sessions:*"]
        for s in sessions:
            drop = s.get("drop_name") or s.get("nft_contract")
            lines.append(f"• Session `#{s['id']}`: {drop}\n  👉 `/stopbot {s['id']}`")
        await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")
        return

    session_id  = int(args[0])
    telegram_id = update.effective_user.id

    success = await queries.pause_mint_session(session_id, telegram_id)
    if success:
        # Notify engine to stop
        engine = get_engine_client()
        try:
            await engine.cancel_session(session_id)
        except Exception as e:
            logger.warning("Engine cancel_session failed: %s", e)

        await update.message.reply_text(
            f"⏸ Session #{session_id} has been *paused/stopped*.\n"
            f"Use /cancelmint {session_id} to fully cancel it.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"❌ Cannot stop session #{session_id}.\n"
            "It may not exist, not be yours, or already completed/cancelled."
        )


# ── /history ──────────────────────────────────────────────────────────────────

@authorized
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    page = int(args[0]) if args and args[0].isdigit() else 1
    limit  = 10
    offset = (page - 1) * limit

    telegram_id = update.effective_user.id
    sessions    = await queries.get_mint_history(telegram_id, offset=offset, limit=limit)
    total       = await queries.count_mint_history(telegram_id)
    total_pages = (total + limit - 1) // limit

    if not sessions:
        await update.message.reply_text("📖 No mint history yet.")
        return

    lines = [f"📖 *Mint History* (Page {page}/{total_pages})\n"]
    for s in sessions:
        lines.append(format_history_row(dict(s)))

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /status ───────────────────────────────────────────────────────────────────

@authorized
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine = get_engine_client()
    engine_ok = await engine.health_check()

    if not engine_ok:
        await update.message.reply_text("⚠️ Rust engine is not responding.")
        return

    try:
        status = await engine.get_status()
        await update.message.reply_text(
            f"📊 *Bot Status*\n"
            f"Engine: ✅ Running\n"
            f"Chains: {status.get('chain_count', '?')} connected\n"
            f"Active mints: {status.get('pending_sessions', 0)}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Engine error: {e}")


# ── /gas ──────────────────────────────────────────────────────────────────────

@authorized
async def cmd_gas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current gas presets."""
    lines = ["⛽ *Gas Presets*\n━━━━━━━━━━━━━━━━━━━━\n"]
    for name, preset in GAS_PRESETS.items():
        lines.append(
            f"{preset['label']}\n"
            f"  Base: ~{preset['base']} gwei\n"
            f"  Priority tip: ~{preset['priority']} gwei\n"
            f"  Multiplier: {preset['multiplier']}x\n"
        )
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("_Set during /mint setup (Step 11)._")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /timezone ─────────────────────────────────────────────────────────────────

@authorized
async def cmd_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show timezone picker UI with popular options + custom entry."""
    telegram_id = update.effective_user.id
    current_tz = await queries.get_user_timezone(telegram_id)
    await _send_timezone_picker(update.message, current_tz)


async def _send_timezone_picker(target, current_tz: str):
    """Send or edit the timezone picker inline keyboard."""
    now_utc = datetime.now(timezone.utc)
    # Build 2-column keyboard of popular timezones
    rows = []
    row = []
    for label, tz_id, offset in POPULAR_TIMEZONES:
        # Show local time preview next to each option
        try:
            import pytz
            local_now = now_utc.astimezone(pytz.timezone(tz_id)).strftime("%H:%M")
        except Exception:
            local_now = "??:??"
        selected = " ✅" if tz_id == current_tz else ""
        btn_label = f"{label} ({offset}) {local_now}{selected}"
        row.append(InlineKeyboardButton(btn_label, callback_data=f"tz_set:{tz_id}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✏️ Enter Custom IANA Timezone", callback_data="tz_custom")])
    rows.append([InlineKeyboardButton("❌ Close", callback_data="tz_close")])

    text = (
        f"🕐 *Timezone Preferences*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Current: `{current_tz}`\n\n"
        f"Select your timezone — the bot will display all\n"
        f"mint times (schedule, notifications, confirmations)\n"
        f"in your local time *AND UTC* simultaneously.\n\n"
        f"_Times shown as: `HH:MM:SS Local  /  HH:MM:SS UTC`_"
    )
    markup = InlineKeyboardMarkup(rows)
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await target.reply_text(text, reply_markup=markup, parse_mode="Markdown")


async def handle_timezone_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id

    if query.data == "tz_close":
        await query.edit_message_text("✅ Timezone settings closed.")
        return

    if query.data == "tz_custom":
        await query.edit_message_text(
            "✏️ *Enter your IANA timezone name:*\n"
            "Examples:\n"
            "`Asia/Kolkata` — IST (+5:30)\n"
            "`America/New_York` — EST/EDT\n"
            "`Europe/London` — GMT/BST\n"
            "`Asia/Singapore` — SGT (+8)\n"
            "`UTC` — Universal Time\n\n"
            "_Reply with your IANA timezone string:_",
            parse_mode="Markdown",
        )
        context.user_data["awaiting_tz_input"] = True
        return

    if query.data.startswith("tz_set:"):
        tz_id = query.data.split(":", 1)[1]
        try:
            import pytz
            pytz.timezone(tz_id)  # validate
        except Exception:
            await query.answer("❌ Invalid timezone.", show_alert=True)
            return
        await queries.set_user_timezone(telegram_id, tz_id)
        now_utc = datetime.now(timezone.utc)
        import pytz as _pytz
        local_now = now_utc.astimezone(_pytz.timezone(tz_id)).strftime("%H:%M:%S %Z")
        await query.answer(f"✅ Timezone set to {tz_id}", show_alert=False)
        # Refresh picker to show updated checkmark
        await _send_timezone_picker(query, tz_id)
        return


async def handle_tz_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle freeform IANA timezone text entry."""
    if not context.user_data.get("awaiting_tz_input"):
        return  # not our message
    context.user_data.pop("awaiting_tz_input", None)

    text = update.message.text.strip()
    telegram_id = update.effective_user.id

    try:
        import pytz
        pytz.timezone(text)  # validate
    except Exception:
        await update.message.reply_text(
            f"❌ `{text}` is not a valid IANA timezone.\n"
            "Examples: `Asia/Kolkata`, `America/New_York`, `Europe/London`\n"
            "Try /timezone again.",
            parse_mode="Markdown",
        )
        return

    await queries.set_user_timezone(telegram_id, text)
    now_utc = datetime.now(timezone.utc)
    import pytz as _pytz
    local_now = now_utc.astimezone(_pytz.timezone(text)).strftime("%H:%M:%S %Z")
    await update.message.reply_text(
        f"✅ *Timezone updated!*\n"
        f"🕐 Now using: `{text}`\n"
        f"Current local time: `{local_now}`\n\n"
        f"All mint times will now show as:\n"
        f"`HH:MM:SS {text.split('/')[-1]}  /  HH:MM:SS UTC`",
        parse_mode="Markdown",
    )


# ── Handler registry ──────────────────────────────────────────────────────────

def get_user_handlers():
    from telegram.ext import MessageHandler, filters
    return [
        CommandHandler("start",        cmd_start),
        CommandHandler("help",         cmd_help),
        CommandHandler("mywallets",    cmd_mywallets),
        CommandHandler("removewallet", cmd_removewallet),
        CommandHandler("balance",      cmd_balance),
        CommandHandler("txhash",       cmd_txhash),
        CommandHandler("logs",         cmd_logs),
        CommandHandler("schedule",     cmd_schedule),
        CommandHandler("cancelmint",   cmd_cancelmint),
        CommandHandler("stopbot",      cmd_stopbot),
        CommandHandler("history",      cmd_history),
        CommandHandler("status",       cmd_status),
        CommandHandler("gas",          cmd_gas),
        CommandHandler("timezone",     cmd_timezone),
        # Mandatory group verification
        CallbackQueryHandler(
            handle_required_group_verification,
            pattern=r"^verify_required_group$",
        ),

        # Inline callbacks
        CallbackQueryHandler(
            handle_remove_wallet_cb,
            pattern=r"^(rw_menu|rw_sel:\d+|rw_confirm:\d+|rw_cancel|none_import_hint)$",
        ),
        CallbackQueryHandler(
            handle_timezone_cb,
            pattern=r"^(tz_set:|tz_custom|tz_close)",
        ),
        # Free-text for timezone entry (must be last)
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_tz_text_input,
        ),
    ]
