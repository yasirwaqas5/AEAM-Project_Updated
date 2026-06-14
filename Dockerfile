# ============================================================
# AEAM — Autonomous Enterprise AI Agent Mesh
# Production Dockerfile
# ============================================================

# ── Stage 1: base ────────────────────────────────────────────
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ── Stage 2: dependencies ────────────────────────────────────
FROM base AS builder

# System dependencies required for compiled packages
# (psycopg2-binary, prophet, scikit-learn, cryptography).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first so Docker layer cache is reused
# on subsequent builds when only application code changes.
COPY requirements.txt .

RUN pip install --upgrade pip && \
    for i in 1 2 3; do pip install --no-cache-dir -r requirements.txt && break || sleep 10; done

# ── Stage 3: production image ────────────────────────────────
FROM base AS production

# Install runtime system dependencies only (no build tools).
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder stage.
COPY --from=builder /usr/local/lib/python3.11 /usr/local/lib/python3.11
COPY --from=builder /usr/local/bin /usr/local/bin

# Create non-root user for security.
RUN groupadd --gid 1001 aeam && \
    useradd --uid 1001 --gid aeam --shell /bin/bash --create-home aeam

WORKDIR /app

# Copy full project source.
COPY --chown=aeam:aeam . .

# Switch to non-root user.
USER aeam

# Expose application port.
EXPOSE 8080

# Health check — requires curl (installed above).
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Production entrypoint — uvicorn, no reload, no debug.
CMD ["uvicorn", "aeam.main:app", "--host", "0.0.0.0", "--port", "8080"]