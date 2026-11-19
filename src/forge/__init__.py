#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Project: 提示词工坊项目
# Copyright (C) 2026 智维ITMSKY
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
InstructionForge —— 指令构造器
================================================================
把技术设计文档「编译」成五槽结构化提示词：
    指令 = 文档定位 + 本期切片 + 反向边界 + 可执行验收 + 歧义停工

核心取舍（与直接把全文塞进上下文的做法对比）：
  - 提示词里只放**行号锚点**（§3 L12-L40），不放正文，模型按需精读；
  - 解析与装配全程纯本地、零远程接口调用，因此结果确定、可复现、可 diff；
  - 槽位撑不起来就标 ❓待填并进「歧义停工」清单，绝不替用户猜测。
================================================================
"""

__version__ = "0.1.9"

from . import parser as _parser
from . import assembler as _assembler
from . import templates as _templates

__all__ = ["__version__", "parser", "assembler", "templates"]
