// src/auth/siwe.rs — OpenSea SIWE authentication
//
// CRITICAL REQUIREMENTS (deviation causes silent 400/rejection):
// - Target: gql.opensea.io/graphql (NOT api.opensea.io)
// - SIWE message: "wants you to sign in with your account:" (NOT "Ethereum account")
// - URI: "https://opensea.io/" — trailing slash MANDATORY
// - Address passed as LOWERCASE (no EIP-55 checksum)
// - POST structured JSON fields (not raw signed message string)

use std::collections::HashMap;
use std::sync::Mutex;
use chrono::{DateTime, Utc, Duration};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use secp256k1::{Secp256k1, SecretKey, Message};
use sha3::{Digest, Keccak256};
use tracing::{info, warn, error};

use crate::opensea::OpenSeaConfig;

/// Stored session for one wallet
#[derive(Debug, Clone)]
pub struct WalletSession {
    pub wallet_address: String,
    pub cookies: String,           // raw Cookie header value
    pub access_token: String,
    pub refresh_token: String,
    pub expires_at: DateTime<Utc>,
}

/// Thread-safe session store: wallet_address → session
pub struct SessionStore {
    sessions: Mutex<HashMap<String, WalletSession>>,
}

impl SessionStore {
    pub fn new() -> Self {
        Self { sessions: Mutex::new(HashMap::new()) }
    }

    pub fn get(&self, wallet: &str) -> Option<WalletSession> {
        self.sessions.lock().unwrap().get(wallet).cloned()
    }

    pub fn set(&self, session: WalletSession) {
        self.sessions.lock().unwrap().insert(session.wallet_address.clone(), session);
    }

    pub fn needs_refresh(&self, wallet: &str, threshold_secs: i64) -> bool {
        match self.sessions.lock().unwrap().get(wallet) {
            None => true,
            Some(s) => {
                let remaining = s.expires_at - Utc::now();
                remaining < Duration::seconds(threshold_secs)
            }
        }
    }
}

// ── SIWE nonce fetch ──────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct NonceResponse {
    data: NonceData,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct NonceData {
    auth: AuthData,
}

#[derive(Deserialize)]
struct AuthData {
    nonce: String,
}

async fn fetch_nonce(client: &Client, cfg: &OpenSeaConfig, headers: &HashMap<String, String>) -> anyhow::Result<String> {
    let query = r#"{
        "query": "query GetNonce { auth { nonce } }",
        "operationName": "GetNonce"
    }"#;

    let mut req = client.post(&cfg.graphql_url).body(query);
    for (k, v) in headers {
        req = req.header(k.as_str(), v.as_str());
    }
    let resp: NonceResponse = req.send().await?.json().await?;
    Ok(resp.data.auth.nonce)
}

// ── SIWE message construction ─────────────────────────────────────────────────

fn build_siwe_message(
    address: &str,   // lowercase, no checksum
    nonce: &str,
    chain_id: u64,
    issued_at: &str,
) -> String {
    // EXACT format required by OpenSea — any deviation causes silent failure
    format!(
        "opensea.io wants you to sign in with your account:\n\
         {}\n\
         \n\
         Click to sign in and accept the OpenSea Terms of Service:\n\
         https://opensea.io/tos\n\
         \n\
         URI: https://opensea.io/\n\
         Version: 1\n\
         Chain ID: {}\n\
         Nonce: {}\n\
         Issued At: {}",
        address, chain_id, nonce, issued_at
    )
}

// ── Ethereum personal_sign ────────────────────────────────────────────────────

fn eth_personal_sign(message: &str, private_key_bytes: &[u8; 32]) -> String {
    let secp = Secp256k1::new();
    let secret_key = SecretKey::from_slice(private_key_bytes)
        .expect("Invalid private key");

    // Ethereum personal_sign prefix
    let prefixed = format!("\x19Ethereum Signed Message:\n{}{}", message.len(), message);

    // Keccak256 of prefixed message
    let mut hasher = Keccak256::new();
    hasher.update(prefixed.as_bytes());
    let hash = hasher.finalize();

    let msg = Message::from_digest_slice(&hash).expect("Invalid hash length");
    let (recovery_id, sig) = secp.sign_ecdsa_recoverable(&msg, &secret_key).serialize_compact();

    // Build 65-byte signature: r (32) + s (32) + v (1)
    let v = recovery_id.to_i32() as u8 + 27;
    let mut sig_bytes = [0u8; 65];
    sig_bytes[..64].copy_from_slice(&sig);
    sig_bytes[64] = v;

    format!("0x{}", hex::encode(sig_bytes))
}

// ── Login POST ────────────────────────────────────────────────────────────────

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct LoginPayload {
    domain: String,
    address: String,
    statement: String,
    uri: String,
    version: String,
    chain_id: u64,
    nonce: String,
    issued_at: String,
    signature: String,
    chain_arch: String,
    connector_id: String,
}

#[derive(Deserialize, Debug)]
#[serde(rename_all = "camelCase")]
struct LoginResponse {
    data: Option<LoginData>,
    errors: Option<Vec<serde_json::Value>>,
}

