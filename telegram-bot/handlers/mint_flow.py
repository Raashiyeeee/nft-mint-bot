"""
handlers/mint_flow.py — Guided ConversationHandler for /mint (19 steps).

Mint Configuration Flow:
  1.  Network / Chain selection
  2.  Wallet selection
  3.  RPC URL (custom or default)
  4.  Chain ID (auto or custom)
  5.  Contract Address
  6.  Mint Phase (FCFS / Public)
  7.  Mint Function signature
  8.  Mint Price (ETH or chain currency)
  9.  Mint Quantity per wallet
  10. Gas Limit (auto or custom)
  11. Gas Fee / Priority Fee (presets or custom)
  12. Slippage (presets or custom)
  13. Mint Start Time (datetime or "now")
  14. Auto-Mint ON/OFF
  15. Auto Gas / Gas Boost ON/OFF + boost %
  16. Transaction Retry ON/OFF
  17. Max Retries (if retry is ON)
  18. Notifications ON/OFF (success + fail together)
  19. Final Review & Confirm

Every step after #1 includes a ⬅️ Back button to re-enter the previous choice.
"""
import logging
import re
import aiohttp
from datetime import datetime, timezone, timedelta
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
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
from engine.client import get_engine_client
from utils.formatters import (
    mask_address,
    format_utc,
    format_utc_precise,
    format_ist,
    format_ist_date,
    format_in_user_tz,
    format_in_user_tz_date,
    format_dual_time,
    CHAIN_DISPLAY,
    CHAIN_EMOJI,
    CHAIN_DEFAULT_IDS,
    CHAIN_DEFAULT_RPCS,
    CHAIN_EXPLORER,
    GAS_PRESETS,
    MINT_FUNCTION_PRESETS,
    MINT_FUNCTION_LABELS,
    gas_label,
    get_chain_currency,
)
from utils.contract_inspector import inspect_contract_phases
from utils.balance_check import (
    validate_selected_wallets_funds,
    format_insufficient_funds,
)

logger = logging.getLogger(__name__)

# ── Conversation States ───────────────────────────────────────────────────────
(
    STATE_CHAIN,          # Step 1/19
    STATE_WALLET,         # Step 2/19
    STATE_RPC,            # Step 3/19
    STATE_CHAIN_ID,       # Step 4/19
    STATE_CONTRACT,       # Step 5/19
    STATE_PHASE,          # Step 6/19 — Select Mint Phase (FCFS, Public)
    STATE_MINT_FN,        # Step 7/19 — Mint Function (tailored to phase)
    STATE_MINT_PRICE,     # Step 8/19 — Mint Price
    STATE_QUANTITY,       # Step 9/19 — Mint Quantity
    STATE_GAS_LIMIT,      # Step 10/19
    STATE_GAS_FEE,        # Step 11/19
    STATE_SLIPPAGE,       # Step 12/19
    STATE_MINT_TIME,      # Step 13/19 — Mint Start Time
    STATE_AUTO_MINT,      # Step 14/19
    STATE_AUTO_GAS,       # Step 15/19
    STATE_RETRIES,        # Step 16/19
    STATE_MAX_RETRIES,    # Step 17/19
    STATE_NOTIFY,         # Step 18/19 — Success+Fail notifications (merged)
    STATE_REVIEW,         # Step 19/19
) = range(1, 20)

SUPPORTED_CHAINS = [
    ("base",      "Base",      "🔵"),
    ("ethereum",  "Ethereum",  "🔷"),
    ("polygon",   "Polygon",   "🟣"),
    ("arbitrum",  "Arbitrum",  "🔶"),
    ("optimism",  "Optimism",  "🔴"),
    ("zora",      "Zora",      "⚪"),
    ("robinhood", "Robinhood", "🏹"),
    ("avalanche", "Avalanche", "🔺"),
    ("bsc",       "BSC",       "🟡"),
    ("shape",     "Shape",     "🔷"),
    ("abstract",  "Abstract",  "⬜"),
    ("ape",       "ApeChain",  "🐵"),
    ("arc",       "Arc",       "🌐"),
    ("hyperevm",  "HyperEVM",  "💧"),
    ("ink",       "Ink",       "🖋️"),
]

ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="mint_cancel")]])


def _nav_row(back_to: str | None = None):
    """Cancel row, optionally with a Back button to a previous step key."""
    row = []
    if back_to:
        row.append(InlineKeyboardButton("⬅️ Back", callback_data=f"mint_back:{back_to}"))
    row.append(InlineKeyboardButton("❌ Cancel", callback_data="mint_cancel"))
    return row


def _back_cancel_keyboard(back_to: str):
    return InlineKeyboardMarkup([_nav_row(back_to)])


# ── Step 1: /mint — Network / Chain selection ──────────────────────────────────

