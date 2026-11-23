# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
"""分组与分级权限：谁能看谁、谁能管谁，全部判断集中于此。

角色
- 最高权限：is_staff 且账号有效，可见全部。
- 小组长：当前周期在某个未归档小组担任组长；只能看、管“该成员在该周期属于自己当前所带小组”的情况。
- 普通成员：只有本人。

范围（Scope）用于单周后台、统计、催交与导出：
- all        全部人员（仅最高权限）
- ungrouped  该周期未分组的成员（仅最高权限）
- group:<id> 某个小组（最高权限或该组当前组长）
- led        组长当前所带全部小组的并集
"""
from dataclasses import dataclass
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.utils import timezone
from .domain import period_dates
from .models import Group, GroupMembership


@dataclass(frozen=True)
class Scope:
    key: str
    label: str
    group_ids: tuple = ()


ALL = Scope('all', '全部人员')
UNGROUPED = Scope('ungrouped', '未分组成员')


def current_start(now=None):
    return period_dates(now or timezone.now())[0]


def covering(start):
    """生效区间 [start, end) 覆盖某周期起始日的分组记录。"""
    return Q(start__lte=start) & (Q(end__isnull=True) | Q(end__gt=start))


def _active(user, now=None):
    from .services import active
    return bool(user and user.is_authenticated and active(user, now))


def is_super(user):
    return _active(user) and user.is_staff


def led_group_ids(user, now=None):
    """当前（而非被查看周期）担任组长的未归档小组；卸任或停用后立即为空。"""
    if not _active(user, now):
        return set()
    return set(GroupMembership.objects.filter(covering(current_start(now)), user=user, role=GroupMembership.LEADER, group__archived=False).values_list('group_id', flat=True))


def can_manage_any(user):
    return is_super(user) or bool(led_group_ids(user))


def memberships_at(period, user_ids=None):
    """{user_id: [Group, ...]}：该周期各成员所属小组，按小组顺序。"""
    rows = GroupMembership.objects.filter(covering(period.start)).select_related('group').order_by('group__sort_order', 'group_id')
    if user_ids is not None:
        rows = rows.filter(user_id__in=list(user_ids))
    result = {}
    for membership in rows:
        groups = result.setdefault(membership.user_id, [])
        if membership.group not in groups:
            groups.append(membership.group)
    return result


def leaders_at(period, group_ids):
    """{group_id: [User, ...]}：该周期各小组组长。"""
    result = {}
    for membership in GroupMembership.objects.filter(covering(period.start), group_id__in=list(group_ids), role=GroupMembership.LEADER).select_related('user').order_by('user__sort_order', 'user_id'):
        result.setdefault(membership.group_id, []).append(membership.user)
    return result


def scopes_for(user):
    if is_super(user):
        scopes = [ALL, UNGROUPED]
        scopes += [Scope(f'group:{g.pk}', g.name + ('（已归档）' if g.archived else ''), (g.pk,)) for g in Group.objects.all()]
        return scopes
    led = led_group_ids(user)
    if not led:
        return []
    groups = list(Group.objects.filter(pk__in=led))
    scopes = [Scope(f'group:{g.pk}', g.name, (g.pk,)) for g in groups]
    if len(groups) > 1:
        scopes.insert(0, Scope('led', '我负责的全部小组', tuple(g.pk for g in groups)))
    return scopes


def resolve_scope(user, key=None):
    """按当前权限解析范围；无权或范围不存在一律 403，不回退到更大的范围。"""
    scopes = scopes_for(user)
    if not scopes:
        raise PermissionDenied
    if not key:
        return scopes[0]
    for scope in scopes:
        if scope.key == key:
            return scope
    raise PermissionDenied


def scope_label(key, operator=None):
    if not key or key == 'all':
        return ALL.label
    if key == 'ungrouped':
        return UNGROUPED.label
    if key == 'led':
        return f'{operator.name} 负责的小组' if operator else '组长负责的小组'
    if key.startswith('group:'):
        group = Group.objects.filter(pk=key[6:]).first() if key[6:].isdigit() else None
        return group.name if group else key
    return key


