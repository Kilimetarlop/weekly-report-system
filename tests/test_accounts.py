# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
"""账号与登录验收。

账号登录、最高权限只做管理、邮件总开关、批量导入、重置与修改密码。全部为虚构数据。
"""
import json
import uuid
from datetime import date
from io import BytesIO, StringIO
from unittest.mock import patch
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import CommandError, call_command
from django.test import Client, TransactionTestCase, override_settings
from openpyxl import Workbook, load_workbook
from reports import access, accounts, grouping
from reports.domain import RuleError
from reports.models import Audit, Group, GroupMembership as M, Mail, Obligation, User
from reports.services import ensure_obligations, period_for, return_report, rows, write
from .test_domain import instant, valid_payload

PASSWORD = 'Test-only-Password-892!'


# 旧模板没有“身份”列（都按学生）；这里的行按旧模板写，顺带验证旧模板仍能导入。新列见 test_identity.py。
OLD_COLUMNS = ['姓名', '账号', '密码', '所在小组', '担任组长的小组', '邮箱（选填）']


def workbook(*data, header=None):
    book = Workbook()
    sheet = book.active
    sheet.append(header or OLD_COLUMNS)
    for row in data:
        sheet.append(list(row))
    stream = BytesIO()
    book.save(stream)
    stream.seek(0)
    return stream


