# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
from datetime import datetime, timedelta
import unittest
from reports.domain import FIELDS, POINT_MAX, SECTIONS, SHANGHAI, RuleError, as_item, effective_chars, empty_payload, first_deadline, period_dates, upgrade, validate

POINT = '虚构要点'


def instant(day=16, hour=17, minute=0, second=0):
    return datetime(2026, 10, day, hour, minute, second, tzinfo=SHANGHAI)


def item(detail, point=POINT):
    return {'point': point, 'detail': detail}


def valid_payload(text=None):
    """第 2 版正文：除“科研进展 → 本周工作”外全部暂无；text 是那一条的详细阐述。"""
    payload = empty_payload()
    for section in SECTIONS:
        for key in FIELDS:
            payload[section][key] = {'state': 'NONE', 'items': []}
    if text is not None:
        payload['research']['work'] = {'state': 'ITEMS', 'items': [item(text)]}
    return payload


class DomainTests(unittest.TestCase):
    def test_T04_fixed_schema(self):
        p = valid_payload()
        del p['research']['work']
        with self.assertRaises(RuleError):
            validate(p, 20)

    def test_T05_unset_draft_only(self):
        validate(empty_payload())
        with self.assertRaisesRegex(RuleError, '科研进展 → 本周工作'):
            validate(empty_payload(), 20)

    def test_T06_19_20_boundary(self):
        with self.assertRaises(RuleError):
            validate(valid_payload('中' * 19), 20)
        self.assertEqual(validate(valid_payload('中' * 20), 20)[1]['research_work'], 1)

    def test_T07_unicode(self):
        import json
        from pathlib import Path
        for text, expected in json.loads((Path(__file__).parent/'unicode-fixtures.json').read_text(encoding='utf-8')):
            self.assertEqual(effective_chars(text), expected)

    def test_T08_multiline_single_item(self):
        text = '汉' * 10 + '\n' + 'A' * 10 + '\n末段'
        p, counts = validate(valid_payload(text), 20)
        self.assertEqual(counts['research_work'], 1)
        self.assertEqual(p['research']['work']['items'], [item(text)])

    def test_T09_all_none(self):
        self.assertEqual(sum(validate(valid_payload(), 20)[1].values()), 0)

    def test_T10_blank_and_short(self):
        p = valid_payload('中' * 20)
        p['research']['work']['items'] += [item('  \n', point=''), item('短')]
        with self.assertRaises(RuleError):
            validate(p, 20)
        p['research']['work']['items'].pop()
        self.assertEqual(len(validate(p, 20)[0]['research']['work']['items']), 1)

    def test_T31_strict_first_deadline(self):
        self.assertEqual(first_deadline(instant(15)).date().day, 16)
        self.assertEqual(first_deadline(instant(16, 18)).date().day, 23)
        self.assertEqual(first_deadline(instant(16, 17, 59)).date().day, 16)

    def test_T33_friday_evening(self):
        self.assertEqual(period_dates(instant(16, 23))[0].day, 10)
        self.assertEqual(period_dates(instant(17, 0))[0].day, 17)

    def test_T34_cross_year(self):
        start, end, deadline = period_dates(datetime(2027, 1, 1, tzinfo=SHANGHAI))
        self.assertEqual(str(start), '2026-12-26')
        self.assertEqual(str(end), '2027-01-01')
        self.assertEqual(deadline.hour, 18)

    def test_control_character_error(self):
        with self.assertRaisesRegex(RuleError, '第 2 字符'):
            validate(valid_payload('中\x01' + '中'*20), 20)

    def test_none_never_silently_discards_text(self):
        p = valid_payload()
        p['research']['work']['items'] = [item('正文')]
        with self.assertRaises(RuleError):
            validate(p)

    # ---- 第 2 版：要点 + 详细阐述（2026-11-16） ----

    def test_point_required_for_submission_but_not_draft(self):
        p = valid_payload('中' * 20)
        p['research']['work']['items'] = [item('中' * 20, point='')]
        validate(p)  # 暂存草稿允许先只写详细阐述
        with self.assertRaisesRegex(RuleError, '请填写要点'):
            validate(p, 20)

    def test_point_single_line_and_at_most_50(self):
        p = valid_payload('中' * 20)
        p['research']['work']['items'] = [item('中' * 20, point='第一行\n第二行')]
        with self.assertRaisesRegex(RuleError, '只能写一行'):
            validate(p, 20)
        p['research']['work']['items'] = [item('中' * 20, point='要' * (POINT_MAX + 1))]
        with self.assertRaisesRegex(RuleError, f'不能超过 {POINT_MAX} 个字'):
            validate(p, 20)
        p['research']['work']['items'] = [item('中' * 20, point='要' * POINT_MAX)]
        self.assertEqual(validate(p, 20)[1]['research_work'], 1)

    def test_threshold_applies_to_detail_not_point(self):
        p = valid_payload('中' * 20)
        p['research']['work']['items'] = [item('中' * 20, point='短')]  # 要点一个字也行
        validate(p, 20)
        p['research']['work']['items'] = [item('中' * 19, point='要' * 30)]  # 要点再长也不替代详细阐述
        with self.assertRaisesRegex(RuleError, '详细阐述：当前 19 个有效字符'):
            validate(p, 20)

    def test_version_1_payload_is_upgraded(self):
        old = valid_payload()
        old['schema_version'] = 1
        old['research']['work'] = {'state': 'ITEMS', 'items': ['第 1 版的一整段原文' + '中' * 20]}
        upgraded = upgrade(old)
        self.assertEqual(upgraded['schema_version'], 2)
        self.assertEqual(upgraded['research']['work']['items'], [{'point': '', 'detail': '第 1 版的一整段原文' + '中' * 20}])
        validate(old)  # 草稿可直接保存（升级后要点为空）
        with self.assertRaisesRegex(RuleError, '请填写要点'):
            validate(old, 20)
        self.assertEqual(as_item('旧文本'), {'point': '', 'detail': '旧文本'})
