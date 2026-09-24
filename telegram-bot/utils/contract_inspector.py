"""
utils/contract_inspector.py — On-chain & OpenSea contract inspector and phase detector.

Priority order for phase data:
  1. OpenSea REST Drops API (by collection slug) → accurate named stages + per-stage max
  2. SeaDrop on-chain RPC calls                 → fallback when OpenSea unavailable
  3. Standard EVM fallback                      → for any other contract

Exposed phases to the UI: only "fcfs" and "public".
  • FCFS  = OpenSea stage_type signed_presale / label FCFS, OR on-chain signed window
            that starts meaningfully before public (≥ 60s)
  • Public = public_sale / public stage
"""
import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import httpx

logger = logging.getLogger(__name__)

SEADROP_ROUTER = "0x00005ea00ac477b1030ce78506496e8c2de24bf5"
OPENSEA_SEADROP_SIGNER = "0xfce4b31128100915f2980bbc3a08894ee5e8f8c3"
OPENSEA_API_BASE = "https://api.opensea.io/api/v2"

# On-chain: only treat signed window as FCFS when it opens this many seconds before public
_FCFS_MIN_LEAD_SECS = 60

_CHAIN_OS_SLUG = {
    "ethereum":  "ethereum",
    "base":      "base",
    "polygon":   "matic",
    "arbitrum":  "arbitrum",
    "optimism":  "optimism",
    "avalanche": "avalanche",
    "bsc":       "bsc",
    "zora":      "zora",
    "robinhood": "robinhood",
    "shape":     "shape",
    "abstract":  "abstract",
    "ape":       "apechain",
    "arc":       "arc",
    "hyperevm":  "hyperevm",
    "ink":       "ink",
}

_FCFS_KEYWORDS = ["fcfs", "first come", "first-come"]
_PUBLIC_KEYWORDS = ["public"]
_FCFS_STAGE_TYPES = {"signed_presale", "allowlist", "presale"}
_PUBLIC_STAGE_TYPES = {"public_sale", "public"}

# Module-level cached instant API key (created once if OPENSEA_API_KEY unset)
_cached_api_key: Optional[str] = None
_api_key_lock = asyncio.Lock()


# ── Low-level RPC helper ──────────────────────────────────────────────────────

async def _rpc_call(rpc_url: str, method: str, params: list, timeout: float = 8.0) -> Optional[Any]:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(rpc_url, json=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                if "result" in data:
                    return data["result"]
                if "error" in data:
                    logger.debug("RPC %s error: %s", method, data["error"])
    except Exception as e:
        logger.debug("RPC %s request failed: %s", method, e)
    return None


async def _eth_call(rpc_url: str, to: str, calldata: str) -> Optional[str]:
    res = await _rpc_call(rpc_url, "eth_call", [{"to": to, "data": calldata}, "latest"])
    if isinstance(res, str) and res.startswith("0x") and len(res) > 2:
        return res
    return None


def _decode_string(hex_data: str) -> str:
    """Decodes an ABI-encoded string returned by eth_call."""
    try:
        raw = hex_data.replace("0x", "")
        if len(raw) < 128:
            decoded = bytes.fromhex(raw).decode("utf-8", errors="ignore").rstrip("\x00")
            return decoded.strip()
        offset = int(raw[:64], 16) * 2
        length = int(raw[offset:offset + 64], 16) * 2
        content_hex = raw[offset + 64:offset + 64 + length]
        return bytes.fromhex(content_hex).decode("utf-8", errors="ignore").strip()
    except Exception:
        return ""


def _label_contains(label: str, keywords: list) -> bool:
    low = (label or "").lower()
    return any(k in low for k in keywords)


# ── OpenSea API key ───────────────────────────────────────────────────────────

async def ensure_opensea_api_key() -> str:
    """
    Return a usable OpenSea API key.
    Prefers OPENSEA_API_KEY env; otherwise creates a free instant key (7-day TTL).
    """
    global _cached_api_key
    env_key = os.getenv("OPENSEA_API_KEY", "").strip()
    if env_key:
        return env_key
    if _cached_api_key:
        return _cached_api_key

    async with _api_key_lock:
        if _cached_api_key:
            return _cached_api_key
        env_key = os.getenv("OPENSEA_API_KEY", "").strip()
        if env_key:
            return env_key
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{OPENSEA_API_BASE}/auth/keys",
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "User-Agent": "Mozilla/5.0",
                    },
                )
                if resp.status_code in (200, 201):
                    key = (resp.json() or {}).get("api_key") or ""
                    if key:
                        _cached_api_key = key
                        logger.info("Created OpenSea instant API key (expires ~7 days)")
                        return key
                logger.warning("OpenSea instant API key create failed: %s %s", resp.status_code, resp.text[:200])
        except Exception as e:
            logger.warning("OpenSea instant API key create error: %s", e)
    return ""