async def _render_chain_selector(target):
    keyboard = []
    row = []
    for chain_id, name, emoji in SUPPORTED_CHAINS:
        row.append(InlineKeyboardButton(f"{emoji} {name}", callback_data=f"mc_chain:{chain_id}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append(_nav_row())  # cancel only on step 1

    text = (
        "🚀 *Setup New NFT Mint*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*Step 1/19 — Select Network / Chain*\n\n"
        "Choose the blockchain for this mint:"
    )
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return STATE_CHAIN


@authorized
async def start_mint_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    context.user_data.clear()
    context.user_data["_telegram_id"] = user_id

    wallets = await queries.get_user_wallets(user_id)
    if not wallets:
        await update.message.reply_text(
            "⚠️ You have no wallets registered.\n\n"
            "Use /importwallet to import a wallet first.",
        )
        return ConversationHandler.END

    context.user_data["all_wallets"] = {w["id"]: dict(w) for w in wallets}
    return await _render_chain_selector(update.message)


async def handle_chain_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "mint_cancel":
        await query.edit_message_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    chain = query.data.split(":")[1]
    context.user_data["mint_chain"] = chain

    context.user_data["mint_selected_wallet_ids"] = []  # default: unselected

    return await _render_wallet_selector(query, context)


# ── Back navigation (any step → previous) ─────────────────────────────────────

async def handle_mint_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route ⬅️ Back presses to the previous step's prompt."""
    query = update.callback_query
    await query.answer()

    if not query.data.startswith("mint_back:"):
        return ConversationHandler.END

    dest = query.data.split(":", 1)[1]

    if dest == "chain":
        return await _render_chain_selector(query)
    if dest == "wallet":
        return await _render_wallet_selector(query, context)
    if dest == "rpc":
        return await _prompt_rpc(query, context)
    if dest == "chain_id":
        return await _prompt_chain_id(query, context)
    if dest == "contract":
        return await _prompt_contract(query)
    if dest == "phase":
        return await _render_phase_selector(query, context)
    if dest == "mint_fn":
        return await _prompt_mint_fn(query, context)
    if dest == "mint_price":
        return await _prompt_mint_price(query, context)
    if dest == "quantity":
        return await _prompt_quantity(query, context)
    if dest == "gas_limit":
        return await _prompt_gas_limit(query)
    if dest == "gas_fee":
        return await _prompt_gas_fee(query)
    if dest == "slippage":
        return await _prompt_slippage(query)
    if dest == "mint_time":
        return await _prompt_mint_time(query, context)
    if dest == "auto_mint":
        return await _prompt_auto_mint(query)
    if dest == "auto_gas":
        return await _prompt_auto_gas(query, context)
    if dest == "retries":
        return await _prompt_retries(query)
    if dest == "max_retries":
        if context.user_data.get("mint_tx_retry"):
            return await _prompt_max_retries(query)
        return await _prompt_retries(query)
    if dest == "notify":
        return await _prompt_notify(query)

    await query.edit_message_text("⚠️ Cannot go back from here. Use /mint to restart.")
    return ConversationHandler.END


# ── Step 2: Wallet selection ──────────────────────────────────────────────────

async def _render_wallet_selector(target, context: ContextTypes.DEFAULT_TYPE):
    all_wallets = context.user_data.get("all_wallets", {})
    selected = set(context.user_data.get("mint_selected_wallet_ids", []))
    chain = context.user_data.get("mint_chain", "")
    emoji = CHAIN_EMOJI.get(chain, "🔗")
    chain_name = CHAIN_DISPLAY.get(chain, chain)

    keyboard = []
    for wid, w in all_wallets.items():
        label = w.get("wallet_label") or mask_address(w["wallet_address"])
        icon = "✅" if wid in selected else "⬜"
        keyboard.append([InlineKeyboardButton(
            f"{icon} {label}",
            callback_data=f"mw_select:{wid}",
        )])

    all_selected = (len(selected) == len(all_wallets) and len(all_wallets) > 0)
    toggle_label = "⬜ Deselect All" if all_selected else "☑️ Select All Wallets"
    keyboard.append([InlineKeyboardButton(toggle_label, callback_data="mw_all")])
    keyboard.append([
        InlineKeyboardButton(f"➡️ Continue ({len(selected)} selected)", callback_data="mw_continue"),
    ])
    keyboard.append(_nav_row("chain"))

    text = (
        "🚀 *Setup New NFT Mint*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*Step 2/19 — Select Wallet(s)*\n\n"
        f"⛓ Network: {emoji} *{chain_name}*\n\n"
        "Tap a wallet to toggle selection:\n"
        f"Selected: *{len(selected)}* of {len(all_wallets)} wallets"
    )
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
    else:
        await target.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
    return STATE_WALLET


async def handle_wallet_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "mint_cancel":
        await query.edit_message_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    all_wallets = context.user_data.get("all_wallets", {})
    selected = set(context.user_data.get("mint_selected_wallet_ids", []))

    if data == "mw_all":
        if len(selected) == len(all_wallets) and len(all_wallets) > 0:
            selected = set()
        else:
            selected = set(all_wallets.keys())
    elif data.startswith("mw_select:"):
        wid = int(data.split(":")[1])
        if wid in selected:
            selected.discard(wid)
        else:
            selected.add(wid)
    elif data == "mw_continue":
        if not selected:
            await query.answer("⚠️ Select at least 1 wallet!", show_alert=True)
            return STATE_WALLET
        context.user_data["mint_selected_wallet_ids"] = list(selected)
        return await _prompt_rpc(query, context)

    context.user_data["mint_selected_wallet_ids"] = list(selected)
    return await _render_wallet_selector(query, context)


# ── Step 3: RPC URL ───────────────────────────────────────────────────────────

async def _prompt_rpc(target, context: ContextTypes.DEFAULT_TYPE):
    chain = context.user_data.get("mint_chain", "")
    emoji = CHAIN_EMOJI.get(chain, "🔗")
    chain_name = CHAIN_DISPLAY.get(chain, chain)
    default_rpc = CHAIN_DEFAULT_RPCS.get(chain, "")

    keyboard = [
        [InlineKeyboardButton(f"✅ Use Default ({default_rpc[:35]}…)", callback_data="mc_rpc_default")],
        [InlineKeyboardButton("✏️ Enter Custom RPC", callback_data="mc_rpc_custom")],
        _nav_row("wallet"),
    ]
    text = (
        f"⛓ Chain: {emoji} *{chain_name}*\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*Step 3/19 — RPC URL*\n\n"
        "Use the default public RPC or enter your own:"
    )
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return STATE_RPC


# ── Step 3: RPC URL ───────────────────────────────────────────────────────────

async def handle_rpc_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles inline button choice for RPC (default or custom)."""
    query = update.callback_query
    await query.answer()

    if query.data == "mint_cancel":
        await query.edit_message_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    chain = context.user_data.get("mint_chain", "")

    if query.data == "mc_rpc_default":
        context.user_data["mint_rpc"] = CHAIN_DEFAULT_RPCS.get(chain, "")
        return await _prompt_chain_id(query, context)

    if query.data == "mc_rpc_custom":
        await query.edit_message_text(
            "✏️ *Enter Custom RPC URL*\n"
            "Examples:\n"
            "• `https://mainnet.infura.io/v3/YOUR_KEY`\n"
            "• `https://eth-mainnet.g.alchemy.com/v2/YOUR_KEY`\n\n"
            "_Paste your RPC URL:_",
            parse_mode="Markdown",
        )
        return STATE_RPC


async def handle_rpc_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles text input for custom RPC URL."""
    if not await check_authorized_inline(update):
        return ConversationHandler.END

    text = update.message.text.strip()
    if text.startswith("/cancel"):
        await update.message.reply_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    if not (text.startswith("http://") or text.startswith("https://")):
        await update.message.reply_text(
            "⚠️ RPC URL must start with `http://` or `https://`. Try again:",
            parse_mode="Markdown",
        )
        return STATE_RPC

    context.user_data["mint_rpc"] = text
    return await _prompt_chain_id(update.message, context)


# ── Step 4: Chain ID ──────────────────────────────────────────────────────────

async def _prompt_chain_id(target, context: ContextTypes.DEFAULT_TYPE):
    chain = context.user_data.get("mint_chain", "")
    default_id = CHAIN_DEFAULT_IDS.get(chain, 1)

    keyboard = [
        [InlineKeyboardButton(f"✅ Use Default ({default_id})", callback_data="mc_cid_default")],
        [InlineKeyboardButton("✏️ Enter Custom Chain ID", callback_data="mc_cid_custom")],
        _nav_row("rpc"),
    ]
    await target.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*Step 4/19 — Chain ID*\n\n"
        f"Default for selected chain: *{default_id}*\n"
        "Use default or enter a custom Chain ID:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    ) if hasattr(target, "reply_text") else await target.edit_message_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*Step 4/19 — Chain ID*\n\n"
        f"Default for selected chain: *{default_id}*\n"
        "Use default or enter a custom Chain ID:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )
    return STATE_CHAIN_ID


async def handle_chain_id_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "mint_cancel":
        await query.edit_message_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    chain = context.user_data.get("mint_chain", "")

    if query.data == "mc_cid_default":
        context.user_data["mint_chain_id"] = CHAIN_DEFAULT_IDS.get(chain, 1)
        return await _prompt_contract(query)

    if query.data == "mc_cid_custom":
        await query.edit_message_text(
            "✏️ *Enter Chain ID* (integer, e.g. 1 for Ethereum, 8453 for Base):",
            parse_mode="Markdown",
        )
        return STATE_CHAIN_ID


async def handle_chain_id_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_inline(update):
        return ConversationHandler.END

    text = update.message.text.strip()
    if text.startswith("/cancel"):
        await update.message.reply_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    try:
        chain_id = int(text)
        if chain_id <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Invalid chain ID. Enter a positive integer:")
        return STATE_CHAIN_ID

    context.user_data["mint_chain_id"] = chain_id
    return await _prompt_contract(update.message)


# ── Step 5: Contract Address ──────────────────────────────────────────────────

async def _prompt_contract(target):
    text = (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*Step 5/19 — Contract Address*\n\n"
        "Enter the NFT contract address:\n"
        "Example: `0xABCDEF1234...`\n\n"
        "_Use ⬅️ Back to change Chain ID, or /cancel to abort_"
    )
    kb = InlineKeyboardMarkup([_nav_row("chain_id")])
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await target.reply_text(text, reply_markup=kb, parse_mode="Markdown")
    return STATE_CONTRACT


async def handle_contract(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_inline(update):
        return ConversationHandler.END

    text = update.message.text.strip()
    if text.startswith("/cancel"):
        await update.message.reply_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    if not ETH_ADDRESS_RE.match(text):
        await update.message.reply_text(
            "⚠️ Invalid contract address. Must be a 42-character hex string starting with `0x`.\n"
            "Try again:",
            parse_mode="Markdown",
        )
        return STATE_CONTRACT

    context.user_data["mint_contract"] = text.lower()

    status_msg = await update.message.reply_text(
        "🔍 *Fetching mint phases from OpenSea & on-chain...*",
        parse_mode="Markdown",
    )

    chain = context.user_data.get("mint_chain", "ethereum")
    rpc_url = context.user_data.get("mint_rpc") or CHAIN_DEFAULT_RPCS.get(chain, "")
    all_wallets = context.user_data.get("all_wallets", {})
    selected_wids = context.user_data.get("mint_selected_wallet_ids", [])
    wallet_addrs = [w["wallet_address"] for wid, w in all_wallets.items() if wid in selected_wids]

    # Pass chain so the inspector can call the OpenSea API for accurate per-stage times
    info = await inspect_contract_phases(rpc_url, text.lower(), wallet_addrs, chain=chain)
    context.user_data["contract_info"] = info
    context.user_data["mint_is_seadrop"] = bool(info.get("is_seadrop"))

    return await _render_phase_selector(status_msg, context)


# ── Step 6: Select Mint Phase ─────────────────────────────────────────────────

async def _render_phase_selector(target, context: ContextTypes.DEFAULT_TYPE):
    info = context.user_data.get("contract_info", {})
    all_phases = info.get("phases", [])
    contract = context.user_data.get("mint_contract", "")
    chain = context.user_data.get("mint_chain", "ethereum")
    currency = get_chain_currency(chain)

    proto_name = "OpenSea SeaDrop" if info.get("is_seadrop") else "Direct EVM Contract"
    now_utc = datetime.now(timezone.utc)

    # ── Bot focuses on FCFS and Public only ──────────────────────────────────
    # All other stages (Treasury, OG, GTD, etc.) are filtered out of the UI.
    SHOWN_PHASE_IDS = ("fcfs", "public")
    phases = [p for p in all_phases if p.get("phase_id") in SHOWN_PHASE_IDS]

    lines = [
        "🚀 *Setup New NFT Mint*",
        "━━━━━━━━━━━━━━━━━━━━",
        "*Step 6/19 — Select Mint Phase*\n",
        f"📜 Contract: `{mask_address(contract)}`",
        f"🏷 Name: *{info.get('name', 'NFT')}* {info.get('symbol', '')}",
        f"⛓ Protocol: *{proto_name}*\n",
        "Select the drop phase to mint in:",
    ]

    keyboard = []
    for p in phases:
        p_name  = p.get("phase_name", "Mint Phase")
        p_id    = p.get("phase_id", "public")
        p_price = p.get("price_eth", "0")
        start_time = p.get("start_time")

        if start_time:
            # Compute UTC display + countdown
            time_utc = start_time.astimezone(timezone.utc).strftime("%H:%M:%S UTC")
            if start_time > now_utc:
                diff_secs = int((start_time - now_utc).total_seconds())
                mins, secs = divmod(diff_secs, 60)
                hours, mins = divmod(mins, 60)
                countdown = f"{hours}h {mins}m {secs}s" if hours else f"{mins}m {secs}s"
                time_display = f"{time_utc}  ⏳ opens in {countdown}"
            else:
                time_display = f"{time_utc}  🟢 LIVE"
        else:
            time_display = "Not fetched — set in Step 13"

        # Phase emoji prefix
        if p_id == "fcfs":
            prefix = "⚡"
        else:
            prefix = "🌐"

        try:
            p_price_f = float(p_price)
        except (TypeError, ValueError):
            p_price_f = None
        price_label = "Free" if p_price_f is not None and p_price_f <= 0 else f"{p_price} {currency}"

        lines.append(
            f"\n• {prefix} *{p_name}*\n"
            f"  💰 Price: *{price_label}*\n"
            f"  ⏰ Start: `{time_display}`\n"
            f"  🎯 Max: *{p.get('max_per_wallet', 1)}* | Status: *{p.get('eligibility_note', 'Eligible')}*"
        )

        keyboard.append([
            InlineKeyboardButton(
                f"{prefix} {p_name} ({price_label})",
                callback_data=f"mc_phase:{p_id}",
            )
        ])

    if not phases:
        lines.append("\n⚠️ No FCFS/Public phases detected. Use Custom to set manually.")

    keyboard.append([InlineKeyboardButton("✏️ Custom / Skip Phase Setup", callback_data="mc_phase_skip")])
    keyboard.append(_nav_row("contract"))

    text = "\n".join(lines)
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return STATE_PHASE


async def handle_phase_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "mint_cancel":
        await query.edit_message_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    if data == "mc_phase_skip":
        return await _prompt_mint_fn(query, context)

    phase_id = data.split(":", 1)[1]
    info = context.user_data.get("contract_info", {})
    phases = info.get("phases", [])
    selected_phase = next((p for p in phases if p.get("phase_id") == phase_id), None)

    if selected_phase:
        context.user_data["mint_phase"] = selected_phase.get("phase_id")
        context.user_data["mint_phase_name"] = selected_phase.get("phase_name")
        context.user_data["mint_fn"] = selected_phase.get("mint_function")
        context.user_data["mint_price_wei"] = selected_phase.get("price_wei", 0)
        context.user_data["mint_price_eth"] = selected_phase.get("price_eth", "0")
        context.user_data["mint_phase_start_time"] = selected_phase.get("start_time")
        context.user_data["mint_scheduled_at"] = selected_phase.get("start_time")
        context.user_data["mint_phase_max_per_wallet"] = selected_phase.get("max_per_wallet", 100)

    return await _prompt_mint_fn(query, context)


# ── Step 7: Mint Function ─────────────────────────────────────────────────────

async def _prompt_mint_fn(target, context: ContextTypes.DEFAULT_TYPE):
    detected_fn = context.user_data.get("mint_fn")
    phase_id = context.user_data.get("mint_phase")
    phase_name = context.user_data.get("mint_phase_name") or "selected phase"
    preset_buttons = []

    if detected_fn:
        fn_name = detected_fn.split("(")[0] if "(" in detected_fn else detected_fn
        default_label = {
            "fcfs": "✅ Use FCFS Default: mintSigned(…)",
            "public": "✅ Use Public Default: mintPublic(…)",
        }.get(phase_id, f"✅ Use Phase Function: {fn_name}(…)")
        preset_buttons.append([
            InlineKeyboardButton(default_label, callback_data="mc_fn_phase")
        ])

    # Prefer phase-matching SeaDrop presets first; hide the opposite signed/public default
    preferred = []
    others = []
    for idx, fn in enumerate(MINT_FUNCTION_PRESETS):
        if fn == detected_fn:
            continue
        label = MINT_FUNCTION_LABELS.get(fn, fn.split("(")[0] + "(…)" if len(fn) > 40 else fn)
        btn = InlineKeyboardButton(label, callback_data=f"mc_fnp:{idx}")
        is_signed = "mintSigned" in fn
        is_public_sd = fn.startswith("mintPublic(address")
        if phase_id == "fcfs" and is_signed:
            preferred.append(btn)
        elif phase_id == "public" and is_public_sd:
            preferred.append(btn)
        elif phase_id == "fcfs" and is_public_sd:
            continue  # don't offer public SeaDrop fn as primary alt for FCFS
        elif phase_id == "public" and is_signed:
            continue  # don't offer FCFS signed fn as primary alt for public
        else:
            others.append(btn)

    for btn in preferred:
        preset_buttons.append([btn])
    row = []
    for btn in others:
        row.append(btn)
        if len(row) == 2:
            preset_buttons.append(row)
            row = []
    if row:
        preset_buttons.append(row)

    preset_buttons.append([InlineKeyboardButton("✏️ Enter Custom Function", callback_data="mc_fn_custom")])
    preset_buttons.append(_nav_row("phase"))

    fn_display = f"\n🎯 Default for {phase_name}:\n`{detected_fn}`\n" if detected_fn else ""
    text = (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*Step 7/19 — Mint Function*\n\n"
        f"{fn_display}"
        "Select the phase default or another mint function:"
    )
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=InlineKeyboardMarkup(preset_buttons), parse_mode="Markdown")
    else:
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(preset_buttons), parse_mode="Markdown")
    return STATE_MINT_FN


async def handle_mint_fn_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "mint_cancel":
        await query.edit_message_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    if query.data == "mc_fn_custom":
        await query.edit_message_text(
            "✏️ *Enter Mint Function Signature*\n"
            "Examples:\n"
            "• `mint(uint256)`\n"
            "• `publicMint(uint256,address)`\n"
            "• `mintNFT(uint256)`",
            parse_mode="Markdown",
        )
        return STATE_MINT_FN

    if query.data == "mc_fn_phase":
        # context.user_data["mint_fn"] was already saved during phase choice
        return await _prompt_mint_price(query, context)

    if query.data.startswith("mc_fnp:"):
        idx = int(query.data.split(":", 1)[1])
        if 0 <= idx < len(MINT_FUNCTION_PRESETS):
            context.user_data["mint_fn"] = MINT_FUNCTION_PRESETS[idx]
        return await _prompt_mint_price(query, context)

    if query.data.startswith("mc_fn:"):
        fn = query.data.split(":", 1)[1]
        context.user_data["mint_fn"] = fn
        return await _prompt_mint_price(query, context)

    return await _prompt_mint_price(query, context)


async def handle_mint_fn_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_inline(update):
        return ConversationHandler.END

    text = update.message.text.strip()
    if text.startswith("/cancel"):
        await update.message.reply_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    if "(" not in text or ")" not in text:
        await update.message.reply_text(
            "⚠️ Invalid function signature. Must contain parentheses, e.g. `mint(uint256)`. Try again:",
            parse_mode="Markdown",
        )
        return STATE_MINT_FN

    context.user_data["mint_fn"] = text.strip()
    return await _prompt_mint_price(update.message, context)


# ── Funds gate (price / quantity) ─────────────────────────────────────────────

async def _edit_or_reply(target, text: str, reply_markup=None, parse_mode="Markdown"):
    kwargs = {"reply_markup": reply_markup, "parse_mode": parse_mode}
    if hasattr(target, "edit_message_text"):
        try:
            await target.edit_message_text(text, **kwargs)
            return
        except Exception:
            pass
    if hasattr(target, "reply_text"):
        await target.reply_text(text, **kwargs)
    elif hasattr(target, "message") and target.message:
        await target.message.reply_text(text, **kwargs)


async def _show_insufficient_funds(target, context: ContextTypes.DEFAULT_TYPE, check: dict, stage: str):
    """Block wizard progress and ask user to top up + recheck."""
    context.user_data["_funds_gate_stage"] = stage
    back_to = "mint_fn" if stage == "price" else "mint_price"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Recheck Balances", callback_data=f"mc_funds_recheck:{stage}")],
        _nav_row(back_to),
    ])
    await _edit_or_reply(target, format_insufficient_funds(check), reply_markup=keyboard)


