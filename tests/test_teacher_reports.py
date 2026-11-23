# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
"""2026-11-23：老师可以自愿写周报（不强制、不计入提交率等指标，批示、反馈、退回、合订配套可用）；甘特图红线随日期移动。"""
import json
import uuid
from datetime import date, timedelta
from io import BytesIO
from types import SimpleNamespace
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from openpyxl import load_workbook
from reports import dashboard, gantt
from reports.domain import RuleError
from reports.models import ExportJob, Group, GroupMembership as M, Instruction, InstructionReceipt, Obligation, Report, Submission, User
from reports.services import backfill, can_write, eligible, is_voluntary, period_for, rows, voluntary_rows, write
from .test_domain import valid_payload

PASSWORD = 'Test-only-Password-892!'
TEXT = '本周完成了虚构数据采集并整理了全部结果记录'


@override_settings(EMAIL_ENABLED=False)
class TeacherReportTests(TransactionTestCase):
    def setUp(self):
        now = timezone.now()
        created = now - timedelta(days=30)

        def make(key, name, order, **extra):
            return User.objects.create_user(f'{key}@example.invalid', PASSWORD, name=name, sort_order=order, created_at=created, **extra)
        self.root = make('root', '虚构最高权限', 0, is_staff=True)
        self.teacher = make('teacher', '虚构王', 1, identity=User.TEACHER)
        self.student = make('student', '虚构甲', 2)
        self.lead_b = make('leadb', '虚构乙组长', 3)
        self.A = Group.objects.create(name='A组', sort_order=1)
        self.B = Group.objects.create(name='B组', sort_order=2)
        start = period_for(created).start
        M.objects.create(user=self.teacher, group=self.A, role=M.LEADER, start=start)
        M.objects.create(user=self.student, group=self.A, role=M.MEMBER, start=start)
        M.objects.create(user=self.lead_b, group=self.B, role=M.LEADER, start=start)
        self.week = period_for()
        self.last_week = period_for(now - timedelta(days=7))

    def submit_by_page(self, user, period=None):
        self.client.force_login(user)
        period = period or self.week
        report = Report.objects.filter(user=user, period=period).first()
        revision = report.draft.revision if report and hasattr(report, 'draft') else 0
        return self.client.post(f'/my/weeks/{period.pk}/submit/', json.dumps({'expected_revision': revision, 'payload': valid_payload(TEXT), 'idempotency_key': str(uuid.uuid4())}), content_type='application/json')

    def test_teacher_report_changes_no_statistics(self):
        backfill()
        before = dashboard.summarize(rows(self.week))
        obligations = Obligation.objects.count()
        self.assertEqual(self.submit_by_page(self.teacher).status_code, 200)
        backfill()
        self.assertTrue(Report.objects.get(user=self.teacher, period=self.week).latest_id)
        self.assertEqual(Obligation.objects.count(), obligations)  # 不产生应交记录
        self.assertFalse(Obligation.objects.filter(user=self.teacher).exists())
        self.assertFalse(eligible(self.teacher, self.week))
        self.assertEqual(dashboard.summarize(rows(self.week)), before)
        self.assertNotIn(self.teacher, [r['user'] for r in rows(self.week)])
        home = self.client.get('/')
        self.assertEqual((home.context['now']['required'], home.context['now']['submitted']), (1, 0))  # 老师组长看本组：只有 1 名学生应交，老师不算
        group = self.client.get(f'/groups/{self.A.pk}/')
        self.assertContains(group, '本周已交 0/1')  # 分母分子都不含老师
        self.assertContains(group, '本周 已交')  # 老师自己的行标已交

    def test_no_deadline_or_overdue_for_teachers_but_students_still_locked(self):
        self.assertTrue(is_voluntary(self.teacher, self.last_week) and can_write(self.teacher, self.last_week))
        late = self.last_week.deadline + timedelta(hours=1)
        draft, first = write(self.teacher, self.last_week, valid_payload(TEXT), 0, uuid.uuid4(), late)
        draft, second = write(self.teacher, self.last_week, valid_payload(TEXT + '补充'), draft.revision, uuid.uuid4(), late)
        self.assertEqual((first.version, second.version), (1, 2))  # 老师截止后也能改，每次都留版本
        self.assertEqual(voluntary_rows(self.last_week)[0]['state'], '已提交')  # 没有“逾期”
        draft, _ = write(self.student, self.last_week, valid_payload(TEXT), 0, uuid.uuid4(), self.last_week.deadline - timedelta(hours=1))
        with self.assertRaises(RuleError):
            write(self.student, self.last_week, valid_payload(TEXT), draft.revision, uuid.uuid4(), late)

    def test_former_student_week_stays_counted_and_locked(self):
        """以前当学生时交过的周：应交记录和统计都不变，不算自愿周报。"""
        from reports import accounts
        before_deadline = self.last_week.deadline - timedelta(hours=1)
        draft, _ = write(self.student, self.last_week, valid_payload(TEXT), 0, uuid.uuid4(), before_deadline)
        user, _ = accounts.set_identity(self.root, self.student, User.TEACHER)
        self.assertFalse(is_voluntary(user, self.last_week))
        self.assertEqual(voluntary_rows(self.last_week), [])
        self.assertIn(user, [r['user'] for r in rows(self.last_week)])
        with self.assertRaises(RuleError):
            write(user, self.last_week, valid_payload(TEXT), draft.revision, uuid.uuid4(), self.last_week.deadline + timedelta(hours=1))

    def test_instruction_feedback_and_return_on_teacher_report(self):
        self.submit_by_page(self.teacher)
        self.client.force_login(self.root)
        page = self.client.get(f'/people/{self.teacher.pk}/')
        self.assertContains(page, '<summary>写批示</summary>', html=False)
        self.assertContains(page, '本周 已交')
        self.client.post('/instructions/new/', {'target': 'report', 'member': self.teacher.pk, 'period': self.week.pk, 'text': '虚构批示：老师这周的安排很清楚', 'next': '/'})
        instruction = Instruction.objects.get(member=self.teacher)
        self.assertTrue(InstructionReceipt.objects.filter(instruction=instruction, user=self.teacher).exists())
        self.client.force_login(self.teacher)
        self.assertContains(self.client.get('/'), '去反馈')
        self.assertContains(self.client.get('/my/'), '虚构批示：老师这周的安排很清楚')
        self.client.post(f'/instructions/{instruction.pk}/feedback/', {'status': 'done', 'note': '虚构反馈', 'next': '/my/'})
        self.assertEqual(InstructionReceipt.objects.get(instruction=instruction, user=self.teacher).status, 'done')
        self.client.force_login(self.root)
        self.assertContains(self.client.get('/'), '虚构反馈')
        self.client.post(f'/manage/weeks/{self.week.pk}/members/{self.teacher.pk}/return/', {'reason': '虚构退回：请补充', 'next': '/'})
        report = Report.objects.get(user=self.teacher, period=self.week)
        self.assertTrue(report.returned)
        self.client.force_login(self.teacher)
        self.assertContains(self.client.get('/my/'), '虚构退回：请补充')
        self.assertEqual(self.submit_by_page(self.teacher).status_code, 200)
        report.refresh_from_db()
        self.assertFalse(report.returned)
        self.assertEqual(Submission.objects.filter(report=report).count(), 2)  # 原版本保留

    def test_portal_export_and_statistics(self):
        self.submit_by_page(self.teacher)
        self.submit_by_page(self.student)
        self.client.force_login(self.root)
        portal = self.client.get(f'/manage/weeks/{self.week.pk}/')
        self.assertContains(portal, '老师周报（选填')
        self.assertEqual([r['user'] for r in portal.context['teacher_rows']], [self.teacher])
        self.assertEqual((portal.context['required'], portal.context['submitted']), (2, 1))  # 学生 + B 组组长，不含老师
        self.client.post(f'/manage/weeks/{self.week.pk}/exports/', {'users': [self.teacher.pk, self.student.pk], 'scope': 'all'})
        self.assertEqual([m['name'] for m in ExportJob.objects.get().manifest['members']], ['虚构王', '虚构甲'])  # 合订可含老师
        response = self.client.post(f'/manage/weeks/{self.week.pk}/statistics.xlsx', {'users': [self.teacher.pk, self.student.pk], 'types': ['status', 'counts'], 'scope': 'all'})
        names = [str(cell.value) for sheet in load_workbook(BytesIO(response.content)).worksheets for row in sheet.iter_rows() for cell in row]
        self.assertIn('虚构甲', names)
        self.assertNotIn('虚构王', names)  # 统计表不含老师

    def test_visibility_stays_within_groups(self):
        self.submit_by_page(self.teacher)
        self.client.force_login(self.lead_b)
        self.assertEqual(self.client.get(f'/people/{self.teacher.pk}/').status_code, 403)
        portal = self.client.get(f'/manage/weeks/{self.week.pk}/')
        self.assertEqual(portal.context['teacher_rows'], [])
        self.client.force_login(self.student)  # 组员看不到老师的周报
        self.assertEqual(self.client.get(f'/people/{self.teacher.pk}/').status_code, 403)

    def test_reading_order_includes_teachers_who_wrote(self):
        self.assertEqual([u for u, _ in dashboard.person_sequence(self.root, self.A.pk)], [self.student])
        self.submit_by_page(self.teacher)
        self.assertEqual([u for u, _ in dashboard.person_sequence(self.root, self.A.pk)], [self.teacher, self.student])

    def test_draft_only_week_has_no_broken_view_link(self):
        write(self.teacher, self.week, valid_payload(TEXT), 0)
        page = self.get_home_for_teacher()
        self.assertContains(page, '草稿')
        self.assertNotContains(page, f'?week={self.week.pk}#report')

    def get_home_for_teacher(self):
        self.client.force_login(self.teacher)
        return self.client.get('/my/')


