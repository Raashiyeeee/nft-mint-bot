// src/opensea/scheduler.rs — Concurrent multi-session scheduler
//
// Design goals:
//  • Many pending sessions (different wallets / CAs / phases) fire independently
//  • Same wallet in N sessions at once → NonceAllocator serializes nonces
//  • Millisecond-precision wake: sleep until T-2ms then micro-spin
//  • Atomic DB claim prevents double-fire under concurrent pollers
//  • Drift checks never block the fire hot-path

use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use chrono::Utc;
use sqlx::PgPool;
use tokio::sync::RwLock;
use tracing::{info, warn, error};
use reqwest::Client;

use crate::auth::{SessionStore, authenticate_wallet, WalletSession};
use crate::chains::ChainRegistry;
use crate::ipc::BotConfig;
use crate::minting;
use crate::minting::nonce::NonceAllocator;
use crate::opensea::{OpenSeaConfig, graphql};

// ── Session manager ───────────────────────────────────────────────────────────

pub struct SessionManager {
    pub http_client: Client,
    pub opensea_cfg: OpenSeaConfig,
    pub db_pool: PgPool,
    pub fernet_key: String,
    pub session_store: SessionStore,
    pub notify_url: String,
    pub nonce_allocator: NonceAllocator,
}

impl SessionManager {
    pub fn new(
        http_client: Client,
        opensea_cfg: OpenSeaConfig,
        db_pool: PgPool,
        fernet_key: String,
    ) -> Self {
        let notify_url = std::env::var("NOTIFICATION_URL")
            .unwrap_or_else(|_| {
                std::env::var("NOTIFICATION_PORT")
                    .map(|p| format!("http://127.0.0.1:{}/notify", p))
                    .unwrap_or_else(|_| "http://127.0.0.1:7879/notify".to_string())
            });

        Self {
            http_client,
            opensea_cfg,
            db_pool,
            fernet_key,
            session_store: SessionStore::new(),
            notify_url,
            nonce_allocator: NonceAllocator::new(),
        }
    }

    pub async fn ensure_session(
        &self,
        wallet_address: &str,
        private_key_bytes: &[u8; 32],
    ) -> anyhow::Result<WalletSession> {
        if !self.session_store.needs_refresh(wallet_address, 21600) {
            if let Some(session) = self.session_store.get(wallet_address) {
                return Ok(session);
            }
        }

        info!("Authenticating wallet {} with OpenSea...", &wallet_address[..10]);
        let session = authenticate_wallet(
            &self.http_client,
            &self.opensea_cfg,
            wallet_address,
            private_key_bytes,
        ).await?;
        self.session_store.set(session.clone());
        Ok(session)
    }

    pub async fn notify(&self, payload: serde_json::Value) {
        let _ = self.http_client
            .post(&self.notify_url)
            .json(&payload)
            .timeout(std::time::Duration::from_secs(5))
            .send()
            .await;
    }
}

// ── Precision sleep ───────────────────────────────────────────────────────────

/// Sleep until `target`, switching to micro-sleeps in the last ~5ms for low jitter.
pub async fn sleep_until_precise(target: chrono::DateTime<Utc>) {
    loop {
        let now = Utc::now();
        if now >= target {
            return;
        }
        let remaining = (target - now).to_std().unwrap_or_default();
        if remaining > std::time::Duration::from_millis(5) {
            tokio::time::sleep(remaining - std::time::Duration::from_millis(2)).await;
        } else if remaining > std::time::Duration::from_micros(200) {
            tokio::time::sleep(std::time::Duration::from_micros(100)).await;
        } else {
            // Final spin — yield so we don't starve other tasks
            tokio::task::yield_now().await;
        }
    }
}

// ── Claim + spawn helpers ─────────────────────────────────────────────────────

