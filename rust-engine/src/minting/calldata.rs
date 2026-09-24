// src/minting/calldata.rs — Direct EVM calldata encoder + SeaDrop router
//
// For direct/public mints (drop_slug starts with 0x), we skip OpenSea APIs
// and build the calldata ourselves:
//  • SeaDrop contracts  → mintPublic(address,address,address,uint256) on 0x00005EA00Ac477B1030CE78506496e8C2dE24bf5
//  • mintSeaDrop(address,uint256) ABI variant
//  • Any other function → ABI-encode selector + params

use std::collections::HashMap;
use chrono::{Utc, DateTime};
use reqwest::Client;
use sha3::{Digest, Keccak256};
use tracing::{info, debug, warn};

use crate::opensea::OpenSeaConfig;
use crate::opensea::graphql::{CalldataResult, WalletCalldata, fetch_batch_calldata};

// ── Known SeaDrop router addresses ──────────────────────────────────────────
// Standard OpenSea SeaDrop v1.0 router (deployed on all EVM chains)
pub const SEADROP_ROUTER: &str = "00005ea00ac477b1030ce78506496e8c2de24bf5"; // lowercase, no 0x
// Standard fee recipient used in public SeaDrop mints
pub const SEADROP_FEE_RECIPIENT: &str = "0000a26b00c1f0df003000390027140000faa719";

// ── ABI-encode helpers ────────────────────────────────────────────────────────

/// Compute keccak256 of a function signature, return first 4 bytes as hex
pub fn selector(sig: &str) -> String {
    let mut hasher = Keccak256::new();
    hasher.update(sig.as_bytes());
    let hash = hasher.finalize();
    hex::encode(&hash[..4])
}

/// ABI-encode a single uint256
pub fn encode_uint256(val: u128) -> String {
    format!("{:0>64x}", val)
}

/// ABI-encode an address (20-byte value, left-padded to 32 bytes)
pub fn encode_address(addr: &str) -> String {
    let clean = addr.trim_start_matches("0x").to_lowercase();
    format!("{:0>64}", clean)
}

// ── Dynamic on-chain SeaDrop detection ───────────────────────────────────────

/// Query the on-chain start time for a SeaDrop drop on any EVM chain.
/// Returns Some(DateTime<Utc>) if a valid public drop exists.
pub async fn get_seadrop_start_time_onchain(client: &Client, rpc_url: &str, nft_contract: &str) -> Option<DateTime<Utc>> {
    if rpc_url.is_empty() {
        return None;
    }
    let clean_contract = nft_contract.trim_start_matches("0x").to_lowercase();
    if clean_contract.len() != 40 {
        return None;
    }
    // getPublicDrop(address) selector = 0xbc6a629c
    let data = format!("0xbc6a629c{:0>64}", clean_contract);
    let payload = serde_json::json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [
            {
                "to": format!("0x{}", SEADROP_ROUTER),
                "data": data
            },
            "latest"
        ]
    });

    match client.post(rpc_url)
        .json(&payload)
        .header("User-Agent", "Mozilla/5.0")
        .timeout(std::time::Duration::from_secs(3))
        .send()
        .await
    {
        Ok(resp) => {
            if let Ok(json) = resp.json::<serde_json::Value>().await {
                if let Some(res_str) = json.get("result").and_then(|v| v.as_str()) {
                    if res_str.len() >= 130 {
                        let start_time_hex = &res_str[66..130];
                        if let Ok(start_time) = u64::from_str_radix(start_time_hex, 16) {
                            if start_time > 0 {
                                return DateTime::from_timestamp(start_time as i64, 0);
                            }
                        }
                    }
                }
            }
        }
        Err(e) => {
            debug!("SeaDrop on-chain probe error for {}: {}", nft_contract, e);
        }
    }
    None
}

/// Check dynamically on-chain if a contract has an active SeaDrop drop on this chain.
/// Works for ANY contract address on ANY EVM chain where SeaDrop is deployed.
pub async fn check_is_seadrop_onchain(client: &Client, rpc_url: &str, nft_contract: &str) -> bool {
    let has_seadrop = get_seadrop_start_time_onchain(client, rpc_url, nft_contract).await.is_some();
    if has_seadrop {
        info!("Detected active SeaDrop collection on-chain: {}", nft_contract);
    }
    has_seadrop
}

// ── Direct EVM calldata builder ───────────────────────────────────────────────

