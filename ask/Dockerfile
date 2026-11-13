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
# tesseract-ocr (+ English data) and poppler-utils (pdftoppm): the pdf source plug-in
# reads scanned PDF pages with OCR. More OCR languages: add tesseract-ocr-<lang>, e.g.
#   docker compose build --build-arg OCR_LANGS="tesseract-ocr-deu tesseract-ocr-fra" vco-ask
ARG OCR_LANGS=""
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
      tesseract-ocr tesseract-ocr-eng poppler-utils ${OCR_LANGS} \
 && rm -rf /var/lib/apt/lists/*

COPY ask/requirements.txt .
# --require-hashes: every package must match the hash in the lock file.
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

# Shared look and feel: the first container's stylesheet, the About page files and the
# vendored front-end libraries (Swagger UI, marked, DOMPurify), served under /shared.
COPY core/static/app.css core/static/about.css core/static/about.js core/static/dialogs.js ./shared/
COPY core/static/vendor ./shared/vendor
# The About page shows the project README and its screenshots.
COPY README.md ./README.md
COPY docs/screenshots ./docs/screenshots
COPY ask/app ./app
COPY ask/sources ./sources
# Retrieval tests (no Ollama needed): docker compose exec vco-ask python -m unittest discover -s tests -v
COPY ask/tests ./tests
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
