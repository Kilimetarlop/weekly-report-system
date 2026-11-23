# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
import time
import socket
import smtplib
from datetime import timedelta
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.mail import EmailMessage
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.utils import timezone
from reports import access
from reports.documents import generate
from reports.models import ExportJob, Mail
from reports.services import active, email_enabled, reminder_candidates


class Command(BaseCommand):
    help = '数据库持久化任务 worker；导出单实例串行，SMTP 不确定状态不会自动重发'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')

    def handle(self, *args, **options):
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_try_advisory_lock(7281522026)')
            if not cursor.fetchone()[0]:
                self.stdout.write('已有 worker 正在运行')
                return
        try:
            # 单实例已死后的任务恢复；不冒充上次 SMTP 发送失败。
            Mail.objects.filter(state='sending').update(state='uncertain', error='worker 中断；需人工核查')
            ExportJob.objects.filter(state='running').update(state='queued')
            while True:
                # 邮件总开关关闭时不发送任何邮件；关闭前已排队的邮件保持 queued，不会被补发出去。
                did_work = self.mail_once() if email_enabled() else False
                did_work = self.export_once() or did_work
                if options['once']:
                    break
                if not did_work:
                    time.sleep(2)
        finally:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_unlock(7281522026)')

    def mail_once(self):
        with transaction.atomic():
            mail = Mail.objects.select_for_update(skip_locked=True).filter(state='queued').order_by('pk').first()
            if not mail:
                return False
            mail.state = 'sending'
            mail.save(update_fields=['state', 'updated_at'])
        try:
            # SMTP 前重取当前资格，不使用预览时的旧名单；按发起时的范围复查。
            # 邮箱选填（2026-11-16 起）：没有邮箱的账号无从投递，记为 skipped 而不是失败。
            if not active(mail.user) or not mail.user.email or mail.kind == 'reminder' and mail.user_id not in self.reminder_targets(mail):
                state = 'skipped'
            else:
                sent = EmailMessage(mail.subject, mail.body, to=[mail.user.email]).send(fail_silently=False)
                if not sent:
                    raise RuntimeError('邮件后端未接受')
                state = 'accepted' if settings.EMAIL_BACKEND.endswith('smtp.EmailBackend') else 'test_written'
            Mail.objects.filter(pk=mail.pk).update(state=state, updated_at=timezone.now())
        except (TimeoutError, socket.timeout, ConnectionError, smtplib.SMTPServerDisconnected, OSError):
            Mail.objects.filter(pk=mail.pk).update(state='uncertain', error='连接中断或超时，需人工核查', updated_at=timezone.now())
        except Exception as error:
            # 不把异常正文（可能含邮件 token / 凭据）写日志。
            Mail.objects.filter(pk=mail.pk).update(state='failed', error=type(error).__name__, updated_at=timezone.now())
        return True

    def reminder_targets(self, mail):
        """发起人已失去该范围权限（卸任、停用）时返回空集，邮件记为 skipped。"""
        try:
            scope = access.resolve_scope(mail.operator, mail.scope or 'all') if mail.operator_id else access.ALL
        except PermissionDenied:
            return set()
        return {u.pk for u in reminder_candidates(mail.period, scope)}

    def export_once(self):
        with transaction.atomic():
            job = ExportJob.objects.select_for_update(skip_locked=True).filter(state='queued').order_by('pk').first()
            if not job:
                return False
            job.state = 'running'
            job.save(update_fields=['state', 'updated_at'])
        try:
            generate(job.manifest, settings.BASE_DIR / 'private' / 'exports' / str(job.pk))
            ExportJob.objects.filter(pk=job.pk).update(state='done', error='', updated_at=timezone.now())
        except Exception as error:
            ExportJob.objects.filter(pk=job.pk).update(state='failed', error=str(error)[:500], updated_at=timezone.now())
        return True
