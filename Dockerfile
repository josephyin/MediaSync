FROM node:22-alpine AS frontend-builder

WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build


FROM python:3.12-slim AS backend-builder

WORKDIR /build
COPY backend/pyproject.toml ./
COPY backend/app ./app
RUN pip wheel --no-cache-dir --wheel-dir /wheels .


FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ADMIN_PASSWORD=admin \
    IMAGE_DEFAULT_ADMIN_ONLY=true

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends --yes nginx \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/nginx/sites-enabled/default

WORKDIR /app

COPY --from=backend-builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl \
    && rm -rf /wheels

COPY backend/alembic.ini ./
COPY backend/migrations ./migrations
COPY --from=frontend-builder /build/dist /usr/share/nginx/html
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY deploy/nginx-appliance.conf /etc/nginx/nginx-appliance.conf

VOLUME ["/data"]

EXPOSE 9090

HEALTHCHECK --interval=30s --timeout=15s --start-period=60s --retries=3 \
    CMD ["python", "-m", "app.appliance.healthcheck"]

CMD ["python", "-m", "app.appliance"]
