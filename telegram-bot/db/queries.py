"""
db/queries.py — All asyncpg database queries.
All queries use parameterized statements — no string interpolation anywhere.
"""
from __future__ import annotations

import asyncpg
from datetime import datetime, timezone
from typing import Optional


# ── Connection pool ──────────────────────────────────────────────────────────

_pool: Optional[asyncpg.Pool] = None


async def init_pool(database_url: str) -> None:
    global _pool
    _pool = await asyncpg.create_pool(database_url, min_size=2, max_size=10)


async def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialized. Call init_pool() first.")
    return _pool


async def close_pool() -> None:
    if _pool:
        await _pool.close()


# ── Access control ───────────────────────────────────────────────────────────

async def get_authorized_user(telegram_id: int) -> Optional[asyncpg.Record]:
    """Return user record if active and not expired, else None."""
    pool = await get_pool()
    return await pool.fetchrow(
        """
        SELECT * FROM authorized_users
        WHERE telegram_id = $1
          AND is_active = TRUE
          AND (expires_at IS NULL OR expires_at > NOW())
        """,
        telegram_id,
    )


async def is_admin(telegram_id: int) -> bool:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT 1 FROM admin_users WHERE telegram_id = $1",
        telegram_id,
    )
    return row is not None


async def log_denied_access(
    telegram_id: int,
    username: Optional[str],
    command_used: Optional[str],
    reason: str = "not_authorized",
) -> None:
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO denied_access_log (telegram_id, username, command_used, reason)
        VALUES ($1, $2, $3, $4)
        """,
        telegram_id, username, command_used, reason,
    )


# ── Rate limiting ─────────────────────────────────────────────────────────────

async def check_and_increment_rate(telegram_id: int, max_per_minute: int = 10) -> bool:
    """Returns True if within rate limit, False if exceeded."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Truncate to current minute (UTC naive for PostgreSQL TIMESTAMP)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0, tzinfo=None)
        row = await conn.fetchrow(
            """
            INSERT INTO rate_limit_log (telegram_id, window_start, command_count)
            VALUES ($1, $2, 1)
            ON CONFLICT (telegram_id, window_start)
            DO UPDATE SET command_count = rate_limit_log.command_count + 1
            RETURNING command_count
            """,
            telegram_id, now,
        )
        return row["command_count"] <= max_per_minute


def _to_naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


# ── Timezone preference ───────────────────────────────────────────────────────

async def get_user_timezone(telegram_id: int) -> str:
    """Return the user's IANA timezone preference (default 'UTC')."""
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT timezone_pref FROM authorized_users WHERE telegram_id = $1",
        telegram_id,
    )
    if row and row["timezone_pref"]:
        return row["timezone_pref"]
    return "UTC"


async def set_user_timezone(telegram_id: int, tz_name: str) -> None:
    """Persist the user's IANA timezone preference."""
    pool = await get_pool()
    await pool.execute(
        "UPDATE authorized_users SET timezone_pref = $1 WHERE telegram_id = $2",
        tz_name, telegram_id,
    )


# ── Admin: user management ────────────────────────────────────────────────────

async def add_authorized_user(
    telegram_id: int,
    telegram_username: Optional[str],
    display_name: Optional[str],
    added_by: int,
    expires_at: Optional[datetime],
) -> None:
    pool = await get_pool()
    expires_at = _to_naive_utc(expires_at)
    await pool.execute(
        """
        INSERT INTO authorized_users
            (telegram_id, telegram_username, display_name, added_by, expires_at)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (telegram_id) DO UPDATE
            SET is_active = TRUE,
                telegram_username = EXCLUDED.telegram_username,
                expires_at = EXCLUDED.expires_at
        """,
        telegram_id, telegram_username, display_name, added_by, expires_at,
    )


async def remove_authorized_user(telegram_id: int) -> bool:
    pool = await get_pool()
    result = await pool.execute(
        "UPDATE authorized_users SET is_active = FALSE WHERE telegram_id = $1",
        telegram_id,
    )
    return result != "UPDATE 0"


