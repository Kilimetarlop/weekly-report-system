# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import BytesIO
from unittest.mock import patch
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import Client, TransactionTestCase, override_settings
from openpyxl import load_workbook
from reports.domain import Conflict, RuleError
from reports.models import Draft, ExportJob, Mail, Obligation, Period, Report, ReturnEvent, Submission, User
from reports.services import active, backfill, create_export, ensure_obligations, period_for, queue_reminders, reminder_candidates, render_mail, return_report, rows, site, write
from reports.statistics import workbook_snapshot
from .test_domain import instant, valid_payload


class BusinessTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user('one@example.invalid', 'Test-only-Password-892!', name='虚构甲', email='one@example.invalid', created_at=instant(15))
        self.other = User.objects.create_user('two@example.invalid', 'Test-only-Password-892!', name='虚构乙', email='two@example.invalid', created_at=instant(15))
        self.admin = User.objects.create_user('admin@example.invalid', 'Test-only-Password-892!', name='虚构管理员', is_staff=True, created_at=instant(15))
        self.period = period_for(instant())
        ensure_obligations(self.period)
        self.client.force_login(self.user)

    def submit(self, user=None, period=None, revision=0, token=None, text=None, now=None):
        return write(user or self.user, period or self.period, valid_payload(text), revision, token or uuid.uuid4(), now or instant())

    def test_T01_no_public_registration(self):
        self.assertEqual(self.client.get('/register/').status_code, 404)

    def test_T02_account_created_by_super_with_password(self):
        # 2026-11-16 起：最高权限设置账号和正式密码，不再发邀请邮件。
        self.client.force_login(self.admin)
        response = self.client.post('/manage/members/', {'action': 'create', 'name': '虚构新成员', 'account': 'NewMember', 'password': 'Weekly-Report-2026!', 'email': 'NEW@EXAMPLE.INVALID'})
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(account='NewMember')
        self.assertTrue(user.check_password('Weekly-Report-2026!'))
        self.assertEqual(user.email, 'new@example.invalid')
        self.assertFalse(Mail.objects.exists())
        with self.assertRaises(DatabaseError), transaction.atomic():
            User.objects.create_user('newmember', name='账号大小写重名')

    def test_T03_super_accounts_do_not_report(self):
        # 2026-11-16 起：最高权限只做管理、不交周报，原“管理员也交周报”取消。
        self.assertFalse(Obligation.objects.filter(user=self.admin).exists())
        with self.assertRaises(PermissionDenied):
            self.submit(self.admin)
        self.assertFalse(Report.objects.filter(user=self.admin).exists())
        self.client.force_login(self.admin)
        self.assertTemplateUsed(self.client.get('/'), 'dashboard.html')  # 首页即看板
        self.assertEqual(self.client.get(f'/my/weeks/{self.period.pk}/').status_code, 403)

    def test_T11_draft_survives_login(self):
        write(self.user, self.period, valid_payload('短草稿'), 0, now=instant())
        self.client.logout()
        self.client.force_login(self.user)
        response = self.client.get(f'/my/weeks/{self.period.pk}/')
        self.assertEqual(response.context['payload']['research']['work']['items'], [{'point': '虚构要点', 'detail': '短草稿'}])
        self.assertFalse(Submission.objects.exists())

    def test_T13_stale_revision(self):
        write(self.user, self.period, valid_payload(), 0, now=instant())
        with self.assertRaises(Conflict):
            write(self.user, self.period, valid_payload(), 0, now=instant())

    def test_T14_idempotent(self):
        token = uuid.uuid4()
        _, one = self.submit(token=token)
        _, two = self.submit(token=token)
        self.assertEqual(one.pk, two.pk)
        self.assertEqual(Submission.objects.count(), 1)

    def test_T15_latest_full_payload_and_late_autosave(self):
        draft, _ = write(self.user, self.period, valid_payload('早期'), 0, now=instant())
        _, submission = self.submit(revision=draft.revision, text='当次最新完整内容必须进入正式版本而不能采用旧草稿')
        self.assertIn('当次最新', submission.payload['research']['work']['items'][0]['detail'])
        with self.assertRaises(Conflict):
            write(self.user, self.period, valid_payload('晚到旧内容'), 1, now=instant())

    def test_T16_draft_not_in_statistics(self):
        self.submit(text='中'*20)
        write(self.user, self.period, valid_payload(), 1, now=instant())
        row = next(r for r in rows(self.period) if r['user']==self.user)
        self.assertEqual(row['counts']['research_work'], 1)

    def test_T17_cross_week(self):
        self.submit()
        next_period = period_for(instant()+timedelta(days=7))
        row = next(r for r in rows(next_period) if r['user']==self.user)
        self.assertIsNone(row['latest'])
        self.assertIsNone(row['counts'])

    def test_T18_late_submission_period(self):
        next_period = period_for(instant()+timedelta(days=7))
        self.submit(now=instant()+timedelta(days=8))
        self.assertEqual(Report.objects.get(user=self.user).period_id, self.period.pk)
        self.assertFalse(Report.objects.filter(user=self.user, period=next_period).exists())

    def test_T20_exact_deadline(self):
        _, before = self.submit(now=instant(16,17,59,59))
        _, at = self.submit(user=self.other, now=instant(16,18))
        self.assertLess(before.submitted_at, self.period.deadline)
        self.assertEqual(at.submitted_at, self.period.deadline)

    def test_T21_first_late_submission(self):
        self.submit(now=instant(17))
        self.assertNotIn(self.user, reminder_candidates(self.period))
        self.assertGreater(Report.objects.get(user=self.user).first_submitted_at,self.period.deadline)

    def test_T22_locked_after_deadline(self):
        self.submit()
        with self.assertRaises(RuleError):
            write(self.user,self.period,valid_payload(),1,now=instant(16,18))

    @override_settings(EMAIL_ENABLED=True)  # 邮件默认关闭；保留代码路径的覆盖，便于以后重新开启
    def test_T23_return_outbox_atomic(self):
        self.submit()
        with self.assertRaises(RuleError):
            return_report(self.admin,self.period,self.user.pk,'  ')
        return_report(self.admin,self.period,self.user.pk,'请补充结果')
        self.assertEqual(Mail.objects.get().kind,'return')
        self.assertTrue(Report.objects.get(user=self.user).returned)

    def test_T24_return_keeps_formal(self):
        self.submit(text='中'*20)
        return_report(self.admin,self.period,self.user.pk,'补充')
        self.assertNotIn(self.user,reminder_candidates(self.period))
        self.assertEqual(next(r for r in rows(self.period) if r['user']==self.user)['counts']['research_work'],1)

    def test_T25_resubmit_preserves_first(self):
        self.submit()
        return_report(self.admin,self.period,self.user.pk,'补充')
        _, submission=self.submit(revision=1,now=instant(17))
        report=Report.objects.get(user=self.user)
        self.assertEqual(report.first_submitted_at,instant())
        self.assertFalse(report.returned)
        self.assertEqual(ReturnEvent.objects.get().resolved_id,submission.pk)

    def test_T26_immutable_even_queryset(self):
        _, submission=self.submit()
        with self.assertRaises(ValueError):
            submission.save()
        with self.assertRaises(DatabaseError), transaction.atomic():
            Submission.objects.filter(pk=submission.pk).update(payload={})
        self.assertEqual(self.client.get('/admin/').status_code,404)

    def test_T27_object_authorization(self):
        _, submission=self.submit(user=self.other)
        self.assertEqual(self.client.get(f'/submissions/{submission.pk}/').status_code,403)
        self.assertEqual(self.client.post(f'/manage/weeks/{self.period.pk}/members/{self.other.pk}/return/',{'reason':'x'}).status_code,403)

    def test_T28_period_exemption(self):
        Obligation.objects.filter(user=self.user,period=self.period).update(exempt=True)
        self.assertNotIn(self.user,reminder_candidates(self.period))
        next_period=period_for(instant()+timedelta(days=7))
        ensure_obligations(next_period)
        self.assertFalse(Obligation.objects.get(user=self.user,period=next_period).exempt)

    def test_T29_exempt_keeps_submission(self):
        self.submit()
        Obligation.objects.filter(user=self.user,period=self.period).update(exempt=True)
        data=rows(self.period)
        required=[r for r in data if not r['obligation'].exempt]
        self.assertLessEqual(sum(bool(r['latest']) for r in required),len(required))
        self.assertEqual(Submission.objects.count(),1)

    def test_T30_disable_live_session_and_preserve_history(self):
        self.submit()
        next_period=period_for(instant()+timedelta(days=7))
        self.user.stop_period=next_period
        self.user.save()
        with patch('reports.services.timezone.now',return_value=instant(24)):
            self.assertEqual(self.client.get('/').status_code,302)
        self.assertTrue(Obligation.objects.filter(user=self.user,period=self.period).exists())
        self.assertFalse(active(self.user,instant(24)))

    def test_T32_creation_time_not_activation(self):
        self.assertFalse(self.user.has_usable_password() is False)
        self.user.last_login=instant(24)
        self.user.save()
        ensure_obligations(self.period)
        self.assertTrue(Obligation.objects.filter(user=self.user,period=self.period).exists())

    def test_T35_backfill_after_outage(self):
        with patch('reports.services.timezone.now',return_value=instant(25)):
            backfill()
        self.assertEqual(Obligation.objects.filter(user=self.user).count(),3)

    def test_T36_threshold_change(self):
        self.submit(text='中'*20)
        config=site();config.min_chars=21;config.save()
        self.assertEqual(Submission.objects.get().threshold,20)
        with self.assertRaisesRegex(RuleError,'21'):
            self.submit(revision=1,text='中'*20)

    def test_T37_template_whitelist(self):
        rendered=render_mail('{{姓名}} {{报告周期}} {{截止时间}} {{填写入口}} {{站点名}}',self.user,self.period)
        self.assertIn(f'/my/weeks/{self.period.pk}/',rendered)
        with self.assertRaises(RuleError):
            render_mail('{{user.password}}',self.user,self.period)

    @override_settings(EMAIL_ENABLED=True)  # 邮件默认关闭；保留代码路径的覆盖，便于以后重新开启
    def test_T38_send_recheck(self):
        queue_reminders(self.admin,self.period,[self.user.pk],str(uuid.uuid4()),'提醒','{{填写入口}}')
        self.submit()
        call_command('worker',once=True,verbosity=0)
        self.assertEqual(Mail.objects.get().state,'skipped')

    @override_settings(EMAIL_ENABLED=True)  # 邮件默认关闭；保留代码路径的覆盖，便于以后重新开启
    def test_T39_uncertain_no_retry(self):
        queue_reminders(self.admin,self.period,[self.user.pk],str(uuid.uuid4()),'提醒','正文')
        with patch('reports.management.commands.worker.EmailMessage.send',side_effect=TimeoutError):
            call_command('worker',once=True,verbosity=0)
        self.assertEqual(Mail.objects.get().state,'uncertain')
        with patch('reports.management.commands.worker.EmailMessage.send') as send:
            call_command('worker',once=True,verbosity=0)
            send.assert_not_called()

    @override_settings(EMAIL_ENABLED=True)  # 邮件默认关闭；保留代码路径的覆盖，便于以后重新开启
    def test_T40_individual_recipients_and_batch_dedupe(self):
        batch=str(uuid.uuid4())
        for _ in range(2):
            queue_reminders(self.admin,self.period,[self.user.pk,self.other.pk],batch,'提醒','正文')
        self.assertEqual(Mail.objects.count(),2)
        with patch('reports.management.commands.worker.EmailMessage.send',return_value=1), patch('reports.management.commands.worker.EmailMessage') as message:
            message.return_value.send.return_value=1
            call_command('worker',once=True,verbosity=0)
            self.assertEqual(len(message.call_args.kwargs['to']),1)

    def test_T41_zero_null_exempt(self):
        self.submit()
        Obligation.objects.filter(user=self.other,period=self.period).update(exempt=True)
        blob=workbook_snapshot(self.period,[self.user.pk,self.other.pk,self.admin.pk],['counts'],[])
        ws=load_workbook(BytesIO(blob)).active
        self.assertEqual(ws.cell(2,4).value,'所属小组')
        self.assertEqual(ws.cell(3,4).value,'未分组')
        self.assertEqual(ws.cell(3,5).value,0)
        self.assertIsNone(ws.cell(4,5).value)
        self.assertIn('免交',ws.cell(4,3).value)

    def test_T42_selected_xlsx(self):
        blob=workbook_snapshot(self.period,[self.user.pk],['counts'],['0','3'])
        wb=load_workbook(BytesIO(blob));ws=wb.active
        self.assertEqual(wb.sheetnames,['要点统计'])
        self.assertEqual(ws.max_row,3)
        self.assertEqual(ws.max_column,8)
        blob=workbook_snapshot(self.period,[self.user.pk],['status'],['s2','s4'])
        ws=load_workbook(BytesIO(blob)).active
        self.assertEqual(ws.max_column,6)
        self.assertEqual(ws.cell(2,5).value,'首次提交')

    def test_T43_sort_preserved(self):
        self.submit();self.submit(user=self.other)
        User.objects.filter(pk=self.other.pk).update(sort_order=0)
        User.objects.filter(pk=self.user.pk).update(sort_order=8)
        job=create_export(self.admin,self.period,[self.user.pk,self.other.pk])
        self.assertEqual([m['user_id'] for m in job.manifest['members']],[self.other.pk,self.user.pk])

    def test_T44_no_formal_skipped(self):
        self.submit()
        job=create_export(self.admin,self.period,[self.user.pk,self.other.pk])
        self.assertEqual(len(job.manifest['members']),1)
        self.assertEqual(job.manifest['skipped'][0]['name'],self.other.name)

    def test_T45_snapshot_frozen(self):
        self.submit()
        job=create_export(self.admin,self.period,[self.user.pk])
        self.submit(revision=1,text='中'*20)
        job.refresh_from_db()
        self.assertEqual(job.manifest['members'][0]['counts']['research_work'],0)
        with self.assertRaises(DatabaseError),transaction.atomic():
            ExportJob.objects.filter(pk=job.pk).update(manifest={})

    def test_T47_escape_and_formula(self):
        text='<script>alert(123)</script>'+'中'*20
        _, submission=self.submit(text=text)
        self.assertContains(self.client.get(f'/submissions/{submission.pk}/'),'&lt;script&gt;')
        self.user.name='=1+1';self.user.save()
        blob=workbook_snapshot(self.period,[self.user.pk],['status'],[])
        cell=load_workbook(BytesIO(blob)).active.cell(3,1)
        self.assertEqual(cell.data_type,'s')
        self.assertEqual(cell.value,'=1+1')

    def test_T48_docx_originals(self):
        import tempfile
        from pathlib import Path
        from docx import Document
        from reports.documents import build_docx
        text='中'*25+'\n第二段 <原文> ABC 123'
        self.submit(text=text)
        job=create_export(self.admin,self.period,[self.user.pk])
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'report.docx';build_docx(job.manifest,path)
            self.assertIn(text,'\n'.join(p.text for p in Document(path).paragraphs))

    def test_T49_private_download(self):
        self.submit()
        job=create_export(self.admin,self.period,[self.user.pk])
        url=f'/manage/export-jobs/{job.pk}/download/docx/'
        self.assertEqual(self.client.get(url).status_code,403)
        self.client.logout();self.assertEqual(self.client.get(url).status_code,302)
        self.assertEqual(self.client.get(f'/private/exports/{job.pk}/report.docx').status_code,404)

    def test_T51_20_concurrent_members(self):
        users=[User.objects.create_user(f'load{i}@example.invalid',name=f'虚构{i}',created_at=instant(15)) for i in range(20)]
        ensure_obligations(self.period)
        def task(user):
            close_old_connections()
            try:
                client=Client();client.force_login(user)
                response=client.put(f'/my/weeks/{self.period.pk}/draft/',data=json.dumps({'payload':valid_payload('草稿'),'expected_revision':0}),content_type='application/json')
                self.assertEqual(response.status_code,200)
                revision=response.json()['revision']
                response=client.post(f'/my/weeks/{self.period.pk}/submit/',data=json.dumps({'payload':valid_payload(str(user.pk)*20),'expected_revision':revision,'idempotency_key':str(uuid.uuid4())}),content_type='application/json')
                self.assertEqual(response.status_code,200)
                return Submission.objects.get(pk=response.json()['submission_id']).report.user_id
            finally:
                close_old_connections()
        started=time.perf_counter()
        with ThreadPoolExecutor(max_workers=20) as pool:
            result=list(pool.map(task,users))
        self.assertEqual(set(result),{u.pk for u in users})
        self.assertEqual(Submission.objects.count(),20)
        print(f'T51: 20 members draft+submit {time.perf_counter()-started:.3f}s')

    def test_cross_report_pointer_database_guard(self):
        _,one=self.submit();self.submit(user=self.other)
        with self.assertRaises(DatabaseError),transaction.atomic():
            Report.objects.filter(user=self.other).update(latest=one)

    def test_activation_one_time(self):
        from reports.views import invite
        user=User.objects.create_user('invite@example.invalid',name='邀请测试')
        url=invite(user).replace(settings.PUBLIC_BASE_URL,'')
        response=self.client.get(url)
        self.assertEqual(response.status_code,302)
        response=self.client.post(response.url,{'new_password1':'Strong-New-Password-429!','new_password2':'Strong-New-Password-429!'})
        self.assertEqual(response.status_code,302)
        self.assertEqual(self.client.get(url).status_code,400)

    def test_manage_pages_render(self):
        self.client.force_login(self.admin)
        for url in [f'/manage/weeks/{self.period.pk}/','/manage/members/','/manage/settings/','/manage/groups/','/manage/members/import/']:
            self.assertEqual(self.client.get(url).status_code,200,url)

    def test_T19_period_switch_clears_choices(self):
        next_period=period_for(instant()+timedelta(days=7))
        self.client.force_login(self.admin)
        response=self.client.get(f'/manage/weeks/{next_period.pk}/')
        self.assertEqual(response.context['period'],next_period)
        self.assertFalse(any(r['latest'] for r in response.context['rows']))
        self.assertNotContains(response,'name="users" value="'+str(self.user.pk)+'" checked')

    def test_T52_conversion_has_no_business_lock(self):
        import threading
        from reports.management.commands.worker import Command
        self.submit()
        job=create_export(self.admin,self.period,[self.user.pk])
        entered=threading.Event();release=threading.Event()
        def slow_convert(*args):
            entered.set();release.wait(10)
        def run():
            close_old_connections()
            try:
                Command().export_once()
            finally:
                close_old_connections()
        with patch('reports.management.commands.worker.generate',side_effect=slow_convert):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(run)
                self.assertTrue(entered.wait(5))
                try:
                    self.submit(user=self.other)
                    self.assertEqual(Submission.objects.count(),2)
                finally:
                    release.set()
                future.result()

    def test_csrf_enforced(self):
        client=Client(enforce_csrf_checks=True);client.force_login(self.user)
        response=client.post(f'/my/weeks/{self.period.pk}/submit/',data=json.dumps({}),content_type='application/json')
        self.assertEqual(response.status_code,403)

    def test_login_same_origin_and_foreign_origin(self):
        client=Client(enforce_csrf_checks=True)
        page=client.get('/login/?next=/my/weeks/%s/' % self.period.pk)
        self.assertEqual(page.status_code,200)
        self.assertEqual(page.headers['Referrer-Policy'],'same-origin')
        token=client.cookies['csrftoken'].value
        data={'username':self.user.account.upper(),'password':'Test-only-Password-892!','csrfmiddlewaretoken':token,'next':f'/my/weeks/{self.period.pk}/'}
        response=client.post('/login/',data,HTTP_ORIGIN='https://foreign.example.invalid')
        self.assertEqual(response.status_code,403)
        response=client.post('/login/',data,HTTP_ORIGIN='http://testserver')
        self.assertEqual(response.status_code,302)
        self.assertEqual(response.url,f'/my/weeks/{self.period.pk}/')

    def test_T54_production_security_configuration(self):
        from pathlib import Path
        text=(Path(settings.BASE_DIR)/'compose.prod.yaml').read_text(encoding='utf-8')
        self.assertIn('443:443',text)
        self.assertIn("BEHIND_PROXY: 'true'",text)  # 生产只经 Caddy 访问，限流取真实来源
        common=(Path(settings.BASE_DIR)/'compose.yaml').read_text(encoding='utf-8')
        self.assertNotIn('5432:5432',common)
        self.assertTrue(settings.SESSION_COOKIE_HTTPONLY)
