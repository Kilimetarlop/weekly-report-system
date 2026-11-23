# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
"""账号管理（仅最高权限）：单个创建、改身份、重置密码、批量导入、下载成员表。

- 账号与密码由最高权限统一设置，是正式密码，不强制首次修改；成员可在“修改密码”页自行修改。
- 密码执行系统规则：至少 8 位、不是常见密码、不是纯数字、不与账号 / 姓名 / 邮箱过于相似。
- 批量导入（直接粘贴表格或上传 Excel）是**增量**的，并且“全有或全无”：
  - 新账号新建：没填账号就用姓名，没填密码就自动生成（只在导入完成的那一页显示，可下载）；
  - 已有账号不重复建，不改密码、姓名和分组；表里填了的年级身份、课题名称和系统不同时才更新；
  - 任何一行有错，一行都不写入。
- 下载的成员表与导入模板同一套表头：可以在后面加人、补年级和课题，再导入。
- 明文密码只在内存中参与校验和哈希，不写日志、审计或会话。
"""
import os
import re
import secrets
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from io import BytesIO
from django.contrib.auth.hashers import make_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from . import access, grouping
from .domain import RuleError
from .models import Group, GroupMembership, Obligation, Period, Report, User
from .security import clear_login_lock
from .services import audit, period_for

ACCOUNT_PATTERN = re.compile(r'\S{1,150}')
GROUP_SEPARATORS = re.compile(r'[、,，;；/\s]+')
MAX_ROWS = 1000
MAX_TEXT = 200_000  # 粘贴内容上限（字符）
TOPIC_MAX = 200
# 导入模板、下载的成员表、没有表头的粘贴内容，都用这一套列和顺序；只有“姓名”必填。
COLUMNS = ['姓名', '年级', '课题名称', '所在小组', '担任组长的小组', '账号', '密码', '邮箱（选填）']
FIELDS = ['name', 'grade', 'topic', 'groups', 'leads', 'account', 'password', 'email']
# 表头 → 字段：认几种常见写法，不认识的列忽略。“身份”是上一版模板的单独一列，仍然支持。
HEADERS = {
    'name': ('姓名',),
    'grade': ('年级', '年级身份', '年级（入学年份）'),
    'identity': ('身份',),
    'topic': ('课题名称', '课题', '研究课题', '研究方向'),
    'groups': ('所在小组', '小组'),
    'leads': ('担任组长的小组',),
    'account': ('账号',),
    'password': ('密码', '初始密码'),
    'email': ('邮箱（选填）', '邮箱'),
}
# 身份可以写的字；不填按“学生（不分硕博）”。老师周报选填。
IDENTITY_WORDS = {'': User.STUDENT, '学生': User.STUDENT, '硕士': User.MASTER, '硕士生': User.MASTER, '博士': User.DOCTOR, '博士生': User.DOCTOR, '老师': User.TEACHER, '教师': User.TEACHER}
GRADE_PATTERN = re.compile(r'(\d{4})级?')
COMBINED = re.compile(r'(\d{4})\s*级?\s*(\S*)')  # “2026级硕士”“2026 博士”“2026级”“2026”
PASSWORD_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789'  # 去掉 0/O、1/l/I 等易混字符
XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def parse_grade(value, identity):
    """年级 = 入学年份（2023 或 2023级）。硕士、博士必填；老师不需要（填了也不保存）。返回 (年份或 None, 问题或 '')。"""
    text = str(value if value is not None else '').strip()
    if identity == User.TEACHER:
        return None, ''
    if not text:
        return None, ('硕士、博士要填年级（入学年份，如 2023）' if identity in User.DEGREES else '')
    match = GRADE_PATTERN.fullmatch(text)
    year = int(match[1]) if match else 0
    if not 1990 <= year <= timezone.localdate().year + 1:
        return None, f'年级要写入学年份，如 2023：{text[:20]}'
    return year, ''