async def list_authorized_users(offset: int = 0, limit: int = 10) -> list[asyncpg.Record]:
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT
            u.telegram_id,
            u.telegram_username,
            u.display_name,
            u.is_active,
            u.expires_at,
            u.added_at,
            COUNT(w.id) FILTER (WHERE w.is_active) AS wallet_count
        FROM authorized_users u
        LEFT JOIN user_wallets w ON w.telegram_id = u.telegram_id
        GROUP BY u.id
        ORDER BY u.added_at DESC
        LIMIT $1 OFFSET $2
        """,
        limit, offset,
    )


async def count_authorized_users() -> int:
    pool = await get_pool()
    return await pool.fetchval("SELECT COUNT(*) FROM authorized_users")


# ── Admin: promote/revoke admin ───────────────────────────────────────────────

async def make_admin(telegram_id: int) -> None:
    pool = await get_pool()
    await pool.execute(
        "INSERT INTO admin_users (telegram_id) VALUES ($1) ON CONFLICT DO NOTHING",
        telegram_id,
    )


async def revoke_admin(telegram_id: int) -> bool:
    pool = await get_pool()
    result = await pool.execute(
        "DELETE FROM admin_users WHERE telegram_id = $1",
        telegram_id,
    )
    return result != "DELETE 0"


async def revoke_non_owner_admins(owner_id: int) -> int:
    """Demote all admin_users rows that are NOT the owner to regular user.
    Returns number of rows removed."""
    pool = await get_pool()
    result = await pool.execute(
        "DELETE FROM admin_users WHERE telegram_id != $1",
        owner_id,
    )
    # result is like 'DELETE N'
    try:
        return int(result.split()[1])
    except Exception:
        return 0


async def list_authorized_users_with_roles(
    offset: int = 0, limit: int = 10
) -> list[asyncpg.Record]:
    """Returns users with is_admin flag based on admin_users table."""
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT
            u.telegram_id,
            u.telegram_username,
            u.display_name,
            u.is_active,
            u.expires_at,
            u.added_at,
            COUNT(w.id) FILTER (WHERE w.is_active) AS wallet_count,
            (a.telegram_id IS NOT NULL)             AS is_admin
        FROM authorized_users u
        LEFT JOIN user_wallets w ON w.telegram_id = u.telegram_id
        LEFT JOIN admin_users  a ON a.telegram_id = u.telegram_id
        GROUP BY u.id, a.telegram_id
        ORDER BY u.added_at DESC
        LIMIT $1 OFFSET $2
        """,
        limit, offset,
    )


async def get_authorized_user_with_role(telegram_id: int) -> Optional[asyncpg.Record]:
    """Returns a single authorized user record with is_admin flag."""
    pool = await get_pool()
    return await pool.fetchrow(
        """
        SELECT
            u.telegram_id,
            u.telegram_username,
            u.display_name,
            u.is_active,
            u.expires_at,
            u.added_at,
            COUNT(w.id) FILTER (WHERE w.is_active) AS wallet_count,
            (a.telegram_id IS NOT NULL)             AS is_admin
        FROM authorized_users u
        LEFT JOIN user_wallets w ON w.telegram_id = u.telegram_id
        LEFT JOIN admin_users  a ON a.telegram_id = u.telegram_id
        WHERE u.telegram_id = $1
        GROUP BY u.id, a.telegram_id
        """,
        telegram_id,
    )


# ── Wallet management ─────────────────────────────────────────────────────────

async def add_wallet(
    telegram_id: int,
    wallet_address: str,
    wallet_label: Optional[str],
    chain: str = "evm",
    private_key_encrypted: str = "",
) -> int:
    """Returns new wallet id or updates existing."""
    pool = await get_pool()
    existing = await pool.fetchrow(
        "SELECT id FROM user_wallets WHERE telegram_id = $1 AND wallet_address = $2",
        telegram_id, wallet_address.lower(),
    )
    if existing:
        await pool.execute(
            """
            UPDATE user_wallets
            SET wallet_label = COALESCE($1, wallet_label),
                private_key_encrypted = $2,
                chain = $3,
                is_active = TRUE
            WHERE id = $4
            """,
            wallet_label, private_key_encrypted, chain, existing["id"],
        )
        return existing["id"]
    return await pool.fetchval(
        """
        INSERT INTO user_wallets
            (telegram_id, wallet_address, wallet_label, chain, private_key_encrypted)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id
        """,
        telegram_id, wallet_address.lower(), wallet_label, chain, private_key_encrypted,
    )


async def update_wallets_chain(wallet_ids: list[int], chain: str) -> None:
    """Updates the chain for selected wallets during mint time."""
    if not wallet_ids:
        return
    pool = await get_pool()
    await pool.execute(
        "UPDATE user_wallets SET chain = $1 WHERE id = ANY($2::int[])",
        chain, wallet_ids,
    )


async def remove_wallet(telegram_id: int, wallet_address: str) -> bool:
    pool = await get_pool()
    result = await pool.execute(
        """
        DELETE FROM user_wallets
        WHERE telegram_id = $1 AND wallet_address = $2
        """,
        telegram_id, wallet_address.lower(),
    )
    return result != "DELETE 0"


