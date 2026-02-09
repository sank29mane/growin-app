# Stage 1: Build - Compile Rust and Python deps
FROM python:3.11-slim as builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y 
    curl 
    build-essential 
    pkg-config 
    libssl-dev 
    && rm -rf /var/lib/apt/lists/*

# Install Rust
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"

# Install uv for fast dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uv/bin/
ENV PATH="/uv/bin:${PATH}"

# Copy backend source
COPY . .

# Compile Rust Core (if source exists)
RUN if [ -d "growin_core_src" ]; then 
    cd growin_core_src && cargo build --release; 
    fi

# Build Python environment
RUN uv venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
RUN uv pip install -r requirements.txt

# Stage 2: Runtime - Slim Production Image
FROM python:3.11-slim

WORKDIR /app

# Copy virtualenv and compiled Rust libs from builder
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /build /app

# Set environment
ENV PATH="/opt/venv/bin:${PATH}"
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Expose FastAPI port
EXPOSE 8002

# Health check
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 
  CMD curl -f http://localhost:8002/health || exit 1

# Start server
CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8002", "--loop", "uvloop"]
