#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Project: 提示词工坊项目
# Copyright (C) 2026 智维ITMSKY
# SPDX-License-Identifier: AGPL-3.0-or-later
"""装配引擎：把解析结果填入五槽骨架，并产出「歧义停工」清单与 token 对比报告。

原则：
  1. 每个槽位要么来自原文（带行号可追溯），要么来自用户显式输入；
  2. 两者都没有 → 标 ❓待填，并记入 unresolved（触发歧义停工），绝不猜测填充；
  3. 提示词正文只放行号锚点，不放章节原文 —— 这是省 token 的主手段。
"""

import re

from . import templates
from . import parser as _p


def approx_tokens(text):
    """粗略估算 token 数（中英混排按 ~2 字符/token），仅用于横向比较，不用于计费。"""
    return max(1, int(len(text) / 2))


def _anchor(doc, h):
    """生成带行号的章节锚点，如：§3.1 xxx（原文 L42-L58）"""
    start, end = _p.section_span(doc, h["line"])
    return "§%s %s（原文 L%d-L%d）" % (h.get("num", ""), h["title"], start, end)


def _later_phases(doc, period):
    r"""找出不属于本期的其他期次，用于生成反向边界。

    注意必须归一化空白：正则的第 1 分组会把前导空格一起捕获
    （\s* 在分组内），导致同一期次出现 "三期" 与 " 三期" 两条。
    """
    if not period:
        return []
    norm_period = re.sub(r"\s+", "", period)
    out, seen = [], set()
    for ph in doc["phases"]:
        m = _p.PHASE_RE.search(ph["text"])
        if not m:
            continue
        name = re.sub(r"\s+", "", m.group(1))
        if not name or name in norm_period or norm_period in name:
            continue                      # 本期自身不作为反向边界
        if name in seen:
            continue                      # 去重：同一期次可能在多处被提及
        seen.add(name)
        out.append(name)
    return out


def assemble(doc, picks=None, task=None, code_files=None, decisions=None,
             period=None, add_exit_gate=True):
    """装配五槽提示词。返回 {"prompt", "unresolved", "report"}"""
    picks = picks or []
    unresolved = []
    PH = templates.PLACEHOLDER

    doc_path = doc.get("path") or PH + "（未提供设计文档，请用 --file / --text）"

    # ---------------- 槽1【任务】----------------
    if task:
        task_txt = task
    elif period:
        task_txt = "按 %s 实现 %s" % (doc_path, period)
    else:
        task_txt = templates.build_missing_hint(
            "【任务】", "缺少动词开头的一句话任务，请用 --task 或 --period 指定")
        unresolved.append("【任务】未确定：未提供 --task / --period")

    # ---------------- 槽2【输入】----------------
    if picks:
        anchors = ", ".join(_anchor(doc, h) for h in picks)
        doc_line = "- 设计文档：%s（你只需读 %s 几节，其余不要读）" % (doc_path, anchors)
    else:
        doc_line = "- 设计文档：%s（%s：本期切片未选，请用 --pick 指定章节编号）" % (
            doc_path, templates.build_missing_hint("", "先跑 --outline 看目录"))
        unresolved.append("【输入】缺本期切片：未提供 --pick")

    if code_files:
        code_line = "- 代码：" + "、".join(code_files)
    else:
        code_line = "- 代码：%s（请用 --code 精确到文件/函数，否则模型会全仓库扫）" % PH
        unresolved.append("【输入】缺代码范围：未提供 --code")

    if decisions:
        decision_line = "- 我的决策：" + "；".join(decisions)
    else:
        if doc["ambiguities"]:
            decision_line = ("- 我的决策：%s（原文有 %d 处待确认/TODO 项未答复，"
                             "见下方歧义停工清单）" % (PH, len(doc["ambiguities"])))
            unresolved.append("【输入】有 %d 处文档待确认项未答复" % len(doc["ambiguities"]))
        else:
            decision_line = "- 我的决策：按文档推荐默认"

    # ---------------- 槽3【范围】----------------
    scope_lines = []
    for i, h in enumerate(picks, 1):
        scope_lines.append("%d. %s（对齐 %s）" % (i, h["title"], _anchor(doc, h)))
        subs = _p.child_headings(doc, h["line"])
        if subs:
            # 选了父章节时，其直接子节往往就是本期的功能点，展开比留悬空标题有用
            for s in subs:
                scope_lines.append("   - %s（%s）" % (s["title"], _anchor(doc, s)))
        else:
            for b in _p.section_bullets(doc, h["line"]):
                scope_lines.append("   - %s" % b)

    # ---------------- 槽4【禁止】（反向边界）----------------
    forbidden = list(templates.DEFAULT_FORBIDDEN)
    for name in _later_phases(doc, period):
        forbidden.append("不做 %s（不属于本期，避免一次吃掉多期）" % name)
    if doc["deps"]:
        d = doc["deps"][0]
        forbidden.append("依赖范围以原文为准（L%d：%s），不引入清单外依赖" % (d["line"], d["text"]))

    # ---------------- 槽5【验收】----------------
    seen_cmd, accept_lines = set(), []
    for c in doc["commands"]:
        if c["cmd"] in seen_cmd:
            continue
        seen_cmd.add(c["cmd"])
        accept_lines.append("%d. `%s`  → 原文 L%d" % (len(accept_lines) + 1, c["cmd"], c["line"]))
    if not accept_lines:
        accept_lines.append(templates.build_missing_hint(
            "【验收】", "文档未检出可执行命令，请用命令显式补充，不要写「看看效果」"))
        unresolved.append("【验收】无可执行命令：文档中未检出 go/test/curl 等命令")
    accept_lines.append("%d. 超范围的东西一件都没有" % (len(accept_lines) + 1))

    # ---------------- 拼装 ----------------
    parts = []
    parts.append("【任务】" + task_txt)
    parts.append("")
    parts.append("【输入】")
    parts.append(doc_line)
    parts.append(code_line)
    parts.append(decision_line)
    parts.append("")
    parts.append("【范围】（只做这些，做完了就停）")
    parts.extend(scope_lines if scope_lines else ["   " + PH])
    parts.append("")
    parts.append("【禁止】")
    for f in forbidden:
        parts.append("- " + f)
    parts.append("")
    parts.append("【验收】")
    parts.extend(accept_lines)
    if add_exit_gate:
        parts.append("")
        parts.append(templates.EXIT_GATE)

    # ---------------- 自检清单（恒定附随）----------------
    parts.append("")
    parts.append("【发送前自检】")
    for name, bad in templates.SELF_CHECK:
        parts.append("- [ ] %s（不过关的信号：%s）" % (name, bad))

    # ---------------- 歧义停工清单 ----------------
    if unresolved:
        parts.append("")
        parts.append("【⚠️ 歧义停工】以下槽位未确认，先答复再执行，不要带病开工：")
        for i, u in enumerate(unresolved, 1):
            parts.append("%d. %s" % (i, u))
        for i, amb in enumerate(doc["ambiguities"][:10], 1):
            parts.append("   原文待确认 #%d（L%d）：%s" % (i, amb["line"], amb["text"]))

    prompt = "\n".join(parts)

    # ---------------- token 对比报告 ----------------
    full_text = "\n".join(doc["_lines"])
    slice_text = "\n".join(_p.section_text(doc, h["line"]) for h in picks) if picks else ""
    report = {
        "doc_lines": doc["total_lines"],
        "full_tokens": approx_tokens(full_text),
        "slice_tokens": approx_tokens(slice_text),
        "prompt_tokens": approx_tokens(prompt),
    }
    if report["full_tokens"]:
        report["saved_pct"] = round(
            (1 - report["prompt_tokens"] / report["full_tokens"]) * 100, 1)

    return {"prompt": prompt, "unresolved": unresolved, "report": report}


