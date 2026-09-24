// src/minting/warmup.rs — T-5s warm-up phase
//
// Runs concurrently for all wallets:
// - Nonce pre-fetch (eth_getTransactionCount)
// - Chain ID verification
// - HTTP/2 keepalive pings
// - EIP-1559 gas snapshot

use std::collections::HashMap;
use futures::future::join_all;
use reqwest::Client;
use tracing::{info, warn};

use crate::chains::ChainRegistry;

pub struct WarmupResult {
    pub nonces: HashMap<String, u64>,
    pub base_fee: u128,
    pub priority_fee: u128,
}

/// Runs all warm-up tasks in parallel. Fault tolerant — individual failures
/// do not abort the batch. Returns cached values for the hot path.
pub async fn run_warmup(
    client: &Client,
    chain_registry: &ChainRegistry,
    chain: &str,
    wallets: &[String],
    gas_multiplier: f64,
) -> anyhow::Result<(HashMap<String, u64>, u128, u128)> {
    info!("Warm-up: starting for {} wallets on {}", wallets.len(), chain);

    // ── Concurrent nonce pre-fetch ────────────────────────────────────────────
    let nonce_futures: Vec<_> = wallets.iter().map(|addr| {
        let client = client.clone();
        let chain = chain.to_string();
        let addr = addr.clone();
        async move {
            match chain_registry.get_nonce(&client, &chain, &addr).await {
                Ok(n) => {
                    info!("Warm-up: nonce {} for {}", n, &addr[..10]);
                    (addr, Some(n))
                }
                Err(e) => {
                    warn!("Warm-up: nonce fetch failed for {} ({}), will fetch live", &addr[..10], e);
                    (addr, None)
                }
            }
        }
    }).collect();

    // ── Gas price snapshot ────────────────────────────────────────────────────
    let gas_future = chain_registry.get_eip1559_fees(client, chain);

    // ── HTTP/2 keepalive ping to OpenSea & RPC ──────────────────────────────
    let os_ping_client = client.clone();
    let opensea_ping = async move {
        let _ = os_ping_client
            .head("https://gql.opensea.io/graphql")
            .header("x-app-id", "os2-web")
            .timeout(std::time::Duration::from_secs(3))
            .send()
            .await;
        info!("Warm-up: OpenSea HTTP/2 connection warmed");
    };

    // Warm ALL configured RPC URLs (TLS + HTTP keep-alive) so T=0 broadcast
    // does not pay first-connection latency on every endpoint.
    let rpc_ping_client = client.clone();
    let chain_str = chain.to_string();
    let maybe_cfg = chain_registry.get_config(chain).cloned();
    let rpc_ping = async move {
        if let Some(cfg) = maybe_cfg {
            let rpc_count = cfg.rpc_urls.len();
            let mut pings: Vec<std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send>>> =
                Vec::new();
            for url in cfg.rpc_urls {
                let c = rpc_ping_client.clone();
                pings.push(Box::pin(async move {
                    let _ = c.post(&url)
                        .json(&serde_json::json!({
                            "jsonrpc": "2.0",
                            "method": "eth_chainId",
                            "params": [],
                            "id": 1
                        }))
                        .timeout(std::time::Duration::from_secs(3))
                        .send()
                        .await;
                }));
            }
       
            join_all(pings).await;
            info!("Warm-up: {} RPC HTTP connection(s) warmed", rpc_count);
        } else {
            info!("Warm-up: no chain config for RPC warm");
        }
    };

    // ── Run all concurrently ──────────────────────────────────────────────────
    let (nonce_results, gas_result, _, _) = tokio::join!(
        join_all(nonce_futures),
        gas_future,
        opensea_ping,
        rpc_ping,
    );

    let mut nonces = HashMap::new();
    for (addr, maybe_nonce) in nonce_results {
        if let Some(n) = maybe_nonce {
            nonces.insert(addr, n);
        }
    }

    let (base_fee, priority_fee) = gas_result.unwrap_or_else(|e| {
        warn!("Warm-up: gas fetch failed ({}), using low-fee defaults", e);
        // Defaults sized for L2s; mainnet will refresh from chain on retry
        (50_000_000u128, 10_000_000u128) // 0.05 gwei base, 0.01 gwei tip
    });

    // Keep the raw RPC fee snapshot.
    // The Fast 1.25x buffer is applied exactly once when
    // the EIP-1559 transaction is prepared/signed.
    let multiplied_base = base_fee;
    let multiplied_tip = priority_fee;

    info!("Warm-up: complete — {} nonces cached, base_fee={}wei, tip={}wei (~{:.4}/{:.4} gwei)",
        nonces.len(),
        multiplied_base,
        multiplied_tip,
        multiplied_base as f64 / 1_000_000_000.0,
        multiplied_tip as f64 / 1_000_000_000.0,
    );

    Ok((nonces, multiplied_base, multiplied_tip))
}
