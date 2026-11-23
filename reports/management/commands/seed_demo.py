# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
import os
import uuid
from datetime import timedelta
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from reports.domain import empty_payload
from reports.models import Group, GroupMembership, Instruction, Obligation, Task, User
from reports import planning
from reports.services import create_export, ensure_obligations, period_for, return_report, write

def sample_payload(long=False):
    """虚构周报（第 2 版：要点 + 详细阐述），覆盖多条要点与长文续页。"""
    detail = '本周完成虚构研究流程的验证，记录了不同条件下的结果，并整理出下一步计划。'
    if long:
        detail += '\n' + '\n'.join(f'第{n}段：这是用于检验自然续页的虚构长文，包含中文 English 123 和 <script>纯文本</script>。' for n in range(1, 75))
    entry = lambda point, text: {'point': point, 'detail': text}
    return {
        'research': {
            'work': {'state': 'ITEMS', 'items': [entry('完成虚构研究流程的验证', detail), entry('整理实验数据并复核异常值', '按虚构的检查清单逐项复核了三批数据，剔除两处录入错误，复核记录已归档。')]},
            'results': {'state': 'ITEMS', 'items': [entry('得到三组条件下的对比结果', '三组虚构条件下的指标差异明显，第二组效果最好，具体数值见组内共享表格。')]},
            'issues': {'state': 'NONE', 'items': []},
            'plan': {'state': 'ITEMS', 'items': [entry('下周补做对照实验', '计划在下周完成两组对照实验，并与本周结果合并分析，周五前形成小结。')]},
        },
        'project': {key: {'state': 'NONE', 'items': []} for key in ('work', 'results', 'issues', 'plan')},
    }


NAMES = ['虚构最高权限', '虚构长文成员', '虚构暂无成员', '虚构待修改成员', '虚构逾期成员', '虚构免交成员', '虚构草稿成员']
TEACHER = ('demo7', '虚构指导老师')  # 老师身份示例
# demo1—demo6 的身份标签：(身份, 入学年份) → “2023级博士”“2024级硕士”……
PROFILES = {1: (User.DOCTOR, 2023), 2: (User.MASTER, 2024), 3: (User.MASTER, 2023), 4: (User.DOCTOR, 2022), 5: (User.MASTER, 2025), 6: (User.MASTER, 2024)}
TOPICS = {1: '虚构课题：图像分类', 2: '虚构课题：文献综述与数据集', 3: '虚构课题：文本分类', 4: '虚构课题：数据增强', 5: '虚构课题：模型压缩', 6: '虚构课题：数据标注工具'}


