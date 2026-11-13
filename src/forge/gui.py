#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Project: 提示词工坊项目
# Copyright (c) 2026 智维工作室
# SPDX-License-Identifier: AGPL-3.0-or-later

import tkinter.filedialog as filedialog
import tkinter.messagebox as messagebox

import customtkinter as ctk

from . import __version__
from .gui_model import InstructionController

# 视觉令牌（浅色主题 · 翡翠青绿系，与 PromptForge 保持一致）
YAHEI = "Microsoft YaHei"
MONO = "Consolas"
PRIMARY = "#1B4FD8"
PRIMARY_HOVER = "#12359E"
ACCENT = "#0E8FA8"
BG = "#F0F3F8"
CARD = "#FFFFFF"
BORDER = "#D9E0EA"
TITLE = "#0F1A2E"
TEXT = "#2B3A50"
MUTED = "#6B7C93"
SOFT_TEAL = "#EBF1FD"
WARN = "#8A5300"
OK = "#0F7A43"

STEPS = [("①", "载入文档或粘贴内容"), ("②", "勾选本期切片"), ("③", "补齐五槽并生成")]


def _font(size, weight="normal"):
    return ctk.CTkFont(family=YAHEI, size=size, weight=weight)


def _ghost_btn(parent, text, command, width=100, height=38):
    """幽灵样式按钮：白底 + 主色描边 + 主色文字。"""
    return ctk.CTkButton(
        parent, text=text, command=command, width=width, height=height,
        fg_color=CARD, hover_color=SOFT_TEAL, text_color=PRIMARY,
        border_color=PRIMARY, border_width=1.5, corner_radius=4,
        font=_font(12))


