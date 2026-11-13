#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Project: 提示词工坊项目
# Copyright (C) 2026 智维ITMSKY
# SPDX-License-Identifier: AGPL-3.0-or-later
"""文档结构解析器（纯标准库，零远程接口调用）。

职责：把技术设计文档解析成「可定位、可切片、可验收」的结构化事实。

红线：本模块**只做抽取与归类**，不生成任何文档中不存在的内容。
      所有输出均带原文行号，便于人工回溯核对。不看 -=数组=-，也不臆造命令。
"""

import re

# ---- 各类识别正则（中文为主，兼容常见英文写法）--------------------------------
ATX_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")          # Markdown 标题
FENCE_RE = re.compile(r"^\s*(```+|~~~+)")                # 代码围栏起止

# 歧义/待确认：命中即列入「歧义停工」清单（prompt 不得替用户回答这些内容）
AMBIGUITY_RE = re.compile(
    r"(待确认|待定|待决策|需明确|需确认|暂定|待补充|不确定|可能有变"
    r"|TBD|TODO|FIXME|❓|⁉️|\?\?|？？)")

# 分期/里程碑：用于推导「本期该做哪一段、哪些属于后期」
# 注意：必须同时覆盖「第 3 期」与「一期/二期」两种写法——中文文档里后者更常见。
PHASE_RE = re.compile(
    r"(第?\s*[一二三四五六七八九十百\d]+\s*期"
    r"|Phase\s*[-\dIVX]+"
    r"|阶段\s*[-\d一二三四五六七八九十]+"
    r"|里程碑\s*[-\d一二三四五六七八九十]+"
    r"|Milestone\s*[-\d]+)")

# 依赖/第三方：用于生成反向边界（不得引入文档外依赖）
DEP_RE = re.compile(
    r"(第三方依赖|依赖库|新增依赖|新增库|外部依赖|requirements"
    r"|package\.json|go\.mod|pyproject|依赖环境)")

BULLET_RE = re.compile(r"^[-*+]\s+(.*\S)\s*$")           # 顶层无序列表项

# 标题自带编号（如 "## 3. 一期"、"### 6.6 服务端"）：优先用它做 --pick 的编号，
# 必须与人引用章节的习惯一致——文档写 §6.6，就只能 pick 6.6。
LEAD_NUM_RE = re.compile(r"^\s*(?:§\s*)?(\d+(?:\.\d+)*)\s*[.、．:：]?\s*(.*)$")

# 可执行命令起始词：用于自动填【验收】，避免人工抄错
COMMAND_START_RE = re.compile(
    r"^\s*(go\s+(build|test|run|vet|fmt)|pytest|tox"
    r"|python[0-9.]*\s+-m\s+pytest|python[0-9.]*\s+-m\s+unittest"
    r"|npm\s+(run\s+)?(test|build|lint)|pnpm\s+(run\s+)?(test|build)"
    r"|yarn\s+(test|build)|make\s+\S+|mvn\s+\w+|gradle\s+\w+"
    r"|cargo\s+(build|test)|dotnet\s+(build|test)"
    r"|docker\s+build|curl\s+-|git\s+diff)")

CODE_LANGS = ("bash", "sh", "shell", "zsh", "console", "powershell", "ps",
              "bat", "cmd", "go", "python", "py", "node", "make", "sql")


def _number_headings(headings):
    """计算章节编号，供 --pick 精确定位。

    优先级：标题自带编号 > 按层级推算的大纲号。
    理由：设计文档里通常已经写死了 `## 3. 一期`、`### 6.6 服务端`，人引用时说的
    是 §3 / §6.6 —— 编号必须与此一致，否则 --pick 形同虚设。
    文档标题（顶层且无自带编号）标记为不可选，避免与真实章节号撞车。
    """
    counters = [0] * 7
    for h in headings:
        lv = h["level"]
        counters[lv - 1] += 1
        for deeper in range(lv, 7):          # 更深层级计数清零
            counters[deeper] = 0
        auto = ".".join(str(counters[i]) for i in range(lv))
        h["_auto"] = auto
        h["raw"] = h["title"]

        m = LEAD_NUM_RE.match(h["title"])
        if m:
            h["num"] = m.group(1)
            rest = m.group(2).strip()
            if rest:                         # 剥掉前缀编号，避免展示时重复
                h["title"] = rest
        elif lv == 1:
            h["num"] = ""                    # 视为文档标题，不可选
        else:
            h["num"] = auto
    return headings


