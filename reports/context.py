# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
_ASSET_VERSION = None


def asset_version():
    """静态文件（CSS/JS）内容的短摘要，加在地址后面（site.css?v=…）。
    服务器前面的反向代理和浏览器会把静态文件缓存 4 小时；文件一改，地址就变，更新后马上生效。"""
    global _ASSET_VERSION
    if _ASSET_VERSION is None:
        import hashlib
        from django.conf import settings
        digest = hashlib.sha256()
        for folder in settings.STATICFILES_DIRS:
            for path in sorted(p for p in folder.rglob('*') if p.is_file() and p.suffix in ('.css', '.js')):
                digest.update(path.name.encode() + path.read_bytes())
        _ASSET_VERSION = digest.hexdigest()[:10]
    return _ASSET_VERSION


def site_settings(request):
    """所有页面共用：站点名、邮件开关，以及侧栏需要的角色和未读批示数。"""
    from . import access
    from .models import Group, InstructionReceipt
    from .services import email_enabled, site
    data = {'site_title': site().title, 'email_enabled': email_enabled(), 'v': asset_version()}
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated:
        data['is_super'] = access.is_super(user)
        led = set() if data['is_super'] else access.led_group_ids(user)
        data['can_manage'] = data['is_super'] or bool(led)
        data['is_leader'] = data['can_manage'] and not data['is_super']
        # 组长侧栏分“我的”和“小组管理”两组；小组管理里每个所带小组一个入口
        data['led_groups'] = list(Group.objects.filter(pk__in=led).order_by('sort_order', 'pk')) if led else []
        # 老师周报选填，不计入提交率
        data['is_teacher'] = not data['is_super'] and user.is_teacher
        if data['is_super']:
            data['role_label'] = '最高权限'
        else:  # 侧栏左下角：身份标签（老师 / 2023级硕士）加“组长”，都没有时为“成员”
            data['role_label'] = ' · '.join(([user.tag] if user.tag else []) + (['组长'] if data['is_leader'] else [])) or '成员'
        # 查看 / 编辑两种模式：默认查看；最高权限和组长点“进入编辑”后才显示修改类操作（记在会话里）
        data['edit_mode'] = data['can_manage'] and bool(request.session.get('edit_mode'))
        data['unread_count'] = 0 if data['is_super'] else InstructionReceipt.objects.filter(user=user, read_at__isnull=True, instruction__withdrawn_at__isnull=True).count()
        # 最高权限：自己写的批示收到、还没看过的反馈数（侧栏“批示记录”角标）
        data['feedback_count'] = InstructionReceipt.objects.filter(instruction__author=user, seen_by_author=False, instruction__withdrawn_at__isnull=True).count() if data['is_super'] else 0
    return data
