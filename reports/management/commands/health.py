# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
import json
import shutil
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection
from reports.models import ExportJob, Mail


class Command(BaseCommand):
    help = '受控运维检查：数据库、磁盘、任务积压和备份记录'

    def handle(self, *args, **options):
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            database = cursor.fetchone()[0] == 1
        backup = settings.BASE_DIR / 'private' / 'backup-success.txt'
        result = {'database': database, 'free_gb': round(shutil.disk_usage(settings.BASE_DIR).free / 1024**3, 2), 'export_queued': ExportJob.objects.filter(state='queued').count(), 'mail_queued': Mail.objects.filter(state='queued').count(), 'mail_uncertain': Mail.objects.filter(state='uncertain').count(), 'backup_success': backup.read_text() if backup.exists() else None}
        self.stdout.write(json.dumps(result, ensure_ascii=False))
