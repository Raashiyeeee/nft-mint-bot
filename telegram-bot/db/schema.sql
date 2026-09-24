-- ═══════════════════════════════════════════════════════════════
-- schema.sql — NFT Mint Bot PostgreSQL Schema
-- Applied automatically on first docker-compose up
-- ═══════════════════════════════════════════════════════════════

-- ── Access control ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS authorized_users (
    id               SERIAL PRIMARY KEY,
    telegram_id      BIGINT  UNIQUE NOT NULL,
    telegram_username TEXT,
    display_name     TEXT,
    is_active        BOOLEAN  DEFAULT TRUE,
    added_by         BIGINT,
    added_at         TIMESTAMP DEFAULT NOW(),
    expires_at       TIMESTAMP,   -- NULL = permanent
    timezone_pref    TEXT      DEFAULT 'UTC'  -- IANA timezone name, e.g. 'Asia/Kolkata'
);

CREATE INDEX IF NOT EXISTS idx_authorized_users_telegram_id ON authorized_users(telegram_id);
CREATE INDEX IF NOT EXISTS idx_authorized_users_active ON authorized_users(is_active, expires_at);

-- ── Admin registry ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS admin_users (
    telegram_id BIGINT PRIMARY KEY,
    added_at    TIMESTAMP DEFAULT NOW()
);

-- ── Wallet registry ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS user_wallets (
    id                   SERIAL PRIMARY KEY,
    telegram_id          BIGINT   REFERENCES authorized_users(telegram_id) ON DELETE CASCADE,
    wallet_address       TEXT     NOT NULL,
    wallet_label         TEXT,
    chain                TEXT     NOT NULL,
    private_key_encrypted TEXT    NOT NULL,  -- Fernet-encrypted
    is_active            BOOLEAN  DEFAULT TRUE,
    added_at             TIMESTAMP DEFAULT NOW(),
    UNIQUE (telegram_id, wallet_address, chain)
);

CREATE INDEX IF NOT EXISTS idx_user_wallets_telegram_id ON user_wallets(telegram_id);
CREATE INDEX IF NOT EXISTS idx_user_wallets_chain ON user_wallets(chain);
CREATE INDEX IF NOT EXISTS idx_user_wallets_active ON user_wallets(is_active);

-- ── Mint sessions ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS mint_sessions (
    id                  SERIAL PRIMARY KEY,
    telegram_id         BIGINT   REFERENCES authorized_users(telegram_id) ON DELETE CASCADE,
    drop_slug           TEXT     NOT NULL,
    drop_name           TEXT,
    nft_contract        TEXT     NOT NULL,
    chain               TEXT     NOT NULL,
    rpc_url             TEXT,
    chain_id            INT,
    mint_function       TEXT     DEFAULT 'mint(uint256)',
    mint_price_wei      NUMERIC  DEFAULT 0,    -- price per NFT in wei
    quantity_per_wallet INT      DEFAULT 1,
    max_per_wallet      INT      DEFAULT 0,    -- 0 = unlimited
    gas_limit           BIGINT,                -- NULL = auto
    base_fee_gwei       NUMERIC  DEFAULT 0,
    priority_fee_gwei   NUMERIC  DEFAULT 2,
    slippage_bps        INT      DEFAULT 50,   -- 50 = 0.5%
    gas_multiplier      NUMERIC(4,2) DEFAULT 1.5,
    gas_preset          TEXT     DEFAULT 'fast',  -- standard|fast|aggressive
    auto_mint           BOOLEAN  DEFAULT FALSE,
    auto_gas            BOOLEAN  DEFAULT FALSE,
    gas_boost_pct       INT      DEFAULT 0,
    tx_retry            BOOLEAN  DEFAULT TRUE,
    max_retries         INT      DEFAULT 3,
    notify_success      BOOLEAN  DEFAULT TRUE,
    notify_fail         BOOLEAN  DEFAULT TRUE,
    status              TEXT     DEFAULT 'pending',  -- pending|active|completed|cancelled|failed|paused
    scheduled_at        TIMESTAMP,
    fired_at            TIMESTAMP,
    created_at          TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mint_sessions_telegram_id ON mint_sessions(telegram_id);
CREATE INDEX IF NOT EXISTS idx_mint_sessions_status ON mint_sessions(status);
CREATE INDEX IF NOT EXISTS idx_mint_sessions_scheduled ON mint_sessions(scheduled_at) WHERE status = 'pending';

-- ── Wallets selected for a session (many-to-many) ───────────────
CREATE TABLE IF NOT EXISTS session_wallets (
    session_id     INT  REFERENCES mint_sessions(id) ON DELETE CASCADE,
    wallet_id      INT  REFERENCES user_wallets(id)  ON DELETE CASCADE,
    PRIMARY KEY (session_id, wallet_id)
);

-- ── Mint results ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS mint_results (
    id             SERIAL PRIMARY KEY,
    session_id     INT  REFERENCES mint_sessions(id) ON DELETE CASCADE,
    wallet_address TEXT NOT NULL,
    tx_hash        TEXT,
    token_id       TEXT,
    gas_used       BIGINT,
    latency_ms     INT,
    status         TEXT,   -- success | failed | skipped
    error_msg      TEXT,
    minted_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mint_results_session_id ON mint_results(session_id);
CREATE INDEX IF NOT EXISTS idx_mint_results_wallet ON mint_results(wallet_address);

-- ── Denied access log ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS denied_access_log (
    id           SERIAL PRIMARY KEY,
    telegram_id  BIGINT   NOT NULL,
    username     TEXT,
    command_used TEXT,
    denied_at    TIMESTAMP DEFAULT NOW(),
    reason       TEXT      DEFAULT 'not_authorized'
);

CREATE INDEX IF NOT EXISTS idx_denied_access_telegram_id ON denied_access_log(telegram_id);
CREATE INDEX IF NOT EXISTS idx_denied_access_denied_at  ON denied_access_log(denied_at);

-- ── Rate limiting ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rate_limit_log (
    telegram_id   BIGINT    NOT NULL,
    window_start  TIMESTAMP NOT NULL,
    command_count INT       DEFAULT 0,
    PRIMARY KEY (telegram_id, window_start)
);

-- ── Transaction logs (per-attempt detail) ────────────────────────
CREATE TABLE IF NOT EXISTS tx_logs (
    id              SERIAL PRIMARY KEY,
    session_id      INT  REFERENCES mint_sessions(id) ON DELETE CASCADE,
    wallet_address  TEXT NOT NULL,
    tx_hash         TEXT,
    attempt_no      INT      DEFAULT 1,
    status          TEXT     DEFAULT 'pending',  -- pending|sent|confirmed|failed
    error_msg       TEXT,
    gas_used        BIGINT,
    gas_price_gwei  NUMERIC,
    block_number    BIGINT,
    explorer_url    TEXT,
    logged_at       TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tx_logs_session_id    ON tx_logs(session_id);
CREATE INDEX IF NOT EXISTS idx_tx_logs_wallet        ON tx_logs(wallet_address);
CREATE INDEX IF NOT EXISTS idx_tx_logs_status        ON tx_logs(status);
