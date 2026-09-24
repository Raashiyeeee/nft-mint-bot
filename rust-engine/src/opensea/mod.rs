// src/opensea/mod.rs
use serde::Deserialize;

pub mod graphql;
pub mod rest;
pub mod scheduler;

#[derive(Debug, Clone, Deserialize)]
pub struct OpenSeaConfig {
    pub graphql_url: String,
    pub public_api_url: String,
}

pub fn load_opensea_config(path: &str) -> anyhow::Result<OpenSeaConfig> {
    let content = std::fs::read_to_string(path)?;
    let raw: toml::Value = toml::from_str(&content)?;
    let opensea = raw.get("opensea").ok_or_else(|| anyhow::anyhow!("Missing [opensea] section"))?;
    Ok(OpenSeaConfig {
        graphql_url: opensea["graphql_url"].as_str().unwrap_or("https://gql.opensea.io/graphql").to_string(),
        public_api_url: opensea["public_api_url"].as_str().unwrap_or("https://api.opensea.io/api/v2").to_string(),
    })
}
