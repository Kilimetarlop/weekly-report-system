# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
"""首页看板、成员阅览、任务甘特图与批示验收。

固定数据：A 组 = 组长 A、甲、乙；B 组 = 乙、丙、组长 B；“未分组”一人；最高权限不交周报。
看板按真实的本周、上周计算，分组从 2026-10-03 起生效，覆盖这两周。
"""
import uuid
from datetime import date, timedelta
from types import SimpleNamespace
from django.core.exceptions import PermissionDenied
from django.test import TransactionTestCase
from django.utils import timezone
from reports import access, dashboard, gantt, planning
from reports.domain import RuleError
from reports.models import Group, GroupMembership as M, Instruction, InstructionReceipt, Report, Task, User
from reports.services import ensure_obligations, period_for, rows, write
from .test_domain import instant, valid_payload

PASSWORD = 'Test-only-Password-892!'
EARLY = date(2026, 10, 3)
TEXT = '本周完成了虚构实验流程的验证并记录了结果'


class DashboardTests(TransactionTestCase):
    def setUp(self):
        def make(key, name, order, **extra):
            return User.objects.create_user(f'{key}@example.invalid', PASSWORD, name=name, sort_order=order, created_at=instant(15), **extra)
        self.root = make('root', '虚构最高权限', 0, is_staff=True)
        self.lead_a = make('leada', '虚构A组长', 1)
        self.a = make('a', '虚构甲', 2)
        self.b = make('b', '虚构乙', 3)
        self.c = make('c', '虚构丙', 4)
        self.lead_b = make('leadb', '虚构B组长', 5)
        self.u = make('u', '虚构未分组', 6)
        self.A = Group.objects.create(name='A组', sort_order=1)
        self.B = Group.objects.create(name='B组', sort_order=2)
        for user, group, role in [(self.lead_a, self.A, M.LEADER), (self.a, self.A, M.MEMBER), (self.b, self.A, M.MEMBER), (self.b, self.B, M.MEMBER), (self.c, self.B, M.MEMBER), (self.lead_b, self.B, M.LEADER)]:
            M.objects.create(user=user, group=group, role=role, start=EARLY)
        self.week = period_for()
        self.last = period_for(timezone.now() - timedelta(days=7))
        ensure_obligations(self.week)
        ensure_obligations(self.last)
        self.today = timezone.localdate()

    def submit(self, user, period=None, text=TEXT):
        period = period or self.week
        now = timezone.now() if period == self.week else period.deadline - timedelta(hours=1)
        return write(user, period, valid_payload(text), 0, uuid.uuid4(), now)[1]

    def login(self, user):
        self.client.force_login(user)
        return self.client

    def task(self, title='数据采集', group=None, days=(-7, 14), **extra):
        return planning.create_group_task(self.lead_a if (group or self.A) == self.A else self.lead_b, group or self.A, title, self.today + timedelta(days=days[0]), self.today + timedelta(days=days[1]), **extra)

    def test_D01_super_dashboard_matches_week_management(self):
        self.submit(self.a)
        self.submit(self.b)
        self.submit(self.a, self.last)
        response = self.login(self.root).get('/')
        self.assertTemplateUsed(response, 'dashboard.html')
        now, prev = response.context['now'], response.context['prev']
        self.assertEqual(now, dashboard.summarize(rows(self.week)))
        self.assertEqual((now['submitted'], now['required'], now['missing']), (2, 6, 4))  # 最高权限不计入
        self.assertEqual((prev['submitted'], prev['required']), (1, 6))
        portal = self.client.get(f'/manage/weeks/{self.week.pk}/')
        self.assertEqual((portal.context['submitted'], portal.context['required']), (now['submitted'], now['required']))
        self.assertContains(response, '本周提交率')
        self.assertContains(response, '上周提交率')

    def test_D02_leader_dashboard_only_own_groups(self):
        self.submit(self.c)
        response = self.login(self.lead_a).get('/')
        self.assertEqual([card['group'] for card in response.context['cards']], [self.A])
        self.assertEqual(response.context['now']['required'], 3)
        self.assertContains(response, '小组概览')  # 2026-11-17 起组长首页只留一行“我的”提醒
        self.assertContains(response, 'class="personal-bar"')
        self.assertNotContains(response, '虚构丙')
        self.assertNotContains(response, '虚构未分组')

    def test_D03_group_card_current_tasks_and_next_milestone(self):
        self.task('数据采集', assignee_ids=[self.a.pk])
        finished = self.task('已经完成的任务')
        planning.update_progress(self.lead_a, finished, 100)
        self.task('下个月才开始', days=(30, 60))
        self.task('中期检查', days=(10, 10), is_milestone=True)
        self.task('开题', days=(-20, -20), is_milestone=True)
        card = next(c for c in dashboard.overview(self.root)['cards'] if c['group'] == self.A)
        self.assertEqual([t.title for t in card['current_tasks']], ['数据采集'])
        self.assertEqual(card['milestone'].title, '中期检查')
        self.assertEqual(card['leaders'], [self.lead_a])

    def test_D04_person_page_shows_groups_leaders_tasks(self):
        self.task('数据采集', assignee_ids=[self.b.pk])
        planning.create_personal_task(self.b, '文献综述', self.today, self.today + timedelta(days=7))
        response = self.login(self.root).get(f'/people/{self.b.pk}/')
        context = response.context
        self.assertEqual([g['group'] for g in context['groups']], [self.A, self.B])
        self.assertEqual([g['leaders'] for g in context['groups']], [[self.lead_a], [self.lead_b]])
        self.assertEqual({t.title for t in context['current_tasks']}, {'数据采集', '文献综述'})
        self.assertEqual(len(context['chart'].rows), 2)
        self.assertContains(response, 'A组</a>（组长 虚构A组长）')

    def test_D05_person_report_defaults_to_latest_submission(self):
        self.submit(self.b, self.last, '上周的正式内容' + '中' * 20)
        write(self.b, self.week, valid_payload('本周草稿内容不应该出现在阅览页'), 0, now=timezone.now())
        response = self.login(self.lead_a).get(f'/people/{self.b.pk}/')
        self.assertEqual(response.context['selected'], self.last)
        self.assertContains(response, '上周的正式内容')
        self.assertNotContains(response, '本周草稿内容')
        response = self.client.get(f'/people/{self.b.pk}/?week={self.week.pk}')
        self.assertEqual(response.context['selected'], self.week)
        self.assertIsNone(response.context['submission'])
        self.assertContains(response, '这一周还没有正式提交')
        self.assertEqual(response.context['older'], self.last)

    def test_D06_next_person_follows_groups_without_looping(self):
        sequence = dashboard.person_sequence(self.root)
        self.assertEqual([(u.name, g.name if g else None) for u, g in sequence], [
            ('虚构A组长', 'A组'), ('虚构甲', 'A组'), ('虚构乙', 'A组'), ('虚构乙', 'B组'), ('虚构丙', 'B组'), ('虚构B组长', 'B组'), ('虚构未分组', None)])
        client = self.login(self.root)
        context = client.get(f'/people/{self.b.pk}/?via={self.A.pk}').context
        self.assertEqual(context['next_url'], f'/people/{self.b.pk}/?via={self.B.pk}')  # 跨组成员：进入 B 组里的“乙”
        context = client.get(context['next_url']).context
        self.assertEqual(context['next_url'], f'/people/{self.c.pk}/?via={self.B.pk}')  # 不会退回 A 组
        context = client.get(f'/people/{self.b.pk}/?group={self.A.pk}').context
        self.assertEqual((context['sequence_total'], context['next_url']), (3, ''))  # 只在 A 组内翻，乙是最后一位
        self.assertEqual({u.pk for u, _ in dashboard.person_sequence(self.lead_a)}, {self.lead_a.pk, self.a.pk, self.b.pk})

    def test_D07_person_page_permissions(self):
        self.assertEqual(self.login(self.lead_a).get(f'/people/{self.c.pk}/').status_code, 403)
        self.assertEqual(self.login(self.a).get(f'/people/{self.b.pk}/').status_code, 403)
        self.assertEqual(self.login(self.a).get(f'/people/{self.a.pk}/').status_code, 200)
        self.assertEqual(self.login(self.root).get(f'/people/{self.root.pk}/').status_code, 403)

    def test_D08_leader_creates_group_task_for_assignee(self):
        client = self.login(self.lead_a)
        response = client.post('/tasks/new/', {'kind': 'group', 'group': self.A.pk, 'title': '模型训练', 'start': str(self.today), 'end': str(self.today + timedelta(days=21)), 'assignees': [self.a.pk], 'next': f'/groups/{self.A.pk}/'})
        self.assertRedirects(response, f'/groups/{self.A.pk}/', fetch_redirect_response=False)
        task = Task.objects.get(title='模型训练')
        self.assertIn(task, planning.tasks_of_person(self.a))
        self.assertContains(self.login(self.a).get('/'), '模型训练')
        self.assertEqual(self.login(self.lead_a).post('/tasks/new/', {'kind': 'group', 'group': self.B.pk, 'title': '越权', 'start': str(self.today), 'end': str(self.today)}).status_code, 403)
        with self.assertRaises(RuleError):
            planning.create_group_task(self.lead_a, self.A, '负责人不在本组', self.today, self.today, assignee_ids=[self.c.pk])

    def test_D09_assignee_updates_progress_only(self):
        task = self.task('数据采集', assignee_ids=[self.a.pk])
        client = self.login(self.a)
        self.assertEqual(client.post(f'/tasks/{task.pk}/progress/', {'progress': 40}).status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.progress, 40)
        self.assertEqual(client.post(f'/tasks/{task.pk}/edit/', {'title': '改名', 'start': str(self.today), 'end': str(self.today)}).status_code, 403)
        client.post(f'/tasks/{task.pk}/progress/', {'progress': 150})
        task.refresh_from_db()
        self.assertEqual(task.progress, 40)
        self.assertEqual(self.login(self.b).post(f'/tasks/{task.pk}/progress/', {'progress': 90}).status_code, 403)

    def test_D10_personal_task_visibility(self):
        task = planning.create_personal_task(self.a, '文献综述', self.today, self.today + timedelta(days=14))
        for user, expected in [(self.a, True), (self.lead_a, True), (self.root, True), (self.b, False), (self.lead_b, False)]:
            self.assertEqual(access.can_view_task(user, task), expected, user.name)
        self.assertNotContains(self.login(self.b).get('/'), '文献综述')
        response = self.login(self.lead_a).post(f'/tasks/{task.pk}/edit/', {'title': '文献综述（修订）', 'start': str(self.today), 'end': str(self.today + timedelta(days=21)), 'progress': 30})
        self.assertEqual(response.status_code, 302)
        task.refresh_from_db()
        self.assertEqual((task.title, task.progress), ('文献综述（修订）', 30))
        with self.assertRaises(PermissionDenied):
            planning.create_personal_task(self.root, '最高权限不建个人任务', self.today)

    def test_D11_milestone(self):
        milestone = self.task('中期检查', days=(10, 10), is_milestone=True)
        self.assertEqual(milestone.start, milestone.end)
        row = gantt.build([milestone], self.today).rows[0]
        self.assertTrue(row.visible)
        self.assertEqual(row.col, (gantt.week_of(milestone.start) - gantt.build([milestone], self.today).start).days // 7 + 1)
        self.assertContains(self.login(self.lead_a).get(f'/groups/{self.A.pk}/'), 'gantt-milestone')
        with self.assertRaises(PermissionDenied):
            planning.update_progress(self.lead_a, milestone, 50)

    def test_D12_gantt_weekly_layout(self):
        today = date(2026, 11, 16)
        task = lambda pk, start, end, progress: SimpleNamespace(pk=pk, title=f'任务{pk}', start=start, end=end, progress=progress, is_milestone=False)
        chart = gantt.build([task(1, date(2026, 10, 1), date(2026, 11, 5), 50), task(2, date(2026, 10, 5), date(2026, 11, 1), 100), task(3, date(2026, 11, 20), date(2026, 12, 31), 0)], today)
        self.assertEqual(chart.start.weekday(), 5)  # 每列从周六开始
        self.assertEqual(chart.start, gantt.week_of(date(2026, 10, 1)))
        self.assertEqual(sum(span for _, span, _ in chart.months), chart.weeks)
        self.assertTrue(all(label == '' if span == 1 else span >= 3 or '年' not in label for label, span, _ in chart.months))  # 一周宽不写字，两周宽不带年份
        self.assertAlmostEqual(chart.today_offset, (today - chart.start).days / 7, places=3)
        row = {r.task.pk: r for r in chart.rows}
        self.assertTrue(row[1].late)
        self.assertTrue(row[2].done and not row[2].late)
        self.assertEqual(row[3].col, (gantt.week_of(date(2026, 11, 20)) - chart.start).days // 7 + 1)
        self.assertEqual(row[3].span, (gantt.week_of(date(2026, 12, 31)) - gantt.week_of(date(2026, 11, 20))).days // 7 + 1)
        long = gantt.build([task(9, date(2024, 1, 1), date(2027, 12, 31), 10)], today)
        self.assertEqual(long.weeks, 52)
        self.assertTrue(long.rows[0].clipped)
        overdue = self.task('已经延期', days=(-30, -3))
        self.assertContains(self.login(self.lead_a).get(f'/groups/{self.A.pk}/'), 'gantt-bar late')
        self.assertTrue(overdue)

    def test_D13_instructions_reach_the_right_people(self):
        task = self.task('数据采集', assignee_ids=[self.a.pk])
        self.submit(self.b)
        client = self.login(self.root)
        client.post('/instructions/new/', {'target': 'report', 'member': self.b.pk, 'period': self.week.pk, 'text': '周报写得很清楚', 'next': '/'})
        client.post('/instructions/new/', {'target': 'task', 'task': task.pk, 'text': '数据采集要提前', 'next': '/'})
        client.post('/instructions/new/', {'target': 'group', 'group': self.B.pk, 'text': 'B组本月加快', 'next': '/'})
        recipients = lambda text: set(InstructionReceipt.objects.filter(instruction__text=text).values_list('user__name', flat=True))
        self.assertEqual(recipients('周报写得很清楚'), {'虚构乙', '虚构A组长', '虚构B组长'})
        self.assertEqual(recipients('数据采集要提前'), {'虚构甲', '虚构A组长'})
        self.assertEqual(recipients('B组本月加快'), {'虚构乙', '虚构丙', '虚构B组长'})
        page = self.login(self.b).get('/')
        self.assertContains(page, '周报写得很清楚')
        self.assertContains(page, 'B组本月加快')
        leader = self.login(self.lead_a)
        self.assertContains(leader.get('/'), '去反馈')  # 2026-11-17 起：小组概览只提醒，批示在“我的周报”里
        self.assertContains(leader.get('/my/'), '数据采集要提前')

    def test_D14_only_super_writes_instructions(self):
        for user in (self.lead_a, self.a):
            self.assertEqual(self.login(user).post('/instructions/new/', {'target': 'group', 'group': self.A.pk, 'text': '越权'}).status_code, 403)
        self.assertFalse(Instruction.objects.exists())

    def test_D15_read_and_withdraw(self):
        instruction = planning.create_instruction(self.root, Instruction.GROUP, '全组注意进度', group=self.A)
        client = self.login(self.a)
        self.assertEqual([i.pk for i in client.get('/').context['unread']], [instruction.pk])
        client.post(f'/instructions/{instruction.pk}/read/', {'next': '/'})  # “知道了”= 反馈“已知悉”
        page = client.get('/')
        self.assertEqual(page.context['unread'], [])  # 不再在“待反馈”里
        self.assertEqual([i.pk for i in page.context['recent']], [instruction.pk])  # 移到“最近已反馈”，可修改
        item = planning.with_read_counts([instruction])[0]
        self.assertEqual((item.read_count, item.responded_count, item.total_count), (1, 1, 3))
        self.assertContains(self.login(self.root).get(f'/groups/{self.A.pk}/'), '反馈 1/3')
        self.client.post(f'/instructions/{instruction.pk}/withdraw/', {'next': '/'})
        self.assertNotContains(self.login(self.b).get('/'), '全组注意进度')

    def test_D16_recipients_fixed_when_written(self):
        instruction = planning.create_instruction(self.root, Instruction.GROUP, '分组变动前的批示', group=self.A)
        M.objects.filter(user=self.a, group=self.A).update(end=access.current_start())
        M.objects.create(user=self.c, group=self.A, role=M.MEMBER, start=EARLY)
        self.assertEqual(set(instruction.receipts.values_list('user__name', flat=True)), {'虚构A组长', '虚构甲', '虚构乙'})
        self.assertContains(self.login(self.a).get('/'), '分组变动前的批示')
        self.assertNotContains(self.login(self.c).get('/'), '分组变动前的批示')

    def test_D17_member_home(self):
        planning.create_personal_task(self.a, '我的个人任务', self.today, self.today + timedelta(days=14))
        self.task('组任务甲', assignee_ids=[self.lead_a.pk])
        planning.create_personal_task(self.b, '乙的个人任务', self.today, self.today + timedelta(days=14))
        page = self.login(self.a).get('/')
        self.assertTemplateUsed(page, 'my_home.html')
        for text in ['本周周报', '待反馈的批示', '我的任务', '我的个人任务', 'A组 · 小组任务', '组任务甲', '我的历史周报']:
            self.assertContains(page, text)
        self.assertNotContains(page, '乙的个人任务')
        self.assertNotContains(page, '后台')

    def test_D18_return_from_person_page(self):
        self.submit(self.a)
        client = self.login(self.lead_a)
        url = f'/people/{self.a.pk}/?group={self.A.pk}'
        self.assertTrue(client.get(url).context['can_return'])
        response = client.post(f'/manage/weeks/{self.week.pk}/members/{self.a.pk}/return/', {'reason': '请补充实验细节', 'next': url})
        self.assertRedirects(response, url, fetch_redirect_response=False)
        self.assertTrue(Report.objects.get(user=self.a, period=self.week).returned)
        self.assertContains(self.login(self.a).get('/'), '请补充实验细节')

    def test_pages_render_for_every_role(self):
        self.task('数据采集', assignee_ids=[self.a.pk])
        for user, urls in [
            (self.root, ['/', f'/people/{self.a.pk}/', f'/groups/{self.A.pk}/', '/manage/members/', '/manage/groups/']),
            (self.lead_a, ['/', '/my/', '/my/tasks/', f'/people/{self.b.pk}/', f'/groups/{self.A.pk}/']),
            (self.a, ['/', '/my/tasks/', f'/groups/{self.A.pk}/', f'/people/{self.a.pk}/']),
            (self.u, ['/', '/my/tasks/']),
        ]:
            client = self.login(user)
            for url in urls:
                self.assertEqual(client.get(url).status_code, 200, (user.name, url))
        self.assertEqual(self.login(self.c).get(f'/groups/{self.A.pk}/').status_code, 403)

    def test_next_parameter_cannot_leave_the_site(self):
        response = self.login(self.a).post('/tasks/new/', {'kind': 'personal', 'title': '外站跳转测试', 'start': str(self.today), 'end': str(self.today), 'next': 'https://evil.example.invalid/'})
        self.assertEqual(response.url, '/')
