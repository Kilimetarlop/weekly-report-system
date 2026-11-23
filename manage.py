# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
import os
import sys
from pathlib import Path
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
sys.path.insert(0, str(Path(__file__).parent / '.packages'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
from django.core.management import execute_from_command_line
execute_from_command_line(sys.argv)
