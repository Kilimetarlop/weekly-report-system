# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
"""课题下放给组长（2026-11-17）：组长在小组页“进入编辑”后给本组所有人（含自己、其他组长、老师）设课题；
最高权限照旧；成员本人不能改；跨组成员一人一个课题，以最后一次为准；只改课题、不改年级身份；每次修改写审计。"""
from django.test import TransactionTestCase, override_settings
from reports.models import Audit, Group, GroupMembership as M, User
from .test_domain import instant

PASSWORD = 'Test-only-Password-892!'
EARLY = instant(1).date().replace(day=3)  # 2026-10-03，周六


@override_settings(EMAIL_ENABLED=False)
class TopicTests(TransactionTestCase):
    def setUp(self):
        def make(key, name, **extra):
            return User.objects.create_user(f'{key}@example.invalid', PASSWORD, name=name, created_at=instant(15), **extra)
        self.root = make('root', '虚构最高权限', is_staff=True)
        self.lead_a = make('leada', '虚构甲组长')
        self.teacher = make('teacher', '虚构王', identity=User.TEACHER)
        self.a = make('a', '虚构甲')
        self.c = make('c', '虚构丙')  # 跨 A、B 两组
        self.lead_b = make('leadb', '虚构乙组长')
        self.b = make('b', '虚构乙')
        self.A = Group.objects.create(name='A组', sort_order=1)
        self.B = Group.objects.create(name='B组', sort_order=2)
        for user, group, role in [(self.lead_a, self.A, M.LEADER), (self.teacher, self.A, M.LEADER), (self.a, self.A, M.MEMBER), (self.c, self.A, M.MEMBER),
                                  (self.lead_b, self.B, M.LEADER), (self.b, self.B, M.MEMBER), (self.c, self.B, M.MEMBER)]:
            M.objects.create(user=user, group=group, role=role, start=EARLY)

    def post_topic(self, operator, person, topic):
        self.client.force_login(operator)
        return self.client.post(f'/people/{person.pk}/topic/', {'topic': topic, 'next': f'/groups/{self.A.pk}/'})

    def topic(self, user):
        user.refresh_from_db()
        return user.topic

    def test_leader_sets_topics_in_edit_mode_on_group_page(self):
        self.client.force_login(self.lead_a)
        page = self.client.get(f'/groups/{self.A.pk}/')
        self.assertNotContains(page, 'name="topic"')  # 查看模式只看
        self.assertContains(page, '设置课题请点“进入编辑”')
        self.client.post('/mode/', {'mode': 'edit'})
        page = self.client.get(f'/groups/{self.A.pk}/')
        for person in (self.lead_a, self.teacher, self.a, self.c):  # 本组所有人，含自己、其他组长和老师
            self.assertContains(page, f'action="/people/{person.pk}/topic/"', html=False)
        response = self.client.post(f'/people/{self.a.pk}/topic/', {'topic': '  虚构课题： 图像分类 ', 'next': f'/groups/{self.A.pk}/#member-{self.a.pk}'}, follow=True)
        self.assertEqual(response.redirect_chain[0][0], f'/groups/{self.A.pk}/#member-{self.a.pk}')  # 保存后回到这一行
        self.assertContains(response, '已保存 虚构甲 的课题：虚构课题： 图像分类')
        self.assertEqual(self.topic(self.a), '虚构课题： 图像分类')
        self.assertContains(response, '<span class="member-topic">虚构课题： 图像分类</span>', html=False)
        audit = Audit.objects.get(action='topic', object_id=str(self.a.pk))
        self.assertEqual((audit.operator, audit.metadata), (self.lead_a, {'before': '', 'after': '虚构课题： 图像分类'}))
        for person in (self.lead_a, self.teacher):
            self.assertEqual(self.post_topic(self.lead_a, person, '虚构课题：带组').status_code, 302)
            self.assertEqual(self.topic(person), '虚构课题：带组')
        self.post_topic(self.lead_a, self.a, '')  # 清空后保存即删除
        self.assertEqual(self.topic(self.a), '')

    def test_who_can_set_topics(self):
        self.assertEqual(self.post_topic(self.lead_a, self.b, '越权').status_code, 403)  # 别的组
        self.assertEqual(self.post_topic(self.a, self.a, '自己改').status_code, 403)  # 成员本人不能改
        self.assertEqual(self.post_topic(self.b, self.a, '组员改别人').status_code, 403)
        self.assertEqual(self.post_topic(self.root, self.root, '最高权限账号').status_code, 403)
        self.assertEqual((self.topic(self.a), self.topic(self.b)), ('', ''))
        # 跨组成员：两个组长都能改，以最后一次为准；最高权限也能改
        self.post_topic(self.lead_b, self.c, '虚构课题：B组定的')
        self.post_topic(self.lead_a, self.c, '虚构课题：A组定的')
        self.assertEqual(self.topic(self.c), '虚构课题：A组定的')
        self.post_topic(self.root, self.b, '虚构课题：最高权限定的')
        self.assertEqual(self.topic(self.b), '虚构课题：最高权限定的')
        # 组长卸任（关系结束）后不能再改
        M.objects.filter(user=self.lead_a, group=self.A).update(end=EARLY.replace(day=10))
        self.assertEqual(self.post_topic(self.lead_a, self.a, '卸任后').status_code, 403)

    def test_only_topic_and_rules(self):
        response = self.post_topic(self.lead_a, self.a, '长' * 201)
        self.assertEqual(self.topic(self.a), '')
        self.assertContains(self.client.get(response.url), '课题名称超过 200 个字')
        self.post_topic(self.lead_a, self.a, '虚构课题')
        self.post_topic(self.lead_a, self.a, '虚构课题')  # 没有改动，不重复写审计
        self.assertEqual(Audit.objects.filter(action='topic').count(), 1)
        # 组长只能改课题：年级身份仍归最高权限
        self.client.force_login(self.lead_a)
        self.assertEqual(self.client.post('/manage/members/', {'action': 'identity', 'user': self.a.pk, 'identity': 'master', 'grade': '2026'}).status_code, 403)
        self.assertEqual(self.client.get(f'/people/{self.a.pk}/topic/').status_code, 405)  # 只接受提交
