"""
utils/balance_check.py — Pre-mint wallet balance + gas affordability checks.

Used by the /mint wizard after mint price (and again after quantity) so the
bot only advances when every selected wallet can cover:
  (price_per_nft × qty) + estimated gas
in the chain's native gas token (ETH, USDC on Arc, APE, etc.).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import aiohttp

from engine.client import get_engine_client
from utils.formatters import (
    CHAIN_DEFAULT_RPCS,
    CHAIN_DISPLAY,
    get_chain_currency,
    mask_address,
)

logger = logging.getLogger(__name__)

# Conservative fallback max-fee (gwei) when live fee fetch fails.
# Kept low for L2s so we don't over-require funds on Robinhood/Base/etc.
_FALLBACK_GAS_GWEI: Dict[str, float] = {
    "ethereum": 40.0,
    "base": 0.05,
    "polygon": 50.0,
    "arbitrum": 0.1,
    "optimism": 0.05,
    "avalanche": 25.0,
    "bsc": 3.0,
    "zora": 0.05,
    "robinhood": 0.1,
    "shape": 0.05,
    "abstract": 0.05,
    "ape": 5.0,
    "arc": 0.1,
    "hyperevm": 0.1,
    "ink": 0.05,
}

_DEFAULT_GAS_LIMIT = 200_000
_SEADROP_GAS_LIMIT = 180_000
_GAS_BUFFER = 1.30  # 30% headroom for fee spikes / multiplier


def default_gas_limit(is_seadrop: bool = False) -> int:
    return _SEADROP_GAS_LIMIT if is_seadrop else _DEFAULT_GAS_LIMIT


async def _rpc_call(
    session: aiohttp.ClientSession,
    rpc_url: str,
    method: str,
    params: list,
) -> Optional[Any]:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    try:
        async with session.post(
            rpc_url,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            if data.get("error"):
                return None
            return data.get("result")
    except Exception as e:
        logger.debug("RPC %s failed on %s: %s", method, rpc_url[:40], e)
        return None


async def fetch_native_balance(
    address: str,
    chain: str,
    rpc_url: Optional[str] = None,
) -> Optional[float]:
    """Native gas-token balance in human units (ETH/USDC/…)."""
    engine = get_engine_client()
    try:
        bal = await engine.get_wallet_balance(address, chain)
        if bal is not None:
            return float(bal)
    except Exception:
        pass

    url = rpc_url or CHAIN_DEFAULT_RPCS.get(chain)
    if not url:
        return None

    async with aiohttp.ClientSession() as session:
        result = await _rpc_call(session, url, "eth_getBalance", [address, "latest"])
        if not result:
            return None
        try:
            return int(result, 16) / 1e18
        except (TypeError, ValueError):
            return None


async def estimate_gas_cost_native(
    chain: str,
    rpc_url: Optional[str] = None,
    gas_limit: Optional[int] = None,
) -> float:
    """
    Estimate max gas spend in native token units for one mint tx.
    Uses live EIP-1559 fees (or eth_gasPrice), with chain-aware fallbacks.
    """
    gl = gas_limit or _DEFAULT_GAS_LIMIT
    url = rpc_url or CHAIN_DEFAULT_RPCS.get(chain, "")
    max_fee_wei: Optional[int] = None

    if url:
        async with aiohttp.ClientSession() as session:
            # Prefer feeHistory base + tip (matches engine path)
            hist = await _rpc_call(
                session, url, "eth_feeHistory", [1, "latest", [50.0]]
            )
            if isinstance(hist, dict):
                try:
                    base_hex = hist["baseFeePerGas"][-1]
                    base = int(base_hex, 16)
                    tip = max(base // 10, 1_000_000)  # ≥ 0.001 gwei
                    tip_res = await _rpc_call(
                        session, url, "eth_maxPriorityFeePerGas", []
                    )
                    if tip_res:
                        tip = max(int(tip_res, 16), 1)
                    # +25% base buffer (same idea as rust-engine hot path)
                    max_fee_wei = (base * 5 // 4) + tip
                except (KeyError, IndexError, TypeError, ValueError):
                    max_fee_wei = None

            if max_fee_wei is None:
                gp = await _rpc_call(session, url, "eth_gasPrice", [])
                if gp:
                    try:
                        max_fee_wei = int(gp, 16)
                    except (TypeError, ValueError):
                        max_fee_wei = None

    if max_fee_wei is None or max_fee_wei <= 0:
        gwei = _FALLBACK_GAS_GWEI.get(chain, 5.0)
        max_fee_wei = int(gwei * 1e9)

    cost_wei = int(gl) * int(max_fee_wei)
    return (cost_wei / 1e18) * _GAS_BUFFER


def _wallet_label(wallet: dict) -> str:
    return wallet.get("wallet_label") or mask_address(wallet.get("wallet_address", ""))


async def validate_selected_wallets_funds(
    context_data: dict,
    price_per_nft: float,
    quantity: int = 1,
) -> Dict[str, Any]:
    """
    Check every selected mint wallet has balance >= mint cost + estimated gas.

    Returns:
      {
        ok, currency, chain, chain_name,
        price_per_nft, quantity, mint_cost, gas_est, required,
        wallets: [{label, address, balance, shortfall, ok, offline}, ...],
        shortfalls: [...],
        offline: [...],
      }
    """
    chain = context_data.get("mint_chain", "ethereum")
    currency = get_chain_currency(chain)
    chain_name = CHAIN_DISPLAY.get(chain, chain)
    rpc_url = context_data.get("mint_rpc") or CHAIN_DEFAULT_RPCS.get(chain)
    is_seadrop = bool(context_data.get("mint_is_seadrop"))
    gas_limit = context_data.get("mint_gas_limit") or default_gas_limit(is_seadrop)

    qty = max(int(quantity or 1), 1)
    price = max(float(price_per_nft or 0), 0.0)
    mint_cost = price * qty
    gas_est = await estimate_gas_cost_native(chain, rpc_url, int(gas_limit))
    required = mint_cost + gas_est

    all_wallets = context_data.get("all_wallets", {})
    selected_ids = context_data.get("mint_selected_wallet_ids", [])

    wallets_to_check: List[tuple] = []
    for wid in selected_ids:
        w = all_wallets.get(wid)
        if w is None and str(wid).isdigit():
            w = all_wallets.get(int(wid))
        if not w:
            continue
        wallets_to_check.append((wid, w))

    results: List[dict] = []
    shortfalls: List[dict] = []
    offline: List[dict] = []

    if wallets_to_check:
        balances = await asyncio.gather(*[
            fetch_native_balance(w["wallet_address"], chain, rpc_url)
            for _, w in wallets_to_check
        ])
        for (wid, w), bal in zip(wallets_to_check, balances):
            addr = w["wallet_address"]
            label = _wallet_label(w)
            entry = {
                "id": wid,
                "label": label,
                "address": addr,
                "balance": bal,
                "shortfall": 0.0,
                "ok": False,
                "offline": bal is None,
            }
            if bal is None:
                entry["shortfall"] = required
                offline.append(entry)
            elif bal + 1e-12 < required:
                entry["shortfall"] = required - bal
                shortfalls.append(entry)
            else:
                entry["ok"] = True
            results.append(entry)

    ok = bool(results) and not shortfalls and not offline
    return {
        "ok": ok,
        "currency": currency,
        "chain": chain,
        "chain_name": chain_name,
        "price_per_nft": price,
        "quantity": qty,
        "mint_cost": mint_cost,
        "gas_est": gas_est,
        "required": required,
        "wallets": results,
        "shortfalls": shortfalls,
        "offline": offline,
        "no_wallets": not bool(wallets_to_check),
    }


def format_funds_ok(check: Dict[str, Any]) -> str:
    cur = check["currency"]
    return (
        f"✅ Funds check passed on *{check['chain_name']}*\n"
        f"Required ≈ `{check['required']:.6f} {cur}` / wallet "
        f"(mint `{check['mint_cost']:.6f}` + gas ≈ `{check['gas_est']:.6f}`)"
    )


def format_insufficient_funds(check: Dict[str, Any]) -> str:
    cur = check["currency"]

    if check.get("no_wallets"):
        return (
            "⚠️ *No wallets selected*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Go back and select at least one wallet to mint with."
        )

    mint_label = (
        f"*Free*" if check["price_per_nft"] <= 0
        else f"`{check['price_per_nft']}` {cur}"
    )
    lines = [
        "⚠️ *Insufficient funds to mint*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"⛓ Network: *{check['chain_name']}* ({cur})",
        f"💵 Mint: {mint_label} × {check['quantity']} "
        f"= `{check['mint_cost']:.6f} {cur}`",
        f"⛽ Est. gas: ≈ `{check['gas_est']:.6f} {cur}`",
        f"📋 *Required per wallet:* ≈ `{check['required']:.6f} {cur}`",
        "",
    ]

    if check["price_per_nft"] <= 0:
        lines.append(
            f"_Free mint — you still need *{cur}* gas on "
            f"{check['chain_name']} to cover the tx._\n"
        )

    for w in check["shortfalls"]:
        bal = w["balance"] if w["balance"] is not None else 0.0
        lines.append(
            f"❌ *{w['label']}* (`{mask_address(w['address'])}`)\n"
            f"   Balance: `{bal:.6f} {cur}`\n"
            f"   Top up: ≈ `{w['shortfall']:.6f} {cur}`"
        )

    for w in check["offline"]:
        lines.append(
            f"❓ *{w['label']}* (`{mask_address(w['address'])}`)\n"
            f"   Could not fetch balance — check RPC / try again"
        )

    lines.extend([
        "",
        f"Fund the selected wallet(s) with enough *{cur}* "
        f"(mint price + gas), then tap *Recheck*.",
    ])
    return "\n".join(lines)
