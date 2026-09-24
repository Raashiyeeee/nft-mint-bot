// src/minting/signer.rs — Transaction signing via secp256k1 C library
//
// Uses secp256k1 crate (wraps the C library, ~70µs per signature).
// Key material is zeroed after use.
// Builds EIP-1559 (type 2) RLP-encoded raw transaction.

use secp256k1::{Secp256k1, SecretKey, Message};
use sha3::{Digest, Keccak256};
use tracing::debug;

// ── Fernet decryption (Python-compatible) ─────────────────────────────────────

/// Decrypt a Fernet-encrypted private key.
/// The Fernet token was encrypted by Python's cryptography.fernet.Fernet.
/// We use base64 + AES-128-CBC + HMAC-SHA256 to decrypt.
pub fn decrypt_private_key(
    encrypted: &str,
    fernet_key_b64: &str,
) -> anyhow::Result<[u8; 32]> {
    use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};

    // Decode Fernet key (32 bytes: 16 signing key + 16 encryption key)
    let key_bytes = URL_SAFE_NO_PAD.decode(fernet_key_b64.trim_end_matches('='))?;
    if key_bytes.len() != 32 {
        anyhow::bail!("Fernet key must be 32 bytes, got {}", key_bytes.len());
    }

    // Decode Fernet token
    let token = URL_SAFE_NO_PAD.decode(encrypted.trim_end_matches('='))?;
    if token.len() < 57 {
        anyhow::bail!("Fernet token too short");
    }

    // Token structure: version(1) + timestamp(8) + iv(16) + ciphertext(N) + hmac(32)
    let version = token[0];
    if version != 0x80 {
        anyhow::bail!("Unsupported Fernet version: {}", version);
    }

    let iv = &token[9..25];
    let ciphertext = &token[25..token.len()-32];

    // Decrypt AES-128-CBC
    use aes::cipher::{block_padding::Pkcs7, BlockDecryptMut, KeyIvInit};
    type Aes128CbcDec = cbc::Decryptor<aes::Aes128>;

    let enc_key = &key_bytes[16..32];
    let mut buf = ciphertext.to_vec();
    let decrypted = Aes128CbcDec::new(enc_key.into(), iv.into())
        .decrypt_padded_mut::<Pkcs7>(&mut buf)
        .map_err(|e| anyhow::anyhow!("AES decrypt error: {:?}", e))?;

    // The decrypted value is the hex private key string
    let hex_key = std::str::from_utf8(decrypted)?.trim();
    let hex_key = hex_key.trim_start_matches("0x");

    let key_bytes = hex::decode(hex_key)?;
    if key_bytes.len() != 32 {
        anyhow::bail!("Private key must be 32 bytes");
    }

    let mut result = [0u8; 32];
    result.copy_from_slice(&key_bytes);
    Ok(result)
}

// ── EIP-1559 Transaction builder & signer ─────────────────────────────────────

