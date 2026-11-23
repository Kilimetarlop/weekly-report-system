# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
import base64
import json
import uuid
from datetime import timedelta
from urllib.parse import quote
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes, force_str
from django.utils.http import url_has_allowed_host_and_scheme, urlsafe_base64_decode, urlsafe_base64_encode
from django.views.decorators.http import require_POST, require_http_methods
from . import access, accounts, dashboard, gantt, grouping, manuals, planning
from .domain import Conflict, FIELDS, GUIDES, POINT_MAX, RuleError, SECTIONS, empty_payload, first_deadline, upgrade
from .models import Draft, ExportJob, Group, GroupMembership, Instruction, Mail, Period, Report, ReturnEvent, Submission, Task, User
from .forms import ChangeOwnPasswordForm
from .services import active, audit, backfill, can_write, create_export, eligible, email_enabled, is_voluntary, obliged_from, period_for, queue_reminders, recent_reminders, reminder_candidates, render_mail, return_report, rows, set_exemption, site, staff, voluntary_rows, write


def portal_url(period_id, scope_key=''):
    url = reverse('portal', args=[period_id])
    return f'{url}?scope={quote(scope_key)}' if scope_key else url


def back(request, default='/'):
    """操作完成后回到来源页面；只接受本站地址。"""
    target = request.POST.get('next') or request.GET.get('next') or ''
    return redirect(target if url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}) else default)


def my_report_card(user):
    """本人的本周周报卡片（成员首页、组长看板顶部共用）。"""
    period = period_for()
    report = Report.objects.filter(user=user, period=period).select_related('latest').first()
    return {'my_period': period, 'my_report': report, 'my_eligible': eligible(user, period), 'my_can_write': can_write(user, period), 'my_voluntary': is_voluntary(user, period), 'my_first': first_deadline(obliged_from(user)),
            'my_has_draft': bool(report and Draft.objects.filter(report=report).exists()), 'my_late': timezone.now() >= period.deadline,
            'my_locked': timezone.now() >= period.deadline and not is_voluntary(user, period),
            'open_returns': ReturnEvent.objects.filter(report__user=user, resolved__isnull=True).select_related('report__period', 'operator').order_by('-pk')}


@login_required
def home(request):
    """首页：最高权限和组长看看板（组长只含本组），成员看自己的首页。"""
    backfill()
    if access.can_manage_any(request.user):
        context = dashboard.overview(request.user)
        context['before_deadline'] = timezone.now() < context['this_week'].deadline
        if access.is_super(request.user):
            context['feedback'] = planning.new_feedback(request.user)  # 给批示人的新反馈
        else:
            context.update(my_report_card(request.user))
            context.update(_my_instruction_lists(request.user))
        return render(request, 'dashboard.html', context)
    return my_home(request)


def _my_instruction_lists(user):
    """收件人：待反馈的批示（未读）在前，最近已反馈的可以展开修改。"""
    mine = planning.my_instructions(user)
    return {'unread': [i for i in mine if not i.my_receipt.read_at], 'recent': [i for i in mine if i.my_receipt.read_at][:5]}


@login_required
def my_home(request):
    """成员首页 / 组长的“我的周报”：本周卡片、未读批示、我的任务、本组任务、历史周报。"""
    if access.is_super(request.user):
        return redirect('home')
    user = request.user
    period = period_for()
    today = timezone.localdate()
    mine = access.memberships_at(period, [user.pk]).get(user.pk, [])
    leaders = access.leaders_at(period, [g.pk for g in mine])
    led = access.led_group_ids(user)
    tasks = planning.tasks_of_person(user)
    context = my_report_card(user)
    context.update({
        'period': period, 'my_groups': [{'group': g, 'leaders': leaders.get(g.pk, [])} for g in mine],
        **_my_instruction_lists(user), 'chart': gantt.build(tasks, today), 'current_tasks': planning.current_tasks(tasks, today),
        # 个人页只放个人的事：自己带的小组在侧栏“小组管理”里看，这里只列自己作为组员所在的小组
        'group_charts': [{'group': g, 'chart': gantt.build(planning.tasks_of_group(g), today)} for g in mine if g.pk not in led],
        'history': [{'period': p, 'report': r} for p, r in _history(user)],
    })
    return render(request, 'my_home.html', context)


def _history(user):
    # 老师没有应交记录，历史按自己写过的周列出
    periods = list(Period.objects.filter(Q(obligation__user=user) | Q(report__user=user)).distinct().order_by('-start')[:26])
    reports = {r.period_id: r for r in Report.objects.filter(user=user, period__in=periods).select_related('latest')}
    return [(p, reports.get(p.pk)) for p in periods]


