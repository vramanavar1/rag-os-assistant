# RAG-OS API image.
#
# MUST stay buildable by the CLASSIC Docker builder: 06-registry-build.ps1 builds this with `az acr build`,
# and ACR Tasks has no BuildKit. So no `RUN --mount`, no `COPY --link`, no heredocs and no `# syntax=`
# directive - any of them fails the build in Azure even though it works locally, where Docker defaults to
# BuildKit. A cache mount would buy nothing on ACR in any case: every task runs in a fresh container.
#
# The same image runs the API, the ingestion worker and the scheduler/bootstrap jobs:
#   api:        uvicorn rag_os.api.main:app --host 0.0.0.0 --port 8000 --proxy-headers   (default CMD)
#   worker:     rag-os worker
#   scheduler:  rag-os schedule-tick
#   bootstrap:  rag-os bootstrap
FROM python:3.13-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-editable
COPY src ./src
RUN uv sync --frozen --no-editable

FROM python:3.13-slim AS runtime
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app \
    && mkdir -p /data/raw && chown -R app:app /data
WORKDIR /app
COPY --from=build --chown=app:app /app/.venv /app/.venv
# Default configuration (used when CONFIG_STORE=filesystem; Azure uses the `config` blob container)
COPY --chown=app:app config /app/config
# Alembic migrations (the rag-bootstrap job applies them)
COPY --chown=app:app alembic.ini /app/alembic.ini
COPY --chown=app:app migrations /app/migrations
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CONFIG_DIR=/app/config \
    RAW_DIR=/data/raw \
    SERVICE_NAME=rag-api
USER 10001
EXPOSE 8000
CMD ["uvicorn", "rag_os.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-access-log"]
