# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager):
    use_in_migrations = True

    def get_by_natural_key(self, account):
        # 登录账号不区分大小写：DemoAdmin 与 demoadmin 是同一个账号。
        return self.get(account__iexact=(account or '').strip())

    def create_user(self, account, password=None, **extra):
        user = self.model(account=account.strip(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, account, password, **extra):
        return self.create_user(account, password, is_staff=True, is_superuser=True, **extra)


class User(AbstractUser):
    """登录凭“账号 + 密码”。账号是任意不含空白的字符串（学号、拼音、邮箱均可），不区分大小写唯一；邮箱选填。
    身份：硕士、博士、学生（不分硕博）要交周报；老师周报选填（可以当组长，查看本组），不进入任何一周的应交名单，不计入提交率。
    标签：老师 / 2023级硕士 / 2021级博士（年级 = 入学年份）。"""
    MASTER, DOCTOR, TEACHER, STUDENT = 'master', 'doctor', 'teacher', 'student'
    IDENTITIES = [(MASTER, '硕士'), (DOCTOR, '博士'), (TEACHER, '老师'), (STUDENT, '学生（不分硕博）')]
    DEGREES = {MASTER: '硕士', DOCTOR: '博士'}
    username = None
    account = models.CharField('账号', max_length=150, unique=True)
    email = models.EmailField('邮箱', blank=True)
    display_email = models.EmailField(blank=True)
    name = models.CharField('姓名', max_length=150)
    identity = models.CharField('身份', max_length=10, choices=IDENTITIES, default=STUDENT)
    grade = models.PositiveSmallIntegerField('年级（入学年份）', null=True, blank=True)
    topic = models.CharField('课题名称', max_length=200, blank=True)  # 个人课题，显示在成员阅览和小组页组员列表
    # 最近一次在老师和学生之间切换的时间。老师改回学生时，从这一刻起按新账号的规则起算应交周。
    identity_changed_at = models.DateTimeField('身份变更时间', null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    sort_order = models.PositiveIntegerField(default=0)
    stop_period = models.ForeignKey('Period', null=True, blank=True, on_delete=models.PROTECT)
    USERNAME_FIELD = 'account'
    REQUIRED_FIELDS = ['name']
    objects = UserManager()

    class Meta:
        ordering = ['sort_order', 'pk']
        constraints = [
            models.UniqueConstraint(models.functions.Lower('account'), name='account_case_unique'),
            models.UniqueConstraint(models.functions.Lower('email'), condition=~models.Q(email=''), name='email_case_unique'),
        ]

    @property
    def is_teacher(self):
        return self.identity == self.TEACHER

    @classmethod
    def make_tag(cls, identity, grade):
        """身份标签：老师 / 2023级硕士 / 2021级博士；不分硕博的学生有年级时为“2023级”，都没填时为空。"""
        if identity == cls.TEACHER:
            return '老师'
        kind = cls.DEGREES.get(identity, '')
        return f'{grade}级{kind}' if grade else kind

    @property
    def tag(self):
        return '' if self.is_staff else self.make_tag(self.identity, self.grade)

    @property
    def name_with_title(self):
        """列组长时用：老师显示为“王伟老师”；名字本身已以“老师”结尾的不再重复。"""
        return f'{self.name}老师' if self.is_teacher and not self.name.endswith('老师') else self.name

    def save(self, *args, **kwargs):
        self.account = self.account.strip()
        if self.email and not self.display_email:
            self.display_email = self.email.strip()
        self.email = self.email.strip().casefold()
        super().save(*args, **kwargs)


class Period(models.Model):
    start = models.DateField(unique=True)
    end = models.DateField()
    deadline = models.DateTimeField()

    class Meta:
        ordering = ['-start']

    def __str__(self):
        return f'{self.start} 至 {self.end}'


class Obligation(models.Model):
    user = models.ForeignKey(User, on_delete=models.PROTECT)
    period = models.ForeignKey(Period, on_delete=models.PROTECT)
    exempt = models.BooleanField(default=False)
    reason = models.TextField(blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'period'], name='one_obligation')]


