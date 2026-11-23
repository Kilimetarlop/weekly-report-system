# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
"""首页看板与成员阅览的数据整理。权限范围沿用 access。"""
from datetime import timedelta
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import Http404
from django.urls import reverse
from django.utils import timezone
from . import access, gantt, planning
from .domain import FIELDS, SECTIONS, as_item
from .models import Group, GroupMembership, Obligation, Period, Report, User
from .services import active, period_for, rows, voluntary_rows


def badge(row, period, now=None):
    """成员本周状态标签：(文字, 样式)。口径与“周报管理”一致。"""
    now = now or timezone.now()
    if row['latest']:
        if row['report'].returned:
            return '待修改', 'warn'
        if row['report'].first_submitted_at >= period.deadline:
            return '逾期已交', 'ok'
        return '已交', 'ok'
    if row['exempt']:
        return '免交', 'muted'
    return ('逾期未交', 'bad') if now >= period.deadline else ('未交', 'pending')


def voluntary_badge(row):
    """老师自愿写的周报：只标已交 / 待修改，没有“未交”“逾期”。"""
    return ('待修改', 'warn') if row['report'].returned else ('已交', 'ok')


def summarize(data):
    """应交、已交、未交、待修改、提交率（免交不计入；名单本身已按范围去重）。"""
    required = [r for r in data if not r['exempt']]
    submitted = sum(1 for r in required if r['latest'])
    returned = sum(1 for r in data if r['latest'] and r['report'].returned)
    return {'required': len(required), 'submitted': submitted, 'missing': len(required) - submitted, 'returned': returned,
            'rate': round(submitted * 100 / len(required)) if required else None}


def _group_scope(group):
    return access.Scope(f'group:{group.pk}', group.name, (group.pk,))


def visible_groups(viewer):
    if access.is_super(viewer):
        return list(Group.objects.filter(archived=False))
    led = access.led_group_ids(viewer)
    if not led:
        raise PermissionDenied
    return list(Group.objects.filter(pk__in=led))


def overview(viewer):
    """看板：总体本周 / 上周指标、每个小组的卡片（含成员标签）、未分组成员（仅最高权限）。"""
    now = timezone.now()
    this_week, last_week = period_for(now), period_for(now - timedelta(days=7))
    groups = visible_groups(viewer)
    if access.is_super(viewer):
        scope = access.ALL
    else:
        scope = access.Scope('led', '我负责的全部小组', tuple(g.pk for g in groups))
    leaders = access.leaders_at(this_week, [g.pk for g in groups])
    cards = []
    for group in groups:
        current = rows(this_week, _group_scope(group))
        tasks = planning.tasks_of_group(group)
        cards.append({
            'group': group, 'leaders': leaders.get(group.pk, []),
            'now': summarize(current), 'prev': summarize(rows(last_week, _group_scope(group))),
            'current_tasks': planning.current_tasks(tasks), 'milestone': planning.next_milestone(tasks),
            'members': [{'user': r['user'], 'badge': badge(r, this_week, now)} for r in current],
        })
    ungrouped = []
    if access.is_super(viewer):
        ungrouped = [{'user': r['user'], 'badge': badge(r, this_week, now)} for r in rows(this_week, access.UNGROUPED)]
    return {'this_week': this_week, 'last_week': last_week, 'now': summarize(rows(this_week, scope)), 'prev': summarize(rows(last_week, scope)),
            'cards': cards, 'ungrouped': ungrouped, 'scope_label': scope.label, 'today': timezone.localdate()}


def person_sequence(viewer, group_id=None):
    """“上一位 / 下一位”的顺序：按小组依次，组内按成员顺序；指定小组时只在该组内。跨组成员在每组各出现一次。
    翻看是为了逐个读周报：老师周报选填，写过周报的老师才排进顺序（没写过的页面仍可从小组页点开）。"""
    teachers_with_reports = Report.objects.filter(user__identity=User.TEACHER, latest__isnull=False).values('user_id')
    period = period_for()
    groups = visible_groups(viewer)
    if group_id:
        groups = [g for g in groups if g.pk == group_id]
    sequence = []
    memberships = (GroupMembership.objects.filter(access.covering(period.start), group__in=groups, user__is_staff=False).filter(~Q(user__identity=User.TEACHER) | Q(user_id__in=teachers_with_reports))
                   .select_related('user', 'user__stop_period', 'group').order_by('group__sort_order', 'group_id', 'user__sort_order', 'user_id'))
    seen = set()
    for membership in memberships:
        key = (membership.user_id, membership.group_id)
        if key not in seen and active(membership.user):
            seen.add(key)
            sequence.append((membership.user, membership.group))
    if access.is_super(viewer) and not group_id:
        grouped = {u.pk for u, _ in sequence}
        for user in User.objects.filter(~Q(identity=User.TEACHER) | Q(pk__in=teachers_with_reports), is_staff=False).select_related('stop_period'):
            if user.pk not in grouped and active(user) and not access.current_group_ids(user):
                sequence.append((user, None))
    return sequence