def parse_identity(identity_text, grade_text):
    """“身份”“年级”两列合起来解析，返回 (身份, 年份, 问题)。
    年级一列可以直接写“2026级硕士”“2021级博士”“老师”；也可以像上一版模板那样“身份”写硕士、“年级”写 2026。"""
    identity_text = (identity_text or '').strip()
    grade_text = str(grade_text if grade_text is not None else '').strip()
    wrong = f'年级写成“2026级硕士”“2021级博士”或“老师”：{grade_text[:20]}'
    match = COMBINED.fullmatch(grade_text)
    if match:
        year_text, word = match[1], match[2]
    elif grade_text in IDENTITY_WORDS:
        year_text, word = '', grade_text
    elif identity_text:
        year_text, word = grade_text, ''  # 身份单独一列时，年级一列按年份检查
    else:
        return None, None, wrong
    if word and word not in IDENTITY_WORDS:
        return None, None, wrong
    if word and identity_text and IDENTITY_WORDS.get(identity_text) != IDENTITY_WORDS[word]:
        return None, None, f'“身份”和“年级”对不上：{identity_text[:10]}、{grade_text[:20]}'
    identity = IDENTITY_WORDS.get(identity_text or word)
    if identity is None:
        return None, None, f'身份只能填 硕士、博士、老师 或 学生（不填按学生）：{identity_text[:20]}'
    year, problem = parse_grade(year_text, identity)
    return identity, year, problem


def generate_password():
    """自动生成的初始密码：10 个字母和数字（含大小写和数字），中间用短横分开，便于转告。"""
    while True:
        text = ''.join(secrets.choice(PASSWORD_ALPHABET) for _ in range(10))
        if any(c.isupper() for c in text) and any(c.islower() for c in text) and any(c.isdigit() for c in text):
            return f'{text[:5]}-{text[5:]}'


def password_problems(password, account, name, email=''):
    """按系统密码规则检查，返回中文问题列表（空列表表示通过）。"""
    try:
        validate_password(password, User(account=account, name=name, email=email))
    except ValidationError as error:
        return list(error.messages)
    return []


def _check_fields(name, account, email):
    problems = []
    if not name:
        problems.append('姓名为空')
    elif len(name) > 150:
        problems.append('姓名超过 150 个字符')
    if not ACCOUNT_PATTERN.fullmatch(account):
        problems.append('账号为空、含空格或超过 150 个字符')
    if email:
        try:
            validate_email(email)
        except ValidationError:
            problems.append(f'邮箱格式不正确：{email}')
    return problems


def _clean_topic(text):
    return ' '.join((text or '').split())


def _next_order():
    return (User.objects.aggregate(m=Max('sort_order'))['m'] or 0) + 1


@transaction.atomic
def create_member(operator, name, account, password, email='', identity=User.STUDENT, grade='', topic=''):
    access.require_super(operator)
    name, account, email, topic = (name or '').strip(), (account or '').strip(), (email or '').strip(), _clean_topic(topic)
    problems = _check_fields(name, account, email)
    year = None
    if identity not in dict(User.IDENTITIES):
        problems.append('身份只能是硕士、博士、老师或学生')
    else:
        year, problem = parse_grade(grade, identity)
        problems += [problem] if problem else []
    if len(topic) > TOPIC_MAX:
        problems.append(f'课题名称超过 {TOPIC_MAX} 个字')
    if not problems and User.objects.filter(account__iexact=account).exists():
        problems.append('账号已存在')
    if email and User.objects.filter(email__iexact=email).exists():
        problems.append('邮箱已被其他账号使用')
    problems += password_problems(password, account, name, email) if password else ['密码为空']
    if problems:
        raise RuleError('；'.join(problems))
    user = User.objects.create_user(account, password, name=name, email=email, identity=identity, grade=year, topic=topic, sort_order=_next_order())
    audit(operator, None, 'create_member', user, account=account, identity=identity, grade=year, topic=topic)
    return user


@transaction.atomic
def set_identity(operator, user, identity, grade='', topic=None):
    """改身份、年级（topic 不为 None 时连同课题名称）（仅最高权限）。返回从应交名单里去掉的周（文字列表）。
    - 硕士、博士、学生之间改：只改标签，应交名单不变。
    - 设为老师：周报改为选填；本周及以往“没有正式提交”的应交记录一并去掉（老师不算未交），已交的周报原样保留。
    - 老师改回学生：从改的这一刻起按新账号的规则起算（本周截止前改，本周就要交），不补以往各周。
    """
    access.require_super(operator)
    if identity not in dict(User.IDENTITIES):
        raise RuleError('身份只能是硕士、博士、老师或学生')
    year, problem = parse_grade(grade, identity)
    if problem:
        raise RuleError(problem)
    topic = None if topic is None else _clean_topic(topic)
    if topic and len(topic) > TOPIC_MAX:
        raise RuleError(f'课题名称超过 {TOPIC_MAX} 个字')
    user = User.objects.select_for_update().get(pk=user.pk)  # 与成员提交周报（同样先锁成员）串行
    if user.is_staff:
        raise RuleError('最高权限账号只做管理、不交周报，不需要设置身份')
    fields, removed = [], []
    if (user.identity, user.grade) != (identity, year):
        fields += ['identity', 'grade']
        if (identity == User.TEACHER) != user.is_teacher:  # 在老师和学生之间切换，才影响应交名单
            if identity == User.TEACHER:
                submitted = Report.objects.filter(user=user, latest__isnull=False).values('period_id')
                stale = Obligation.objects.filter(user=user).exclude(period_id__in=submitted)
                removed = [str(p) for p in Period.objects.filter(obligation__in=stale).order_by('start')]
                stale.delete()
            user.identity_changed_at = timezone.now()
            fields.append('identity_changed_at')
        user.identity, user.grade = identity, year
    if topic is not None and topic != user.topic:
        user.topic = topic
        fields.append('topic')
    if not fields:
        return user, []
    user.save(update_fields=fields)
    audit(operator, None, 'identity', user, identity=user.identity, grade=user.grade, topic=user.topic, removed_weeks=removed)
    return user, removed


