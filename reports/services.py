# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
import hashlib
import json
import re
import uuid
from datetime import datetime, time, timedelta
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone
from . import access
from .domain import Conflict, FIELDS, RuleError, SECTIONS, SHANGHAI, empty_payload, first_deadline, period_dates, validate
from .models import Audit, Draft, ExportJob, GroupExemption, Mail, Obligation, Period, Report, ReturnEvent, SiteSettings, Submission, User


def site():
    return SiteSettings.objects.get_or_create(pk=1)[0]


def period_for(now=None):
    start, end, deadline = period_dates(now or timezone.now())
    return Period.objects.get_or_create(start=start, defaults={'end': end, 'deadline': deadline})[0]


def active(user, now=None):
    return user.is_active and (not user.stop_period_id or (now or timezone.now()).astimezone(SHANGHAI).date() < user.stop_period.start)


def obliged_from(user):
    """开始应交的时刻：账号创建时间；老师改回学生的，从改身份的那一刻起按新账号的规则起算（不补以往各周）。"""
    return max(user.created_at, user.identity_changed_at) if user.identity_changed_at else user.created_at


def eligible(user, period):
    # 最高权限账号只做管理、不交周报；老师周报选填。二者都不进入任何周期的应交名单。
    if user.is_staff or user.is_teacher:
        return False
    return first_deadline(obliged_from(user)) <= period.deadline and (not user.stop_period_id or period.start < user.stop_period.start)


def can_write(user, period):
    """能不能写这一周的周报。应交的人照旧；老师不进应交名单，但可以自愿写（账号建立当周起、停用之前）。"""
    if user.is_staff:
        return False
    if user.is_teacher:
        return period.end >= user.created_at.astimezone(SHANGHAI).date() and (not user.stop_period_id or period.start < user.stop_period.start)
    return eligible(user, period)


def is_voluntary(user, period):
    """这一周是老师自愿写的：没有应交记录，不计入统计，也不受截止锁定。还是学生时已经应交的周，仍按应交处理。"""
    return user.is_teacher and not Obligation.objects.filter(user=user, period=period).exists()


def email_enabled():
    """邮件总开关；关闭时不产生任何邮件（催交、退回通知、重置密码）。"""
    return bool(getattr(settings, 'EMAIL_ENABLED', False))


def ensure_obligations(period):
    for user in User.objects.select_related('stop_period').all():
        if eligible(user, period):
            Obligation.objects.get_or_create(user=user, period=period)


def backfill():
    earliest = User.objects.order_by('created_at').first()
    if not earliest:
        return
    cursor = earliest.created_at
    now = timezone.now()
    while period_dates(cursor)[0] <= period_dates(now)[0]:
        ensure_obligations(period_for(cursor))
        cursor += timedelta(days=7)


def staff(user):
    if not user.is_staff or not active(user):
        raise PermissionDenied


def audit(user, period, action, obj, **metadata):
    Audit.objects.create(operator=user, period=period, action=action, object_id=str(obj.pk), metadata=metadata)


def writable(user, period, report, now):
    # 免交（全局或按组）只表示不计入应交、不被催交，不阻止本人自愿填写和提交。
    if not active(user, now) or not can_write(user, period) or period.start > now.astimezone(SHANGHAI).date():
        raise PermissionDenied('此周期不开放填写')
    if report.latest_id and now >= period.deadline and not report.returned and not is_voluntary(user, period):
        raise RuleError('截止后已交周报锁定，请联系组长或最高权限填写原因退回')


@transaction.atomic
def write(user, period, payload, revision, token=None, now=None):
    """所有权由 session user 传入。锁成员以串行化首次创建与停用，再锁报告。"""
    user = User.objects.select_for_update(of=('self',)).select_related('stop_period').get(pk=user.pk)
    ensure_obligations(period)
    report, _ = Report.objects.get_or_create(user=user, period=period)
    report = Report.objects.select_for_update().get(pk=report.pk)
    draft, _ = Draft.objects.get_or_create(report=report, defaults={'payload': empty_payload()})
    now = now or timezone.now()
    token = uuid.UUID(str(token)) if token else None
    if token:
        previous = report.submissions.filter(token=token).first()
        if previous:
            return draft, previous
    writable(user, period, report, now)
    if draft.revision != revision:
        raise Conflict('版本冲突（409）：请保留当前文字，重新加载后合并')
    threshold = site().min_chars
    payload, counts = validate(payload, threshold if token else None)
    submission = None
    if token:
        submission = Submission.objects.create(report=report, version=report.submissions.count() + 1, payload=payload, counts=counts, threshold=threshold, submitted_at=now, token=token, digest=hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest())
        ReturnEvent.objects.filter(report=report, resolved__isnull=True).update(resolved=submission)
        report.latest = submission
        report.first_submitted_at = report.first_submitted_at or now
        report.returned = False
        report.save()
        audit(user, period, 'submit', submission)
    draft.payload = payload
    draft.revision += 1
    draft.save()
    return draft, submission


