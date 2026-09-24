// build.rs — Links libsecp256k1 system library for C FFI speed
fn main() {
    // The secp256k1 Rust crate bundles and compiles the C library itself.
    // No additional linking needed here — the crate's build script handles it.
    // This file exists as a hook for future custom link flags (e.g., static linking).
    println!("cargo:rerun-if-changed=build.rs");
}
