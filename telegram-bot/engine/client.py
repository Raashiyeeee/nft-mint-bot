"""
engine/client.py — Async JSON-RPC client for the Rust engine on port 7878.
"""
import aiohttp
import os
from typing import Any, Optional


class EngineClient:
    def __init__(self, url: Optional[str] = None):
        self.url = url or os.environ.get("RUST_ENGINE_URL", "http://127.0.0.1:7878")
        self._session: Optional[aiohttp.ClientSession] = None
        self._id_counter = 0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=10),
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    async def call(self, method: str, params: Any = None) -> Any:
        """Make a JSON-RPC call. Raises on error."""
        session = await self._get_session()
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": self._next_id(),
        }
        async with session.post(self.url, json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
            if "error" in data and data["error"]:
                raise RuntimeError(f"Engine error: {data['error']['message']}")
            return data.get("result")

    async def schedule_mint(self, session_id: int, telegram_id: int) -> bool:
        return await self.call("schedule_mint", {
            "session_id": session_id,
            "telegram_id": telegram_id,
        })

    async def cancel_mint(self, session_id: int, telegram_id: int) -> bool:
        return await self.call("cancel_mint", {
            "session_id": session_id,
            "telegram_id": telegram_id,
        })

    async def get_drop_info(self, slug: str, cookies: str = "") -> dict:
        return await self.call("get_drop_info", {
            "slug": slug,
            "cookies": cookies,
        })

    async def get_status(self) -> dict:
        return await self.call("status")

    async def ping_pipeline(self) -> dict:
        return await self.call("ping_pipeline")

    async def get_session_results(self, session_id: int) -> list:
        return await self.call("get_session_results", {"session_id": session_id})

    async def get_wallet_balance(self, wallet_address: str, chain: str) -> Optional[float]:
        """Returns wallet ETH balance as float, or None if engine can't fetch it."""
        try:
            result = await self.call("get_wallet_balance", {
                "wallet_address": wallet_address,
                "chain": chain,
            })
            if result is not None:
                return float(result)
            return None
        except Exception:
            return None

    async def cancel_session(self, session_id: int) -> bool:
        """Signal the engine to stop processing a session."""
        try:
            return await self.call("cancel_session", {"session_id": session_id})
        except Exception:
            return False


    async def health_check(self) -> bool:
        """Quick health check against the Rust engine."""
        try:
            session = await self._get_session()
            async with session.get(f"{self.url.rstrip('/')}/health", timeout=aiohttp.ClientTimeout(total=3)) as resp:
                return resp.status == 200
        except Exception:
            return False


# Singleton
_engine_client: Optional[EngineClient] = None


def get_engine_client() -> EngineClient:
    global _engine_client
    if _engine_client is None:
        _engine_client = EngineClient()
    return _engine_client
