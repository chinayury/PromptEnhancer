#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Project: 提示词工坊项目
# Copyright (C) 2026 智维ITMSKY
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
提示词工坊 · PromptForge  v0.1.8
================================================================
一款本地运行的提示词增强工具：把一句原始提示词，借助任意通用兼容文本生成接口，
改写为更清晰、更专业、更可执行的版本；并内置「指令构造器」，把技术设计文档
编译成五槽结构化指令。所有数据（连接配置、增强历史、清空审计）均存于本地
SQLite，默认不上云。

分层结构：
  promptforge.core     —— 业务层：提示词模板与增强引擎（与 UI / 存储解耦）
  promptforge.data     —— 数据层：SQLite 持久化、原子备份、审计与统计
  promptforge.config   —— 配置层：厂商预设加载（providers.json 为单一真源）
  promptforge.gui      —— 表现层：customtkinter 桌面界面
  promptforge.cli      —— 表现层：无界面命令行模式（多场景适配）
  promptforge.utils    —— 工具层：日志等通用能力

入口：python -m promptforge（启动桌面 GUI；python -m promptforge cli ... 进入命令行）。
================================================================
"""

__version__ = "0.1.8"

# 版本口径（v0.1.8）：对外统一使用 vX.Y.Z 单版本号，随发布递增
# （2026-11-14 起按维护循环规则常规优化/修复 → patch 升号）。
# 历史：桌面线曾用 v0.1.0→1.x；Web 线 v1.x(noVNC)/v2.0(Flask 单口令)/
# 3.0(多用户)/3.1(后台)/3.2(自助注册)/3.3(用量·检索·模板变量)，
# 2026-10-12 起内部 3.x 线全部归一为 v0.1.0，沿革详见 CHANGELOG.md 与
# docs/版本迭代说明-v0.1.0.md。

# 注意：gui / cli 不在包初始化时导入，以支持无桌面（服务器/CI）环境下
# 直接 import promptforge 并运行命令行模式，无需安装 customtkinter。
__all__ = ["__version__", "core", "data", "config", "gui", "cli", "utils"]
