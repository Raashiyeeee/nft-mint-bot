"""
utils/formatters.py — Message formatting helpers for Telegram output.
"""
import os
from datetime import datetime, timezone
from typing import Optional
import pytz


CHAIN_EMOJI = {
    "ethereum":  "🔷",
    "base":      "🔵",
    "polygon":   "🟣",
    "arbitrum":  "🔶",
    "optimism":  "🔴",
    "avalanche": "🔺",
    "bsc":       "🟡",
    "zora":      "⚪",
    "robinhood": "🟢",
    "shape":     "🔷",
    "abstract":  "⬜",
    "ape":       "🐵",
    "arc":       "🌐",
    "hyperevm":   "💧",
    "ink":        "🖋️",
}

CHAIN_DISPLAY = {
    "ethereum":  "Ethereum",
    "base":      "Base",
    "polygon":   "Polygon",
    "arbitrum":  "Arbitrum",
    "optimism":  "Optimism",
    "avalanche": "Avalanche",
    "bsc":       "BSC",
    "zora":      "Zora",
    "robinhood": "Robinhood",
    "shape":     "Shape",
    "abstract":  "Abstract",
    "ape":       "ApeChain",
    "arc":       "Arc",
    "hyperevm":   "HyperEVM",
    "ink":        "Ink",
}

CHAIN_CURRENCY = {
    "ethereum":  "ETH",
    "base":      "ETH",
    "polygon":   "POL",
    "arbitrum":  "ETH",
    "optimism":  "ETH",
    "avalanche": "AVAX",
    "bsc":       "BNB",
    "zora":      "ETH",
    "robinhood": "ETH",
    "shape":     "ETH",
    "abstract":  "ETH",
    "ape":       "APE",
    "arc":       "USDC",
    "hyperevm":   "HYPE",
    "ink":        "ETH",
}


def get_chain_currency(chain: Optional[str]) -> str:
    if not chain:
        return "ETH"
    return CHAIN_CURRENCY.get(chain.lower(), "ETH")


CHAIN_EXPLORER = {
    "ethereum":  "https://etherscan.io/tx/",
    "base":      "https://basescan.org/tx/",
    "polygon":   "https://polygonscan.com/tx/",
    "arbitrum":  "https://arbiscan.io/tx/",
    "optimism":  "https://optimistic.etherscan.io/tx/",
    "avalanche": "https://snowtrace.io/tx/",
    "bsc":       "https://bscscan.com/tx/",
    "zora":      "https://explorer.zora.energy/tx/",
    "robinhood": "https://robinhoodchain.blockscout.com/tx/",
    "shape":     "https://shapescan.xyz/tx/",
    "abstract":  "https://abscan.org/tx/",
    "ape":       "https://apescan.io/tx/",
    "arc":       "https://explorer.arc.io/tx/",
    "hyperevm":   "https://hyperevmscan.io/tx/",
    "ink":        "https://explorer.inkonchain.com/tx/",
}

# Default chain IDs for known networks
CHAIN_DEFAULT_IDS = {
    "ethereum":  1,
    "base":      8453,
    "polygon":   137,
    "arbitrum":  42161,
    "optimism":  10,
    "avalanche": 43114,
    "bsc":       56,
    "zora":      7777777,
    "robinhood": 4663,
    "shape":     360,
    "abstract":  2741,
    "ape":       33139,
    "arc":       5042,
    "hyperevm":   999,
    "ink":        57073,
}

_alchemy_key = os.getenv("ALCHEMY_KEY", "")