class Command(BaseCommand):
    help = '显式开发环境虚构示例；不重置现有账号。所有示例账号的口令从 DEV_DEMO_PASSWORD 环境读取'

    def add_arguments(self, parser):
        parser.add_argument('--tasks-only', action='store_true', help='只给已有的示例账号补充任务、里程碑和批示样例')

    def handle(self, *args, **options):
        accounts = [f'demo{index}' for index in range(len(NAMES))]
        if options['tasks_only']:
            if not settings.DEBUG:
                raise CommandError('仅开发环境允许')
            users = [User.objects.filter(account=account).first() for account in accounts]
            groups = [Group.objects.filter(name=name).first() for name in ('虚构课题A组', '虚构课题B组')]
            if not all(users) or not all(groups):
                raise CommandError('没有找到示例账号或示例小组，请先运行 seed_demo')
            if Task.objects.filter(group__in=groups).exists():
                raise CommandError('示例小组已有任务，拒绝重复添加')
            self.seed_tasks(users, *groups, period_for())
            self.stdout.write('已补充任务、里程碑和批示样例')
            return
        if not settings.DEBUG or not os.environ.get('DEV_DEMO_PASSWORD'):
            raise CommandError('仅开发环境允许，且须显式设置 DEV_DEMO_PASSWORD')
        if User.objects.filter(account__in=accounts + [TEACHER[0]]).exists():
            raise CommandError('已有示例账号，拒绝覆盖')
        now = timezone.now()
        period = period_for(now)
        created = period.deadline - timedelta(days=8)
        # demo0 是最高权限（只做管理、不交周报）；demo1—demo6 是成员。
        users = []
        for index, (account, name) in enumerate(zip(accounts, NAMES)):
            identity, grade = PROFILES.get(index, (User.STUDENT, None))
            users.append(User.objects.create_user(account, os.environ['DEV_DEMO_PASSWORD'], name=name, email=f'{account}@example.invalid', is_staff=index == 0,
                                                  sort_order=index, created_at=created, identity=identity, grade=grade, topic=TOPICS.get(index, '')))
        # 虚构分组：长文成员带 A 组，逾期成员带 B 组，待修改成员同时在两组（跨组）。
        start = period.start - timedelta(days=7)
        group_a, _ = Group.objects.get_or_create(name='虚构课题A组', defaults={'sort_order': 1})
        group_b, _ = Group.objects.get_or_create(name='虚构课题B组', defaults={'sort_order': 2})
        for index, group, role in [(1, group_a, 'leader'), (2, group_a, 'member'), (3, group_a, 'member'), (4, group_b, 'leader'), (3, group_b, 'member'), (5, group_b, 'member'), (6, group_b, 'member')]:
            GroupMembership.objects.create(user=users[index], group=group, role=role, start=start)
        # demo7：老师身份（周报选填），同时是 A、B 两组的组长
        teacher = User.objects.create_user(TEACHER[0], os.environ['DEV_DEMO_PASSWORD'], name=TEACHER[1], email=f'{TEACHER[0]}@example.invalid', identity=User.TEACHER, sort_order=len(NAMES), created_at=created)
        for group in (group_a, group_b):
            GroupMembership.objects.create(user=teacher, group=group, role='leader', start=start)
        ensure_obligations(period)
        ontime = min(now, period.deadline - timedelta(seconds=1))
        for index in range(1, 5):
            payload = empty_payload()
            for section in ('research', 'project'):
                for field in ('work', 'results', 'issues', 'plan'):
                    payload[section][field] = {'state': 'NONE', 'items': []}
            if index != 2:
                payload.update(sample_payload(long=index == 1))
            write(users[index], period, payload, 0, uuid.uuid4(), period.deadline + timedelta(seconds=1) if index == 4 else ontime)
        return_report(users[1], period, users[3].pk, '虚构退回原因：请补充下周验证计划')
        Obligation.objects.filter(user=users[5], period=period).update(exempt=True, reason='虚构单周免交')
        write(users[6], period, empty_payload(), 0, now=ontime)
        job = create_export(users[0], period, [u.pk for u in users])
        self.seed_tasks(users, group_a, group_b, period)
        self.stdout.write(f'虚构示例已创建；周期 {period.pk}，合订任务 {job.pk}；最高权限账号 demo0，组长 demo1（A 组）、demo4（B 组），老师 demo7（A、B 两组组长，周报选填），成员 demo2、demo3、demo5、demo6')

    def seed_tasks(self, users, group_a, group_b, period):
        """虚构任务：进行中、已完成、延期各有，外加里程碑、个人任务和三类批示，便于看板和甘特图有内容可看。"""
        today = timezone.localdate()
        day = lambda offset: today + timedelta(days=offset)
        admin, lead_a, lead_b = users[0], users[1], users[4]
        def group_task(leader, group, title, start, end, assignees, progress):
            task = planning.create_group_task(leader, group, title, day(start), day(end), assignee_ids=[u.pk for u in assignees])
            Task.objects.filter(pk=task.pk).update(progress=progress)
            return task
        collect = group_task(lead_a, group_a, '数据采集与清洗', -21, 14, [users[2], users[3]], 60)
        group_task(lead_a, group_a, '论文初稿', -7, 35, [lead_a], 20)
        group_task(lead_a, group_a, '实验平台搭建', -56, -7, [users[2]], 80)
        planning.create_group_task(lead_a, group_a, '中期检查', day(12), is_milestone=True)
        group_task(lead_b, group_b, '模型训练', -14, 28, [users[3], users[5]], 35)
        group_task(lead_b, group_b, '数据标注', -35, -2, [users[6]], 100)
        planning.create_group_task(lead_b, group_b, '论文投稿', day(40), is_milestone=True)
        review = planning.create_personal_task(users[2], '文献综述', day(-10), day(20))
        Task.objects.filter(pk=review.pk).update(progress=40)
        planning.create_personal_task(users[3], '准备组会报告', day(0), day(5))
        planning.create_personal_task(users[3], '提交组会材料', day(5), is_milestone=True)
        planning.create_instruction(admin, Instruction.REPORT, '本周八项全部填了“暂无”，下周请写清楚实验进展。（虚构批示）', member=users[2], period=period)
        planning.create_instruction(admin, Instruction.TASK, '数据采集请在中期检查前完成。（虚构批示）', task=collect)
        planning.create_instruction(admin, Instruction.GROUP, 'B 组本月重点推进模型训练，周五组会汇报。（虚构批示）', group=group_b)
