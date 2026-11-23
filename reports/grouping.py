# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
"""分组维护（仅最高权限）。所有变更写审计；生效周期可选过去的周，用于补录。

变更只影响“谁能看到”和各组历史统计口径，不改动任何周报正文，也不影响已生成的导出快照。
"""
from datetime import date, datetime, time
from django.db import IntegrityError, transaction
from django.db.models import Max
from . import access
from .domain import SHANGHAI, RuleError, period_dates
from .models import Group, GroupMembership, User
from .services import audit


def week_start(value):
    """任意日期（或 YYYY-MM-DD）归一到所在周期的起始周六。"""
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value.strip())
        except ValueError:
            raise RuleError('日期格式应为 YYYY-MM-DD')
    return period_dates(datetime.combine(value, time(12), SHANGHAI))[0]


def _check_overlap(user, group, start, end, exclude=None):
    others = GroupMembership.objects.filter(user=user, group=group)
    if exclude:
        others = others.exclude(pk=exclude)
    for other in others:
        if other.start < (end or date.max) and start < (other.end or date.max):
            raise RuleError(f'{user.name} 在“{group.name}”已有重叠的生效区间（{other.start} 起），请先调整原记录')


def _lock(user_id):
    # 锁成员行，串行化同一成员的分组变更，保证区间不重叠。
    return User.objects.select_for_update(of=('self',)).get(pk=user_id)


@transaction.atomic
def create_group(operator, name):
    access.require_super(operator)
    name = name.strip()
    if not name:
        raise RuleError('小组名称不能为空')
    order = (Group.objects.aggregate(m=Max('sort_order'))['m'] or 0) + 1
    try:
        with transaction.atomic():
            group = Group.objects.create(name=name, sort_order=order)
    except IntegrityError:
        raise RuleError('已有同名小组')
    audit(operator, None, 'group_create', group, name=name)
    return group


@transaction.atomic
def rename_group(operator, group, name):
    access.require_super(operator)
    name = name.strip()
    if not name:
        raise RuleError('小组名称不能为空')
    previous = group.name
    group.name = name
    try:
        with transaction.atomic():
            group.save(update_fields=['name'])
    except IntegrityError:
        raise RuleError('已有同名小组')
    audit(operator, None, 'group_rename', group, previous=previous, name=name)


@transaction.atomic
def set_archived(operator, group, archived):
    access.require_super(operator)
    if archived and GroupMembership.objects.filter(group=group).exclude(end__lte=access.current_start()).exists():
        raise RuleError('归档前请先结束该组本周及以后的全部组员关系')
    group.archived = archived
    group.save(update_fields=['archived'])
    audit(operator, None, 'group_archive', group, archived=archived)


@transaction.atomic
def add_membership(operator, group, user, role, start, end=None):
    access.require_super(operator)
    if role not in dict(GroupMembership.ROLES):
        raise RuleError('角色只能是组员或组长')
    if group.archived:
        raise RuleError('小组已归档，不能加入成员')
    user = _lock(user.pk)
    if user.is_staff:
        raise RuleError('最高权限账号只做管理、不交周报，不加入小组')
    start = week_start(start)
    end = week_start(end) if end else None
    if end and end <= start:
        raise RuleError('结束周期必须晚于起始周期')
    _check_overlap(user, group, start, end)
    membership = GroupMembership.objects.create(user=user, group=group, role=role, start=start, end=end)
    audit(operator, None, 'membership_add', membership, group=group.pk, member=user.pk, role=role, start=str(start), end=str(end) if end else None)
    return membership


@transaction.atomic
def end_membership(operator, membership, end):
    """设置或修改结束周期（含过去的周）；end 为空表示恢复为至今仍在组内。"""
    access.require_super(operator)
    _lock(membership.user_id)
    membership = GroupMembership.objects.select_for_update().get(pk=membership.pk)
    end = week_start(end) if end else None
    if end and end <= membership.start:
        raise RuleError('结束周期必须晚于起始周期；录入错误请删除这条记录')
    _check_overlap(membership.user, membership.group, membership.start, end, exclude=membership.pk)
    previous = membership.end
    membership.end = end
    membership.save(update_fields=['end'])
    audit(operator, None, 'membership_end', membership, group=membership.group_id, member=membership.user_id, previous=str(previous) if previous else None, end=str(end) if end else None)


@transaction.atomic
def change_role(operator, membership, at):
    """从 at 所在周起切换组员 / 组长：原记录在该周结束，新记录从该周开始，区间连续。"""
    access.require_super(operator)
    _lock(membership.user_id)
    membership = GroupMembership.objects.select_for_update().get(pk=membership.pk)
    at = week_start(at)
    new_role = GroupMembership.MEMBER if membership.role == GroupMembership.LEADER else GroupMembership.LEADER
    if at < membership.start or (membership.end and at >= membership.end):
        raise RuleError('切换周期必须落在这条记录的生效区间内')
    if at == membership.start:
        membership.role = new_role
        membership.save(update_fields=['role'])
        result = membership
    else:
        old_end = membership.end
        membership.end = at
        membership.save(update_fields=['end'])
        result = GroupMembership.objects.create(user=membership.user, group=membership.group, role=new_role, start=at, end=old_end)
    audit(operator, None, 'membership_role', result, group=membership.group_id, member=membership.user_id, role=new_role, at=str(at))
    return result


@transaction.atomic
def delete_membership(operator, membership):
    """仅用于更正录入错误；审计保留被删除记录的完整内容。"""
    access.require_super(operator)
    _lock(membership.user_id)
    audit(operator, None, 'membership_delete', membership, group=membership.group_id, member=membership.user_id, role=membership.role, start=str(membership.start), end=str(membership.end) if membership.end else None)
    GroupMembership.objects.filter(pk=membership.pk).delete()
