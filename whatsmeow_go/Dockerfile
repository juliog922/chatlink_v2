# -------- Etapa de build --------
    FROM golang:1.23-bookworm AS builder
    WORKDIR /src
    ENV CGO_ENABLED=0 GOOS=linux
    
    # Dependencias
    COPY go.mod go.sum ./
    RUN go mod download
    
    # Copiamos el código
    COPY . .
    
    # 🔧 Asegura que exista el driver Postgres y limpia deps
    # (si ya tienes el import en main.go: _ "github.com/lib/pq")
    RUN go get github.com/lib/pq@latest && go mod tidy
    
    # Compila binario estático
    RUN go build -trimpath -ldflags="-s -w -buildid=" -o /out/wa-grpc ./
    
    # -------- Etapa final --------
    FROM gcr.io/distroless/static-debian12:nonroot
    WORKDIR /app
    COPY --from=builder /out/wa-grpc /app/wa-grpc
    EXPOSE 50051
    ENV WHATSMEOW_HOST=0.0.0.0 WHATSMEOW_PORT=50051
    USER nonroot:nonroot
    ENTRYPOINT ["/app/wa-grpc"]
    