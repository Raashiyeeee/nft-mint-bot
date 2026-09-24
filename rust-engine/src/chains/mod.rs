// src/chains/mod.rs — Chain registry with RPC failover

use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::RwLock;
use tracing::{info, warn};
use reqwest::Client;

// ── Config types ──────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Deserialize)]
pub struct ChainConfig {
    pub name: String,
    pub chain_id: u64,
    pub rpc_urls: Vec<String>,
    pub explorer: String,
    pub opensea_chain_slug: String,
    pub native_token: String,
    pub use_eip1559: bool,
    #[serde(default)]
    pub use_base_preconf: bool,
}

#[derive(Debug, Deserialize)]
pub struct ChainsFile {
    pub chains: HashMap<String, ChainConfig>,
}

pub fn load_chains_config(path: &str) -> anyhow::Result<HashMap<String, ChainConfig>> {
    let content = std::fs::read_to_string(path)?;
    // Substitute env vars for ${INFURA_KEY} and ${ALCHEMY_KEY}
    let infura = std::env::var("INFURA_KEY").unwrap_or_default();
    let alchemy = std::env::var("ALCHEMY_KEY").unwrap_or_default();
    let content = content
        .replace("${INFURA_KEY}", &infura)
        .replace("${ALCHEMY_KEY}", &alchemy);
    let mut file: ChainsFile = toml::from_str(&content)?;
    // Drop provider URLs that still have an empty API key after substitution
    // (e.g. ".../v2/" or ".../v2/ "). Keeps public fallbacks usable.
    for cfg in file.chains.values_mut() {
        cfg.rpc_urls.retain(|u| {
            let trimmed = u.trim();
            !trimmed.is_empty()
                && !trimmed.ends_with("/v2/")
                && !trimmed.ends_with("/v2")
                && !trimmed.contains("${")
        });
    }
    Ok(file.chains)
}

// ── RPC failover state ────────────────────────────────────────────────────────

#[derive(Debug, Default)]
struct ChainRpcState {
    current_index: usize,
    failure_counts: Vec<u32>,
}

// ── Chain registry ────────────────────────────────────────────────────────────

pub struct ChainRegistry {
    chains: HashMap<String, ChainConfig>,
    rpc_state: Arc<RwLock<HashMap<String, ChainRpcState>>>,
}

impl ChainRegistry {
    pub fn new(chains: HashMap<String, ChainConfig>) -> Self {
        let mut state = HashMap::new();
        for (key, cfg) in &chains {
            state.insert(key.clone(), ChainRpcState {
                current_index: 0,
                failure_counts: vec![0u32; cfg.rpc_urls.len()],
            });
        }
        info!("Chain registry: {} chains loaded", chains.len());
        Self {
            chains,
            rpc_state: Arc::new(RwLock::new(state)),
        }
    }

    pub fn count(&self) -> usize {
        self.chains.len()
    }

    pub fn get_config(&self, chain_key: &str) -> Option<&ChainConfig> {
        self.chains.get(chain_key)
    }

    pub fn all_chains(&self) -> &HashMap<String, ChainConfig> {
        &self.chains
    }

    /// Returns the current primary RPC URL for a chain
    pub async fn current_rpc(&self, chain_key: &str) -> Option<String> {
        let state = self.rpc_state.read().await;
        let cfg = self.chains.get(chain_key)?;
        let s = state.get(chain_key)?;
        cfg.rpc_urls.get(s.current_index).cloned()
    }

    /// Rotate to next RPC on failure
    pub async fn rotate_rpc(&self, chain_key: &str) {
        let mut state = self.rpc_state.write().await;
        if let Some(s) = state.get_mut(chain_key) {
            if let Some(cfg) = self.chains.get(chain_key) {
                if cfg.rpc_urls.len() > 1 {
                    s.current_index = (s.current_index + 1) % cfg.rpc_urls.len();
                    warn!("Chain {}: rotated to RPC index {}", chain_key, s.current_index);
                }
            }
        }
    }

