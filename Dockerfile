# Build stage: resolve the locked dependency set into a self-contained venv.
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# --no-editable installs the project into the venv itself, so stage 1 needs no source tree.
RUN uv sync --locked --no-default-groups --no-editable

# Runtime stage: no uv, no build tooling, no source tree outside the venv.
FROM python:3.14-slim-bookworm

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PORT=8000

USER app
EXPOSE 8000

CMD ["uvicorn", "glia_nav.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
