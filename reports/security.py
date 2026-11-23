# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
"""敏感请求限流。计数存数据库（多进程共享、重启不清零），不记录正文或密码。

登录只计“失败”的尝试，成功登录不计数——同一校园网出口后面很多人同时登录不会互相挡住：
- 同一账号 10 分钟内失败 10 次：该账号暂停登录（防猜密码）；
- 同一来源 10 分钟内失败 50 次：该来源暂停登录（防撞库）。
忘记密码、设置密码、催交：每个来源 10 分钟最多 20 次。

来源 IP：部署在反向代理（Caddy）后面时设置 BEHIND_PROXY=true，取代理写入的 X-Forwarded-For 最后一段；
网站直接对外时不要开启，否则客户端可以伪造来源绕过限流。
"""
import hashlib
from django.conf import settings
from django.db import connection
from django.shortcuts import render

WINDOW_SECONDS = 600
LOGIN_ACCOUNT_LIMIT = 10
LOGIN_IP_LIMIT = 50
OTHER_LIMIT = 20

_WINDOW = 'floor(extract(epoch from now()) / 600)::bigint'


def client_ip(request):
    if getattr(settings, 'BEHIND_PROXY', False):
        forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
        if forwarded:
            return forwarded.split(',')[-1].strip()
    return request.META.get('REMOTE_ADDR', '')


def _key(*parts):
    return hashlib.sha256(':'.join(parts).encode()).hexdigest()


def _count(key):
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT hits FROM reports_throttle WHERE key = %s AND window_id = {_WINDOW}', [key])
        row = cursor.fetchone()
    return row[0] if row else 0


def _add(key):
    with connection.cursor() as cursor:
        cursor.execute(f'''INSERT INTO reports_throttle (key, window_id, hits) VALUES (%s, {_WINDOW}, 1)
            ON CONFLICT (key) DO UPDATE SET hits = CASE WHEN reports_throttle.window_id = excluded.window_id THEN reports_throttle.hits + 1 ELSE 1 END,
            window_id = excluded.window_id RETURNING hits''', [key])
        return cursor.fetchone()[0]


def _account_key(account):
    return _key('login-account', account.strip().lower())


def clear_login_lock(account):
    """最高权限重置密码后，立即解除该账号的登录暂停。"""
    with connection.cursor() as cursor:
        cursor.execute('DELETE FROM reports_throttle WHERE key = %s', [_account_key(account)])


def _too_many(request, text):
    response = render(request, 'message.html', {'text': text}, status=429)
    response['Retry-After'] = str(WINDOW_SECONDS)
    return response


class RateLimitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method != 'POST':
            return self.get_response(request)
        ip = client_ip(request)
        if request.path == '/login/':
            account = request.POST.get('username', '').strip()[:150]
            ip_key = _key('login-ip', ip)
            account_key = _account_key(account) if account else None
            if _count(ip_key) >= LOGIN_IP_LIMIT or account_key and _count(account_key) >= LOGIN_ACCOUNT_LIMIT:
                return _too_many(request, '登录失败次数过多，请 10 分钟后再试。如果忘记了密码，请联系最高权限重置。')
            response = self.get_response(request)
            if response.status_code == 200:  # 登录页原样返回 = 账号或密码不对
                _add(ip_key)
                if account_key:
                    _add(account_key)
            elif response.status_code == 302 and account_key:
                clear_login_lock(account)
            return response
        if request.path == '/password-reset/' or request.path.startswith('/activate/') or '/reminders/' in request.path:
            if _add(_key(ip, request.path.split('/')[1])) > OTHER_LIMIT:
                return _too_many(request, '操作过于频繁，请 10 分钟后再试。')
        return self.get_response(request)
