// src/auth/mod.rs
pub mod siwe;
pub use siwe::{authenticate_wallet, build_headers, SessionStore, WalletSession};
