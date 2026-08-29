FROM ghcr.io/astral-sh/uv:0.10.11 AS uv

FROM python:3.11-slim

COPY --from=uv /uv /uvx /bin/

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    APP_DB_PATH=/data/waggle_recover.db \
    WAGGLE_DB_PATH=/data/waggle_memory.db \
    WAGGLE_EMBEDDING_MODEL=fake \
    DECISION_PROVIDER=deterministic

RUN uv venv /app/.venv \
    && uv pip install --python /app/.venv/bin/python \
        "fastapi>=0.111.0" \
        "uvicorn>=0.30.0" \
        "pydantic>=2.7.0" \
        "pydantic-settings>=2.3.0" \
        "python-dotenv>=1.0.0" \
        "httpx>=0.27.0" \
        "numpy>=1.26.0" \
        "networkx>=3.2.1" \
        "cryptography>=45.0.0,<46.0.0" \
        "requests>=2.32.5" \
        "langgraph>=1.0,<2" \
        "groq>=1.0,<2" \
    && uv pip install --python /app/.venv/bin/python --no-deps \
        "waggle-mcp @ git+https://github.com/Abhigyan-Shekhar/Waggle-mcp.git@cf2118fc62f4972d2c072e9061c6e01da36ecc13"

COPY app ./app

RUN mkdir -p /data

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
