FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.2 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN groupadd --system newsroom \
    && useradd --system --gid newsroom --no-create-home newsroom \
    && install -d -o newsroom -g newsroom -m 0750 /data \
    && install -o newsroom -g newsroom -m 0600 /dev/null /data/.keep

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini ./
COPY migrations ./migrations
COPY evals/gemini-flash-lite.json ./evals/gemini-flash-lite.json

USER newsroom

CMD ["telegram-ai-newsroom", "--help"]