# Default RPC URLs (Alchemy if key is configured, fallback to public)
CHAIN_DEFAULT_RPCS = {
    "ethereum":  f"https://eth-mainnet.g.alchemy.com/v2/{_alchemy_key}",
    "base":      f"https://base-mainnet.g.alchemy.com/v2/{_alchemy_key}",
    "polygon":   f"https://polygon-mainnet.g.alchemy.com/v2/{_alchemy_key}",
    "arbitrum":  f"https://arb-mainnet.g.alchemy.com/v2/{_alchemy_key}",
    "optimism":  f"https://opt-mainnet.g.alchemy.com/v2/{_alchemy_key}",
    "avalanche": f"https://avax-mainnet.g.alchemy.com/v2/{_alchemy_key}",
    "bsc":       f"https://bnb-mainnet.g.alchemy.com/v2/{_alchemy_key}",
    "zora":      f"https://zora-mainnet.g.alchemy.com/v2/{_alchemy_key}",
    "shape":     f"https://shape-mainnet.g.alchemy.com/v2/{_alchemy_key}",
    "robinhood": f"https://robinhood-mainnet.g.alchemy.com/v2/{_alchemy_key}",
    "arc":       f"https://arc-mainnet.g.alchemy.com/v2/{_alchemy_key}",
    "hyperevm":   f"https://hyperliquid-mainnet.g.alchemy.com/v2/{_alchemy_key}",
    "ink":        f"https://ink-mainnet.g.alchemy.com/v2/{_alchemy_key}",
    "abstract":  "https://api.mainnet.abs.xyz",
    "ape":       "https://rpc.apechain.com",
}

# Gas presets (base_fee_gwei, priority_fee_gwei)
GAS_PRESETS = {
    "standard":   {"base": 20,  "priority": 1,  "label": "🐢 Standard",   "multiplier": 1.0},
    "fast":       {"base": 30,  "priority": 2,  "label": "⚡ Fast",        "multiplier": 1.5},
    "aggressive": {"base": 50,  "priority": 5,  "label": "🚀 Aggressive",  "multiplier": 2.0},
    "max":        {"base": 100, "priority": 10, "label": "🔥 Max Priority", "multiplier": 3.0},
}

# Common mint function signatures
MINT_FUNCTION_PRESETS = [
    "mintPublic(address,address,address,uint256)",  # SeaDrop public
    "mintSigned(address,address,address,uint256,(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool),uint256,bytes)",  # SeaDrop FCFS
    "mint(uint256)",
    "publicMint(uint256)",
    "mintPublic(uint256)",
    "mintNFT(uint256)",
    "safeMint(address,uint256)",
    "mint(address,uint256)",
    "mintSeaDrop(address,uint256)",
]

# Short labels for Telegram buttons (full sig kept in MINT_FUNCTION_PRESETS)
MINT_FUNCTION_LABELS = {
    "mintPublic(address,address,address,uint256)": "mintPublic (SeaDrop Public)",
    "mintSigned(address,address,address,uint256,(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool),uint256,bytes)": "mintSigned (SeaDrop FCFS)",
}


def mask_address(address: str) -> str:
    """0x1234...5678"""
    if len(address) < 10:
        return address
    return f"{address[:6]}...{address[-4:]}"


def format_utc(dt: Optional[datetime]) -> str:
    if not dt:
        return "TBD"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def format_utc_precise(dt: Optional[datetime]) -> str:
    """Full UTC time with seconds: HH:MM:SS UTC (for exact mint timing display)."""
    if not dt:
        return "TBD"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%H:%M:%S UTC")


def format_ist(dt: Optional[datetime]) -> str:
    """Kept for backward compatibility — now returns UTC precise (HH:MM:SS UTC)."""
    return format_utc_precise(dt)


def format_ist_date(dt: Optional[datetime]) -> str:
    """Kept for backward compatibility — now returns full UTC (YYYY-MM-DD HH:MM:SS UTC)."""
    return format_utc(dt)


def format_local(dt: Optional[datetime], tz_name: str = "UTC") -> str:
    return format_utc(dt)


def format_in_user_tz(dt: Optional[datetime], tz_name: str = "UTC") -> str:
    """Format datetime as HH:MM:SS UTC."""
    return format_utc_precise(dt)


def format_in_user_tz_date(dt: Optional[datetime], tz_name: str = "UTC") -> str:
    """Format datetime as YYYY-MM-DD HH:MM:SS UTC."""
    return format_utc(dt)


