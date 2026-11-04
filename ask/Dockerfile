# vCommunity Ask image. Build from the REPOSITORY ROOT (it reuses the first
# container's theme from core/static):
#   docker build -f ask/Dockerfile -t vcommunity-ask:0.1.0 .
# or simply: docker compose up -d --build vco-ask
#
# Python only (no PowerShell), native arm64 on Apple Silicon. Layers go
# base OS -> git -> Python deps -> app code, so patching one dependency
# rebuilds only from that layer down.

ARG PYTHON_IMAGE=python:3.13-slim
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# git: the markdown-repo source plug-in clones and pulls repositories.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY ask/requirements.txt .
# --require-hashes: every package must match the hash in the lock file.
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

# Shared look and feel: the first container's stylesheet and the vendored Swagger UI.
COPY core/static/app.css ./shared/app.css
COPY core/static/vendor/swagger-ui ./shared/vendor/swagger-ui
COPY ask/app ./app
COPY ask/sources ./sources
COPY ask/config/ask.example.yaml ./config/ask.yaml

RUN chmod -R a+rX /app \
 && useradd --system --uid 10002 --create-home ask \
 && mkdir -p /app/data /sources /knowledge && chown -R ask /app/data /sources
USER ask
ENV ASK_CONFIG=/app/config/ask.yaml ASK_DATA_DIR=/app/data ASK_SHARED_DIR=/app/shared
EXPOSE 8081
VOLUME ["/app/data"]
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/api/health')"
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8081"]
