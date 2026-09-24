// src/opensea/rest.rs — OpenSea REST Drops mint-build (FCFS / signed stages)
//
// Replaces broken GraphQL SIWE + swap() for mintSigned.
// Requires only an API key — no wallet cookies.
// Docs: POST /api/v2/drops/{slug}/mint  { minter, quantity }

use std::collections::HashMap;
use std::sync::OnceLock;
use chrono::{Utc, DateTime};
use futures::future::join_all;
use reqwest::Client;
use tracing::{info, warn, debug};

use crate::opensea::OpenSeaConfig;
use crate::opensea::graphql::{CalldataResult, WalletCalldata};

/// Cached instant API key when OPENSEA_API_KEY env is unset.
static CACHED_KEY: OnceLock<String> = OnceLock::new();

pub async fn ensure_api_key(client: &Client, cfg: &OpenSeaConfig) -> anyhow::Result<String> {
    if let Ok(k) = std::env::var("OPENSEA_API_KEY") {
        let k = k.trim().to_string();
        if !k.is_empty() {
            return Ok(k);
        }
    }
    if let Some(k) = CACHED_KEY.get() {
        return Ok(k.clone());
    }

    let url = format!("{}/auth/keys", cfg.public_api_url.trim_end_matches('/'));
    let resp = client
        .post(&url)
        .header("Accept", "application/json")
        .header("Content-Type", "application/json")
        .header("User-Agent", "Mozilla/5.0")
        .timeout(std::time::Duration::from_secs(15))
        .send()
        .await?;

    let status = resp.status();
    let body: serde_json::Value = resp.json().await.unwrap_or_default();
    let key = body.get("api_key").and_then(|v| v.as_str()).unwrap_or("").to_string();
    if key.is_empty() {
        anyhow::bail!("OpenSea instant API key create failed ({}): {:?}", status, body);
    }
    info!("OpenSea: created instant API key (7-day TTL)");
    let _ = CACHED_KEY.set(key.clone());
    Ok(key)
}

/// Resolve collection slug from contract address via OpenSea.
pub async fn resolve_collection_slug(
    client: &Client,
    cfg: &OpenSeaConfig,
    api_key: &str,
    chain_slug: &str,
    contract: &str,
) -> Option<String> {
    let url = format!(
        "{}/chain/{}/contract/{}",
        cfg.public_api_url.trim_end_matches('/'),
        chain_slug,
        contract.to_lowercase()
    );
    let resp = client
        .get(&url)
        .header("Accept", "application/json")
        .header("X-API-KEY", api_key)
        .header("User-Agent", "Mozilla/5.0")
        .timeout(std::time::Duration::from_secs(8))
        .send()
        .await
        .ok()?;
    if !resp.status().is_success() {
        return None;
    }
    let body: serde_json::Value = resp.json().await.ok()?;
    body.get("collection")
        .or_else(|| body.get("collection_slug"))
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
}

async fn build_one_mint(
    client: &Client,
    cfg: &OpenSeaConfig,
    api_key: &str,
    slug: &str,
    wallet: &str,
    quantity: u32,
) -> CalldataResult {
    let url = format!(
        "{}/drops/{}/mint",
        cfg.public_api_url.trim_end_matches('/'),
        slug
    );
    let body = serde_json::json!({
        "minter": wallet,
        "quantity": quantity
    });

    let resp = match client
        .post(&url)
        .header("Accept", "application/json")
        .header("Content-Type", "application/json")
        .header("X-API-KEY", api_key)
        .header("User-Agent", "Mozilla/5.0")
        .json(&body)
        .timeout(std::time::Duration::from_secs(8))
        .send()
        .await
    {
        Ok(r) => r,
        Err(e) => return CalldataResult::Error(format!("HTTP: {}", e)),
    };

    let status = resp.status().as_u16();
    let json: serde_json::Value = match resp.json().await {
        Ok(j) => j,
        Err(e) => return CalldataResult::Error(format!("JSON: {}", e)),
    };

    match status {
        200 => {
            let to = json.get("to").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let data = json.get("data").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let value = json
                .get("value")
                .and_then(|v| v.as_str())
                .unwrap_or("0x0")
                .to_string();
            if to.is_empty() || data.is_empty() {
                return CalldataResult::Error("Empty mint calldata".into());
            }
            CalldataResult::Success(WalletCalldata {
                wallet_address: wallet.to_string(),
                to,
                data,
                value,
            })
        }
        409 => CalldataResult::DropNotLive,
        422 => {
            let errs = json
                .get("errors")
                .and_then(|v| v.as_array())
                .map(|a| {
                    a.iter()
                        .filter_map(|e| e.as_str())
                        .collect::<Vec<_>>()
                        .join("; ")
                })
                .unwrap_or_else(|| "precondition failed".into());
            let low = errs.to_lowercase();
            if low.contains("insufficient") || low.contains("balance") {
                CalldataResult::InsufficientFund
            } else {
                CalldataResult::Error(errs)
            }
        }
        other => {
            let errs = json
                .get("errors")
                .and_then(|v| v.as_array())
                .map(|a| {
                    a.iter()
                        .filter_map(|e| e.as_str())
                        .collect::<Vec<_>>()
                        .join("; ")
                })
                .unwrap_or_else(|| format!("HTTP {}", other));
            CalldataResult::Error(errs)
        }
    }
}

