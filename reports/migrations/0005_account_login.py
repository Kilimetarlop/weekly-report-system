# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
"""登录标识由邮箱改为账号（2026-11-16）。

已有用户的账号取其原邮箱，因此迁移后仍可用原邮箱登录；邮箱改为选填，非空时仍不区分大小写唯一。
"""
from django.db import migrations, models
import django.db.models.functions.text


def copy_email_to_account(apps, schema_editor):
    User = apps.get_model('reports', 'User')
    User.objects.update(account=models.F('email'))


class Migration(migrations.Migration):

    dependencies = [
        ('reports', '0004_groups'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='account',
            field=models.CharField(max_length=150, null=True),
        ),
        migrations.RunPython(copy_email_to_account, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='user',
            name='account',
            field=models.CharField(max_length=150, unique=True, verbose_name='账号'),
        ),
        migrations.RemoveConstraint(
            model_name='user',
            name='email_case_unique',
        ),
        migrations.AlterField(
            model_name='user',
            name='email',
            field=models.EmailField(blank=True, max_length=254, verbose_name='邮箱'),
        ),
        migrations.AlterField(
            model_name='user',
            name='name',
            field=models.CharField(max_length=150, verbose_name='姓名'),
        ),
        migrations.AddConstraint(
            model_name='user',
            constraint=models.UniqueConstraint(django.db.models.functions.text.Lower('account'), name='account_case_unique'),
        ),
        migrations.AddConstraint(
            model_name='user',
            constraint=models.UniqueConstraint(django.db.models.functions.text.Lower('email'), condition=models.Q(('email', ''), _negated=True), name='email_case_unique'),
        ),
    ]