async fn try_claim_session(db: &PgPool, session_id: i32) -> bool {
    match sqlx::query_scalar::<_, i32>(
        "UPDATE mint_sessions SET status = 'active'
         WHERE id = $1 AND status = 'pending'
         RETURNING id"
    )
    .bind(session_id)
    .fetch_optional(db)
    .await
    {
        Ok(Some(_)) => true,
        Ok(None) => false,
        Err(e) => {
            error!("Session {} claim error: {}", session_id, e);
            false
        }
    }
}

fn spawn_mint(
    sm: Arc<SessionManager>,
    chain_registry: Arc<ChainRegistry>,
    db: PgPool,
    session_id: i32,
    telegram_id: i64,
    drop_slug: String,
    drop_name: String,
    nft_contract: String,
    chain: String,
    quantity_per_wallet: i32,
    gas_multiplier: f64,
    fire_time: chrono::DateTime<Utc>,
    inflight: Arc<RwLock<HashSet<i32>>>,
) {
    let fk = sm.fernet_key.clone();
    tokio::spawn(async move {
        let result = minting::run_mint_session(
            sm,
            chain_registry,
            db,
            session_id,
            telegram_id,
            drop_slug,
            drop_name,
            nft_contract,
            chain,
            quantity_per_wallet as u32,
            gas_multiplier,
            fire_time,
            fk,
        )
        .await;

        if let Err(e) = result {
            error!("Session {} failed: {}", session_id, e);
        }
        inflight.write().await.remove(&session_id);
    });
}

// ── Main scheduler loop ───────────────────────────────────────────────────────