def report_blocks(submission):
    """[(部分, [(小项, {'state', 'items': [{'point', 'detail'}]})])]；第 1 版旧正文的每条视为只有详细阐述。"""
    def field(item):
        return {'state': item['state'], 'items': [as_item(entry) for entry in item['items']]}
    return [(name, [(label, field(submission.payload[key][key2])) for key2, label in FIELDS.items()]) for key, name in SECTIONS.items()]


def person_context(viewer, person, period_id=None, group_id=None, via=None):
    if person.is_staff or not access.can_view_person(viewer, person):
        raise PermissionDenied
    today = timezone.localdate()
    this_week = period_for()
    # 可查看的周（组长只看成员在本组期间的周），新的在前。只有草稿、没有应交记录的周（如改为老师后）不列出。
    periods = Period.objects.filter(Q(obligation__user=person) | Q(report__user=person, report__latest__isnull=False)).distinct().order_by('-start')
    weeks = access.viewable_periods(viewer, person, periods)
    reports = {r.period_id: r for r in Report.objects.filter(user=person, period__in=weeks).select_related('latest')}
    exempt = set(Obligation.objects.filter(user=person, period__in=weeks, exempt=True).values_list('period_id', flat=True))
    if period_id:
        selected = next((p for p in weeks if p.pk == period_id), None)
        if selected is None:
            raise Http404
    else:
        selected = next((p for p in weeks if reports.get(p.pk) and reports[p.pk].latest_id), weeks[0] if weeks else None)
    options = []
    for p in weeks:
        report = reports.get(p.pk)
        if report and report.latest_id:
            label = '待修改' if report.returned else '已交'
        else:
            label = '免交' if p.pk in exempt else '未交'
        options.append({'period': p, 'label': label})
    index = next((i for i, p in enumerate(weeks) if selected and p.pk == selected.pk), None)
    report = reports.get(selected.pk) if selected else None
    submission = report.latest if report and report.latest_id else None

    groups = access.memberships_at(this_week, [person.pk]).get(person.pk, [])
    leaders = access.leaders_at(this_week, [g.pk for g in groups])
    tasks = planning.tasks_of_person(person)
    # 翻看顺序：group=只在该组内翻；via=在“全部小组”顺序里，从该组的位置进来（跨组成员会出现多次，靠它定位，避免来回跳）
    sequence = person_sequence(viewer, group_id) if access.can_manage_any(viewer) else []
    slot = lambda entry: entry[1].pk if entry[1] else 0
    position = next((i for i, entry in enumerate(sequence) if entry[0].pk == person.pk and (group_id or via is None or slot(entry) == via)), None)
    if position is None:
        position = next((i for i, entry in enumerate(sequence) if entry[0].pk == person.pk), None)
    link = lambda entry: reverse('person', args=[entry[0].pk]) + (f'?group={group_id}' if group_id else f'?via={slot(entry)}')
    this_row = next((r for r in rows(this_week, access.ALL) if r['user'].pk == person.pk), None)
    voluntary = None if this_row else next((r for r in voluntary_rows(this_week) if r['user'].pk == person.pk), None)
    instructions = planning.instructions_about_person(viewer, person)
    return {
        'person': person, 'groups': [{'group': g, 'leaders': leaders.get(g.pk, [])} for g in groups],
        # 老师周报选填：写了标已交 / 待修改；没写时 this_badge 为 None，页面只显示“老师”标签
        'this_week': this_week, 'this_badge': badge(this_row, this_week) if this_row else voluntary_badge(voluntary) if voluntary else None if person.is_teacher else ('无需提交', 'muted'),
        'tasks': tasks, 'current_tasks': planning.current_tasks(tasks, today), 'chart': gantt.build(tasks, today),
        'weeks': options, 'selected': selected, 'report': report, 'submission': submission,
        'blocks': report_blocks(submission) if submission else [],
        'newer': weeks[index - 1] if index not in (None, 0) else None,
        'older': weeks[index + 1] if index is not None and index + 1 < len(weeks) else None,
        'can_return': bool(submission and not report.returned and access.can_manage(viewer, person, selected)),
        'can_instruct': access.is_super(viewer),
        'report_instructions': [i for i in instructions if i.target == 'report' and selected and i.period_id == selected.pk],
        'other_instructions': [i for i in instructions if not (i.target == 'report' and selected and i.period_id == selected.pk)],
        'sequence_total': len(sequence), 'position': position,
        'previous_url': link(sequence[position - 1]) if position not in (None, 0) else '',
        'next_url': link(sequence[position + 1]) if position is not None and position + 1 < len(sequence) else '',
        'nav_query': f'group={group_id}' if group_id else (f'via={slot(sequence[position])}' if position is not None else ''),
        'group_id': group_id, 'via_value': slot(sequence[position]) if position is not None and not group_id else None,
    }