def format_dual_time(dt: Optional[datetime], user_tz: str = "UTC") -> str:
    """Returns exact UTC time (HH:MM:SS UTC)."""
    return format_utc_precise(dt)


# ── Popular timezone reference (for the /timezone UI picker) ──────────────────
POPULAR_TIMEZONES = [
    ("🇮🇳 IST",  "Asia/Kolkata",         "+5:30"),
    ("🇺🇸 EST",  "America/New_York",      "-5:00"),
    ("🇺🇸 PST",  "America/Los_Angeles",   "-8:00"),
    ("🇺🇸 CST",  "America/Chicago",       "-6:00"),
    ("🇬🇧 GMT",  "Europe/London",         "+0:00"),
    ("🇪🇺 CET",  "Europe/Berlin",         "+1:00"),
    ("🇦🇪 GST",  "Asia/Dubai",            "+4:00"),
    ("🇸🇬 SGT",  "Asia/Singapore",        "+8:00"),
    ("🇯🇵 JST",  "Asia/Tokyo",            "+9:00"),
    ("🇦🇺 AEST", "Australia/Sydney",      "+10:00"),
    ("🇧🇷 BRT",  "America/Sao_Paulo",     "-3:00"),
    ("🇷🇺 MSK",  "Europe/Moscow",         "+3:00"),
    ("🌍 UTC",   "UTC",                   "+0:00"),
]


def format_wallet_line(wallet: dict) -> str:
    """🔷 ETH | 0x1234...5678 | "Main wallet" | ✅ Active"""
    chain_val = wallet.get("chain", "")
    addr  = mask_address(wallet.get("wallet_address", ""))
    label = wallet.get("wallet_label") or "No label"
    active = "✅ Active" if wallet.get("is_active") else "❌ Inactive"
    if chain_val in ("evm", "all", "", None):
        return f"🔑 `{addr}` | \"{label}\" | {active}"
    emoji = CHAIN_EMOJI.get(chain_val, "🔗")
    chain = CHAIN_DISPLAY.get(chain_val, chain_val)
    return f"{emoji} {chain} | `{addr}` | \"{label}\" | {active}"


def format_balance_line(wallet: dict, balance_eth: Optional[float] = None) -> str:
    """🔵 Base | 0x1234...5678 | 'Main' | 💰 0.1234 ETH"""
    chain_val = wallet.get("chain", "")
    emoji = CHAIN_EMOJI.get(chain_val, "🔗")
    chain = CHAIN_DISPLAY.get(chain_val, chain_val)
    addr  = mask_address(wallet.get("wallet_address", ""))
    label = wallet.get("wallet_label") or "No label"
    currency = get_chain_currency(chain_val)
    bal   = f"💰 {balance_eth:.6f} {currency}" if balance_eth is not None else "💰 —"
    if chain_val in ("evm", "all", "", None):
        return f"🔑 `{addr}` | \"{label}\" | {bal}"
    return f"{emoji} {chain} | `{addr}` | \"{label}\" | {bal}"


def format_mint_summary(
    drop_name: str,
    chain: str,
    contract: str,
    wallet_count: int,
    quantity_per_wallet: int,
    gas_label: str,
    fire_time: Optional[datetime],
    session_id: int,
) -> str:
    emoji = CHAIN_EMOJI.get(chain, "🔗")
    chain_display = CHAIN_DISPLAY.get(chain, chain)
    utc_str = format_utc(fire_time) if fire_time else "⚡ Immediately"
    return (
        f"✅ *Mint Session Created*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 Drop: {drop_name}\n"
        f"⛓ Chain: {emoji} {chain_display}\n"
        f"📜 Contract: `{mask_address(contract)}`\n"
        f"👛 Wallets: {wallet_count} selected\n"
        f"🎯 Qty/wallet: {quantity_per_wallet}\n"
        f"📊 Total qty: {wallet_count * quantity_per_wallet}\n"
        f"⛽ Gas: {gas_label}\n"
        f"🕐 Fires at: `{utc_str}`\n"
        f"🆔 Session ID: #{session_id}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Bot will notify you when:\n"
        f"• Mint goes live 🚀\n"
        f"• Each transaction is sent 📤\n"
        f"• Each confirmation lands ✅ or fails ❌"
    )