@override_settings(EMAIL_ENABLED=False)
class AccountTests(TransactionTestCase):
    def setUp(self):
        self.root = User.objects.create_user('DemoAdmin-Test', PASSWORD, name='虚构实验室账号', is_staff=True, created_at=instant(15))
        self.member = User.objects.create_user('member01', PASSWORD, name='虚构成员', created_at=instant(15))
        self.period = period_for(instant())
        ensure_obligations(self.period)

    def login(self, user):
        client = Client()
        client.force_login(user)
        return client

    def test_A01_login_by_account_case_insensitive(self):
        page = Client().get('/login/')
        self.assertContains(page, '>账号</label>')
        for typed in ('DemoAdmin-Test', 'demoadmin-test', '  DEMOADMIN-TEST '):
            self.assertEqual(Client().post('/login/', {'username': typed, 'password': PASSWORD}).status_code, 302, typed)
        self.assertContains(Client().post('/login/', {'username': 'member01', 'password': 'wrong-password'}), '账号或密码不正确')

    def test_A02_super_accounts_only_manage(self):
        self.assertFalse(Obligation.objects.filter(user=self.root).exists())
        self.assertNotIn(self.root, [r['user'] for r in rows(self.period)])
        client = self.login(self.root)
        self.assertTemplateUsed(client.get('/'), 'dashboard.html')  # 首页即看板
        self.assertNotContains(client.get('/manage/members/'), '我的周报')
        with self.assertRaises(RuleError):
            grouping.add_membership(self.root, Group.objects.create(name='A组'), self.root, M.MEMBER, '2026-11-16')

    def test_A03_email_switch_off_sends_nothing(self):
        client = self.login(self.root)
        self.assertEqual(client.get(f'/manage/weeks/{self.period.pk}/reminders/').status_code, 404)
        self.assertEqual(client.post('/manage/mail/test/').status_code, 404)
        self.assertNotContains(client.get(f'/manage/weeks/{self.period.pk}/'), '催交')
        self.assertContains(client.get('/manage/settings/'), '邮件功能：已关闭')
        self.assertContains(Client().post('/password-reset/', {'email': 'x@example.invalid'}), '联系课题组的最高权限')
        Mail.objects.create(user=self.member, kind='test', dedupe='left-over', subject='s', body='b')
        call_command('worker', once=True, verbosity=0)
        self.assertEqual(Mail.objects.get(dedupe='left-over').state, 'queued')

    def test_A04_return_shown_in_app_without_email(self):
        write(self.member, self.period, valid_payload(), 0, uuid.uuid4(), instant())
        return_report(self.root, self.period, self.member.pk, '请补充实验记录')
        self.assertFalse(Mail.objects.exists())
        page = self.login(self.member).get('/')
        self.assertContains(page, '请补充实验记录')
        self.assertContains(page, '退回人：虚构实验室账号')

    def test_A05_import_creates_accounts_groups_and_leaders(self):
        file = workbook(
            ['张测试', 'zhangtest', 'Weekly-Report-Zt-26', 'A组', 'A组', ''],
            ['李测试', 'litest', 'Weekly-Report-Lt-26', 'A组、B组', '', 'li@example.invalid'],
            ['王测试', 2023001, 'Weekly-Report-Ww-26', 'B组', 'B组', None],
        )
        report = accounts.import_members(self.root, file, '2026-10-14')
        self.assertTrue(report.ok)
        self.assertEqual(report.new_groups, ['A组', 'B组'])
        self.assertEqual(report.start, date(2026, 10, 10))
        self.assertTrue(User.objects.get(account='2023001').check_password('Weekly-Report-Ww-26'))
        self.assertEqual(Client().post('/login/', {'username': 'LiTest', 'password': 'Weekly-Report-Lt-26'}).status_code, 302)
        start = date(2026, 10, 10)
        self.assertEqual(set(M.objects.values_list('user__account', 'group__name', 'role', 'start')), {
            ('zhangtest', 'A组', M.LEADER, start), ('litest', 'A组', M.MEMBER, start), ('litest', 'B组', M.MEMBER, start), ('2023001', 'B组', M.LEADER, start)})
        self.assertEqual(access.led_group_ids(User.objects.get(account='zhangtest')), {Group.objects.get(name='A组').pk})
        self.assertEqual(User.objects.get(account='litest').email, 'li@example.invalid')
        self.assertTrue(Audit.objects.filter(action='import_members', operator=self.root).exists())
        self.assertFalse(any('Weekly-Report' in json.dumps(a.metadata, ensure_ascii=False) for a in Audit.objects.all()))

    def test_A06_check_only_writes_nothing(self):
        report = accounts.import_members(self.root, workbook(['试导入', 'drytest', 'Weekly-Report-Dr-26', 'C组', '', '']), '2026-11-16', dry_run=True)
        self.assertTrue(report.ok and report.dry_run)
        self.assertEqual([c['account'] for c in report.created], ['drytest'])
        self.assertFalse(User.objects.filter(account='drytest').exists())
        self.assertFalse(Group.objects.exists())

    def test_A07_any_error_imports_nothing(self):
        Group.objects.create(name='旧组', archived=True)
        file = workbook(
            ['好人', 'goodone', 'Weekly-Report-Ok-26', 'A组', '', ''],
            ['', 'noname', 'Weekly-Report-Nn-26', '', '', ''],
            ['弱密码', 'weakpw', '12345678', '', '', ''],
            ['重复', 'GOODONE', 'Weekly-Report-Dp-26', '', '', ''],
            ['空格', 'has space', 'Weekly-Report-Sp-26', '', '', ''],
            ['坏邮箱', 'bademail', 'Weekly-Report-Be-26', '', '', 'not-an-email'],
            ['首尾空格', 'trail', ' Weekly-Report-Tr-26', '', '', ''],
            ['归档', 'archtest', 'Weekly-Report-Ar-26', '旧组', '', ''],
        )
        report = accounts.import_members(self.root, file, '2026-11-16')
        self.assertFalse(report.ok)
        self.assertEqual([e['row'] for e in report.errors], [3, 4, 5, 6, 7, 8, 9])
        self.assertIn('与第 2 行重复', report.errors[2]['message'])
        self.assertFalse(User.objects.filter(account='goodone').exists())
        self.assertFalse(Group.objects.filter(name='A组').exists())

    def test_A08_existing_accounts_skipped_unchanged(self):
        file = workbook(['改了名', 'MEMBER01', 'Weekly-Report-New-26', 'A组', 'A组', ''], ['新人', 'newbie', 'Weekly-Report-Nb-26', '', '', ''])
        report = accounts.import_members(self.root, file, '2026-11-16')
        self.assertTrue(report.ok)
        self.assertEqual([s['account'] for s in report.skipped], ['member01'])
        self.member.refresh_from_db()
        self.assertTrue(self.member.check_password(PASSWORD))
        self.assertEqual(self.member.name, '虚构成员')
        self.assertFalse(M.objects.filter(user=self.member).exists())
        self.assertEqual(report.new_groups, [])
        self.assertTrue(User.objects.filter(account='newbie').exists())

    def test_A09_import_pages_super_only(self):
        member = self.login(self.member)
        for url in ['/manage/members/import/', '/manage/members/import/template.xlsx']:
            self.assertEqual(member.get(url).status_code, 403, url)
        self.assertEqual(member.post('/manage/members/import/', {'start': '2026-11-16', 'action': 'import'}).status_code, 403)
        client = self.login(self.root)
        template = load_workbook(BytesIO(client.get('/manage/members/import/template.xlsx').content))
        self.assertEqual([c.value for c in template['成员'][1]], accounts.COLUMNS)
        # 模板不含示例数据行（避免误导入）；第 2 行起只预设了文本格式，没有任何值。
        self.assertTrue(all(cell.value is None for row in template['成员'].iter_rows(min_row=2) for cell in row))
        self.assertEqual(template['成员']['B2'].number_format, '@')
        content = workbook(['页面导入', 'webtest', 'Weekly-Report-Wb-26', 'D组', 'D组', '']).getvalue()
        page = client.post('/manage/members/import/', {'file': SimpleUploadedFile('m.xlsx', content), 'start': '2026-11-16', 'action': 'check'})
        self.assertContains(page, '检查通过')
        self.assertFalse(User.objects.filter(account='webtest').exists())
        page = client.post('/manage/members/import/', {'file': SimpleUploadedFile('m.xlsx', content), 'start': '2026-11-16', 'action': 'import'})
        self.assertContains(page, '导入完成')
        self.assertTrue(User.objects.get(account='webtest').check_password('Weekly-Report-Wb-26'))
        self.assertContains(client.post('/manage/members/import/', {'file': SimpleUploadedFile('m.xlsx', b'not a workbook'), 'start': '2026-11-16', 'action': 'check'}), '无法读取文件')

    def test_A10_super_resets_member_password(self):
        accounts.reset_password(self.root, self.member, 'Weekly-Report-Reset-26')
        self.member.refresh_from_db()
        self.assertTrue(self.member.check_password('Weekly-Report-Reset-26'))
        with self.assertRaises(RuleError):
            accounts.reset_password(self.root, self.root, 'Weekly-Report-Reset-27')
        with self.assertRaises(RuleError):
            accounts.reset_password(self.root, self.member, '123')
        with self.assertRaises(PermissionDenied):
            accounts.reset_password(self.member, self.member, 'Weekly-Report-Reset-28')
        self.assertEqual(self.login(self.member).post('/manage/members/', {'action': 'reset', 'user': self.member.pk, 'password': 'Weekly-Report-Reset-29'}).status_code, 403)
        self.assertTrue(Audit.objects.filter(action='password_reset', operator=self.root).exists())

    def test_A11_member_changes_own_password(self):
        client = self.login(self.member)
        new = 'Weekly-Report-Own-26'
        self.assertEqual(client.post('/password/', {'old_password': 'wrong', 'new_password1': new, 'new_password2': new}).status_code, 200)
        self.assertContains(client.post('/password/', {'old_password': PASSWORD, 'new_password1': PASSWORD, 'new_password2': PASSWORD}), '新密码不能与原密码相同')
        self.assertEqual(client.post('/password/', {'old_password': PASSWORD, 'new_password1': new, 'new_password2': new}).status_code, 302)
        self.member.refresh_from_db()
        self.assertTrue(self.member.check_password(new))
        self.assertEqual(client.get('/').status_code, 200)  # 当前会话保持登录

    def test_A12_password_rules_apply(self):
        self.assertIn('账号', ''.join(accounts.password_problems('zhangsan2026', 'zhangsan2026x', '虚构张三')))
        self.assertTrue(accounts.password_problems('12345678', 'someone', '虚构某人'))
        self.assertEqual(accounts.password_problems('Weekly-Report-Ok-26', 'someone', '虚构某人'), [])

    def test_A13_init_admin_creates_manager_account(self):
        with patch('reports.management.commands.init_admin.getpass.getpass', return_value='Weekly-Report-Admin-26'):
            call_command('init_admin', '--account', 'DemoAdmin', '--name', 'DemoAdmin', stdout=StringIO())
        user = User.objects.get(account='DemoAdmin')
        self.assertTrue(user.is_staff)
        self.assertTrue(user.check_password('Weekly-Report-Admin-26'))
        self.assertFalse(Obligation.objects.filter(user=user).exists())
        with patch('reports.management.commands.init_admin.getpass.getpass', return_value='Weekly-Report-Admin-27'), self.assertRaises(CommandError):
            call_command('init_admin', '--account', 'DEMOADMIN', '--name', '重复')

    def test_A14_single_create_validates(self):
        client = self.login(self.root)
        client.post('/manage/members/', {'action': 'create', 'name': '虚构新人', 'account': 'MEMBER01', 'password': 'Weekly-Report-Dup-26'})
        self.assertEqual(User.objects.filter(account__iexact='member01').count(), 1)
        page = client.post('/manage/members/', {'action': 'create', 'name': '虚构新人', 'account': 'newone', 'password': '123'}, follow=True)
        self.assertContains(page, '操作失败')
        self.assertFalse(User.objects.filter(account='newone').exists())