/// Build calldata for a direct on-chain mint for ANY contract address and ANY function.
///
/// Supported paths:
/// 1. SeaDrop path (either detected on-chain or specified in function signature):
///   → mintPublic(address nftContract, address feeRecipient, address minter, uint256 quantity)
///   → called on canonical SEADROP_ROUTER, passing nftContract as argument
///
/// 2. Direct EVM path (any custom or standard function on target contract):
///   • mint(uint256)             → selector + qty
///   • publicMint(uint256)       → selector + qty
///   • safeMint(address,uint256) → selector + minter + qty
///   • mint(address,uint256)     → selector + minter + qty
///   • mint(uint256,address)     → selector + qty + minter
///   • mint(address)             → selector + minter
///   • mint()                    → selector (no args)
///   • ANY custom signature      → sanitized ABI encoding
///
/// Returns (to_address, calldata_hex, value_hex)
pub fn encode_direct_calldata(
    mint_function: &str,
    nft_contract: &str,
    wallet_address: &str,
    quantity: u32,
    mint_price_wei: u128,
    is_seadrop_override: bool,
) -> anyhow::Result<WalletCalldata> {
    let fn_name_lower = mint_function.to_lowercase();

    // ── SeaDrop routing ───────────────────────────────────────────────────────
    let is_seadrop = is_seadrop_override
        || fn_name_lower.contains("mintseadrop")
        || fn_name_lower.contains("seadrop")
        || fn_name_lower.starts_with("mintpublic(address")
        || fn_name_lower == "mintpublic(address,address,address,uint256)";

    if is_seadrop {
        // mintPublic(address nftContract, address feeRecipient, address minterIfNotPayer, uint256 quantity)
        let sel = selector("mintPublic(address,address,address,uint256)");
        let data = format!(
            "{}{}{}{}{}",
            sel,
            encode_address(nft_contract),
            encode_address(SEADROP_FEE_RECIPIENT),
            encode_address(wallet_address),
            encode_uint256(quantity as u128),
        );
        let to = format!("0x{}", SEADROP_ROUTER);
        let value = format!("0x{:x}", mint_price_wei * quantity as u128);
        info!("Calldata: SeaDrop mintPublic on {} for NFT {} qty={}", &to[..12], &nft_contract[..10], quantity);
        return Ok(WalletCalldata {
            wallet_address: wallet_address.to_string(),
            to,
            data: format!("0x{}", data),
            value,
        });
    }

    // ── Direct Contract Path: Parse function signature ─────────────────────────
    let (fn_name, params_str) = if let (Some(l), Some(r)) = (
        mint_function.find('('),
        mint_function.rfind(')')
    ) {
        (&mint_function[..l], &mint_function[l+1..r])
    } else {
        // Bare name with no parens — treat as mint(uint256)
        (mint_function, "uint256")
    };

    // Sanitize parameter types (remove any parameter names e.g. "uint256 amount" -> "uint256")
    let params: Vec<&str> = if params_str.trim().is_empty() {
        vec![]
    } else {
        params_str.split(',')
            .map(|s| s.trim().split_whitespace().next().unwrap_or("").trim())
            .filter(|s| !s.is_empty())
            .collect()
    };

    let canonical_sig = format!("{}({})", fn_name.trim(), params.join(","));
    let sel = selector(&canonical_sig);
    let to = nft_contract.to_string();
    let value = format!("0x{:x}", mint_price_wei * quantity as u128);

    // Build calldata based on parameter pattern
    let args = if params.is_empty() {
        String::new()
    } else if params.len() == 1 {
        let p0 = params[0].to_lowercase();
        if p0 == "address" {
            encode_address(wallet_address)
        } else {
            // uint256, uint32, etc.
            encode_uint256(quantity as u128)
        }
    } else if params.len() == 2 {
        let p0 = params[0].to_lowercase();
        let p1 = params[1].to_lowercase();
        if p0 == "address" && p1.starts_with("uint") {
            // safeMint(address,uint256) or mint(address,uint256)
            format!("{}{}", encode_address(wallet_address), encode_uint256(quantity as u128))
        } else if p0.starts_with("uint") && p1 == "address" {
            // mint(uint256,address)
            format!("{}{}", encode_uint256(quantity as u128), encode_address(wallet_address))
        } else if p0.starts_with("uint") && p1.starts_with("uint") {
            // mint(uint256 id, uint256 quantity)
            format!("{}{}", encode_uint256(0), encode_uint256(quantity as u128))
        } else {
            encode_uint256(quantity as u128)
        }
    } else {
        // Multi-param fallback: encode quantity as first uint256 arg
        encode_uint256(quantity as u128)
    };

    let data = format!("0x{}{}", sel, args);
    info!("Calldata: direct {} ({}) → {} qty={}", fn_name.trim(), canonical_sig, &to[..10], quantity);

    Ok(WalletCalldata {
        wallet_address: wallet_address.to_string(),
        to,
        data,
        value,
    })
}