class Report(models.Model):
    user = models.ForeignKey(User, on_delete=models.PROTECT)
    period = models.ForeignKey(Period, on_delete=models.PROTECT)
    first_submitted_at = models.DateTimeField(null=True)
    latest = models.ForeignKey('Submission', null=True, on_delete=models.PROTECT, related_name='+')
    returned = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'period'], name='one_report')]


class Draft(models.Model):
    report = models.OneToOneField(Report, on_delete=models.PROTECT)
    payload = models.JSONField(default=dict)
    revision = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)


class Submission(models.Model):
    report = models.ForeignKey(Report, on_delete=models.PROTECT, related_name='submissions')
    version = models.PositiveIntegerField()
    payload = models.JSONField()
    counts = models.JSONField()
    threshold = models.PositiveIntegerField()
    submitted_at = models.DateTimeField()
    token = models.UUIDField()
    digest = models.CharField(max_length=64)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['report', 'version'], name='one_version'), models.UniqueConstraint(fields=['report', 'token'], name='one_submit_token')]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError('正式版本不可修改')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError('正式版本不可删除')


class ReturnEvent(models.Model):
    report = models.ForeignKey(Report, on_delete=models.PROTECT)
    submission = models.ForeignKey(Submission, on_delete=models.PROTECT)
    reason = models.TextField()
    operator = models.ForeignKey(User, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)
    resolved = models.ForeignKey(Submission, null=True, on_delete=models.PROTECT, related_name='+')


class SiteSettings(models.Model):
    title = models.CharField(max_length=150, default='课题组周报')
    min_chars = models.PositiveIntegerField(default=20)
    subject = models.TextField(default='【{{站点名}}】{{报告周期}}周报提交提醒')
    body = models.TextField(default='{{姓名}}，您好：请在 {{截止时间}} 前提交 {{报告周期}} 周报。填写入口：{{填写入口}}。仅暂存草稿不算提交。')


class Audit(models.Model):
    operator = models.ForeignKey(User, null=True, on_delete=models.PROTECT)
    period = models.ForeignKey(Period, null=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=50)
    object_id = models.CharField(max_length=100)
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)


class Mail(models.Model):
    period = models.ForeignKey(Period, null=True, on_delete=models.PROTECT)
    user = models.ForeignKey(User, on_delete=models.PROTECT)
    kind = models.CharField(max_length=20)
    dedupe = models.CharField(max_length=160, unique=True)
    subject = models.TextField()
    body = models.TextField()
    state = models.CharField(max_length=30, default='queued')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    error = models.CharField(max_length=100, blank=True)
    # 催交的发起人与发起范围（all / ungrouped / group:<id> / led）；worker 发送前按同一范围复查。
    operator = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    scope = models.CharField(max_length=40, blank=True)


class Group(models.Model):
    """扁平小组；只归档不删除，历史分组关系与统计口径保留。"""
    name = models.CharField(max_length=100, unique=True)
    sort_order = models.PositiveIntegerField(default=0)
    archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['sort_order', 'pk']

    def __str__(self):
        return self.name


class GroupMembership(models.Model):
    """成员在 [start, end) 周期内属于小组；start/end 均为周期起始的周六，end 为空表示至今。

    组长记录本身即组员身份。同一成员在同一小组的区间不得重叠（服务层加锁校验）。
    """
    MEMBER, LEADER = 'member', 'leader'
    ROLES = [(MEMBER, '组员'), (LEADER, '组长')]
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name='memberships')
    group = models.ForeignKey(Group, on_delete=models.PROTECT, related_name='memberships')
    role = models.CharField(max_length=10, choices=ROLES, default=MEMBER)
    start = models.DateField()
    end = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['group__sort_order', 'group_id', 'user__sort_order', 'user_id', 'start']
        indexes = [models.Index(fields=['group', 'start']), models.Index(fields=['user', 'group'])]
        constraints = [models.CheckConstraint(condition=models.Q(end__isnull=True) | models.Q(end__gt=models.F('start')), name='membership_end_after_start')]


