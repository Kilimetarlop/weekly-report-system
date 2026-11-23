# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
"""任务、里程碑与批示。所有写操作先检查权限，并写审计记录。"""
from datetime import date
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from . import access
from .domain import RuleError
from .models import Group, GroupMembership, Instruction, InstructionReceipt, Period, Task, User
from .services import active, audit

EARLIEST, LATEST = date(2000, 1, 1), date(2100, 12, 31)


def today():
    return timezone.localdate()


def _date(value, label):
    if isinstance(value, date):
        return value
    try:
        result = date.fromisoformat((value or '').strip())
    except ValueError:
        raise RuleError(f'{label}格式不对，应为 年-月-日')
    if not EARLIEST <= result <= LATEST:
        raise RuleError(f'{label}超出范围')
    return result


def _clean(title, start, end, is_milestone):
    title = (title or '').strip()
    if not title:
        raise RuleError('名称不能为空')
    if len(title) > 200:
        raise RuleError('名称不能超过 200 个字')
    start = _date(start, '开始日期' if not is_milestone else '日期')
    end = start if is_milestone else _date(end, '结束日期')
    if end < start:
        raise RuleError('结束日期不能早于开始日期')
    return title, start, end


def _progress(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise RuleError('进度应为 0—100 的整数')
    if not 0 <= value <= 100:
        raise RuleError('进度应为 0—100 的整数')
    return value


def group_member_ids(group, now=None):
    """该组当前的全部成员（含组长）。"""
    return set(GroupMembership.objects.filter(access.covering(access.current_start(now)), group=group).values_list('user_id', flat=True))


def _assignees(group, ids):
    ids = {int(i) for i in ids if str(i).strip()}
    members = group_member_ids(group)
    if not ids <= members:
        raise RuleError('负责人只能从本组当前成员中选择')
    return ids


@transaction.atomic
def create_group_task(operator, group, title, start, end=None, is_milestone=False, assignee_ids=()):
    if not (access.is_super(operator) or group.pk in access.led_group_ids(operator)):
        raise PermissionDenied
    if group.archived:
        raise RuleError('小组已归档')
    title, start, end = _clean(title, start, end, is_milestone)
    task = Task.objects.create(kind=Task.GROUP, group=group, title=title, start=start, end=end, is_milestone=is_milestone, created_by=operator)
    if not is_milestone:
        task.assignees.set(_assignees(group, assignee_ids))
    audit(operator, None, 'task_create', task, kind=task.kind, group=group.pk, title=title, start=str(start), end=str(end), milestone=is_milestone)
    return task


@transaction.atomic
def create_personal_task(operator, title, start, end=None, is_milestone=False):
    """个人任务只能由本人建（组长和最高权限可以修改，但不替人新建）。"""
    if not active(operator) or operator.is_staff:
        raise PermissionDenied
    title, start, end = _clean(title, start, end, is_milestone)
    task = Task.objects.create(kind=Task.PERSONAL, owner=operator, title=title, start=start, end=end, is_milestone=is_milestone, created_by=operator)
    audit(operator, None, 'task_create', task, kind=task.kind, title=title, start=str(start), end=str(end), milestone=is_milestone)
    return task


@transaction.atomic
def update_task(operator, task, title, start, end=None, progress=None, assignee_ids=None):
    task = Task.objects.select_for_update().get(pk=task.pk)
    if not access.can_edit_task(operator, task):
        raise PermissionDenied
    title, start, end = _clean(title, start, end, task.is_milestone)
    task.title, task.start, task.end = title, start, end
    if progress is not None and not task.is_milestone:
        task.progress = _progress(progress)
    task.save()
    if task.kind == Task.GROUP and assignee_ids is not None and not task.is_milestone:
        task.assignees.set(_assignees(task.group, assignee_ids))
    audit(operator, None, 'task_update', task, title=title, start=str(start), end=str(end), progress=task.progress)
    return task


@transaction.atomic
def update_progress(operator, task, progress):
    task = Task.objects.select_for_update().get(pk=task.pk)
    if not access.can_update_progress(operator, task):
        raise PermissionDenied
    previous = task.progress
    task.progress = _progress(progress)
    task.save(update_fields=['progress', 'updated_at'])
    audit(operator, None, 'task_progress', task, previous=previous, progress=task.progress)
    return task


@transaction.atomic
def delete_task(operator, task):
    task = Task.objects.select_for_update().get(pk=task.pk)
    if not access.can_edit_task(operator, task):
        raise PermissionDenied
    audit(operator, None, 'task_delete', task, kind=task.kind, title=task.title, start=str(task.start), end=str(task.end))
    task.assignees.clear()
    task.delete()  # 相关批示保留，只是不再链接到任务（subject 中保留了任务名）


def tasks_of_person(person):
    """成员阅览 / 我的任务的甘特图：本人负责的小组任务、个人任务，以及所在小组的里程碑。"""
    groups = access.current_group_ids(person)
    return list(Task.objects.filter(
        Q(kind=Task.PERSONAL, owner=person) | Q(kind=Task.GROUP, assignees=person) | Q(kind=Task.GROUP, is_milestone=True, group_id__in=groups)
    ).distinct().select_related('group', 'owner').prefetch_related('assignees'))


def tasks_of_group(group):
    return list(Task.objects.filter(kind=Task.GROUP, group=group).select_related('group').prefetch_related('assignees'))


def current_tasks(tasks, on=None):
    """当前任务：今天在起止日期内、进度未到 100% 的非里程碑任务。"""
    on = on or today()
    return [t for t in tasks if not t.is_milestone and t.start <= on <= t.end and t.progress < 100]


def next_milestone(tasks, on=None):
    on = on or today()
    upcoming = sorted((t for t in tasks if t.is_milestone and t.start >= on), key=lambda t: t.start)
    return upcoming[0] if upcoming else None


# ---- 批示 ----

def _recipients_for_report(member):
    return {member.pk} | access.leader_ids_of(member)


def _recipients_for_task(task):
    if task.kind == Task.GROUP:
        leaders = set(GroupMembership.objects.filter(access.covering(access.current_start()), group=task.group, role=GroupMembership.LEADER).values_list('user_id', flat=True))
        return set(task.assignees.values_list('pk', flat=True)) | leaders
    return {task.owner_id} | access.leader_ids_of(task.owner)


@transaction.atomic
def create_instruction(operator, target, text, member=None, period=None, task=None, group=None):
    """只有最高权限能写；收件人在此刻确定，排除最高权限账号和已停用账号。"""
    access.require_super(operator)
    text = (text or '').strip()
    if not text:
        raise RuleError('批示内容不能为空')
    if len(text) > 2000:
        raise RuleError('批示不能超过 2000 字')
    if target == Instruction.REPORT:
        if member is None or period is None:
            raise RuleError('缺少成员或周期')
        recipients, subject = _recipients_for_report(member), f'{member.name} · {period} 周报'
    elif target == Instruction.TASK:
        if task is None:
            raise RuleError('缺少任务')
        recipients = _recipients_for_task(task)
        subject = f'任务：{task.title}（{task.group.name if task.group_id else task.owner.name + "的个人任务"}）'
    elif target == Instruction.GROUP:
        if group is None:
            raise RuleError('缺少小组')
        recipients, subject = group_member_ids(group), f'小组：{group.name}'
    else:
        raise RuleError('未知的批示对象')
    users = [u for u in User.objects.filter(pk__in=recipients).select_related('stop_period') if not u.is_staff and active(u)]
    instruction = Instruction.objects.create(author=operator, target=target, member=member if target == Instruction.REPORT else None, period=period if target == Instruction.REPORT else None, task=task, group=group, subject=subject, text=text)
    InstructionReceipt.objects.bulk_create([InstructionReceipt(instruction=instruction, user=u) for u in users])
    audit(operator, period, 'instruction_create', instruction, target=target, recipients=sorted(u.pk for u in users))
    return instruction


FEEDBACK_LABELS = dict(InstructionReceipt.FEEDBACK)


@transaction.atomic
def respond(user, instruction, status, note=''):
    """收件人给批示人的反馈：已知悉 / 已落实 / 有困难，可附说明；可以修改，修改后批示人会再次看到“新反馈”。"""
    if status not in FEEDBACK_LABELS:
        raise RuleError('请选择反馈：已知悉、已落实或有困难')
    note = (note or '').strip()
    if len(note) > 1000:
        raise RuleError('说明不能超过 1000 字')
    receipt = InstructionReceipt.objects.select_for_update().filter(instruction=instruction, user=user).first()
    if receipt is None or instruction.withdrawn_at:
        raise PermissionDenied
    now = timezone.now()
    receipt.status, receipt.note, receipt.responded_at, receipt.seen_by_author = status, note, now, False
    receipt.read_at = receipt.read_at or now
    receipt.save()
    audit(user, instruction.period, 'instruction_feedback', instruction, status=status)
    return receipt


@transaction.atomic
def mark_read(user, instruction):
    """只点“知道了”：等同于反馈“已知悉”（已有反馈的不覆盖）。"""
    receipt = InstructionReceipt.objects.filter(instruction=instruction, user=user).first()
    if receipt and not receipt.status and not instruction.withdrawn_at:
        respond(user, instruction, 'noted', '')
    elif receipt and not receipt.read_at:
        InstructionReceipt.objects.filter(pk=receipt.pk).update(read_at=timezone.now())


def my_instructions(user, limit=30):
    """收件人看到的批示（未撤回），附上自己的收件记录 my_receipt；新的在前。"""
    result = []
    receipts = InstructionReceipt.objects.filter(user=user, instruction__withdrawn_at__isnull=True).select_related('instruction', 'instruction__author', 'instruction__period', 'instruction__task', 'instruction__group', 'instruction__member').order_by('-instruction__created_at')[:limit]
    for receipt in receipts:
        instruction = receipt.instruction
        instruction.my_receipt = receipt
        result.append(instruction)
    return result


def new_feedback(author):
    """批示人收到、还没看过的反馈。"""
    return list(InstructionReceipt.objects.filter(instruction__author=author, seen_by_author=False, instruction__withdrawn_at__isnull=True).select_related('instruction', 'user').order_by('-responded_at'))


@transaction.atomic
def mark_feedback_seen(author, receipt_id=None):
    query = InstructionReceipt.objects.filter(instruction__author=author, seen_by_author=False)
    if receipt_id:
        query = query.filter(pk=receipt_id)
    query.update(seen_by_author=True)


def task_state(task, on=None):
    """任务状态（文字, 样式）：已完成 / 延期 / 未开始 / 进行中；里程碑按日期判断。"""
    on = on or today()
    if task.is_milestone:
        return ('已过', 'muted') if task.start < on else ('即将到来', 'info')
    if task.progress >= 100:
        return '已完成', 'ok'
    if task.end < on:
        return '延期', 'bad'
    if task.start > on:
        return '未开始', 'muted'
    return '进行中', 'info'


@transaction.atomic
def withdraw_instruction(operator, instruction):
    access.require_super(operator)
    if instruction.withdrawn_at:
        return
    instruction.withdrawn_at = timezone.now()
    instruction.save(update_fields=['withdrawn_at'])
    audit(operator, instruction.period, 'instruction_withdraw', instruction)


def unread_for(user):
    return list(Instruction.objects.filter(receipts__user=user, receipts__read_at__isnull=True, withdrawn_at__isnull=True).select_related('author', 'period', 'task', 'group', 'member').order_by('-created_at'))


def with_read_counts(instructions):
    """给每条批示附上 read / total（最高权限查看已读情况用）和未读者姓名。"""
    instructions = list(instructions)
    receipts = {}
    for receipt in InstructionReceipt.objects.filter(instruction__in=instructions).select_related('user'):
        receipts.setdefault(receipt.instruction_id, []).append(receipt)
    for item in instructions:
        mine = receipts.get(item.pk, [])
        item.read_count = sum(1 for r in mine if r.read_at)
        item.total_count = len(mine)
        item.responded_count = sum(1 for r in mine if r.status)
        item.unread_names = [r.user.name for r in mine if not r.read_at]
        item.feedback = sorted(mine, key=lambda r: (not r.status, r.user.sort_order, r.user_id))  # 已反馈的排前面
    return instructions


def visible_instructions(viewer, queryset):
    """最高权限看全部（含已撤回）；其他人只看自己是收件人、且未撤回的。"""
    if access.is_super(viewer):
        return with_read_counts(queryset.select_related('author', 'period', 'task', 'group', 'member'))
    return list(queryset.filter(receipts__user=viewer, withdrawn_at__isnull=True).select_related('author', 'period', 'task', 'group', 'member').distinct())


def instructions_about_person(viewer, person):
    groups = access.current_group_ids(person)
    task_ids = [t.pk for t in tasks_of_person(person)]
    query = Instruction.objects.filter(Q(target=Instruction.REPORT, member=person) | Q(target=Instruction.TASK, task_id__in=task_ids) | Q(target=Instruction.GROUP, group_id__in=groups))
    return visible_instructions(viewer, query)


def instructions_about_group(viewer, group):
    query = Instruction.objects.filter(Q(target=Instruction.GROUP, group=group) | Q(target=Instruction.TASK, task__group=group))
    return visible_instructions(viewer, query)