pub async fn run_scheduler_loop(
    sm: Arc<SessionManager>,
    chain_registry: Arc<ChainRegistry>,
    db: PgPool,
    bot_cfg: BotConfig,
) {
    info!(
        "Scheduler loop started (base poll: {}s) — concurrent multi-session mode",
        bot_cfg.schedule_poll_interval_secs
    );

    // schedule-drift tracker: session_id → last known mint time
    let known_times: Arc<RwLock<HashMap<i32, chrono::DateTime<Utc>>>> =
        Arc::new(RwLock::new(HashMap::new()));

    // sessions with an active precision timer or mint task
    let inflight: Arc<RwLock<HashSet<i32>>> = Arc::new(RwLock::new(HashSet::new()));

    loop {
        let mut soonest_fire_ms: Option<i64> = None;

        let sessions: Vec<sqlx::postgres::PgRow> = match sqlx::query(
            "SELECT id, telegram_id, drop_slug, drop_name, nft_contract, chain,
                    quantity_per_wallet, CAST(gas_multiplier AS FLOAT8) as gas_multiplier, scheduled_at
             FROM mint_sessions
             WHERE status = 'pending'
             ORDER BY scheduled_at NULLS FIRST
             LIMIT 500"
        )
        .fetch_all(&db)
        .await
        {
            Ok(rows) => rows,
            Err(e) => {
                error!("Scheduler DB poll error: {}", e);
                tokio::time::sleep(std::time::Duration::from_secs(2)).await;
                continue;
            }
        };

        // Process sessions concurrently (drift + fire decisions) without serializing I/O
        let mut handles = Vec::with_capacity(sessions.len());

        for row in sessions {
            let sm = sm.clone();
            let cr = chain_registry.clone();
            let db = db.clone();
            let known_times = known_times.clone();
            let inflight = inflight.clone();

            handles.push(tokio::spawn(async move {
                use sqlx::Row;
                let session_id: i32 = row.get("id");
                let telegram_id: i64 = row.get("telegram_id");
                let drop_slug: String = row.get("drop_slug");
                let drop_name: String = row
                    .get::<Option<String>, _>("drop_name")
                    .unwrap_or_else(|| drop_slug.clone());
                let nft_contract: String = row.get("nft_contract");
                let chain: String = row.get("chain");
                let quantity_per_wallet: i32 = row.get("quantity_per_wallet");
                let gas_multiplier: f64 = row.try_get::<f64, _>("gas_multiplier").unwrap_or(1.5);
                let mut scheduled_at: Option<chrono::DateTime<Utc>> = row
                    .try_get::<Option<chrono::NaiveDateTime>, _>("scheduled_at")
                    .ok()
                    .flatten()
                    .map(|naive| naive.and_utc())
                    .or_else(|| {
                        row.try_get::<Option<chrono::DateTime<Utc>>, _>("scheduled_at")
                            .ok()
                            .flatten()
                    });

                // Skip if already claimed / timer running
                if inflight.read().await.contains(&session_id) {
                    return None;
                }

                let now = Utc::now();
                let fire_preview = scheduled_at.unwrap_or(now);
                let secs_until = (fire_preview - now).num_seconds();

                // ── Drift detection only when >30s away (never on hot path) ──
                if secs_until > 30 {
                    if !drop_slug.starts_with("0x") && !drop_slug.is_empty() {
                        if let Ok(drop_info) = graphql::fetch_drop_by_slug(
                            &sm.http_client,
                            &sm.opensea_cfg,
                            "",
                            &drop_slug,
                        )
                        .await
                        {
                            if let Some(new_time) = drop_info.mint_start_time {
                                let last = known_times.read().await.get(&session_id).copied();
                                if last.map(|t| (t - new_time).num_seconds().abs() > 5).unwrap_or(false)
                                {
                                    warn!("Session {}: schedule changed → {:?}", session_id, new_time);
                                    let _ = sqlx::query(
                                        "UPDATE mint_sessions SET scheduled_at = $1 WHERE id = $2 AND status = 'pending'",
                                    )
                                    .bind(new_time.naive_utc())
                                    .bind(session_id)
                                    .execute(&db)
                                    .await;

                                    sm.notify(serde_json::json!({
                                        "type": "schedule_changed",
                                        "telegram_id": telegram_id,
                                        "session_id": session_id,
                                        "drop_name": drop_name,
                                        "new_time": new_time.to_rfc3339(),
                                    }))
                                    .await;
                                    scheduled_at = Some(new_time);
                                }
                                known_times.write().await.insert(session_id, new_time);
                            }
                        }
                    } else if scheduled_at.is_none() {
                        if let Some(chain_cfg) = cr.get_config(&chain) {
                            let rpc_url = chain_cfg.rpc_urls.first().map(|s| s.as_str()).unwrap_or("");
                            if let Some(onchain_time) =
                                crate::minting::calldata::get_seadrop_start_time_onchain(
                                    &sm.http_client,
                                    rpc_url,
                                    &nft_contract,
                                )
                                .await
                            {
                                let last = known_times.read().await.get(&session_id).copied();
                                if last != Some(onchain_time) {
                                    info!(
                                        "Session {}: SeaDrop start auto-set {:?}",
                                        session_id, onchain_time
                                    );
                                    let _ = sqlx::query(
                                        "UPDATE mint_sessions SET scheduled_at = $1 WHERE id = $2 AND status = 'pending'",
                                    )
                                    .bind(onchain_time.naive_utc())
                                    .bind(session_id)
                                    .execute(&db)
                                    .await;
                                    scheduled_at = Some(onchain_time);
                                    known_times.write().await.insert(session_id, onchain_time);
                                }
                            }
                        }
                    }
                }

                let fire_time = scheduled_at.unwrap_or_else(Utc::now);
                let now = Utc::now();
                let ms_until = (fire_time - now).num_milliseconds();
                let seconds_until = ms_until / 1000;

                // Warmup notice ~T-5m (once-ish window)
                if seconds_until > 290 && seconds_until < 305 {
                    sm.notify(serde_json::json!({
                        "type": "warmup_notice",
                        "telegram_id": telegram_id,
                        "session_id": session_id,
                        "drop_name": drop_name,
                    }))
                    .await;
                }

                // ── Precision timer: within 45s of fire ──────────────────────
                if ms_until > 0 && ms_until <= 45_000 {
                    inflight.write().await.insert(session_id);

                    info!(
                        "Session {}: precision timer armed (T-{}.{:03}s)",
                        session_id,
                        ms_until / 1000,
                        (ms_until % 1000).abs()
                    );

                    let sm2 = sm.clone();
                    let cr2 = cr.clone();
                    let db2 = db.clone();
                    let inflight2 = inflight.clone();
                    let drop_slug2 = drop_slug.clone();
                    let drop_name2 = drop_name.clone();
                    let nft_contract2 = nft_contract.clone();
                    let chain2 = chain.clone();

                    tokio::spawn(async move {
                        // Wake at T-5s for warmup inside run_mint_session
                        let warmup_at = fire_time - chrono::Duration::seconds(5);
                        sleep_until_precise(warmup_at).await;

                        // Re-check pending (user may have cancelled)
                        let status: String = sqlx::query_scalar(
                            "SELECT status FROM mint_sessions WHERE id = $1",
                        )
                        .bind(session_id)
                        .fetch_one(&db2)
                        .await
                        .unwrap_or_default();

                        if status != "pending" {
                            info!(
                                "Session {}: timer abort (status={})",
                                session_id, status
                            );
                            inflight2.write().await.remove(&session_id);
                            return;
                        }

                        if !try_claim_session(&db2, session_id).await {
                            inflight2.write().await.remove(&session_id);
                            return;
                        }

                        info!(
                            "Session {}: CLAIMED — mint pipeline start (target={:?}, now={:?})",
                            session_id,
                            fire_time,
                            Utc::now()
                        );

                        spawn_mint(
                            sm2,
                            cr2,
                            db2,
                            session_id,
                            telegram_id,
                            drop_slug2,
                            drop_name2,
                            nft_contract2,
                            chain2,
                            quantity_per_wallet,
                            gas_multiplier,
                            fire_time,
                            inflight2,
                        );
                    });

                    return Some(ms_until.max(0));
                }

                // ── Overdue: fire immediately ────────────────────────────────
                if ms_until <= 0 && seconds_until >= -43200 {
                    inflight.write().await.insert(session_id);

                    if !try_claim_session(&db, session_id).await {
                        inflight.write().await.remove(&session_id);
                        return None;
                    }

                    info!(
                        "Session {}: overdue fire (ms_until={})",
                        session_id, ms_until
                    );

                    spawn_mint(
                        sm,
                        cr,
                        db,
                        session_id,
                        telegram_id,
                        drop_slug,
                        drop_name,
                        nft_contract,
                        chain,
                        quantity_per_wallet,
                        gas_multiplier,
                        fire_time,
                        inflight,
                    );
                    return None;
                }

                if seconds_until < -43200 {
                    warn!(
                        "Session {}: expired >12h, marking failed",
                        session_id
                    );
                    let _ = sqlx::query(
                        "UPDATE mint_sessions SET status = 'failed', error_msg = 'Expired before firing'
                         WHERE id = $1 AND status = 'pending'",
                    )
                    .bind(session_id)
                    .execute(&db)
                    .await;
                }

                if ms_until > 0 {
                    Some(ms_until)
                } else {
                    None
                }
            }));
        }

        for h in handles {
            if let Ok(Some(ms)) = h.await {
                soonest_fire_ms = Some(match soonest_fire_ms {
                    Some(cur) => cur.min(ms),
                    None => ms,
                });
            }
        }

        // ── Adaptive poll — sub-second when a mint is imminent ───────────────
        let sleep = match soonest_fire_ms {
            Some(ms) if ms <= 2_000 => std::time::Duration::from_millis(50),
            Some(ms) if ms <= 10_000 => std::time::Duration::from_millis(200),
            Some(ms) if ms <= 60_000 => std::time::Duration::from_millis(500),
            Some(ms) if ms <= 300_000 => std::time::Duration::from_secs(2),
            _ => std::time::Duration::from_secs(
                bot_cfg.schedule_poll_interval_secs.min(10) as u64,
            ),
        };
        tokio::time::sleep(sleep).await;
    }
}
