# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
"""身份（2026-11-16）：硕士 / 博士 / 学生（不分硕博）写周报，老师选填（2026-11-23）——不进应交名单、不计入提交率；老师可以当组长看本组。
标签只显示“老师”或“2023级硕士”“2021级博士”（年级 = 入学年份）。成员管理和批量导入可以设置身份和年级。"""
import uuid
from datetime import timedelta
from io import BytesIO
from django.core.exceptions import PermissionDenied
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from openpyxl import Workbook
from reports import accounts, dashboard
from reports.domain import RuleError
from reports.models import Audit, Group, GroupMembership as M, Obligation, User
from reports.services import backfill, eligible, ensure_obligations, period_for, rows, write
from .test_domain import valid_payload

PASSWORD = 'Test-only-Password-892!'
TEXT = '本周完成了虚构数据采集并整理了全部结果记录'
TEACHER_TAG = '<span class="badge tag">老师</span>'


# 上一版模板：身份、年级分两列（仍然支持；新模板的“年级”一列直接写“2026级硕士”，见 test_quick_import.py）
PREVIOUS_COLUMNS = ['姓名', '账号', '密码', '身份', '年级', '所在小组', '担任组长的小组', '邮箱（选填）']


def workbook(*data):
    book = Workbook()
    sheet = book.active
    sheet.append(PREVIOUS_COLUMNS)
    for row in data:
        sheet.append(list(row))
    stream = BytesIO()
    book.save(stream)
    stream.seek(0)
    return stream


