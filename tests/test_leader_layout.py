# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
"""2026-11-17：组长的“我的”和“小组管理”分开；填周报的引导词；三份使用手册（系统页面 + Word）。"""
from datetime import date
from io import BytesIO
from django.test import TransactionTestCase, override_settings
from docx import Document
from reports import planning
from reports.domain import FIELDS, GUIDES, SECTIONS
from reports.models import Group, GroupMembership as M, Instruction, User
from reports.services import period_for
from .test_domain import instant

PASSWORD = 'Test-only-Password-892!'
EARLY = date(2026, 10, 3)


@override_settings(EMAIL_ENABLED=False, PUBLIC_BASE_URL='https://weekly.example.invalid')
class LeaderLayoutTests(TransactionTestCase):
    def setUp(self):
        def make(key, name, **extra):
            return User.objects.create_user(f'{key}@example.invalid', PASSWORD, name=name, created_at=instant(15), **extra)
        self.root = make('root', '虚构最高权限', is_staff=True)
        self.lead = make('lead', '虚构组长', identity=User.MASTER, grade=2024)
        self.a = make('a', '虚构甲')
        self.A = Group.objects.create(name='虚构A组', sort_order=1)
        self.B = Group.objects.create(name='虚构B组', sort_order=2)
        M.objects.create(user=self.lead, group=self.A, role=M.LEADER, start=EARLY)
        M.objects.create(user=self.lead, group=self.B, role=M.MEMBER, start=EARLY)  # 在 B 组只是组员
        M.objects.create(user=self.a, group=self.A, role=M.MEMBER, start=EARLY)

    def get(self, user, path):
        self.client.force_login(user)
        return self.client.get(path)

    def test_leader_sidebar_has_personal_and_group_sections(self):
        home = self.get(self.lead, '/')
        self.assertTemplateUsed(home, 'dashboard.html')
        html = home.content.decode()
        mine, group = html.index('<p class="nav-label">我的</p>'), html.index('<p class="nav-label">小组管理</p>')
        self.assertLess(mine, html.index('href="/my/"'))
        self.assertLess(html.index('href="/my/"'), group)  # 我的周报在“我的”下
        self.assertLess(group, html.index(f'href="/groups/{self.A.pk}/"'))  # 所带小组在“小组管理”下
        self.assertNotIn(f'href="/groups/{self.B.pk}/" class="nav-group', html)  # 只是组员的小组不放在小组管理里
        self.assertContains(home, '<h1>小组概览</h1>', html=False)
        self.assertContains(home, 'class="personal-bar"')
        self.assertContains(home, '去填写')
        self.assertNotContains(home, '<h2>我的本周周报</h2>', html=False)
        self.assertNotContains(home, '<h2>待反馈的批示</h2>', html=False)

    def test_instructions_badge_moves_to_personal(self):
        planning.create_instruction(self.root, Instruction.GROUP, '虚构批示：本周汇报', group=self.A)
        home = self.get(self.lead, '/')
        self.assertContains(home, '去反馈')
        self.assertContains(home, '待反馈的批示">1</span>', html=False)  # 角标在“我的周报”上
        my = self.client.get('/my/')
        self.assertContains(my, '虚构批示：本周汇报')
        self.assertNotContains(my, '虚构A组 · 小组任务')  # 自己带的小组不在个人页
        self.assertContains(my, '虚构B组 · 小组任务')  # 只是组员的小组仍在

    def test_member_sidebar_unchanged(self):
        home = self.get(self.a, '/')
        self.assertTemplateUsed(home, 'my_home.html')
        self.assertNotContains(home, 'class="nav-label"')
        for link in ('href="/"', 'href="/my/tasks/"', 'href="/help/"'):
            self.assertContains(home, link, html=False)

    def test_editor_shows_guides_for_every_item(self):
        page = self.get(self.a, f'/my/weeks/{period_for().pk}/')
        for section in SECTIONS:
            for key in FIELDS:
                hint, point, detail = GUIDES[(section, key)]
                self.assertContains(page, f'<p class="field-guide">{hint}</p>', html=False)
                self.assertContains(page, f'data-point-placeholder="{point}"', html=False)
                self.assertContains(page, f'data-detail-placeholder="{detail}"', html=False)
        self.assertContains(page, '点“提交周报”才算正式提交')

    def test_manuals_by_role(self):
        page = self.get(self.a, '/help/')
        self.assertContains(page, '<h1>组员使用手册</h1>', html=False)
        self.assertContains(page, 'https://weekly.example.invalid')  # {url} 换成网站地址
        self.assertNotContains(page, '{url}')
        self.assertNotContains(page, 'help-tabs')
        self.assertEqual(self.client.get('/help/?role=leader').status_code, 404)
        self.assertEqual(self.client.get('/help/super.docx').status_code, 404)
        page = self.get(self.lead, '/help/')
        self.assertContains(page, '<h1>小组长使用手册</h1>', html=False)
        self.assertContains(page, '?role=member')
        self.assertNotContains(page, '?role=super')
        page = self.get(self.root, '/help/')
        self.assertContains(page, '<h1>最高权限使用手册</h1>', html=False)
        for role in ('leader', 'member'):
            self.assertContains(page, f'?role={role}')

    def test_static_files_carry_version(self):
        """反向代理和浏览器会缓存静态文件 4 小时；地址带内容摘要，更新后马上用新文件。"""
        import re
        from reports.context import asset_version
        version = asset_version()
        self.assertRegex(version, r'^[0-9a-f]{10}$')
        login = self.client.get('/login/')
        self.assertContains(login, f'/static/auth.css?v={version}', html=False)
        page = self.get(self.a, f'/my/weeks/{period_for().pk}/')
        for name in ('site.css', 'ui.js', 'count.js', 'editor.js'):
            self.assertContains(page, f'/static/{name}?v={version}', html=False)
        self.assertFalse(re.search(r'/static/[\w.]+\.(css|js)"', page.content.decode()))  # 没有不带版本的

    def test_manual_word_download(self):
        self.client.force_login(self.root)
        for role, title in (('super', '最高权限使用手册'), ('leader', '小组长使用手册'), ('member', '组员使用手册')):
            response = self.client.get(f'/help/{role}.docx')
            self.assertEqual(response.status_code, 200)
            self.assertIn('wordprocessingml', response['Content-Type'])
            text = '\n'.join(p.text for p in Document(BytesIO(response.content)).paragraphs)
            self.assertIn(title, text)
            self.assertIn('https://weekly.example.invalid', text)
            self.assertNotIn('{url}', text)