#[derive(Deserialize, Debug)]
#[serde(rename_all = "camelCase")]
struct LoginData {
    login_with_nonce: Option<LoginResult>,
}

#[derive(Deserialize, Debug)]
#[serde(rename_all = "camelCase")]
struct LoginResult {
    success: bool,
}

// ── Public: authenticate_wallet ───────────────────────────────────────────────

/// Performs full SIWE login for a wallet. Returns a WalletSession.
/// private_key_bytes is zeroed by the caller after this returns.
pub async fn authenticate_wallet(
    client: &Client,
    cfg: &OpenSeaConfig,
    wallet_address: &str,  // must be lowercase
    private_key_bytes: &[u8; 32],
) -> anyhow::Result<WalletSession> {
    let headers = build_headers(cfg, None);

    // 1. Fetch nonce
    let nonce = fetch_nonce(client, cfg, &headers).await?;
    info!("SIWE: got nonce for {}", &wallet_address[..10]);

    // 2. Build SIWE message
    let issued_at = Utc::now().format("%Y-%m-%dT%H:%M:%S%.3fZ").to_string();
    let message = build_siwe_message(wallet_address, &nonce, 1, &issued_at);

    // 3. Sign
    let signature = eth_personal_sign(&message, private_key_bytes);

    // 4. Parse statement from message (third line of the message)
    let statement = "Click to sign in and accept the OpenSea Terms of Service:\nhttps://opensea.io/tos";

    // 5. Build structured JSON payload (NOT raw message string)
    let payload = LoginPayload {
        domain: "opensea.io".into(),
        address: wallet_address.to_string(),
        statement: statement.to_string(),
        uri: "https://opensea.io/".into(),
        version: "1".into(),
        chain_id: 1,
        nonce: nonce.clone(),
        issued_at: issued_at.clone(),
        signature,
        chain_arch: "EVM".into(),
        connector_id: "injected".into(),
    };

    let gql_body = serde_json::json!({
        "query": "mutation LoginWithNonce($domain: String!, $address: String!, $statement: String!, $uri: String!, $version: String!, $chainId: Int!, $nonce: String!, $issuedAt: String!, $signature: String!, $chainArch: String!, $connectorId: String!) { loginWithNonce(domain: $domain, address: $address, statement: $statement, uri: $uri, version: $version, chainId: $chainId, nonce: $nonce, issuedAt: $issuedAt, signature: $signature, chainArch: $chainArch, connectorId: $connectorId) { success } }",
        "operationName": "LoginWithNonce",
        "variables": payload
    });

    // 6. POST to gql.opensea.io
    let mut req_builder = client.post(&cfg.graphql_url);
    for (k, v) in &headers {
        req_builder = req_builder.header(k.as_str(), v.as_str());
    }

    let response = req_builder
        .json(&gql_body)
        .send()
        .await?;

    // 7. Extract Set-Cookie headers
    let cookies = response.headers()
        .get_all("set-cookie")
        .iter()
        .filter_map(|v| v.to_str().ok())
        .collect::<Vec<_>>()
        .join("; ");

    // Extract access_token and refresh_token from cookie string
    let access_token = extract_cookie_value(&cookies, "access_token").unwrap_or_default();
    let refresh_token = extract_cookie_value(&cookies, "refresh_token").unwrap_or_default();

    let body: LoginResponse = response.json().await?;

    if let Some(errors) = &body.errors {
        if !errors.is_empty() {
            error!("SIWE login errors: {:?}", errors);
            anyhow::bail!("OpenSea login failed: {:?}", errors);
        }
    }

    let success = body.data
        .as_ref()
        .and_then(|d| d.login_with_nonce.as_ref())
        .map(|r| r.success)
        .unwrap_or(false);

    if !success {
        anyhow::bail!("OpenSea SIWE login returned success=false");
    }

    info!("SIWE: authenticated wallet {}", &wallet_address[..10]);

    Ok(WalletSession {
        wallet_address: wallet_address.to_string(),
        cookies,
        access_token,
        refresh_token,
        // TTL ~3.5 days from now
        expires_at: Utc::now() + Duration::seconds(302400),
    })
}

// ── Header builder ────────────────────────────────────────────────────────────

pub fn build_headers(_cfg: &OpenSeaConfig, cookies: Option<&str>) -> HashMap<String, String> {
    let mut h = HashMap::new();
    h.insert("x-app-id".into(), "os2-web".into());
    h.insert("content-type".into(), "application/json".into());
    h.insert("origin".into(), "https://opensea.io".into());
    h.insert("referer".into(), "https://opensea.io/".into());
    h.insert(
        "user-agent".into(),
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36".into(),
    );
    h.insert("accept".into(), "*/*".into());
    h.insert("accept-language".into(), "en-US,en;q=0.9".into());
    if let Some(c) = cookies {
        h.insert("cookie".into(), c.to_string());
    }
    h
}

// ── Helpers ───────────────────────────────────────────────────────────────────

fn extract_cookie_value(cookie_str: &str, name: &str) -> Option<String> {
    for part in cookie_str.split(';') {
        let part = part.trim();
        if let Some(val) = part.strip_prefix(&format!("{}=", name)) {
            return Some(val.split(';').next().unwrap_or(val).to_string());
        }
    }
    None
}