def _os_headers(api_key: str) -> Dict[str, str]:
    h = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    if api_key:
        h["X-API-KEY"] = api_key
    return h


# ── OpenSea REST: resolve slug + drop stages ──────────────────────────────────

async def resolve_collection_slug(chain: str, contract_address: str, api_key: str) -> Optional[str]:
    """GET /api/v2/chain/{chain}/contract/{address} → collection slug."""
    os_chain = _CHAIN_OS_SLUG.get(chain, chain)
    url = f"{OPENSEA_API_BASE}/chain/{os_chain}/contract/{contract_address.lower()}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=_os_headers(api_key))
            if resp.status_code != 200:
                logger.debug("OpenSea contract lookup %d for %s", resp.status_code, contract_address[:10])
                return None
            data = resp.json() or {}
            slug = data.get("collection") or data.get("collection_slug")
            if slug:
                logger.info("OpenSea collection slug for %s: %s", contract_address[:10], slug)
            return slug
    except Exception as e:
        logger.debug("OpenSea contract lookup failed: %s", e)
        return None


async def fetch_opensea_drop_by_slug(slug: str, api_key: str) -> Optional[Dict[str, Any]]:
    """GET /api/v2/drops/{slug} → detailed drop with stages."""
    url = f"{OPENSEA_API_BASE}/drops/{slug}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=_os_headers(api_key))
            if resp.status_code != 200:
                logger.debug("OpenSea drop-by-slug %d for %s", resp.status_code, slug)
                return None
            data = resp.json() or {}
            stages = data.get("stages") or []
            logger.info("OpenSea drop '%s': %d stages", slug, len(stages))
            return data
    except Exception as e:
        logger.debug("OpenSea drop-by-slug failed for %s: %s", slug, e)
        return None


def _parse_wei_price(price_raw: Any) -> tuple:
    """Return (price_wei:int, price_eth:str) from OpenSea wei decimal string or nested price."""
    try:
        if isinstance(price_raw, dict):
            unit = price_raw.get("unit") or price_raw.get("amount") or "0"
            # unit may already be ETH float string from old API
            if "." in str(unit):
                price_eth_str = str(float(unit))
                price_wei = int(float(unit) * 1e18)
                return price_wei, price_eth_str
            price_wei = int(unit)
        else:
            price_wei = int(price_raw or 0)
        price_eth = f"{price_wei / 1e18:.6f}".rstrip("0").rstrip(".") if price_wei > 0 else "0"
        return price_wei, price_eth
    except Exception:
        return 0, "0"


def _parse_max_per_wallet(stage: Dict[str, Any]) -> int:
    for key in ("max_per_wallet", "maxMintsPerWallet", "max_total_mintable_by_wallet", "limit"):
        raw = stage.get(key)
        if raw is None or raw == "":
            continue
        try:
            val = int(str(raw).strip())
            if val > 0:
                return val
        except Exception:
            continue
    return 0  # unknown — UI will ask user to enter manually


