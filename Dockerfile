FROM ghcr.io/astral-sh/uv:0.7.15 AS uv

FROM python:3.13.4-slim-bookworm AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY docs/api-v1.md ./docs/api-v1.md
COPY alembic.ini ./
COPY migrations ./migrations
RUN --mount=type=secret,id=local_ca,required=false \
    if [ -f /run/secrets/local_ca ]; then \
      cp /run/secrets/local_ca /usr/local/share/ca-certificates/local-ca.crt; \
      update-ca-certificates; \
    fi && \
    uv sync --frozen --no-dev --no-editable --native-tls

FROM mcr.microsoft.com/playwright/python:v1.62.0-noble@sha256:aa81288e738725378becba5b3e06cb0f3a7f012a610e87e8d767a090ea3f740d AS browser-tests
WORKDIR /preview
RUN pip install --no-cache-dir \
    playwright==1.62.0 \
    pyee==13.0.1 \
    greenlet==3.5.5 \
    typing-extensions==4.16.0
COPY scripts/run_local_preview_journey.py ./run_local_preview_journey.py
ENTRYPOINT ["python", "run_local_preview_journey.py"]

FROM python:3.13.4-slim-bookworm AS runtime
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=build --chown=app:app /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
USER app
EXPOSE 8080
ENTRYPOINT ["agent-data-oracle"]
CMD ["web"]
