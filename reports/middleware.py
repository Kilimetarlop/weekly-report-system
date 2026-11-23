# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
from django.contrib.auth import logout
from django.shortcuts import redirect
from .services import active


class ActiveMembershipMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and not active(request.user):
            logout(request)
            return redirect('/login/')
        return self.get_response(request)


class PrivateCacheMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response['Cache-Control'] = 'no-store, private'
        return response
