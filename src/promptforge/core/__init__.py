#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Project: 提示词工坊项目
# Copyright (c) 2026 智维工作室
# SPDX-License-Identifier: AGPL-3.0-or-later
"""业务层：提示词模板与增强引擎。"""

from .templates import (
    SYSTEM_TEMPLATE,
    USER_TEMPLATE,
    STYLE_GUIDANCE,
    STYLE_LABEL_TO_KEY,
    STYLE_KEY_TO_LABEL,
    TEMP_OPTIONS,
    MAXTOKEN_OPTIONS,
)
from .engine import (
    EnhanceError,
    AuthError,
    RateLimitError,
    TimeoutErrorEx,
    NetworkError,
    EmptyResponseError,
    enhance,
    is_low_quality,
    validate_config,
)
# 注意：lint 模块（promptforge/core/lint.py）不在此处重导出其函数，
# 以免函数名 `lint` 遮蔽子模块名；调用方用 `from promptforge.core import lint`
# 取得模块，再调用 lint.lint(text)。

__all__ = [
    "SYSTEM_TEMPLATE",
    "USER_TEMPLATE",
    "STYLE_GUIDANCE",
    "STYLE_LABEL_TO_KEY",
    "STYLE_KEY_TO_LABEL",
    "TEMP_OPTIONS",
    "MAXTOKEN_OPTIONS",
    "EnhanceError",
    "AuthError",
    "RateLimitError",
    "TimeoutErrorEx",
    "NetworkError",
    "EmptyResponseError",
    "enhance",
    "is_low_quality",
    "validate_config",
]
