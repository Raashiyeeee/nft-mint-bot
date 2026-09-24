-- Migration: add missing v2 columns to mint_sessions
ALTER TABLE mint_sessions
  ADD COLUMN IF NOT EXISTS rpc_url             TEXT,
  ADD COLUMN IF NOT EXISTS chain_id            INT,
  ADD COLUMN IF NOT EXISTS mint_function       TEXT DEFAULT 'mint(uint256)',
  ADD COLUMN IF NOT EXISTS mint_price_wei      NUMERIC DEFAULT 0,
  ADD COLUMN IF NOT EXISTS max_per_wallet      INT DEFAULT 0,
  ADD COLUMN IF NOT EXISTS gas_limit           BIGINT,
  ADD COLUMN IF NOT EXISTS base_fee_gwei       NUMERIC DEFAULT 0,
  ADD COLUMN IF NOT EXISTS priority_fee_gwei   NUMERIC DEFAULT 2,
  ADD COLUMN IF NOT EXISTS slippage_bps        INT DEFAULT 50,
  ADD COLUMN IF NOT EXISTS gas_preset          TEXT DEFAULT 'fast',
  ADD COLUMN IF NOT EXISTS auto_mint           BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS auto_gas            BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS gas_boost_pct       INT DEFAULT 0,
  ADD COLUMN IF NOT EXISTS tx_retry            BOOLEAN DEFAULT TRUE,
  ADD COLUMN IF NOT EXISTS max_retries         INT DEFAULT 3,
  ADD COLUMN IF NOT EXISTS notify_success      BOOLEAN DEFAULT TRUE,
  ADD COLUMN IF NOT EXISTS notify_fail         BOOLEAN DEFAULT TRUE;

CREATE INDEX IF NOT EXISTS idx_mint_sessions_status ON mint_sessions(status);
CREATE INDEX IF NOT EXISTS idx_mint_sessions_telegram_id ON mint_sessions(telegram_id);
