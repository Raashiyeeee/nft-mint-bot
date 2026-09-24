// src/minting/nonce.rs — Cross-session per-wallet nonce allocator
//
// Critical for concurrent mints: if wallet A is in session #1 (FCFS) and
// session #2 (public) firing at the same second, both must get distinct
// sequential nonces. Without this, both warmups cache the same nonce and
// one tx is dropped as "nonce too low" / replaced.

use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::{Mutex, RwLock};
use tracing::{info, warn};
use reqwest::Client;

use crate::chains::ChainRegistry;

struct NonceSlot {
    /// Next nonce to hand out. None = not seeded from chain yet.
    next: Option<u64>,
}

/// Process-wide allocator keyed by `"{chain}:{wallet_lower}"`.
#[derive(Clone, Default)]
pub struct NonceAllocator {
    slots: Arc<RwLock<HashMap<String, Arc<Mutex<NonceSlot>>>>>,
}

impl NonceAllocator {
    pub fn new() -> Self {
        Self::default()
    }

    fn key(chain: &str, wallet: &str) -> String {
        format!("{}:{}", chain.to_lowercase(), wallet.to_lowercase())
    }

    async fn slot(&self, chain: &str, wallet: &str) -> Arc<Mutex<NonceSlot>> {
        let k = Self::key(chain, wallet);
        {
            let map = self.slots.read().await;
            if let Some(s) = map.get(&k) {
                return s.clone();
            }
        }
        let mut map = self.slots.write().await;
        map.entry(k)
            .or_insert_with(|| Arc::new(Mutex::new(NonceSlot { next: None })))
            .clone()
    }

    /// Allocate the next nonce for (chain, wallet), seeding from RPC if needed.
    /// Optional `hint` from warmup is used only when the slot is still unseeded.
    pub async fn allocate(
        &self,
        client: &Client,
        chain_registry: &ChainRegistry,
        chain: &str,
        wallet: &str,
        hint: Option<u64>,
    ) -> anyhow::Result<u64> {
        let slot = self.slot(chain, wallet).await;
        let mut guard = slot.lock().await;

        if guard.next.is_none() {
            let seeded = if let Some(h) = hint {
                h
            } else {
                chain_registry.get_nonce(client, chain, wallet).await?
            };
            guard.next = Some(seeded);
            info!("NonceAllocator: seeded {} @ {} → {}", &wallet[..10.min(wallet.len())], chain, seeded);
        }

        let n = guard.next.unwrap();
        guard.next = Some(n + 1);
        Ok(n)
    }

    /// After a failed send (nonce too low / already known), re-sync from chain.
    pub async fn resync(
        &self,
        client: &Client,
        chain_registry: &ChainRegistry,
        chain: &str,
        wallet: &str,
    ) -> anyhow::Result<u64> {
        let fresh = chain_registry.get_nonce(client, chain, wallet).await?;
        let slot = self.slot(chain, wallet).await;
        let mut guard = slot.lock().await;
        // Only move forward — never rewind under concurrency
        let next = match guard.next {
            Some(cur) if cur > fresh => cur,
            _ => fresh,
        };
        guard.next = Some(next);
        warn!(
            "NonceAllocator: resync {} @ {} → {}",
            &wallet[..10.min(wallet.len())],
            chain,
            next
        );
        Ok(next)
    }

    /// Prefill from warmup map without allocating (so first allocate() uses these).
    /// Always move the slot forward if warmup sees a higher on-chain nonce.
    pub async fn seed_batch(&self, chain: &str, nonces: &HashMap<String, u64>) {
        for (wallet, &n) in nonces {
            let slot = self.slot(chain, wallet).await;
            let mut guard = slot.lock().await;
            match guard.next {
                None => guard.next = Some(n),
                Some(cur) if n > cur => {
                    warn!(
                        "NonceAllocator: warmup advanced {} @ {} {} → {}",
                        &wallet[..10.min(wallet.len())],
                        chain,
                        cur,
                        n
                    );
                    guard.next = Some(n);
                }
                _ => {}
            }
        }
    }
}
