# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
"""2026-11-16 追加需求验收：周报“要点 + 详细阐述”、批示反馈、先小组后成员。全部为虚构数据。"""
import tempfile
import uuid
from datetime import date, timedelta
from pathlib import Path
from django.core.exceptions import PermissionDenied
from django.test import TransactionTestCase
from django.utils import timezone
from docx import Document
from reports import planning
from reports.documents import build_docx
from reports.domain import RuleError
from reports.models import Draft, Group, GroupMembership as M, Instruction, InstructionReceipt, Report, Submission, User
from reports.services import create_export, ensure_obligations, period_for, write
from .test_domain import instant, item, valid_payload

PASSWORD = 'Test-only-Password-892!'
EARLY = date(2026, 10, 3)


class FeedbackAndPointsTests(TransactionTestCase):
    def setUp(self):
        def make(key, name, order, **extra):
            return User.objects.create_user(f'{key}@example.invalid', PASSWORD, name=name, sort_order=order, created_at=instant(15), **extra)
        self.root = make('root', '虚构最高权限', 0, is_staff=True)
        self.other_root = make('root2', '虚构第二位最高权限', 1, is_staff=True)
        self.lead = make('lead', '虚构组长', 2)
        self.a = make('a', '虚构甲', 3)
        self.b = make('b', '虚构乙', 4)
        self.outsider = make('o', '虚构组外', 5)
        self.A = Group.objects.create(name='A组', sort_order=1)
        M.objects.create(user=self.lead, group=self.A, role=M.LEADER, start=EARLY)
        M.objects.create(user=self.a, group=self.A, role=M.MEMBER, start=EARLY)
        M.objects.create(user=self.b, group=self.A, role=M.MEMBER, start=EARLY)
        self.week = period_for()
        ensure_obligations(self.week)
        self.today = timezone.localdate()

    def login(self, user):
        self.client.force_login(user)
        return self.client

    def payload(self):
        p = valid_payload()
        p['research']['work'] = {'state': 'ITEMS', 'items': [item('完成了虚构数据采集，覆盖三个场景共九百条样本。', point='完成数据采集'), item('对比三种方法，第二种方法误差最低，约为百分之三。', point='得到对比结果')]}
        return p

    # ---- 周报：要点在前，点开看详细阐述 ----

    def test_report_shows_points_first_with_expandable_detail(self):
        write(self.a, self.week, self.payload(), 0, uuid.uuid4(), timezone.now())
        page = self.login(self.lead).get(f'/people/{self.a.pk}/')
        self.assertContains(page, '<details class="point-view"><summary>完成数据采集</summary>', html=False)
        self.assertContains(page, '完成了虚构数据采集，覆盖三个场景共九百条样本。')
        self.assertContains(page, 'data-expand-all')
        submission = Submission.objects.get(report__user=self.a)
        self.assertContains(self.client.get(f'/submissions/{submission.pk}/'), '<summary>得到对比结果</summary>', html=False)

    def test_version_1_submissions_still_display(self):
        report = Report.objects.create(user=self.b, period=self.week)
        old = valid_payload()
        old['schema_version'] = 1
        old['research']['work'] = {'state': 'ITEMS', 'items': ['第 1 版提交的一整段原文，没有要点。']}
        submission = Submission.objects.create(report=report, version=1, payload=old, counts={}, threshold=20, submitted_at=timezone.now(), token=uuid.uuid4(), digest='0' * 64)
        Report.objects.filter(pk=report.pk).update(latest=submission, first_submitted_at=timezone.now())
        page = self.login(self.root).get(f'/submissions/{submission.pk}/')
        self.assertContains(page, '第 1 版提交的一整段原文，没有要点。')
        self.assertNotContains(page, '<summary></summary>', html=False)

    def test_editor_upgrades_old_draft(self):
        report = Report.objects.create(user=self.a, period=self.week)
        old = valid_payload()
        old['schema_version'] = 1
        old['research']['work'] = {'state': 'ITEMS', 'items': ['旧草稿正文']}
        Draft.objects.create(report=report, payload=old, revision=1)
        page = self.login(self.a).get(f'/my/weeks/{self.week.pk}/')
        self.assertEqual(page.context['payload']['research']['work']['items'], [{'point': '', 'detail': '旧草稿正文'}])
        self.assertEqual(page.context['point_max'], 50)

    def test_docx_export_points_bold_then_detail(self):
        write(self.a, self.week, self.payload(), 0, uuid.uuid4(), timezone.now())
        job = create_export(self.root, self.week, [self.a.pk])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'report.docx'
            build_docx(job.manifest, path)
            paragraphs = Document(path).paragraphs
        point = next(p for p in paragraphs if p.text == '1. 完成数据采集')
        self.assertTrue(any(run.bold and run.text == '完成数据采集' for run in point.runs))
        detail = paragraphs[paragraphs.index(point) + 1]
        self.assertEqual(detail.text, '完成了虚构数据采集，覆盖三个场景共九百条样本。')

    # ---- 批示反馈 ----

    def test_feedback_reaches_the_author(self):
        instruction = planning.create_instruction(self.root, Instruction.GROUP, '本月完成数据采集', group=self.A)
        response = self.login(self.a).post(f'/instructions/{instruction.pk}/feedback/', {'status': 'done', 'note': '已全部采集完毕', 'next': '/'})
        self.assertEqual(response.status_code, 302)
        receipt = InstructionReceipt.objects.get(instruction=instruction, user=self.a)
        self.assertEqual((receipt.status, receipt.note, receipt.seen_by_author), ('done', '已全部采集完毕', False))
        self.assertIsNotNone(receipt.read_at)
        page = self.login(self.root).get('/')
        self.assertEqual([r.pk for r in page.context['feedback']], [receipt.pk])
        self.assertContains(page, '已全部采集完毕')
        self.assertContains(page, '1 条新反馈')
        listing = self.client.get('/instructions/')
        self.assertContains(listing, '反馈 1/3')
        self.assertContains(listing, '已落实')

    def test_author_marks_feedback_seen_and_update_notifies_again(self):
        instruction = planning.create_instruction(self.root, Instruction.GROUP, '注意进度', group=self.A)
        planning.respond(self.a, instruction, 'noted')
        planning.respond(self.b, instruction, 'blocked', '缺少实验设备')
        client = self.login(self.root)
        first = InstructionReceipt.objects.get(user=self.a)
        client.post('/instructions/feedback/seen/', {'receipt': first.pk, 'next': '/'})
        self.assertEqual([r.user for r in planning.new_feedback(self.root)], [self.b])
        client.post('/instructions/feedback/seen/', {'next': '/'})
        self.assertEqual(planning.new_feedback(self.root), [])
        planning.respond(self.a, instruction, 'done', '已经补齐')
        self.assertEqual([r.user for r in planning.new_feedback(self.root)], [self.a])

    def test_feedback_permissions_and_validation(self):
        instruction = planning.create_instruction(self.root, Instruction.GROUP, '注意进度', group=self.A)
        self.assertEqual(self.login(self.outsider).post(f'/instructions/{instruction.pk}/feedback/', {'status': 'done'}).status_code, 403)
        self.login(self.a).post(f'/instructions/{instruction.pk}/feedback/', {'status': 'whatever'})
        self.assertEqual(InstructionReceipt.objects.get(user=self.a).status, '')
        with self.assertRaises(RuleError):
            planning.respond(self.a, instruction, 'done', '长' * 1001)
        planning.withdraw_instruction(self.root, instruction)
        with self.assertRaises(PermissionDenied):
            planning.respond(self.a, instruction, 'done')

    def test_feedback_only_notifies_the_author(self):
        instruction = planning.create_instruction(self.root, Instruction.GROUP, '注意进度', group=self.A)
        planning.respond(self.a, instruction, 'done')
        self.assertEqual(len(planning.new_feedback(self.root)), 1)
        self.assertEqual(planning.new_feedback(self.other_root), [])

    def test_member_home_shows_feedback_buttons(self):
        planning.create_instruction(self.root, Instruction.GROUP, '请周五前汇报', group=self.A)
        page = self.login(self.a).get('/')
        for text in ['待反馈的批示', '请周五前汇报', 'value="noted"', 'value="done"', 'value="blocked"']:
            self.assertContains(page, text)

    # ---- 先小组，后成员 ----

    def test_dashboard_lists_groups_not_members(self):
        page = self.login(self.root).get('/')
        self.assertContains(page, f'href="/groups/{self.A.pk}/"')
        self.assertNotContains(page, f'href="/people/{self.a.pk}/')

    def test_group_page_marks_member_tasks_and_status(self):
        planning.create_group_task(self.lead, self.A, '数据采集', self.today - timedelta(days=7), self.today + timedelta(days=7), assignee_ids=[self.a.pk])
        late = planning.create_group_task(self.lead, self.A, '设备调试', self.today - timedelta(days=30), self.today - timedelta(days=2), assignee_ids=[self.a.pk])
        planning.update_progress(self.lead, late, 70)
        write(self.a, self.week, self.payload(), 0, uuid.uuid4(), timezone.now())
        page = self.login(self.lead).get(f'/groups/{self.A.pk}/')
        member = next(m for m in page.context['members'] if m.pk == self.a.pk)
        self.assertEqual(member.badge[0], '已交')
        self.assertEqual([(t.title, t.state[0]) for t in member.active_tasks], [('设备调试', '延期'), ('数据采集', '进行中')])
        self.assertContains(page, f'href="/people/{self.a.pk}/?group={self.A.pk}"')

    def test_members_do_not_see_member_list(self):
        page = self.login(self.a).get(f'/groups/{self.A.pk}/')
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'class="member-item"')
        self.assertEqual(self.login(self.outsider).get(f'/groups/{self.A.pk}/').status_code, 403)
