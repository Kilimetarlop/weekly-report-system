# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
"""查看 / 编辑模式（2026-11-16）：默认查看，最高权限和组长点“进入编辑”后才显示修改类操作；批示属于查看功能。"""
import uuid
from datetime import date, timedelta
from django.test import TransactionTestCase
from django.utils import timezone
from reports import planning
from reports.models import Group, GroupMembership as M, Instruction, User
from reports.services import ensure_obligations, period_for, write
from .test_domain import instant, valid_payload

PASSWORD = 'Test-only-Password-892!'
EARLY = date(2026, 10, 3)
EDIT_MARKERS = ['name="progress"', '<summary>修改</summary>', '<summary>退回修改</summary>']


class ModeTests(TransactionTestCase):
    def setUp(self):
        def make(key, name, order, **extra):
            return User.objects.create_user(f'{key}@example.invalid', PASSWORD, name=name, sort_order=order, created_at=instant(15), **extra)
        self.root = make('root', '虚构最高权限', 0, is_staff=True)
        self.lead = make('lead', '虚构组长', 1)
        self.a = make('a', '虚构甲', 2)
        self.A = Group.objects.create(name='A组', sort_order=1)
        M.objects.create(user=self.lead, group=self.A, role=M.LEADER, start=EARLY)
        M.objects.create(user=self.a, group=self.A, role=M.MEMBER, start=EARLY)
        self.week = period_for()
        ensure_obligations(self.week)
        today = timezone.localdate()
        self.task = planning.create_group_task(self.lead, self.A, '数据采集', today - timedelta(days=3), today + timedelta(days=10), assignee_ids=[self.a.pk])
        write(self.a, self.week, valid_payload('本周完成了虚构数据采集并整理了全部结果记录'), 0, uuid.uuid4(), timezone.now())
        self.instruction = planning.create_instruction(self.root, Instruction.GROUP, '注意进度', group=self.A)

    def login(self, user):
        self.client.force_login(user)
        return self.client

    def test_view_mode_is_default_and_clean(self):
        page = self.login(self.root).get(f'/people/{self.a.pk}/')
        for marker in EDIT_MARKERS:
            self.assertNotContains(page, marker, html=False)
        self.assertNotContains(page, 'class="mode-banner"')
        self.assertContains(page, '<summary>写批示</summary>', html=False)  # 批示属于查看功能
        self.assertContains(page, 'class="mode-toggle"')
        self.assertContains(page, '进入编辑')
        group = self.client.get(f'/groups/{self.A.pk}/')
        self.assertNotContains(group, '添加小组任务')
        self.assertNotContains(group, '>撤回<', html=False)
        self.assertContains(group, '<summary>写批示</summary>', html=False)

    def test_edit_mode_shows_controls_until_finished(self):
        client = self.login(self.root)
        client.post('/mode/', {'mode': 'edit', 'next': f'/people/{self.a.pk}/'})
        page = client.get(f'/people/{self.a.pk}/')
        for marker in EDIT_MARKERS:
            self.assertContains(page, marker, html=False)
        self.assertContains(page, 'class="mode-banner"')
        self.assertNotContains(page, 'class="mode-toggle"')  # 退出编辑只在顶部横幅，避免两个“完成编辑”
        group = client.get(f'/groups/{self.A.pk}/')  # 模式在各页面之间保持
        self.assertContains(group, '添加小组任务')
        self.assertContains(group, '>撤回<', html=False)
        client.post('/mode/', {'mode': 'view'})
        self.assertNotContains(client.get(f'/groups/{self.A.pk}/'), '添加小组任务')

    def test_admin_pages_follow_the_mode(self):
        client = self.login(self.root)
        checks = {'/manage/members/': 'value="create"', '/manage/groups/': 'value="create"', '/manage/settings/': 'name="min_chars"', f'/manage/weeks/{self.week.pk}/': 'name="exempt"'}
        for url, marker in checks.items():
            self.assertNotContains(client.get(url), marker, html=False, msg_prefix=url)
        client.post('/mode/', {'mode': 'edit'})
        for url, marker in checks.items():
            self.assertContains(client.get(url), marker, html=False, msg_prefix=url)

    def test_leader_uses_the_same_modes(self):
        client = self.login(self.lead)
        self.assertNotContains(client.get(f'/groups/{self.A.pk}/'), '添加小组任务')
        client.post('/mode/', {'mode': 'edit'})
        self.assertContains(client.get(f'/groups/{self.A.pk}/'), '添加小组任务')
        self.assertContains(client.get(f'/people/{self.a.pk}/?group={self.A.pk}'), '<summary>退回修改</summary>', html=False)

    def test_members_cannot_switch_and_own_tasks_stay_editable(self):
        client = self.login(self.a)
        self.assertEqual(client.post('/mode/', {'mode': 'edit'}).status_code, 403)
        self.assertContains(client.get('/my/tasks/'), 'name="progress"', html=False)
        self.assertNotContains(client.get(f'/groups/{self.A.pk}/'), 'name="progress"', html=False)
