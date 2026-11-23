# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
from django import forms
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm


class AccountLoginForm(AuthenticationForm):
    username = forms.CharField(label='账号', max_length=150, widget=forms.TextInput(attrs={'autocomplete': 'username', 'autocapitalize': 'none', 'spellcheck': 'false', 'placeholder': '请输入账号', 'autofocus': True}))
    password = forms.CharField(label='密码', strip=False, widget=forms.PasswordInput(attrs={'autocomplete': 'current-password', 'placeholder': '请输入密码'}))

    def clean_username(self):
        return self.cleaned_data['username'].strip()


class ChangeOwnPasswordForm(PasswordChangeForm):
    """本人修改密码：须输入原密码；新密码同样执行系统密码规则，且不能与原密码相同。"""

    def clean(self):
        # Django 5.2 的密码一致性与强度校验都在 clean() 中完成，这里在其后追加“不得与原密码相同”。
        cleaned = super().clean()
        old, new = self.cleaned_data.get('old_password'), self.cleaned_data.get('new_password1')
        if old and new and old == new and not self.has_error('new_password2'):
            self.add_error('new_password2', '新密码不能与原密码相同')
        return cleaned
