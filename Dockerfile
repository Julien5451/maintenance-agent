FROM ghcr.io/astral-sh/uv:0.13.6-python3.12-trixie-slim@sha256:bf889bda77474aa50c351caf1538b855ef2b7a30add45f26ca50dcb6ade06402

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH" \
    DATABASE_DIR=/app/runtime

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY data ./data
RUN uv sync --frozen --no-dev && \
    groupadd --system app && useradd --system --gid app --home-dir /app app && \
    mkdir -p /app/runtime && chown -R app:app /app
USER app
EXPOSE 8000 8501
CMD ["uvicorn", "maintenance_agent.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
