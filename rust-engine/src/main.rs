// src/main.rs — NFT Mint Engine entry point
// Starts the JSON-RPC server on 127.0.0.1:7878 and spawns all subsystems.

mod auth;
mod chains;
mod ipc;
mod minting;
mod opensea;

use std::sync::Arc;
use tracing::{info, warn};
use tracing_subscriber::{fmt, EnvFilter};

pub use chains::ChainRegistry;
pub use opensea::scheduler::SessionManager;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // ── Logging ──────────────────────────────────────────────────────────────
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::from_default_env().add_directive("info".parse()?))
        .with_target(true)
        .init();

    info!("═══ NFT Mint Engine starting ═══");

    // ── Environment ──────────────────────────────────────────────────────────
    dotenv::dotenv().ok();
    let database_url = std::env::var("DATABASE_URL")
        .expect("DATABASE_URL must be set");
    let fernet_key = std::env::var("FERNET_KEY")
        .expect("FERNET_KEY must be set");

    // ── Config loading ───────────────────────────────────────────────────────
    let chains_config = chains::load_chains_config("config/chains.toml")
        .expect("Failed to load chains.toml");
    let opensea_config = opensea::load_opensea_config("config/opensea.toml")
        .expect("Failed to load opensea.toml");
    let bot_config = crate::ipc::load_bot_config("config/bot.toml")
        .expect("Failed to load bot.toml");

    // ── Database pool ────────────────────────────────────────────────────────
    let db_pool = sqlx::postgres::PgPoolOptions::new()
        .max_connections(40)
        .min_connections(4)
        .connect(&database_url)
        .await
        .expect("Failed to connect to PostgreSQL");
    info!("PostgreSQL connected (pool max=40)");

    // ── Chain registry ───────────────────────────────────────────────────────
    let chain_registry = Arc::new(ChainRegistry::new(chains_config));
    info!("Chain registry initialized with {} chains", chain_registry.count());

    // ── HTTP client (shared, low-latency for multi-wallet parallel mints) ─────
    let http_client = reqwest::Client::builder()
        .user_agent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        .pool_max_idle_per_host(64)
        .pool_idle_timeout(std::time::Duration::from_secs(90))
        .tcp_nodelay(true)
        .tcp_keepalive(std::time::Duration::from_secs(30))
        .connect_timeout(std::time::Duration::from_secs(2))
        .timeout(std::time::Duration::from_secs(10))
        .http2_keep_alive_interval(std::time::Duration::from_secs(15))
        .http2_keep_alive_timeout(std::time::Duration::from_secs(10))
        .build()
        .expect("Failed to build HTTP client");

    // ── Session manager (OpenSea auth sessions per wallet) ───────────────────
    let session_manager = Arc::new(SessionManager::new(
        http_client.clone(),
        opensea_config.clone(),
        db_pool.clone(),
        fernet_key.clone(),
    ));

    // ── Scheduler polling task ────────────────────────────────────────────────
    let scheduler_handle = {
        let sm = session_manager.clone();
        let cr = chain_registry.clone();
        let db = db_pool.clone();
        let cfg = bot_config.clone();
        tokio::spawn(async move {
            opensea::scheduler::run_scheduler_loop(sm, cr, db, cfg).await
        })
    };

    // ── JSON-RPC IPC server ───────────────────────────────────────────────────
    let bind_addr = std::env::var("RPC_BIND_ADDR").unwrap_or_else(|_| "0.0.0.0:7878".to_string());
    info!("Starting JSON-RPC server on {}", bind_addr);
    let ipc_handle = tokio::spawn(ipc::server::run_server(
        bind_addr,
        session_manager.clone(),
        chain_registry.clone(),
        db_pool.clone(),
        bot_config.clone(),
    ));

    // ── Graceful shutdown ─────────────────────────────────────────────────────
    tokio::select! {
        res = scheduler_handle => {
            warn!("Scheduler task ended: {:?}", res);
        }
        res = ipc_handle => {
            warn!("IPC server ended: {:?}", res);
        }
        _ = tokio::signal::ctrl_c() => {
            info!("Received Ctrl+C, shutting down...");
        }
    }

    db_pool.close().await;
    info!("NFT Mint Engine stopped.");
    Ok(())
}