class ForgePanel(ctk.CTkScrollableFrame):
    """嵌入版：作为 PromptForge 的一个内容分页。master 为分页父容器，app 为宿主应用。"""

    def __init__(self, master, app=None):
        super().__init__(master, fg_color=BG, corner_radius=0)
        self.app = app
        self.ctrl = InstructionController()
        self.checkbox_vars = {}
        self.last_prompt = ""

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=26, pady=(18, 8))
        ctk.CTkLabel(head, text="把设计文档编译为五槽结构化指令", font=_font(22, "bold"),
                     text_color=TITLE).pack(anchor="w")
        ctk.CTkLabel(head,
                     text="文档定位 · 本期切片 · 反向边界 · 可执行验收 · 歧义停工",
                     font=_font(12), text_color=MUTED).pack(anchor="w", pady=(6, 0))
        # 配置共享说明：本模块纯本地，密钥在宿主的「连接配置」统一配置
        ctk.CTkLabel(head,
                     text="本模块为纯本地工具，无需 API 密钥（增强模块的密钥在上方「连接配置」统一配置）",
                     font=_font(11), text_color=ACCENT).pack(anchor="w", pady=(4, 0))

        self._build_source_card()
        self._build_outline_card()
        self._build_slots_card()
        self._build_output_card()

    # 兼容 clipboard：优先用宿主根窗口，保证复制动作稳定
    def _clip(self):
        return self.app.root if (self.app and hasattr(self.app, "root")) else self

    def _card(self, title, subtitle=None):
        f = ctk.CTkFrame(self, fg_color=CARD, corner_radius=14,
                         border_width=1, border_color=BORDER)
        f.pack(fill="x", padx=26, pady=(0, 14))
        ctk.CTkLabel(f, text=title, font=_font(18, "bold"),
                     text_color=TITLE).pack(anchor="w", padx=20, pady=(16, 4))
        if subtitle:
            ctk.CTkLabel(f, text=subtitle, font=_font(12),
                         text_color=MUTED).pack(anchor="w", padx=20)
        body = ctk.CTkFrame(f, fg_color="transparent")
        body.pack(fill="x", padx=20, pady=(10, 18))
        return f, body

    # ================= 卡片①：文档来源 =================
    def _build_source_card(self):
        _, body = self._card("① 载入设计文档", "选择设计文档，或直接粘贴内容（两者都有时以文件为准）")
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(fill="x")
        ctk.CTkLabel(row, text="文件路径", font=_font(12), text_color=MUTED,
                     width=72, anchor="e").pack(side="left")
        self.var_path = ctk.StringVar()
        self.ent_path = ctk.CTkEntry(row, textvariable=self.var_path, font=_font(12))
        self.ent_path.pack(side="left", fill="x", expand=True, padx=(10, 8))
        ctk.CTkButton(row, text="浏览…", width=90, font=_font(12), fg_color=PRIMARY,
                      hover_color=PRIMARY_HOVER, corner_radius=4,
                      command=self._browse).pack(side="left")

        ctk.CTkLabel(body, text="或粘贴文档内容", font=_font(12),
                     text_color=MUTED).pack(anchor="w", pady=(12, 4))
        self.txt_paste = ctk.CTkTextbox(body, height=96, font=(MONO, 11))
        self.txt_paste.pack(fill="x")

        ctk.CTkButton(body, text="✦ 解析并列出目录", height=38, font=_font(13, "bold"),
                      fg_color=PRIMARY, hover_color=PRIMARY_HOVER, corner_radius=4,
                      command=self._on_parse).pack(anchor="w", pady=(14, 0))

    def _browse(self):
        p = filedialog.askopenfilename(
            title="选择设计文档",
            filetypes=[("Markdown", "*.md"), ("文本", "*.txt"), ("所有文件", "*.*")])
        if p:
            self.var_path.set(p)

    def _on_parse(self):
        path = self.var_path.get().strip()
        paste = self.txt_paste.get("1.0", "end").strip()
        if not path and not paste:
            messagebox.showwarning("缺少输入", "请先选择文件，或粘贴文档内容。")
            return
        try:
            if path:
                self.ctrl.load_file(path)
            else:
                self.ctrl.load_text(paste)
        except FileNotFoundError:
            messagebox.showerror("文件不存在", "找不到该文件：\n%s" % path)
            return
        except UnicodeDecodeError:
            messagebox.showerror("编码错误", "无法按 UTF-8 解码，请确认文件编码后重试。")
            return
        except Exception as e:                       # 兜底，避免界面静默崩溃
            messagebox.showerror("解析失败", "%s: %s" % (type(e).__name__, e))
            return
        self._rebuild_outline()

    # ================= 卡片②：本期切片 =================
    def _build_outline_card(self):
        _, body = self._card("② 勾选本期切片", "勾选本期要做的章节；选父章节会自动收敛其已勾选的子节")
        bar = ctk.CTkFrame(body, fg_color="transparent")
        bar.pack(fill="x", pady=(0, 8))
        self.lbl_detect = ctk.CTkLabel(bar, text="尚未载入文档", font=_font(12),
                                       text_color=MUTED)
        self.lbl_detect.pack(side="left")
        _ghost_btn(bar, "清空勾选", lambda: self._bulk(False), 88).pack(
            side="right", padx=(8, 0))
        _ghost_btn(bar, "全选章节", lambda: self._bulk(True), 88).pack(side="right")

        self.box_outline = ctk.CTkScrollableFrame(body, height=210, fg_color=BG,
                                                  corner_radius=8)
        self.box_outline.pack(fill="x")
        ctk.CTkLabel(self.box_outline, text="载入文档后在此显示章节树",
                     font=_font(12), text_color=MUTED).pack(pady=24)

    def _bulk(self, on):
        for line, var in self.checkbox_vars.items():
            var.set(on)
            self.ctrl.toggle(line, on)

    def _rebuild_outline(self):
        for w in self.box_outline.winfo_children():
            w.destroy()
        self.checkbox_vars = {}
        self.ctrl.set_selected([])

        for item in self.ctrl.outline():
            if not item["pickable"]:                 # 顶层文档标题不可选
                continue
            indent = "  " * max(0, item["level"] - 2)
            var = ctk.BooleanVar(value=False)
            label = "%s§%-6s %s" % (indent, item["num"], item["title"])
            cb = ctk.CTkCheckBox(
                self.box_outline, text=label, variable=var, font=(MONO, 11),
                command=lambda ln=item["line"], v=var: self.ctrl.toggle(ln, v.get()))
            cb.pack(anchor="w", padx=(8 + 14 * max(0, item["level"] - 2), 4), pady=2)
            self.checkbox_vars[item["line"]] = var

        d = self.ctrl.detected()
        self.lbl_detect.configure(
            text="共 %d 章 | 命令 %d 条 | 待确认 %d 处 | 分期 %d 处"
                 % (len(self.ctrl.outline()), len(d["commands"]),
                    len(d["ambiguities"]), len(d["phases"])),
            text_color=ACCENT)

    # ================= 卡片③：五槽输入 =================
    def _build_slots_card(self):
        _, body = self._card("③ 填写五槽", "缺失会触发【歧义停工】，工具绝不替你猜测——请补齐再生成")
        body.grid_columnconfigure(1, weight=1)
        body.grid_columnconfigure(3, weight=1)

        def field(label, col, row, width=150, ph=""):
            ctk.CTkLabel(body, text=label, font=_font(12), text_color=MUTED,
                         width=width, anchor="e").grid(row=row, column=col, padx=(0, 10), pady=8,
                                                       sticky="e")
            v = ctk.StringVar()
            e = ctk.CTkEntry(body, textvariable=v, font=_font(12), placeholder_text=ph)
            e.grid(row=row, column=col + 1, padx=(0, 18), pady=8, sticky="ew")
            return v

        self.var_task = field("任务", 0, 0, ph="动词开头，如：实现 verify 签名防伪")
        self.var_period = field("本期", 2, 0, ph="如：一期 / Phase 1")
        self.var_code = field("代码文件", 0, 1, ph="逗号分隔：internal/a.go,b.go")

        ctk.CTkLabel(body, text="决策答复", font=_font(12), text_color=MUTED,
                     width=150, anchor="e").grid(row=1, column=2, padx=(0, 10), sticky="e")
        self.txt_decision = ctk.CTkTextbox(body, height=68, font=(MONO, 11))
        self.txt_decision.grid(row=1, column=3, padx=(0, 0), pady=8, sticky="ew")

        ctk.CTkLabel(body, text="每行一条，对应文档里的待确认 / TODO 项",
                     font=_font(11), text_color=MUTED).grid(
                         row=2, column=1, columnspan=3, sticky="w", pady=(0, 4))

    # ================= 卡片④：输出 =================
    def _build_output_card(self):
        _, body = self._card("④ 生成指令", "结果可直接复制使用；报告与停工提示显示在下方状态")
        acts = ctk.CTkFrame(body, fg_color="transparent")
        acts.pack(fill="x", pady=(0, 10))
        ctk.CTkButton(acts, text="▶ 生成五槽指令", width=170, height=40,
                      font=_font(13, "bold"), fg_color=PRIMARY,
                      hover_color=PRIMARY_HOVER, corner_radius=4,
                      command=self._on_generate).pack(side="left")
        self.btn_copy = _ghost_btn(acts, "⧉ 复制", self._copy, 110, 40)
        self.btn_copy.pack(side="right", padx=(8, 0))
        self.btn_save = _ghost_btn(acts, "⤓ 保存为 md", self._save, 130, 40)
        self.btn_save.pack(side="right")

        self.txt_out = ctk.CTkTextbox(body, height=320, font=(MONO, 12))
        self.txt_out.pack(fill="x")

        self.lbl_status = ctk.CTkLabel(body, text="等待生成", font=_font(12),
                                       text_color=MUTED, justify="left")
        self.lbl_status.pack(anchor="w", pady=(10, 0))

    def _on_generate(self):
        if not self.ctrl.loaded:
            messagebox.showwarning("尚未解析", "请先在上面①载入并解析文档。")
            return
        try:
            res = self.ctrl.generate(
                task=self.var_task.get(),
                period=self.var_period.get(),
                code=self.var_code.get(),
                decisions=self.txt_decision.get("1.0", "end"))
        except ValueError as e:
            messagebox.showwarning("无法生成", str(e))
            return
        except Exception as e:                       # 引擎异常兜底，界面不崩
            messagebox.showerror("生成失败", "%s: %s" % (type(e).__name__, e))
            return

        self.last_prompt = res["prompt"]
        self.txt_out.delete("1.0", "end")
        self.txt_out.insert("1.0", self.last_prompt)

        rep = res["report"]
        if rep.get("saved_pct", 0) > 0:
            save_txt = "节省 %s%%" % rep["saved_pct"]
        else:
            save_txt = "文档偏短，省幅尚未体现"
        if res["unresolved"]:
            self.lbl_status.configure(
                text="⚠️ 存在 %d 处未确认槽位，已写入【歧义停工】清单，先答复再开工\n"
                     "文档 %d 行 | 全文≈%d tokens | 提示词≈%d tokens | %s"
                     % (len(res["unresolved"]), rep["doc_lines"],
                        rep["full_tokens"], rep["prompt_tokens"], save_txt),
                text_color=WARN)
        else:
            self.lbl_status.configure(
                text="✅ 五槽齐备，可直接发出 | 文档 %d 行 | 全文≈%d tokens | "
                     "提示词≈%d tokens | %s"
                     % (rep["doc_lines"], rep["full_tokens"],
                        rep["prompt_tokens"], save_txt),
                text_color=OK)

    def _get_prompt(self):
        return self.txt_out.get("1.0", "end").strip()

    def _copy(self):
        p = self._get_prompt()
        if not p:
            messagebox.showwarning("无内容", "还没有生成结果。")
            return
        c = self._clip()
        c.clipboard_clear()
        c.clipboard_append(p)
        c.update()
        messagebox.showinfo("已复制", "提示词已复制到剪贴板。")

    def _save(self):
        p = self._get_prompt()
        if not p:
            messagebox.showwarning("无内容", "还没有生成结果。")
            return
        path = filedialog.asksaveasfilename(
            title="保存提示词", defaultextension=".md",
            filetypes=[("Markdown", "*.md"), ("文本", "*.txt")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(p + "\n")
        except Exception as e:
            messagebox.showerror("保存失败", "%s: %s" % (type(e).__name__, e))
            return
        messagebox.showinfo("已保存", "已写入：%s" % path)


class ForgeApp:
    """独立运行入口（python -m forge.gui）。"""

    def __init__(self):
        ctk.set_appearance_mode("light")          # 强制亮色主题
        ctk.set_default_color_theme("blue")
        self.root = ctk.CTk()
        self.root.title("指令构造器 · InstructionForge v%s" % __version__)
        self.root.geometry("1220x880")
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(0, weight=1)
        panel = ForgePanel(self.root, app=self)
        panel.pack(fill="both", expand=True)

    def run(self):
        self.root.mainloop()


def main():
    ForgeApp().run()


if __name__ == "__main__":
    main()
