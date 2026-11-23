# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
"""登录限流验收（L01—L07）：只计失败、按账号和来源分别计数、反向代理后取真实来源。"""
from unittest.mock import patch
from django.contrib.auth.hashers import make_password
from django.db import connection
from django.test import Client, TransactionTestCase, override_settings
from reports import accounts
from reports.models import User
from .test_domain import instant

PASSWORD = 'Test-only-Password-892!'


def clear_throttle():
    # reports_throttle 由原生 SQL 创建，测试框架不会自动清空。
    with connection.cursor() as cursor:
        cursor.execute('DELETE FROM reports_throttle')


class LoginLimitTests(TransactionTestCase):
    def setUp(self):
        clear_throttle()
        self.root = User.objects.create_user('root-test', PASSWORD, name='虚构最高权限', is_staff=True, created_at=instant(15))
        self.user = User.objects.create_user('member-a', PASSWORD, name='虚构甲', created_at=instant(15))

    def tearDown(self):
        clear_throttle()

    def login(self, account, password=PASSWORD, forwarded=None, **meta):
        if forwarded:
            meta['HTTP_X_FORWARDED_FOR'] = forwarded
        return Client().post('/login/', {'username': account, 'password': password}, **meta)

    def test_L01_many_people_behind_one_address_can_all_log_in(self):
        # 原来全组 10 分钟共 20 次；现在成功登录不计数，同一出口 22 人依次登录都成功。
        hashed = make_password(PASSWORD)
        for index in range(22):
            User.objects.create(account=f'lab{index}', name=f'虚构成员{index}', password=hashed, created_at=instant(15))
        for index in range(22):
            self.assertEqual(self.login(f'lab{index}').status_code, 302, index)

    def test_L02_ten_failures_lock_that_account_only(self):
        for _ in range(10):
            self.assertEqual(self.login('member-a', 'wrong-password').status_code, 200)
        blocked = self.login('member-a')  # 密码正确也要等
        self.assertEqual(blocked.status_code, 429)
        self.assertContains(blocked, '10 分钟后再试', status_code=429)
        self.assertEqual(blocked['Retry-After'], '600')
        self.assertEqual(self.login('MEMBER-A').status_code, 429)  # 账号不区分大小写
        self.assertEqual(self.login('root-test').status_code, 302)  # 同一来源的其他账号不受影响

    @override_settings(BEHIND_PROXY=True)
    def test_L03_behind_proxy_uses_real_client_address(self):
        with patch('reports.security.LOGIN_IP_LIMIT', 3):
            for index in range(3):
                self.assertEqual(self.login(f'nobody{index}', 'wrong', forwarded='203.0.113.7').status_code, 200)
            self.assertEqual(self.login('member-a', forwarded='203.0.113.7').status_code, 429)
            self.assertEqual(self.login('member-a', forwarded='spoofed, 203.0.113.7').status_code, 429)  # 只认代理追加的最后一段
            self.assertEqual(self.login('member-a', forwarded='198.51.100.9').status_code, 302)

    @override_settings(BEHIND_PROXY=False)
    def test_L04_forwarded_header_ignored_without_proxy(self):
        with patch('reports.security.LOGIN_IP_LIMIT', 3):
            for index in range(3):
                self.login(f'nobody{index}', 'wrong', forwarded=f'198.51.100.{index}')  # 伪造不同来源
            self.assertEqual(self.login('member-a', forwarded='192.0.2.55').status_code, 429)

    def test_L05_success_restarts_account_count(self):
        for _ in range(9):
            self.login('member-a', 'wrong')
        self.assertEqual(self.login('member-a').status_code, 302)
        for _ in range(9):
            self.assertEqual(self.login('member-a', 'wrong').status_code, 200)
        self.assertEqual(self.login('member-a').status_code, 302)

    def test_L06_password_reset_unlocks_account(self):
        for _ in range(10):
            self.login('member-a', 'wrong')
        self.assertEqual(self.login('member-a').status_code, 429)
        accounts.reset_password(self.root, self.user, 'Weekly-Report-Reset-26')
        self.assertEqual(self.login('member-a', 'Weekly-Report-Reset-26').status_code, 302)

    def test_L07_other_sensitive_pages_still_limited(self):
        for _ in range(20):
            self.assertEqual(Client().post('/password-reset/', {'email': 'x@example.invalid'}).status_code, 200)
        self.assertEqual(Client().post('/password-reset/', {'email': 'x@example.invalid'}).status_code, 429)