def can_manage(viewer, user, period):
    """viewer 能否查看、退回、免交 user 在 period 的周报。组长也是组员，可对自己操作。"""
    if is_super(viewer):
        return True
    led = led_group_ids(viewer)
    return bool(led) and GroupMembership.objects.filter(covering(period.start), user=user, group_id__in=led).exists()


def can_view(viewer, user, period):
    return viewer.pk == user.pk or can_manage(viewer, user, period)


def require_manage(viewer, user, period):
    if not can_manage(viewer, user, period):
        raise PermissionDenied


def require_super(user):
    if not is_super(user):
        raise PermissionDenied


def can_exempt_group(viewer, group_id):
    return is_super(viewer) or group_id in led_group_ids(viewer)


def can_access_job(viewer, job):
    """最高权限看全部导出；组长只看自己创建、且仍担任其范围内全部小组组长的导出。"""
    if is_super(viewer):
        return True
    ids = set((job.manifest.get('scope') or {}).get('group_ids') or [])
    return job.creator_id == viewer.pk and bool(ids) and ids <= led_group_ids(viewer)


# ---- 首页看板、成员阅览、任务 ----

def current_group_ids(user, now=None):
    """该用户当前（本周）所在的小组。"""
    return set(GroupMembership.objects.filter(covering(current_start(now)), user=user).values_list('group_id', flat=True))


def can_set_topic(viewer, person, now=None):
    """课题名称：最高权限可以给任何组长和成员设；组长可以给自己当前所带小组里的人设（含自己、其他组长和老师）；
    成员本人不能改。最高权限账号没有课题。"""
    if person.is_staff:
        return False
    if is_super(viewer):
        return True
    return bool(led_group_ids(viewer, now) & current_group_ids(person, now))


def leader_ids_of(user, now=None):
    """该用户当前所在各组的组长（不含本人），用于个人任务的查看与修改权限、批示收件人。"""
    groups = current_group_ids(user, now)
    return set(GroupMembership.objects.filter(covering(current_start(now)), group_id__in=groups, role=GroupMembership.LEADER).exclude(user=user).values_list('user_id', flat=True))


def can_view_person(viewer, person):
    """成员阅览页：最高权限看所有人；本人；组长看本组成员（含曾在组内的人，周报按周期再判断）。"""
    if is_super(viewer) or viewer.pk == person.pk:
        return True
    led = led_group_ids(viewer)
    return bool(led) and GroupMembership.objects.filter(user=person, group_id__in=led).exists()


def viewable_periods(viewer, person, periods):
    """在给定周期里筛出 viewer 能看 person 周报的那些（组长只看成员在本组期间的周）。"""
    periods = list(periods)
    if is_super(viewer) or viewer.pk == person.pk:
        return periods
    led = led_group_ids(viewer)
    ranges = list(GroupMembership.objects.filter(user=person, group_id__in=led).values_list('start', 'end'))
    return [p for p in periods if any(start <= p.start and (end is None or p.start < end) for start, end in ranges)]


def can_view_group(viewer, group):
    """小组任务页：最高权限、该组当前组长、该组当前成员（成员只读）。"""
    return is_super(viewer) or group.pk in led_group_ids(viewer) or group.pk in current_group_ids(viewer)


def can_edit_task(viewer, task):
    """新建 / 修改 / 删除：小组任务归该组组长和最高权限；个人任务归本人、其组长和最高权限。"""
    if is_super(viewer):
        return True
    if task.kind == 'group':
        return task.group_id in led_group_ids(viewer)
    return _active(viewer) and (task.owner_id == viewer.pk or task.owner_id is not None and viewer.pk in leader_ids_of(task.owner))


def can_update_progress(viewer, task):
    """更新进度：能修改任务的人，以及小组任务的负责人。里程碑没有进度。"""
    if task.is_milestone:
        return False
    return can_edit_task(viewer, task) or task.kind == 'group' and _active(viewer) and task.assignees.filter(pk=viewer.pk).exists()


def can_view_task(viewer, task):
    """小组任务：本组成员、组长、最高权限；个人任务：本人、其组长、最高权限。"""
    if task.kind == 'group':
        return can_view_group(viewer, task.group)
    return can_edit_task(viewer, task)
