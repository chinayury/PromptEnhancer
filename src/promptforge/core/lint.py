#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Project: 提示词工坊项目
# Copyright (C) 2026 智维ITMSKY
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
提示词静态体检（Prompt Lint）。

借鉴 LangGPT 结构化框架中的可静态验证特征（Role / Goal / Skills / Rules /
Workflow / OutputFormat），把「提示词质量差」的常见形态规则化，零 LLM 成本、
毫秒级返回、纯本地执行。不阻断增强流程——只做提示，决定权在用户。

规则按六类组织：
  1. 长度异常（过短 / 过长 / 超上限）
  2. 安全信号（提示词注入 / 明文密钥 / PII，借鉴 promptfoo 的断言分层）
  3. 模糊措辞
  4. 缺角色 / 缺输出格式 / 未填模板变量
  5. LangGPT 六段结构槽位缺项（info 级，最多 3 条，避免刷屏）
  6. 示例缺失
任何误报都不致命（用户可选择忽略）。

每条 issue 额外携带 `dimension` 字段（八维评分维度之一，思路借鉴国产
prompt-optimizer 的多维评分），返回值同时给出 `dimensions` 汇总，便于前端
按维度展示；顶层 `score` 仍按既有全局扣分逻辑计算，行为不变。
"""

import re

# 模糊措辞 → 建议（ LangGPT Rules 维度的核心痛点）
_VAGUE_WORDS = [
    ("尽量", "尽量具体：用数字或明确标准替代「尽量」"),
    ("尽可能", "改为可验证的约束（如「不超过 500 字」）"),
    ("适当", "说明尺度/范围，避免「适当」这种不可验收的表述"),
    ("一些", "列出明确条目或数量"),
    ("若干", "列出明确条目或数量"),
    ("相关", "点明具体相关方/范围"),
    ("优化一下", "说明优化目标（性能/可读性/体积/……）"),
    ("改进一下", "说明改进方向与验收标准"),
    ("好一点", "好在哪里？给出评判标准"),
    ("差不多", "给出精确的边界条件"),
    ("等等", "避免列举省略，改为明确枚举或「包括但不限于」+边界说明"),
    ("之类", "避免列举省略，改为明确枚举"),
    ("大概", "说明精确口径（数量/时间/范围）"),
    ("左右", "给出上下限"),
]

# 缺少角色设定的常见信号：以「帮我/请/写一个/做一个」等裸请求开头
_ROLE_HINT_RE = re.compile(r"^(请|帮我|麻烦|你好|hey|hello|please\b)", re.I)

# 缺少输出格式约束的检测：全文未出现任何格式指示词
_FORMAT_HINTS = ("格式", "字数", "条", "表", "json", "markdown", "结构", "清单",
                 "列表", "分段", "输出", "要求", "示例", "format", "schema", "bullet")

# 未替换模板变量（{{name}}）
_VAR_RE = re.compile(r"\{\{[^}]+\}\}")

# 把匹配片段压成单行用（避免 issue 消息里出现换行）
_WS_RE = re.compile(r"\s+")

MAX_INPUT_CHARS = 100_000
RECOMMENDED_MIN_CHARS = 20          # 过短提示：约 20 字以下几乎必然缺边界
RECOMMENDED_LENGTH_WARN = 2000      # 过长提示：提示可能想拆成多条
EXAMPLE_HINT_MIN_CHARS = 200        # 超过该长度仍无示例词时，建议补输入输出示例

# =====================================================================
# B-01 LangGPT 六段结构槽位（缺段仅 info 提示，不阻断增强流程）
# =====================================================================
# 每项四元组：(槽位 key, 中文段名, 命中正则, 一句话建议)。
#   · pattern 为 None 表示该段复用 _FORMAT_HINTS（output 段）；
#   · 正则大小写不敏感，既覆盖 `## Role` 这类 Markdown 标题，也覆盖行内/
#     行首的裸关键词（标题写法本身含关键词，故统一用 search 即可）；
#   · 定义顺序即上报优先级：role → goal → output → rules → workflow → skills。
_STRUCTURE_SLOTS = (
    ("role", "角色",
     re.compile(r"角色|你是|扮演|作为|Role|You\s+are|act\s+as", re.I),
     "用一句话定义「你是谁」（如「你是一名资深后端工程师」），角色越具体输出越聚焦"),
    ("goal", "目标/验收",
     re.compile(r"目标|验收|完成标准|Goal|Done\s+Criteria|Non-?Goals|不做|范围", re.I),
     "写清要达成什么、做到什么程度算完成，可用 Non-Goals 划清「不做的事」"),
    ("output", "输出格式",
     None,
     "约定输出结构（条数/字数/JSON/Markdown/表格/是否含代码块），结果不易跑偏"),
    ("rules", "规则/约束",
     re.compile(r"规则|约束|禁止|不得|Rules|Constraint", re.I),
     "列出必须遵守与严禁的事项，把隐性要求显性化为可检查的规则"),
    ("workflow", "流程/步骤",
     re.compile(r"流程|步骤|Workflow|Step", re.I),
     "给出执行顺序或步骤拆分，复杂任务更易稳定复现"),
    ("skills", "技能/能力",
     re.compile(r"技能|能力|Skill", re.I),
     "声明完成任务所需技能/工具与能力边界，避免模型越界作答"),
)
_STRUCTURE_MAX_MISSING = 3          # 缺段最多报 3 条，避免刷屏

# 槽位 key → 命中正则（output 段为 None，表示复用 _FORMAT_HINTS）
_STRUCTURE_SLOT_RES = {slot[0]: slot[2] for slot in _STRUCTURE_SLOTS}

# =====================================================================
# B-02 安全规则组（借鉴 promptfoo 断言分层：注入/密钥 error，PII warn）
# =====================================================================
_SECURITY_INJECTION_RE = re.compile(
    r"忽略以上|忽略之前"
    r"|ignore\s+(?:all\s+)?(?:previous|above|prior)\s+instructions"
    r"|disregard[^\n]{0,120}instructions"
    r"|系统提示|system\s+prompt"
    r"|越狱|jailbreak|DAN\s+mode",
    re.I,
)

# 疑似硬编码密钥形态：(说明, 正则)；一类密钥只报一次
_SECURITY_SECRET_PATTERNS = (
    ("OpenAI/Anthropic 风格 API Key", re.compile(r"sk-[A-Za-z0-9]{16,}")),
    ("AWS AccessKeyId", re.compile(r"AKIA[0-9A-Z]{12,}")),
    ("GitHub Personal Access Token", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("PEM 私钥块", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("JWT", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
)

# 明显 PII 赋值形态；限定同行内匹配，避免跨行误报
_SECURITY_PII_RE = re.compile(
    r"身份证号|手机号[ \t]*[:：]|银行卡号|(?:密码|password)[ \t]*[:：][ \t]*\S",
    re.I,
)

# =====================================================================
# B-03 八维评分（思路借鉴国产 prompt-optimizer 的多维评分）
# =====================================================================
# specificity（具体性）当前由 vague_word 等规则间接覆盖，此处保留维度位，
# 保证前端拿到的维度键集合稳定（雷达图不需要处理缺键）。
DIMENSIONS = ("clarity", "specificity", "context", "format",
              "constraint", "role", "example", "security", "length")

# issue code → 维度
_CODE_DIMENSION = {
    "empty": "clarity",             # 空输入：一切都不清晰
    "too_short": "length",
    "too_long": "length",
    "overflow": "length",
    "vague_word": "clarity",
    "no_role": "role",
    "no_format": "format",
    "unfilled_var": "context",
    "no_example": "example",
}

# structure_missing_<slot> → 维度
_STRUCTURE_DIMENSION = {
    "role": "role",
    "goal": "context",
    "output": "format",
    "rules": "constraint",
    "workflow": "constraint",
    "skills": "context",
}

_STRUCTURE_CODE_PREFIX = "structure_missing_"
_DEFAULT_DIMENSION = "clarity"      # 未知 code 的兜底维度

# 逐级扣分表：全局 score 与各维度分数共用同一张表
_LEVEL_PENALTY = {"error": 40, "warn": 15, "info": 5}

# 示例缺失判定：长度超限且全文没有任何示例指示词
_EXAMPLE_HINT_RE = re.compile(r"示例|例子|例如|example|e\.g\.", re.I)


def _dimension_of_code(code):
    """把一个 issue code 归到八维之一；未知 code 走兜底维度。"""
    code = code or ""
    if code.startswith(_STRUCTURE_CODE_PREFIX):
        return _STRUCTURE_DIMENSION.get(code[len(_STRUCTURE_CODE_PREFIX):], "context")
    if code.startswith("security_"):
        return "security"
    return _CODE_DIMENSION.get(code, _DEFAULT_DIMENSION)


def _issue(level, code, message, dimension=None):
    """构造一条 issue；dimension 缺省时按 code 自动归类到八维之一。"""
    return {
        "level": level,
        "code": code,
        "message": message,
        "dimension": dimension or _dimension_of_code(code),
    }


def _shorten(s, limit=40):
    """把匹配片段压成单行并截断，避免 issue 消息里出现换行/超长文本。"""
    s = _WS_RE.sub(" ", s or "").strip()
    return s if len(s) <= limit else s[:limit] + "…"


def _check_security(text, issues):
    """B-02：安全规则组，命中项直接追加到 issues。"""
    m = _SECURITY_INJECTION_RE.search(text)
    if m:
        issues.append(_issue(
            "error", "security_injection",
            "检测到疑似提示词注入信号「%s」：该表述可能诱导模型忽略既有指令或越权，"
            "建议删除或改写为正常的任务描述" % _shorten(m.group(0))))
    for label, pattern in _SECURITY_SECRET_PATTERNS:
        if pattern.search(text):
            issues.append(_issue(
                "error", "security_secret",
                "疑似明文密钥（%s）：请改用占位符（如 {{API_KEY}}）并由运行时注入，"
                "切勿提交进仓库或分享链接" % label))
            break                      # 一类密钥报一次即可，避免重复刷屏
    m = _SECURITY_PII_RE.search(text)
    if m:
        issues.append(_issue(
            "warn", "security_pii",
            "检测到疑似个人敏感信息「%s」：请替换为示例值或脱敏占位符，"
            "避免真实数据随提示词外泄" % _shorten(m.group(0))))


def _missing_structure_slots(text):
    """B-01：返回缺失的 LangGPT 结构槽位（按优先级，最多 3 个）。"""
    lowered = text.lower()
    missing = []
    for key, label, pattern, advice in _STRUCTURE_SLOTS:
        if pattern is None:           # output 段复用输出格式关键词表
            hit = any(h.lower() in lowered for h in _FORMAT_HINTS)
        else:
            hit = pattern.search(text) is not None
        if not hit:
            missing.append((key, label, advice))
    return missing[:_STRUCTURE_MAX_MISSING]


def _summarize_dimensions(issues):
    """B-03：按维度独立扣分并汇总，返回 {维度: {"score": 0-100, "issues": n}}。"""
    dims = {name: {"score": 100, "issues": 0} for name in DIMENSIONS}
    for it in issues:
        name = it.get("dimension") or _dimension_of_code(it.get("code"))
        slot = dims.setdefault(name, {"score": 100, "issues": 0})
        slot["issues"] += 1
        slot["score"] -= _LEVEL_PENALTY.get(it.get("level"), 0)
    for slot in dims.values():
        slot["score"] = max(0, min(100, slot["score"]))
    return dims


def _zero_dimensions(issues):
    """空输入专用：保留真实问题计数，但所有维度记 0 分（无内容可评估）。"""
    dims = _summarize_dimensions(issues)
    for slot in dims.values():
        slot["score"] = 0
    return dims


def lint(text):
    """对一段原始提示词做静态体检。

    返回 {"issues": [...], "score": int, "dimensions": {...}}：
      issues      —— 每条含 level / code / message / dimension；
      score       —— 100 为满分，按问题逐条扣分（不低于 0），仅供参考，不用于门禁；
      dimensions  —— {维度名: {"score": 0-100, "issues": n}}，各维度独立扣分。
    空输入直接返回 1 条 error 级问题，且所有维度记 0 分。"""
    text = (text or "").strip()
    issues = []

    if not text:
        issues.append(_issue("error", "empty", "提示词为空"))
        return {
            "issues": issues,
            "score": 0,
            "dimensions": _zero_dimensions(issues),
        }

    n = len(text)

    # ---- 长度判据 ----
    if n < RECOMMENDED_MIN_CHARS:
        issues.append(_issue("warn", "too_short",
                             "提示词过短（%d 字），通常缺少约束与输出要求，建议补充背景、目标与格式" % n))
    elif n > RECOMMENDED_LENGTH_WARN:
        issues.append(_issue("info", "too_long",
                             "提示词较长（%d 字），若包含多个独立任务，建议拆成多条分别增强" % n))
    if n > MAX_INPUT_CHARS:
        issues.append(_issue("error", "overflow", "超过 %d 字符上限，请精简后再增强" % MAX_INPUT_CHARS))

    # ---- B-02 安全规则组（注入 / 明文密钥 / PII）----
    _check_security(text, issues)

    # ---- 模糊措辞 ----
    for word, hint in _VAGUE_WORDS:
        if word in text:
            issues.append(_issue("warn", "vague_word", "发现模糊措辞「%s」：%s" % (word, hint)))

    # ---- 缺少角色设定 ----
    if _ROLE_HINT_RE.match(text) and not _STRUCTURE_SLOT_RES["role"].search(text):
        issues.append(_issue("info", "no_role",
                             "未设定角色（如「你是一名资深后端工程师」），补充角色后输出通常更聚焦"))

    # ---- 缺少输出格式约束 ----
    if not any(h.lower() in text.lower() for h in _FORMAT_HINTS):
        issues.append(_issue("warn", "no_format",
                             "未说明输出格式（条数/结构/字数/是否含代码块），结果容易跑偏"))

    # ---- 未替换的模板变量 ----
    vars_ = _VAR_RE.findall(text)
    if vars_:
        issues.append(_issue("info", "unfilled_var",
                             "发现 %d 处未替换变量（%s），直接增强会把占位符当作正文处理"
                             % (len(vars_), "、".join(sorted(set(vars_))[:5]))))

    # ---- B-01 LangGPT 六段结构槽位（缺段提示，最多 3 条）----
    for key, label, advice in _missing_structure_slots(text):
        issues.append(_issue("info", "structure_missing_%s" % key,
                             "未定义%s段（LangGPT 结构建议）：%s" % (label, advice)))

    # ---- B-03 示例缺失 ----
    if n > EXAMPLE_HINT_MIN_CHARS and not _EXAMPLE_HINT_RE.search(text):
        issues.append(_issue("info", "no_example",
                             "建议补充 1 组输入/输出示例（如「输入：… → 输出：…」），"
                             "示例是让结果稳定复现最便宜的约束"))

    score = 100
    for it in issues:
        score -= _LEVEL_PENALTY.get(it["level"], 0)
    return {
        "issues": issues,
        "score": max(0, score),
        "dimensions": _summarize_dimensions(issues),
    }