async def _gate_selected_wallets(target, context: ContextTypes.DEFAULT_TYPE, *, price_eth: float, qty: int, stage: str):
    """
    Verify every selected wallet can cover mint cost + estimated gas.
    Returns (ok: bool, stay_state: int).
    """
    currency = get_chain_currency(context.user_data.get("mint_chain", "ethereum"))
    await _edit_or_reply(
        target,
        f"⏳ Checking selected wallet balances on "
        f"*{CHAIN_DISPLAY.get(context.user_data.get('mint_chain', ''), 'chain')}* "
        f"({currency})…\n"
        f"_Mint + estimated gas_",
    )

    try:
        check = await validate_selected_wallets_funds(context.user_data, price_eth, qty)
    except Exception as e:
        logger.exception("Funds check failed: %s", e)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Retry Check", callback_data=f"mc_funds_recheck:{stage}")],
            _nav_row("mint_fn" if stage == "price" else "mint_price"),
        ])
        await _edit_or_reply(
            target,
            "⚠️ Could not verify wallet balances. Check your RPC and try again.",
            reply_markup=keyboard,
        )
        return False, STATE_MINT_PRICE if stage == "price" else STATE_QUANTITY

    if not check["ok"]:
        await _show_insufficient_funds(target, context, check, stage)
        return False, STATE_MINT_PRICE if stage == "price" else STATE_QUANTITY

    return True, None


