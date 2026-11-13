#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Project: 提示词工坊项目
# Copyright (c) 2026 智维工作室
# SPDX-License-Identifier: AGPL-3.0-or-later
"""命令行入口：文档 / 粘贴文本 → 五槽提示词。

典型用法（两步走，最省 token）：
  1. 先看目录锚点：  python -m forge --file <设计文档.md> --outline
  2. 再出提示词：    python -m forge --file <设计文档.md> --pick 3,6.6 --period 一期 \
                        --code internal/license/verify.go --task "实现 verify 签名防伪"
"""

import argparse
import sys

from . import __version__
from . import parser as _p
from . import assembler as _a


def _read_source(args):
    """返回 (文本, 来源名)；支持文件路径 / stdin / 直接粘贴三种入口。"""
    if args.file == "-":
        return sys.stdin.read(), "<stdin>"
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            return f.read(), args.file
    if args.text:
        return args.text, "<粘贴文本>"
    return None, None


def _print_outline(doc):
    """打印「文档定位」结果：目录 + 命令 + 待确认 + 分期 + 依赖。"""
    print("文档：%s（%d 行）" % (doc.get("path"), doc["total_lines"]))
    if doc["title"]:
        print("标题：%s" % doc["title"])

    print("\n[目录]")
    for h in doc["headings"]:
        indent = "  " * (h["level"] - 1)
        print("%s§%-7s %s  (L%d)" % (indent, h.get("num", ""), h["title"], h["line"]))

    print("\n[检出命令] %d 条" % len(doc["commands"]))
    for c in doc["commands"]:
        print("   L%-4d %s" % (c["line"], c["cmd"]))

    print("\n[待确认 / TODO] %d 处" % len(doc["ambiguities"]))
    for amb in doc["ambiguities"]:
        print("   L%-4d %s" % (amb["line"], amb["text"]))

    print("\n[分期标记] %d 条" % len(doc["phases"]))
    for ph in doc["phases"]:
        print("   L%-4d %s" % (ph["line"], ph["text"]))

    print("\n[依赖线索] %d 条" % len(doc["deps"]))
    for d in doc["deps"]:
        print("   L%-4d %s" % (d["line"], d["text"]))

    print("\n下一步：python -m forge --file %s --pick <章节号> --period <本期> "
          "--code <文件> --task <一句话>" % (doc.get("path") or "docs/design.md"))


def _print_report(report, unresolved, stream=sys.stderr):
    """把 token 对比与歧义清单打到 stderr，保证 stdout 的提示词可纯净重定向。"""
    print("", file=stream)
    print("---- 装配报告 ----", file=stream)
    print("文档总行数：%d  |  全文≈%d tokens" % (report["doc_lines"], report["full_tokens"]),
          file=stream)
    print("选中切片≈%d tokens  |  本提示词≈%d tokens" % (
        report["slice_tokens"], report["prompt_tokens"]), file=stream)
    if report["full_tokens"]:
        if report["saved_pct"] > 0:
            print("相对「全文塞上下文」节省：约 %s%%" % report["saved_pct"], file=stream)
        else:
            # 小文档 honest 反馈：模板是固定开销，正文太短时会倒挂，如实说明而非粉饰
            print("本文档仅 %d 行，模板开销高于正文（收益随文档增大而显现，"
                  "建议在较大设计文档上使用）" % report["doc_lines"], file=stream)
    if unresolved:
        print("⚠️ 存在 %d 处未确认槽位，已进【歧义停工】清单，先答复再开工："
              % len(unresolved), file=stream)
        for u in unresolved:
            print("   - %s" % u, file=stream)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="forge", description="指令构造器 InstructionForge v%s —— 文档/粘贴 → 五槽提示词" % __version__)
    ap.add_argument("--file", help="设计文档路径；传 - 表示从 stdin 读")
    ap.add_argument("--text", help="直接粘贴文档内容（与 --file 二选一）")
    ap.add_argument("--outline", action="store_true",
                    help="只打印目录/命令/待确认项届时并退出（定位用，最省 token）")
    ap.add_argument("--pick", help="本期切片章节编号，逗号分隔，如 3,4,6.6")
    ap.add_argument("--task", help="一句话任务，动词开头")
    ap.add_argument("--code", help="涉及的代码文件，逗号分隔（精确到文件/函数）")
    ap.add_argument("--decision", action="append", default=[],
                    help="对文档待确认项的答复，可重复传入多条")
    ap.add_argument("--period", help="本期名称，如 一期 / Phase 1")
    ap.add_argument("--out", help="把提示词写入指定文件；缺省打印到 stdout")
    ap.add_argument("--no-gate", action="store_true", help="不附加【本期出口】门禁")
    args = ap.parse_args(argv)

    text, path = _read_source(args)
    if not text:
        ap.error("请用 --file <路径> 或 --text <内容> 提供设计文档")

    doc = _p.parse(text, path)

    if args.outline:
        _print_outline(doc)
        return 0

    tokens = [t.strip() for t in args.pick.split(",") if t.strip()] if args.pick else []
    picks = _p.resolve_picks(doc, tokens) if tokens else []
    if tokens and not picks:
        print("[warn] --pick %s 未命中任何章节，先跑 --outline 核对编号" % args.pick,
              file=sys.stderr)

    code_files = [c.strip() for c in args.code.split(",") if c.strip()] if args.code else []

    res = _a.assemble(doc,
                      picks=picks,
                      task=args.task,
                      code_files=code_files,
                      decisions=args.decision,
                      period=args.period,
                      add_exit_gate=not args.no_gate)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(res["prompt"] + "\n")
        print("[done] 提示词已写出：%s" % args.out, file=sys.stderr)
    else:
        print(res["prompt"])

    _print_report(res["report"], res["unresolved"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
