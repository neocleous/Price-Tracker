# syntax=docker/dockerfile:1
ARG WITH_PLAYWRIGHT=false

FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TZ=Europe/Zurich
RUN apt-get update && apt-get install -y --no-install-recommends tzdata ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY tracker ./tracker
RUN pip install .

# --- optional headless Chromium for bot-walled sources (parser: playwright) ---
FROM base AS playwright-true
RUN pip install "playwright>=1.45" && playwright install --with-deps chromium && rm -rf /var/lib/apt/lists/*

FROM base AS playwright-false

FROM playwright-${WITH_PLAYWRIGHT} AS final
COPY products /config/products
VOLUME ["/data", "/config/products"]
ENV DB_PATH=/data/tracker.sqlite PRODUCTS_DIR=/config/products WEB_PORT=8099
EXPOSE 8099
HEALTHCHECK --interval=10m --timeout=10s --start-period=1m \
  CMD python -c "import sqlite3,sys,datetime as d,os; c=sqlite3.connect(os.environ['DB_PATH']); r=c.execute('select max(ts_end) from runs').fetchone()[0]; sys.exit(0 if r and (d.datetime.now(d.timezone.utc)-d.datetime.fromisoformat(r)).total_seconds()<10*3600 else 1)"
ENTRYPOINT ["python", "-m", "tracker"]
CMD ["schedule"]
