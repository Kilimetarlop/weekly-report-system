# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
from io import BytesIO
from django.db import connection, transaction
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from .domain import FIELDS, SECTIONS
from .services import rows


@transaction.atomic
def workbook_snapshot(period, ids, types, columns, scope=None):
    """scope 由视图按当前权限解析；免交、应交按该范围口径。"""
    with connection.cursor() as cursor:
        cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
    selected = [row for row in rows(period, scope) if row['user'].pk in ids]
    if not selected or not types or not set(types) <= {'status', 'counts'}:
        raise ValueError('请选择本周期人员和至少一种统计类型')
    count_labels = [f'{s}·{f}' for s in SECTIONS.values() for f in FIELDS.values()]
    count_keys = [f'{s}_{f}' for s in SECTIONS for f in FIELDS]
    indexes = [int(c) for c in columns if c.isdigit()] or list(range(8))
    status_indexes = [int(c[1:]) for c in columns if c.startswith('s') and c[1:].isdigit()] or list(range(6))
    if not set(columns) <= {str(i) for i in range(8)} | {f's{i}' for i in range(6)} or not set(indexes) <= set(range(8)) or not set(status_indexes) <= set(range(6)):
        raise ValueError('不支持的导出列')
    wb = Workbook()
    wb.remove(wb.active)
    for kind in types:
        ws = wb.create_sheet('提交情况' if kind == 'status' else '要点统计')
        ws.append([f'{period} / 范围 {scope.label if scope else "全部人员"} / 导出时间 {timezone.localtime().isoformat()}'])
        labels = ['姓名', '账号', '状态', '所属小组']
        if kind == 'status':
            status_labels = ['是否应交', '是否免交', '首次提交', '最新提交', '首次是否逾期', '是否待修改']
            labels += [status_labels[i] for i in status_indexes]
        else:
            labels += [count_labels[i] for i in indexes] + ['科研合计', '项目合计']
        ws.append(labels)
        for row in selected:
            report, submission = row['report'], row['latest']
            values = [row['user'].name, row['user'].account, row['state'], '、'.join(g.name for g in row['groups']) or '未分组']
            if kind == 'status':
                status_values = [not row['exempt'], row['exempt'], report.first_submitted_at.isoformat() if submission else None, submission.submitted_at.isoformat() if submission else None, report.first_submitted_at >= period.deadline if submission else None, report.returned if submission else False]
                values += [status_values[i] for i in status_indexes]
            else:
                values += [submission.counts[count_keys[i]] if submission else None for i in indexes]
                values += [sum(submission.counts[f'{s}_{f}'] for f in FIELDS) if submission else None for s in SECTIONS]
            ws.append(values)
        ws.freeze_panes = 'E3'
        ws.auto_filter.ref = f'A2:{get_column_letter(len(labels))}{ws.max_row}'
        for cells in ws.iter_rows():
            for cell in cells:
                if isinstance(cell.value, str):
                    cell.data_type = 's'  # formula-like strings remain text
                cell.alignment = Alignment(vertical='top', wrap_text=True)
        for cell in ws[2]:
            cell.fill = PatternFill('solid', fgColor='20384D')
            cell.font = Font(color='FFFFFF', bold=True)
        ws.row_dimensions[1].height = 36
        ws.row_dimensions[2].height = 34
        for column in range(1, len(labels) + 1):
            ws.column_dimensions[get_column_letter(column)].width = 30 if column in (2, 3) else 20
    stream = BytesIO()
    wb.save(stream)
    return stream.getvalue()