# =====================================================================
# 模板变量（v3.3, F5）：槽位/文档内容中的 {{变量名}} 简单替换
# =====================================================================
# 安全边界：只开放"简单变量替换"这一种模板能力。变量名必须整体匹配
# [A-Za-z0-9_]+；渲染前拒绝 {% ... %} 语句与 {# ... #} 注释语法，防止
# 文档内容把 if/for 等模板表达式注入 jinja2。检测到任何形如 {{...}} 但
# 不是纯标识符的片段一律拒绝（挡掉属性访问/表达式注入）。
# jinja2 延迟导入：forge 被桌面版按纯标准库复用，只有 Web 端真正渲染时
# 才需要它（Flask 自带，无需额外安装）。

_NAME_OK_RE = re.compile(r"^[A-Za-z0-9_]+$")
_VAR_LIKE_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def detect_variables(text):
    """扫描模板文本中的 {{变量名}}。返回 (合法变量名列表[按出现顺序去重], 非法名单)。"""
    names, bad = [], []
    for m in _VAR_LIKE_RE.finditer(text or ""):
        name = m.group(1)
        if _NAME_OK_RE.match(name):
            if name not in names:
                names.append(name)
        elif name not in bad:
            bad.append(name)
    return names, bad


def render_variables(text, variables):
    """用 jinja2 把 {{变量名}} 替换为给定值（严格模式：缺变量直接报错）。

    约束（防模板注入，渲染前校验）：
      - 模板含 {% / {# 语法 → 拒绝（禁止 if/for/注释等非替换语法）；
      - 检测到的变量名必须整体匹配 [A-Za-z0-9_]+，否则拒绝；
      - 存在未提供值的变量 → ValueError（友好提示由上层转述）。
    不含变量的文本原样返回，行为与旧版完全一致。"""
    text = text or ""
    if "{%" in text or "{#" in text:
        raise ValueError("模板包含 {% ... %} / {# ... #} 语法，仅支持 {{变量名}} 简单替换")
    names, bad = detect_variables(text)
    if bad:
        raise ValueError("存在非法模板变量名（仅允许字母/数字/下划线）：%s" % "、".join(bad))
    clean = {}
    for k, v in (variables or {}).items():
        if not _NAME_OK_RE.match(str(k)):
            raise ValueError("变量名仅允许字母、数字、下划线：%s" % k)
        clean[k] = "" if v is None else str(v)
    if not names:
        return text                      # 无变量：不做 jinja2 渲染，原样返回
    missing = [n for n in names if n not in clean]
    if missing:
        raise ValueError("存在未填变量：%s" % "、".join(missing))
    try:
        import jinja2
    except ImportError:                  # 桌面版无 Flask/jinja2 时不该走到这里
        raise ValueError("当前环境缺少 jinja2，无法渲染模板变量")
    try:
        return jinja2.Template(
            text, undefined=jinja2.StrictUndefined, keep_trailing_newline=True
        ).render(**clean)
    except jinja2.UndefinedError as e:
        raise ValueError("存在未填变量：%s" % e)
    except jinja2.TemplateError as e:
        raise ValueError("模板渲染失败：%s" % e)
