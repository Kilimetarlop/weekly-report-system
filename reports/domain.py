# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
"""不访问数据库的周期与正文规则；前端采用相同 Unicode L / Nd 语义。

正文第 2 版（2026-11-16 起）：每条由“要点”（一句话，不超过 50 字）和“详细阐述”组成。
字数下限（默认 20 个有效字符，后台可调）只管详细阐述。第 1 版正文（每条一段纯文本）读取时视为只有详细阐述。
"""
import copy
import unicodedata
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo('Asia/Shanghai')
SECTIONS = {'research': '科研进展', 'project': '项目推进'}
FIELDS = {'work': '本周工作', 'results': '取得的结果', 'issues': '存在的问题', 'plan': '下周计划'}
# 填写引导（2026-11-17）：每一项下面一行说明“写什么”，输入框里是灰色示例（开始输入就消失）。
# (部分, 小项) → (说明, 要点示例, 详细阐述示例)
GUIDES = {
    ('research', 'work'): ('本周实际做了哪些科研工作：实验、算法与代码、文献调研、论文写作等。一条写一件事。',
                           '例：复现图像分类基线方法',
                           '例：用什么数据和方法、做到了哪一步、花了多少时间，遇到的情况……'),
    ('research', 'results'): ('这周拿得出来的结果：指标、结论、图表、投稿或录用等。只是“做了”还没有结果的，写在本周工作里。',
                              '例：图像分类准确率提升到 92.3%',
                              '例：与上周相比提升多少、主要因为哪项改动、结果表或图放在哪里……'),
    ('research', 'issues'): ('卡住的地方、风险、需要老师或同学帮忙的事。写清楚已经试过什么。',
                             '例：新数据集上模型不收敛',
                             '例：现象是什么，已经尝试过哪些办法，怀疑的原因，希望谁帮什么忙……'),
    ('research', 'plan'): ('下周打算做什么，尽量具体到下周五能检查的结果。',
                           '例：完成三组消融实验并整理结果表',
                           '例：做哪几项、各自的目标，什么时候给出结果……'),
    ('project', 'work'): ('参与的项目（纵向/横向课题、平台搭建、设备调试、报告材料等）本周做了什么。没有项目任务就勾“本周无”。',
                          '例：完成项目中期报告第 3 章',
                          '例：哪个项目、负责的哪一部分、完成到什么程度……'),
    ('project', 'results'): ('项目上的交付物或进展：完成的模块、测试通过、提交的材料等。',
                             '例：数据采集平台联调通过',
                             '例：交付了什么、验收或测试情况、交给了谁……'),
    ('project', 'issues'): ('项目上的阻碍：需要协调的资源、设备、时间冲突等。',
                            '例：测试设备到货推迟一周',
                            '例：影响到哪项进度，需要谁来协调，打算怎么应对……'),
    ('project', 'plan'): ('项目下周要推进的事项和时间节点。',
                          '例：完成接口文档并提交甲方审阅',
                          '例：具体要做的事、截止时间、需要配合的人……'),
}
SCHEMA_VERSION = 2
POINT_MAX = 50


class RuleError(ValueError):
    pass


class Conflict(RuleError):
    pass


def period_dates(now):
    date = now.astimezone(SHANGHAI).date()
    start = date - timedelta(days=(date.weekday() - 5) % 7)
    end = start + timedelta(days=6)
    return start, end, datetime.combine(end, time(18), SHANGHAI)


def first_deadline(created):
    deadline = period_dates(created)[2]
    return deadline if deadline > created else deadline + timedelta(days=7)


def effective_chars(text):
    return sum(unicodedata.category(char).startswith('L') or unicodedata.category(char) == 'Nd' for char in text)


def empty_payload():
    return {'schema_version': SCHEMA_VERSION, **{section: {key: {'state': 'UNSET', 'items': []} for key in FIELDS} for section in SECTIONS}}