async def handle_funds_recheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🔄 Recheck after user tops up (from price or quantity gate)."""
    query = update.callback_query
    await query.answer()

    if query.data == "mint_cancel":
        await query.edit_message_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    stage = query.data.split(":")[1] if ":" in query.data else context.user_data.get("_funds_gate_stage", "price")
    price_eth = float(context.user_data.get("mint_price_eth") or 0)
    qty = int(context.user_data.get("mint_qty") or 1)

    if stage == "qty":
        ok, stay = await _gate_selected_wallets(
            query, context, price_eth=price_eth, qty=qty, stage="qty",
        )
        if not ok:
            return stay
        return await _prompt_gas_limit(query)

    # Default: re-validate at price stage (qty unknown → assume 1)
    ok, stay = await _gate_selected_wallets(
        query, context, price_eth=price_eth, qty=1, stage="price",
    )
    if not ok:
        return stay
    return await _prompt_quantity(query, context)


# ── Step 8: Mint Price ────────────────────────────────────────────────────────

async def _prompt_mint_price(target, context: ContextTypes.DEFAULT_TYPE):
    chain = context.user_data.get("mint_chain", "ethereum")
    currency = get_chain_currency(chain)
    raw_phase_price = context.user_data.get("mint_price_eth")
    phase_price_eth = None
    if raw_phase_price is not None:
        try:
            phase_price_eth = float(raw_phase_price)
        except (TypeError, ValueError):
            phase_price_eth = None

    keyboard = []
    if phase_price_eth is not None:
        if phase_price_eth <= 0:
            phase_btn = InlineKeyboardButton(
                f"✅ Free mint (0 {currency})",
                callback_data="mc_price:0",
            )
        else:
            phase_btn = InlineKeyboardButton(
                f"✅ Use Phase Price ({phase_price_eth} {currency})",
                callback_data=f"mc_price:{phase_price_eth}",
            )
        keyboard.append([phase_btn])

    keyboard.extend([
        [
            InlineKeyboardButton("🎁 Free (0)", callback_data="mc_price:0"),
            InlineKeyboardButton(f"0.001 {currency}", callback_data="mc_price:0.001"),
            InlineKeyboardButton(f"0.01 {currency}", callback_data="mc_price:0.01"),
        ],
        [
            InlineKeyboardButton(f"0.05 {currency}", callback_data="mc_price:0.05"),
            InlineKeyboardButton(f"0.1 {currency}",  callback_data="mc_price:0.1"),
            InlineKeyboardButton("✏️ Custom", callback_data="mc_price_custom"),
        ],
        _nav_row("mint_fn"),
    ])

    text = (
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"*Step 8/19 — Mint Price ({currency})*\n\n"
        f"Enter the price per NFT in {currency}:\n"
        f"• `0` / Free = free mint (gas still required in {currency})\n"
        f"• `0.05` = 0.05 {currency} per NFT\n\n"
        f"_After you confirm, every selected wallet is checked for mint + {currency} gas "
        f"on this chain._\n"
        "_or /cancel_"
    )
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return STATE_MINT_PRICE


async def handle_mint_price_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "mint_cancel":
        await query.edit_message_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    if query.data.startswith("mc_funds_recheck:"):
        return await handle_funds_recheck(update, context)

    if query.data == "mc_price_custom":
        chain = context.user_data.get("mint_chain", "ethereum")
        currency = get_chain_currency(chain)
        await query.edit_message_text(
            f"✏️ Enter mint price in {currency} (e.g. `0.08`):",
            parse_mode="Markdown",
        )
        return STATE_MINT_PRICE

    price_eth = float(query.data.split(":")[1])
    context.user_data["mint_price_wei"] = int(price_eth * 10**18)
    context.user_data["mint_price_eth"] = price_eth

    # Gate: every selected wallet must cover price×1 + estimated gas
    ok, stay = await _gate_selected_wallets(
        query, context, price_eth=price_eth, qty=1, stage="price",
    )
    if not ok:
        return stay
    return await _prompt_quantity(query, context)


async def handle_mint_price_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_inline(update):
        return ConversationHandler.END

    text = update.message.text.strip()
    if text.startswith("/cancel"):
        await update.message.reply_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    chain = context.user_data.get("mint_chain", "ethereum")
    currency = get_chain_currency(chain)

    try:
        price_eth = float(text)
        if price_eth < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(f"⚠️ Invalid price. Enter a non-negative number in {currency} (e.g. `0.05`):")
        return STATE_MINT_PRICE

    context.user_data["mint_price_wei"] = int(price_eth * 10**18)
    context.user_data["mint_price_eth"] = price_eth

    ok, stay = await _gate_selected_wallets(
        update.message, context, price_eth=price_eth, qty=1, stage="price",
    )
    if not ok:
        return stay
    return await _prompt_quantity(update.message, context)


# ── Step 9: Quantity per Wallet ───────────────────────────────────────────────

_QTY_PRESETS = (1, 2, 3, 4, 5, 10, 20)


def _quantity_options(phase_max: Optional[int]) -> list[int]:
    """Build qty buttons from the CA phase max (when known)."""
    if isinstance(phase_max, int) and phase_max > 0:
        if phase_max <= 5:
            # e.g. max 5 → 1, 2, 3, 4, 5
            return list(range(1, phase_max + 1))
        if phase_max <= 9:
            # e.g. max 8 → 1..8
            return list(range(1, phase_max + 1))
        # High cap (e.g. 100) → standard presets that fit under the limit
        return [n for n in _QTY_PRESETS if n <= phase_max]
    return list(_QTY_PRESETS)


async def _prompt_quantity(target, context: ContextTypes.DEFAULT_TYPE = None):
    phase_max = context.user_data.get("mint_phase_max_per_wallet") if context else None
    if phase_max is not None:
        try:
            phase_max = int(phase_max)
        except (TypeError, ValueError):
            phase_max = None
    if phase_max is not None and phase_max <= 0:
        phase_max = None

    if phase_max:
        max_hint = (
            f"\n_(Fetched from CA: *{phase_max}* max per wallet — "
            "SeaDrop counts free + paid toward the same wallet cap)_"
        )
    else:
        max_hint = "\n_(Phase max unknown — enter the amount you want)_"

    options = _quantity_options(phase_max)
    keyboard = []
    row = []
    for n in options:
        row.append(InlineKeyboardButton(str(n), callback_data=f"mc_qty:{n}"))
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    if phase_max:
        keyboard.append([
            InlineKeyboardButton(
                f"🎯 Maximum quantity ({phase_max})",
                callback_data="mc_qty_max",
            )
        ])

    keyboard.append([InlineKeyboardButton("✏️ Enter Custom Quantity", callback_data="mc_qty_custom")])
    keyboard.append(_nav_row("mint_price"))

    text = (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*Step 9/19 — Mint Quantity per Wallet*\n\n"
        f"How many NFTs to mint per wallet?{max_hint}"
    )
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return STATE_QUANTITY


async def handle_quantity_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "mint_cancel":
        await query.edit_message_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    if query.data.startswith("mc_funds_recheck:"):
        return await handle_funds_recheck(update, context)

    phase_max = context.user_data.get("mint_phase_max_per_wallet")
    try:
        phase_max = int(phase_max) if phase_max is not None else None
    except (TypeError, ValueError):
        phase_max = None
    if phase_max is not None and phase_max <= 0:
        phase_max = None

    if query.data == "mc_qty_custom":
        upper = phase_max if phase_max else 100
        await query.edit_message_text(
            "✏️ *Enter quantity per wallet*\n\n"
            f"Type a number from *1* to *{upper}* and send it:",
            parse_mode="Markdown",
        )
        return STATE_QUANTITY

    if query.data == "mc_qty_max":
        if not phase_max:
            await query.answer("⚠️ Max per wallet unknown for this CA.", show_alert=True)
            return STATE_QUANTITY
        qty = phase_max
    else:
        qty = int(query.data.split(":")[1])
        if phase_max and qty > phase_max:
            await query.answer(
                f"⚠️ This CA allows max {phase_max} per wallet.",
                show_alert=True,
            )
            return STATE_QUANTITY

    context.user_data["mint_qty"] = qty
    context.user_data["mint_max_per_wallet"] = phase_max or qty

    price_eth = float(context.user_data.get("mint_price_eth") or 0)
    ok, stay = await _gate_selected_wallets(
        query, context, price_eth=price_eth, qty=qty, stage="qty",
    )
    if not ok:
        return stay
    return await _prompt_gas_limit(query)


async def handle_quantity_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_inline(update):
        return ConversationHandler.END

    text = update.message.text.strip()
    if text.startswith("/cancel"):
        await update.message.reply_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    phase_max = context.user_data.get("mint_phase_max_per_wallet")
    try:
        phase_max = int(phase_max) if phase_max is not None else None
    except (TypeError, ValueError):
        phase_max = None
    if phase_max is not None and phase_max <= 0:
        phase_max = None
    upper = phase_max if phase_max else 100

    try:
        qty = int(text)
        if qty < 1 or qty > upper:
            raise ValueError
    except ValueError:
        await update.message.reply_text(f"⚠️ Enter a number between 1 and {upper}:")
        return STATE_QUANTITY

    context.user_data["mint_qty"] = qty
    context.user_data["mint_max_per_wallet"] = phase_max or qty

    price_eth = float(context.user_data.get("mint_price_eth") or 0)
    ok, stay = await _gate_selected_wallets(
        update.message, context, price_eth=price_eth, qty=qty, stage="qty",
    )
    if not ok:
        return stay
    return await _prompt_gas_limit(update.message)


# ── Step 10: Gas Limit ────────────────────────────────────────────────────────

async def _prompt_gas_limit(target):
    keyboard = [
        [InlineKeyboardButton("🤖 Auto (recommended)", callback_data="mc_gl_auto")],
        [
            InlineKeyboardButton("100k",  callback_data="mc_gl:100000"),
            InlineKeyboardButton("200k",  callback_data="mc_gl:200000"),
            InlineKeyboardButton("300k",  callback_data="mc_gl:300000"),
            InlineKeyboardButton("500k",  callback_data="mc_gl:500000"),
        ],
        [InlineKeyboardButton("✏️ Custom", callback_data="mc_gl_custom")],
        _nav_row("quantity"),
    ]
    text = (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*Step 10/19 — Gas Limit*\n\n"
        "Set the gas limit for each transaction.\n"
        "_Auto will simulate and estimate before sending._"
    )
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return STATE_GAS_LIMIT


async def handle_gas_limit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "mint_cancel":
        await query.edit_message_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    if query.data == "mc_gl_auto":
        is_sd = context.user_data.get("mint_is_seadrop") or (context.user_data.get("contract_info") or {}).get("is_seadrop")
        context.user_data["mint_gas_limit"] = 180000 if is_sd else 200000
        return await _prompt_gas_fee(query)

    if query.data == "mc_gl_custom":
        await query.edit_message_text("✏️ Enter gas limit (e.g. `250000`):")
        return STATE_GAS_LIMIT

    gas_limit = int(query.data.split(":")[1])
    context.user_data["mint_gas_limit"] = gas_limit
    return await _prompt_gas_fee(query)


async def handle_gas_limit_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_inline(update):
        return ConversationHandler.END

    text = update.message.text.strip()
    if text.startswith("/cancel"):
        await update.message.reply_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    try:
        gl = int(text)
        if gl < 21000:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Enter a valid gas limit (minimum 21000):")
        return STATE_GAS_LIMIT

    context.user_data["mint_gas_limit"] = gl
    return await _prompt_gas_fee(update.message)


# ── Step 11: Gas Fee / Priority Fee ──────────────────────────────────────────

async def _prompt_gas_fee(target):
    keyboard = []
    for preset_name, preset in GAS_PRESETS.items():
        keyboard.append([InlineKeyboardButton(
            f"{preset['label']}  ({preset['base']}+{preset['priority']} gwei)",
            callback_data=f"mc_gas:{preset_name}",
        )])
    keyboard.append([InlineKeyboardButton("✏️ Custom Base+Priority", callback_data="mc_gas_custom")])
    keyboard.append(_nav_row("gas_limit"))

    text = (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*Step 11/19 — Gas Fee / Priority Fee*\n\n"
        "Select a gas preset or enter custom values:\n"
        "_(Format: base_fee + priority_fee in gwei)_"
    )
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return STATE_GAS_FEE


async def handle_gas_fee_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "mint_cancel":
        await query.edit_message_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    if query.data == "mc_gas_custom":
        await query.edit_message_text(
            "✏️ *Enter custom gas fees (gwei)*\n"
            "Format: `base_fee priority_fee`\n"
            "Example: `30 5` means 30 gwei base + 5 gwei priority tip",
            parse_mode="Markdown",
        )
        return STATE_GAS_FEE

    preset_name = query.data.split(":")[1]
    preset = GAS_PRESETS.get(preset_name, GAS_PRESETS["fast"])
    context.user_data["mint_gas_preset"] = preset_name
    context.user_data["mint_base_fee"] = preset["base"]
    context.user_data["mint_priority_fee"] = preset["priority"]
    context.user_data["mint_gas_multiplier"] = preset["multiplier"]
    return await _prompt_slippage(query)


async def handle_gas_fee_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_inline(update):
        return ConversationHandler.END

    text = update.message.text.strip()
    if text.startswith("/cancel"):
        await update.message.reply_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    parts = text.split()
    if len(parts) != 2:
        await update.message.reply_text(
            "⚠️ Enter two numbers separated by space: `base_fee priority_fee`\nExample: `30 5`",
            parse_mode="Markdown",
        )
        return STATE_GAS_FEE

    try:
        base_fee = float(parts[0])
        priority = float(parts[1])
        if base_fee < 0 or priority < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Both values must be non-negative numbers. Try again:")
        return STATE_GAS_FEE

    context.user_data["mint_gas_preset"] = "custom"
    context.user_data["mint_base_fee"] = base_fee
    context.user_data["mint_priority_fee"] = priority
    context.user_data["mint_gas_multiplier"] = 1.0
    return await _prompt_slippage(update.message)


# ── Step 12: Slippage ─────────────────────────────────────────────────────────

async def _prompt_slippage(target):
    keyboard = [
        [
            InlineKeyboardButton("0.5%",  callback_data="mc_slip:50"),
            InlineKeyboardButton("1%",    callback_data="mc_slip:100"),
            InlineKeyboardButton("2%",    callback_data="mc_slip:200"),
            InlineKeyboardButton("5%",    callback_data="mc_slip:500"),
        ],
        [InlineKeyboardButton("✏️ Custom", callback_data="mc_slip_custom")],
        _nav_row("gas_fee"),
    ]
    text = (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*Step 12/19 — Slippage Tolerance*\n\n"
        "Maximum acceptable price deviation from the set mint price:"
    )
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return STATE_SLIPPAGE


async def handle_slippage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "mint_cancel":
        await query.edit_message_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    if query.data == "mc_slip_custom":
        await query.edit_message_text(
            "✏️ Enter slippage as a percentage (e.g. `1.5` for 1.5%):",
            parse_mode="Markdown",
        )
        return STATE_SLIPPAGE

    bps = int(query.data.split(":")[1])
    context.user_data["mint_slippage_bps"] = bps
    return await _prompt_mint_time(query, context)


async def handle_slippage_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_inline(update):
        return ConversationHandler.END

    text = update.message.text.strip()
    if text.startswith("/cancel"):
        await update.message.reply_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    try:
        pct = float(text.rstrip("%"))
        if pct < 0 or pct > 50:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Enter a percentage between 0 and 50 (e.g. `2.5`):")
        return STATE_SLIPPAGE

    context.user_data["mint_slippage_bps"] = int(pct * 100)
    return await _prompt_mint_time(update.message, context)


# ── Step 13: Mint Start Time ──────────────────────────────────────────────────

async def query_seadrop_onchain_start_time(rpc_url: str, contract_address: str) -> Optional[datetime]:
    """Query SeaDrop getPublicDrop on-chain to find the exact start time."""
    if not rpc_url or not contract_address or not contract_address.startswith("0x"):
        return None
    clean = contract_address[2:].lower().rjust(64, '0')
    data = "0xbc6a629c" + clean
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [
            {"to": "0x00005ea00ac477b1030ce78506496e8c2de24bf5", "data": data},
            "latest"
        ],
        "id": 1,
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(rpc_url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                if resp.status == 200:
                    res = await resp.json()
                    res_str = res.get("result", "")
                    if len(res_str) >= 130:
                        start_time_hex = res_str[66:130]
                        start_time = int(start_time_hex, 16)
                        if start_time > 0:
                            return datetime.fromtimestamp(start_time, tz=timezone.utc)
    except Exception as e:
        logger.debug("Failed to query on-chain SeaDrop start time: %s", e)
    return None


async def _prompt_mint_time(target, context=None):
    contract = context.user_data.get("mint_contract", "") if context else ""
    chain = context.user_data.get("mint_chain", "") if context else ""
    rpc_url = (context.user_data.get("mint_rpc") or CHAIN_DEFAULT_RPCS.get(chain, "")) if context else ""

    # Load user's preferred timezone from the DB
    user_id = context._user_id if hasattr(context, "_user_id") else None
    # Try to get telegram_id from context
    try:
        from db import queries as _q
        # context.user_data is keyed by user id in PTB
        # We get it from bot_data we cached at phase selection
        tg_id = context.user_data.get("_telegram_id")
        user_tz = await _q.get_user_timezone(tg_id) if tg_id else "UTC"
    except Exception:
        user_tz = "UTC"

    onchain_dt = None
    if rpc_url and contract:
        onchain_dt = await query_seadrop_onchain_start_time(rpc_url, contract)

    keyboard = []
    now = datetime.now(timezone.utc)
    detected_text = ""

    phase_dt = context.user_data.get("mint_phase_start_time") if context else None
    phase_name = context.user_data.get("mint_phase_name") if context else None

    if phase_dt:
        utc_time_str = format_utc_precise(phase_dt)
        epoch_ts     = phase_dt.timestamp()
        if phase_dt > now:
            diff_secs = int((phase_dt - now).total_seconds())
            mins, secs = divmod(diff_secs, 60)
            hours, mins = divmod(mins, 60)
            countdown = f"{hours}h {mins}m {secs}s" if hours else f"{mins}m {secs}s"
            detected_text = (
                f"🎯 *{phase_name or 'Phase'} Start Time Detected!*\n"
                f"🕐 `{utc_time_str}`\n"
                f"⏳ Opens in: `{countdown}`\n\n"
            )
            keyboard.append([
                InlineKeyboardButton(
                    f"🎯 Use {phase_name or 'Phase'} Time: {utc_time_str}",
                    callback_data=f"mc_time_onchain:{epoch_ts}"
                )
            ])
        else:
            detected_text = (
                f"🟢 *{phase_name or 'Phase'} is already LIVE!*\n"
                f"Started at: `{utc_time_str}`\n"
                f"👉 Recommended: *⚡ Mint Now (Immediately)*\n\n"
            )
    elif onchain_dt:
        utc_time_str = format_utc_precise(onchain_dt)
        epoch_ts     = onchain_dt.timestamp()
        if onchain_dt > now:
            diff_secs = int((onchain_dt - now).total_seconds())
            mins, secs = divmod(diff_secs, 60)
            hours, mins = divmod(mins, 60)
            countdown = f"{hours}h {mins}m {secs}s" if hours else f"{mins}m {secs}s"
            detected_text = (
                f"🎯 *On-Chain Start Time Detected!*\n"
                f"🕐 `{utc_time_str}`\n"
                f"⏳ Opens in: `{countdown}`\n\n"
            )
            keyboard.append([
                InlineKeyboardButton(
                    f"🎯 Auto-Align: {utc_time_str}",
                    callback_data=f"mc_time_onchain:{epoch_ts}"
                )
            ])
        else:
            detected_text = (
                f"🟢 *Drop is already LIVE on-chain!*\n"
                f"Started at: `{utc_time_str}`\n"
                f"👉 Recommended: *⚡ Mint Now (Immediately)*\n\n"
            )

    keyboard.extend([
        [InlineKeyboardButton("⚡ Mint Now (Immediately)", callback_data="mc_time_now")],
        [InlineKeyboardButton("⏰ +5 min",  callback_data="mc_time_rel:5")],
        [InlineKeyboardButton("⏰ +15 min", callback_data="mc_time_rel:15")],
        [InlineKeyboardButton("⏰ +1 hour", callback_data="mc_time_rel:60")],
        [InlineKeyboardButton("✏️ Custom DateTime (UTC)", callback_data="mc_time_custom")],
        _nav_row("slippage"),
    ])
    text = (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*Step 13/19 — Mint Start Time*\n\n"
        f"{detected_text}"
        "All times are in UTC 🌐"
    )
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return STATE_MINT_TIME


async def handle_mint_time_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "mint_cancel":
        await query.edit_message_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    if query.data.startswith("mc_time_onchain:"):
        # Epoch stored as float string to preserve millisecond precision
        epoch_ts = float(query.data.split(":", 1)[1])
        fire_at = datetime.fromtimestamp(epoch_ts, tz=timezone.utc)
        context.user_data["mint_scheduled_at"] = fire_at
        tg_id = context.user_data.get("_telegram_id")
        try:
            from db import queries as _q
            user_tz = await _q.get_user_timezone(tg_id) if tg_id else "UTC"
        except Exception:
            user_tz = "UTC"
        dual_str = format_dual_time(fire_at, user_tz)
        await query.answer(f"✅ Scheduled: {dual_str}", show_alert=False)
        return await _prompt_auto_mint(query)

    if query.data == "mc_time_now":
        context.user_data["mint_scheduled_at"] = None
        return await _prompt_auto_mint(query)

    if query.data.startswith("mc_time_rel:"):
        minutes = int(query.data.split(":")[1])
        fire_at = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        context.user_data["mint_scheduled_at"] = fire_at
        return await _prompt_auto_mint(query)

    if query.data == "mc_time_custom":
        await query.edit_message_text(
            "✏️ *Enter mint start time in UTC (seconds required!)*\n"
            "Format: `YYYY-MM-DD HH:MM:SS`\n"
            "Example: `2026-09-16 12:30:41`\n\n"
            "_Include seconds for millisecond-precision scheduling_ ⚡",
            parse_mode="Markdown",
        )
        context.user_data["mint_time_input_mode"] = "utc"
        return STATE_MINT_TIME

    if query.data == "mc_time_custom_ist":
        tg_id = context.user_data.get("_telegram_id")
        try:
            from db import queries as _q
            user_tz = await _q.get_user_timezone(tg_id) if tg_id else "UTC"
        except Exception:
            user_tz = "UTC"
        tz_label = user_tz.split("/")[-1] if "/" in user_tz else user_tz
        await query.edit_message_text(
            f"✏️ *Enter mint start time in your timezone ({tz_label})*\n"
            "Format: `YYYY-MM-DD HH:MM:SS`\n"
            "Example: `2026-09-16 18:00:41`\n\n"
            "_Bot will convert to UTC automatically_ 🕐",
            parse_mode="Markdown",
        )
        context.user_data["mint_time_input_mode"] = "local"
        return STATE_MINT_TIME

    if query.data == "mc_time_tz_hint":
        await query.answer(
            "🕐 Use /timezone to change your display timezone.\nCurrent mint flow is not interrupted.",
            show_alert=True,
        )
        return STATE_MINT_TIME


async def handle_mint_time_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_inline(update):
        return ConversationHandler.END

    text = update.message.text.strip()
    if text.startswith("/cancel"):
        await update.message.reply_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    input_mode = context.user_data.pop("mint_time_input_mode", "utc")

    # Accept both with-seconds and without-seconds formats
    formats_utc = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"]
    dt = None

    # Accept both full datetime and time-only (today in UTC)
    formats_utc = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
    ]
    dt = None

    # First try full datetime formats
    for fmt in formats_utc:
        try:
            dt = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            continue

    # Also support time-only input (e.g. "12:30:05" or "12:30") using current UTC date
    if not dt:
        for t_fmt in ["%H:%M:%S", "%H:%M"]:
            try:
                parsed_time = datetime.strptime(text, t_fmt).time()
                today_utc = datetime.now(timezone.utc).date()
                dt = datetime.combine(today_utc, parsed_time, tzinfo=timezone.utc)
                break
            except ValueError:
                continue

    if not dt:
        mode_label = "UTC" if input_mode == "utc" else "your local timezone"
        await update.message.reply_text(
            f"⚠️ Invalid datetime. Use format: `YYYY-MM-DD HH:MM:SS` ({mode_label})\n"
            f"Example: `2026-09-16 12:30:41`\n"
            "_Include seconds for precision_ ⚡",
            parse_mode="Markdown",
        )
        return STATE_MINT_TIME

    now_utc = datetime.now(timezone.utc)
    if dt < now_utc - timedelta(seconds=60):
        # Use user's tz for the current-time display
        tg_id = context.user_data.get("_telegram_id")
        try:
            from db import queries as _q
            user_tz = await _q.get_user_timezone(tg_id) if tg_id else "UTC"
        except Exception:
            user_tz = "UTC"
        local_now = format_in_user_tz(now_utc, user_tz)
        utc_now = format_utc_precise(now_utc)
        await update.message.reply_text(
            f"⚠️ That time is in the past!\n"
            f"Current local: `{local_now}`  /  UTC: `{utc_now}`\n"
            "Enter a future datetime:",
            parse_mode="Markdown",
        )
        return STATE_MINT_TIME

    # If slightly in the past (within 60s grace), treat as immediate
    if dt <= now_utc:
        context.user_data["mint_scheduled_at"] = None
    else:
        context.user_data["mint_scheduled_at"] = dt
        tg_id = context.user_data.get("_telegram_id")
        try:
            from db import queries as _q
            user_tz = await _q.get_user_timezone(tg_id) if tg_id else "UTC"
        except Exception:
            user_tz = "UTC"
        dual_str = format_dual_time(dt, user_tz)
        await update.message.reply_text(
            f"✅ Scheduled: `{dual_str}`",
            parse_mode="Markdown",
        )
    return await _prompt_auto_mint(update.message)


# ── Step 14: Auto-Mint ────────────────────────────────────────────────────────

async def _prompt_auto_mint(target):
    current = context_auto_mint_default()
    keyboard = [
        [
            InlineKeyboardButton("✅ Auto-Mint ON",  callback_data="mc_am:on"),
            InlineKeyboardButton("❌ Auto-Mint OFF", callback_data="mc_am:off"),
        ],
        _nav_row("mint_time"),
    ]
    text = (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*Step 14/19 — Auto-Mint*\n\n"
        "When enabled, the bot automatically executes the mint\n"
        "at the scheduled time without requiring manual confirmation.\n\n"
        "Enable Auto-Mint?"
    )
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return STATE_AUTO_MINT


def context_auto_mint_default():
    return True  # Default: ON


async def handle_auto_mint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "mint_cancel":
        await query.edit_message_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    context.user_data["mint_auto_mint"] = (query.data == "mc_am:on")
    return await _prompt_auto_gas(query, context)


# ── Step 15: Auto Gas / Gas Boost ────────────────────────────────────────────

async def _prompt_auto_gas(target, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("✅ Auto Gas ON",  callback_data="mc_ag:on"),
            InlineKeyboardButton("❌ Auto Gas OFF", callback_data="mc_ag:off"),
        ],
        _nav_row("auto_mint"),
    ]
    text = (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*Step 15/19 — Auto Gas / Gas Boost*\n\n"
        "When enabled, the bot auto-adjusts gas based on network conditions\n"
        "and can boost gas fees if transactions are stuck.\n\n"
        "Enable Auto Gas?"
    )
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return STATE_AUTO_GAS


async def handle_auto_gas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "mint_cancel":
        await query.edit_message_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    auto_gas = (query.data == "mc_ag:on")
    context.user_data["mint_auto_gas"] = auto_gas

    if auto_gas:
        keyboard = [
            [
                InlineKeyboardButton("10%", callback_data="mc_boost:10"),
                InlineKeyboardButton("25%", callback_data="mc_boost:25"),
                InlineKeyboardButton("50%", callback_data="mc_boost:50"),
            ],
            [
                InlineKeyboardButton("100%", callback_data="mc_boost:100"),
                InlineKeyboardButton("✏️ Custom", callback_data="mc_boost_custom"),
            ],
            _nav_row("auto_mint"),
        ]
        await query.edit_message_text(
            "⚡ *Gas Boost Percentage*\n\n"
            "By how much should the bot boost gas if a TX is stuck?\n"
            "_(e.g. 25% means gas fee is increased by 25%)_",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return STATE_AUTO_GAS
    else:
        context.user_data["mint_gas_boost_pct"] = 0
        return await _prompt_retries(query)


async def handle_gas_boost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "mint_cancel":
        await query.edit_message_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    if query.data == "mc_boost_custom":
        await query.edit_message_text("✏️ Enter gas boost percentage (e.g. `30` for 30%):")
        return STATE_AUTO_GAS

    boost = int(query.data.split(":")[1])
    context.user_data["mint_gas_boost_pct"] = boost
    return await _prompt_retries(query)


async def handle_gas_boost_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_inline(update):
        return ConversationHandler.END

    text = update.message.text.strip()
    if text.startswith("/cancel"):
        await update.message.reply_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    try:
        boost = int(text.rstrip("%"))
        if boost < 0 or boost > 500:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Enter a percentage between 0 and 500:")
        return STATE_AUTO_GAS

    context.user_data["mint_gas_boost_pct"] = boost
    return await _prompt_retries(update.message)


# ── Step 16: Transaction Retry ────────────────────────────────────────────────

async def _prompt_retries(target):
    keyboard = [
        [
            InlineKeyboardButton("✅ Retry ON",  callback_data="mc_retry:on"),
            InlineKeyboardButton("❌ Retry OFF", callback_data="mc_retry:off"),
        ],
        _nav_row("auto_gas"),
    ]
    text = (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*Step 16/19 — Transaction Retry*\n\n"
        "Should the bot automatically retry failed transactions?"
    )
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return STATE_RETRIES


async def handle_retries(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "mint_cancel":
        await query.edit_message_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    tx_retry = (query.data == "mc_retry:on")
    context.user_data["mint_tx_retry"] = tx_retry

    if tx_retry:
        return await _prompt_max_retries(query)
    else:
        context.user_data["mint_max_retries"] = 0
        return await _prompt_notify(query)


# ── Step 17: Max Retries ──────────────────────────────────────────────────────

async def _prompt_max_retries(target):
    keyboard = [
        [
            InlineKeyboardButton("1",  callback_data="mc_mr:1"),
            InlineKeyboardButton("3",  callback_data="mc_mr:3"),
            InlineKeyboardButton("5",  callback_data="mc_mr:5"),
            InlineKeyboardButton("10", callback_data="mc_mr:10"),
        ],
        [InlineKeyboardButton("✏️ Custom", callback_data="mc_mr_custom")],
        _nav_row("retries"),
    ]
    text = (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*Step 17/19 — Max Retries*\n\n"
        "Maximum number of retry attempts per transaction:"
    )
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return STATE_MAX_RETRIES


async def handle_max_retries_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "mint_cancel":
        await query.edit_message_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    if query.data == "mc_mr_custom":
        await query.edit_message_text("✏️ Enter max retries (1–20):")
        return STATE_MAX_RETRIES

    retries = int(query.data.split(":")[1])
    context.user_data["mint_max_retries"] = retries
    return await _prompt_notify(query)


async def handle_max_retries_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_inline(update):
        return ConversationHandler.END

    text = update.message.text.strip()
    if text.startswith("/cancel"):
        await update.message.reply_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    try:
        retries = int(text)
        if retries < 1 or retries > 20:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Enter a number between 1 and 20:")
        return STATE_MAX_RETRIES

    context.user_data["mint_max_retries"] = retries
    return await _prompt_notify(update.message)


# ── Step 18: Notifications (success + fail merged) ────────────────────────────

async def _prompt_notify(target):
    keyboard = [
        [
            InlineKeyboardButton("🔔 Notifications ON",  callback_data="mc_notify:on"),
            InlineKeyboardButton("🔕 Notifications OFF", callback_data="mc_notify:off"),
        ],
        _nav_row("max_retries"),
    ]
    text = (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*Step 18/19 — Notifications* 🔔\n\n"
        "Enable Telegram alerts for this mint?\n\n"
        "• *ON* — notify on both success *and* failure\n"
        "• *OFF* — no mint result pop-ups"
    )
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return STATE_NOTIFY


async def handle_notify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "mint_cancel":
        await query.edit_message_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    enabled = (query.data == "mc_notify:on")
    context.user_data["mint_notify_success"] = enabled
    context.user_data["mint_notify_fail"] = enabled
    return await _render_review(query, context)


# ── Step 19: Review & Confirm ─────────────────────────────────────────────────

async def _render_review(target, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data
    chain     = d.get("mint_chain", "?")
    emoji     = CHAIN_EMOJI.get(chain, "🔗")
    chain_name = CHAIN_DISPLAY.get(chain, chain)
    wallets   = d.get("mint_selected_wallet_ids", [])
    qty       = d.get("mint_qty", 1)
    max_w     = d.get("mint_max_per_wallet", 0)
    price_eth = d.get("mint_price_eth", 0)
    gas_preset = d.get("mint_gas_preset", "fast")
    base_fee  = d.get("mint_base_fee", 30)
    priority  = d.get("mint_priority_fee", 2)
    gas_limit = d.get("mint_gas_limit")
    slippage  = d.get("mint_slippage_bps", 50)
    sched_at  = d.get("mint_scheduled_at")
    auto_mint = d.get("mint_auto_mint", True)
    auto_gas  = d.get("mint_auto_gas", False)
    boost_pct = d.get("mint_gas_boost_pct", 0)
    tx_retry  = d.get("mint_tx_retry", True)
    max_ret   = d.get("mint_max_retries", 3)
    ns        = d.get("mint_notify_success", True)
    nf        = d.get("mint_notify_fail", True)
    contract  = d.get("mint_contract", "?")
    mint_fn   = d.get("mint_fn", "mint(uint256)")
    rpc       = d.get("mint_rpc", "default")
    chain_id  = d.get("mint_chain_id", "?")

    if sched_at:
        tg_id = context.user_data.get("_telegram_id")
        try:
            from db import queries as _q
            user_tz = await _q.get_user_timezone(tg_id) if tg_id else "UTC"
        except Exception:
            user_tz = "UTC"
        dual_str = format_dual_time(sched_at, user_tz)
        sched_str = f"`{dual_str}`"
    else:
        sched_str = "⚡ Immediately"
    gas_lim_str = f"{gas_limit:,}" if gas_limit else "Auto"
    slippage_str = f"{slippage / 100:.1f}%"
    boost_str = f"{boost_pct}%" if auto_gas else "Disabled"

    currency = get_chain_currency(chain)

    text = (
        "📋 *Review Mint Configuration*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"⛓ Chain: {emoji} {chain_name} (ID: {chain_id})\n"
        f"🎯 Phase: *{d.get('mint_phase_name', 'Default / Custom')}*\n"
        f"🌐 RPC: `{(rpc[:40] + '…') if len(str(rpc)) > 40 else rpc}`\n"
        f"📜 Contract: `{mask_address(contract)}`\n"
        f"🔧 Mint Fn: `{mint_fn}`\n"
        f"💰 Price: {price_eth} {currency}\n"
        f"👛 Wallets: {len(wallets)}\n"
        f"🎯 Qty/wallet: {qty}  (Max: {'Unlimited' if max_w == 0 else max_w})\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⛽ Gas: {gas_preset.capitalize()} ({base_fee}+{priority} gwei)\n"
        f"⚡ Gas Limit: {gas_lim_str}\n"
        f"📊 Slippage: {slippage_str}\n"
        f"🚀 Auto Gas: {'ON' if auto_gas else 'OFF'} | Boost: {boost_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ Start: {sched_str}\n"
        f"🤖 Auto-Mint: {'ON ✅' if auto_mint else 'OFF ❌'}\n"
        f"🔄 Retry: {'ON' if tx_retry else 'OFF'} | Max: {max_ret}\n"
        f"🔔 Notifications: {'ON (success + fail)' if (ns and nf) else 'OFF'}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Confirm and schedule this mint?"
    )

    keyboard = [
        [InlineKeyboardButton("🚀 Confirm & Schedule Mint", callback_data="mc_final_confirm")],
        _nav_row("notify"),
    ]

    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return STATE_REVIEW


# ── Final confirm: DB commit + engine ─────────────────────────────────────────

async def handle_final_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "mint_cancel":
        await query.edit_message_text("❌ Mint setup cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    await query.edit_message_text("⏳ Creating mint session...")

    user_id = update.effective_user.id
    d = context.user_data

    try:
        info = d.get("contract_info") or {}
        mint_fn = d.get("mint_fn", "mint(uint256)")
        is_fcfs = mint_fn.startswith("mintSigned") or "Signed" in mint_fn
        collection_slug = (info.get("collection_slug") or "").strip()
        # FCFS needs OpenSea collection slug for REST mint-build; public keeps CA for direct path
        if is_fcfs and collection_slug:
            drop_slug = collection_slug
        else:
            drop_slug = d.get("mint_contract", "")
        drop_name = info.get("name") or f"Mint @ {mask_address(d.get('mint_contract', ''))}"
        phase_name = d.get("mint_phase_name") or ""
        if phase_name:
            # Keep drop_name short but identifiable across concurrent sessions
            short_phase = "FCFS" if is_fcfs else ("Public" if "public" in phase_name.lower() else phase_name[:24])
            drop_name = f"{drop_name} [{short_phase}]"

        if is_fcfs and not collection_slug:
            await query.edit_message_text(
                "⚠️ *FCFS setup incomplete*\n\n"
                "OpenSea collection slug was not resolved for this CA, so signed "
                "mint calldata cannot be built.\n\n"
                "Re-enter the contract address (Step 5) and ensure the drop is "
                "listed on OpenSea, then try again.",
                parse_mode="Markdown",
            )
            return ConversationHandler.END

        session_id = await queries.create_mint_session_v2(
            telegram_id=user_id,
            nft_contract=d["mint_contract"],
            chain=d["mint_chain"],
            rpc_url=d.get("mint_rpc"),
            chain_id=d.get("mint_chain_id"),
            mint_function=mint_fn,
            mint_price_wei=d.get("mint_price_wei", 0),
            quantity_per_wallet=d.get("mint_qty", 1),
            max_per_wallet=d.get("mint_max_per_wallet", 0),
            gas_limit=d.get("mint_gas_limit") or (180000 if (d.get("mint_is_seadrop") or info.get("is_seadrop")) else 200000),
            base_fee_gwei=d.get("mint_base_fee", 30),
            priority_fee_gwei=d.get("mint_priority_fee", 2),
            slippage_bps=d.get("mint_slippage_bps", 50),
            gas_multiplier=d.get("mint_gas_multiplier", 1.5),
            gas_preset=d.get("mint_gas_preset", "fast"),
            auto_mint=d.get("mint_auto_mint", True),
            auto_gas=d.get("mint_auto_gas", False),
            gas_boost_pct=d.get("mint_gas_boost_pct", 0),
            tx_retry=d.get("mint_tx_retry", True),
            max_retries=d.get("mint_max_retries", 3),
            notify_success=d.get("mint_notify_success", True),
            notify_fail=d.get("mint_notify_fail", True),
            scheduled_at=d.get("mint_scheduled_at"),
            drop_slug=drop_slug,
            drop_name=drop_name,
        )

        await queries.attach_wallets_to_session(session_id, d["mint_selected_wallet_ids"])
        await queries.update_wallets_chain(d["mint_selected_wallet_ids"], d["mint_chain"])

        # Notify Rust engine
        engine = get_engine_client()
        engine_ok = False
        try:
            engine_ok = await engine.schedule_mint(session_id, user_id)
        except Exception as e:
            logger.warning("Engine schedule_mint warning: %s", e)

        chain = d["mint_chain"]
        emoji = CHAIN_EMOJI.get(chain, "🔗")
        chain_name = CHAIN_DISPLAY.get(chain, chain)
        sched_at = d.get("mint_scheduled_at")
        if sched_at:
            sched_str = f"`{format_utc_precise(sched_at)} / {format_utc(sched_at)}`"
        else:
            sched_str = "⚡ Immediately"
        engine_note = "✅ Engine registered" if engine_ok else "ℹ️ Engine will pick up from DB"

        await query.edit_message_text(
            f"✅ *Mint Session #{session_id} Created!*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⛓ {emoji} {chain_name}\n"
            f"🎯 Phase: *{d.get('mint_phase_name', 'Default')}*\n"
            f"📜 Contract: `{mask_address(d['mint_contract'])}`\n"
            f"👛 Wallets: {len(d['mint_selected_wallet_ids'])}\n"
            f"🎯 Qty/wallet: {d.get('mint_qty', 1)}\n"
            f"⏰ Fires at: {sched_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"_{engine_note}_\n\n"
            f"📋 /logs {session_id} — detailed logs\n"
            f"🔗 /txhash {session_id} — TX hashes\n"
            f"🛑 /stopbot {session_id} — cancel session",
            parse_mode="Markdown",
        )

    except Exception as e:
        logger.error("Mint session create error: %s", e, exc_info=True)
        await query.edit_message_text(f"❌ Failed to create mint session: {e}")

    context.user_data.clear()
    return ConversationHandler.END


# ── Cancel / Timeout ──────────────────────────────────────────────────────────

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Mint setup cancelled.")
    return ConversationHandler.END


async def conversation_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if update.effective_message:
        await update.effective_message.reply_text(
            "⏱ Mint setup timed out. Use /mint to start again."
        )
    return ConversationHandler.END


def get_mint_conversation_handler() -> ConversationHandler:
    # Common text+command fallbacks
    text_no_cmd = filters.TEXT & ~filters.COMMAND
    cancel_cmd  = CommandHandler("cancel", cancel_command)

    def back():
        return CallbackQueryHandler(handle_mint_back, pattern=r"^mint_back:")

    return ConversationHandler(
        entry_points=[CommandHandler("mint", start_mint_flow)],
        states={
            STATE_WALLET: [
                CallbackQueryHandler(
                    handle_wallet_choice,
                    pattern=r"^(mw_select:|mw_all|mw_continue|mint_cancel)",
                ),
                back(),
            ],
            STATE_CHAIN: [
                CallbackQueryHandler(
                    handle_chain_choice, pattern=r"^(mc_chain:|mint_cancel)"
                ),
            ],
            STATE_RPC: [
                CallbackQueryHandler(
                    handle_rpc_choice, pattern=r"^(mc_rpc_default|mc_rpc_custom|mint_cancel)"
                ),
                back(),
                MessageHandler(text_no_cmd, handle_rpc_text),
                cancel_cmd,
            ],
            STATE_CHAIN_ID: [
                CallbackQueryHandler(
                    handle_chain_id_choice, pattern=r"^(mc_cid_default|mc_cid_custom|mint_cancel)"
                ),
                back(),
                MessageHandler(text_no_cmd, handle_chain_id_text),
                cancel_cmd,
            ],
            STATE_CONTRACT: [
                back(),
                MessageHandler(text_no_cmd, handle_contract),
                cancel_cmd,
            ],
            STATE_PHASE: [
                CallbackQueryHandler(
                    handle_phase_choice, pattern=r"^(mc_phase:|mc_phase_skip|mint_cancel)"
                ),
                back(),
                cancel_cmd,
            ],
            STATE_MINT_FN: [
                CallbackQueryHandler(
                    handle_mint_fn_choice, pattern=r"^(mc_fn|mc_fnp:|mint_cancel)"
                ),
                back(),
                MessageHandler(text_no_cmd, handle_mint_fn_text),
                cancel_cmd,
            ],
            STATE_MINT_PRICE: [
                CallbackQueryHandler(
                    handle_mint_price_callback,
                    pattern=r"^(mc_price:|mc_price_custom|mc_funds_recheck:|mint_cancel)",
                ),
                back(),
                MessageHandler(text_no_cmd, handle_mint_price_text),
                cancel_cmd,
            ],
            STATE_QUANTITY: [
                CallbackQueryHandler(
                    handle_quantity_callback,
                    pattern=r"^(mc_qty:|mc_qty_max|mc_qty_custom|mc_funds_recheck:|mint_cancel)",
                ),
                back(),
                MessageHandler(text_no_cmd, handle_quantity_text),
                cancel_cmd,
            ],
            STATE_GAS_LIMIT: [
                CallbackQueryHandler(
                    handle_gas_limit_callback,
                    pattern=r"^(mc_gl_auto|mc_gl:|mc_gl_custom|mint_cancel)",
                ),
                back(),
                MessageHandler(text_no_cmd, handle_gas_limit_text),
                cancel_cmd,
            ],
            STATE_GAS_FEE: [
                CallbackQueryHandler(
                    handle_gas_fee_callback,
                    pattern=r"^(mc_gas:|mc_gas_custom|mint_cancel)",
                ),
                back(),
                MessageHandler(text_no_cmd, handle_gas_fee_text),
                cancel_cmd,
            ],
            STATE_SLIPPAGE: [
                CallbackQueryHandler(
                    handle_slippage_callback,
                    pattern=r"^(mc_slip:|mc_slip_custom|mint_cancel)",
                ),
                back(),
                MessageHandler(text_no_cmd, handle_slippage_text),
                cancel_cmd,
            ],
            STATE_MINT_TIME: [
                CallbackQueryHandler(
                    handle_mint_time_callback,
                    pattern=r"^(mc_time_|mint_cancel)",
                ),
                back(),
                MessageHandler(text_no_cmd, handle_mint_time_text),
                cancel_cmd,
            ],
            STATE_AUTO_MINT: [
                CallbackQueryHandler(
                    handle_auto_mint, pattern=r"^(mc_am:|mint_cancel)"
                ),
                back(),
            ],
            STATE_AUTO_GAS: [
                CallbackQueryHandler(
                    handle_auto_gas, pattern=r"^(mc_ag:|mint_cancel)"
                ),
                CallbackQueryHandler(
                    handle_gas_boost, pattern=r"^(mc_boost:|mc_boost_custom|mint_cancel)"
                ),
                back(),
                MessageHandler(text_no_cmd, handle_gas_boost_text),
                cancel_cmd,
            ],
            STATE_RETRIES: [
                CallbackQueryHandler(
                    handle_retries, pattern=r"^(mc_retry:|mint_cancel)"
                ),
                back(),
            ],
            STATE_MAX_RETRIES: [
                CallbackQueryHandler(
                    handle_max_retries_callback, pattern=r"^(mc_mr:|mc_mr_custom|mint_cancel)"
                ),
                back(),
                MessageHandler(text_no_cmd, handle_max_retries_text),
                cancel_cmd,
            ],
            STATE_NOTIFY: [
                CallbackQueryHandler(
                    handle_notify, pattern=r"^(mc_notify:|mint_cancel)"
                ),
                back(),
            ],
            STATE_REVIEW: [
                CallbackQueryHandler(
                    handle_final_confirm,
                    pattern=r"^(mc_final_confirm|mint_cancel)",
                ),
                back(),
            ],
            ConversationHandler.TIMEOUT: [
                MessageHandler(filters.ALL, conversation_timeout),
            ],
        },
        fallbacks=[cancel_cmd, CallbackQueryHandler(handle_mint_back, pattern=r"^mint_back:")],
        conversation_timeout=600,  # 10 minutes for the full flow
    )