def format_mint_result_card(
    drop_name: str,
    chain: str,
    wallet_address: str,
    status: str,
    tx_hash: Optional[str] = None,
    token_id: Optional[int] = None,
    latency_ms: Optional[int] = None,
    error_msg: Optional[str] = None,
    attempt_no: int = 1,
    fired_at: Optional[datetime] = None,
    user_tz: str = "UTC",
) -> str:
    """Rich single-wallet mint outcome card shown as a Telegram notification."""
    chain_display = CHAIN_DISPLAY.get(chain, chain)
    chain_emoji   = CHAIN_EMOJI.get(chain, "🔗")
    explorer      = CHAIN_EXPLORER.get(chain, "")
    wallet_short  = mask_address(wallet_address)

    fired_str = format_dual_time(fired_at or datetime.now(timezone.utc), user_tz)

    if status == "success":
        latency_str = f"{latency_ms} ms" if latency_ms is not None else "—"
        token_str   = f"🎟 Token #`{token_id}`" if token_id is not None else ""
        tx_link     = f"[{tx_hash[:10]}...{tx_hash[-6:]}]({explorer}{tx_hash})" if (tx_hash and explorer) else (f"`{tx_hash[:10]}...`" if tx_hash else "N/A")
        return (
            f"🟢 *MINT SUCCESS* — {drop_name}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⛓ {chain_emoji} {chain_display}\n"
            f"👛 Wallet: `{wallet_short}`\n"
            f"{(token_str + chr(10)) if token_str else ''}"
            f"📤 TX: {tx_link}\n"
            f"⚡ Latency: `{latency_str}`\n"
            f"🕐 Fired at: `{fired_str}`\n"
            f"🔁 Attempt: #{attempt_no}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ NFT minted successfully!"
        )
    elif status == "skipped":
        return (
            f"⏭ *MINT SKIPPED* — {drop_name}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👛 Wallet: `{wallet_short}`\n"
            f"📝 Reason: {error_msg or 'Already minted / not eligible'}\n"
            f"🕐 At: `{fired_str}`"
        )
    else:
        err_display = (error_msg or status or "Unknown error")[:120]
        return (
            f"🔴 *MINT FAILED* — {drop_name}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⛓ {chain_emoji} {chain_display}\n"
            f"👛 Wallet: `{wallet_short}`\n"
            f"❌ Error: `{err_display}`\n"
            f"🕐 Failed at: `{fired_str}`\n"
            f"🔁 Attempt: #{attempt_no}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💡 Check /logs for details"
        )


def format_mint_results(
    drop_name: str,
    chain: str,
    results: list[dict],
    user_tz: str = "UTC",
) -> str:
    """Multi-wallet batch summary card."""
    chain_display = CHAIN_DISPLAY.get(chain, chain)
    chain_emoji   = CHAIN_EMOJI.get(chain, "🔗")
    explorer      = CHAIN_EXPLORER.get(chain, "")

    success_count = sum(1 for r in results if r.get("status") == "success")
    fail_count    = sum(1 for r in results if r.get("status") not in ("success", "skipped"))
    skip_count    = sum(1 for r in results if r.get("status") == "skipped")
    total         = len(results)

    overall_icon = "✅" if fail_count == 0 and success_count > 0 else ("⚠️" if success_count > 0 else "❌")
    now_str = format_dual_time(datetime.now(timezone.utc), user_tz)

    lines = [
        f"{overall_icon} *Mint Results — {drop_name}*",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"{chain_emoji} {chain_display} | ✅ {success_count}/{total} success  ❌ {fail_count} fail  ⏭ {skip_count} skip",
        f"🕐 Completed at: `{now_str}`",
        f"━━━━━━━━━━━━━━━━━━━━",
    ]

    for i, r in enumerate(results, 1):
        wallet   = mask_address(r.get("wallet_address", ""))
        status   = r.get("status", "unknown")
        tx       = r.get("tx_hash") or ""
        token_id = r.get("token_id")
        latency  = r.get("latency_ms")
        error    = r.get("error_msg") or ""

        if status == "success":
            latency_str = f"`{latency} ms`" if latency is not None else ""
            tx_link     = f"[{tx[:8]}...{tx[-4:]}]({explorer}{tx})" if (tx and explorer) else (f"`{tx[:10]}...`" if tx else "")
            token_str   = f"#`{token_id}` " if token_id is not None else ""
            lines.append(f"✅ W{i} `{wallet}` → Token {token_str}| {latency_str} | {tx_link}")
        elif status == "skipped":
            lines.append(f"⏭ W{i} `{wallet}` → {error or 'Skipped'}")
        else:
            lines.append(f"❌ W{i} `{wallet}` → {error[:60] or status}")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    if explorer and any(r.get("tx_hash") for r in results):
        lines.append(f"🔍 [View on {chain_display} Explorer]({explorer})")

    return "\n".join(lines)


