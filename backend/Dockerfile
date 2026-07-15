# ---- build stage: compile wheels with toolchain, kept out of runtime image ----
FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    freetds-dev gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# ---- runtime stage: only the FreeTDS runtime lib, no compiler ----
FROM python:3.12-slim

# freetds-bin pulls in libsybdb5 (the runtime lib pymssql links against)
RUN apt-get update && apt-get install -y --no-install-recommends \
    freetds-bin \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

COPY . .

EXPOSE 5000

# Gunicorn tuning is env-overridable so dev can re-enable --reload via compose.
#   --preload        : import app once in master, workers share it copy-on-write
#                      (also serializes migrations to a single run at startup)
#   --max-requests   : recycle each worker after N requests to reclaim leaked/
#                      fragmented heap (jitter avoids all workers recycling at once)
#   --worker-tmp-dir : /dev/shm keeps gunicorn heartbeat files off disk
ENV GUNICORN_WORKERS=2 \
    GUNICORN_THREADS=4 \
    GUNICORN_TIMEOUT=300 \
    GUNICORN_MAX_REQUESTS=500 \
    GUNICORN_MAX_REQUESTS_JITTER=50

CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:5000 \
    --workers ${GUNICORN_WORKERS} \
    --threads ${GUNICORN_THREADS} \
    --timeout ${GUNICORN_TIMEOUT} \
    --max-requests ${GUNICORN_MAX_REQUESTS} \
    --max-requests-jitter ${GUNICORN_MAX_REQUESTS_JITTER} \
    --worker-tmp-dir /dev/shm \
    --preload \
    'app:create_app()'"]
