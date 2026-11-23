# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
from django.core.wsgi import get_wsgi_application
application = get_wsgi_application()