def _parse_os_stages(stages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Parse OpenSea DropStageResponse list into internal phase format.
    Only returns 'fcfs' and 'public'.
    """
    public_stages = []
    fcfs_stages = []

    for s in stages:
        label = s.get("label") or s.get("stageName") or ""
        stage_type = (s.get("stage_type") or s.get("mintType") or "").lower()
        start_raw = s.get("start_time") or s.get("startTime")
        end_raw = s.get("end_time") or s.get("endTime")

        start_dt = None
        end_dt = None
        if start_raw:
            try:
                start_dt = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00")).astimezone(timezone.utc)
            except Exception:
                pass
        if end_raw:
            try:
                end_dt = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00")).astimezone(timezone.utc)
            except Exception:
                pass

        price_wei, price_eth = _parse_wei_price(s.get("price"))
        max_per_wallet = _parse_max_per_wallet(s)

        entry = {
            "label": label,
            "stage_type": stage_type,
            "start_time": start_dt,
            "end_time": end_dt,
            "price_wei": price_wei,
            "price_eth": price_eth,
            "max_per_wallet": max_per_wallet,
        }

        is_public = stage_type in _PUBLIC_STAGE_TYPES or _label_contains(label, _PUBLIC_KEYWORDS)
        is_fcfs = (
            stage_type in _FCFS_STAGE_TYPES
            or _label_contains(label, _FCFS_KEYWORDS)
        )
        # signed_presale labeled something other than GTD/OG still counts as FCFS for our bot
        if is_public and not _label_contains(label, _FCFS_KEYWORDS):
            public_stages.append(entry)
        elif is_fcfs:
            fcfs_stages.append(entry)
        # GTD / OG / Treasury without FCFS keywords are ignored

    fcfs_stages.sort(key=lambda x: x["start_time"].timestamp() if x["start_time"] else 0)
    public_stages.sort(key=lambda x: x["start_time"].timestamp() if x["start_time"] else 0)

    phases: List[Dict[str, Any]] = []

    if fcfs_stages:
        # Prefer the earliest upcoming / latest labeled FCFS (usually the active allowlist before public)
        fcfs_stage = fcfs_stages[-1]
        phases.append({
            "phase_id":        "fcfs",
            "phase_name":      "⚡ FCFS Phase (First-Come, First-Served)",
            "phase_badge":     "FCFS Allowlist",
            "mint_function":   "mintSigned(address,address,address,uint256,(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool),uint256,bytes)",
            "start_time":      fcfs_stage["start_time"],
            "end_time":        fcfs_stage["end_time"],
            "price_wei":       fcfs_stage["price_wei"],
            "price_eth":       fcfs_stage["price_eth"],
            "max_per_wallet":  fcfs_stage["max_per_wallet"],
            "eligible":        True,
            "eligibility_note": "FCFS — wallet must be allowlisted by the drop creator",
        })

    if public_stages:
        pub = public_stages[0]
        phases.append({
            "phase_id":        "public",
            "phase_name":      "🌐 Public Mint",
            "phase_badge":     "Public (All)",
            "mint_function":   "mintPublic(address,address,address,uint256)",
            "start_time":      pub["start_time"],
            "end_time":        pub["end_time"],
            "price_wei":       pub["price_wei"],
            "price_eth":       pub["price_eth"],
            "max_per_wallet":  pub["max_per_wallet"],
            "eligible":        True,
            "eligibility_note": "Open to all wallets",
        })

    return phases


# ── SeaDrop on-chain fallback ─────────────────────────────────────────────────

async def _fetch_seadrop_phases_onchain(
    rpc_url: str,
    contract_address: str,
    max_supply: int,
) -> List[Dict[str, Any]]:
    """
    On-chain SeaDrop fallback when OpenSea API is unavailable.
    Public from getPublicDrop; FCFS only when signed window starts ≥60s before public.
    """
    clean_contract = contract_address.lower().replace("0x", "").zfill(64)
    clean_signer = OPENSEA_SEADROP_SIGNER.lower().replace("0x", "").zfill(64)
    router = SEADROP_ROUTER

    pub_res, signed_res = await asyncio.gather(
        _eth_call(rpc_url, router, "0xbc6a629c" + clean_contract),
        _eth_call(rpc_url, router, "0x81bf9af3" + clean_contract + clean_signer),
    )

    phases: List[Dict[str, Any]] = []
    pub_start = 0
    pub_end = 0
    pub_price = 0
    pub_max = 10

    if isinstance(pub_res, str) and len(pub_res) >= 130:
        raw = pub_res.replace("0x", "")
        chunks = [raw[i:i + 64] for i in range(0, len(raw), 64)]
        if len(chunks) >= 4:
            pub_price = int(chunks[0], 16)
            pub_start = int(chunks[1], 16)
            pub_end = int(chunks[2], 16)
            pub_max = int(chunks[3], 16) if len(chunks) > 3 else 10

            if pub_start > 0:
                start_dt = datetime.fromtimestamp(pub_start, timezone.utc)
                end_dt = datetime.fromtimestamp(pub_end, timezone.utc) if pub_end > 0 else None
                price_eth = f"{pub_price / 1e18:.4f}".rstrip("0").rstrip(".") if pub_price > 0 else "0"
                phases.append({
                    "phase_id":        "public",
                    "phase_name":      "🌐 Public Mint",
                    "phase_badge":     "Public (All)",
                    "mint_function":   "mintPublic(address,address,address,uint256)",
                    "start_time":      start_dt,
                    "end_time":        end_dt,
                    "price_wei":       pub_price,
                    "price_eth":       price_eth,
                    "max_per_wallet":  pub_max if pub_max > 0 else 0,
                    "eligible":        True,
                    "eligibility_note": "Open to all wallets",
                })

    # Distinct FCFS window: signed minStart meaningfully before public
    if isinstance(signed_res, str) and len(signed_res) >= 194:
        raw = signed_res.replace("0x", "")
        chunks = [raw[i:i + 64] for i in range(0, len(raw), 64)]
        # SignedMintValidationParams (each field 32-byte ABI slot):
        # [0] minUnitPrice [1] maxUnitPrice [2] minStartTime [3] maxStartTime
        # [4] maxEndTime [5] maxMaxTotalMintableByWallet ...
        if len(chunks) >= 6:
            signed_start = int(chunks[2], 16)
            signed_end = int(chunks[3], 16)
            # Prefer end from maxEndTime if it looks like a timestamp
            if len(chunks) > 4:
                maybe_end = int(chunks[4], 16)
                if maybe_end > 1_600_000_000:
                    signed_end = maybe_end
            signed_max = int(chunks[5], 16) if len(chunks) > 5 else 0
            # Guard: OpenSea often sets validation max to a huge ceiling — don't treat as stage max
            if signed_max > 100:
                signed_max = 0

            if signed_start > 0 and pub_start > 0 and (pub_start - signed_start) >= _FCFS_MIN_LEAD_SECS:
                start_dt = datetime.fromtimestamp(signed_start, timezone.utc)
                end_dt = datetime.fromtimestamp(signed_end, timezone.utc) if signed_end > 0 else None
                price_eth = f"{pub_price / 1e18:.4f}".rstrip("0").rstrip(".") if pub_price > 0 else "0"
                phases.insert(0, {
                    "phase_id":        "fcfs",
                    "phase_name":      "⚡ FCFS Phase (First-Come, First-Served)",
                    "phase_badge":     "FCFS Allowlist",
                    "mint_function":   "mintSigned(address,address,address,uint256,(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool),uint256,bytes)",
                    "start_time":      start_dt,
                    "end_time":        end_dt,
                    "price_wei":       pub_price,
                    "price_eth":       price_eth,
                    "max_per_wallet":  signed_max,  # 0 = unknown → user enters manually
                    "eligible":        True,
                    "eligibility_note": "FCFS — wallet must be allowlisted (on-chain window; enter qty manually if limit unknown)",
                })
                logger.info(
                    "On-chain FCFS for %s: starts %ds before public",
                    contract_address[:10],
                    pub_start - signed_start,
                )
            elif signed_start > 0:
                logger.info(
                    "On-chain SeaDrop for %s: signed window not distinct from public (lead=%ss) — FCFS omitted",
                    contract_address[:10],
                    pub_start - signed_start if pub_start else "n/a",
                )

    return phases


# ── Main entry point ──────────────────────────────────────────────────────────

async def inspect_contract_phases(
    rpc_url: str,
    contract_address: str,
    wallet_addresses: List[str],
    chain: str = "ethereum",
) -> Dict[str, Any]:
    """
    Inspects a contract and returns phase data. Only exposes FCFS and Public phases.

    Also returns collection_slug when resolved via OpenSea (needed for FCFS mintSigned).
    """
    target = contract_address.lower()

    async def _get_metadata():
        name_call, sym_call, max_sup_call = await asyncio.gather(
            _eth_call(rpc_url, target, "0x06fdde03"),   # name()
            _eth_call(rpc_url, target, "0x95d89b41"),   # symbol()
            _eth_call(rpc_url, target, "0xd5abeb01"),   # maxSupply()
            return_exceptions=True,
        )
        name = _decode_string(name_call) if isinstance(name_call, str) else ""
        symbol = _decode_string(sym_call) if isinstance(sym_call, str) else ""
        max_supply = 0
        if isinstance(max_sup_call, str) and len(max_sup_call) >= 10:
            try:
                max_supply = int(max_sup_call, 16)
            except Exception:
                pass
        return name, symbol, max_supply

    api_key = await ensure_opensea_api_key()
    (contract_name, contract_symbol, max_supply), slug = await asyncio.gather(
        _get_metadata(),
        resolve_collection_slug(chain, contract_address, api_key) if api_key else asyncio.sleep(0, result=None),
    )

    phases: List[Dict[str, Any]] = []
    is_seadrop = False
    collection_slug = slug or ""

    if collection_slug and api_key:
        drop = await fetch_opensea_drop_by_slug(collection_slug, api_key)
        if drop:
            stages = drop.get("stages") or []
            phases = _parse_os_stages(stages)
            is_seadrop = len(phases) > 0 or (drop.get("drop_type") or "").startswith("seadrop")
            if drop.get("collection_name") and not contract_name:
                contract_name = drop["collection_name"]
            logger.info(
                "Using OpenSea drop stages for %s (slug=%s, phases=%s)",
                contract_address[:10],
                collection_slug,
                [p["phase_id"] for p in phases],
            )

    if not phases:
        logger.info("Falling back to on-chain SeaDrop RPC for %s", contract_address[:10])
        phases = await _fetch_seadrop_phases_onchain(rpc_url, contract_address, max_supply)
        is_seadrop = len(phases) > 0

    if not phases:
        price_wei = 0
        price_call = await _eth_call(rpc_url, target, "0x6352211e")  # cost()
        if not price_call:
            price_call = await _eth_call(rpc_url, target, "0xa035b1fe")  # price()
        if isinstance(price_call, str) and len(price_call) >= 10:
            try:
                price_wei = int(price_call, 16)
            except Exception:
                price_wei = 0
        price_eth = f"{price_wei / 1e18:.4f}".rstrip("0").rstrip(".") if price_wei > 0 else "0"
        phases.append({
            "phase_id":        "public",
            "phase_name":      "🌐 Public Mint",
            "phase_badge":     "Public",
            "mint_function":   "mint(uint256)",
            "start_time":      None,
            "end_time":        None,
            "price_wei":       price_wei,
            "price_eth":       price_eth,
            "max_per_wallet":  0,
            "eligible":        True,
            "eligibility_note": "Direct on-chain contract mint",
        })

    def _sort_key(p):
        t = p.get("start_time")
        if t is None:
            return 0
        return t.timestamp()

    phases.sort(key=_sort_key)

    return {
        "name":             contract_name or "NFT Contract",
        "symbol":           contract_symbol or "",
        "is_seadrop":       is_seadrop,
        "collection_slug":  collection_slug,
        "phases":           phases,
    }