@transaction.atomic
def set_topic(operator, user, topic):
    """改课题名称：最高权限，或该成员当前所在小组的组长（2026-11-17 起下放给组长）。
    空着保存就是清空。返回 (成员, 是否有改动)；有改动才写审计，记下改前和改后。"""
    if not access.can_set_topic(operator, user):
        raise PermissionDenied
    topic = _clean_topic(topic)
    if len(topic) > TOPIC_MAX:
        raise RuleError(f'课题名称超过 {TOPIC_MAX} 个字')
    user = User.objects.select_for_update().get(pk=user.pk)
    if user.topic == topic:
        return user, False
    before, user.topic = user.topic, topic
    user.save(update_fields=['topic'])
    audit(operator, None, 'topic', user, before=before, after=topic)
    return user, True


@transaction.atomic
def reset_password(operator, user, password):
    """最高权限为普通成员 / 组长重置密码。最高权限账号的密码只能在服务器上用 changepassword 修改。"""
    access.require_super(operator)
    if user.is_staff:
        raise RuleError('最高权限账号的密码请在服务器上用 python manage.py changepassword 修改')
    problems = password_problems(password, user.account, user.name, user.email) if password else ['密码为空']
    if problems:
        raise RuleError('；'.join(problems))
    user.set_password(password)
    user.save(update_fields=['password'])
    clear_login_lock(user.account)
    audit(operator, None, 'password_reset', user, account=user.account)


# ---------- 批量导入 ----------

@dataclass
class ImportReport:
    dry_run: bool
    start: date
    created: list = field(default_factory=list)   # 新建（导入后含自动生成的初始密码）
    updated: list = field(default_factory=list)   # 已有账号：更新了年级身份或课题
    skipped: list = field(default_factory=list)   # 已有账号：没有要改的
    errors: list = field(default_factory=list)
    new_groups: list = field(default_factory=list)

    @property
    def ok(self):
        return not self.errors

    @property
    def generated(self):
        return [item for item in self.created if item['password']]


def _text(value):
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


def _header_index(cells):
    """表头 → {字段: 列号}。"""
    header = [_text(cell).strip().replace('(', '（').replace(')', '）') for cell in cells]
    index = {}
    for key, labels in HEADERS.items():
        position = next((header.index(label) for label in labels if label in header), None)
        if position is not None:
            index[key] = position
    return index


def _record(cells, index, number):
    """一行 → 记录；整行为空返回 None。密码保留原样（首尾空格在校验时报错，不悄悄改动），其他列去掉首尾空白。"""
    cells = list(cells or [])
    record = {key: '' for key in HEADERS}
    for key, position in index.items():
        if position < len(cells):
            record[key] = _text(cells[position])
    if not any(text.strip() for text in record.values()):
        return None
    record = {key: (text if key == 'password' else text.strip()) for key, text in record.items()}
    record['row'] = number
    return record


def _collect(records):
    rows = []
    for record in records:
        if record is None:
            continue
        rows.append(record)
        if len(rows) > MAX_ROWS:
            raise RuleError(f'一次最多导入 {MAX_ROWS} 人，请分批导入')
    return rows