@login_required
def person(request, user_id):
    """成员阅览：小组、组长、当前任务、甘特图、周报（默认最近一次提交的周）、批示、上一位 / 下一位。"""
    target = get_object_or_404(User, pk=user_id)
    week = request.GET.get('week', '')
    group, via = request.GET.get('group', ''), request.GET.get('via', '')
    context = dashboard.person_context(request.user, target, int(week) if week.isdigit() else None, int(group) if group.isdigit() else None, int(via) if via.isdigit() else None)
    context['group_name'] = Group.objects.filter(pk=context['group_id']).values_list('name', flat=True).first() if context['group_id'] else ''
    for task in context['tasks']:
        task.can_edit = access.can_edit_task(request.user, task)
        task.can_progress = access.can_update_progress(request.user, task)
        task.state = planning.task_state(task)
    context['today'] = timezone.localdate()
    return render(request, 'person.html', context)


def _site_url(request):
    """手册里写的网址：优先用配置的 PUBLIC_BASE_URL；没配（还是本机默认值）时用访问者正在用的地址。"""
    configured = settings.PUBLIC_BASE_URL or ''
    if configured and 'localhost' not in configured and '127.0.0.1' not in configured:
        return configured.rstrip('/')
    return request.build_absolute_uri('/').rstrip('/')


@login_required
def help_page(request):
    """使用说明：按登录角色默认显示对应手册；最高权限能看三份，组长能看组长和组员两份。"""
    roles = manuals.roles_for(request.user)
    role = request.GET.get('role') or roles[0]
    if role not in roles:
        raise Http404
    return render(request, 'help.html', {'manual': manuals.get(role, _site_url(request)), 'role': role,
                                         'roles': [(key, manuals.LABELS[key]) for key in roles]})


