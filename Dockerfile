FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PUBLIC_MODE=1 AUTO_HOST=1 DEMO_MODE=1 ALLOW_PUBLIC_DEMO=1 KITTY_DB_PATH=/data/catino-v2.db

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd --create-home catino && mkdir -p /data && chown -R catino /data /app
USER catino
VOLUME /data

EXPOSE 8001
# One worker only: the crowd table and the rounds live in memory.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8001} --proxy-headers --forwarded-allow-ips='*'"]