/// Resolve slug if `drop_slug` looks like a contract address.
pub async fn resolve_drop_slug(
    client: &Client,
    cfg: &OpenSeaConfig,
    api_key: &str,
    chain_slug: &str,
    drop_slug: &str,
    nft_contract: &str,
) -> String {
    if !drop_slug.is_empty() && !drop_slug.starts_with("0x") {
        return drop_slug.to_string();
    }
    let contract = if nft_contract.starts_with("0x") {
        nft_contract
    } else {
        drop_slug
    };
    resolve_collection_slug(client, cfg, api_key, chain_slug, contract)
        .await
        .unwrap_or_else(|| drop_slug.to_string())
}

/// Parallel per-wallet REST mint-build with tight retry until fire_time + grace.
pub async fn fetch_rest_calldata_with_retry(
    client: &Client,
    cfg: &OpenSeaConfig,
    chain_slug: &str,
    drop_slug: &str,
    nft_contract: &str,
    wallets: &[String],
    quantity: u32,
    fire_time: DateTime<Utc>,
) -> anyhow::Result<HashMap<String, CalldataResult>> {
    let api_key = ensure_api_key(client, cfg).await?;
    let slug = resolve_drop_slug(client, cfg, &api_key, chain_slug, drop_slug, nft_contract).await;

    if slug.is_empty() || slug.starts_with("0x") {
        anyhow::bail!(
            "FCFS requires OpenSea collection slug (got '{}'). Re-run mint setup after CA inspect.",
            slug
        );
    }

    info!(
        "OpenSea REST mint-build: slug={} wallets={} qty={}",
        slug,
        wallets.len(),
        quantity
    );

    let mut retry_wallets: Vec<String> = wallets.to_vec();
    let mut final_results: HashMap<String, CalldataResult> = HashMap::new();
    let deadline = fire_time + chrono::Duration::seconds(45);
    let mut attempt = 0u32;

    // Start hammering slightly before open so we land in the first eligible ms
    let start_hammer = fire_time - chrono::Duration::milliseconds(800);
    let now = Utc::now();
    if now < start_hammer {
        let wait = (start_hammer - now).to_std().unwrap_or_default();
        tokio::time::sleep(wait).await;
    }

    loop {
        attempt += 1;
        let now = Utc::now();
        if now > deadline {
            warn!("OpenSea REST: deadline after {} attempts", attempt);
            break;
        }
        if retry_wallets.is_empty() {
            break;
        }

        let futs: Vec<_> = retry_wallets
            .iter()
            .map(|w| {
                let c = client.clone();
                let cfg = cfg.clone();
                let key = api_key.clone();
                let slug = slug.clone();
                let wallet = w.clone();
                async move { (wallet.clone(), build_one_mint(&c, &cfg, &key, &slug, &wallet, quantity).await) }
            })
            .collect();

        let results = join_all(futs).await;
        let mut still_pending = Vec::new();
        let mut got_success = false;

        for (addr, result) in results {
            match &result {
                CalldataResult::Success(_) => {
                    info!("OpenSea REST: ✓ calldata for {}", &addr[..10.min(addr.len())]);
                    got_success = true;
                    final_results.insert(addr, result);
                }
                CalldataResult::InsufficientFund => {
                    final_results.insert(addr, result);
                }
                CalldataResult::DropNotLive => {
                    still_pending.push(addr);
                }
                CalldataResult::Error(e) => {
                    debug!("OpenSea REST: {} → {} (retry)", &addr[..10.min(addr.len())], e);
                    // Permanent allowlist / limit errors — don't spin forever
                    let low = e.to_lowercase();
                    if low.contains("allowlist")
                        || low.contains("not eligible")
                        || low.contains("limit")
                        || low.contains("exhausted")
                        || low.contains("not found")
                    {
                        final_results.insert(addr, result);
                    } else {
                        still_pending.push(addr);
                    }
                }
            }
        }

        if got_success && now >= fire_time {
            for addr in still_pending {
                final_results
                    .entry(addr)
                    .or_insert(CalldataResult::DropNotLive);
            }
            break;
        }

        retry_wallets = still_pending;
        // Ultra-tight poll while waiting for stage to open
        tokio::time::sleep(std::time::Duration::from_millis(40)).await;
    }

    // Fill any missing as DropNotLive/Error
    for w in wallets {
        final_results
            .entry(w.clone())
            .or_insert(CalldataResult::DropNotLive);
    }

    Ok(final_results)
}