@transaction.atomic
def return_report(operator, period, user_id, reason):
    access.require_manage(operator, User.objects.get(pk=user_id), period)
    if not reason.strip():
        raise RuleError('必须填写退回原因')
    report = Report.objects.select_for_update(of=('self',)).select_related('latest', 'user').get(user_id=user_id, period=period)
    if not report.latest_id or report.returned:
        raise RuleError('没有可退回的正式版本，或已经待修改')
    event = ReturnEvent.objects.create(report=report, submission=report.latest, operator=operator, reason=reason)
    report.returned = True
    report.save(update_fields=['returned'])
    if email_enabled():
        Mail.objects.create(period=period, user=report.user, kind='return', dedupe=f'return:{event.pk}', subject=f'{period} 周报退回修改', body=f'{report.user.name}，您的 {period} 周报待修改。退回人：{operator.name}。原因：{reason}\n{settings.PUBLIC_BASE_URL}/my/weeks/{period.pk}/')
    audit(operator, period, 'return', event)


def rows(period, scope=None):
    """单周名单。scope 决定纳入谁以及免交口径：
    - 全部：在任一所属小组应交即应交，只有所有小组都免交才算免交；未分组只看全局免交。
    - 小组 / 组长并集：只看范围内的小组；该范围内的小组都免交才算免交。
    全局免交对所有范围生效。草稿永不进入统计。
    """
    scope = scope or access.ALL
    ensure_obligations(period)
    reports = {r.user_id: r for r in Report.objects.filter(period=period).select_related('latest')}
    groups = access.memberships_at(period)
    group_exemptions = {(e.user_id, e.group_id): e for e in GroupExemption.objects.filter(period=period).select_related('operator')}
    drafts = dict(Draft.objects.filter(report__period=period).values_list('report_id', 'payload'))  # 一次取齐，避免逐人查询
    result = []
    now = timezone.now()
    for obligation in Obligation.objects.filter(period=period).select_related('user').order_by('user__sort_order', 'user_id'):
        mine = groups.get(obligation.user_id, [])
        if scope.key == 'all':
            relevant = mine
        elif scope.key == 'ungrouped':
            if mine:
                continue
            relevant = []
        else:
            relevant = [g for g in mine if g.pk in scope.group_ids]
            if not relevant:
                continue
        group_exempt = [g for g in relevant if (obligation.user_id, g.pk) in group_exemptions]
        # 免交原因只在可见范围内展示：全局免交对所有范围可见，按组免交只在该组范围内可见。
        notes = [f'全局免交：{obligation.reason or "未填原因"}'] if obligation.exempt else []
        notes += [f'{g.name}免交（{group_exemptions[(obligation.user_id, g.pk)].operator.name}）：{group_exemptions[(obligation.user_id, g.pk)].reason or "未填原因"}' for g in group_exempt]
        exempt = obligation.exempt or (bool(relevant) and len(group_exempt) == len(relevant))
        report = reports.get(obligation.user_id)
        latest = report.latest if report else None
        state = '免交' if exempt else '逾期未交' if now >= period.deadline else '未提交'
        if latest:
            state = '逾期已交' if report.first_submitted_at >= period.deadline else '已提交'
            if report.returned:
                state += ' / 待修改（使用上一正式版本）'
            elif report.pk in drafts and drafts[report.pk] != latest.payload:
                state += ' / 有未提交修改'
            if exempt:
                state += ' / 免交'
        elif not exempt and report and report.pk in drafts:
            state += ' / 有草稿'
        result.append({'user': obligation.user, 'obligation': obligation, 'report': report, 'latest': latest, 'state': state, 'counts': latest.counts if latest else None,
                       'exempt': exempt, 'groups': mine, 'relevant_groups': relevant, 'group_exempt': group_exempt, 'exempt_notes': notes})
    return result


def voluntary_rows(period, scope=None):
    """老师自愿提交的周报：可以查看、批示、退回、勾选合订，但不在 rows() 里，所以不进应交名单、提交率和统计表。"""
    scope = scope or access.ALL
    obliged = Obligation.objects.filter(period=period).values('user_id')
    reports = (Report.objects.filter(period=period, latest__isnull=False, user__identity=User.TEACHER, user__is_staff=False).exclude(user_id__in=obliged)
               .select_related('user', 'latest').order_by('user__sort_order', 'user_id'))
    groups = access.memberships_at(period)
    result = []
    for report in reports:
        mine = groups.get(report.user_id, [])
        if scope.key == 'ungrouped' and mine:
            continue
        if scope.key not in ('all', 'ungrouped') and not any(g.pk in scope.group_ids for g in mine):
            continue
        result.append({'user': report.user, 'report': report, 'latest': report.latest, 'groups': mine, 'exempt': False,
                       'state': '已提交 / 待修改（使用上一正式版本）' if report.returned else '已提交'})
    return result


def reminder_candidates(period, scope=None):
    return [row['user'] for row in rows(period, scope) if not row['exempt'] and not row['latest'] and active(row['user'])]


