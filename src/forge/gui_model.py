#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Project: 提示词工坊项目
# Copyright (c) 2026 智维工作室
# SPDX-License-Identifier: AGPL-3.0-or-later
"""GUI 控制器（纯逻辑，不 import tkinter）。

为什么要单独抽出这一层：
  界面依赖 tkinter，在无桌面环境里无法跑 unittest；而「载入→选章节→装配」
  这套状态机的判断逻辑恰恰最需要自动化回归。把逻辑放这里，让 **90% 的
  GUI 风险可以被无桌面地测掉**，界面层只负责把输入搬进来、把结果画出去。
"""

from . import parser as _p
from . import assembler as _a


class InstructionController:
    """持有当前文档与用户选择，串起 解析 → 选节 → 装配 的完整流程。"""

    def __init__(self):
        self.doc = None
        self.path = None
        self.selected = []          # 选中的标题行号（升序去重）

    # ---------------- 载入 ----------------
    def clear(self):
        self.doc, self.path, self.selected = None, None, []

    def load_file(self, path):
        """从文件载入并解析，返回大纲。失败时异常向上抛，由界面提示。"""
        with open(path, "r", encoding="utf-8") as f:
            return self.load_text(f.read(), path)

    def load_text(self, text, name="<粘贴文本>"):
        self.path = name
        self.doc = _p.parse(text, name)
        self.selected = []
        return self.outline()

    @property
    def loaded(self):
        return self.doc is not None

    # ---------------- 大纲与选择 ----------------
    def outline(self):
        """返回大纲列表；不含 num 的顶层文档标题由 pickable 标为不可选。"""
        if not self.doc:
            return []
        return [{"num": h.get("num", ""), "title": h["title"],
                 "line": h["line"], "level": h["level"],
                 "pickable": bool(h.get("num"))}
                for h in self.doc["headings"]]

    def set_selected(self, lines):
        self.selected = sorted({int(x) for x in lines})

    def toggle(self, line, on):
        s = set(self.selected)
        s.add(line) if on else s.discard(line)
        self.selected = sorted(s)

    def picks(self):
        """返回最终进入【范围】的标题，自动剔除「祖先已被选中」的冗余子孙项。"""
        if not self.doc:
            return []
        hs = [h for h in self.doc["headings"] if h["line"] in set(self.selected)]
        return [h for h in hs if not any(self._is_ancestor(a, h) for a in hs if a is not h)]

    def _is_ancestor(self, a, h):
        start, end = _p.section_span(self.doc, a["line"])
        return a["level"] < h["level"] and start < h["line"] <= end

    # ---------------- 装配 ----------------
    def detected(self):
        """返回定位信息摘要（命令/待确认/分期/依赖），供界面展示。"""
        if not self.doc:
            return {"commands": [], "ambiguities": [], "phases": [], "deps": []}
        d = self.doc
        return {"commands": d["commands"], "ambiguities": d["ambiguities"],
                "phases": d["phases"], "deps": d["deps"]}

    @staticmethod
    def split_list(raw):
        """把「逗号或换行分隔」的输入切成干净列表。"""
        return [x.strip() for x in (raw or "").replace("\n", ",").split(",") if x.strip()]

    @staticmethod
    def split_lines(raw):
        return [x.strip() for x in (raw or "").splitlines() if x.strip()]

    def generate(self, task=None, period=None, code=None, decisions=None):
        """执行装配；未载入文档时抛 ValueError，由界面转成友好提示。"""
        if not self.doc:
            raise ValueError("尚未载入文档")
        return _a.assemble(
            self.doc,
            picks=self.picks(),
            task=(task or "").strip() or None,
            code_files=self.split_list(code),
            decisions=self.split_lines(decisions),
            period=(period or "").strip() or None,
        )
