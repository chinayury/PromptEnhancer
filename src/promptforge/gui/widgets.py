#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Project: 提示词工坊项目
# Copyright (C) 2026 智维ITMSKY
# SPDX-License-Identifier: AGPL-3.0-or-later
"""GUI 通用组件：统一按钮样式，保证交互体验一致。"""

import customtkinter as ctk

# 视觉令牌（亮色主题：靛蓝主色 + 浅色界面）
PRIMARY = "#1B4FD8"
PRIMARY_HOVER = "#12359E"
CARD = "#FFFFFF"
BORDER = "#D9E0EA"


def pill(parent, text, command, primary=True, width=150, height=38):
    """统一风格的按钮：
      primary=True  —— 实心钴蓝（主要操作）
      primary=False —— 白底描边（次要操作）
    返回 CTkButton 实例，便于后续改文本/状态。
    """
    if primary:
        return ctk.CTkButton(
            parent, text=text, command=command, width=width, height=height,
            fg_color=PRIMARY, hover_color=PRIMARY_HOVER, text_color="#FFFFFF",
            corner_radius=4, font=("Microsoft YaHei", 13, "bold"), border_width=0)
    return ctk.CTkButton(
        parent, text=text, command=command, width=width, height=height,
        fg_color=CARD, hover_color="#EBF1FD", text_color=PRIMARY,
        corner_radius=4, font=("Microsoft YaHei", 13), border_color=PRIMARY,
        border_width=1.5)
