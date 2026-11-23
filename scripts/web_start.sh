#!/bin/sh
# 网站容器启动：自动迁移数据库、更新静态文件，再启动 Gunicorn。
# 按 1 GB 内存服务器调优：2 个进程 × 2 线程；--preload 让进程共享已加载的代码；
# 每处理约 1000 个请求轮换一次进程，避免内存缓慢增长。内存更大时可在 .env 里调 WEB_WORKERS / WEB_THREADS。
set -e
python manage.py migrate --noinput
python manage.py collectstatic --noinput --verbosity 0
exec gunicorn config.wsgi:application \
  --bind 0.0.0.0:8000 \
  --workers "${WEB_WORKERS:-2}" --threads "${WEB_THREADS:-2}" \
  --preload --max-requests 1000 --max-requests-jitter 100 --timeout 60 \
  --access-logfile - --access-logformat '%(h)s %(m)s %(s)s %(D)s'
