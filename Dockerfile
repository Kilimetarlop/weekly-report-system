FROM python:3.12.15-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends libreoffice-writer fonts-noto-cjk postgresql-client curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
RUN useradd --create-home --uid 10001 weekly
COPY --chown=weekly:weekly . .
RUN mkdir -p /app/private /app/staticfiles && chown -R weekly:weekly /app/private /app/staticfiles
USER weekly
RUN python manage.py collectstatic --noinput
# 启动时自动迁移、更新静态文件，再以 1 GB 内存的配置启动 Gunicorn（见 scripts/web_start.sh）
CMD ["sh", "scripts/web_start.sh"]
