// src/ipc/mod.rs
use serde::Deserialize;

pub mod server;

#[derive(Debug, Clone, Deserialize)]
pub struct BotConfig {
    pub schedule_poll_interval_secs: u32,
    pub max_wallets_per_batch: u32,
    pub rpc_timeout_secs: u32,
    pub conversation_timeout_secs: u32,
}

pub fn load_bot_config(path: &str) -> anyhow::Result<BotConfig> {
    let content = std::fs::read_to_string(path)?;
    let raw: toml::Value = toml::from_str(&content)?;
    let bot = raw.get("bot").ok_or_else(|| anyhow::anyhow!("Missing [bot] in bot.toml"))?;
    Ok(BotConfig {
        schedule_poll_interval_secs: bot["schedule_poll_interval_secs"].as_integer().unwrap_or(30) as u32,
        max_wallets_per_batch: bot["max_wallets_per_batch"].as_integer().unwrap_or(50) as u32,
        rpc_timeout_secs: bot["rpc_timeout_secs"].as_integer().unwrap_or(2) as u32,
        conversation_timeout_secs: bot["conversation_timeout_secs"].as_integer().unwrap_or(300) as u32,
    })
}
