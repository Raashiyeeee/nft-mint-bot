// src/ipc/server.rs — JSON-RPC server on 127.0.0.1:7878
// Exposes methods for the Python Telegram bot to call.

use std::sync::Arc;
use actix_web::{web, App, HttpServer, HttpResponse};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sqlx::PgPool;
use tracing::{info, warn, error};

use crate::chains::ChainRegistry;
use crate::ipc::BotConfig;
use crate::opensea::scheduler::SessionManager;

// ── JSON-RPC request/response types ──────────────────────────────────────────

#[derive(Deserialize)]
struct RpcRequest {
    jsonrpc: String,
    method: String,
    params: Option<Value>,
    id: Option<Value>,
}

#[derive(Serialize)]
struct RpcResponse {
    jsonrpc: String,
    result: Option<Value>,
    error: Option<RpcError>,
    id: Option<Value>,
}

#[derive(Serialize)]
struct RpcError {
    code: i32,
    message: String,
}

impl RpcResponse {
    fn ok(id: Option<Value>, result: Value) -> Self {
        Self { jsonrpc: "2.0".into(), result: Some(result), error: None, id }
    }
    fn err(id: Option<Value>, code: i32, msg: &str) -> Self {
        Self { jsonrpc: "2.0".into(), result: None, error: Some(RpcError { code, message: msg.into() }), id }
    }
}

// ── App state ─────────────────────────────────────────────────────────────────

struct AppState {
    session_manager: Arc<SessionManager>,
    chain_registry: Arc<ChainRegistry>,
    db: PgPool,
    bot_cfg: BotConfig,
}

// ── Handler ───────────────────────────────────────────────────────────────────