// ── Direct mint calldata map (one per wallet) ─────────────────────────────────

/// Build direct calldata for all wallets instantly for ANY contract address.
pub fn build_direct_calldata_for_wallets(
    mint_function: &str,
    nft_contract: &str,
    wallets: &[String],
    quantity: u32,
    mint_price_wei: u128,
    is_seadrop_override: bool,
) -> HashMap<String, CalldataResult> {
    let mut map = HashMap::new();
    for wallet in wallets {
        match encode_direct_calldata(mint_function, nft_contract, wallet, quantity, mint_price_wei, is_seadrop_override) {
            Ok(cd) => {
                map.insert(wallet.clone(), CalldataResult::Success(cd));
            }
            Err(e) => {
                warn!("Calldata build failed for {}: {}", &wallet[..10], e);
                map.insert(wallet.clone(), CalldataResult::Error(e.to_string()));
            }
        }
    }
    map
}

// ── OpenSea GraphQL calldata fetch (for opensea slugs) ────────────────────────

/// Tight retry loop starting at T-1.5s.
/// Retries the batch query immediately on DropNotMintingError until drop goes live.
/// Permanently-failed wallets (InsufficientFund) are excluded from retries.
pub async fn fetch_calldata_with_retry(
    client: &Client,
    cfg: &OpenSeaConfig,
    cookies: &str,
    chain_slug: &str,
    nft_contract: &str,
    all_wallets: &[String],
    quantity: u32,
    fire_time: DateTime<Utc>,
) -> anyhow::Result<HashMap<String, CalldataResult>> {
    // Wallets that should still be retried (remove permanently failed ones)
    let mut retry_wallets: Vec<String> = all_wallets.to_vec();
    let mut final_results: HashMap<String, CalldataResult> = HashMap::new();

    let deadline = fire_time + chrono::Duration::seconds(30); // give up 30s after fire time
    let mut attempt = 0u32;

    loop {
        attempt += 1;
        let now = Utc::now();

        if now > deadline {
            warn!("Calldata: deadline exceeded after {} attempts", attempt);
            break;
        }

        if retry_wallets.is_empty() {
            info!("Calldata: all wallets resolved after {} attempts", attempt);
            break;
        }

        debug!("Calldata: attempt {} — {} wallets", attempt, retry_wallets.len());

        let batch = fetch_batch_calldata(
            client, cfg, cookies,
            chain_slug, nft_contract,
            &retry_wallets, quantity,
        ).await;

        match batch {
            Err(e) => {
                warn!("Calldata: batch request failed ({}), retrying in 50ms", e);
                tokio::time::sleep(std::time::Duration::from_millis(50)).await;
                continue;
            }
            Ok(results) => {
                let mut still_pending = Vec::new();
                let mut has_live = false;

                for (addr, result) in results {
                    match &result {
                        CalldataResult::Success(_) => {
                            info!("Calldata: ✓ success for {}", &addr[..10]);
                            has_live = true;
                            final_results.insert(addr, result);
                        }
                        CalldataResult::InsufficientFund => {
                            warn!("Calldata: insufficient funds for {}, skipping", &addr[..10]);
                            final_results.insert(addr, result);
                        }
                        CalldataResult::DropNotLive => {
                            still_pending.push(addr);
                        }
                        CalldataResult::Error(e) => {
                            warn!("Calldata: error for {} ({}), will retry", &addr[..10], e);
                            still_pending.push(addr);
                        }
                    }
                }

                if has_live && now >= fire_time {
                    info!("Calldata: {} successful, {} pending — proceeding to sign",
                        final_results.values().filter(|r| matches!(r, CalldataResult::Success(_))).count(),
                        still_pending.len()
                    );
                    for addr in still_pending {
                        final_results.entry(addr).or_insert(CalldataResult::DropNotLive);
                    }
                    break;
                }

                retry_wallets = still_pending;
                tokio::time::sleep(std::time::Duration::from_millis(50)).await;
            }
        }
    }

    Ok(final_results)
}