class TodayLineTests(TransactionTestCase):
    @staticmethod
    def task(pk, start, end):
        return SimpleNamespace(pk=pk, title=f'任务{pk}', start=start, end=end, progress=30, is_milestone=False)

    def test_line_moves_every_day_and_window_stays(self):
        task = self.task(1, date(2026, 11, 9), date(2026, 12, 21))
        days = [date(2026, 11, 16) + timedelta(days=n) for n in range(22)]
        charts = [gantt.build([task], day) for day in days]
        self.assertEqual({c.start for c in charts}, {gantt.week_of(task.start)})  # 窗口起点不跟着今天滑动
        self.assertEqual({c.rows[0].col for c in charts}, {1})  # 任务条位置不变
        offsets = [c.today_offset for c in charts]
        self.assertEqual(offsets, sorted(set(offsets)))  # 红线每天都往右走
        self.assertAlmostEqual(offsets[7] - offsets[0], 1, places=2)  # 一周走一格

    def test_future_only_and_long_timelines(self):
        future = gantt.build([self.task(1, date(2027, 1, 4), date(2027, 2, 1))], date(2026, 11, 23))
        self.assertGreaterEqual(future.today_offset, 0)
        self.assertLess(future.today_offset, future.weeks)
        long_task = self.task(2, date(2024, 1, 1), date(2028, 12, 31))
        charts = [gantt.build([long_task], date(2026, 11, 23) + timedelta(days=n)) for n in range(0, 400)]
        self.assertTrue(all(c.weeks == 52 and 0 <= c.today_offset < 52 for c in charts))  # 今天始终在窗口里
        changes = sum(a.start != b.start for a, b in zip(charts, charts[1:]))
        self.assertLessEqual(changes, 3)  # 400 天里只整段翻页两三次（每半年一次），不是每周滑动

    def test_rendered_line_shows_today(self):
        chart = gantt.build([self.task(1, date(2026, 11, 9), date(2026, 12, 21))], date(2026, 11, 23))
        from django.template.loader import render_to_string
        for task_row in chart.rows:
            task_row.task.group = None
        html = render_to_string('_gantt.html', {'chart': chart})
        self.assertIn('data-date="2026-11-23"', html)
        self.assertIn('<span class="gantt-today-flag">11/23</span>', html)
        self.assertIn('title="今天 2026-11-23"', html)
