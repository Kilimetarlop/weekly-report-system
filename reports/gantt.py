# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
"""按周的甘特图布局。

服务器端算好月份表头、每个任务占哪几列、今天的位置，模板用 CSS 网格画出来，
不依赖外部图表库或 CDN，在国内网络和手机上都能打开。每一列是一周（周六至周五，与周报周期一致）。
"""
from dataclasses import dataclass, field
from datetime import date, timedelta


def week_of(day):
    """该日期所在周的周六（周期起始日）。"""
    return day - timedelta(days=(day.weekday() - 5) % 7)


@dataclass
class Row:
    task: object
    visible: bool
    col: int = 1          # 在时间轴网格中的起始列（从 1 开始）
    span: int = 1
    late: bool = False    # 已过结束日期且进度未到 100%
    done: bool = False
    clipped: bool = False  # 部分超出显示窗口
    tip: str = ''


@dataclass
class Chart:
    weeks: int
    start: date
    months: list = field(default_factory=list)   # [(标签, 跨几列)]
    rows: list = field(default_factory=list)
    today_offset: float = 0.0                      # 今天距窗口起点的周数（可带小数）
    today: date = None


def build(tasks, today, after=2, min_weeks=12, max_weeks=52):
    """窗口起点固定在最早的任务（2026-11-23 修复）：不再是“今天往前 4 周”，否则图跟着今天滑动，红线永远停在同一格。"""
    today_week = week_of(today)
    first = min((week_of(t.start) for t in tasks), default=today_week)
    last = max((week_of(t.end) for t in tasks), default=today_week)
    start = min(first, today_week)
    end = max(last, today_week) + timedelta(weeks=after)
    weeks = (end - start).days // 7 + 1
    if weeks < min_weeks:
        weeks = min_weeks
    if weeks > max_weeks:
        # 太长时分段显示：今天快到窗口右边才整段往后翻半年，平时窗口不动，红线才看得出在走
        step = max_weeks // 2
        overflow = (today_week - start).days // 7 - (max_weeks - after - 1)
        if overflow > 0:
            start += timedelta(weeks=(overflow + step - 1) // step * step)
        weeks = max_weeks
    chart = Chart(weeks=weeks, start=start, today_offset=round((today - start).days / 7, 3), today=today)

    spans = []  # [(年, 月), 跨几周]
    for index in range(weeks):
        middle = start + timedelta(weeks=index, days=3)  # 以周中（周二）所在月份为准
        if spans and spans[-1][0] == (middle.year, middle.month):
            spans[-1][1] += 1
        else:
            spans.append([(middle.year, middle.month), 1])
    # 标签按宽度取舍：够宽的第一个月和每个 1 月带年份；两周宽只写“几月”；只有一周的不写字（悬停可见）
    year_shown = False
    for (year, month), span in spans:
        full = f'{year}年{month}月'
        if span >= 3 and (not year_shown or month == 1):
            label, year_shown = full, True
        else:
            label = f'{month}月' if span >= 2 else ''
        chart.months.append((label, span, full))

    for task in sorted(tasks, key=lambda t: (t.is_milestone, t.start, t.end, t.pk)):
        first_col = (week_of(task.start) - start).days // 7
        last_col = (week_of(task.end) - start).days // 7
        progress = '' if task.is_milestone else f'，进度 {task.progress}%'
        tip = f'{task.title}：{task.start}' + ('' if task.start == task.end else f' 至 {task.end}') + progress
        if last_col < 0 or first_col >= weeks:
            chart.rows.append(Row(task=task, visible=False, tip=tip + '（不在当前显示范围内）'))
            continue
        clipped_first, clipped_last = max(first_col, 0), min(last_col, weeks - 1)
        chart.rows.append(Row(
            task=task, visible=True, col=clipped_first + 1, span=clipped_last - clipped_first + 1,
            late=not task.is_milestone and task.end < today and task.progress < 100,
            done=not task.is_milestone and task.progress >= 100,
            clipped=(clipped_first, clipped_last) != (first_col, last_col), tip=tip,
        ))
    return chart
