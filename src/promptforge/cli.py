#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Project: 提示词工坊项目
# Copyright (C) 2026 智维ITMSKY
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
命令行模式（表现层 · 多场景适配）。

在不启动桌面的场景下使用，例如服务器批处理、CI、自动化流水线：
  python -m promptforge cli --text "写个登录页"
  python -m promptforge cli --file prompts.txt --out enhanced.csv
  python -m promptforge cli --file prompts.txt --provider DeepSeek --style 更技术

特性：
  - 复用与 GUI 完全相同的增强引擎与数据库连接，保证行为一致。
  - 支持单条文本、文件批处理（每行一条）、CSV 导出。
  - 连接配置来自本地数据库（与 GUI 共享）；可用环境变量 PROMPTFORGE_API_KEY
    覆盖密钥，避免明文落库风险。
"""

import os
import sys
import argparse
import csv

from . import __version__
from .core import engine, templates
from .data import database as db
from .config import load_providers
from .utils.logging_setup import ensure_utf8_console


def _build_cfg(args, saved):
    provider = args.provider or saved.get("provider", "DeepSeek")
    preset = load_providers().get(provider, {})
    env_key = os.environ.get("PROMPTFORGE_API_KEY")
    return {
        "provider": provider,
        "api_key": env_key or saved.get("api_key", ""),
        "base_url": args.base_url or preset.get("base_url") or saved.get("base_url", ""),
        "model": args.model or preset.get("model") or saved.get("model", ""),
        "temperature": args.temperature or saved.get("temperature", "0.7"),
        "max_tokens": args.max_tokens or saved.get("max_tokens", "1024"),
        "style": templates.STYLE_LABEL_TO_KEY.get(args.style, saved.get("style", "balanced")),
    }


def _run_one(cfg, text):
    try:
        r = engine.enhance(cfg, text)
        return True, r["output"], r.get("quality", ""), ""
    except engine.EnhanceError as e:
        return False, "", "", str(e)


def run(argv=None):
    ensure_utf8_console()
    parser = argparse.ArgumentParser(
        prog="promptforge", description="提示词工坊 命令行模式 v%s" % __version__)
    parser.add_argument("--text", help="单条待增强的提示词")
    parser.add_argument("--file", help="批量输入文件，每行一条提示词")
    parser.add_argument("--out", help="批量结果输出路径（CSV）")
    parser.add_argument("--provider", help="厂商名（覆盖已保存配置）")
    parser.add_argument("--base-url", dest="base_url", help="Base URL（覆盖已保存配置）")
    parser.add_argument("--model", help="模型名（覆盖已保存配置）")
    parser.add_argument("--style", default="均衡", choices=list(templates.STYLE_LABEL_TO_KEY.keys()),
                        help="增强风格")
    parser.add_argument("--temperature", help="温度（覆盖已保存配置）")
    parser.add_argument("--max-tokens", dest="max_tokens", help="最大长度（覆盖已保存配置）")
    args = parser.parse_args(argv)

    if not args.text and not args.file:
        parser.error("请至少提供 --text 或 --file 之一")

    db.init_db()
    saved = db.load_config()
    cfg = _build_cfg(args, saved)

    if args.text:
        ok, out, quality, err = _run_one(cfg, args.text)
        if ok:
            print(out)
            if quality == "low_quality":
                print("[warn] 低质量：疑似复述原文", file=sys.stderr)
        else:
            print("[error] %s" % err, file=sys.stderr)
            return 1
        return 0

    # 批量
    with open(args.file, "r", encoding="utf-8") as f:
        lines = [l.strip() for l in f.read().splitlines() if l.strip()]
    if not lines:
        print("[warn] 输入文件无有效内容", file=sys.stderr)
        return 0
    ok_total, fail_total = 0, 0
    rows = []
    for line in lines:
        ok, out, quality, err = _run_one(cfg, line)
        if ok:
            rows.append((line, out, quality))
            ok_total += 1
        else:
            rows.append((line, "失败：%s" % err, ""))
            fail_total += 1
        print("[%d/%d] %s" % (ok_total + fail_total, len(lines),
                              "OK" if ok else "FAIL"), file=sys.stderr)

    if args.out:
        with open(args.out, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["原始提示词", "增强提示词", "质量"])
            w.writerows(rows)
        print("[done] 成功 %d 失败 %d，已写出 %s" % (ok_total, fail_total, args.out))
    else:
        for orig, enh, _ in rows:
            print("原始：%s\n增强：%s\n" % (orig, enh))
    return 0


if __name__ == "__main__":
    sys.exit(run())
