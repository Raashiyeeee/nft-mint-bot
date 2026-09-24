// src/opensea/graphql.rs — OpenSea GraphQL operations
//
// Critical: uses gql.opensea.io (not api.opensea.io)
// Batched swap() uses field aliasing: w0, w1, w2... for one round-trip.

use std::collections::HashMap;
use reqwest::Client;
use serde::{Deserialize, Serialize};
use tracing::{info, warn, debug};

use crate::auth::build_headers;
use crate::opensea::OpenSeaConfig;

// ── Drop info ─────────────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct DropInfo {
    pub slug: String,
    pub name: String,
    pub contract_address: String,
    pub chain: String,
    pub price_eth: String,
    pub mint_start_time: Option<chrono::DateTime<chrono::Utc>>,
    pub stage_index: Option<u32>,
    pub stage_label: Option<String>,
    pub max_supply: Option<u64>,
}

pub async fn fetch_drop_by_slug(
    client: &Client,
    cfg: &OpenSeaConfig,
    cookies: &str,
    slug: &str,
) -> anyhow::Result<DropInfo> {
    let headers = build_headers(cfg, Some(cookies));

    let query = serde_json::json!({
        "operationName": "dropBySlug",
        "query": r#"
            query dropBySlug($slug: String!) {
                drop(slug: $slug) {
                    slug
                    name
                    chain
                    mintedItemCount
                    totalItemCount
                    activeStage {
                        id
                        label
                        startTime
                        price {
                            unit
                            symbol
                        }
                    }
                    stages {
                        id
                        label
                        startTime
                        endTime
                    }
                    nftContract {
                        address
                    }
                }
            }
        "#,
        "variables": { "slug": slug }
    });

    let mut req = client.post(&cfg.graphql_url).json(&query);
    for (k, v) in &headers {
        req = req.header(k.as_str(), v.as_str());
    }

    let resp: serde_json::Value = req.send().await?.json().await?;

    // Check for webpack chunk hash change warning
    if let Some(ext) = resp.get("extensions") {
        if let Some(new_hash) = ext.get("chunkHash") {
            warn!("⚠️ OpenSea webpack chunk hash changed: {}. Endpoint verification may be needed.", new_hash);
        }
    }

    let drop = resp["data"]["drop"].as_object()
        .ok_or_else(|| anyhow::anyhow!("drop not found in response: {:?}", resp))?;

    let contract = drop["nftContract"]["address"].as_str().unwrap_or("").to_string();
    let chain = drop["chain"].as_str().unwrap_or("").to_lowercase();
    let name = drop["name"].as_str().unwrap_or(slug).to_string();

    let (mint_start_time, stage_index, stage_label, price_eth) =
        if let Some(active) = drop.get("activeStage").and_then(|s| s.as_object()) {
            let start = active["startTime"].as_str()
                .and_then(|s| chrono::DateTime::parse_from_rfc3339(s).ok())
                .map(|dt| dt.with_timezone(&chrono::Utc));
            let label = active["label"].as_str().map(|s| s.to_string());
            let price = active["price"]["unit"].as_str().unwrap_or("0").to_string();
            (start, Some(0u32), label, price)
        } else {
            // Fall back to first upcoming stage
            let stages = drop["stages"].as_array();
            let next_stage = stages.and_then(|arr| arr.first());
            let start = next_stage
                .and_then(|s| s["startTime"].as_str())
                .and_then(|s| chrono::DateTime::parse_from_rfc3339(s).ok())
                .map(|dt| dt.with_timezone(&chrono::Utc));
            let label = next_stage.and_then(|s| s["label"].as_str()).map(|s| s.to_string());
            (start, None, label, "0".to_string())
        };

    Ok(DropInfo {
        slug: slug.to_string(),
        name,
        contract_address: contract,
        chain,
        price_eth,
        mint_start_time,
        stage_index,
        stage_label,
        max_supply: drop["totalItemCount"].as_u64(),
    })
}

// ── Batched swap() calldata ────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct WalletCalldata {
    pub wallet_address: String,
    pub to: String,
    pub data: String,
    pub value: String,
}

#[derive(Debug, Clone)]
pub enum CalldataResult {
    Success(WalletCalldata),
    InsufficientFund,
    DropNotLive,
    Error(String),
}

