# Staging cloud image -- a small, self-contained HTTP + MongoDB service.
#
# Standalone: the build context is THIS repository's root (no parent repo, no
# `COPY src/...`). Build it with:
#
#   docker build -t staging-cloud .
#
# Runtime: plain HTTP on 0.0.0.0:$PORT. TLS is terminated by the platform
# (Render) or a reverse proxy in front -- never in this container.

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    PORT=8080

WORKDIR /app

# Install runtime deps first for layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# The staging cloud package (its vendored contracts travel with it).
COPY staging_cloud/ ./staging_cloud/

# Render / most PaaS health-check this path.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
        u=f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8080\")}/healthz'; \
        sys.exit(0 if urllib.request.urlopen(u, timeout=4).status == 200 else 1)"

EXPOSE 8080

CMD ["python", "-m", "staging_cloud"]