@login_required
def help_docx(request, role):
    """手册 Word 版（可打印、发群里），内容与“使用说明”页相同。"""
    if role not in manuals.roles_for(request.user):
        raise Http404
    response = HttpResponse(manuals.to_docx(role, _site_url(request)), content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document')
    response['Content-Disposition'] = f"attachment; filename*=UTF-8''{quote(manuals.LABELS[role] + '使用手册.docx')}"
    return response


@login_required
@require_POST
def member_topic(request, user_id):
    """改课题：组长（本组的人）和最高权限，在小组页“进入编辑”后提交；成员本人不能改。"""
    person = get_object_or_404(User, pk=user_id)
    try:
        person, changed = accounts.set_topic(request.user, person, request.POST.get('topic', ''))
        if changed:
            messages.success(request, f'已保存 {person.name} 的课题：{person.topic}' if person.topic else f'已清空 {person.name} 的课题。')
    except RuleError as error:
        messages.error(request, f'没有保存：{error}')
    return back(request)


@login_required
def group_page(request, group_id):
    """小组页：先看组员（每人标出本周周报状态、正在做的任务和任务状态），点组员进入甘特图和周报；
    下面是小组任务甘特图与维护、小组批示。组员列表只给组长和最高权限看（成员看不到组员的个人任务）。"""
    group = get_object_or_404(Group, pk=group_id)
    if not access.can_view_group(request.user, group):
        raise PermissionDenied
    period = period_for()
    today = timezone.localdate()
    tasks = planning.tasks_of_group(group)
    can_edit = access.is_super(request.user) or group.pk in access.led_group_ids(request.user)
    can_manage_people = can_edit
    for task in tasks:
        task.can_edit = can_edit
        task.can_progress = access.can_update_progress(request.user, task)
        task.assignee_ids = {u.pk for u in task.assignees.all()}
        task.state = planning.task_state(task, today)
    memberships = GroupMembership.objects.filter(access.covering(period.start), group=group).select_related('user').order_by('user__sort_order', 'user_id')
    members = [m.user for m in memberships]
    leader_ids = {m.user_id for m in memberships if m.role == GroupMembership.LEADER}
    scope = access.Scope(f'group:{group.pk}', group.name, (group.pk,))
    this_rows = rows(period, scope)
    if can_manage_people:
        by_user = {r['user'].pk: r for r in this_rows}
        voluntary = {r['user'].pk: r for r in voluntary_rows(period, scope)}
        for member in members:
            row = by_user.get(member.pk)
            member.badge = dashboard.badge(row, period) if row else ('无需提交', 'muted')
            if member.is_teacher and not row:  # 老师周报选填：写了标已交，没写不标“未交”
                member.badge = dashboard.voluntary_badge(voluntary[member.pk]) if member.pk in voluntary else None
            member.is_leader = member.pk in leader_ids
            own = [t for t in planning.tasks_of_person(member) if not t.is_milestone]
            for task in own:
                task.state = planning.task_state(task, today)
            member.active_tasks = sorted((t for t in own if t.state[1] in ('info', 'bad')), key=lambda t: (t.state[1] != 'bad', t.end))
            member.upcoming = sum(1 for t in own if t.state[0] == '未开始')
            member.finished = sum(1 for t in own if t.state[1] == 'ok')
    return render(request, 'group.html', {
        'group': group, 'tasks': tasks, 'chart': gantt.build(tasks, today), 'members': members,
        'leaders': access.leaders_at(period, [group.pk]).get(group.pk, []),
        'now': dashboard.summarize(this_rows), 'prev': dashboard.summarize(rows(period_for(timezone.now() - timedelta(days=7)), scope)),
        'can_edit': can_edit, 'can_manage_people': can_manage_people,
        'instructions': planning.instructions_about_group(request.user, group), 'today': today,
    })


@login_required
def my_tasks(request):
    """我的任务：新建 / 修改个人任务和里程碑，更新自己负责的小组任务进度。"""
    if access.is_super(request.user):
        return redirect('home')
    user = request.user
    personal = list(Task.objects.filter(kind=Task.PERSONAL, owner=user))
    assigned = list(Task.objects.filter(kind=Task.GROUP, assignees=user, is_milestone=False).select_related('group').prefetch_related('assignees'))
    for task in personal + assigned:
        task.can_edit = access.can_edit_task(user, task)
        task.can_progress = access.can_update_progress(user, task)
        task.state = planning.task_state(task)
    return render(request, 'my_tasks.html', {
        'chart': gantt.build(planning.tasks_of_person(user), timezone.localdate()),
        'personal': personal, 'assigned': assigned, 'today': timezone.localdate(),
    })


@login_required
@require_POST
def task_create(request):
    try:
        milestone = request.POST.get('milestone') == '1'
        title, start, end = request.POST.get('title', ''), request.POST.get('start', ''), request.POST.get('end', '')
        if request.POST.get('kind') == Task.GROUP:
            group = get_object_or_404(Group, pk=request.POST.get('group'))
            planning.create_group_task(request.user, group, title, start, end, milestone, request.POST.getlist('assignees'))
        else:
            planning.create_personal_task(request.user, title, start, end, milestone)
        messages.success(request, '已添加里程碑。' if milestone else '已添加任务。')
    except RuleError as error:
        messages.error(request, f'没有保存：{error}')
    return back(request)


@login_required
@require_POST
def task_edit(request, task_id):
    task = get_object_or_404(Task, pk=task_id)
    try:
        assignees = request.POST.getlist('assignees') if request.POST.get('has_assignees') == '1' else None
        planning.update_task(request.user, task, request.POST.get('title', ''), request.POST.get('start', ''), request.POST.get('end', ''), request.POST.get('progress'), assignees)
        messages.success(request, '已保存。')
    except RuleError as error:
        messages.error(request, f'没有保存：{error}')
    return back(request)


@login_required
@require_POST
def task_progress(request, task_id):
    task = get_object_or_404(Task, pk=task_id)
    try:
        planning.update_progress(request.user, task, request.POST.get('progress'))
        messages.success(request, f'“{task.title}”进度已更新。')
    except RuleError as error:
        messages.error(request, f'没有保存：{error}')
    return back(request)


@login_required
@require_POST
def task_delete(request, task_id):
    task = get_object_or_404(Task, pk=task_id)
    planning.delete_task(request.user, task)
    messages.success(request, f'已删除“{task.title}”。')
    return back(request)


@login_required
@require_POST
def instruction_create(request):
    access.require_super(request.user)
    target = request.POST.get('target')
    try:
        if target == Instruction.REPORT:
            member = get_object_or_404(User, pk=request.POST.get('member'), is_staff=False)
            period = get_object_or_404(Period, pk=request.POST.get('period'))
            planning.create_instruction(request.user, target, request.POST.get('text'), member=member, period=period)
        elif target == Instruction.TASK:
            planning.create_instruction(request.user, target, request.POST.get('text'), task=get_object_or_404(Task, pk=request.POST.get('task')))
        else:
            planning.create_instruction(request.user, target, request.POST.get('text'), group=get_object_or_404(Group, pk=request.POST.get('group')))
        messages.success(request, '批示已发送，收件人会在首页看到。')
    except RuleError as error:
        messages.error(request, f'没有发送：{error}')
    return back(request)


@login_required
@require_POST
def instruction_read(request, instruction_id):
    planning.mark_read(request.user, get_object_or_404(Instruction, pk=instruction_id))
    return back(request)


@login_required
@require_POST
def instruction_withdraw(request, instruction_id):
    planning.withdraw_instruction(request.user, get_object_or_404(Instruction, pk=instruction_id))
    messages.success(request, '批示已撤回。')
    return back(request)


@login_required
@require_POST
def instruction_feedback(request, instruction_id):
    """收件人给批示人反馈：已知悉 / 已落实 / 有困难 + 选填说明。"""
    try:
        planning.respond(request.user, get_object_or_404(Instruction, pk=instruction_id), request.POST.get('status'), request.POST.get('note'))
        messages.success(request, '反馈已发给批示人。')
    except RuleError as error:
        messages.error(request, f'没有发送：{error}')
    return back(request)


@login_required
def instruction_list(request):
    """批示记录（最高权限）：全部批示及每位收件人的反馈；上方是还没看过的新反馈。"""
    access.require_super(request.user)
    instructions = planning.with_read_counts(Instruction.objects.select_related('author', 'period', 'task', 'group', 'member').order_by('-created_at')[:100])
    return render(request, 'instructions.html', {'instructions': instructions, 'feedback': planning.new_feedback(request.user)})


@login_required
@require_POST
def feedback_seen(request):
    """批示人把新反馈标为已阅（单条或全部）。"""
    access.require_super(request.user)
    receipt = request.POST.get('receipt', '')
    planning.mark_feedback_seen(request.user, int(receipt) if receipt.isdigit() else None)
    return back(request)


@login_required
@require_POST
def set_mode(request):
    """切换查看 / 编辑模式（仅最高权限和组长）。模式只决定页面显示哪些操作，权限仍由服务端逐项检查。"""
    if not access.can_manage_any(request.user):
        raise PermissionDenied
    request.session['edit_mode'] = request.POST.get('mode') == 'edit'
    return back(request)


@login_required
def manage_home(request):
    if not access.can_manage_any(request.user):
        raise PermissionDenied
    return redirect('portal', period_for().pk)


@login_required
def editor(request, period_id):
    period = get_object_or_404(Period, pk=period_id)
    if not can_write(request.user, period) or period.start > timezone.localdate():
        raise PermissionDenied
    report = Report.objects.filter(user=request.user, period=period).select_related('latest').first()
    draft = Draft.objects.filter(report=report).first() if report else None
    # [(部分键, 部分名, [(小项键, 小项名, (说明, 要点示例, 详细阐述示例)), ...]), ...]
    layout = [(section, name, [(key, label, GUIDES[(section, key)]) for key, label in FIELDS.items()]) for section, name in SECTIONS.items()]
    return render(request, 'editor.html', {'period': period, 'report': report, 'payload': upgrade(draft.payload) if draft else empty_payload(), 'point_max': POINT_MAX, 'revision': draft.revision if draft else 0, 'threshold': site().min_chars, 'layout': layout, 'voluntary': is_voluntary(request.user, period), 'returns': report.returnevent_set.all() if report else []})


@login_required
@require_http_methods(['PUT', 'POST'])
def save(request, period_id, submit=False):
    if submit and request.method != 'POST':
        return JsonResponse({'error': '正式提交必须使用 POST'}, status=405)
    period = get_object_or_404(Period, pk=period_id)
    try:
        data = json.loads(request.body)
        revision = data['expected_revision']
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise RuleError('revision 必须是非负整数')
        token = data.get('idempotency_key') if submit else None
        if submit and not token:
            raise RuleError('正式提交缺少请求 token')
        draft, submission = write(request.user, period, data['payload'], revision, token)
        return JsonResponse({'revision': draft.revision, 'saved_at': draft.updated_at.isoformat(), 'submission_id': submission.pk if submission else None})
    except Conflict as error:
        return JsonResponse({'error': str(error)}, status=409)
    except (ValueError, KeyError, TypeError) as error:
        return JsonResponse({'error': str(error)}, status=400)


@login_required
def history(request, period_id):
    period = get_object_or_404(Period, pk=period_id)
    versions = Submission.objects.filter(report__period=period, report__user=request.user).order_by('-version')
    return render(request, 'history.html', {'period': period, 'versions': versions})


@login_required
def version(request, submission_id):
    submission = get_object_or_404(Submission.objects.select_related('report__user', 'report__period'), pk=submission_id)
    if not access.can_view(request.user, submission.report.user, submission.report.period):
        raise PermissionDenied
    return render(request, 'version.html', {'submission': submission, 'blocks': dashboard.report_blocks(submission)})


@login_required
def portal(request, period_id):
    scope = access.resolve_scope(request.user, request.GET.get('scope'))
    period = get_object_or_404(Period, pk=period_id)
    data = rows(period, scope)
    visible_ids = [r['user'].pk for r in data]
    required = [r for r in data if not r['exempt']]
    submitted = sum(bool(r['latest']) for r in required)
    for row in data:
        row['count_values'] = [row['counts'][f'{s}_{f}'] if row['counts'] else None for s in SECTIONS for f in FIELDS]
        row['research_total'] = sum(row['counts'][f'research_{f}'] for f in FIELDS) if row['counts'] else None
        row['project_total'] = sum(row['counts'][f'project_{f}'] for f in FIELDS) if row['counts'] else None
        row['last_mail'] = Mail.objects.filter(period=period, user=row['user'], kind='reminder').order_by('-updated_at').first()
        # 退回对所有小组生效；其他组长也要看到退回人和原因。
        row['open_return'] = ReturnEvent.objects.filter(report=row['report'], resolved__isnull=True).select_related('operator').order_by('-pk').first() if row['report'] and row['report'].returned else None
        if scope.key in ('all', 'ungrouped'):
            # 最高权限的“全部 / 未分组”视图操作全局免交。
            row['exempt_actions'] = [{'group': None, 'label': '全局', 'exempt': row['obligation'].exempt}]
        else:
            exempted = {g.pk for g in row['group_exempt']}
            row['exempt_actions'] = [{'group': g, 'label': g.name, 'exempt': g.pk in exempted} for g in row['relevant_groups']]
    state_filter = request.GET.get('state', '')
    if state_filter:
        if state_filter == '未交':
            data = [r for r in data if not r['latest'] and not r['exempt']]
        elif state_filter == '已提交':
            data = [r for r in data if r['latest']]
        elif state_filter == '免交':
            data = [r for r in data if r['exempt']]
        else:
            data = [r for r in data if state_filter in r['state']]
    teacher_rows = voluntary_rows(period, scope)  # 老师自愿写的周报单独列出，不计入上面的应交和提交率
    for row in teacher_rows:
        row['open_return'] = ReturnEvent.objects.filter(report=row['report'], resolved__isnull=True).select_related('operator').order_by('-pk').first() if row['report'].returned else None
    jobs = [job for job in ExportJob.objects.filter(period=period).select_related('creator').order_by('-pk')[:100] if access.can_access_job(request.user, job)][:20]
    mails = Mail.objects.filter(period=period, user_id__in=visible_ids).select_related('user', 'operator').order_by('-pk')[:30]
    return render(request, 'portal.html', {'period': period, 'periods': Period.objects.all(), 'scope': scope, 'scopes': access.scopes_for(request.user), 'is_super': access.is_super(request.user), 'rows': data, 'teacher_rows': teacher_rows, 'state_filter': state_filter, 'state_options': ['未交', '已提交', '逾期', '免交', '待修改'],'required': len(required), 'submitted': submitted, 'missing': len(required) - submitted, 'rate': f'{submitted / len(required):.0%}' if required else '—', 'columns': [f'{s}·{f}' for s in SECTIONS.values() for f in FIELDS.values()], 'jobs': jobs, 'mails': mails})


@login_required
@require_POST
def exemption(request, period_id, user_id):
    period = get_object_or_404(Period, pk=period_id)
    target = get_object_or_404(User, pk=user_id)
    group = request.POST.get('group', '')
    try:
        set_exemption(request.user, period, target, int(group) if group else None, request.POST.get('exempt') == '1', request.POST.get('reason', ''))
    except (RuleError, ValueError) as error:
        messages.error(request, str(error))
    return redirect(portal_url(period_id, request.POST.get('scope', '')))


@login_required
@require_POST
def return_view(request, period_id, user_id):
    period = get_object_or_404(Period, pk=period_id)
    target = get_object_or_404(User, pk=user_id)
    access.require_manage(request.user, target, period)
    try:
        return_report(request.user, period, target.pk, request.POST.get('reason', ''))
    except (RuleError, Report.DoesNotExist) as error:
        messages.error(request, str(error))
    else:
        messages.success(request, f'已退回 {target.name} 的周报。')
    return back(request, portal_url(period_id, request.POST.get('scope', '')))


@login_required
@require_http_methods(['GET', 'POST'])
def reminders(request, period_id):
    if not email_enabled():
        raise Http404
    scope = access.resolve_scope(request.user, request.POST.get('scope') or request.GET.get('scope'))
    period = get_object_or_404(Period, pk=period_id)
    config = site()
    can_edit = access.is_super(request.user)
    candidates = reminder_candidates(period, scope)
    subject = request.POST.get('subject', config.subject) if can_edit else config.subject
    body = request.POST.get('body', config.body) if can_edit else config.body
    if request.method == 'POST' and request.POST.get('action') != 'preview':
        try:
            queue_reminders(request.user, period, list(map(int, request.POST.getlist('users'))), request.POST['batch'], subject, body, scope)
            messages.success(request, '已排队；发送前再次校验，邮件状态见后台。')
            return redirect(portal_url(period_id, scope.key))
        except (ValueError, KeyError) as error:
            messages.error(request, str(error))
    recent = recent_reminders(period, [u.pk for u in candidates])
    chosen = set(map(int, request.POST.getlist('users'))) if request.method == 'POST' else None
    entries = []
    for user in candidates:
        mail = recent.get(user.pk)
        entries.append({'user': user, 'recent': mail, 'recent_label': access.scope_label(mail.scope, mail.operator) if mail else '', 'checked': user.pk in chosen if chosen is not None else not mail})
    return render(request, 'reminders.html', {'period': period, 'scope': scope, 'entries': entries, 'can_edit': can_edit, 'subject': subject, 'body': body, 'batch': uuid.uuid4(), 'previews': [(u, render_mail(body, u, period)) for u in candidates]})


def invite(user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    return f'{settings.PUBLIC_BASE_URL}/activate/{uid}/{token}/'


@login_required
def members(request):
    staff(request.user)
    if request.method == 'POST':
        try:
            action = request.POST.get('action')
            with transaction.atomic():
                if action == 'create':
                    user = accounts.create_member(request.user, request.POST.get('name', ''), request.POST.get('account', ''), request.POST.get('password', ''), request.POST.get('email', ''),
                                                  request.POST.get('identity') or User.STUDENT, request.POST.get('grade', ''), request.POST.get('topic', ''))
                    messages.success(request, f'已创建账号 {user.account}（{user.tag or "学生"}）；请把账号和密码告知本人。')
                elif action == 'identity':
                    target = get_object_or_404(User, pk=request.POST.get('user'))
                    was_teacher = target.is_teacher
                    user, removed = accounts.set_identity(request.user, target, request.POST.get('identity', ''), request.POST.get('grade', ''), request.POST.get('topic'))
                    if user.is_teacher and not was_teacher:
                        messages.success(request, f'{user.name} 已设为老师：周报改为选填、不计入提交率' + (f'；去掉了 {len(removed)} 个没交的周。' if removed else '。') + '已交的周报保留。')
                    elif was_teacher and not user.is_teacher:
                        due = timezone.localtime(first_deadline(obliged_from(user)))
                        messages.success(request, f'{user.name} 已改为{user.tag or "学生"}：从 {due.month}月{due.day}日 {due:%H:%M} 截止的那一周起需要写周报。')
                    else:
                        messages.success(request, f'{user.name} 的资料已保存：{user.tag or "学生"}' + (f' · 课题：{user.topic}' if user.topic else '') + '。')
                elif action == 'reset':
                    user = get_object_or_404(User, pk=request.POST.get('user'))
                    accounts.reset_password(request.user, user, request.POST.get('password', ''))
                    messages.success(request, f'已重置 {user.name}（{user.account}）的密码；该账号已登录的会话随之失效。')
                elif action == 'stop':
                    user = User.objects.select_for_update().get(pk=request.POST['user'])
                    period = Period.objects.get(pk=request.POST['period'])
                    if period.start <= period_for().start or user.stop_period_id:
                        raise RuleError('停用只允许指定未来周期；已设置的边界不得追溯改写')
                    user.stop_period = period
                    user.save(update_fields=['stop_period'])
                    audit(request.user, period, 'stop', user)
                    messages.success(request, '停用周期已保存。')
                elif action == 'sort':
                    ids = list(map(int, request.POST['order'].split(',')))
                    if set(ids) != set(User.objects.values_list('pk', flat=True)) or len(ids) != len(set(ids)):
                        raise RuleError('排序必须包含全部成员且不重复')
                    for index, user_id in enumerate(ids):
                        User.objects.filter(pk=user_id).update(sort_order=index)
                    audit(request.user, None, 'sort', request.user, order=ids)
                    messages.success(request, '排序已保存。')
                else:
                    raise RuleError('未知操作')
        except (ValueError, KeyError, ValidationError, IntegrityError) as error:
            messages.error(request, f'操作失败：{error}')
        return redirect('members')
    current = period_for()
    next_period = period_for(timezone.now() + timedelta(days=7))
    groups = access.memberships_at(current)
    users = list(User.objects.all())
    for user in users:
        user.current_groups = groups.get(user.pk, [])
    return render(request, 'members.html', {'users': users, 'future_periods': Period.objects.filter(start__gte=next_period.start), 'identities': User.IDENTITIES})


@login_required
def member_import(request):
    """批量导入（仅最高权限）：直接粘贴表格，或上传 Excel（模板或下载的成员表）。
    “检查”只校验不写入；“导入”全有或全无，按增量处理。自动生成的初始密码只在导入完成的这一页显示和下载。"""
    staff(request.user)
    report = credentials = None
    start = request.POST.get('start') or access.current_start().isoformat()
    text = request.POST.get('text', '')
    group = request.POST.get('group', '')
    group_id = int(group) if group.isdigit() else None
    if request.method == 'POST':
        dry_run = request.POST.get('action') != 'import'
        try:
            if request.POST.get('source') == 'text':
                report = accounts.import_text(request.user, text, start, dry_run, group_id)
            else:
                upload = request.FILES.get('file')
                if not upload:
                    raise RuleError('请选择要导入的 .xlsx 文件')
                if upload.size > 2 * 1024 * 1024:
                    raise RuleError('文件超过 2 MB，请检查是否选错文件')
                report = accounts.import_members(request.user, upload, start, dry_run, group_id)
            if report.ok and not report.dry_run:
                messages.success(request, f'导入完成：新建 {len(report.created)} 人，更新 {len(report.updated)} 人，没有变化 {len(report.skipped)} 人。')
                text = ''  # 已导入，清空粘贴框，免得重复提交
                if report.created:
                    credentials = {
                        'download': base64.b64encode(accounts.credentials_workbook(report.created)).decode(),
                        'text': '\n'.join(f"{c['name']}\t{c['account']}\t{c['password'] or '（表里已填）'}" for c in report.created),
                    }
        except RuleError as error:
            messages.error(request, str(error))
    return render(request, 'member_import.html', {'report': report, 'credentials': credentials, 'start': start, 'text': text, 'group_id': group_id,
                                                  'groups': Group.objects.filter(archived=False), 'columns': accounts.COLUMNS})


def _xlsx_response(content, filename):
    response = HttpResponse(content, content_type=accounts.XLSX)
    response['Content-Disposition'] = f"attachment; filename*=UTF-8''{quote(filename)}"
    return response


@login_required
def import_template(request):
    staff(request.user)
    return _xlsx_response(accounts.template_workbook(), '成员导入模板.xlsx')


@login_required
def member_roster(request):
    """下载成员表（仅最高权限）：当前全部组长和成员，表头与导入模板相同；加人、补年级和课题后可以直接导入。"""
    staff(request.user)
    return _xlsx_response(accounts.roster_workbook(), f'成员表-{timezone.localdate():%Y%m%d}.xlsx')


@login_required
def password_change(request):
    """本人修改密码：需输入原密码；成功后保持当前登录，其他设备上的会话失效。"""
    form = ChangeOwnPasswordForm(request.user, request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)
        audit(user, None, 'password_change', user)
        messages.success(request, '密码已修改。')
        return redirect('home')
    return render(request, 'form.html', {'form': form, 'title': '修改密码'})


@login_required
def groups(request):
    """分组维护：仅最高权限。生效周期按所选日期所在的周（周六至周五）计算。"""
    staff(request.user)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'create':
                grouping.create_group(request.user, request.POST.get('name', ''))
            elif action == 'rename':
                grouping.rename_group(request.user, get_object_or_404(Group, pk=request.POST.get('group')), request.POST.get('name', ''))
            elif action == 'archive':
                grouping.set_archived(request.user, get_object_or_404(Group, pk=request.POST.get('group')), request.POST.get('archived') == '1')
            elif action == 'add':
                grouping.add_membership(request.user, get_object_or_404(Group, pk=request.POST.get('group')), get_object_or_404(User, pk=request.POST.get('user')), request.POST.get('role', ''), request.POST.get('start', ''), request.POST.get('end') or None)
            elif action == 'end':
                grouping.end_membership(request.user, get_object_or_404(GroupMembership, pk=request.POST.get('membership')), request.POST.get('end') or None)
            elif action == 'role':
                grouping.change_role(request.user, get_object_or_404(GroupMembership, pk=request.POST.get('membership')), request.POST.get('at', ''))
            elif action == 'delete':
                grouping.delete_membership(request.user, get_object_or_404(GroupMembership, pk=request.POST.get('membership')))
            else:
                raise RuleError('未知操作')
            messages.success(request, '分组已更新并写入审计记录。')
        except RuleError as error:
            messages.error(request, f'操作失败：{error}')
        return redirect('groups')
    current = access.current_start()
    group_list = list(Group.objects.prefetch_related('memberships__user'))
    for group in group_list:
        group.records = list(group.memberships.all())
        for record in group.records:
            record.status = '未开始' if record.start > current else '已结束' if record.end and record.end <= current else '在组'
            record.last_week = record.end - timedelta(days=1) if record.end else None
    return render(request, 'groups.html', {'groups': group_list, 'users': User.objects.filter(is_staff=False),'current': current, 'roles': GroupMembership.ROLES})


def activate(request, uid, token):
    try:
        user = User.objects.get(pk=force_str(urlsafe_base64_decode(uid)))
    except (ValueError, User.DoesNotExist, UnicodeDecodeError):
        raise Http404
    checked_token = request.session.get('password_token', '') if token == 'set-password' else token
    if not default_token_generator.check_token(user, checked_token) or not active(user):
        return render(request, 'message.html', {'text': '链接已失效，请联系管理员或重新申请重置。'}, status=400)
    # 首次打开后立即把 token 移入会话，重定向到无 token 地址。
    if token != 'set-password':
        request.session['password_token'] = token
        return redirect('activate', uid, 'set-password')
    form = SetPasswordForm(user, request.POST or None)
    if request.method == 'POST' and form.is_valid():
        form.save()
        request.session.pop('password_token', None)
        return redirect('/login/')
    return render(request, 'form.html', {'form': form, 'title': '设置密码'})


def password_reset(request):
    if not email_enabled():
        return render(request, 'message.html', {'text': '忘记密码请联系课题组的最高权限管理员，在“成员管理”中为你重置密码。'})
    from django import forms
    class ResetForm(forms.Form):
        email = forms.EmailField(label='邮箱')
    form = ResetForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = User.objects.filter(email=form.cleaned_data['email'].strip().casefold()).exclude(email='').first()
        if user and active(user):
            Mail.objects.create(user=user, kind='reset', dedupe=f'reset:{uuid.uuid4()}', subject='重置周报账号密码', body=f'请在一小时内设置密码：{invite(user)}')
        return render(request, 'message.html', {'text': '若该邮箱可用，设置密码邮件将排队发送。'})
    return render(request, 'form.html', {'form': form, 'title': '重置密码'})


@login_required
def settings_view(request):
    staff(request.user)
    config = site()
    if request.method == 'POST':
        try:
            threshold = int(request.POST['min_chars'])
            if not 1 <= threshold <= 10000:
                raise RuleError('最低有效字符阈值范围 1—10000')
            subject, body = request.POST['subject'], request.POST['body']
            render_mail(subject, request.user, period_for())
            render_mail(body, request.user, period_for())
            if '\n' in subject or '\r' in subject:
                raise RuleError('邮件主题不能换行')
            config.title = request.POST['title']
            config.min_chars, config.subject, config.body = threshold, subject, body
            config.save()
            audit(request.user, None, 'settings', config, min_chars=threshold)
            messages.success(request, '配置已保存；已交版本不受阈值变化影响。')
        except (ValueError, KeyError) as error:
            messages.error(request, str(error))
    import shutil
    disk = shutil.disk_usage(settings.BASE_DIR)
    backup = settings.BASE_DIR / 'private' / 'backup-success.txt'
    return render(request, 'settings.html', {'config': config, 'free_gb': round(disk.free / 1024**3, 1), 'queued': ExportJob.objects.filter(state='queued').count(), 'mail_queued': Mail.objects.filter(state='queued').count(), 'backup_status': backup.read_text() if backup.exists() else None})


@login_required
@require_POST
def mail_retry(request, mail_id):
    mail = get_object_or_404(Mail, pk=mail_id)
    if not (access.is_super(request.user) or mail.period_id and access.can_manage(request.user, mail.user, mail.period)):
        raise PermissionDenied
    if mail.state == 'failed' or mail.state == 'uncertain' and request.POST.get('verified') == '1':
        Mail.objects.filter(pk=mail.pk).update(state='queued', error='')
        audit(request.user, mail.period, 'mail_retry', mail, previous_state=mail.state)
    return redirect(portal_url(mail.period_id, request.POST.get('scope', ''))) if mail.period_id else redirect('settings')


@login_required
@require_POST
def mail_test(request):
    staff(request.user)
    if not email_enabled():
        raise Http404
    Mail.objects.create(user=request.user, kind='test', dedupe=f'test:{uuid.uuid4()}', subject='周报系统测试邮件', body='这是管理员主动发起的邮件测试，不包含科研正文。')
    messages.success(request, '测试邮件已排队，仅发送给当前管理员账号。')
    return redirect('settings')


@login_required
@require_POST
def export_view(request, period_id):
    scope = access.resolve_scope(request.user, request.POST.get('scope'))
    period = get_object_or_404(Period, pk=period_id)
    try:
        job = create_export(request.user, period, list(map(int, request.POST.getlist('users'))), scope)
        return redirect('job', job.pk)
    except ValueError as error:
        messages.error(request, str(error))
        return redirect(portal_url(period_id, scope.key))


def accessible_job(request, job_id):
    job = get_object_or_404(ExportJob, pk=job_id)
    if not access.can_access_job(request.user, job):
        raise PermissionDenied
    return job


@login_required
def job_view(request, job_id):
    return render(request, 'job.html', {'job': accessible_job(request, job_id)})


@login_required
def download(request, job_id, format):
    job = accessible_job(request, job_id)
    if format not in ('docx', 'pdf') or job.state != 'done':
        raise Http404
    path = settings.BASE_DIR / 'private' / 'exports' / str(job.pk) / f'report.{format}'
    if not path.is_file():
        raise Http404
    return FileResponse(path.open('rb'), as_attachment=True, filename=f'weekly-{job.period_id}-{job.pk}.{format}')


@login_required
@require_POST
def retry_export(request, job_id):
    job = accessible_job(request, job_id)
    ExportJob.objects.filter(pk=job.pk, state='failed').update(state='queued', error='')
    return redirect('job', job.pk)


@login_required
@require_POST
def xlsx(request, period_id):
    scope = access.resolve_scope(request.user, request.POST.get('scope'))
    from .statistics import workbook_snapshot
    period = get_object_or_404(Period, pk=period_id)
    try:
        blob = workbook_snapshot(period, list(map(int, request.POST.getlist('users'))), request.POST.getlist('types'), request.POST.getlist('columns'), scope)
    except ValueError as error:
        messages.error(request, str(error))
        return redirect(portal_url(period_id, scope.key))
    from django.http import HttpResponse
    response = HttpResponse(blob, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="statistics-{period.pk}.xlsx"'
    return response