class GroupExemption(models.Model):
    """按组免交：只影响该小组口径的应交、统计与催交，不阻止填写。"""
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name='+')
    period = models.ForeignKey(Period, on_delete=models.PROTECT)
    group = models.ForeignKey(Group, on_delete=models.PROTECT)
    reason = models.TextField(blank=True)
    operator = models.ForeignKey(User, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'period', 'group'], name='one_group_exemption')]


class ExportJob(models.Model):
    period = models.ForeignKey(Period, on_delete=models.PROTECT)
    creator = models.ForeignKey(User, on_delete=models.PROTECT)
    manifest = models.JSONField()
    state = models.CharField(max_length=20, default='queued')
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)


class Task(models.Model):
    """任务与里程碑。

    - 小组任务：group 非空、owner 为空，可指定多名负责人；组长和最高权限维护，负责人更新进度。
    - 个人任务：owner 非空、group 为空；本人维护，其组长和最高权限也可修改。
    - 里程碑：is_milestone=True，start == end，只有一个日期，不计进度。
    完成与否以进度 100% 为准，不另设状态字段。
    """
    GROUP, PERSONAL = 'group', 'personal'
    KINDS = [(GROUP, '小组任务'), (PERSONAL, '个人任务')]
    kind = models.CharField(max_length=10, choices=KINDS)
    group = models.ForeignKey(Group, null=True, blank=True, on_delete=models.PROTECT, related_name='tasks')
    owner = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name='personal_tasks')
    title = models.CharField('名称', max_length=200)
    start = models.DateField()
    end = models.DateField()
    progress = models.PositiveSmallIntegerField(default=0)
    is_milestone = models.BooleanField(default=False)
    assignees = models.ManyToManyField(User, blank=True, related_name='assigned_tasks')
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['start', 'end', 'pk']
        constraints = [
            models.CheckConstraint(condition=models.Q(end__gte=models.F('start')), name='task_end_not_before_start'),
            models.CheckConstraint(condition=models.Q(progress__lte=100), name='task_progress_max_100'),
            models.CheckConstraint(condition=models.Q(kind='group', group__isnull=False, owner__isnull=True) | models.Q(kind='personal', group__isnull=True, owner__isnull=False), name='task_kind_matches_owner'),
            models.CheckConstraint(condition=models.Q(is_milestone=False) | models.Q(start=models.F('end')), name='milestone_single_day'),
        ]

    def __str__(self):
        return self.title


class Instruction(models.Model):
    """批示：只有最高权限能写；写在某人某周的周报、某个任务或某个小组上（§7）。

    收件人在写批示的那一刻确定（InstructionReceipt），之后分组变动不影响。
    subject 保存写批示时的对象说明，任务被删除后历史仍然可读。
    """
    REPORT, TASK, GROUP = 'report', 'task', 'group'
    TARGETS = [(REPORT, '周报'), (TASK, '任务'), (GROUP, '小组')]
    author = models.ForeignKey(User, on_delete=models.PROTECT, related_name='+')
    target = models.CharField(max_length=10, choices=TARGETS)
    member = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    period = models.ForeignKey(Period, null=True, blank=True, on_delete=models.PROTECT)
    task = models.ForeignKey(Task, null=True, blank=True, on_delete=models.SET_NULL, related_name='instructions')
    group = models.ForeignKey(Group, null=True, blank=True, on_delete=models.PROTECT, related_name='instructions')
    subject = models.CharField(max_length=300)
    text = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)
    withdrawn_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', '-pk']


class InstructionReceipt(models.Model):
    """批示的收件人：已读时间，以及给批示人的反馈（状态 + 说明，可修改）。"""
    FEEDBACK = [('noted', '已知悉'), ('done', '已落实'), ('blocked', '有困难')]
    instruction = models.ForeignKey(Instruction, on_delete=models.CASCADE, related_name='receipts')
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name='instruction_receipts')
    read_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=FEEDBACK, blank=True)
    note = models.TextField(blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    seen_by_author = models.BooleanField(default=True)  # 有新反馈时为 False，批示人看过后改回 True

    class Meta:
        constraints = [models.UniqueConstraint(fields=['instruction', 'user'], name='one_receipt_per_user')]