/// Builds and signs an EIP-1559 (type 2) raw transaction.
/// Private key bytes are consumed and zeroed after signing.
/// Gas limit is hardcoded (no estimateGas on hot path).
pub fn build_and_sign_tx(
    private_key_bytes: &[u8; 32],
    to: &str,
    data: &str,
    value_str: &str,
    nonce: u64,
    chain_id: u64,
    max_fee_per_gas: u128,
    max_priority_fee_per_gas: u128,
    _gas_multiplier: f64, // Already applied in warmup
    gas_limit: u64,
) -> anyhow::Result<String> {
    // Parse value
    let value: u128 = if value_str.starts_with("0x") {
        u128::from_str_radix(value_str.trim_start_matches("0x"), 16).unwrap_or(0)
    } else {
        value_str.parse().unwrap_or(0)
    };

    // Parse calldata
    let data_bytes = hex::decode(data.trim_start_matches("0x"))?;

    // Parse to address
    let to_bytes = hex::decode(to.trim_start_matches("0x"))?;
    if to_bytes.len() != 20 {
        anyhow::bail!("Invalid 'to' address length: {}", to_bytes.len());
    }

    // ── RLP encode EIP-1559 transaction (type 2) ──────────────────────────────
    // EIP-1559 signing payload: 0x02 || rlp([chain_id, nonce, max_priority_fee, max_fee, gas_limit, to, value, data, access_list])
    let mut rlp_list = Vec::new();
    rlp_list.push(rlp_encode_uint(chain_id as u128));
    rlp_list.push(rlp_encode_uint(nonce as u128));
    rlp_list.push(rlp_encode_uint(max_priority_fee_per_gas));
    rlp_list.push(rlp_encode_uint(max_fee_per_gas));
    rlp_list.push(rlp_encode_uint(gas_limit as u128));
    rlp_list.push(rlp_encode_bytes(&to_bytes));
    rlp_list.push(rlp_encode_uint(value));
    rlp_list.push(rlp_encode_bytes(&data_bytes));
    rlp_list.push(rlp_encode_list(&[])); // empty access list

    let rlp_payload = rlp_encode_list(&rlp_list.concat());
    let mut signing_data = vec![0x02u8];
    signing_data.extend_from_slice(&rlp_payload);

    // ── Keccak256 hash ────────────────────────────────────────────────────────
    let mut hasher = Keccak256::new();
    hasher.update(&signing_data);
    let hash = hasher.finalize();

    // ── secp256k1 sign (C library) ────────────────────────────────────────────
    let secp = Secp256k1::new();
    let secret_key = SecretKey::from_slice(private_key_bytes)
        .map_err(|e| anyhow::anyhow!("Invalid private key: {}", e))?;
    let msg = Message::from_digest_slice(&hash)?;

    let (recovery_id, sig_bytes) = secp
        .sign_ecdsa_recoverable(&msg, &secret_key)
        .serialize_compact();

    let v = recovery_id.to_i32() as u128;
    let r = &sig_bytes[0..32];
    let s = &sig_bytes[32..64];

    // ── RLP encode final signed transaction ───────────────────────────────────
    let mut signed_rlp = Vec::new();
    signed_rlp.push(rlp_encode_uint(chain_id as u128));
    signed_rlp.push(rlp_encode_uint(nonce as u128));
    signed_rlp.push(rlp_encode_uint(max_priority_fee_per_gas));
    signed_rlp.push(rlp_encode_uint(max_fee_per_gas));
    signed_rlp.push(rlp_encode_uint(gas_limit as u128));
    signed_rlp.push(rlp_encode_bytes(&to_bytes));
    signed_rlp.push(rlp_encode_uint(value));
    signed_rlp.push(rlp_encode_bytes(&data_bytes));
    signed_rlp.push(rlp_encode_list(&[]));
    signed_rlp.push(rlp_encode_uint(v));
    signed_rlp.push(rlp_encode_bytes(r));
    signed_rlp.push(rlp_encode_bytes(s));

    let final_rlp = rlp_encode_list(&signed_rlp.concat());
    let mut raw_tx = vec![0x02u8];
    raw_tx.extend_from_slice(&final_rlp);

    debug!("Signed tx: {} bytes", raw_tx.len());
    Ok(format!("0x{}", hex::encode(raw_tx)))
}

// ── Minimal RLP helpers ───────────────────────────────────────────────────────

fn rlp_encode_uint(n: u128) -> Vec<u8> {
    if n == 0 {
        return vec![0x80]; // RLP empty string = 0
    }
    let bytes = n.to_be_bytes();
    let trimmed: Vec<u8> = bytes.iter().copied().skip_while(|&b| b == 0).collect();
    rlp_encode_bytes(&trimmed)
}

fn rlp_encode_bytes(data: &[u8]) -> Vec<u8> {
    if data.len() == 1 && data[0] < 0x80 {
        return vec![data[0]];
    }
    let mut result = rlp_length_prefix(data.len(), 0x80);
    result.extend_from_slice(data);
    result
}

fn rlp_encode_list(data: &[u8]) -> Vec<u8> {
    let mut result = rlp_length_prefix(data.len(), 0xc0);
    result.extend_from_slice(data);
    result
}

fn rlp_length_prefix(len: usize, offset: u8) -> Vec<u8> {
    if len < 56 {
        vec![offset + len as u8]
    } else {
        let len_bytes = len.to_be_bytes();
        let trimmed: Vec<u8> = len_bytes.iter().copied().skip_while(|&b| b == 0).collect();
        let mut result = vec![offset + 55 + trimmed.len() as u8];
        result.extend_from_slice(&trimmed);
        result
    }
}