def read_rows(fileobj):
    """读取 Excel 的第一个工作表：按第 1 行表头定位列（只有“姓名”必需），空行跳过。"""
    try:
        workbook = load_workbook(fileobj, read_only=True, data_only=True)
    except Exception:
        raise RuleError('无法读取文件，请上传 Excel .xlsx 文件（可先下载模板或成员表）')
    try:
        values = workbook.worksheets[0].iter_rows(values_only=True)
        index = _header_index(next(values, None) or [])
        if 'name' not in index:
            raise RuleError('第 1 行表头缺少：姓名（请使用模板或下载的成员表的表头）')
        return _collect(_record(cells, index, number) for number, cells in enumerate(values, start=2))
    finally:
        workbook.close()


def read_text(text):
    """读取粘贴的表格。从 Excel、WPS、在线表格复制来的是制表符分隔，列可以很多；
    也接受逗号或空格分隔，这时没有表头就只认前三列（姓名、年级、课题名称），课题名称里可以有空格。
    第一行有“姓名”时当表头，否则按模板顺序读取。"""
    text = (text or '').replace('\r\n', '\n').replace('\r', '\n')
    if len(text) > MAX_TEXT:
        raise RuleError('粘贴的内容太长，请分批导入')
    lines = [(number, line) for number, line in enumerate(text.split('\n'), start=1) if line.strip()]
    if not lines:
        raise RuleError('请先粘贴表格内容')
    tabbed = any('\t' in line for _, line in lines)

    def split(line, width):
        return line.split('\t') if tabbed else re.split(r'[,，\s]+', line.strip(), maxsplit=width - 1)

    first = split(lines[0][1], len(FIELDS))
    if any(cell.strip() == '姓名' for cell in first):
        index, body, width = _header_index(first), lines[1:], len(first)
    else:
        index, body, width = {key: i for i, key in enumerate(FIELDS)}, lines, (len(FIELDS) if tabbed else 3)
    return _collect(_record(split(line, width), index, number) for number, line in body)


def _split_groups(text):
    return [name for name in GROUP_SEPARATORS.split(text or '') if name]


def _profile_change(user, record, identity, year, topic):
    """已有账号要改的内容：表里填了、而且和系统不同的年级身份、课题名称（空着的格子不算要改）。"""
    change, notes = {}, []
    if (record['grade'] or record['identity']) and (identity, year) != (user.identity, user.grade):
        change.update(identity=identity, grade=year)
        notes.append(f'年级身份：{user.tag or "学生"} → {User.make_tag(identity, year) or "学生"}')
    if topic and topic != user.topic:
        change['topic'] = topic
        notes.append(f'课题：{user.topic or "（空）"} → {topic}')
    return {**change, 'notes': notes} if change else {}


def plan_import(rows, report, default_group=None):
    """逐行校验，分成新建、更新、无需改动三类；有问题的行记入 report.errors。返回 (新建计划, 更新计划)。"""
    existing = {u.account.lower(): u for u in User.objects.all()}
    emails = set(User.objects.exclude(email='').values_list('email', flat=True))
    groups = {g.name: g for g in Group.objects.all()}
    seen_accounts, seen_emails, creates, updates = {}, {}, [], []
    for record in rows:
        row, name, email, password = record['row'], record['name'], record['email'], record['password']
        account = record['account'] or re.sub(r'\s+', '', name)  # 没填账号就用姓名
        key = account.lower()
        problems = []
        if key and key in seen_accounts:
            problems.append(f'账号与第 {seen_accounts[key]} 行重复' + ('' if record['account'] else '（同名的人请在“账号”列另填账号）'))
        seen_accounts.setdefault(key, row)
        identity, year, problem = parse_identity(record['identity'], record['grade'])
        problems += [problem] if problem else []
        topic = _clean_topic(record['topic'])
        if len(topic) > TOPIC_MAX:
            problems.append(f'课题名称超过 {TOPIC_MAX} 个字')
        user = existing.get(key) if key else None
        if user:  # 已有账号：增量，只补年级身份和课题
            if problems:
                report.errors.append({'row': row, 'account': user.account, 'message': '；'.join(problems)})
            elif user.is_staff:
                report.skipped.append({'row': row, 'name': user.name, 'account': user.account, 'reason': '最高权限账号，不在导入范围'})
            elif change := _profile_change(user, record, identity, year, topic):
                updates.append({'row': row, 'user': user, **change})
            else:
                report.skipped.append({'row': row, 'name': user.name, 'account': user.account, 'reason': '已有账号，没有要更新的内容（不改密码、姓名和分组）'})
            continue
        problems += _check_fields(name, account, email)
        if email:
            folded = email.casefold()
            if folded in emails:
                problems.append('邮箱已被已有账号使用')
            elif folded in seen_emails:
                problems.append(f'邮箱与第 {seen_emails[folded]} 行重复')
            seen_emails.setdefault(folded, row)
        members, leads = _split_groups(record['groups']), _split_groups(record['leads'])
        wanted = list(dict.fromkeys(members + leads))  # 担任组长的小组自动计入所在小组
        if not wanted and default_group:
            wanted = [default_group.name]
        for group_name in wanted:
            if len(group_name) > 100:
                problems.append(f'小组名称超过 100 个字符：{group_name[:20]}…')
            elif group_name in groups and groups[group_name].archived:
                problems.append(f'小组“{group_name}”已归档，不能加入')
        generated = not password
        if generated:
            password = generate_password()
        elif password != password.strip():
            problems.append('密码首尾不能有空格')
        else:
            problems += password_problems(password, account, name, email)
        if problems:
            report.errors.append({'row': row, 'account': account or '（空）', 'message': '；'.join(problems)})
            continue
        creates.append({'row': row, 'name': name, 'account': account, 'password': password, 'generated': generated, 'email': email,
                        'identity': identity, 'grade': year, 'topic': topic, 'groups': wanted, 'leads': [g for g in wanted if g in leads]})
    report.new_groups = list(dict.fromkeys(g for item in creates for g in item['groups'] if g not in groups))
    return creates, updates


