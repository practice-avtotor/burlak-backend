FROM python:3.12-slim-bookworm AS builder
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY ai_analyzer/pyproject.ml.toml /app/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-install-project --no-dev

FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY ai_analyzer /app/ai_analyzer
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1
CMD ["uvicorn", "ai_analyzer.main:app", "--host", "0.0.0.0", "--port", "8000"]