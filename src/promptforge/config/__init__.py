#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Project: 提示词工坊项目
# Copyright (C) 2026 智维ITMSKY
# SPDX-License-Identifier: AGPL-3.0-or-later
"""配置层：厂商预设加载。providers.json 为单一真源。"""

from .providers import load_providers, DEFAULT_PROVIDERS

__all__ = ["load_providers", "DEFAULT_PROVIDERS"]
