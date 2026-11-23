# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
"""分组与分级权限验收。

固定数据：A 组 = 组长 A、甲、乙；B 组 = 乙、丙、组长 B；“未分组”与最高权限不在任何组。
分组记录从 2026-10-03 起生效且未结束，因此覆盖被测周期（2026-10-10 至 10-16）和真实的当前周期。
组长身份按真实当前周期判断（卸任即失效），被查看的内容按被查看周期的分组判断。
"""
import uuid
from datetime import date, timedelta
from io import BytesIO
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.test import TransactionTestCase, override_settings
from openpyxl import load_workbook
from reports import access, grouping
from reports.domain import RuleError
from reports.models import Audit, Group, GroupMembership as M, Mail, Obligation, Report, Submission, User
from reports.services import create_export, ensure_obligations, period_for, queue_reminders, reminder_candidates, return_report, rows, set_exemption, write
from reports.statistics import workbook_snapshot
from .test_domain import instant, valid_payload

PASSWORD = 'Test-only-Password-892!'
EARLY = date(2026, 10, 3)


class GroupTests(TransactionTestCase):
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
        self.period = period_for(instant())
        ensure_obligations(self.period)
        self.scope_a = access.resolve_scope(self.lead_a, f'group:{self.A.pk}')
        self.scope_b = access.resolve_scope(self.lead_b, f'group:{self.B.pk}')

    def submit(self, user, text=None, period=None, now=None):
        return write(user, period or self.period, valid_payload(text), 0, uuid.uuid4(), now or instant())[1]

    def users(self, scope=None, period=None):
        return [r['user'] for r in rows(period or self.period, scope)]

    def row(self, user, scope=None):
        return next(r for r in rows(self.period, scope) if r['user'] == user)

    def login(self, user):
        self.client.force_login(user)
        return self.client

    def test_G01_member_has_no_manage_access(self):
        client = self.login(self.a)
        p = self.period.pk
        for url in ['/manage/', f'/manage/weeks/{p}/', '/manage/groups/', '/manage/members/', '/manage/settings/']:
            self.assertEqual(client.get(url).status_code, 403, url)
        for url in [f'/manage/weeks/{p}/members/{self.b.pk}/exemption/', f'/manage/weeks/{p}/members/{self.b.pk}/return/', f'/manage/weeks/{p}/exports/', f'/manage/weeks/{p}/statistics.xlsx']:
            self.assertEqual(client.post(url, {'reason': 'x', 'exempt': '1', 'users': [self.b.pk], 'types': ['status']}).status_code, 403, url)
        self.assertNotContains(client.get('/'), '后台')

    def test_G02_leader_cannot_reach_outside_scope(self):
        outside, inside = self.submit(self.c), self.submit(self.a)
        client = self.login(self.lead_a)
        p = self.period.pk
        self.assertEqual(client.get(f'/submissions/{outside.pk}/').status_code, 403)
        self.assertEqual(client.get(f'/submissions/{inside.pk}/').status_code, 200)
        self.assertEqual(client.post(f'/manage/weeks/{p}/members/{self.c.pk}/return/', {'reason': '越权'}).status_code, 403)
        self.assertEqual(client.post(f'/manage/weeks/{p}/members/{self.c.pk}/exemption/', {'group': self.B.pk, 'exempt': '1'}).status_code, 403)
        for scope in [f'group:{self.B.pk}', 'all', 'ungrouped', 'led']:
            self.assertEqual(client.get(f'/manage/weeks/{p}/?scope={scope}').status_code, 403, scope)
        self.assertFalse(Report.objects.get(user=self.c).returned)

    def test_G03_cross_group_member_visible_to_both_counted_once(self):
        self.assertEqual(self.users(self.scope_a), [self.lead_a, self.a, self.b])
        self.assertEqual(self.users(self.scope_b), [self.b, self.c, self.lead_b])
        everyone = self.users()
        self.assertEqual(everyone.count(self.b), 1)
        self.assertEqual(len(everyone), 6)  # 最高权限不交周报，不在名单中
        response = self.login(self.lead_b).get(f'/manage/weeks/{self.period.pk}/')
        self.assertEqual(response.context['required'], 3)
        self.assertContains(response, 'A组、B组')

    def test_G04_leaving_a_group_limits_history(self):
        later = period_for(instant() + timedelta(days=7))
        M.objects.filter(user=self.a, group=self.A).update(end=later.start)
        before, after = self.submit(self.a), self.submit(self.a, period=later, now=instant(23))
        client = self.login(self.lead_a)
        self.assertEqual(client.get(f'/submissions/{before.pk}/').status_code, 200)
        self.assertEqual(client.get(f'/submissions/{after.pk}/').status_code, 403)
        self.assertNotIn(self.a, self.users(self.scope_a, later))
        self.assertIn(self.a, self.users(access.UNGROUPED, later))

    def test_G05_new_leader_sees_pre_tenure_history_until_stepping_down(self):
        old = self.submit(self.a)
        new = User.objects.create_user('newlead@example.invalid', PASSWORD, name='虚构新组长', sort_order=9, created_at=instant(15))
        record = grouping.add_membership(self.root, self.A, new, M.LEADER, access.current_start() - timedelta(days=7))
        client = self.login(new)
        self.assertEqual(client.get(f'/submissions/{old.pk}/').status_code, 200)
        grouping.end_membership(self.root, record, access.current_start())
        self.assertEqual(client.get(f'/submissions/{old.pk}/').status_code, 403)
        self.assertEqual(client.get(f'/manage/weeks/{self.period.pk}/').status_code, 403)

    def test_G06_leader_counts_as_member(self):
        self.assertIn(self.lead_a, self.users(self.scope_a))
        self.submit(self.lead_a)
        self.submit(self.a)
        job = create_export(self.lead_a, self.period, [self.a.pk, self.lead_a.pk], self.scope_a)
        self.assertEqual([m['user_id'] for m in job.manifest['members']], [self.lead_a.pk, self.a.pk])

    def test_G07_group_exemption_only_affects_that_group(self):
        set_exemption(self.lead_a, self.period, self.b, self.A.pk, True, '只免 A 组')
        self.assertTrue(self.row(self.b, self.scope_a)['exempt'])
        self.assertFalse(self.row(self.b, self.scope_b)['exempt'])
        self.assertNotIn(self.b, reminder_candidates(self.period, self.scope_a))
        self.assertIn(self.b, reminder_candidates(self.period, self.scope_b))
        self.assertFalse(Obligation.objects.get(user=self.b, period=self.period).exempt)
        self.assertEqual(self.row(self.b, self.scope_b)['exempt_notes'], [])
        self.assertIn('只免 A 组', self.row(self.b, self.scope_a)['exempt_notes'][0])

    def test_G08_overall_exempt_only_when_every_group_exempts(self):
        set_exemption(self.lead_a, self.period, self.b, self.A.pk, True, '')
        self.assertFalse(self.row(self.b)['exempt'])
        set_exemption(self.lead_b, self.period, self.b, self.B.pk, True, '')
        self.assertTrue(self.row(self.b)['exempt'])
        self.assertEqual(self.row(self.b)['state'], '免交')
        set_exemption(self.lead_b, self.period, self.b, self.B.pk, False, '')
        self.assertFalse(self.row(self.b)['exempt'])

    def test_G09_exempt_member_can_still_submit(self):
        set_exemption(self.root, self.period, self.u, None, True, '出差')
        set_exemption(self.lead_a, self.period, self.a, self.A.pk, True, '')
        for user in (self.u, self.a):
            self.submit(user)
            row = self.row(user)
            self.assertTrue(row['exempt'])
            self.assertTrue(row['state'].startswith('已提交'))
            self.assertIn('免交', row['state'])
        response = self.login(self.root).get(f'/manage/weeks/{self.period.pk}/')
        self.assertEqual((response.context['required'], response.context['submitted']), (4, 0))

    def test_G10_leader_cannot_touch_global_exemption(self):
        set_exemption(self.root, self.period, self.a, None, True, '请假')
        with self.assertRaises(PermissionDenied):
            set_exemption(self.lead_a, self.period, self.a, None, False, '')
        response = self.login(self.lead_a).post(f'/manage/weeks/{self.period.pk}/members/{self.a.pk}/exemption/', {'exempt': '0'})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Obligation.objects.get(user=self.a, period=self.period).exempt)
        self.assertTrue(self.row(self.a, self.scope_a)['exempt'])

    @override_settings(EMAIL_ENABLED=True)  # 邮件默认关闭；保留代码路径的覆盖，便于以后重新开启
    def test_G11_return_by_one_leader_visible_to_the_other(self):
        self.submit(self.b)
        return_report(self.lead_a, self.period, self.b.pk, '请补充 A 组实验')
        self.assertIn('待修改', self.row(self.b, self.scope_b)['state'])
        self.assertIn('退回人：虚构A组长', Mail.objects.get(kind='return').body)
        with self.assertRaises(RuleError):
            return_report(self.lead_b, self.period, self.b.pk, '再次退回')
        response = self.login(self.lead_b).get(f'/manage/weeks/{self.period.pk}/')
        self.assertContains(response, '退回人 虚构A组长：请补充 A 组实验')

    def test_G12_leader_can_act_on_self_with_audit(self):
        set_exemption(self.lead_a, self.period, self.lead_a, self.A.pk, True, '自己免交')
        self.submit(self.lead_a)
        return_report(self.lead_a, self.period, self.lead_a.pk, '自己退回')
        actions = set(Audit.objects.filter(operator=self.lead_a).values_list('action', flat=True))
        self.assertTrue({'group_exemption', 'return'} <= actions)

    @override_settings(EMAIL_ENABLED=True)  # 邮件默认关闭；保留代码路径的覆盖，便于以后重新开启
    def test_G13_recent_reminder_flagged_and_unchecked(self):
        queue_reminders(self.lead_b, self.period, [self.b.pk, self.c.pk], str(uuid.uuid4()), '被忽略的主题', '被忽略的正文', self.scope_b)
        mail = Mail.objects.get(user=self.b)
        self.assertEqual((mail.operator, mail.scope), (self.lead_b, f'group:{self.B.pk}'))
        self.assertIn('周报提交提醒', mail.subject)  # 组长只能用系统统一模板
        response = self.login(self.lead_a).get(f'/manage/weeks/{self.period.pk}/reminders/')
        entries = {e['user']: e for e in response.context['entries']}
        self.assertFalse(entries[self.b]['checked'])
        self.assertTrue(entries[self.a]['checked'])
        self.assertContains(response, '被“B组”催过')
        self.assertFalse(response.context['can_edit'])

    @override_settings(EMAIL_ENABLED=True)  # 邮件默认关闭；保留代码路径的覆盖，便于以后重新开启
    def test_G14_worker_skips_targets_no_longer_in_scope(self):
        queue_reminders(self.lead_a, self.period, [self.a.pk, self.b.pk], str(uuid.uuid4()), '', '', self.scope_a)
        M.objects.filter(user=self.a, group=self.A).update(start=self.period.start + timedelta(days=7))
        set_exemption(self.lead_a, self.period, self.b, self.A.pk, True, '')
        for _ in range(2):
            call_command('worker', once=True, verbosity=0)
        self.assertEqual(set(Mail.objects.values_list('state', flat=True)), {'skipped'})

    @override_settings(EMAIL_ENABLED=True)  # 邮件默认关闭；保留代码路径的覆盖，便于以后重新开启
    def test_G14_worker_skips_when_operator_stepped_down(self):
        queue_reminders(self.lead_a, self.period, [self.a.pk], str(uuid.uuid4()), '', '', self.scope_a)
        M.objects.filter(user=self.lead_a, group=self.A).update(end=access.current_start())
        call_command('worker', once=True, verbosity=0)
        self.assertEqual(Mail.objects.get().state, 'skipped')

    def test_G15_union_export_dedupes_and_keeps_global_order(self):
        M.objects.create(user=self.lead_a, group=self.B, role=M.LEADER, start=EARLY)
        scope = access.resolve_scope(self.lead_a, 'led')
        self.assertEqual(set(scope.group_ids), {self.A.pk, self.B.pk})
        for user in (self.c, self.b, self.a, self.u):
            self.submit(user)
        job = create_export(self.lead_a, self.period, [self.c.pk, self.b.pk, self.a.pk, self.u.pk, self.lead_b.pk], scope)
        self.assertEqual([m['user_id'] for m in job.manifest['members']], [self.a.pk, self.b.pk, self.c.pk])
        self.assertEqual([s['name'] for s in job.manifest['skipped']], ['虚构B组长'])
        self.assertEqual(job.manifest['members'][1]['groups'], ['A组', 'B组'])
        self.assertEqual(job.manifest['scope']['key'], 'led')

    def test_G16_export_snapshot_and_access_survive_regrouping(self):
        self.submit(self.a)
        job = create_export(self.lead_a, self.period, [self.a.pk], self.scope_a)
        M.objects.filter(user=self.a, group=self.A).delete()
        job.refresh_from_db()
        self.assertEqual(job.manifest['members'][0]['groups'], ['A组'])
        url = f'/manage/export-jobs/{job.pk}/'
        self.assertEqual(self.login(self.lead_a).get(url).status_code, 200)
        self.assertEqual(self.login(self.lead_b).get(url).status_code, 403)
        self.assertEqual(self.login(self.root).get(url).status_code, 200)
        M.objects.filter(user=self.lead_a, group=self.A).update(end=access.current_start())
        self.assertEqual(self.login(self.lead_a).get(url).status_code, 403)

    def test_G17_ungrouped_only_visible_to_super(self):
        self.assertEqual(self.users(access.UNGROUPED), [self.u])
        for scope in (self.scope_a, self.scope_b):
            self.assertNotIn(self.u, self.users(scope))
        submission = self.submit(self.u)
        self.assertEqual(self.login(self.lead_a).get(f'/submissions/{submission.pk}/').status_code, 403)
        self.assertEqual(self.login(self.root).get(f'/submissions/{submission.pk}/').status_code, 200)
        self.assertEqual(self.client.get(f'/manage/weeks/{self.period.pk}/?scope=ungrouped').status_code, 200)

    def test_G18_leader_cannot_change_groups_or_accounts(self):
        client = self.login(self.lead_a)
        self.assertEqual(client.post('/manage/groups/', {'action': 'add', 'group': self.A.pk, 'user': self.c.pk, 'role': 'member', 'start': '2026-10-10'}).status_code, 403)
        self.assertEqual(client.post('/manage/members/', {'action': 'create', 'name': '越权', 'account': 'x', 'password': 'Weekly-Report-2026!'}).status_code, 403)
        self.assertEqual(client.get('/manage/settings/').status_code, 403)
        self.assertFalse(M.objects.filter(user=self.c, group=self.A).exists())
        with self.assertRaises(PermissionDenied):
            grouping.create_group(self.lead_a, 'C组')

    def test_G19_backdated_membership_is_audited_and_content_untouched(self):
        submission = self.submit(self.c)
        response = self.login(self.root).post('/manage/groups/', {'action': 'add', 'group': self.A.pk, 'user': self.c.pk, 'role': 'member', 'start': '2026-10-01'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(M.objects.get(user=self.c, group=self.A).start, date(2026, 9, 26))  # 归一到所在周的周六
        self.assertTrue(Audit.objects.filter(action='membership_add', operator=self.root).exists())
        self.assertEqual(self.login(self.lead_a).get(f'/submissions/{submission.pk}/').status_code, 200)
        self.assertEqual(Submission.objects.get(pk=submission.pk).digest, submission.digest)

    def test_G20_overlapping_membership_rejected(self):
        with self.assertRaises(RuleError):
            grouping.add_membership(self.root, self.A, self.a, M.LEADER, '2026-11-01')
        record = M.objects.get(user=self.a, group=self.A)
        with self.assertRaises(RuleError):
            grouping.end_membership(self.root, record, EARLY)
        grouping.end_membership(self.root, record, '2026-10-24')
        grouping.add_membership(self.root, self.A, self.a, M.LEADER, '2026-10-24')  # 首尾相接不算重叠
        grouping.change_role(self.root, M.objects.get(user=self.a, group=self.A, role=M.LEADER), '2026-10-31')
        history = list(M.objects.filter(user=self.a, group=self.A).order_by('start').values_list('role', 'start', 'end'))
        self.assertEqual(history, [(M.MEMBER, EARLY, date(2026, 10, 24)), (M.LEADER, date(2026, 10, 24), date(2026, 10, 31)), (M.MEMBER, date(2026, 10, 31), None)])

    def test_G21_stopped_leader_loses_access(self):
        submission = self.submit(self.a)
        User.objects.filter(pk=self.lead_a.pk).update(stop_period=self.period)
        self.lead_a.refresh_from_db()
        self.assertEqual(access.led_group_ids(self.lead_a), set())
        self.assertEqual(self.login(self.lead_a).get(f'/submissions/{submission.pk}/').status_code, 302)

    def test_G22_leader_sees_draft_flag_not_text(self):
        write(self.a, self.period, valid_payload('这是一段只在草稿里的秘密文字内容'), 0, now=instant())
        response = self.login(self.lead_a).get(f'/manage/weeks/{self.period.pk}/')
        self.assertContains(response, '有草稿')
        self.assertNotContains(response, '秘密文字')

    def test_G23_xlsx_follows_scope(self):
        set_exemption(self.lead_a, self.period, self.b, self.A.pk, True, '')
        blob = workbook_snapshot(self.period, [self.b.pk, self.c.pk], ['status'], ['s0', 's1'], self.scope_a)
        ws = load_workbook(BytesIO(blob)).active
        self.assertEqual([c.value for c in ws[2]], ['姓名', '账号', '状态', '所属小组', '是否应交', '是否免交'])
        self.assertEqual(ws.max_row, 3)  # 丙不在 A 组范围内
        self.assertEqual([c.value for c in ws[3]][3:], ['A组、B组', False, True])
        response = self.login(self.lead_a).post(f'/manage/weeks/{self.period.pk}/statistics.xlsx', {'users': [self.c.pk, self.a.pk], 'types': ['status'], 'scope': f'group:{self.A.pk}'})
        self.assertEqual(response.status_code, 200)

    def test_G24_without_groups_behaviour_matches_before(self):
        M.objects.all().delete()
        self.assertEqual(access.led_group_ids(self.lead_a), set())
        self.assertEqual(self.login(self.lead_a).get(f'/manage/weeks/{self.period.pk}/').status_code, 403)
        self.assertEqual(len(self.users(access.UNGROUPED)), 6)
        self.assertEqual(access.scopes_for(self.root)[0], access.ALL)

    def test_archive_requires_ended_memberships(self):
        with self.assertRaises(RuleError):
            grouping.set_archived(self.root, self.B, True)
        M.objects.filter(group=self.B).update(end=access.current_start())
        grouping.set_archived(self.root, self.B, True)
        self.assertEqual(access.led_group_ids(self.lead_b), set())
        self.assertIn(self.c, self.users(access.resolve_scope(self.root, f'group:{self.B.pk}')))

    def test_pages_render_with_groups(self):
        client = self.login(self.root)
        self.assertContains(client.get('/manage/groups/'), '虚构A组长')
        self.assertEqual(client.get(f'/manage/weeks/{self.period.pk}/?scope=group:{self.B.pk}').status_code, 200)
        self.assertContains(client.get('/manage/members/'), 'A组、B组')
        response = self.login(self.b).get('/')
        self.assertContains(response, 'A组（组长：虚构A组长）')
        self.assertContains(response, 'B组（组长：虚构B组长）')
        client = self.login(self.lead_a)
        self.assertContains(client.get('/'), 'class="personal-bar"')  # 组长首页：小组概览 + 一行“我的”提醒
        self.assertEqual(client.get('/manage/').status_code, 302)
        self.assertEqual(client.get(f'/manage/weeks/{self.period.pk}/reminders/').status_code, 404)  # 邮件关闭时催交入口不存在
        M.objects.create(user=self.lead_a, group=self.B, role=M.LEADER, start=EARLY)
        response = client.get(f'/manage/weeks/{self.period.pk}/?scope=led')
        self.assertEqual(response.context['required'], 5)
        self.assertContains(response, '我负责的全部小组')
        self.assertNotContains(response, 'B组免交（保留已交内容）')  # 默认查看模式：不显示免交按钮
        client.post('/mode/', {'mode': 'edit'})
        self.assertContains(client.get(f'/manage/weeks/{self.period.pk}/?scope=led'), 'B组免交（保留已交内容）')