def format_tx_log_line(log: dict) -> str:
    """Format a single tx_logs row for display."""
    status = log.get("status", "unknown")
    status_emoji = {
        "pending":   "⏳",
        "sent":      "📤",
        "confirmed": "✅",
        "failed":    "❌",
    }.get(status, "❓")

    wallet  = mask_address(log.get("wallet_address", ""))
    attempt = log.get("attempt_no", 1)
    tx      = log.get("tx_hash") or ""
    tx_str  = f"`{tx[:10]}...{tx[-6:]}`" if len(tx) > 20 else (f"`{tx}`" if tx else "N/A")
    block   = log.get("block_number")
    gas_gwei = log.get("gas_price_gwei")
    err     = log.get("error_msg") or ""
    url     = log.get("explorer_url") or ""

    line = f"{status_emoji} Attempt #{attempt} | `{wallet}` | TX: {tx_str}"
    if block:
        line += f" | Block #{block}"
    if gas_gwei:
        line += f" | {gas_gwei} gwei"
    if url:
        line += f"\n   🔗 [Explorer]({url})"
    if err and status == "failed":
        line += f"\n   ⚠️ {err[:80]}"
    return line


def format_history_row(session: dict) -> str:
    chain   = session.get("chain", "")
    emoji   = CHAIN_EMOJI.get(chain, "🔗")
    drop    = session.get("drop_name") or session.get("drop_slug", "Unknown")
    status  = session.get("status", "unknown")
    fired_at = session.get("fired_at")
    success = session.get("success_count", 0)
    total   = session.get("total_results", 0)
    session_id = session.get("id", "?")

    status_emoji = {
        "completed": "✅", "pending":  "⏳", "active":    "🔄",
        "cancelled": "❌", "failed":  "💥", "paused":    "⏸",
    }.get(status, "❓")

    time_str = format_utc(fired_at) if fired_at else "Pending"
    return f"{status_emoji} #{session_id} | {emoji} {drop} | {success}/{total} ✓ | {time_str}"


def gas_label(multiplier: float) -> str:
    labels = {
        1.0: "🐢 Standard (1x)",
        1.5: "⚡ Fast (1.5x)",
        2.0: "🚀 Turbo (2x)",
        3.0: "🔥 Max Priority (3x)",
    }
    return labels.get(multiplier, f"✏️ Custom ({multiplier}x)")


def format_gas_preset_info(preset_name: str) -> str:
    """Return a formatted string describing a gas preset."""
    preset = GAS_PRESETS.get(preset_name)
    if not preset:
        return f"Unknown preset: {preset_name}"
    return (
        f"{preset['label']}\n"
        f"  Base fee: ~{preset['base']} gwei\n"
        f"  Priority: ~{preset['priority']} gwei\n"
        f"  Multiplier: {preset['multiplier']}x"
    )