def import_rows(operator, rows, start, dry_run=False, group_id=None):
    """返回 ImportReport。dry_run 或存在错误时不写入任何数据。group_id：新成员没填小组的，加入这个小组。"""
    access.require_super(operator)
    report = ImportReport(dry_run=dry_run, start=grouping.week_start(start))
    if not rows:
        raise RuleError('表格里没有成员数据')
    default_group = None
    if group_id:
        default_group = Group.objects.filter(pk=group_id, archived=False).first()
        if default_group is None:
            raise RuleError('所选小组不存在或已归档')
    creates, updates = plan_import(rows, report, default_group)
    report.created = [{**{key: item[key] for key in ('row', 'name', 'account', 'topic', 'groups', 'leads', 'generated')},
                       'tag': User.make_tag(item['identity'], item['grade']) or '学生',
                       'password': item['password'] if item['generated'] and not dry_run else ''} for item in creates]
    report.updated = [{'row': item['row'], 'name': item['user'].name, 'account': item['user'].account, 'notes': item['notes']} for item in updates]
    if report.errors or dry_run or not (creates or updates):
        return report
    # PBKDF2 哈希较慢（每个约 0.3 秒），在事务外并行计算；pbkdf2 计算时释放 GIL。
    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
        hashes = list(pool.map(make_password, [item['password'] for item in creates]))
    try:
        with transaction.atomic():
            group_map = {g.name: g for g in Group.objects.select_for_update().filter(name__in=[g for item in creates for g in item['groups']])}
            for name in report.new_groups:
                group_map[name] = grouping.create_group(operator, name)
            order = _next_order()
            for item, hashed in zip(creates, hashes):
                user = User(account=item['account'], name=item['name'], email=item['email'], identity=item['identity'], grade=item['grade'],
                            topic=item['topic'], password=hashed, sort_order=order)
                user.save()
                order += 1
                audit(operator, None, 'create_member', user, account=user.account, identity=user.identity, grade=user.grade, topic=user.topic, via='import')
                for group_name in item['groups']:
                    role = GroupMembership.LEADER if group_name in item['leads'] else GroupMembership.MEMBER
                    grouping.add_membership(operator, group_map[group_name], user, role, report.start)
            for item in updates:
                user = item['user']
                set_identity(operator, user, item.get('identity', user.identity), item.get('grade', user.grade), item.get('topic'))
            audit(operator, None, 'import_members', operator, created=len(creates), updated=len(updates), skipped=len(report.skipped),
                  new_groups=report.new_groups, start=str(report.start))
    except IntegrityError:
        raise RuleError('导入期间有账号、邮箱或小组被同时创建，未写入任何数据，请重新导入')
    return report


def import_members(operator, fileobj, start, dry_run=False, group_id=None):
    """上传的 Excel。"""
    access.require_super(operator)
    return import_rows(operator, read_rows(fileobj), start, dry_run, group_id)


def import_text(operator, text, start, dry_run=False, group_id=None):
    """粘贴的表格。"""
    access.require_super(operator)
    return import_rows(operator, read_text(text), start, dry_run, group_id)


# ---------- Excel：模板、成员表、新账号初始密码 ----------

