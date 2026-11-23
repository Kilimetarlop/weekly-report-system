# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
import os
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', '')
DEBUG = os.environ.get('APP_ENV', 'development') == 'development'
if not SECRET_KEY:
    if not DEBUG:
        raise RuntimeError('生产环境必须设置 DJANGO_SECRET_KEY')
    SECRET_KEY = 'development-only-not-for-production'
if not DEBUG and (len(SECRET_KEY) < 50 or SECRET_KEY.startswith('replace-')):
    raise RuntimeError('生产密钥必须为至少 50 字符的安全随机值')
ALLOWED_HOSTS = os.environ.get('DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1,testserver').split(',')
CSRF_TRUSTED_ORIGINS = list(filter(None, os.environ.get('DJANGO_CSRF_TRUSTED_ORIGINS', '').split(',')))
INSTALLED_APPS = ['django.contrib.auth', 'django.contrib.contenttypes', 'django.contrib.sessions', 'django.contrib.messages', 'django.contrib.staticfiles', 'reports']
MIDDLEWARE = ['django.middleware.security.SecurityMiddleware', 'django.middleware.clickjacking.XFrameOptionsMiddleware', 'django.contrib.sessions.middleware.SessionMiddleware', 'django.middleware.common.CommonMiddleware', 'django.middleware.csrf.CsrfViewMiddleware', 'reports.security.RateLimitMiddleware', 'django.contrib.auth.middleware.AuthenticationMiddleware', 'reports.middleware.ActiveMembershipMiddleware', 'django.contrib.messages.middleware.MessageMiddleware', 'reports.middleware.PrivateCacheMiddleware']
ROOT_URLCONF = 'config.urls'
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates', 'DIRS': [BASE_DIR / 'templates'], 'APP_DIRS': True, 'OPTIONS': {'context_processors': ['django.template.context_processors.request', 'django.contrib.auth.context_processors.auth', 'django.contrib.messages.context_processors.messages', 'reports.context.site_settings']}}]
DATABASES = {'default': {'ENGINE': 'django.db.backends.postgresql', 'NAME': os.environ.get('DB_NAME', 'weekly_reports'), 'USER': os.environ.get('DB_USER', 'weekly_app'), 'PASSWORD': os.environ.get('DB_PASSWORD', ''), 'HOST': os.environ.get('DB_HOST', '127.0.0.1'), 'PORT': os.environ.get('DB_PORT', '5432')}}
AUTH_USER_MODEL = 'reports.User'
AUTH_PASSWORD_VALIDATORS = [{'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator', 'OPTIONS': {'user_attributes': ('account', 'name', 'email')}}, {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'}, {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'}, {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'}]
TIME_ZONE = 'Asia/Shanghai'
USE_TZ = True
LANGUAGE_CODE = 'zh-hans'
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/login/'
# 邮件总开关（默认关闭）：关闭时不发催交、退回通知、重置密码等任何邮件，催交入口隐藏；忘记密码改由最高权限在网页重置。
EMAIL_ENABLED = os.environ.get('EMAIL_ENABLED', 'false').lower() == 'true'
EMAIL_BACKEND = os.environ.get('EMAIL_BACKEND', 'django.core.mail.backends.filebased.EmailBackend')
EMAIL_FILE_PATH = BASE_DIR / 'private' / 'mail'
EMAIL_HOST = os.environ.get('EMAIL_HOST', '')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
EMAIL_USE_TLS = os.environ.get('EMAIL_USE_TLS', 'true').lower() == 'true'
EMAIL_USE_SSL = os.environ.get('EMAIL_USE_SSL', 'false').lower() == 'true'
if EMAIL_USE_TLS and EMAIL_USE_SSL:
    raise RuntimeError('TLS 与 SSL 不可同时启用')
EMAIL_TIMEOUT = 30
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', 'weekly@example.invalid')
PUBLIC_BASE_URL = os.environ.get('PUBLIC_BASE_URL', 'http://localhost:8000').rstrip('/')
PASSWORD_RESET_TIMEOUT = 3600
DATA_UPLOAD_MAX_MEMORY_SIZE = 8 * 1024 * 1024
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_SSL_REDIRECT = not DEBUG
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
# 只有部署在反向代理（Caddy）后面、且网站端口不直接对外时才设为 true；限流据此从 X-Forwarded-For 取真实来源。
BEHIND_PROXY = os.environ.get('BEHIND_PROXY', 'false').lower() == 'true'
X_FRAME_OPTIONS = 'DENY'
SECURE_REFERRER_POLICY = 'same-origin'
