FROM python:3.12-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
WORKDIR /app
COPY ai_analyzer/pyproject.ml.toml /app/pyproject.toml
RUN uv sync --frozen --no-install-project --no-dev

FROM python:3.12-slim-bookworm
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY ai_analyzer /app/ai_analyzer
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "ai_analyzer.main:app", "--host", "0.0.0.0", "--port", "8000"]