    /// Sends a JSON-RPC call with automatic failover (3s timeout, rotate on error)
    pub async fn rpc_call(
        &self,
        client: &Client,
        chain_key: &str,
        method: &str,
        params: serde_json::Value,
    ) -> anyhow::Result<serde_json::Value> {
        let cfg = self.chains.get(chain_key)
            .ok_or_else(|| anyhow::anyhow!("Unknown chain: {}", chain_key))?;

        let body = serde_json::json!({
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": 1
        });

        let max_attempts = cfg.rpc_urls.len().min(8); // try all RPCs, cap at 8
        let mut last_err: Option<String> = None;

        for attempt in 0..max_attempts {
            let url = self.current_rpc(chain_key).await
                .ok_or_else(|| anyhow::anyhow!("No RPC URL for chain {}", chain_key))?;

            let result = tokio::time::timeout(
                std::time::Duration::from_secs(3),
                client.post(&url)
                    .header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
                    .json(&body)
                    .send()
            ).await;

            match result {
                Ok(Ok(resp)) => {
                    let json: serde_json::Value = resp.json().await?;
                    if let Some(err) = json.get("error") {
                        warn!("Chain {} RPC error (attempt {}): {:?}", chain_key, attempt, err);
                        last_err = Some(err.to_string());
                        self.rotate_rpc(chain_key).await;
                        continue;
                    }
                    return Ok(json["result"].clone());
                }
                Ok(Err(e)) => {
                    warn!("Chain {} HTTP error (attempt {}): {}", chain_key, attempt, e);
                    last_err = Some(e.to_string());
                    self.rotate_rpc(chain_key).await;
                }
                Err(_) => {
                    warn!("Chain {} RPC timeout (attempt {})", chain_key, attempt);
                    last_err = Some("timeout".to_string());
                    self.rotate_rpc(chain_key).await;
                }
            }
        }

        if let Some(e) = last_err {
            anyhow::bail!("All RPC endpoints failed for chain {}: {}", chain_key, e)
        }
        anyhow::bail!("All RPC endpoints failed for chain {}", chain_key)
    }

    /// Fetch transaction count (nonce) for a wallet
    pub async fn get_nonce(&self, client: &Client, chain_key: &str, address: &str) -> anyhow::Result<u64> {
        let result = self.rpc_call(
            client, chain_key, "eth_getTransactionCount",
            serde_json::json!([address, "pending"]),
        ).await?;
        let hex = result.as_str().unwrap_or("0x0");
        Ok(u64::from_str_radix(hex.trim_start_matches("0x"), 16)?)
    }

    /// Fetch EIP-1559 gas prices
    pub async fn get_eip1559_fees(&self, client: &Client, chain_key: &str) -> anyhow::Result<(u128, u128)> {
        let result = self.rpc_call(client, chain_key, "eth_feeHistory",
            serde_json::json!([1, "latest", [50.0]])
        ).await?;

        let base_fee_hex = result["baseFeePerGas"]
            .as_array()
            .and_then(|arr| arr.last())
            .and_then(|v| v.as_str())
            .unwrap_or("0x1");

        let base_fee = u128::from_str_radix(base_fee_hex.trim_start_matches("0x"), 16)?;

        // Prefer eth_maxPriorityFeePerGas when available; otherwise tip scales with base fee.
        // Hard-coding 1.5 gwei breaks low-fee L2s (Robinhood ~0.05 gwei) — wallets run out of gas funds.
        let priority_fee = match self.rpc_call(client, chain_key, "eth_maxPriorityFeePerGas", serde_json::json!([])).await {
            Ok(v) => v.as_str()
                .and_then(|h| u128::from_str_radix(h.trim_start_matches("0x"), 16).ok())
                .filter(|p| *p > 0)
                .unwrap_or_else(|| std::cmp::max(base_fee / 10, 1_000_000)), // ≥0.001 gwei
            Err(_) => std::cmp::max(base_fee / 10, 1_000_000),
        };

        Ok((base_fee, priority_fee))
    }

