# NFT Mint Bot

A high-performance, multi-chain NFT minting bot designed for competitive public drops. Built with a **Rust execution engine** on the hot path for sub-second transaction dispatch and a **Python Telegram interface** for remote wallet management and drop scheduling.

---

## Architecture

- **Rust Engine (`rust-engine/`)**: Handles low-latency tasks—JSON-RPC communication, calldata generation, gas pricing, pre-computed signatures, and concurrent transaction broadcasting across 15 EVM chains.
- **Telegram Bot (`telegram-bot/`)**: Interactive management UI. Lets you add wallets, configure drop parameters, schedule mint sessions, and receive real-time push confirmations.
- **PostgreSQL**: Stores encrypted wallets, authorized users, mint session configs, and transaction receipts.
- **Security at Rest**: Private keys are encrypted using **Fernet (AES-128-CBC)** before being written to the database.

---

## Supported Chains

Supports 15 networks out of the box via Alchemy RPC endpoints (`config/chains.toml`):

- **L1**: Ethereum Mainnet
- **L2s / EVMs**: Base, Arbitrum One, Optimism, Polygon, Avalanche C-Chain, BNB Smart Chain, Zora, Robinhood Chain, Shape, Abstract, ApeChain, Arc, HyperEVM, Ink

---

## Quick Start (Docker)

### 1. Requirements
- Docker & Docker Compose
- Telegram Bot Token (from [@BotFather](https://t.me/BotFather))
- Alchemy API Key (for multi-chain RPCs)
- PostgreSQL (bundled via Docker)

### 2. Clone and Configure
```bash
git clone https://github.com/Raashiyeeee/nft-mint-bot.git
cd nft-mint-bot

cp .env.example .env
```

### 3. Generate Fernet Encryption Key
Generate an encryption key to protect stored private keys:
```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```
Paste this value into `.env` as `FERNET_KEY`.

### 4. Edit Environment Variables
Open `.env` and fill in your values:
```env
BOT_TOKEN=your_telegram_bot_token_here
DB_PASSWORD=your_strong_postgres_password
FERNET_KEY=your_generated_fernet_key
ALCHEMY_KEY=your_alchemy_api_key
OPENSEA_API_KEY=your_opensea_key
ADMIN_TELEGRAM_ID=your_telegram_user_id
```

### 5. Start the Services
```bash
docker compose up -d --build
```

Check logs:
```bash
docker compose logs -f
```

---

## Telegram Bot Commands

### User Commands
- `/start` — Welcome message and bot health status.
- `/wallets` — View and manage registered wallets for each chain.
- `/schedule` — Schedule an automated mint session for an upcoming drop.
- `/status` — View active sessions and pending transactions.

### Admin Commands (Owner Only)
- `/admin` — Open the inline admin dashboard.
- `/adduser <telegram_id>` — Authorize a new user to access the bot.
- `/removeuser <telegram_id>` — Revoke access and securely wipe associated keys.
- `/makeadmin <telegram_id>` — Promote an authorized user to admin.
- `/revokeadmin <telegram_id>` — Demote an admin back to regular user.

---

## VPS Deployment (Ubuntu 22.04)

For competitive drops, deploy on a VPS close to RPC servers (e.g., AWS Ashburn / `us-east-1`):

```bash
chmod +x deploy.sh
sudo ./deploy.sh
```

The script installs Docker, builds the Rust engine, sets up UFW firewall rules, generates systemd service units, and configures automated restarts.

---

## Project Structure

```text
├── config/
│   ├── bot.toml          # Timing, gas multipliers, and retry settings
│   ├── chains.toml       # RPC endpoints and chain definitions
│   └── opensea.toml      # OpenSea GraphQL headers and session configs
├── rust-engine/          # High-speed transaction execution engine
│   ├── src/              # RPC clients, signer, and mempool listeners
│   └── Cargo.toml
├── telegram-bot/         # Python management interface
│   ├── handlers/         # User and admin command handlers
│   ├── middleware/       # Auth guards and rate limiters
│   ├── db/               # PostgreSQL schema & query layer
│   └── utils/            # Fernet encryption and validation helpers
├── deploy.sh             # One-shot Ubuntu setup script
├── update.sh             # Rebuild and restart script
└── docker-compose.yml    # Multi-container orchestration
```

---

## Security

- **No plaintext private keys**: Wallet keys are encrypted with Fernet prior to database insertion and only decrypted in memory during the warm-up execution window.
- **Local IPC**: The Rust engine JSON-RPC port (`7878`) binds only to internal Docker network interfaces.
- **Access control**: Commands are gated behind `ADMIN_TELEGRAM_ID` and the `authorized_users` whitelist.

---

## License

MIT License. For educational and automation testing purposes. Always verify gas settings and contract addresses before scheduling live transactions.
