# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
"""快速批量导入（2026-11-16）：直接粘贴表格（姓名、年级、课题名称……）、“年级”一列写“2026级硕士”、
没填账号用姓名、没填密码自动生成（只在导入完成的页面显示一次、可下载）、下载成员表、增量导入。"""
import base64
import json
import re
from io import BytesIO
from django.test import Client, TransactionTestCase, override_settings
from openpyxl import load_workbook
from reports import accounts
from reports.models import Audit, Group, GroupMembership as M, User
from .test_domain import instant

PASSWORD = 'Test-only-Password-892!'
PASTE = '姓名\t年级\t课题名称\n虚构甲\t2026级硕士\t虚构课题：图像分类\n虚构乙\t2026级硕士\t虚构课题：模型压缩\n虚构丙\t2021级博士\t虚构课题：数据增强\n'
START = '2026-11-16'


@override_settings(EMAIL_ENABLED=False)
class QuickImportTests(TransactionTestCase):
    def setUp(self):
        self.root = User.objects.create_user('root@example.invalid', PASSWORD, name='虚构最高权限', is_staff=True, created_at=instant(15))
        self.group = Group.objects.create(name='A组', sort_order=1)

    def client_for(self, user):
        client = Client()
        client.force_login(user)
        return client

    def test_paste_three_columns_creates_accounts_with_generated_passwords(self):
        report = accounts.import_text(self.root, PASTE, START, group_id=self.group.pk)
        self.assertTrue(report.ok, report.errors)
        self.assertEqual([(c['account'], c['tag'], c['topic']) for c in report.created],
                         [('虚构甲', '2026级硕士', '虚构课题：图像分类'), ('虚构乙', '2026级硕士', '虚构课题：模型压缩'), ('虚构丙', '2021级博士', '虚构课题：数据增强')])
        for item in report.created:
            self.assertRegex(item['password'], r'^[A-Za-z0-9]{5}-[A-Za-z0-9]{5}$')
            self.assertTrue(User.objects.get(account=item['account']).check_password(item['password']))
        # 新成员没填小组的，加入所选小组
        self.assertEqual(set(M.objects.values_list('user__account', 'group__name', 'role')), {(a, 'A组', M.MEMBER) for a in ('虚构甲', '虚构乙', '虚构丙')})
        # 账号就是姓名，可以直接登录；审计里没有明文密码
        self.assertEqual(Client().post('/login/', {'username': '虚构甲', 'password': report.created[0]['password']}).status_code, 302)
        dumped = json.dumps([a.metadata for a in Audit.objects.all()], ensure_ascii=False)
        self.assertFalse(any(item['password'] in dumped for item in report.created))

    def test_paste_without_header_or_with_spaces_and_check_only(self):
        report = accounts.import_text(self.root, '虚构丁 2025级博士 虚构课题 带空格的名称\n虚构戊，老师', START, dry_run=True)
        self.assertTrue(report.ok, report.errors)
        self.assertEqual([(c['name'], c['tag'], c['topic']) for c in report.created], [('虚构丁', '2025级博士', '虚构课题 带空格的名称'), ('虚构戊', '老师', '')])
        self.assertEqual([c['password'] for c in report.created], ['', ''])  # 检查时不生成、不显示密码
        self.assertFalse(User.objects.filter(account='虚构丁').exists())

    def test_paste_problems_import_nothing(self):
        text = '姓名\t年级\n虚构甲\t2026级硕士\n虚构甲\t2026级硕士\n虚构己\t26级硕士\n虚构庚\t硕士\n'
        report = accounts.import_text(self.root, text, START)
        self.assertFalse(report.ok)
        problems = {e['row']: e['message'] for e in report.errors}
        self.assertIn('与第 2 行重复', problems[3])
        self.assertIn('同名', problems[3])  # 提示同名的人另填账号
        self.assertIn('年级写成', problems[4])
        self.assertIn('要填年级', problems[5])
        self.assertFalse(User.objects.filter(account='虚构甲').exists())

    def test_incremental_reimport_only_fills_in_profile(self):
        accounts.import_text(self.root, PASTE, START)
        user = User.objects.get(account='虚构甲')
        password_hash = user.password
        text = '姓名\t年级\t课题名称\n虚构甲\t2026级硕士\t虚构课题：图像分类（改）\n虚构乙\t\t\n虚构辛\t2026级硕士\t虚构课题：新加的人\n'
        report = accounts.import_text(self.root, text, START)
        self.assertTrue(report.ok, report.errors)
        self.assertEqual([c['account'] for c in report.created], ['虚构辛'])
        self.assertEqual([u['account'] for u in report.updated], ['虚构甲'])
        self.assertIn('课题', report.updated[0]['notes'][0])
        self.assertEqual([s['account'] for s in report.skipped], ['虚构乙'])
        user.refresh_from_db()
        self.assertEqual((user.topic, user.password), ('虚构课题：图像分类（改）', password_hash))  # 已有账号不改密码
        self.assertEqual(User.objects.get(account='虚构乙').topic, '虚构课题：模型压缩')  # 空着的格子不清空
        self.assertEqual(User.objects.filter(name='虚构甲').count(), 1)  # 不重复建

    def test_roster_download_and_incremental_reimport(self):
        accounts.import_text(self.root, PASTE, START, group_id=self.group.pk)
        response = self.client_for(self.root).get('/manage/members/roster.xlsx')
        self.assertEqual(response.status_code, 200)
        sheet = load_workbook(BytesIO(response.content))['成员']
        rows = [[cell.value for cell in row] for row in sheet.iter_rows()]
        self.assertEqual(rows[0], accounts.COLUMNS)
        self.assertEqual(len(rows), 4)  # 不含最高权限
        self.assertEqual([rows[1][i] for i in (0, 1, 2, 3, 5)], ['虚构甲', '2026级硕士', '虚构课题：图像分类', 'A组', '虚构甲'])
        # 原样导回：什么都不变
        report = accounts.import_members(self.root, BytesIO(response.content), START)
        self.assertTrue(report.ok, report.errors)
        self.assertEqual((len(report.created), len(report.updated), len(report.skipped)), (0, 0, 3))
        # 在后面加一个老师（担任 A 组组长）再导入：只新建这一个
        book = load_workbook(BytesIO(response.content))
        book['成员'].append(['虚构壬', '老师', '', '', 'A组', '', '', ''])
        stream = BytesIO()
        book.save(stream)
        stream.seek(0)
        report = accounts.import_members(self.root, stream, START)
        self.assertEqual([c['account'] for c in report.created], ['虚构壬'])
        self.assertTrue(User.objects.get(account='虚构壬').is_teacher)
        self.assertEqual(M.objects.get(user__account='虚构壬').role, M.LEADER)

    def test_import_page_paste_flow_shows_passwords_once(self):
        client = self.client_for(self.root)
        page = client.post('/manage/members/import/', {'source': 'text', 'text': PASTE, 'start': START, 'action': 'check', 'group': ''})
        self.assertContains(page, '检查通过')
        self.assertContains(page, '导入时自动生成')
        self.assertFalse(User.objects.filter(account='虚构甲').exists())
        page = client.post('/manage/members/import/', {'source': 'text', 'text': PASTE, 'start': START, 'action': 'import', 'group': str(self.group.pk)})
        self.assertContains(page, '导入完成')
        self.assertContains(page, '初始密码只显示这一次')
        html = page.content.decode()
        password = re.search(r'<code class="password">([^<]+)</code>', html).group(1)
        self.assertTrue(User.objects.get(account='虚构甲').check_password(password))
        data = re.search(r'base64,([A-Za-z0-9+/=]+)"', html).group(1)
        sheet = load_workbook(BytesIO(base64.b64decode(data)))['新账号']
        self.assertEqual((sheet['A2'].value, sheet['B2'].value, sheet['C2'].value), ('虚构甲', '虚构甲', password))
        again = client.get('/manage/members/import/')
        self.assertNotContains(again, password)  # 密码只在导入完成的那一页出现

    def test_topic_shows_on_pages_and_can_be_edited(self):
        accounts.import_text(self.root, PASTE, START, group_id=self.group.pk)
        client = self.client_for(self.root)
        user = User.objects.get(account='虚构甲')
        self.assertContains(client.get(f'/people/{user.pk}/'), '虚构课题：图像分类')
        self.assertContains(client.get(f'/groups/{self.group.pk}/'), '<span class="member-topic">虚构课题：图像分类</span>', html=False)
        client.post('/mode/', {'mode': 'edit'})
        client.post('/manage/members/', {'action': 'identity', 'user': user.pk, 'identity': 'master', 'grade': '2026', 'topic': '虚构课题：改过'})
        user.refresh_from_db()
        self.assertEqual(user.topic, '虚构课题：改过')

    def test_super_rows_skipped_and_roster_is_super_only(self):
        report = accounts.import_text(self.root, '姓名\t账号\n虚构最高权限\troot@example.invalid', START)
        self.assertEqual([s['reason'] for s in report.skipped], ['最高权限账号，不在导入范围'])
        member = User.objects.create_user('member@example.invalid', PASSWORD, name='虚构成员')
        self.assertEqual(self.client_for(member).get('/manage/members/roster.xlsx').status_code, 403)
