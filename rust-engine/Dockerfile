FROM rust:latest AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    pkg-config libssl-dev libsecp256k1-dev build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Cache dependencies layer
COPY Cargo.toml Cargo.lock* ./
RUN mkdir src && echo "fn main() {}" > src/main.rs
RUN cargo build --release 2>/dev/null || true
RUN rm -rf src

# Build actual source
COPY src ./src
COPY build.rs ./
RUN touch src/main.rs && cargo build --release

# ── Runtime image ────────────────────────────────────────────────────────────
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libssl3 libsecp256k1-1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app/target/release/nft-mint-engine ./
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod 700 /app/docker-entrypoint.sh
COPY --from=builder /usr/lib/x86_64-linux-gnu/libsecp256k1.so* /usr/lib/x86_64-linux-gnu/

EXPOSE 7878
CMD ["./nft-mint-engine"]
