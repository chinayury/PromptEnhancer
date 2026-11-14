#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Project: 提示词工坊项目
# Copyright (C) 2026 智维ITMSKY
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
import csv
import threading
import time
import tkinter.filedialog as filedialog
import tkinter.messagebox as messagebox

import customtkinter as ctk

from ..core import engine, templates
from ..data import database as db
from ..config import load_providers
from ..utils.logging_setup import get_logger
from .widgets import pill as _pill_widget
from . import widgets as _widgets_mod
from forge.gui import ForgePanel

logger = get_logger("promptforge.gui.app")

# 视觉设计令牌（浅色主题 · 钴蓝工坊 Cobalt Forge，与 Web 端同套令牌）
YAHEI = "Microsoft YaHei"
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

# 让共享按钮组件（widgets.pill）跟随本主题的钴蓝主色，无需改动其源文件
_widgets_mod.PRIMARY = PRIMARY
_widgets_mod.PRIMARY_HOVER = PRIMARY_HOVER
_widgets_mod.CARD = CARD
_widgets_mod.BORDER = BORDER


def _font(size, weight="normal"):
    return ctk.CTkFont(family=YAHEI, size=size, weight=weight)


def pill(parent, text, command, primary=True, width=150, height=38):
    """统一风格的按钮（外观跟随翡翠青绿主题）：
      primary=True  —— 实心主色（主要操作）
      primary=False —— 白底描边（次要操作）
    """
    b = _pill_widget(parent, text, command, primary, width, height)
    if primary:
        b.configure(fg_color=PRIMARY, hover_color=PRIMARY_HOVER, text_color="#FFFFFF")
    else:
        b.configure(fg_color=CARD, hover_color=SOFT_TEAL, text_color=PRIMARY,
                    border_color=PRIMARY, border_width=1.5)
    return b


NAV_ITEMS = [
    ("single", "✎", "单条增强"),
    ("batch", "☰", "批量增强"),
    ("history", "⟳", "历史档案"),
    ("forge", "⚙", "指令构造"),
]

PAGE_META = {
    "single": ("✎ 单条提示词增强", "输入一句话，立即生成更专业、更清晰的提示词"),
    "batch": ("☰ 批量提示词增强", "一次打磨一批提示，支持导出 CSV 归档"),
    "history": ("⟳ 历史打磨档案", "自动保存你的每一次灵感打磨，可随时复用与统计"),
    "forge": ("⚙ 五槽指令构造", "把设计文档编译成五槽结构化指令，纯本地、不消耗 token"),
}

DISPLAY_LIMIT = 100  # 历史档案页展示上限（数据库本身不截断，便于统计）