    /// Send raw transaction to all configured RPCs in parallel for ultra-fast broadcasting.
    /// Races until the first *success* (does not abandon siblings on the first timeout).
    /// If every RPC errors/times out, polls for the expected hash — a timed-out broadcast
    /// may still have been accepted by the sequencer.
    pub async fn send_raw_tx(&self, client: &Client, chain_key: &str, raw_tx: &str) -> anyhow::Result<String> {
        let cfg = self.chains.get(chain_key)
            .ok_or_else(|| anyhow::anyhow!("Unknown chain: {}", chain_key))?;

        if cfg.rpc_urls.is_empty() {
            anyhow::bail!("No RPC URLs for chain {}", chain_key);
        }

        // Calculate expected tx hash: keccak256(raw_tx_bytes)
        let expected_hash = {
            let hex_clean = raw_tx.trim_start_matches("0x");
            if let Ok(bytes) = hex::decode(hex_clean) {
                use sha3::{Digest, Keccak256};
                let mut hasher = Keccak256::new();
                hasher.update(&bytes);
                format!("0x{}", hex::encode(hasher.finalize()))
            } else {
                String::new()
            }
        };

        let body = serde_json::json!({
            "jsonrpc": "2.0",
            "method": "eth_sendRawTransaction",
            "params": [raw_tx],
            "id": 1
        });

        // Launch concurrent requests to ALL configured RPC endpoints
        let mut futures = Vec::new();
        for url in &cfg.rpc_urls {
            let u = url.clone();
            let c = client.clone();
            let b = body.clone();
            let exp_h = expected_hash.clone();

            futures.push(Box::pin(async move {
                // Broadcasts get a longer window than read RPCs — public sequencers stall at T=0
                let resp = tokio::time::timeout(
                    std::time::Duration::from_secs(8),
                    c.post(&u)
                        .header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
                        .json(&b)
                        .send()
                ).await;

                match resp {
                    Ok(Ok(res)) => {
                        if let Ok(json) = res.json::<serde_json::Value>().await {
                            if let Some(err) = json.get("error") {
                                let err_msg = err.to_string().to_lowercase();
                                if err_msg.contains("already known")
                                    || err_msg.contains("already imported")
                                    || err_msg.contains("already in pool")
                                {
                                    if !exp_h.is_empty() {
                                        return Ok((u, exp_h));
                                    }
                                }
                                return Err(anyhow::anyhow!("RPC error from {}: {:?}", u, err));
                            }
                            if let Some(h) = json.get("result").and_then(|v| v.as_str()) {
                                if h.starts_with("0x") {
                                    return Ok((u, h.to_string()));
                                }
                            }
                        }
                        Err(anyhow::anyhow!("Invalid response from {}", u))
                    }
                    Ok(Err(e)) => Err(anyhow::anyhow!("HTTP error from {}: {}", u, e)),
                    Err(_) => Err(anyhow::anyhow!("Timeout connecting to {}", u)),
                }
            }));
        }

        // Race until first Ok — keep waiting on remaining RPCs after a fast failure/timeout
        let mut last_err: Option<String> = None;
        let mut pending = futures;
        while !pending.is_empty() {
            let (res, _idx, remaining) = futures::future::select_all(pending).await;
            pending = remaining;
            match res {
                Ok((winning_rpc, tx_hash)) => {
                    info!("Session TX broadcast succeeded via RPC {} → {}", winning_rpc, tx_hash);
                    return Ok(tx_hash);
                }
                Err(e) => {
                    warn!("Parallel send RPC failed ({}), {} remaining", e, pending.len());
                    last_err = Some(e.to_string());
                }
            }
        }

        // Timed-out / errored replies often still mean the sequencer accepted the tx.
        // Poll briefly before declaring failure (avoids nonce-too-low retry storms).
        if !expected_hash.is_empty() {
            if let Some(h) = self.poll_tx_seen(client, chain_key, &expected_hash, 12).await {
                warn!(
                    "Broadcast RPCs failed but tx {} is visible on-chain/mempool — treating as sent",
                    &h[..12.min(h.len())]
                );
                return Ok(h);
            }
            let err_l = last_err.as_deref().unwrap_or("").to_lowercase();
            if err_l.contains("already known")
                || err_l.contains("already imported")
                || err_l.contains("already in pool")
            {
                return Ok(expected_hash);
            }
        }

        if let Some(e) = last_err {
            anyhow::bail!("All RPC endpoints failed for chain {}: {}", chain_key, e)
        }
        anyhow::bail!("All RPC endpoints failed for chain {}", chain_key)
    }

    /// Returns Some(hash) if eth_getTransactionByHash or receipt sees the tx within `attempts`.
    async fn poll_tx_seen(
        &self,
        client: &Client,
        chain_key: &str,
        tx_hash: &str,
        attempts: u32,
    ) -> Option<String> {
        for i in 0..attempts {
            if i > 0 {
                tokio::time::sleep(std::time::Duration::from_millis(400)).await;
            }
            if let Ok(v) = self
                .rpc_call(
                    client,
                    chain_key,
                    "eth_getTransactionByHash",
                    serde_json::json!([tx_hash]),
                )
                .await
            {
                if !v.is_null() {
                    return Some(tx_hash.to_string());
                }
            }
            if let Ok(Some(_)) = self.get_receipt(client, chain_key, tx_hash).await {
                return Some(tx_hash.to_string());
            }
        }
        None
    }

    /// Poll receipt
    pub async fn get_receipt(&self, client: &Client, chain_key: &str, tx_hash: &str) -> anyhow::Result<Option<serde_json::Value>> {
        let result = self.rpc_call(
            client, chain_key, "eth_getTransactionReceipt",
            serde_json::json!([tx_hash]),
        ).await?;
        if result.is_null() {
            Ok(None)
        } else {
            Ok(Some(result))
        }
    }

    /// Base-specific: base_transactionStatus (preconfirmation)
    pub async fn base_tx_status(&self, client: &Client, tx_hash: &str) -> anyhow::Result<String> {
        let preconf_url = self.chains.get("base")
            .and_then(|cfg| cfg.rpc_urls.iter().find(|u| u.contains("preconf")).cloned())
            .unwrap_or_else(|| "https://mainnet-preconf.base.org".to_string());

        let body = serde_json::json!({
            "jsonrpc": "2.0",
            "method": "base_transactionStatus",
            "params": [tx_hash],
            "id": 1
        });

        let resp = tokio::time::timeout(
            std::time::Duration::from_millis(2000),
            client.post(&preconf_url)
                .header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
                .json(&body)
                .send()
        ).await??;

        let json: serde_json::Value = resp.json().await?;
        if let Some(err) = json.get("error") {
            anyhow::bail!("Preconf RPC error from {}: {:?}", preconf_url, err);
        }
        Ok(json["result"].as_str().unwrap_or("Unknown").to_string())
    }
}