def render_mail(template, user, period):
    variables = {'姓名': user.name, '报告周期': str(period), '截止时间': period.deadline.astimezone(SHANGHAI).strftime('%Y-%m-%d %H:%M'), '填写入口': f'{settings.PUBLIC_BASE_URL}/my/weeks/{period.pk}/', '站点名': site().title}
    def replace(match):
        if match[1] not in variables:
            raise RuleError('邮件模板含未知变量')
        return variables[match[1]]
    return re.sub(r'{{\s*(.*?)\s*}}', replace, template)


@transaction.atomic
def queue_reminders(operator, period, ids, batch, subject, body, scope=None):
    if not email_enabled():
        raise RuleError('邮件功能已关闭，不能催交')
    scope = access.resolve_scope(operator, (scope or access.ALL).key)
    if not access.is_super(operator):
        # 组长使用系统统一模板，不能逐批改写。
        config = site()
        subject, body = config.subject, config.body
    uuid.UUID(batch)
    if '\n' in subject or '\r' in subject:
        raise RuleError('主题不能包含换行')
    for user in reminder_candidates(period, scope):
        if user.pk in ids:
            Mail.objects.get_or_create(dedupe=f'reminder:{batch}:{user.pk}', defaults={'period': period, 'user': user, 'kind': 'reminder', 'subject': render_mail(subject, user, period), 'body': render_mail(body, user, period), 'operator': operator, 'scope': scope.key})


def recent_reminders(period, user_ids, hours=12):
    """{user_id: Mail}：hours 小时内任意范围发出的最近一次催交（不含 skipped / failed）。"""
    since = timezone.now() - timedelta(hours=hours)
    result = {}
    for mail in Mail.objects.filter(period=period, kind='reminder', user_id__in=list(user_ids), created_at__gte=since).exclude(state__in=['skipped', 'failed']).select_related('operator').order_by('created_at', 'pk'):
        result[mail.user_id] = mail
    return result


@transaction.atomic
def set_exemption(operator, period, user, group_id, exempt, reason):
    """group_id 为空 = 全局免交（仅最高权限）；否则 = 按组免交（该组组长或最高权限）。"""
    ensure_obligations(period)
    if group_id is None:
        access.require_super(operator)
        obligation = Obligation.objects.select_for_update().filter(user=user, period=period).first()
        if not obligation:
            raise RuleError('该成员此周期没有应交资格')
        obligation.exempt, obligation.reason = exempt, reason
        obligation.save()
        audit(operator, period, 'exemption', obligation, member=user.pk, exempt=exempt, reason=reason)
        return
    if not access.can_exempt_group(operator, group_id):
        raise PermissionDenied
    if group_id not in {g.pk for g in access.memberships_at(period, [user.pk]).get(user.pk, [])}:
        raise RuleError('该成员在此周期不属于这个小组')
    if not Obligation.objects.filter(user=user, period=period).exists():
        raise RuleError('该成员此周期没有应交资格')
    if exempt:
        GroupExemption.objects.update_or_create(user=user, period=period, group_id=group_id, defaults={'reason': reason, 'operator': operator})
    else:
        GroupExemption.objects.filter(user=user, period=period, group_id=group_id).delete()
    audit(operator, period, 'group_exemption', user, group=group_id, exempt=exempt, reason=reason)


@transaction.atomic
def create_export(operator, period, ids, scope=None):
    # PostgreSQL REPEATABLE READ 给整个 manifest 同一个读取快照；必须是事务内第一条语句。
    from django.db import connection
    with connection.cursor() as cursor:
        cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
    scope = access.resolve_scope(operator, (scope or access.ALL).key)
    # 老师自愿写的周报也可以勾选合订；统计表（statistics.py）只看应交名单，不含老师。
    selected = sorted((r for r in rows(period, scope) + voluntary_rows(period, scope) if r['user'].pk in ids), key=lambda r: (r['user'].sort_order, r['user'].pk))
    members, skipped = [], []
    for row in selected:
        if not row['latest']:
            skipped.append({'name': row['user'].name, 'reason': '此周期没有正式版本'})
            continue
        s = row['latest']
        members.append({'user_id': row['user'].pk, 'name': row['user'].name, 'submission_id': s.pk, 'payload': s.payload, 'counts': s.counts, 'returned': row['report'].returned, 'state': row['state'], 'account': row['user'].account, 'first': row['report'].first_submitted_at.isoformat(), 'latest': s.submitted_at.isoformat(), 'exempt': row['exempt'], 'groups': [g.name for g in row['groups']]})
    if not members:
        raise RuleError('选中人员在此周期没有正式内容可导出')
    job = ExportJob.objects.create(period=period, creator=operator, manifest={'template_version': '1', 'period': str(period), 'created_at': timezone.now().isoformat(), 'scope': {'key': scope.key, 'label': scope.label, 'group_ids': list(scope.group_ids)}, 'members': members, 'skipped': skipped})
    audit(operator, period, 'export', job)
    return job