def as_item(value):
    """条目统一成 {'point', 'detail'}；第 1 版的纯文本条目视为只有详细阐述。"""
    if isinstance(value, dict):
        return {'point': value.get('point', ''), 'detail': value.get('detail', '')}
    return {'point': '', 'detail': value}


def upgrade(payload):
    """第 1 版正文升级为第 2 版：原来的一段文字放进“详细阐述”，要点留空待填。"""
    if not isinstance(payload, dict) or payload.get('schema_version') != 1:
        return payload
    result = copy.deepcopy(payload)
    result['schema_version'] = SCHEMA_VERSION
    for section in SECTIONS:
        for key in FIELDS:
            field = result.get(section, {}).get(key) if isinstance(result.get(section), dict) else None
            if isinstance(field, dict) and isinstance(field.get('items'), list):
                field['items'] = [as_item(text) if isinstance(text, str) else text for text in field['items']]
    return result


def _check_characters(text, where):
    for position, char in enumerate(text, 1):
        code = ord(char)
        if code < 32 and char not in '\n\t' or 0xD800 <= code <= 0xDFFF or code in (0xFFFE, 0xFFFF):
            raise RuleError(f'{where}第 {position} 字符不能进入文档')


def validate(payload, threshold=None):
    """threshold 为 None 时是暂存草稿：只查结构与非法字符；给出阈值时按正式提交检查。"""
    payload = upgrade(payload)
    if not isinstance(payload, dict) or set(payload) != {'schema_version', *SECTIONS} or payload['schema_version'] != SCHEMA_VERSION:
        raise RuleError('正文结构错误，请刷新页面后重试')
    result = copy.deepcopy(payload)
    counts = {}
    for section, section_name in SECTIONS.items():
        if not isinstance(result[section], dict) or set(result[section]) != set(FIELDS):
            raise RuleError(f'{section_name}：固定四小项不可缺失或增加')
        for key, label in FIELDS.items():
            item = result[section][key]
            where = f'{section_name} → {label}'
            if not isinstance(item, dict) or set(item) != {'state', 'items'} or item['state'] not in ('UNSET', 'NONE', 'ITEMS') or not isinstance(item['items'], list):
                raise RuleError(f'{where}：结构错误')
            clean = []
            for index, raw in enumerate(item['items'], 1):
                if not isinstance(raw, dict) or set(raw) != {'point', 'detail'} or not all(isinstance(raw[k], str) for k in ('point', 'detail')):
                    raise RuleError(f'{where}：第 {index} 条结构错误')
                point = raw['point'].replace('\r\n', '\n').replace('\r', '\n').strip()
                detail = raw['detail'].replace('\r\n', '\n').replace('\r', '\n')
                _check_characters(point, f'{where} → 第 {index} 条要点')
                _check_characters(detail, f'{where} → 第 {index} 条详细阐述')
                if '\n' in point:
                    raise RuleError(f'{where} → 第 {index} 条：要点只能写一行，展开内容请写在详细阐述里')
                if len(point) > POINT_MAX:
                    raise RuleError(f'{where} → 第 {index} 条：要点不能超过 {POINT_MAX} 个字，展开内容请写在详细阐述里')
                if point or detail.strip():
                    clean.append({'point': point, 'detail': detail})
            if item['state'] == 'NONE' and clean:
                raise RuleError(f'{where}：暂无状态不得携带正文，请确认清除')
            if threshold is not None:
                if item['state'] == 'UNSET' or item['state'] == 'ITEMS' and not clean:
                    raise RuleError(f'{where}：请填写要点或明确勾选暂无')
                for index, entry in enumerate(clean, 1):
                    if not entry['point']:
                        raise RuleError(f'{where} → 第 {index} 条：请填写要点（一句话概括）')
                    count = effective_chars(entry['detail'])
                    if count < threshold:
                        raise RuleError(f'{where} → 第 {index} 条详细阐述：当前 {count} 个有效字符，当前阈值至少 {threshold} 个。')
            item['items'] = clean
            counts[f'{section}_{key}'] = len(clean) if item['state'] == 'ITEMS' else 0
    return result, counts