async def remove_wallet_by_id(telegram_id: int, wallet_id: int) -> bool:
    """Permanently delete a selected wallet and its private key for a user."""
    pool = await get_pool()
    result = await pool.execute(
        """
        DELETE FROM user_wallets
        WHERE id = $1 AND telegram_id = $2
        """,
        wallet_id, telegram_id,
    )
    return result != "DELETE 0"


async def get_wallet_by_id(telegram_id: int, wallet_id: int) -> Optional[asyncpg.Record]:
    """Retrieve an active wallet by its ID for a specific user."""
    pool = await get_pool()
    return await pool.fetchrow(
        """
        SELECT id, wallet_address, wallet_label, chain, is_active
        FROM user_wallets
        WHERE id = $1 AND telegram_id = $2 AND is_active = TRUE
        """,
        wallet_id, telegram_id,
    )


async def get_user_wallets(telegram_id: int, chain: Optional[str] = None) -> list[asyncpg.Record]:
    pool = await get_pool()
    if chain:
        return await pool.fetch(
            """
            SELECT id, wallet_address, wallet_label, chain, is_active
            FROM user_wallets
            WHERE telegram_id = $1 AND chain = $2 AND is_active = TRUE
            ORDER BY added_at
            """,
            telegram_id, chain,
        )
    return await pool.fetch(
        """
        SELECT id, wallet_address, wallet_label, chain, is_active
        FROM user_wallets
        WHERE telegram_id = $1 AND is_active = TRUE
        ORDER BY chain, added_at
        """,
        telegram_id,
    )


async def get_wallet_encrypted_key(wallet_id: int) -> Optional[str]:
    """Returns encrypted private key for a wallet (decrypted in-memory by crypto.py)."""
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT private_key_encrypted FROM user_wallets WHERE id = $1 AND is_active = TRUE",
        wallet_id,
    )
    return row["private_key_encrypted"] if row else None