async fn handle_rpc(
    state: web::Data<AppState>,
    body: web::Json<RpcRequest>,
) -> HttpResponse {
    let id = body.id.clone();
    let params = body.params.clone().unwrap_or(Value::Null);

    let result = match body.method.as_str() {
        // ── schedule_mint ─────────────────────────────────────────────────────
        "schedule_mint" => {
            // Called by Python bot after user completes /mint flow
            // params: { session_id, telegram_id, drop_slug, nft_contract, chain, ... }
            info!("RPC: schedule_mint called");
            Value::Bool(true) // Scheduler picks it up from DB automatically
        }

        // ── cancel_mint ───────────────────────────────────────────────────────
        "cancel_mint" => {
            let session_id = params["session_id"].as_i64().unwrap_or(0) as i32;
            let telegram_id = params["telegram_id"].as_i64().unwrap_or(0);

            match sqlx::query(
                "UPDATE mint_sessions SET status = 'cancelled' WHERE id = $1 AND telegram_id = $2 AND status = 'pending'"
            )
            .bind(session_id)
            .bind(telegram_id)
            .execute(&state.db)
            .await {
                Ok(r) if r.rows_affected() > 0 => {
                    info!("RPC: cancelled session {}", session_id);
                    Value::Bool(true)
                }
                Ok(_) => {
                    warn!("RPC: cancel_mint — session {} not found or not cancellable", session_id);
                    Value::Bool(false)
                }
                Err(e) => {
                    error!("RPC: cancel_mint DB error: {}", e);
                    return HttpResponse::Ok().json(RpcResponse::err(id, -32000, &e.to_string()));
                }
            }
        }

        // ── get_drop_info ─────────────────────────────────────────────────────
        "get_drop_info" => {
            let slug = params["slug"].as_str().unwrap_or("").to_string();
            let cookies = params["cookies"].as_str().unwrap_or("").to_string();

            if slug.is_empty() {
                return HttpResponse::Ok().json(RpcResponse::err(id, -32602, "Missing slug"));
            }

            match crate::opensea::graphql::fetch_drop_by_slug(
                &state.session_manager.http_client,
                &state.session_manager.opensea_cfg,
                &cookies,
                &slug,
            ).await {
                Ok(drop) => serde_json::json!({
                    "slug": drop.slug,
                    "name": drop.name,
                    "contract": drop.contract_address,
                    "chain": drop.chain,
                    "price": drop.price_eth,
                    "mint_start": drop.mint_start_time.map(|t| t.to_rfc3339()),
                    "stage_label": drop.stage_label,
                }),
                Err(e) => {
                    error!("RPC: get_drop_info error: {}", e);
                    return HttpResponse::Ok().json(RpcResponse::err(id, -32000, &e.to_string()));
                }
            }
        }

        // ── status ────────────────────────────────────────────────────────────
        "status" => {
            let chain_count = state.chain_registry.count();
            let pending_sessions: i64 = sqlx::query_scalar::<_, i64>(
                "SELECT COUNT(*) FROM mint_sessions WHERE status IN ('pending', 'active')"
            )
            .fetch_one(&state.db)
            .await
            .unwrap_or(0);

            serde_json::json!({
                "engine": "running",
                "chain_count": chain_count,
                "pending_sessions": pending_sessions,
                "uptime_secs": 0, // TODO: track start time
            })
        }

        // ── ping_pipeline ─────────────────────────────────────────────────────
        "ping_pipeline" => {
            // Smoke test: verify RPC connectivity for each chain
            let client = state.session_manager.http_client.clone();
            let mut chain_status = serde_json::Map::new();

            for (key, cfg) in state.chain_registry.all_chains() {
                let rpc_url = state.chain_registry.current_rpc(key).await
                    .unwrap_or_else(|| "none".to_string());

                let ok = state.chain_registry.rpc_call(
                    &client, key, "eth_blockNumber", Value::Array(vec![])
                ).await.is_ok();

                chain_status.insert(key.clone(), serde_json::json!({
                    "rpc": rpc_url,
                    "ok": ok,
                }));
            }

            Value::Object(chain_status)
        }

        // ── get_session_results ───────────────────────────────────────────────
        "get_session_results" => {
            let session_id = params["session_id"].as_i64().unwrap_or(0) as i32;
            use sqlx::Row;
            match sqlx::query(
                "SELECT wallet_address, tx_hash, token_id, gas_used, latency_ms, status, error_msg
                 FROM mint_results WHERE session_id = $1 ORDER BY minted_at"
            )
            .bind(session_id)
            .fetch_all(&state.db)
            .await {
                Ok(rows) => {
                    let results: Vec<Value> = rows.iter().map(|r| serde_json::json!({
                        "wallet": r.get::<String, _>("wallet_address"),
                        "tx_hash": r.get::<Option<String>, _>("tx_hash"),
                        "token_id": r.get::<Option<String>, _>("token_id"),
                        "gas_used": r.get::<Option<i64>, _>("gas_used"),
                        "latency_ms": r.get::<Option<i32>, _>("latency_ms"),
                        "status": r.get::<String, _>("status"),
                        "error_msg": r.get::<Option<String>, _>("error_msg"),
                    })).collect();
                    Value::Array(results)
                }
                Err(e) => {
                    return HttpResponse::Ok().json(RpcResponse::err(id, -32000, &e.to_string()));
                }
            }
        }

        // ── get_wallet_balance ────────────────────────────────────────────────
        "get_wallet_balance" => {
            let wallet_address = match params["wallet_address"].as_str() {
                Some(a) if !a.is_empty() => a.to_string(),
                _ => return HttpResponse::Ok().json(RpcResponse::err(id, -32602, "Missing wallet_address")),
            };
            let chain = match params["chain"].as_str() {
                Some(c) if !c.is_empty() => c.to_string(),
                _ => return HttpResponse::Ok().json(RpcResponse::err(id, -32602, "Missing chain")),
            };

            let client = state.session_manager.http_client.clone();
            match state.chain_registry.rpc_call(
                &client,
                &chain,
                "eth_getBalance",
                serde_json::json!([wallet_address, "latest"]),
            ).await {
                Ok(result) => {
                    // Convert hex balance (wei) to ETH float
                    let hex = result.as_str().unwrap_or("0x0");
                    let wei = u128::from_str_radix(hex.trim_start_matches("0x"), 16).unwrap_or(0);
                    let eth = wei as f64 / 1e18;
                    serde_json::json!(eth)
                }
                Err(e) => {
                    warn!("RPC: get_wallet_balance error for {} on {}: {}", wallet_address, chain, e);
                    return HttpResponse::Ok().json(RpcResponse::err(id, -32000, &e.to_string()));
                }
            }
        }

        method => {
            warn!("RPC: unknown method '{}'", method);
            return HttpResponse::Ok().json(RpcResponse::err(id, -32601, &format!("Method not found: {}", method)));
        }
    };

    HttpResponse::Ok().json(RpcResponse::ok(id, result))
}

// ── Server startup ────────────────────────────────────────────────────────────

pub async fn run_server(
    bind_addr: String,
    session_manager: Arc<SessionManager>,
    chain_registry: Arc<ChainRegistry>,
    db: PgPool,
    bot_cfg: BotConfig,
) -> std::io::Result<()> {
    info!("JSON-RPC server starting on {}", bind_addr);

    let state = web::Data::new(AppState {
        session_manager,
        chain_registry,
        db,
        bot_cfg,
    });

    let server = HttpServer::new(move || {
        App::new()
            .app_data(state.clone())
            .app_data(web::JsonConfig::default().limit(1_048_576)) // 1MB max body
            .route("/", web::post().to(handle_rpc))
            .route("/health", web::get().to(|| async { HttpResponse::Ok().body("ok") }))
    })
    .bind(&bind_addr)?;

    server.run().await
}
