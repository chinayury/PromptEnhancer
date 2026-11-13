#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Project: 提示词工坊项目
# Copyright (c) 2026 智维工作室
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
统一日志规范。

设计要点：
  - 提供模块级 get_logger(name) 获取命名 logger，便于按组件分级。
  - configure_logging() 在程序启动时调用一次，同时输出到控制台与文件
    （默认 <数据目录>/promptforge.log），便于排查问题。
  - 所有写日志的地方都**不得包含 API Key 等敏感字段**（调用方负责脱敏）。
"""

import logging
import os

DEFAULT_LOG_FILE = None
_CONFIGURED = False


def set_log_file(path):
    """指定日志文件路径（需在 configure_logging 之前调用）。"""
    global DEFAULT_LOG_FILE
    DEFAULT_LOG_FILE = path


def configure_logging(level=logging.INFO):
    """配置根日志：控制台 + 文件双出口。幂等，重复调用只生效一次。
    日志文件路径优先级：set_log_file() 显式指定 > 环境变量 PROMPTFORGE_LOG_FILE > 不写文件。"""
    global _CONFIGURED
    if _CONFIGURED:
        return
    handlers = [logging.StreamHandler()]
    log_file = DEFAULT_LOG_FILE or os.environ.get("PROMPTFORGE_LOG_FILE")
    if log_file:
        try:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        except Exception:
            # 日志文件不可写不应阻断主流程
            pass
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )
    _CONFIGURED = True


def get_logger(name):
    """获取命名 logger。日志输出由 configure_logging() 统一配置（幂等，通常在程序入口调用一次）。
    在 configure_logging 之前产生的日志会经 logging 的 lastResort 落到 stderr（仅 WARNING 及以上）。"""
    return logging.getLogger(name)