def _sheet(workbook, title, header, widths):
    sheet = workbook.active
    sheet.title = title
    sheet.append(header)
    for cell in sheet[1]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='20384D')
    for letter, width in zip('ABCDEFGHIJ', widths):
        sheet.column_dimensions[letter].width = width
    sheet.freeze_panes = 'A2'
    return sheet


def _save(workbook):
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


NOTES = [
    ['一人一行。只有“姓名”必填，其他列都可以空着。'],
    ['年级：写成“2026级硕士”“2021级博士”或“老师”。硕士、博士要带入学年份；老师周报选填、不计入提交率。'],
    ['课题名称：个人课题，显示在成员阅览页和小组页的组员列表里。'],
    ['所在小组：多个小组用顿号（、）分隔，表里出现的新小组会自动创建；担任组长的小组自动计入所在小组。'],
    ['账号：登录用，不区分大小写、不能含空格；不填就用姓名当账号（同名的人请另填账号，如学号）。'],
    ['密码：不填就自动生成，导入完成后在页面上显示一次，可以下载发给本人；自己填的话至少 8 位，不能是常见密码、不能全是数字、不能和账号或姓名太像。'],
    ['导入是增量的：新账号新建；已有账号不重复建，不改密码、姓名和分组，只更新表里填了的年级和课题。任何一行有错，整份表都不会导入。'],
]


def template_workbook():
    """导入模板：成员表只有表头（避免示例行被误导入），示例与说明放在第二个工作表。"""
    workbook = Workbook()
    sheet = _sheet(workbook, '成员', COLUMNS, (12, 14, 30, 18, 18, 16, 16, 24))
    for letter in 'ABCDEFGH':
        for row in range(2, 302):
            sheet[f'{letter}{row}'].number_format = '@'  # 文本格式：学号前导 0、纯数字账号不被 Excel 改写
    notes = workbook.create_sheet('填写说明')
    for line in [['只读取第一个工作表“成员”，第 1 行表头不要改动。也可以不用这个文件：在导入页直接粘贴表格。'], *NOTES, [],
                 ['示例（不要把示例填进“成员”表）：'], COLUMNS,
                 ['张三', '老师', '', 'A组、B组', 'A组、B组', '', '', ''],
                 ['李四', '2022级博士', '虚构课题：图像分类', 'A组', '', 'lisi', '', ''],
                 ['王五', '2026级硕士', '虚构课题：模型压缩', 'B组', '', '2026001', '', '']]:
        notes.append(line)
    notes.column_dimensions['A'].width = 110
    return _save(workbook)


def roster_workbook():
    """下载成员表：当前全部组长和成员（不含最高权限），表头与导入模板相同，密码列留空。
    可以直接在后面加人、补年级和课题，再导入（增量）。"""
    period = period_for()
    groups = access.memberships_at(period)
    leads = {}
    for membership in GroupMembership.objects.filter(access.covering(period.start), role=GroupMembership.LEADER).select_related('group').order_by('group__sort_order', 'group_id'):
        leads.setdefault(membership.user_id, []).append(membership.group.name)
    workbook = Workbook()
    sheet = _sheet(workbook, '成员', COLUMNS, (12, 14, 30, 18, 18, 16, 16, 24))
    for user in User.objects.filter(is_staff=False).order_by('sort_order', 'pk'):
        sheet.append([user.name, user.tag, user.topic, '、'.join(g.name for g in groups.get(user.pk, [])), '、'.join(leads.get(user.pk, [])),
                      user.account, '', user.display_email or user.email])
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.number_format = '@'
    notes = workbook.create_sheet('填写说明')
    for line in [[f'这是 {timezone.localdate():%Y-%m-%d} 下载的成员表（所在小组为本周的分组）。要加人：在后面接着写；要补年级、课题：直接改对应的格子。改好后在“批量导入”里上传或粘贴。'],
                 ['密码列是空的（系统不保存明文密码）：已有账号的密码不会被改；新加的人不填密码会自动生成。'], *NOTES]:
        notes.append(line)
    notes.column_dimensions['A'].width = 110
    return _save(workbook)


def credentials_workbook(created):
    """导入完成后下载：新账号和初始密码。只在导入完成的那一页生成，系统不保存明文密码；发给本人后请删除。"""
    workbook = Workbook()
    sheet = _sheet(workbook, '新账号', ['姓名', '账号', '初始密码', '标签', '课题名称'], (12, 16, 16, 14, 30))
    for item in created:
        sheet.append([item['name'], item['account'], item['password'] or '（表里已填）', item['tag'], item['topic']])
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.number_format = '@'
    return _save(workbook)