async def get_wallet_encrypted_keys_for_session(session_id: int) -> list[asyncpg.Record]:
    """Returns wallet address + encrypted key for all wallets in a session."""
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT w.id, w.wallet_address, w.chain, w.private_key_encrypted
        FROM session_wallets sw
        JOIN user_wallets w ON w.id = sw.wallet_id
        WHERE sw.session_id = $1 AND w.is_active = TRUE
        """,
        session_id,
    )


async def wallet_address_exists(telegram_id: int, wallet_address: str, chain: Optional[str] = None) -> bool:
    """Check if a wallet address is already registered for this user."""
    pool = await get_pool()
    if chain:
        row = await pool.fetchrow(
            """
            SELECT 1 FROM user_wallets
            WHERE telegram_id = $1 AND wallet_address = $2 AND chain = $3 AND is_active = TRUE
            """,
            telegram_id, wallet_address.lower(), chain,
        )
    else:
        row = await pool.fetchrow(
            """
            SELECT 1 FROM user_wallets
            WHERE telegram_id = $1 AND wallet_address = $2 AND is_active = TRUE
            """,
            telegram_id, wallet_address.lower(),
        )
    return row is not None


# ── Mint sessions ─────────────────────────────────────────────────────────────

async def create_mint_session(
    telegram_id: int,
    drop_slug: str,
    drop_name: Optional[str],
    nft_contract: str,
    chain: str,
    quantity_per_wallet: int,
    gas_multiplier: float,
    scheduled_at: Optional[datetime],
) -> int:
    """Legacy create (used by old flow). Creates a mint session and returns its ID."""
    pool = await get_pool()
    scheduled_at = _to_naive_utc(scheduled_at)
    return await pool.fetchval(
        """
        INSERT INTO mint_sessions
            (telegram_id, drop_slug, drop_name, nft_contract, chain,
             quantity_per_wallet, gas_multiplier, scheduled_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING id
        """,
        telegram_id, drop_slug, drop_name, nft_contract, chain,
        quantity_per_wallet, gas_multiplier, scheduled_at,
    )


async def create_mint_session_v2(
    telegram_id: int,
    nft_contract: str,
    chain: str,
    rpc_url: Optional[str],
    chain_id: Optional[int],
    mint_function: str,
    mint_price_wei: int,
    quantity_per_wallet: int,
    max_per_wallet: int,
    gas_limit: Optional[int],
    base_fee_gwei: float,
    priority_fee_gwei: float,
    slippage_bps: int,
    gas_multiplier: float,
    gas_preset: str,
    auto_mint: bool,
    auto_gas: bool,
    gas_boost_pct: int,
    tx_retry: bool,
    max_retries: int,
    notify_success: bool,
    notify_fail: bool,
    scheduled_at: Optional[datetime],
    drop_slug: str = "",
    drop_name: str = "",
) -> int:
    """Full v2 create with all mint parameters. Returns session ID."""
    pool = await get_pool()
    scheduled_at = _to_naive_utc(scheduled_at)
    if not gas_limit:
        fn_lower = (mint_function or "").lower()
        gas_limit = 180000 if ("seadrop" in fn_lower or "mintpublic" in fn_lower or "mintsigned" in fn_lower) else 200000
    return await pool.fetchval(
        """
        INSERT INTO mint_sessions (
            telegram_id, drop_slug, drop_name, nft_contract, chain,
            rpc_url, chain_id, mint_function, mint_price_wei,
            quantity_per_wallet, max_per_wallet,
            gas_limit, base_fee_gwei, priority_fee_gwei, slippage_bps,
            gas_multiplier, gas_preset,
            auto_mint, auto_gas, gas_boost_pct,
            tx_retry, max_retries,
            notify_success, notify_fail,
            scheduled_at
        )
        VALUES (
            $1, $2, $3, $4, $5,
            $6, $7, $8, $9,
            $10, $11,
            $12, $13, $14, $15,
            $16, $17,
            $18, $19, $20,
            $21, $22,
            $23, $24,
            $25
        )
        RETURNING id
        """,
        telegram_id, drop_slug, drop_name, nft_contract, chain,
        rpc_url, chain_id, mint_function, mint_price_wei,
        quantity_per_wallet, max_per_wallet,
        gas_limit, base_fee_gwei, priority_fee_gwei, slippage_bps,
        gas_multiplier, gas_preset,
        auto_mint, auto_gas, gas_boost_pct,
        tx_retry, max_retries,
        notify_success, notify_fail,
        scheduled_at,
    )


async def attach_wallets_to_session(session_id: int, wallet_ids: list[int]) -> None:
    pool = await get_pool()
    await pool.executemany(
        "INSERT INTO session_wallets (session_id, wallet_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        [(session_id, wid) for wid in wallet_ids],
    )


async def update_session_status(
    session_id: int, status: str, fired_at: Optional[datetime] = None
) -> None:
    pool = await get_pool()
    fired_at = _to_naive_utc(fired_at)
    await pool.execute(
        "UPDATE mint_sessions SET status = $1, fired_at = $2 WHERE id = $3",
        status, fired_at, session_id,
    )


async def cancel_mint_session(session_id: int, telegram_id: int) -> bool:
    pool = await get_pool()
    result = await pool.execute(
        """
        UPDATE mint_sessions SET status = 'cancelled'
        WHERE id = $1 AND telegram_id = $2 AND status IN ('pending', 'active', 'paused')
        """,
        session_id, telegram_id,
    )
    return result != "UPDATE 0"


async def pause_mint_session(session_id: int, telegram_id: int) -> bool:
    pool = await get_pool()
    result = await pool.execute(
        """
        UPDATE mint_sessions SET status = 'paused'
        WHERE id = $1 AND telegram_id = $2 AND status IN ('pending', 'active')
        """,
        session_id, telegram_id,
    )
    return result != "UPDATE 0"


async def get_pending_sessions(telegram_id: int) -> list[asyncpg.Record]:
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT ms.*, COUNT(sw.wallet_id) AS wallet_count
        FROM mint_sessions ms
        LEFT JOIN session_wallets sw ON sw.session_id = ms.id
        WHERE ms.telegram_id = $1 AND ms.status IN ('pending', 'active', 'paused')
        GROUP BY ms.id
        ORDER BY ms.scheduled_at NULLS LAST
        """,
        telegram_id,
    )


async def get_all_pending_sessions() -> list[asyncpg.Record]:
    """Used by Rust engine poller to get all scheduled sessions."""
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT ms.*, COUNT(sw.wallet_id) AS wallet_count
        FROM mint_sessions ms
        LEFT JOIN session_wallets sw ON sw.session_id = ms.id
        WHERE ms.status = 'pending'
        GROUP BY ms.id
        ORDER BY ms.scheduled_at NULLS LAST
        """,
    )


async def get_mint_history(
    telegram_id: int, offset: int = 0, limit: int = 10
) -> list[asyncpg.Record]:
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT
            ms.id, ms.drop_slug, ms.drop_name, ms.chain, ms.status,
            ms.nft_contract, ms.quantity_per_wallet, ms.gas_preset,
            ms.scheduled_at, ms.fired_at, ms.created_at,
            COUNT(mr.id) AS total_results,
            COUNT(mr.id) FILTER (WHERE mr.status = 'success') AS success_count,
            COUNT(mr.id) FILTER (WHERE mr.status = 'failed')  AS fail_count
        FROM mint_sessions ms
        LEFT JOIN mint_results mr ON mr.session_id = ms.id
        WHERE ms.telegram_id = $1
        GROUP BY ms.id
        ORDER BY ms.created_at DESC
        LIMIT $2 OFFSET $3
        """,
        telegram_id, limit, offset,
    )


