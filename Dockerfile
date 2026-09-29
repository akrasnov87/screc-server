FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# --- фиксированный UID/GID = 1000 ---
# Совпадает с типичным первым пользователем Linux (ubuntu, user и т.п.),
# поэтому bind-mount ./data и ./logs будут доступны на запись без chown.
ARG UID=1000
ARG GID=1000

RUN groupadd -g ${GID} screc 2>/dev/null || true \
    && useradd -u ${UID} -g ${GID} -d /app -s /sbin/nologin screc \
    && mkdir -p /data /logs \
    && chown -R screc:screc /app /data /logs

USER screc

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]