/// Batch fetches calldata for all wallets in one GraphQL request using field aliasing.
/// Returns map of wallet_address → CalldataResult.
pub async fn fetch_batch_calldata(
    client: &Client,
    cfg: &OpenSeaConfig,
    cookies: &str,
    chain_slug: &str,
    nft_contract: &str,
    wallets: &[String],
    quantity: u32,
) -> anyhow::Result<HashMap<String, CalldataResult>> {
    if wallets.is_empty() {
        return Ok(HashMap::new());
    }

    let headers = build_headers(cfg, Some(cookies));

    // Build aliased query: w0, w1, w2...
    // Operation name must be "B" (minimal bytes on hot path)
    let mut fields = String::new();
    for (i, addr) in wallets.iter().enumerate() {
        fields.push_str(&format!(
            r#"
  w{i}: swap(
    address: "{addr}",
    fromAssets: [{{ asset: {{ chain: "{chain}", contractAddress: "0x0000000000000000000000000000000000000000" }} }}]
    toAssets: [{{ asset: {{ chain: "{chain}", contractAddress: "{contract}", tokenId: "0" }}, quantity: "{qty}" }}]
    action: MINT
    capabilities: {{ eip7702: false }}
  ) {{
    actions {{
      __typename
      ... on TransactionAction {{
        transactionSubmissionData {{ to data value }}
      }}
      ... on MintAction {{
        transactionSubmissionData {{ to data value }}
      }}
    }}
    errors {{ __typename }}
  }}"#,
            i = i,
            addr = addr,
            chain = chain_slug,
            contract = nft_contract,
            qty = quantity,
        ));
    }

    let query_str = format!("query B {{{}}}", fields);

    let body = serde_json::json!({
        "operationName": "B",
        "query": query_str
    });

    let mut req = client.post(&cfg.graphql_url).json(&body);
    for (k, v) in &headers {
        req = req.header(k.as_str(), v.as_str());
    }

    let resp: serde_json::Value = req.send().await?.json().await?;
    debug!("swap() batch response: {:?}", &resp);

    let mut results = HashMap::new();

    for (i, addr) in wallets.iter().enumerate() {
        let key = format!("w{}", i);
        let wallet_data = &resp["data"][&key];

        if wallet_data.is_null() {
            results.insert(addr.clone(), CalldataResult::Error("No data returned".into()));
            continue;
        }

        // Check for errors array
        let errors = wallet_data["errors"].as_array();
        if let Some(errs) = errors {
            if !errs.is_empty() {
                let err_type = errs[0]["__typename"].as_str().unwrap_or("UnknownError");
                let result = match err_type {
                    "InsufficientFund" => {
                        warn!("Wallet {}: InsufficientFund — skipping permanently", &addr[..10]);
                        CalldataResult::InsufficientFund
                    }
                    "DropNotMintingError" => {
                        debug!("Wallet {}: DropNotMintingError — drop not live yet", &addr[..10]);
                        CalldataResult::DropNotLive
                    }
                    other => {
                        warn!("Wallet {}: error {}", &addr[..10], other);
                        CalldataResult::Error(other.to_string())
                    }
                };
                results.insert(addr.clone(), result);
                continue;
            }
        }

        // Extract TransactionAction or MintAction
        let actions = wallet_data["actions"].as_array();
        let tx_action = actions
            .and_then(|arr| arr.iter().find(|a| {
                let tn = a["__typename"].as_str().unwrap_or("");
                tn == "TransactionAction" || tn == "MintAction"
            }));

        if let Some(action) = tx_action {
            let sub = &action["transactionSubmissionData"];
            let to = sub["to"].as_str().unwrap_or("").to_string();
            let data = sub["data"].as_str().unwrap_or("").to_string();
            let value = sub["value"].as_str().unwrap_or("0").to_string();

            if to.is_empty() || data.is_empty() {
                results.insert(addr.clone(), CalldataResult::Error("Empty calldata".into()));
                continue;
            }

            results.insert(addr.clone(), CalldataResult::Success(WalletCalldata {
                wallet_address: addr.clone(),
                to,
                data,
                value,
            }));
        } else {
            results.insert(addr.clone(), CalldataResult::Error("No TransactionAction in response".into()));
        }
    }

    Ok(results)
}
