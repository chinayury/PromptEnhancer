#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Project: 提示词工坊项目
# Copyright (c) 2026 智维工作室
# SPDX-License-Identifier: AGPL-3.0-or-later
"""包入口：python -m promptforge [cli ...]"""

import os
import sys

from .utils.logging_setup import configure_logging, set_log_file, ensure_utf8_console
from .data import database as db

# 在导入任何会触发日志初始化的模块前，确定日志文件路径
set_log_file(os.path.join(db.BASE_DIR, "promptforge.log"))


def _gui_available():
    try:
        import customtkinter  # noqa: F401
        return True
    except ImportError:
        return False


def main():
    ensure_utf8_console()
    configure_logging()
    db.init_db()
    if len(sys.argv) > 1 and sys.argv[1] == "cli":
        from . import cli
        sys.exit(cli.run(sys.argv[2:]))
    if not _gui_available():
        print("=" * 60)
        print("缺少 customtkinter 库，无法启动 GUI。")
        print("可安装依赖后用 GUI：  pip install -r requirements.txt")
        print("或使用命令行模式：    python -m promptforge cli --help")
        print("=" * 60)
        sys.exit(2)
    from . import gui
    app = gui.PromptForgeApp()
    app.run()


if __name__ == "__main__":
    main()
