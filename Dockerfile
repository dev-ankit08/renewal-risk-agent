# Self-hosted image: admin UI + REST API + Slack app + scheduler in one process.
#   docker build -t renewal-risk .
#   docker run --env-file .env -v $PWD/config:/app/config -v rr-state:/app/state -p 8080:8080 renewal-risk
# Then open http://localhost:8080/admin/ — the first-run setup token is in the container log.

# --- 1. build the admin UI ---------------------------------------------------
FROM node:22-slim AS ui
WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund
COPY web/ ./
RUN mkdir -p /renewal_risk/admin && npm run build

# --- 2. the service ------------------------------------------------------------
FROM python:3.12-slim

# Node.js runs MCP servers started with npx (Salesforce MCP via the sf CLI, others).
# Salesforce sources connected with OAuth in the admin UI do not need it.
ARG INSTALL_SF_CLI=false
RUN apt-get update \
 && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
 && if [ "$INSTALL_SF_CLI" = "true" ]; then npm install -g @salesforce/cli; fi \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY renewal_risk ./renewal_risk
COPY --from=ui /renewal_risk/admin/static ./renewal_risk/admin/static
COPY examples/csv-demo/config ./examples/csv-demo/config
COPY examples/csv-demo/generate.py ./examples/csv-demo/generate.py
RUN pip install --no-cache-dir ".[all]"

RUN useradd --create-home --uid 10001 app && mkdir -p /app/config /app/state && chown -R app /app
USER app

# Set RENEWAL_RISK_MASTER_KEY (Fernet key) from your secret manager in production;
# without it a key file is generated in the state volume. Set RENEWAL_RISK_PUBLIC_URL
# to the URL people use to reach this service (OAuth callbacks are built from it).
ENV RENEWAL_RISK_CONFIG_DIR=/app/config \
    RENEWAL_RISK_STATE_DIR=/app/state \
    RENEWAL_RISK_DEMO_DIR=/app/examples/csv-demo \
    HOST=0.0.0.0 \
    PORT=8080 \
    PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/v1/health')"
CMD ["renewal-risk", "serve"]
