# ===== builder =====
FROM rust:latest AS build
WORKDIR /app

# Instala pkg-config/openssl para sqlx (si usas rustls da igual tenerlos)
RUN apt-get update && apt-get install -y --no-install-recommends \
    pkg-config libssl-dev ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Copia TODO el crate del servidor (sin trucos de dummy main)
COPY server/ /app/server/

# Compila en release (binario "server")
WORKDIR /app/server
RUN cargo build --release --bin server && ls -lh target/release/server

# ===== runtime =====
FROM debian:bookworm-slim AS runtime
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates wget \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app

# Copia el binario REAL
COPY --from=build /app/server/target/release/server /usr/local/bin/server

# Copia el frontend
COPY frontend /app/static

EXPOSE 8080
CMD ["server"]
