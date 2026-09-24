// src/minting/mod.rs — Minting orchestrator

pub mod calldata;
pub mod nonce;
pub mod sender;
pub mod signer;
pub mod warmup;

use std::sync::Arc;
use chrono::{Utc, DateTime};
use sqlx::PgPool;
use tracing::{info, error, warn};

use crate::chains::ChainRegistry;
use crate::opensea::graphql::CalldataResult;
use crate::opensea::scheduler::SessionManager;

/// Top-level mint session runner. Called from scheduler when T-5s fires.
pub async fn run_mint_session(
    sm: Arc<SessionManager>,
    chain_registry: Arc<ChainRegistry>,
    db: PgPool,
    session_id: i32,
    telegram_id: i64,
    drop_slug: String,
    drop_name: String,
    nft_contract: String,
    chain: String,
    quantity_per_wallet: u32,
    gas_multiplier: f64,
    fire_time: DateTime<Utc>,
    fernet_key: String,
) -> anyhow::Result<()> {
    info!("Session {}: starting mint pipeline for '{}'", session_id, drop_name);

    // ── Load session details (mint_function, mint_price_wei, gas_limit, tx_retry, max_retries, notify flags) ───
    use sqlx::Row;
    let session_row = sqlx::query(
        "SELECT mint_function, CAST(mint_price_wei AS TEXT) as mint_price_wei_str, gas_limit, tx_retry, max_retries, notify_success, notify_fail FROM mint_sessions WHERE id = $1"
    )
    .bind(session_id)
    .fetch_optional(&db)
    .await?;

    let mint_function = session_row.as_ref()
        .and_then(|r| r.try_get::<String, _>("mint_function").ok())
        .unwrap_or_else(|| "mint(uint256)".to_string());

    let mint_price_wei: u128 = session_row.as_ref()
        .and_then(|r| r.try_get::<String, _>("mint_price_wei_str").ok())
        .and_then(|s| s.parse::<u128>().ok())
        .unwrap_or(0);

    let session_gas_limit: u64 = session_row.as_ref()
        .and_then(|r| r.try_get::<Option<i64>, _>("gas_limit").ok().flatten())
        .map(|g| g as u64)
        .unwrap_or(350_000u64);
    info!("Session {}: configured gas_limit = {}", session_id, session_gas_limit);

    let tx_retry: bool = session_row.as_ref()
        .and_then(|r| r.try_get::<bool, _>("tx_retry").ok())
        .unwrap_or(true);

    let max_retries: i32 = session_row.as_ref()
        .and_then(|r| r.try_get::<i32, _>("max_retries").ok())
        .unwrap_or(3);

    let notify_success: bool = session_row.as_ref()
        .and_then(|r| r.try_get::<bool, _>("notify_success").ok())
        .unwrap_or(true);

    let notify_fail: bool = session_row.as_ref()
        .and_then(|r| r.try_get::<bool, _>("notify_fail").ok())
        .unwrap_or(true);

    let max_attempts = if tx_retry { max_retries.max(1) as u32 } else { 1 };

    // ── Load wallets for this session ────────────────────────────────────────
    let wallet_rows = sqlx::query(
        r#"
        SELECT w.id, w.wallet_address, w.chain, w.private_key_encrypted
        FROM session_wallets sw
        JOIN user_wallets w ON w.id = sw.wallet_id
        WHERE sw.session_id = $1 AND w.is_active = TRUE
        "#
    )
    .bind(session_id)
    .fetch_all(&db)
    .await?;

    if wallet_rows.is_empty() {
        warn!("Session {}: no wallets found, aborting", session_id);
        return Ok(());
    }

    info!("Session {}: {} wallets loaded", session_id, wallet_rows.len());

    // ── Resolve chain config ──────────────────────────────────────────────────
    let chain_cfg = chain_registry.get_config(&chain)
        .ok_or_else(|| anyhow::anyhow!("Unknown chain: {}", chain))?
        .clone();

    let chain_slug = chain_cfg.opensea_chain_slug.clone();

    // FCFS / signed stages need OpenSea REST mint-build (API key only — no SIWE).
    // Public SeaDrop / direct contracts use local calldata (no OpenSea).
    let is_signed_fcfs = mint_function.starts_with("mintSigned") || mint_function.contains("Signed");

    let is_direct_mint = !is_signed_fcfs && (
        drop_slug.starts_with("0x")
        || drop_slug.is_empty()
        || chain == "arc"
        || chain_cfg.opensea_chain_slug.is_empty()
    );

    // ── Decrypt keys (OpenSea SIWE no longer required for FCFS) ───────────────
    let mut wallet_keys: Vec<(String, [u8; 32])> = Vec::new();

    for row in &wallet_rows {
        let wallet_address: String = row.get("wallet_address");
        let private_key_encrypted: String = row.get("private_key_encrypted");

        let key_bytes = match crate::minting::signer::decrypt_private_key(
            &private_key_encrypted,
            &fernet_key,
        ) {
            Ok(k) => k,
            Err(e) => {
                error!("Session {}: failed to decrypt key for {}: {}", session_id, &wallet_address[..10], e);
                continue;
            }
        };
        wallet_keys.push((wallet_address, key_bytes));
    }

    if wallet_keys.is_empty() {
        error!("Session {}: all wallet decryptions failed", session_id);
        return Ok(());
    }

    let wallet_addresses: Vec<String> = wallet_keys.iter().map(|(a, _)| a.clone()).collect();

    // ── Phase 1: Warm-up (T-5s from fire time) ───────────────────────────────
    let now = Utc::now();
    let warmup_start = fire_time - chrono::Duration::seconds(5);
    if now < warmup_start {
        crate::opensea::scheduler::sleep_until_precise(warmup_start).await;
    }

    let warmup_result = match warmup::run_warmup(
        &sm.http_client,
        &chain_registry,
        &chain,
        &wallet_addresses,
        gas_multiplier,
    ).await {
        Ok(r) => Ok(r),
        Err(e) => {
            warn!("Session {}: warmup attempt 1 failed ({}), rotating RPC and retrying...", session_id, e);
            chain_registry.rotate_rpc(&chain).await;
            warmup::run_warmup(
                &sm.http_client,
                &chain_registry,
                &chain,
                &wallet_addresses,
                gas_multiplier,
            ).await
        }
    };

    let (cached_nonces, base_fee, priority_fee) = match warmup_result {
        Ok(r) => r,
        Err(e) => {
            warn!("Session {}: warmup failed after retry ({}), falling back to live fetch", session_id, e);
            (std::collections::HashMap::new(), 50_000_000u128, 10_000_000u128)
        }
    };

    // Seed cross-session nonce allocator (same wallet in parallel sessions)
    sm.nonce_allocator.seed_batch(&chain, &cached_nonces).await;

    // ── Notify: firing ────────────────────────────────────────────────────────
    sm.notify(serde_json::json!({
        "type": "firing",
        "telegram_id": telegram_id,
        "session_id": session_id,
        "drop_name": drop_name,
        "wallet_count": wallet_keys.len(),
    })).await;

    // ── Phase 2: Calldata ─────────────────────────────────────────────────────
    //   • direct / public SeaDrop → local ABI encode (instant)
    //   • FCFS mintSigned        → OpenSea REST /drops/{slug}/mint (parallel wallets)
    let (calldata_map, _is_seadrop) = if is_direct_mint {
        let rpc_url = chain_cfg.rpc_urls.first().map(|s| s.as_str()).unwrap_or("");
        let seadrop_start = calldata::get_seadrop_start_time_onchain(&sm.http_client, rpc_url, &nft_contract).await;
        let is_seadrop = seadrop_start.is_some();

        info!("Session {}: building direct EVM calldata (fn: '{}', price: {} wei, seadrop_detected: {})",
            session_id, mint_function, mint_price_wei, is_seadrop);

        let map = calldata::build_direct_calldata_for_wallets(
            &mint_function,
            &nft_contract,
            &wallet_addresses,
            quantity_per_wallet,
            mint_price_wei,
            is_seadrop,
        );
        (map, is_seadrop)
    } else if is_signed_fcfs {
        info!("Session {}: FCFS via OpenSea REST mint-build (slug/ca='{}')", session_id, drop_slug);
        let map = crate::opensea::rest::fetch_rest_calldata_with_retry(
            &sm.http_client,
            &sm.opensea_cfg,
            &chain_slug,
            &drop_slug,
            &nft_contract,
            &wallet_addresses,
            quantity_per_wallet,
            fire_time,
        ).await?;
        (map, true)
    } else {
        // Legacy OpenSea GraphQL path (slug-based non-signed drops)
        let calldata_start = fire_time - chrono::Duration::milliseconds(1500);
        let now = Utc::now();
        if now < calldata_start {
            crate::opensea::scheduler::sleep_until_precise(calldata_start).await;
        }

        let map = calldata::fetch_calldata_with_retry(
            &sm.http_client,
            &sm.opensea_cfg,
            "",
            &chain_slug,
            &nft_contract,
            &wallet_addresses,
            quantity_per_wallet,
            fire_time,
        ).await?;
        (map, false)
    };

    // ── Phase 3 prep: resolve calldata, allocate nonces, PRE-SIGN before T=0 ──
    // Hot path at fire_time must only broadcast — signing here costs tens–hundreds µs
    // but under load / many wallets it adds measurable delay vs competitive bots.
    struct PreparedWallet {
        wallet_addr: String,
        key_bytes: [u8; 32],
        calldata: crate::opensea::graphql::WalletCalldata,
        nonce: u64,
        raw_tx: String,
    }

    let mut prepared: Vec<PreparedWallet> = Vec::new();

    for (wallet_addr, key_bytes) in wallet_keys {
        let calldata_result = calldata_map.get(&wallet_addr).cloned();

        let calldata = match calldata_result {
            Some(CalldataResult::Success(cd)) => cd,
            Some(CalldataResult::InsufficientFund) => {
                let _ = sqlx::query(
                    "INSERT INTO mint_results (session_id, wallet_address, status, error_msg)
                     VALUES ($1, $2, 'skipped', 'InsufficientFund')"
                )
                .bind(session_id)
                .bind(&wallet_addr)
                .execute(&db).await;

                sm.notify(serde_json::json!({
                    "type": "tx_skipped",
                    "telegram_id": telegram_id,
                    "wallet": &wallet_addr[wallet_addr.len().saturating_sub(6)..],
                    "reason": "InsufficientFund",
                })).await;
                continue;
            }
            Some(CalldataResult::DropNotLive) | Some(CalldataResult::Error(_)) | None => {
                let reason = match calldata_result {
                    Some(CalldataResult::Error(e)) => e,
                    _ => "No calldata".to_string(),
                };
                let _ = sqlx::query(
                    "INSERT INTO mint_results (session_id, wallet_address, status, error_msg)
                     VALUES ($1, $2, 'failed', $3)"
                )
                .bind(session_id)
                .bind(&wallet_addr)
                .bind(&reason)
                .execute(&db).await;
                continue;
            }
        };

        let hint = cached_nonces.get(&wallet_addr).copied();
        let nonce = match sm.nonce_allocator
            .allocate(&sm.http_client, &chain_registry, &chain, &wallet_addr, hint)
            .await
        {
            Ok(n) => n,
            Err(e) => {
                error!("Session {}: nonce alloc failed for {}: {}", session_id, &wallet_addr[..10], e);
                continue;
            }
        };

        let safe_base_fee = std::cmp::max(base_fee, 1u128);
        let safe_priority_fee = std::cmp::max(priority_fee, 1u128);
        let buffered_base = safe_base_fee.saturating_mul(5).saturating_div(4);
        let effective_max_fee = buffered_base.saturating_add(safe_priority_fee);

        let raw_tx = match signer::build_and_sign_tx(
            &key_bytes,
            &calldata.to,
            &calldata.data,
            &calldata.value,
            nonce,
            chain_cfg.chain_id,
            effective_max_fee,
            safe_priority_fee,
            gas_multiplier,
            session_gas_limit,
        ) {
            Ok(tx) => tx,
            Err(e) => {
                error!("Session {}: pre-sign failed for {}: {}", session_id, &wallet_addr[..10], e);
                let _ = sqlx::query(
                    "INSERT INTO mint_results (session_id, wallet_address, status, error_msg)
                     VALUES ($1, $2, 'failed', $3)"
                )
                .bind(session_id)
                .bind(&wallet_addr)
                .bind(format!("Pre-sign failed: {}", e))
                .execute(&db).await;
                continue;
            }
        };

        prepared.push(PreparedWallet {
            wallet_addr,
            key_bytes,
            calldata,
            nonce,
            raw_tx,
        });
    }

    if prepared.is_empty() {
        warn!("Session {}: no wallets prepared to send", session_id);
        let _ = sqlx::query(
            "UPDATE mint_sessions SET status = 'completed', fired_at = $1 WHERE id = $2"
        )
        .bind(Utc::now().naive_utc())
        .bind(session_id)
        .execute(&db)
        .await;
        return Ok(());
    }

    info!(
        "Session {}: {} wallet(s) pre-signed — precision wait until {:?}",
        session_id, prepared.len(), fire_time
    );
    // No artificial post-T=0 delay (old 150ms SeaDrop buffer). Fire immediately;
    // NotActive reverts are handled by the normal retry path.
    crate::opensea::scheduler::sleep_until_precise(fire_time).await;

    // ── Phase 3: Broadcast only (concurrent per wallet) ───────────────────────
    let fire_timestamp = Utc::now();
    let mut send_handles = Vec::new();

    for prep in prepared {
        let wallet_addr = prep.wallet_addr;
        let key_bytes = prep.key_bytes;
        let calldata = prep.calldata;
        let nonce = prep.nonce;
        let pre_signed = prep.raw_tx;

        let db2 = db.clone();
        let cr2 = chain_registry.clone();
        let client2 = sm.http_client.clone();
        let sm2 = sm.clone();
        let chain2 = chain.clone();
        let chain_cfg2 = chain_cfg.clone();
        let drop_name2 = drop_name.clone();
        let notify_success2 = notify_success;
        let notify_fail2 = notify_fail;
        let nonce_alloc = sm.nonce_allocator.clone();

        let handle = tokio::spawn(async move {
            let mut current_nonce = nonce;
            let mut current_base_fee = base_fee;
            let mut current_priority_fee = priority_fee;
            // Only bump nonce after a nonce-related send failure. Reusing the
            // same nonce on gas/RPC retries avoids holes that stall the wallet.
            let mut need_fresh_nonce = false;
            let mut use_presigned: Option<String> = Some(pre_signed);

            for attempt in 1..=max_attempts {
                let start_ms = std::time::Instant::now();

                if attempt > 1 {
                    tokio::time::sleep(std::time::Duration::from_millis(400)).await;
                    use_presigned = None;
                    if need_fresh_nonce {
                        let _ = nonce_alloc.resync(&client2, &cr2, &chain2, &wallet_addr).await;
                        if let Ok(n) = nonce_alloc
                            .allocate(&client2, &cr2, &chain2, &wallet_addr, None)
                            .await
                        {
                            current_nonce = n;
                        }
                        need_fresh_nonce = false;
                    }
                    if let Ok((b_fee, p_fee)) = cr2.get_eip1559_fees(&client2, &chain2).await {
                        current_base_fee = b_fee;
                        current_priority_fee = p_fee;
                    }
                }

                // EIP-1559: max_fee_per_gas = base_fee + priority_fee.
                // base_fee here already includes the user's gas multiplier (applied at warmup).
                // We add a 25% safety buffer on top for block-to-block fee spikes — do NOT
                // multiply by 2 again (that would compound to 4-6x on low-fee chains like Robinhood).
                // Do NOT floor at 1 gwei — that overpays massively on L2s (~0.05 gwei) and causes
                // "insufficient funds for gas" on wallets that only hold a little ETH for gas.
                let raw_tx = if let Some(tx) = use_presigned.take() {
                    tx
                } else {
                    let safe_base_fee = std::cmp::max(current_base_fee, 1u128);
                    let safe_priority_fee = std::cmp::max(current_priority_fee, 1u128);
                    let buffered_base = safe_base_fee.saturating_mul(5).saturating_div(4);
                    let effective_max_fee = buffered_base.saturating_add(safe_priority_fee);

                    match signer::build_and_sign_tx(
                        &key_bytes,
                        &calldata.to,
                        &calldata.data,
                        &calldata.value,
                        current_nonce,
                        chain_cfg2.chain_id,
                        effective_max_fee,
                        safe_priority_fee,
                        gas_multiplier,
                        session_gas_limit,
                    ) {
                        Ok(tx) => tx,
                        Err(e) => {
                            error!("Sign failed for {} (attempt {}/{}): {}", &wallet_addr[..10], attempt, max_attempts, e);
                            if attempt == max_attempts {
                                let _ = sqlx::query(
                                    "INSERT INTO mint_results (session_id, wallet_address, status, error_msg)
                                     VALUES ($1, $2, 'failed', $3)"
                                )
                                .bind(session_id)
                                .bind(&wallet_addr)
                                .bind(format!("Sign failed: {}", e))
                                .execute(&db2).await;
                            }
                            continue;
                        }
                    }
                };

                // Send
                let tx_hash = match cr2.send_raw_tx(&client2, &chain2, &raw_tx).await {
                    Ok(h) => {
                        info!("Session {}: tx sent for {} (attempt {}/{}) → {}", session_id, &wallet_addr[..10], attempt, max_attempts, &h);
                        h
                    }
                    Err(e) => {
                        error!("Send failed for {} (attempt {}/{}): {}", &wallet_addr[..10], attempt, max_attempts, e);
                        let err_low = e.to_string().to_lowercase();
                        // Match nested RPC errors like "All RPC endpoints failed ... nonce too low"
                        if err_low.contains("nonce") || err_low.contains("too low") {
                            need_fresh_nonce = true;
                            let _ = nonce_alloc.resync(&client2, &cr2, &chain2, &wallet_addr).await;
                        }
                        let _ = sqlx::query(
                            "INSERT INTO tx_logs (session_id, wallet_address, attempt_no, status, error_msg)
                             VALUES ($1, $2, $3, 'failed', $4)"
                        )
                        .bind(session_id)
                        .bind(&wallet_addr)
                        .bind(attempt as i32)
                        .bind(e.to_string())
                        .execute(&db2).await;

                        if attempt == max_attempts {
                            let _ = sqlx::query(
                                "INSERT INTO mint_results (session_id, wallet_address, status, error_msg)
                                 VALUES ($1, $2, 'failed', $3)"
                            )
                            .bind(session_id)
                            .bind(&wallet_addr)
                            .bind(e.to_string())
                            .execute(&db2).await;
                        }
                        continue;
                    }
                };

                let explorer_url = format!("{}{}", chain_cfg2.explorer, tx_hash);

                // Record this attempt in tx_logs
                let _ = sqlx::query(
                    "INSERT INTO tx_logs (session_id, wallet_address, tx_hash, attempt_no, status, explorer_url)
                     VALUES ($1, $2, $3, $4, 'sent', $5)"
                )
                .bind(session_id)
                .bind(&wallet_addr)
                .bind(&tx_hash)
                .bind(attempt as i32)
                .bind(&explorer_url)
                .execute(&db2).await;

                if attempt == 1 {
                    let _ = sqlx::query(
                        "INSERT INTO mint_results (session_id, wallet_address, tx_hash, status)
                         VALUES ($1, $2, $3, 'sent')"
                    )
                    .bind(session_id)
                    .bind(&wallet_addr)
                    .bind(&tx_hash)
                    .execute(&db2).await;
                } else {
                    let _ = sqlx::query(
                        "UPDATE mint_results
                         SET tx_hash = $1, status = 'sent', error_msg = NULL
                         WHERE session_id = $2 AND wallet_address = $3"
                    )
                    .bind(&tx_hash)
                    .bind(session_id)
                    .bind(&wallet_addr)
                    .execute(&db2).await;
                }

                sm2.notify(serde_json::json!({
                    "type": "tx_sent",
                    "telegram_id": telegram_id,
                    "session_id": session_id,
                    "wallet": &wallet_addr[wallet_addr.len().saturating_sub(6)..],
                    "tx_hash": &tx_hash,
                    "explorer_url": &explorer_url,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                })).await;

                // ── Confirm ────────────────────────────────────────────────────────
                let (status, token_id, gas_used) = sender::wait_for_confirmation(
                    &client2, &cr2, &chain2, &chain_cfg2, &tx_hash
                ).await.unwrap_or(("failed".into(), None, None));

                let latency_ms = start_ms.elapsed().as_millis() as i32;

                // Update existing mint_results and tx_logs records
                let _ = sqlx::query(
                    "UPDATE mint_results
                     SET token_id = $1, gas_used = $2, latency_ms = $3, status = $4
                     WHERE session_id = $5 AND wallet_address = $6"
                )
                .bind(&token_id)
                .bind(gas_used.map(|g| g as i64))
                .bind(latency_ms)
                .bind(&status)
                .bind(session_id)
                .bind(&wallet_addr)
                .execute(&db2).await;

                let _ = sqlx::query(
                    "UPDATE tx_logs
                     SET status = $1, gas_used = $2
                     WHERE session_id = $3 AND tx_hash = $4"
                )
                .bind(&status)
                .bind(gas_used.map(|g| g as i64))
                .bind(session_id)
                .bind(&tx_hash)
                .execute(&db2).await;

                let should_notify = if status == "success" { notify_success2 } else { notify_fail2 };
                if should_notify {
                    sm2.notify(serde_json::json!({
                        "type": "tx_confirmed",
                        "telegram_id": telegram_id,
                        "session_id": session_id,
                        "drop_name": &drop_name2,
                        "chain": &chain2,
                        "wallet": &wallet_addr,
                        "tx_hash": tx_hash,
                        "token_id": token_id,
                        "latency_ms": latency_ms,
                        "status": status,
                        "attempt": attempt,
                    })).await;
                }

                if status == "success" {
                    info!("Session {}: Wallet {} minted successfully on attempt {}/{}!", session_id, &wallet_addr[..10], attempt, max_attempts);
                    break;
                } else if status == "failed" {
                    // Receipt confirmed an on-chain revert (e.g. MaxSupply / MaxPerWallet).
                    // Retrying the same calldata usually only burns more gas.
                    warn!(
                        "Session {}: Wallet {} on-chain revert on attempt {}/{} — not retrying",
                        session_id, &wallet_addr[..10], attempt, max_attempts
                    );
                    break;
                } else {
                    warn!("Session {}: Wallet {} attempt {}/{} failed (status: {}), will retry if attempts remain", session_id, &wallet_addr[..10], attempt, max_attempts, status);
                }
            }
        });

        send_handles.push(handle);
    }

    // Wait for all wallet sends to complete
    for h in send_handles {
        let _ = h.await;
    }

    // Mark session complete (bind naive_utc for TIMESTAMP column)
    sqlx::query(
        "UPDATE mint_sessions SET status = 'completed', fired_at = $1 WHERE id = $2"
    )
    .bind(fire_timestamp.naive_utc())
    .bind(session_id)
    .execute(&db)
    .await?;

    info!("Session {}: completed.", session_id);
    Ok(())
}