@override_settings(EMAIL_ENABLED=False)
class IdentityTests(TransactionTestCase):
    def setUp(self):
        now = timezone.now()
        created = now - timedelta(days=30)

        def make(key, name, order, **extra):
            return User.objects.create_user(f'{key}@example.invalid', PASSWORD, name=name, sort_order=order, created_at=created, **extra)
        self.root = make('root', '虚构最高权限', 0, is_staff=True)
        self.teacher = make('teacher', '虚构王', 1, identity=User.TEACHER)
        self.student = make('student', '虚构甲', 2)
        self.A = Group.objects.create(name='A组', sort_order=1)
        start = period_for(created).start
        M.objects.create(user=self.teacher, group=self.A, role=M.LEADER, start=start)
        M.objects.create(user=self.student, group=self.A, role=M.MEMBER, start=start)
        self.week = period_for()
        self.last_week = period_for(now - timedelta(days=7))
        self.next_week = period_for(now + timedelta(days=7))

    def test_teacher_is_never_obliged_or_counted(self):
        backfill()
        self.assertFalse(Obligation.objects.filter(user=self.teacher).exists())
        self.assertTrue(Obligation.objects.filter(user=self.student, period=self.last_week).exists())
        data = rows(self.week)
        self.assertEqual([r['user'] for r in data], [self.student])
        self.assertEqual(dashboard.summarize(data)['required'], 1)
        self.assertFalse(eligible(self.teacher, self.next_week))

    def test_teacher_leader_sees_group_and_report_is_optional(self):
        self.client.force_login(self.teacher)
        home = self.client.get('/')
        self.assertTemplateUsed(home, 'dashboard.html')  # 组长：本组看板
        self.assertNotContains(home, '我的本周周报')
        self.assertContains(home, 'href="/my/"')  # 侧栏“我的周报”：老师也可以写，选填
        self.assertContains(home, '选填')
        self.assertContains(home, '老师 · 组长')
        self.assertContains(home, '组长 虚构王老师')
        self.assertEqual(self.client.get(f'/my/weeks/{self.week.pk}/').status_code, 200)
        group = self.client.get(f'/groups/{self.A.pk}/')
        self.assertContains(group, TEACHER_TAG, html=False)  # 标签只写“老师”
        self.assertNotContains(group, '不写周报')
        self.assertContains(group, '本周已交 0/1')  # 只算学生
        # 不当组长的老师：成员首页有选填的“本周周报”，页头标签只写“老师”
        other = User.objects.create_user('other@example.invalid', PASSWORD, name='虚构李', identity=User.TEACHER)
        self.client.force_login(other)
        page = self.client.get('/')
        self.assertTemplateUsed(page, 'my_home.html')
        self.assertContains(page, '<h2>本周周报</h2>', html=False)
        self.assertContains(page, '选填，不计入提交率')
        self.assertNotContains(page, '逾期未交')
        self.assertContains(page, TEACHER_TAG, html=False)

    def test_person_page_and_reading_order_skip_teacher(self):
        self.client.force_login(self.root)
        self.assertEqual([u for u, _ in dashboard.person_sequence(self.root)], [self.student])
        page = self.client.get(f'/people/{self.teacher.pk}/?group={self.A.pk}')
        self.assertContains(page, TEACHER_TAG, html=False)
        self.assertNotContains(page, '老师 · 不写周报')
        self.assertContains(page, '老师周报选填，还没有提交。')  # 周报区的空白说明
        self.assertNotContains(page, '/1 人')  # 不在翻看顺序里，不显示“第 x/1 人”

    def test_degree_tags_show_on_pages_without_touching_obligations(self):
        backfill()
        before = Obligation.objects.filter(user=self.student).count()
        user, removed = accounts.set_identity(self.root, self.student, User.MASTER, '2023级')
        self.assertEqual((user.tag, removed), ('2023级硕士', []))
        self.assertIsNone(user.identity_changed_at)  # 学生之间改只换标签，不影响应交起算
        self.assertEqual(Obligation.objects.filter(user=self.student).count(), before)
        self.assertEqual([u for u, _ in dashboard.person_sequence(self.root)], [user])  # 硕士照常排进翻看顺序
        self.client.force_login(self.root)
        self.assertContains(self.client.get(f'/groups/{self.A.pk}/'), '<span class="badge tag">2023级硕士</span>', html=False)
        self.assertContains(self.client.get(f'/people/{user.pk}/'), '<span class="badge tag">2023级硕士</span>', html=False)
        self.client.force_login(user)
        home = self.client.get('/')
        self.assertContains(home, '<span class="badge tag">2023级硕士</span>', html=False)  # 本人首页
        self.assertContains(home, '<small>2023级硕士</small>', html=False)  # 侧栏左下角

    def test_setting_teacher_drops_unsubmitted_weeks_and_keeps_submitted(self):
        backfill()
        write(self.student, self.last_week, valid_payload(TEXT), 0, uuid.uuid4(), self.last_week.deadline - timedelta(hours=1))
        weeks = Obligation.objects.filter(user=self.student).count()
        self.assertGreater(weeks, 2)
        user, removed = accounts.set_identity(self.root, self.student, User.TEACHER)
        self.assertTrue(user.is_teacher)
        self.assertEqual(len(removed), weeks - 1)
        self.assertEqual(list(Obligation.objects.filter(user=self.student).values_list('period_id', flat=True)), [self.last_week.pk])
        self.assertEqual(rows(self.week), [])
        self.assertEqual([r['state'] for r in rows(self.last_week)], ['已提交'])  # 已交的那周保留
        self.assertTrue(Audit.objects.filter(action='identity', object_id=str(self.student.pk)).exists())
        backfill()
        self.assertEqual(Obligation.objects.filter(user=self.student).count(), 1)  # 不会再被补出来

    def test_back_to_student_starts_now_without_backfilling(self):
        backfill()
        user, removed = accounts.set_identity(self.root, self.teacher, User.DOCTOR, 2024)
        self.assertEqual((removed, user.tag), ([], '2024级博士'))
        backfill()
        starts = list(Obligation.objects.filter(user=self.teacher).values_list('period__start', flat=True))
        self.assertTrue(all(start >= self.week.start for start in starts), starts)  # 以往各周不补
        self.assertFalse(eligible(user, self.last_week))
        self.assertTrue(eligible(user, self.next_week))

    def test_identity_rules(self):
        with self.assertRaises(RuleError):
            accounts.set_identity(self.root, self.root, User.TEACHER)  # 最高权限不设身份
        with self.assertRaises(RuleError):
            accounts.set_identity(self.root, self.student, 'boss')
        with self.assertRaisesMessage(RuleError, '要填年级'):
            accounts.set_identity(self.root, self.student, User.DOCTOR)  # 硕士、博士必须有年级
        with self.assertRaisesMessage(RuleError, '入学年份'):
            accounts.set_identity(self.root, self.student, User.MASTER, '23')
        with self.assertRaises(PermissionDenied):
            accounts.set_identity(self.teacher, self.student, User.TEACHER)  # 只有最高权限能改
        # 标签：老师 / 2023级硕士 / 2021级博士；不分硕博的学生只显示年级或不显示
        self.assertEqual(User(identity=User.TEACHER, grade=2020).tag, '老师')
        self.assertEqual(User(identity=User.MASTER, grade=2023).tag, '2023级硕士')
        self.assertEqual(User(identity=User.DOCTOR, grade=2021).tag, '2021级博士')
        self.assertEqual(User(identity=User.STUDENT, grade=2024).tag, '2024级')
        self.assertEqual(User().tag, '')
        self.assertEqual(User(is_staff=True, identity=User.MASTER, grade=2023).tag, '')
        # 列组长时老师名后加“老师”，名字已带“老师”的不重复
        self.assertEqual(User(name='王伟', identity=User.TEACHER).name_with_title, '王伟老师')
        self.assertEqual(User(name='张老师', identity=User.TEACHER).name_with_title, '张老师')
        self.assertEqual(User(name='王伟').name_with_title, '王伟')

    def test_members_page_sets_identity_in_edit_mode(self):
        self.client.force_login(self.root)
        page = self.client.get('/manage/members/')
        self.assertContains(page, TEACHER_TAG, html=False)
        self.assertNotContains(page, 'data-identity-was')  # 查看模式只看
        self.client.post('/mode/', {'mode': 'edit'})
        self.assertContains(self.client.get('/manage/members/'), 'data-identity-was')
        response = self.client.post('/manage/members/', {'action': 'identity', 'user': self.student.pk, 'identity': 'master', 'grade': '2023'}, follow=True)
        self.assertContains(response, '资料已保存：2023级硕士')
        response = self.client.post('/manage/members/', {'action': 'identity', 'user': self.student.pk, 'identity': 'doctor', 'grade': ''}, follow=True)
        self.assertContains(response, '硕士、博士要填年级')  # 不合规时提示，不改动
        self.student.refresh_from_db()
        self.assertEqual(self.student.tag, '2023级硕士')
        response = self.client.post('/manage/members/', {'action': 'identity', 'user': self.student.pk, 'identity': 'teacher', 'grade': '2023'}, follow=True)
        self.assertContains(response, '已设为老师')
        self.student.refresh_from_db()
        self.assertEqual((self.student.tag, self.student.grade), ('老师', None))  # 老师不保存年级
        self.client.post('/manage/members/', {'action': 'create', 'name': '虚构新博士', 'account': 'newdoctor', 'password': 'Weekly-Report-Nd-26', 'identity': 'doctor', 'grade': '2025'})
        self.assertEqual(User.objects.get(account='newdoctor').tag, '2025级博士')
        self.client.post('/manage/members/', {'action': 'create', 'name': '虚构新老师', 'account': 'newteacher', 'password': 'Weekly-Report-Nt-26', 'identity': 'teacher'})
        self.assertTrue(User.objects.get(account='newteacher').is_teacher)

    def test_import_identity_and_grade_columns(self):
        report = accounts.import_members(self.root, workbook(
            ['虚构坏身份', 'badidentity', 'Weekly-Report-Bi-26', '博士后', '', '', '', ''],
            ['虚构缺年级', 'nograde', 'Weekly-Report-Ng-26', '博士', '', '', '', ''],
            ['虚构坏年级', 'badgrade', 'Weekly-Report-Bg-26', '硕士', '23', '', '', ''],
        ), self.week.start.isoformat(), dry_run=True)
        problems = [e['message'] for e in report.errors]
        self.assertIn('身份只能填', problems[0])
        self.assertIn('要填年级', problems[1])
        self.assertIn('年级要写入学年份', problems[2])
        report = accounts.import_members(self.root, workbook(
            ['虚构李老师', 'teacher02', 'Weekly-Report-Te-26', '老师', '', 'B组', 'B组', ''],
            ['虚构乙', 'student02', 'Weekly-Report-St-26', '硕士', '2024级', 'B组', '', ''],
            ['虚构丙', 'student03', 'Weekly-Report-Sc-26', '博士生', 2022, 'B组', '', ''],
        ), self.week.start.isoformat())
        self.assertTrue(report.ok, report.errors)
        self.assertEqual([c['tag'] for c in report.created], ['老师', '2024级硕士', '2022级博士'])
        teacher, student = User.objects.get(account='teacher02'), User.objects.get(account='student02')
        self.assertTrue(teacher.is_teacher)
        self.assertEqual(student.tag, '2024级硕士')
        self.assertEqual(M.objects.get(user=teacher).role, M.LEADER)
        ensure_obligations(self.next_week)
        self.assertFalse(Obligation.objects.filter(user=teacher).exists())
        self.assertTrue(Obligation.objects.filter(user=student, period=self.next_week).exists())
