FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev

RUN useradd --create-home --shell /bin/bash appuser \
    && mkdir -p /app/config \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8765

CMD ["statement-fetcher", "serve", "--host", "0.0.0.0", "--port", "8765"]
