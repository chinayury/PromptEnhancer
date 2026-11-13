#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Project: 提示词工坊项目
# Copyright (C) 2026 智维ITMSKY
# SPDX-License-Identifier: AGPL-3.0-or-later
"""包入口：python -m forge ...（指令构造器命令行）"""

import sys
from .cli import main

if __name__ == "__main__":
    sys.exit(main())