async def count_mint_history(telegram_id: int) -> int:
    pool = await get_pool()
    return await pool.fetchval(
        "SELECT COUNT(*) FROM mint_sessions WHERE telegram_id = $1",
        telegram_id,
    )


# ── Mint results ──────────────────────────────────────────────────────────────

async def insert_mint_result(
    session_id: int,
    wallet_address: str,
    tx_hash: Optional[str],
    token_id: Optional[str],
    gas_used: Optional[int],
    latency_ms: Optional[int],
    status: str,
    error_msg: Optional[str],
) -> None:
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO mint_results
            (session_id, wallet_address, tx_hash, token_id, gas_used, latency_ms, status, error_msg)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        session_id, wallet_address, tx_hash, token_id, gas_used, latency_ms, status, error_msg,
    )


async def get_session_results(session_id: int) -> list[asyncpg.Record]:
    pool = await get_pool()
    return await pool.fetch(
        "SELECT * FROM mint_results WHERE session_id = $1 ORDER BY minted_at",
        session_id,
    )


# ── TX Logs ───────────────────────────────────────────────────────────────────

async def insert_tx_log(
    session_id: int,
    wallet_address: str,
    tx_hash: Optional[str],
    attempt_no: int,
    status: str,
    error_msg: Optional[str],
    gas_used: Optional[int],
    gas_price_gwei: Optional[float],
    block_number: Optional[int],
    explorer_url: Optional[str],
) -> None:
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO tx_logs
            (session_id, wallet_address, tx_hash, attempt_no, status,
             error_msg, gas_used, gas_price_gwei, block_number, explorer_url)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        """,
        session_id, wallet_address, tx_hash, attempt_no, status,
        error_msg, gas_used, gas_price_gwei, block_number, explorer_url,
    )


async def get_tx_logs(
    session_id: int, limit: int = 50
) -> list[asyncpg.Record]:
    """Return detailed per-attempt tx logs for a session."""
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT * FROM tx_logs
        WHERE session_id = $1
        ORDER BY logged_at DESC
        LIMIT $2
        """,
        session_id, limit,
    )


async def get_latest_session_id(telegram_id: int) -> Optional[int]:
    """Return the most recent session ID for a user."""
    pool = await get_pool()
    return await pool.fetchval(
        """
        SELECT id FROM mint_sessions
        WHERE telegram_id = $1
        ORDER BY id DESC
        LIMIT 1
        """,
        telegram_id,
    )


async def get_tx_hashes_for_session(session_id: int) -> list[dict]:
    """Return confirmed/sent tx hashes + explorer URLs for a session from tx_logs or mint_results fallback."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT wallet_address, tx_hash, explorer_url, block_number, status
        FROM tx_logs
        WHERE session_id = $1
          AND tx_hash IS NOT NULL
        ORDER BY logged_at
        """,
        session_id,
    )
    if rows:
        return [dict(r) for r in rows]

    # Fallback to mint_results
    results = await pool.fetch(
        """
        SELECT mr.wallet_address, mr.tx_hash, NULL::text AS explorer_url, NULL::bigint AS block_number, mr.status
        FROM mint_results mr
        WHERE mr.session_id = $1 AND mr.tx_hash IS NOT NULL
        ORDER BY mr.minted_at
        """,
        session_id,
    )
    return [dict(r) for r in results]


async def get_user_tx_logs(telegram_id: int, limit: int = 20) -> list[asyncpg.Record]:
    """Return recent tx logs across all sessions for a user."""
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT tl.*, ms.chain, ms.drop_name
        FROM tx_logs tl
        JOIN mint_sessions ms ON ms.id = tl.session_id
        WHERE ms.telegram_id = $1
        ORDER BY tl.logged_at DESC
        LIMIT $2
        """,
        telegram_id, limit,
    )


# ── Broadcast ─────────────────────────────────────────────────────────────────

async def get_all_active_telegram_ids() -> list[int]:
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT telegram_id FROM authorized_users
        WHERE is_active = TRUE AND (expires_at IS NULL OR expires_at > NOW())
        """,
    )
    return [r["telegram_id"] for r in rows]
