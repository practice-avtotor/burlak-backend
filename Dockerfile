FROM python:3.12-slim-bookworm AS builder

RUN pip install --no-cache-dir uv

WORKDIR /app

# Copy burlak_parser first so the path dependency can be resolved
COPY burlak_parser/ ./burlak_parser/
COPY burlak-backend/pyproject.toml burlak-backend/uv.lock ./burlak-backend/

WORKDIR /app/burlak-backend
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libreoffice \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/burlak-backend

# Copy burlak_parser (needed at runtime for imports)
COPY --from=builder /app/burlak_parser /app/burlak_parser

COPY --from=builder /app/burlak-backend/.venv /app/burlak-backend/.venv
COPY burlak-backend/app ./app
COPY burlak-backend/pyproject.toml burlak-backend/uv.lock ./

# Activate venv by adding to PATH
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/burlak-backend/.venv/bin:$PATH" \
    REDIS_URL=redis://redis:6379/0 \
    DB_URL=/data/jobs.db \
    STORAGE_PATH=/data

RUN mkdir -p /data && \
    useradd --create-home --shell /bin/bash appuser && \
    chown -R appuser:appuser /data /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:8000/api/v1/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
