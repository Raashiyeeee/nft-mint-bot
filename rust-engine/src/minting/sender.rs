// src/minting/sender.rs — Transaction confirmation polling

use reqwest::Client;
use std::time::Duration;
use tokio::time::sleep;
use tracing::{info, warn, debug};

use crate::chains::{ChainRegistry, ChainConfig};

/// Wait for transaction confirmation.
/// - Base: uses base_transactionStatus first (preconfirmation)
/// - Others: eth_getTransactionReceipt with 500ms intervals + exponential backoff
///
/// Returns (status, token_id, gas_used)
pub async fn wait_for_confirmation(
    client: &Client,
    chain_registry: &ChainRegistry,
    chain: &str,
    chain_cfg: &ChainConfig,
    tx_hash: &str,
) -> anyhow::Result<(String, Option<String>, Option<u64>)> {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(120);
    let mut interval_ms = 500u64;

    // ── Base chain: use base_transactionStatus for preconfirmation ────────────
    if chain == "base" && chain_cfg.use_base_preconf {
        for _ in 0..20 {
            if tokio::time::Instant::now() > deadline {
                break;
            }
            match chain_registry.base_tx_status(client, tx_hash).await {
                Ok(status) => {
                    debug!("base_transactionStatus: {}", status);
                    match status.as_str() {
                        "Preconfirmed" | "Known" => {
                            info!("TX {} preconfirmed on Base", &tx_hash[..12]);
                            // Fall through to get full receipt
                            break;
                        }
                        "Unknown" => {
                            sleep(Duration::from_millis(200)).await;
                            continue;
                        }
                        _ => break,
                    }
                }
                Err(e) => {
                    warn!("base_transactionStatus error: {}", e);
                    break;
                }
            }
        }
    }

    // ── Full receipt polling ──────────────────────────────────────────────────
    loop {
        if tokio::time::Instant::now() > deadline {
            warn!("TX {}: confirmation timeout", &tx_hash[..12]);
            return Ok(("timeout".into(), None, None));
        }

        match chain_registry.get_receipt(client, chain, tx_hash).await {
            Ok(Some(receipt)) => {
                let status_hex = receipt["status"].as_str().unwrap_or("0x0");
                let success = status_hex == "0x1";
                let gas_used = receipt["gasUsed"]
                    .as_str()
                    .and_then(|s| u64::from_str_radix(s.trim_start_matches("0x"), 16).ok());

                // Extract token ID from Transfer event logs (ERC-721)
                let token_id = extract_token_id_from_logs(&receipt);

                let status = if success { "success" } else { "failed" }.to_string();
                info!("TX {} confirmed: {} (gas={:?}, token={:?})",
                    &tx_hash[..12], status, gas_used, token_id);

                return Ok((status, token_id, gas_used));
            }
            Ok(None) => {
                debug!("TX {}: no receipt yet, waiting {}ms", &tx_hash[..12], interval_ms);
                sleep(Duration::from_millis(interval_ms)).await;
                // Exponential backoff up to 5s
                interval_ms = (interval_ms * 2).min(5000);
            }
            Err(e) => {
                warn!("Receipt poll error for {}: {}", &tx_hash[..12], e);
                sleep(Duration::from_millis(interval_ms)).await;
                interval_ms = (interval_ms * 2).min(5000);
            }
        }
    }
}

/// Extract token ID from ERC-721 Transfer event in receipt logs.
/// Transfer event: Transfer(address indexed from, address indexed to, uint256 indexed tokenId)
/// Topic[0] = keccak256("Transfer(address,address,uint256)")
/// Topic[3] = tokenId
fn extract_token_id_from_logs(receipt: &serde_json::Value) -> Option<String> {
    let transfer_sig = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef";

    let logs = receipt["logs"].as_array()?;
    for log in logs {
        let topics = log["topics"].as_array()?;
        if topics.first()?.as_str() == Some(transfer_sig) && topics.len() >= 4 {
            let token_id_hex = topics[3].as_str()?;
            if let Ok(id) = u128::from_str_radix(token_id_hex.trim_start_matches("0x"), 16) {
                return Some(id.to_string());
            }
        }
    }
    None
}
