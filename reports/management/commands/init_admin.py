# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
import getpass
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from reports.accounts import ACCOUNT_PATTERN
from reports.models import User


class Command(BaseCommand):
    help = '交互创建最高权限账号（只做管理、不交周报），不提供默认密码，不覆盖已有账号'

    def add_arguments(self, parser):
        parser.add_argument('--account', required=True, help='登录账号，不区分大小写，不能含空格，例如 DemoAdmin')
        parser.add_argument('--name', required=True, help='显示名称')
        parser.add_argument('--email', default='', help='选填')

    def handle(self, *args, **options):
        account, name, email = options['account'].strip(), options['name'].strip(), options['email'].strip()
        if not ACCOUNT_PATTERN.fullmatch(account):
            raise CommandError('账号不能为空、不能含空格，最长 150 个字符')
        if User.objects.filter(account__iexact=account).exists():
            raise CommandError('账号已存在，未重置任何账号；修改密码请用 python manage.py changepassword')
        if email and User.objects.filter(email__iexact=email).exists():
            raise CommandError('邮箱已被其他账号使用')
        password = getpass.getpass('最高权限账号密码: ')
        if password != getpass.getpass('再次输入: '):
            raise CommandError('两次密码不一致')
        try:
            validate_password(password, User(account=account, name=name, email=email))
        except ValidationError as error:
            raise CommandError('；'.join(error.messages))
        User.objects.create_user(account, password, name=name, email=email, is_staff=True)
        self.stdout.write(self.style.SUCCESS(f'最高权限账号 {account} 已创建'))