class PromptForgeApp:
    def __init__(self):
        self.cfg = db.load_config()
        self.providers = load_providers()
        self.last_original = ""

        ctk.set_appearance_mode("light")          # 强制亮色主题
        ctk.set_default_color_theme("blue")
        self.root = ctk.CTk()
        self.root.title("提示词工坊 · PromptForge v%s" % _version())
        self.root.geometry("1200x820")
        self.root.grid_columnconfigure(1, weight=1)
        self.root.grid_rowconfigure(0, weight=1)

        self._build_sidebar()
        self._build_main()
        self._refresh_history()

    # ---------------- 侧边栏 ----------------
    def _build_sidebar(self):
        sb = ctk.CTkFrame(self.root, width=244, fg_color=CARD, corner_radius=0,
                          border_width=1, border_color=BORDER)
        sb.grid(row=0, column=0, sticky="ns")
        sb.grid_propagate(False)

        brand = ctk.CTkFrame(sb, fg_color="transparent")
        brand.pack(fill="x", padx=22, pady=(30, 22))
        ctk.CTkLabel(brand, text="✦", font=_font(38, "bold"), text_color=PRIMARY).pack(anchor="w")
        ctk.CTkLabel(brand, text="提示词工坊", font=_font(21, "bold"),
                     text_color=TITLE).pack(anchor="w", pady=(8, 0))
        ctk.CTkLabel(brand, text="PromptForge · 让每句话更精准",
                     font=_font(12), text_color=MUTED).pack(anchor="w", pady=(3, 0))

        nav = ctk.CTkFrame(sb, fg_color="transparent")
        nav.pack(fill="x", padx=14, pady=(8, 0))
        ctk.CTkLabel(nav, text="功能导航", font=_font(12, "bold"),
                     text_color=MUTED).pack(anchor="w", padx=8, pady=(0, 6))
        self.nav_buttons = {}
        for key, icon, label in NAV_ITEMS:
            b = ctk.CTkButton(
                nav, text="  %s  %s" % (icon, label),
                command=lambda k=key: self._show_page(k),
                width=200, height=46, anchor="w", corner_radius=4,
                fg_color="transparent", hover_color=SOFT_TEAL,
                text_color=TEXT, font=_font(14), border_width=0)
            b.pack(fill="x", pady=5, padx=4)
            self.nav_buttons[key] = b

        footer = ctk.CTkFrame(sb, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=22, pady=18)
        ctk.CTkLabel(footer, text="● 本地运行 · 数据不出本机", font=_font(11),
                     text_color=MUTED).pack(anchor="w")
        ctk.CTkLabel(footer, text="v%s  本地提示词增强 · SQLite 存储" % _version(),
                     font=_font(11), text_color=MUTED).pack(anchor="w", pady=(4, 0))

    # ---------------- 主内容区 ----------------
    def _build_main(self):
        main = ctk.CTkFrame(self.root, fg_color=BG, corner_radius=0)
        main.grid(row=0, column=1, sticky="nsew")

        header = ctk.CTkFrame(main, fg_color="transparent")
        header.pack(fill="x", padx=24, pady=(24, 4))
        self.page_title = ctk.CTkLabel(header, text="", font=_font(22, "bold"), text_color=TITLE)
        self.page_title.pack(anchor="w")
        self.page_desc = ctk.CTkLabel(header, text="", font=_font(13), text_color=MUTED)
        self.page_desc.pack(anchor="w", pady=(5, 0))

        self._build_settings(main)

        self.content = ctk.CTkFrame(main, fg_color="transparent")
        self.content.pack(fill="both", expand=True, padx=24, pady=(6, 12))

        self.status_var = ctk.StringVar(value="就绪 · 选择一个功能开始")
        status_bar = ctk.CTkFrame(main, fg_color=CARD, corner_radius=0,
                                  border_width=1, border_color=BORDER)
        status_bar.pack(fill="x", side="bottom")
        anchor_block = ctk.CTkFrame(status_bar, fg_color=PRIMARY, width=6, corner_radius=0)
        anchor_block.pack(side="left", fill="y")
        self.status = ctk.CTkLabel(status_bar, textvariable=self.status_var, anchor="w",
                                   font=_font(12), text_color=MUTED)
        self.status.pack(side="left", fill="x", expand=True, padx=14, pady=9)

        self.pages = {}
        for key in ("single", "batch", "history", "forge"):
            self.pages[key] = ctk.CTkFrame(self.content, fg_color="transparent")
        self._build_single_page(self.pages["single"])
        self._build_batch_page(self.pages["batch"])
        self._build_history_page(self.pages["history"])
        self._build_forge_page(self.pages["forge"])
        self._show_page("single")

    def _build_settings(self, main):
        card = ctk.CTkFrame(main, fg_color=CARD, corner_radius=14,
                            border_width=1, border_color=BORDER)
        card.pack(fill="x", padx=24, pady=(10, 14))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=20, pady=18)
        inner.grid_columnconfigure(0, weight=1)

        head = ctk.CTkFrame(inner, fg_color="transparent")
        head.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 4))
        ctk.CTkLabel(head, text="⚙ 连接配置", font=_font(18, "bold"), text_color=TITLE).pack(side="left")
        ctk.CTkLabel(head, text="配置自动保存在本机，下次启动自动加载", font=_font(11),
                     text_color=MUTED).pack(side="right")

        grid = ctk.CTkFrame(inner, fg_color="transparent")
        grid.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        grid.grid_columnconfigure(0, minsize=78)
        grid.grid_columnconfigure(1, weight=1, minsize=190)
        grid.grid_columnconfigure(2, minsize=78)
        grid.grid_columnconfigure(3, weight=1, minsize=190)

        LBL = dict(font=_font(12), text_color=MUTED, anchor="e")
        PAD = dict(pady=6)

        ctk.CTkLabel(grid, text="厂商", **LBL).grid(row=0, column=0, sticky="e", padx=(0, 10), **PAD)
        self.provider_var = ctk.StringVar(value=self.cfg.get("provider", "DeepSeek"))
        self.provider_cb = ctk.CTkComboBox(
            grid, values=list(self.providers.keys()), variable=self.provider_var,
            height=34, command=self._on_provider_change, fg_color=CARD, border_color=BORDER,
            button_color=PRIMARY, button_hover_color=PRIMARY_HOVER, dropdown_fg_color=CARD,
            dropdown_text_color=TEXT, font=_font(12))
        self.provider_cb.grid(row=0, column=1, sticky="ew", padx=(0, 16), **PAD)
        ctk.CTkLabel(grid, text="API Key", **LBL).grid(row=0, column=2, sticky="e", padx=(0, 10), **PAD)
        self.key_entry = ctk.CTkEntry(grid, height=34, show="*", placeholder_text="粘贴厂商密钥",
                                      fg_color=CARD, border_color=BORDER, font=_font(12))
        self.key_entry.insert(0, self.cfg.get("api_key", ""))
        self.key_entry.grid(row=0, column=3, sticky="ew", padx=(0, 4), **PAD)

        ctk.CTkLabel(grid, text="Base URL", **LBL).grid(row=1, column=0, sticky="e", padx=(0, 10), **PAD)
        self.url_entry = ctk.CTkEntry(grid, height=34, placeholder_text="https://.../v1",
                                      fg_color=CARD, border_color=BORDER, font=_font(12))
        self.url_entry.insert(0, self.cfg.get("base_url", ""))
        self.url_entry.grid(row=1, column=1, columnspan=3, sticky="ew", padx=(0, 4), **PAD)

        ctk.CTkLabel(grid, text="模型", **LBL).grid(row=2, column=0, sticky="e", padx=(0, 10), **PAD)
        self.model_entry = ctk.CTkEntry(grid, height=34, placeholder_text="model name",
                                        fg_color=CARD, border_color=BORDER, font=_font(12))
        self.model_entry.insert(0, self.cfg.get("model", ""))
        self.model_entry.grid(row=2, column=1, sticky="ew", padx=(0, 16), **PAD)
        ctk.CTkLabel(grid, text="增强风格", **LBL).grid(row=2, column=2, sticky="e", padx=(0, 10), **PAD)
        self.style_var = ctk.StringVar(
            value=templates.STYLE_KEY_TO_LABEL.get(self.cfg.get("style", "balanced"), "均衡"))
        self.style_cb = ctk.CTkComboBox(
            grid, values=list(templates.STYLE_LABEL_TO_KEY.keys()), variable=self.style_var,
            height=34, fg_color=CARD, border_color=BORDER, button_color=PRIMARY,
            button_hover_color=PRIMARY_HOVER, dropdown_fg_color=CARD, dropdown_text_color=TEXT,
            font=_font(12))
        self.style_cb.grid(row=2, column=3, sticky="ew", padx=(0, 4), **PAD)

        ctk.CTkLabel(grid, text="温度", **LBL).grid(row=3, column=0, sticky="e", padx=(0, 10), **PAD)
        self.temp_var = ctk.StringVar(value=str(self.cfg.get("temperature", "0.7")))
        self.temp_cb = ctk.CTkComboBox(
            grid, values=templates.TEMP_OPTIONS, variable=self.temp_var, height=34,
            fg_color=CARD, border_color=BORDER, button_color=PRIMARY, button_hover_color=PRIMARY_HOVER,
            dropdown_fg_color=CARD, dropdown_text_color=TEXT, font=_font(12))
        self.temp_cb.grid(row=3, column=1, sticky="ew", padx=(0, 16), **PAD)
        ctk.CTkLabel(grid, text="最大长度", **LBL).grid(row=3, column=2, sticky="e", padx=(0, 10), **PAD)
        self.maxtok_var = ctk.StringVar(value=str(self.cfg.get("max_tokens", "1024")))
        self.maxtok_cb = ctk.CTkComboBox(
            grid, values=templates.MAXTOKEN_OPTIONS, variable=self.maxtok_var, height=34,
            fg_color=CARD, border_color=BORDER, button_color=PRIMARY, button_hover_color=PRIMARY_HOVER,
            dropdown_fg_color=CARD, dropdown_text_color=TEXT, font=_font(12))
        self.maxtok_cb.grid(row=3, column=3, sticky="ew", padx=(0, 4), **PAD)

        ctk.CTkLabel(grid, text="目标角色", **LBL).grid(row=4, column=0, sticky="e", padx=(0, 10), **PAD)
        self.mode_var = ctk.StringVar(
            value=templates.MODE_KEY_TO_LABEL.get(self.cfg.get("mode", "user"), "用户提示词"))
        self.mode_cb = ctk.CTkComboBox(
            grid, values=list(templates.MODE_LABEL_TO_KEY.keys()), variable=self.mode_var,
            height=34, fg_color=CARD, border_color=BORDER, button_color=PRIMARY,
            button_hover_color=PRIMARY_HOVER, dropdown_fg_color=CARD, dropdown_text_color=TEXT,
            font=_font(12))
        self.mode_cb.grid(row=4, column=1, sticky="ew", padx=(0, 16), **PAD)

        foot = ctk.CTkFrame(inner, fg_color="transparent")
        foot.grid(row=2, column=0, columnspan=4, sticky="e", pady=(16, 0))
        self.show_key_btn = pill(foot, "◎ 显示", self._toggle_key, False, 90)
        self.show_key_btn.pack(side="right", padx=(10, 0))
        self.save_btn = pill(foot, "✔ 保存配置", self._save_cfg, True, 120)
        self.save_btn.pack(side="right")

    def _show_page(self, name):
        for k, p in self.pages.items():
            if k == name:
                p.pack(fill="both", expand=True)
            else:
                p.pack_forget()
        for k, b in self.nav_buttons.items():
            if k == name:
                b.configure(fg_color=PRIMARY, text_color="#FFFFFF", hover_color=PRIMARY_HOVER)
            else:
                b.configure(fg_color="transparent", text_color=TEXT, hover_color=SOFT_TEAL)
        title, desc = PAGE_META[name]
        self.page_title.configure(text=title)
        self.page_desc.configure(text=desc)

    # ---------------- 配置交互 ----------------
    def _on_provider_change(self, _=None):
        p = self.provider_var.get()
        preset = self.providers.get(p)
        if preset:
            if preset.get("base_url"):
                self.url_entry.delete(0, "end")
                self.url_entry.insert(0, preset["base_url"])
            if preset.get("model"):
                self.model_entry.delete(0, "end")
                self.model_entry.insert(0, preset["model"])
        self._set_status("已切换厂商预设: %s — %s" % (p, preset.get("note", "")))

    def _toggle_key(self):
        cur = self.key_entry.cget("show")
        self.key_entry.configure(show="" if cur == "*" else "*")

    def _save_cfg(self):
        self.cfg = self._current_cfg(persist=False)
        db.save_config(self.cfg)
        self._set_status("配置已保存到本地数据库（promptforge.db）")

    def _current_cfg(self, persist=True):
        # 环境变量优先级高于界面保存值，便于在 CI / 无界面场景注入密钥
        env_key = os.environ.get("PROMPTFORGE_API_KEY")
        api_key = env_key if env_key else self.key_entry.get().strip()
        cfg = {
            "provider": self.provider_var.get(),
            "api_key": api_key,
            "base_url": self.url_entry.get().strip(),
            "model": self.model_entry.get().strip(),
            "temperature": self.temp_var.get().strip(),
            "max_tokens": self.maxtok_var.get().strip(),
            "style": templates.STYLE_LABEL_TO_KEY.get(self.style_var.get(), "balanced"),
            "mode": templates.MODE_LABEL_TO_KEY.get(self.mode_var.get(), "user"),
        }
        if persist:
            db.save_config(cfg)
            self.cfg = cfg
        return cfg

    # ---------------- 单条增强页 ----------------
    def _build_single_page(self, p):
        bar = ctk.CTkFrame(p, fg_color="transparent")
        bar.pack(fill="x", pady=(0, 12))
        self.enhance_btn = pill(bar, "✦ 增强提示词", self._enhance, True, 170)
        self.enhance_btn.pack(side="left", padx=(0, 10))
        pill(bar, "⧉ 复制结果", self._copy_result, False, 120).pack(side="left", padx=(0, 8))
        pill(bar, "↺ 还原输入", self._restore_input, False, 120).pack(side="left", padx=(0, 8))
        pill(bar, "⌫ 清空输入", self._clear_single, False, 110).pack(side="left")

        cols = ctk.CTkFrame(p, fg_color="transparent")
        cols.pack(fill="both", expand=True)
        cols.grid_columnconfigure(0, weight=1)
        cols.grid_columnconfigure(1, weight=1)
        cols.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(cols, fg_color=CARD, corner_radius=14, border_width=1, border_color=BORDER)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        ctk.CTkLabel(left, text="✎ 原始提示", font=_font(16, "bold"), text_color=TITLE).pack(
            anchor="w", padx=16, pady=(14, 8))
        self.orig_box = ctk.CTkTextbox(left, font=_font(13), fg_color=BG,
                                       border_color=BORDER, border_width=1, corner_radius=4)
        self.orig_box.pack(fill="both", expand=True, padx=16, pady=(0, 16))

        right = ctk.CTkFrame(cols, fg_color=CARD, corner_radius=14, border_width=1, border_color=BORDER)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        ctk.CTkLabel(right, text="✦ 增强结果", font=_font(16, "bold"), text_color=ACCENT).pack(
            anchor="w", padx=16, pady=(14, 8))
        self.result_box = ctk.CTkTextbox(right, font=_font(13), fg_color=BG,
                                         border_color=BORDER, border_width=1, corner_radius=4)
        self.result_box.pack(fill="both", expand=True, padx=16, pady=(0, 16))

    def _enhance(self):
        text = self.orig_box.get("0.0", "end").strip()
        if not text:
            self._set_status("请输入要增强的提示词")
            return
        self.last_original = text
        self.enhance_btn.configure(state="disabled", text="增强中…")
        self._set_status("正在调用 %s 增强…" % self._current_cfg(persist=False).get("model"))
        threading.Thread(target=self._enhance_worker, args=(text,), daemon=True).start()

    def _enhance_worker(self, text):
        t0 = time.time()
        cfg = self._current_cfg(persist=False)
        err = ""
        result = None
        try:
            result = engine.enhance(cfg, text)
        except engine.ConfigError as e:
            err = str(e)
        except engine.EmptyResponseError as e:
            err = str(e)
        except engine.EnhanceError as e:
            err = str(e)
        dur = int((time.time() - t0) * 1000)
        self.root.after(0, self._on_enhance_done, text, result, dur, err)

    def _on_enhance_done(self, text, result, dur, err):
        self.enhance_btn.configure(state="normal", text="✦ 增强提示词")
        cfg = self._current_cfg(persist=False)
        if not result:
            db.append_record(
                created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                provider=self.provider_var.get(), model=cfg.get("model", ""),
                base_url=cfg.get("base_url", ""), input_text=text, output_text="",
                status="failed", error_msg=err[:500], duration_ms=dur, source="single",
                style=cfg.get("style", "balanced"), enh_mode=cfg.get("mode", "user"))
            self._set_status("增强失败: " + err)
            self._refresh_history()
            return
        output = result["output"]
        quality = "low_quality" if engine.is_low_quality(text, output) else ""
        length_note = ""
        if len(output) > templates.MAX_OUTPUT_CHARS:
            length_note = "（注意：结果 %d 字，超过 %d 字建议精简）" % (
                len(output), templates.MAX_OUTPUT_CHARS)
        self.result_box.delete("0.0", "end")
        self.result_box.insert("0.0", output)
        db.append_record(
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            provider=self.provider_var.get(), model=cfg.get("model", ""),
            base_url=cfg.get("base_url", ""), input_text=text, output_text=output,
            status="success", duration_ms=dur, source="single",
            prompt_tokens=result.get("prompt_tokens"),
            completion_tokens=result.get("completion_tokens"),
            request_id=result.get("request_id"), quality=quality,
            style=cfg.get("style", "balanced"), enh_mode=cfg.get("mode", "user"))
        self._refresh_history()
        qnote = "（质量偏低：疑似复述原文）" if quality else ""
        self._set_status("增强完成 ✓  已记入历史档案 %s%s" % (qnote, length_note))

    def _copy_result(self):
        txt = self.result_box.get("0.0", "end").strip()
        if txt:
            self.root.clipboard_clear()
            self.root.clipboard_append(txt)
            self._set_status("已复制增强结果到剪贴板")
        else:
            self._set_status("结果为空，无可复制内容")

    def _restore_input(self):
        """将输入框还原为最近一次增强所用的原始提示词。"""
        if self.last_original:
            self.orig_box.delete("0.0", "end")
            self.orig_box.insert("0.0", self.last_original)
            self._set_status("已还原为最近一次增强的原始提示词")
        else:
            self._set_status("没有可还原的原始内容")

    def _clear_single(self):
        self.orig_box.delete("0.0", "end")
        self.result_box.delete("0.0", "end")
        self.last_original = ""
        self._set_status("已清空输入框（不影响历史档案）")

    # ---------------- 批量增强页 ----------------
    def _build_batch_page(self, p):
        ctk.CTkLabel(p, text="☰ 批量输入（每行一条提示词，空行自动跳过）", font=_font(16, "bold"),
                     text_color=TITLE, anchor="w").pack(anchor="w", pady=(0, 8))
        self.batch_in = ctk.CTkTextbox(p, height=150, font=_font(13), fg_color=CARD,
                                       border_color=BORDER, border_width=1, corner_radius=4)
        self.batch_in.pack(fill="x", pady=(0, 12))

        bar = ctk.CTkFrame(p, fg_color="transparent")
        bar.pack(fill="x", pady=(0, 12))
        pill(bar, "✦ 批量增强", self._batch_enhance, True, 150).pack(side="left", padx=(0, 10))
        pill(bar, "⤓ 导出 CSV", self._export_csv, False, 120).pack(side="left", padx=(0, 8))
        pill(bar, "⌫ 清空输入", self._clear_batch, False, 110).pack(side="left")

        ctk.CTkLabel(p, text="⤓ 批量结果（每条：原始 → 增强）", font=_font(16, "bold"),
                     text_color=TITLE, anchor="w").pack(anchor="w", pady=(0, 8))
        self.batch_out = ctk.CTkTextbox(p, font=_font(13), fg_color=CARD, border_color=BORDER,
                                        border_width=1, corner_radius=4)
        self.batch_out.pack(fill="both", expand=True)

    def _clear_batch(self):
        self.batch_in.delete("0.0", "end")
        self.batch_out.delete("0.0", "end")
        self._set_status("已清空批量输入框（不影响历史档案）")

    def _batch_enhance(self):
        lines = [l.strip() for l in self.batch_in.get("0.0", "end").splitlines() if l.strip()]
        if not lines:
            self._set_status("批量框为空，请每行输入一条提示词")
            return
        cfg = self._current_cfg(persist=False)
        self.batch_out.delete("0.0", "end")
        self._set_status("批量增强中… 共 %d 条" % len(lines))
        threading.Thread(target=self._batch_worker, args=(cfg, lines, self.provider_var.get()),
                         daemon=True).start()

    def _batch_worker(self, cfg, lines, provider):
        ok, fail = 0, 0
        out_parts = []
        for i, line in enumerate(lines, 1):
            t0 = time.time()
            result = None
            err = ""
            try:
                result = engine.enhance(cfg, line)
            except engine.ConfigError as e:
                err = str(e)
            except engine.EmptyResponseError as e:
                err = str(e)
            except engine.EnhanceError as e:
                err = str(e)
            dur = int((time.time() - t0) * 1000)
            if result:
                output = result["output"]
                quality = "low_quality" if engine.is_low_quality(line, output) else ""
                db.append_record(
                    created_at=time.strftime("%Y-%m-%d %H:%M:%S"), provider=provider,
                    model=cfg.get("model", ""), base_url=cfg.get("base_url", ""),
                    input_text=line, output_text=output, status="success", duration_ms=dur,
                    source="batch", prompt_tokens=result.get("prompt_tokens"),
                    completion_tokens=result.get("completion_tokens"),
                    request_id=result.get("request_id"), quality=quality,
                    style=cfg.get("style", "balanced"), enh_mode=cfg.get("mode", "user"))
                out_parts.append("【%d】原始：%s\n增强：%s\n" % (i, line, output))
                ok += 1
            else:
                db.append_record(
                    created_at=time.strftime("%Y-%m-%d %H:%M:%S"), provider=provider,
                    model=cfg.get("model", ""), base_url=cfg.get("base_url", ""),
                    input_text=line, output_text="", status="failed", error_msg=err[:500],
                    duration_ms=dur, source="batch", style=cfg.get("style", "balanced"),
                    enh_mode=cfg.get("mode", "user"))
                out_parts.append("【%d】原始：%s\n失败：%s\n" % (i, line, err))
                fail += 1
            self.root.after(0, self._batch_progress, i, len(lines), ok, fail)
        self.root.after(0, self._batch_done, "\n".join(out_parts), ok, fail)

    def _batch_progress(self, cur, total, ok, fail):
        self._set_status("批量增强中… %d/%d  成功 %d 失败 %d" % (cur, total, ok, fail))

    def _batch_done(self, text, ok, fail):
        self.batch_out.delete("0.0", "end")
        self.batch_out.insert("0.0", text)
        self._refresh_history()
        self._set_status("批量完成 ✓  成功 %d 失败 %d" % (ok, fail))

    def _export_csv(self):
        text = self.batch_out.get("0.0", "end").strip()
        if not text:
            self._set_status("没有可导出的批量结果")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")], title="导出批量增强结果")
        if not path:
            return
        rows = []
        for b in text.split("【"):
            b = b.strip()
            if not b:
                continue
            orig, enh = "", ""
            for ln in b.splitlines():
                if ln.startswith("原始："):
                    orig = ln[len("原始："):]
                elif ln.startswith("增强："):
                    enh = ln[len("增强："):]
                elif ln.startswith("失败："):
                    enh = "失败：" + ln[len("失败："):]
            rows.append((orig, enh))
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["原始提示词", "增强提示词"])
                w.writerows(rows)
            self._set_status("已导出 CSV: " + path)
        except Exception as e:
            self._set_status("导出失败: " + str(e))

    # ---------------- 历史档案页 ----------------
    def _build_history_page(self, p):
        bar = ctk.CTkFrame(p, fg_color="transparent")
        bar.pack(fill="x", pady=(0, 12))
        pill(bar, "⟳ 刷新", self._refresh_history, False, 110).pack(side="left", padx=(0, 8))
        pill(bar, "⤓ 导出全部", self._export_all_history, False, 120).pack(side="left", padx=(0, 8))
        pill(bar, "⌫ 清空档案", self._clear_history, False, 120).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(bar, text="（清空前先备份数据库，再清业务数据）", font=_font(11),
                     text_color=MUTED).pack(side="left", padx=(4, 0))

        self.stats_frame = ctk.CTkFrame(p, fg_color=SOFT_TEAL, corner_radius=14,
                                       border_width=1, border_color="#E3D9B8")
        self.stats_frame.pack(fill="x", pady=(0, 12), padx=2)
        self.stats_label = ctk.CTkLabel(self.stats_frame, text="", anchor="w",
                                        font=_font(12), text_color=PRIMARY)
        self.stats_label.pack(fill="x", padx=16, pady=10)

        self.history_frame = ctk.CTkScrollableFrame(
            p, fg_color=CARD, border_color=BORDER, border_width=1, corner_radius=14,
            label_text="  增强历史（最新在前）", label_font=_font(13, "bold"),
            label_text_color=TITLE)
        self.history_frame.pack(fill="both", expand=True)

    # ---------------- 指令构造页（嵌入 ForgePanel）----------------
    def _build_forge_page(self, p):
        panel = ForgePanel(p, app=self)
        panel.pack(fill="both", expand=True)

    def _refresh_history(self):
        if not hasattr(self, "history_frame"):
            return
        try:
            s = db.get_stats()
            rate = (s["success"] / s["total"] * 100) if s["total"] else 0
            self.stats_label.configure(text=(
                "📊 总计 %d 条 · 今日 %d 条 · 成功率 %.1f%% · 平均耗时 %dms · "
                "累计 token %d（输入 %d / 输出 %d）· 低质量 %d 条"
                % (s["total"], s["today"], rate, s["avg_duration_ms"],
                   s["total_prompt_tokens"] + s["total_completion_tokens"],
                   s["total_prompt_tokens"], s["total_completion_tokens"], s["low_quality"])))
        except Exception:
            pass
        for w in self.history_frame.winfo_children():
            w.destroy()
        data = db.load_records(DISPLAY_LIMIT)
        if not data:
            ctk.CTkLabel(self.history_frame, text="（暂无历史记录，去单条或批量页试一次吧）",
                         font=_font(13), text_color=MUTED).pack(padx=10, pady=16)
            return
        for item in data:
            is_ok = item.get("status", "success") == "success"
            quality = item.get("quality", "")
            row = ctk.CTkFrame(self.history_frame, fg_color=BG, corner_radius=4,
                               border_width=1, border_color=BORDER)
            row.pack(fill="x", padx=8, pady=5)
            preview = (item.get("input", "") or "")[:40].replace("\n", " ")
            mark = "✓" if is_ok else "✗"
            qmark = " ⚠低质量" if quality == "low_quality" else ""
            lbl = ctk.CTkLabel(
                row,
                text="%s %s   ·   %s   ·   %s%s\n%s" % (
                    mark, item.get("time", ""), item.get("provider", ""),
                    item.get("style", "") or "—", qmark, preview),
                anchor="w", justify="left", font=_font(12), text_color=TEXT)
            lbl.pack(side="left", fill="x", expand=True, padx=12, pady=8)
            out_text = item.get("output", "") or ""
            if not out_text and not is_ok:
                out_text = "（该条增强失败，无结果）"
            pill(row, "⧉ 复制", lambda o=out_text: self._copy_text(o), False, 90).pack(
                side="right", padx=(0, 6), pady=6)
            pill(row, "⤓ 载入", lambda o=out_text: self._load_output(o), True, 80).pack(
                side="right", padx=(0, 10), pady=6)

    def _export_all_history(self):
        data = db.load_records()  # 不传 limit，导出全部历史
        if not data:
            self._set_status("暂无可导出的历史记录")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            title="导出全部增强历史")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["时间", "厂商", "模型", "风格", "状态", "输入字数",
                            "输出字数", "质量", "输入", "输出", "RequestID"])
                for it in data:
                    w.writerow([
                        it.get("time", ""), it.get("provider", ""),
                        it.get("model", ""), it.get("style", ""), it.get("status", ""),
                        len(it.get("input", "") or ""), len(it.get("output", "") or ""),
                        it.get("quality", ""), it.get("input", ""), it.get("output", ""),
                        it.get("request_id", ""),
                    ])
            self._set_status("已导出 %d 条历史到 %s" % (len(data), path))
        except Exception as e:
            self._set_status("导出失败: " + str(e))

    def _copy_text(self, txt):
        if txt:
            self.root.clipboard_clear()
            self.root.clipboard_append(txt)
            self._set_status("已复制历史结果")

    def _load_output(self, txt):
        self._show_page("single")
        self.result_box.delete("0.0", "end")
        self.result_box.insert("0.0", txt)
        self._set_status("已载入历史结果到「单条增强」结果框")

    def _clear_history(self):
        if not messagebox.askyesno(
                "确认清空历史档案",
                "将执行以下操作：\n"
                "1. 先按今日日期原子备份整个数据库（含配置与全部历史）；\n"
                "2. 再仅清空增强历史，连接配置不会被删除；\n"
                "3. 记录本次清空到审计日志。\n\n"
                "确定继续？"):
            return
        try:
            backup_path = db.backup_database()
        except db.BackupError as e:
            self._set_status("数据库备份失败，已取消清空：%s" % e)
            return
        removed = db.clear_records()
        db.log_clear(os.path.basename(backup_path), removed, "用户主动清空历史档案")
        self._refresh_history()
        self._set_status("已备份至 %s 并清空 %d 条历史（配置保留）" % (
            os.path.basename(backup_path), removed))

    # ---------------- 状态栏 ----------------
    def _set_status(self, msg):
        self.status_var.set(msg)

    def run(self):
        self.root.mainloop()


def _version():
    from .. import __version__
    return __version__
