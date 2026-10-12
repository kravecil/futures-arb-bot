# ---- Сборка окружения ----
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project

COPY src/ ./src/
RUN uv sync --locked --no-dev

# ---- Рантайм ----
FROM python:3.14-slim-bookworm
WORKDIR /app
COPY --from=build /app /app
COPY config/ /app/config/
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

ENTRYPOINT ["futures-arb-bot"]