def parse(text, doc_path=None):
    """解析整份文档，返回结构化字典（含 _lines 便于后续切片定位）。"""
    lines = text.splitlines()
    doc = {
        "path": doc_path,
        "title": None,
        "total_lines": len(lines),
        "headings": [],
        "code_blocks": [],
        "commands": [],
        "ambiguities": [],
        "phases": [],
        "deps": [],
        "_lines": lines,
    }

    in_fence = False
    fence_tok = None
    block = None

    for idx, raw in enumerate(lines, start=1):
        fence = FENCE_RE.match(raw)
        if fence:
            tok = fence.group(1)[:3]
            if not in_fence:
                in_fence, fence_tok = True, tok
                lang = raw.strip()[len(fence.group(1)):].strip().lower()
                block = {"lang": lang, "start": idx, "lines": []}
                continue
            if tok == fence_tok:
                in_fence = False
                if block:
                    block["end"] = idx
                    block["content"] = "\n".join(block["lines"]).strip()
                    if block["content"]:
                        doc["code_blocks"].append(block)
                    block = None
                continue

        if in_fence:                      # 围栏内的 # / TODO 不当正文处理
            if block is not None:
                block["lines"].append(raw)
            continue

        stripped = raw.rstrip()
        m = ATX_RE.match(raw)
        if m:
            level, title = len(m.group(1)), m.group(2)
            doc["headings"].append({"level": level, "title": title, "line": idx, "num": ""})
            if doc["title"] is None and level == 1:
                doc["title"] = title
            # 分期 / 待确认常写在标题里（如 "## 3. 一期：xxx"），标题行同样要扫，
            # 否则这类最关键的元信息会被整条漏掉。
            if PHASE_RE.search(title):
                doc["phases"].append({"line": idx, "text": title})
            if AMBIGUITY_RE.search(title):
                doc["ambiguities"].append({"line": idx, "text": title})
            continue

        if AMBIGUITY_RE.search(stripped):
            doc["ambiguities"].append({"line": idx, "text": stripped.strip()})
        if PHASE_RE.search(stripped):
            doc["phases"].append({"line": idx, "text": stripped.strip()})
        if DEP_RE.search(stripped):
            doc["deps"].append({"line": idx, "text": stripped.strip()})

    if block and block.get("lines"):      # 未闭合围栏兜底
        block["end"] = len(lines)
        block["content"] = "\n".join(block["lines"]).strip()
        doc["code_blocks"].append(block)

    _number_headings(doc["headings"])

    for b in doc["code_blocks"]:
        if b["lang"] and b["lang"] not in CODE_LANGS:
            continue                      # 非脚本语言代码块不抽命令
        for offset, cl in enumerate(b["content"].splitlines()):
            cmd = cl.strip()
            if COMMAND_START_RE.match(cmd):
                doc["commands"].append({
                    "line": b["start"] + 1 + offset,
                    "cmd": cmd,
                    "lang": b["lang"] or "",
                })
    return doc


def section_span(doc, heading_line):
    """返回某标题的完整跨度 (起始行, 结束行)：到下一个同级或更高级标题前为止。"""
    lines = doc["_lines"]
    level = next((h["level"] for h in doc["headings"] if h["line"] == heading_line), None)
    if level is None:
        return (heading_line, heading_line)
    end = len(lines)
    for h in doc["headings"]:
        if h["line"] > heading_line and h["level"] <= level:
            end = h["line"] - 1
            break
    return (heading_line, end)


def section_text(doc, heading_line):
    """取某标题下的正文切片（不含标题自身的 # 行）。"""
    start, end = section_span(doc, heading_line)
    return "\n".join(doc["_lines"][start:end])


def section_bullets(doc, heading_line, limit=6):
    """取某标题下顶层的无序列表项，作为【范围】的候选条目（最多 limit 条）。"""
    out = []
    for raw in section_text(doc, heading_line).splitlines():
        m = BULLET_RE.match(raw)
        if m:
            out.append(m.group(1).strip())
            if len(out) >= limit:
                break
    return out


def child_headings(doc, heading_line):
    """取某标题的直接子标题（层级 +1），用于把父章节展开成本期功能点清单。"""
    parent = next((h for h in doc["headings"] if h["line"] == heading_line), None)
    if parent is None:
        return []
    start, end = section_span(doc, heading_line)
    return [h for h in doc["headings"]
            if start < h["line"] <= end and h["level"] == parent["level"] + 1]


def resolve_picks(doc, tokens):
    """把 --pick 的编号（如 3 / 3.1 / 6.6）解析为具体标题列表。

    若命中了父章节，自动剔除其子孙条目，保持【范围】精简（只给大而准的锚点）。
    """
    matched = []
    for t in tokens:
        t = t.strip().lstrip("§")
        if not t:
            continue
        for h in doc["headings"]:
            n = h.get("num", "")
            if n == t or n.startswith(t + "."):
                matched.append(h)
    nums = {h["num"] for h in matched}

    def has_ancestor(h):
        return any(h["num"].startswith(a + ".") for a in nums if a != h["num"])

    seen, out = set(), []
    for h in sorted([x for x in matched if not has_ancestor(x)], key=lambda x: x["line"]):
        if h["line"] in seen:
            continue
        seen.add(h["line"])
        out.append(h)
    return out
