"""
notifications/sender.py — Push notification sender for all mint events.
Called by both the Python bot and the notification HTTP endpoint
(which receives events pushed from the Rust engine).
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from db import queries
from utils import formatters
from utils.formatters import (
    format_dual_time,
    format_in_user_tz_date,
    format_utc,
    format_utc_precise,
    format_mint_result_card,
    format_mint_results,
    CHAIN_DISPLAY,
    CHAIN_EMOJI,
    CHAIN_EXPLORER,
    mask_address,
)

logger = logging.getLogger(__name__)


class NotificationSender:
    def __init__(self, bot: Bot):
        self.bot = bot

    async def _get_tz(self, telegram_id: int) -> str:
        try:
            return await queries.get_user_timezone(telegram_id)
        except Exception:
            return "UTC"

    async def _send(
        self,
        telegram_id: int,
        text: str,
        parse_mode: str = ParseMode.MARKDOWN,
        reply_markup=None,
    ):
        try:
            await self.bot.send_message(
                chat_id=telegram_id,
                text=text,
                parse_mode=parse_mode,
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )
        except Exception as e:
            logger.error("Failed to send notification to %s: %s", telegram_id, e)

    # ── T-5 min warmup notice ─────────────────────────────────────────────────
    async def notify_warmup(
        self,
        telegram_id: int,
        drop_name: str,
        wallet_count: int,
        session_id: int,
        fire_at: Optional[datetime] = None,
    ):
        user_tz = await self._get_tz(telegram_id)
        time_line = format_dual_time(fire_at, user_tz) if fire_at else "Imminent"
        text = (
            f"🕐 *Warming Up — {drop_name}*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏱ Mint fires in ~5 minutes\n"
            f"🕐 Time: `{time_line}`\n"
            f"👛 Wallets ready: `{wallet_count}`\n"
            f"🆔 Session: #{session_id}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔥 Engine is warming up — stay alert!"
        )
        await self._send(telegram_id, text)

    # ── Firing ────────────────────────────────────────────────────────────────
    async def notify_firing(
        self,
        telegram_id: int,
        drop_name: str,
        wallet_count: int,
        fire_at: Optional[datetime] = None,
    ):
        user_tz = await self._get_tz(telegram_id)
        fire_str = format_dual_time(fire_at or datetime.now(timezone.utc), user_tz)
        text = (
            f"🚀 *FIRING — {drop_name}*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 Fired at: `{fire_str}`\n"
            f"👛 Sending to `{wallet_count}` wallet(s)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ Transactions launching NOW..."
        )
        await self._send(telegram_id, text)

    # ── TX sent (per wallet) ──────────────────────────────────────────────────
    async def notify_tx_sent(
        self,
        telegram_id: int,
        wallet_short: str,
        tx_hash: str,
        chain: str,
        drop_name: str = "Mint",
        attempt_no: int = 1,
    ):
        explorer = CHAIN_EXPLORER.get(chain, "")
        chain_emoji = CHAIN_EMOJI.get(chain, "🔗")
        chain_display = CHAIN_DISPLAY.get(chain, chain)
        user_tz = await self._get_tz(telegram_id)
        now_str = format_dual_time(datetime.now(timezone.utc), user_tz)
        tx_link = (
            f"[{tx_hash[:10]}...{tx_hash[-6:]}]({explorer}{tx_hash})"
            if explorer
            else f"`{tx_hash[:10]}...`"
        )
        text = (
            f"📤 *TX Sent — {drop_name}*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{chain_emoji} {chain_display}\n"
            f"👛 Wallet: `...{wallet_short}`\n"
            f"📄 TX: {tx_link}\n"
            f"🔁 Attempt: #{attempt_no}\n"
            f"🕐 Sent at: `{now_str}`\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏳ Waiting for on-chain confirmation..."
        )
        await self._send(telegram_id, text)

    # ── TX skipped ────────────────────────────────────────────────────────────
    async def notify_tx_skipped(
        self,
        telegram_id: int,
        wallet_short: str,
        reason: str,
        drop_name: str = "Mint",
    ):
        user_tz = await self._get_tz(telegram_id)
        now_str = format_dual_time(datetime.now(timezone.utc), user_tz)
        text = (
            f"⏭ *TX Skipped — {drop_name}*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👛 Wallet: `...{wallet_short}`\n"
            f"📝 Reason: {reason}\n"
            f"🕐 At: `{now_str}`"
        )
        await self._send(telegram_id, text)

    # ── Single wallet mint result card ────────────────────────────────────────
    async def notify_mint_complete_card(
        self,
        telegram_id: int,
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
        session_id: Optional[int] = None,
    ):
        """Rich per-wallet outcome card: success/fail with user local + UTC time and ms latency."""
        user_tz = await self._get_tz(telegram_id)
        text = format_mint_result_card(
            drop_name=drop_name,
            chain=chain,
            wallet_address=wallet_address,
            status=status,
            tx_hash=tx_hash,
            token_id=token_id,
            latency_ms=latency_ms,
            error_msg=error_msg,
            attempt_no=attempt_no,
            fired_at=fired_at,
            user_tz=user_tz,
        )
        # Add quick-action buttons for transaction lookup
        keyboard = None
        if session_id:
            explorer = CHAIN_EXPLORER.get(chain, "")
            btns = []
            if tx_hash and explorer:
                btns.append(InlineKeyboardButton("🔍 View TX", url=f"{explorer}{tx_hash}"))
            btns.append(InlineKeyboardButton(f"📋 /logs {session_id}", callback_data=f"noop"))
            keyboard = InlineKeyboardMarkup([[b] for b in btns])
        await self._send(telegram_id, text, reply_markup=keyboard)

    # ── Full mint results (batch summary) ─────────────────────────────────────
    async def notify_mint_results(
        self,
        telegram_id: int,
        drop_name: str,
        chain: str,
        results: list[dict],
        session_id: Optional[int] = None,
    ):
        user_tz = await self._get_tz(telegram_id)
        text = format_mint_results(drop_name, chain, results, user_tz=user_tz)
        explorer = CHAIN_EXPLORER.get(chain, "")
        # Build action buttons
        btns = []
        if session_id:
            btns.append([InlineKeyboardButton(f"📋 Logs /logs {session_id}", callback_data="noop")])
            btns.append([InlineKeyboardButton(f"🔗 TX /txhash {session_id}", callback_data="noop")])
        keyboard = InlineKeyboardMarkup(btns) if btns else None
        await self._send(telegram_id, text, reply_markup=keyboard)

    # ── Schedule changed ──────────────────────────────────────────────────────
    async def notify_schedule_changed(
        self,
        telegram_id: int,
        drop_name: str,
        new_time: Optional[datetime],
        session_id: int,
    ):
        user_tz = await self._get_tz(telegram_id)
        time_line = format_dual_time(new_time, user_tz) if new_time else "TBD"
        text = (
            f"⚠️ *Schedule Changed — {drop_name}*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 New Time: `{time_line}`\n"
            f"🆔 Session: #{session_id}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Session updated automatically."
        )
        await self._send(telegram_id, text)

    # ── Broadcast to all users ────────────────────────────────────────────────
    async def broadcast(self, telegram_ids: list[int], message: str):
        for tid in telegram_ids:
            await self._send(tid, message, parse_mode=ParseMode.MARKDOWN)


# ── Notification HTTP server for Rust engine push events ─────────────────────

import asyncio
from aiohttp import web


def create_notification_server(sender: NotificationSender, port: int = 7879):
    """
    Creates a local HTTP server on port 7879 that receives push events
    from the Rust engine and dispatches them to Telegram users.
    """
    routes = web.RouteTableDef()

    @routes.post("/notify")
    async def handle_notify(request: web.Request) -> web.Response:
        try:
            data = await request.json()
            event_type = data.get("type")
            telegram_id = int(data.get("telegram_id", 0))

            if not telegram_id:
                return web.Response(text="Missing telegram_id", status=400)

            # Parse fired_at if present
            fired_at = None
            fired_at_str = data.get("fired_at")
            if fired_at_str:
                try:
                    fired_at = datetime.fromisoformat(fired_at_str.replace("Z", "+00:00"))
                except ValueError:
                    pass

            # Parse fire_at (scheduled time) if present
            fire_at = None
            fire_at_str = data.get("fire_at")
            if fire_at_str:
                try:
                    fire_at = datetime.fromisoformat(fire_at_str.replace("Z", "+00:00"))
                except ValueError:
                    pass

            if event_type == "warmup_notice":
                await sender.notify_warmup(
                    telegram_id,
                    data.get("drop_name", "Unknown Drop"),
                    data.get("wallet_count", 0),
                    data.get("session_id", 0),
                    fire_at=fire_at,
                )

            elif event_type == "firing":
                await sender.notify_firing(
                    telegram_id,
                    data.get("drop_name", "Unknown"),
                    data.get("wallet_count", 0),
                    fire_at=fire_at,
                )

            elif event_type == "tx_sent":
                await sender.notify_tx_sent(
                    telegram_id,
                    data.get("wallet", "??????"),
                    data.get("tx_hash", ""),
                    data.get("chain", ""),
                    drop_name=data.get("drop_name", "Mint"),
                    attempt_no=data.get("attempt_no", 1),
                )

            elif event_type == "tx_skipped":
                await sender.notify_tx_skipped(
                    telegram_id,
                    data.get("wallet", "??????"),
                    data.get("reason", "Unknown"),
                    drop_name=data.get("drop_name", "Mint"),
                )

            elif event_type == "tx_confirmed":
                # Rich per-wallet card
                await sender.notify_mint_complete_card(
                    telegram_id=telegram_id,
                    drop_name=data.get("drop_name", "Unknown"),
                    chain=data.get("chain", ""),
                    wallet_address=data.get("wallet", "0x??????"),
                    status=data.get("status", "unknown"),
                    tx_hash=data.get("tx_hash"),
                    token_id=data.get("token_id"),
                    latency_ms=data.get("latency_ms"),
                    error_msg=data.get("error"),
                    attempt_no=data.get("attempt_no", 1),
                    fired_at=fired_at,
                    session_id=data.get("session_id"),
                )

            elif event_type == "mint_complete":
                # Final batch summary
                await sender.notify_mint_results(
                    telegram_id=telegram_id,
                    drop_name=data.get("drop_name", "Unknown"),
                    chain=data.get("chain", ""),
                    results=data.get("results", []),
                    session_id=data.get("session_id"),
                )

            elif event_type == "schedule_changed":
                new_time_str = data.get("new_time")
                new_time = None
                if new_time_str:
                    try:
                        new_time = datetime.fromisoformat(new_time_str.replace("Z", "+00:00"))
                    except ValueError:
                        pass
                await sender.notify_schedule_changed(
                    telegram_id,
                    data.get("drop_name", "Unknown"),
                    new_time,
                    data.get("session_id", 0),
                )

            return web.Response(text="ok")

        except Exception as e:
            logger.error("Notification handler error: %s", e)
            return web.Response(text=str(e), status=500)

    app = web.Application()
    app.add_routes(routes)
    return app
