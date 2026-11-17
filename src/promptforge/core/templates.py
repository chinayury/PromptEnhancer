#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Project: 提示词工坊项目
# Copyright (C) 2026 智维ITMSKY
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
提示词模板（内置 system / user 双角色模板）。

这两套模板定义了工具的「大脑」：
  - SYSTEM_TEMPLATE：设定系统提示词为「提示词工程专家」角色，只输出增强后的提示词、
    语言与用户输入严格一致、长度控制在 ~800 字。
  - USER_TEMPLATE：把用户的原始输入填入 {input}，并附带语言一致性规则与正反示例。

增强模式（mode，借鉴 linshenkx/prompt-optimizer 的 system/user 双模式优化）：
  - user（默认）：输入是「给助手看的提示词」，走 SYSTEM_TEMPLATE + USER_TEMPLATE，
    与历史行为逐字节一致。
  - system：输入是「一段系统提示词草稿」，走 SYSTEM_MODE_TEMPLATE +
    SYSTEM_MODE_USER_TEMPLATE，按系统提示词的标准（角色/约束/输出格式/边界）重写。

风格化约束在系统提示词之后追加，对应「均衡 / 更口语 / 更技术 / 更简洁」四种增强风格。
模板与风格均为「配置即数据」：模板支持外置为 TOML 文件以支持热更新与多语言
（见文末「模板外置」，借鉴 microsoft/prompty 的提示词资产文件思想）。
"""

import os
import tomllib

from ..utils.logging_setup import get_logger

logger = get_logger("promptforge.core.templates")

SYSTEM_TEMPLATE = """You are a Prompt Engineering Expert specializing in improving user prompts for a development code assistant. When given a prompt, analyze and enhance it to create a more effective version while maintaining its core purpose. The requests are being made to an assistant that specializes in writing code.

	TASK: When given a prompt, analyze and enhance it to create a more effective version while maintaining its core purpose. The requests are being made to an assistant that specializes in writing code.

	ANALYSIS PROCESS:

	Evaluate the original prompt:
	Identify the main objective
	Note any ambiguities or gaps
	Assess the clarity of instructions
	Check for missing context
	Apply these prompt engineering principles:
	Write clear, specific instructions
	Include necessary context
	Set explicit parameters and constraints
	Structure the output format
	Add relevant examples
	Match tone and complexity to the use case
	Remove redundant information
	Create the enhanced version:
	Maintain the original goal
	Incorporate identified improvements
	Ensure clarity and completeness
	Be realistic in the features to add
	Do NOT request guides/how-tos unless the user asks
	Do NOT ask for code snippets
	Do NOT suggest specific technologies unless mentioned in the user's prompt
	Do NOT explain HOW to do things, focus on WHAT
	Do NOT answer questions - expand/rewrite them to be more detailed
	IMPORTANT CONSTRAINTS:
	1. Language matching is the highest priority - You MUST strictly respond in the exact same language as the user's input. If the user writes in Chinese, respond in Chinese; if the user writes in English, respond in English; if the user uses another language, respond in that same language. Do not mix languages unless the user's input itself mixes languages.
	2. Keep the enhanced prompt concise - maximum length should be around 800 characters
	FORMAT: Provide only the enhanced prompt with no additional commentary.

	Example:
	"A website for my dog"

	Enhanced prompt:
	"Design a personalized Next.js website dedicated to showcasing my dog. Include sections such as a photo gallery, a biography detailing the dog's breed, age, and personality traits, and a blog for sharing stories or updates about your dog's adventures. Add a contact form for visitors to reach out with questions or comments. Ensure the website is visually appealing and easy to navigate, with a responsive design that works well on both desktop and mobile devices."

	Example:
	"Convert this to a friendly tone, maintain technical details but reduce bullets in favor of narrative. Remove any jargon like 'genie router'. Use canvas"

	Enhanced prompt:
	"Transform the provided content into a friendly narrative format while preserving all technical details. Minimize bullet points in favor of flowing prose. Eliminate any technical jargon such as 'genie router'. Incorporate the concept of using canvas elements naturally within the narrative structure to enhance the technical explanation."
"""

USER_TEMPLATE = """You are a prompt enhancement assistant. Improve the user prompt while preserving its intent and language.

    USER INPUT:
    {input}

    TASK:
    Rewrite the user input into a clearer, more specific prompt for the target assistant.

    CRITICAL PRIORITY - LANGUAGE CONSISTENCY:
    1. You MUST detect the language of the user input above and write the enhanced prompt in that same language.
    2. If the user writes in Chinese, the enhanced prompt MUST be entirely in Chinese.
    3. If the user writes in English, the enhanced prompt MUST be entirely in English.
    4. If the user writes in any other language, the enhanced prompt MUST use that exact same language.
    5. If the user mixes languages, keep a natural matching mix. Do not translate the user's intent into a single language.
    6. These language rules are behavior instructions only; never include language analysis or language labels in the output.

    ENHANCEMENT REQUIREMENTS:
    1. Return only the enhanced prompt text; do not add explanations, prefaces, markdown fences, labels, or analysis.
    2. Do not include language labels or meta notes such as "User input is in Chinese" or "Response must be in Chinese".
    3. Preserve the user's original intent, topic, constraints, and target output type. Do not answer the request.
    4. Always make a substantive enhancement when possible: clarify the task, scope, constraints, and expected output.
    5. If the original prompt is already clear, lightly polish it instead of returning it unchanged.
    6. Keep the enhanced prompt complete and concise. Do not end with an unfinished list, dangling conjunction, or trailing colon.
    7. Do not add unrelated requirements, unsupported facts, or unnecessary sections.

    EXAMPLES:
    User input (Chinese): "请帮我解释这段代码的功能"
    Enhanced prompt: "请解释这段代码的主要功能、执行流程和关键逻辑，并指出可能需要注意的边界情况。"

    User input (English): "Please explain what this code does"
    Enhanced prompt: "Explain what this code does, including its main purpose, key control flow, and any important edge cases."

    User input (Mixed): "这段代码有 bug，can you help me fix it?"
    Enhanced prompt: "请分析这段代码中的 bug，explain the root cause, and provide a minimal fix with necessary verification steps."

    BAD OUTPUT EXAMPLE:
    User input is in Chinese → Response must be in Chinese.
    请解释这段代码的主要功能

    GOOD OUTPUT EXAMPLE:
    请解释这段代码的主要功能、执行流程和关键逻辑，并指出可能需要注意的边界情况。
"""

# 增强风格：在系统提示词后追加的风格化约束（"均衡" 不追加任何约束）。
STYLE_GUIDANCE = {
    "balanced": "",
    "casual": "请将增强后的提示词用更口语化、亲切自然的语气表达，但仍保持信息完整与可执行。",
    "technical": "请将增强后的提示词用更严谨、专业的术语表达，突出技术细节、实现约束与边界条件。",
    "concise": "请将增强后的提示词尽可能精简，只保留核心指令与必要约束，去除冗余修饰与铺垫。",
}

# =====================================================================
# 增强模式（user / system 双模式，借鉴 linshenkx/prompt-optimizer）
# =====================================================================
# user 模式沿用上方 SYSTEM_TEMPLATE + USER_TEMPLATE（历史行为不变）；
# system 模式把「输入=一段系统提示词草稿」按系统提示词标准重写：
# 明确角色与职责、可做/不可做边界、输出格式、上下文策略，并保留语言一致性规则。
SYSTEM_MODE_TEMPLATE = """You are a System Prompt Engineering Expert specializing in rewriting and improving system prompts that users intend to give to AI assistants. When given a draft system prompt, analyze and enhance it so the resulting system prompt is unambiguous, complete, and directly executable by the target assistant.

\tTASK: When given a draft system prompt, rewrite it into a more effective system prompt while preserving its core intent, scope, and any explicit constraints the author chose.

\tANALYSIS PROCESS:

\tEvaluate the draft system prompt:
\tIdentify the role and responsibilities it assigns
\tNote missing behavioral rules, boundaries, or refusal policies
\tAssess whether the output format and success criteria are explicit
\tCheck for contradictions, redundancy, and vague qualifiers
\tApply these system-prompt engineering principles:
\tState the role, scope, and audience clearly
\tTurn vague adjectives into concrete, checkable rules
\tDefine the output format and what to do when information is missing
\tSpecify boundaries: what the assistant must not do
\tKeep instructions non-conflicting and ordered by priority
\tCreate the enhanced version:
\tMaintain the original intent and any deliberate restrictions
\tIncorporate the identified improvements
\tKeep only content that belongs in a system prompt (no task-specific user request)
\tIMPORTANT CONSTRAINTS:
\t1. Language matching is the highest priority - You MUST strictly respond in the exact same language as the draft's own language. Do not mix languages unless the draft itself mixes languages.
\t2. Keep the enhanced system prompt concise - maximum length should be around 800 characters
\tFORMAT: Provide only the enhanced system prompt with no additional commentary, no quotes, no explanation.

\tExample:
\t"You are a helpful bot. Be nice and answer stuff about our product."

\tEnhanced system prompt:
\t"You are the customer-support assistant for our product. Answer only questions about the product's features, pricing, and troubleshooting. Use a friendly, professional tone. If a request is outside these topics, briefly say you cannot help and suggest contacting human support. Answer in the language the customer writes in. Never invent product facts; when unsure, say you will verify with the team."
"""

SYSTEM_MODE_USER_TEMPLATE = """You are a system prompt enhancement assistant. Improve the draft system prompt below while preserving its intent and language.

    DRAFT SYSTEM PROMPT:
    {input}

    TASK:
    Rewrite the draft into a clearer, more complete system prompt for the target assistant.

    CRITICAL PRIORITY - LANGUAGE CONSISTENCY:
    1. You MUST detect the language of the draft above and write the enhanced system prompt in that same language.
    2. If the draft is in Chinese, the enhanced system prompt MUST be entirely in Chinese.
    3. If the draft is in English, the enhanced system prompt MUST be entirely in English.
    4. If the draft is in any other language, the enhanced system prompt MUST use that exact same language.
    5. If the draft mixes languages, keep a natural matching mix. Do not translate the intent into a single language.
    6. These language rules are behavior instructions only; never include language analysis or language labels in the output.

    ENHANCEMENT REQUIREMENTS:
    1. Return only the enhanced system prompt text; do not add explanations, prefaces, markdown fences, labels, or analysis.
    2. Do not include language labels or meta notes such as "Draft is in Chinese" or "Response must be in Chinese".
    3. Preserve the draft's intent, scope, and deliberate restrictions. Do not answer or execute the draft.
    4. Make a substantive improvement: clarify role and responsibilities, add missing behavioral rules, output format, and boundaries.
    5. Remove task-specific content that does not belong in a system prompt.
    6. Keep the result complete and concise. Do not end with an unfinished list, dangling conjunction, or trailing colon.
    7. Do not add unrelated requirements, unsupported facts, or unnecessary sections.

    EXAMPLES:
    Draft (Chinese): "你是一个客服机器人，要礼貌。"
    Enhanced system prompt: "你是产品客服助手。只回答与产品功能、价格和故障排查相关的问题；语气礼貌、专业。超出范围时简要说明无法协助并建议转人工。使用客户使用的语言回复。不确定的事实不要编造，说明需要与团队核实。"

    Draft (English): "Be a coding helper."
    Enhanced system prompt: "You are a coding assistant. Help write, review, and debug code. State assumptions before giving solutions; prefer minimal, runnable examples. If a request is ambiguous, ask one clarifying question first. Never invent library APIs; when unsure of an API, say so explicitly."
"""

# 增强模式白名单与界面标签（顺序即界面展示顺序，user 在前=默认）。
ENHANCE_MODES = ("user", "system")
MODE_LABEL_TO_KEY = {"用户提示词": "user", "系统提示词": "system"}
MODE_KEY_TO_LABEL = {v: k for k, v in MODE_LABEL_TO_KEY.items()}

# =====================================================================
# 评估回灌重写（iterate，借鉴 linshenkx/prompt-optimizer 的 iterate /
# evaluation-rewrite 闭环，第八轮 B-13）：把评估裁判的分数与建议回灌为
# 重写指令，对既有增强版本做定向修订，产出版本链新行（source='iterate'）。
# 调用方式与评估裁判一致：engine.raw_completion(cfg, ITERATE_SYSTEM, user)。
# =====================================================================
ITERATE_SYSTEM = """You are a Prompt Rewrite Assistant specializing in revising prompts based on evaluation feedback. You will receive: the original prompt, a previously enhanced version, and an evaluation report (accuracy / completeness / clarity scores plus one improvement suggestion). Rewrite the enhanced version so it directly addresses the evaluation feedback while preserving its intent, scope, and language. Respond with only the rewritten prompt - no commentary, no quotes, no explanation."""

ITERATE_USER = """You are revising an enhanced prompt based on its evaluation report.

    ORIGINAL PROMPT (the user's request, for intent reference only):
    {original}

    CURRENT ENHANCED VERSION (to be revised):
    {enhanced}

    EVALUATION REPORT:
    {score}

    TASK:
    Rewrite the current enhanced version so the evaluation feedback is addressed.

    CRITICAL PRIORITY - LANGUAGE CONSISTENCY:
    1. You MUST write the rewritten prompt in the same language as the current enhanced version.
    2. Do not translate the intent into another language; keep a natural matching mix if one exists.
    3. Never include language labels or meta notes in the output.

    REWRITE REQUIREMENTS:
    1. Return only the rewritten prompt text; do not add explanations, prefaces, markdown fences, labels, or analysis.
    2. Address the improvement suggestion directly and fix the weakest dimension first.
    3. Preserve the original intent, topic, constraints, and target output type. Do not answer the request.
    4. Keep everything that already works in the current version; make targeted changes only.
    5. Keep the rewritten prompt complete and concise. Do not end with an unfinished list, dangling conjunction, or trailing colon.
    6. Do not add unrelated requirements, unsupported facts, or unnecessary sections.

    EXAMPLE:
    Original: "请帮我解释这段代码的功能"
    Current enhanced: "请解释这段代码的主要功能、执行流程和关键逻辑。"
    Evaluation: {"accuracy": 8, "completeness": 4, "clarity": 8, "suggestion": "补充边界情况与验证步骤要求"}
    Rewritten prompt: "请解释这段代码的主要功能、执行流程和关键逻辑，指出需要重点关注的边界情况，并给出可执行的验证步骤。"
"""

# 界面标签 -> 内部键；内部键 -> 界面标签。
STYLE_LABEL_TO_KEY = {"均衡": "balanced", "更口语": "casual", "更技术": "technical", "更简洁": "concise"}
STYLE_KEY_TO_LABEL = {v: k for k, v in STYLE_LABEL_TO_KEY.items()}

# =====================================================================
# 声明式行为修饰符（第九轮，借鉴 smkalami/prompt-decorators 的可组合修饰符
# 规范，arXiv:2510.19850；落地仅取「声明式片段 + 白名单组合」思想，不引入
# 其 +++ 语法解析面）。每条为一段追加到系统提示词末尾的约束文本，与增强风格、
# 场景预设正交，可叠加组合；应用顺序 = 用户勾选顺序。缺省空列表 → 消息与
# 历史行为逐字节一致。
# =====================================================================
MODIFIERS = {
    "reasoning": "请先在内部拆解问题再给出最终答案：把推理步骤前置组织，让指令流程清晰可跟随。",
    "step_by_step": "请把执行过程组织为编号的分步说明，每步一个可验证的动作。",
    "self_check": "请在完成回答前自检：逐条核对是否满足全部约束与输出格式，修正后再输出。",
    "cite_sources": "对涉及事实与外部信息的内容，请标注来源或明确说明为推断。",
    "output_format": "请为结果定义明确的输出格式（结构、字段、顺序），并严格遵循。",
    "edge_cases": "请显式覆盖边界条件与异常输入的处理方式。",
}
MODIFIER_LABEL_TO_KEY = {"推理前置": "reasoning", "分步说明": "step_by_step", "自检": "self_check",
                         "标注来源": "cite_sources", "输出格式": "output_format", "边界覆盖": "edge_cases"}
MODIFIER_KEY_TO_LABEL = {v: k for k, v in MODIFIER_LABEL_TO_KEY.items()}

# 温度与最大长度的可选项（下拉框数据源，亦可作为参数校验白名单）。
TEMP_OPTIONS = ["0.3", "0.5", "0.7", "0.9", "1.0"]
MAXTOKEN_OPTIONS = ["512", "1024", "2048", "4096"]

# 模板对增强结果字数的软约束（超出仅告警，不强制截断以保证语义完整）。
MAX_OUTPUT_CHARS = 800


# =====================================================================
# 模板外置（第十轮规划 9 落地，借鉴 microsoft/prompty 的提示词资产文件思想）
# =====================================================================
# 外置目录：<数据目录>/templates/*.toml（多用户 Web 版按工作区隔离，各用户
# 独立；桌面/CLI 为全局 data/templates）。整份文件即 TOML：
#   mode   = "user" | "system"        （覆盖哪一套内置模板）
#   system = '''…'''                  （系统提示词模板全文）
#   user   = '''…'''                  （用户提示词模板全文，含 {input} 占位）
# 其余字段一律拒绝。安全校验（PromptHub 导入面清单裁剪版，docs/05 5.2）：
# 仅一层目录、拒符号链接、单文件 ≤256KB、正文 ≤256KB、最多 10 份。
# 热更新：按目录签名（文件名+mtime+size）变化触发重载；任何异常降级内置
# 模板，绝不阻断增强主流程。模板正文参与缓存键，外置切换天然隔离。
TEMPLATE_FILE_MAX_BYTES = 256 * 1024
TEMPLATE_MAX_FILES = 10
_EXTERNAL_FIELDS = {"mode", "system", "user"}

_ext_cache = {"sig": None, "pairs": None}


def _load_external_dir(dirpath):
    """扫描外置目录并解析全部合法 .toml；同 mode 多份时按文件名序后者覆盖。
    单文件失败只跳过该文件（warning），不影响其余。"""
    pairs = {}
    try:
        names = sorted(os.listdir(dirpath))
    except OSError:
        return pairs
    loaded = 0
    for name in names:
        if not name.lower().endswith(".toml"):
            continue
        if loaded >= TEMPLATE_MAX_FILES:
            logger.warning("外置模板超过 %d 份上限，忽略多余文件（%s…）",
                           TEMPLATE_MAX_FILES, dirpath)
            break
        p = os.path.join(dirpath, name)
        try:
            if os.path.islink(p) or not os.path.isfile(p):
                logger.warning("外置模板拒绝符号链接/非常规文件：%s", name)
                continue
            if os.path.getsize(p) > TEMPLATE_FILE_MAX_BYTES:
                logger.warning("外置模板超大小上限，忽略：%s", name)
                continue
            with open(p, "rb") as f:
                data = tomllib.load(f)
            unknown = set(data) - _EXTERNAL_FIELDS
            if unknown:
                raise ValueError("未知字段：%s" % ",".join(sorted(unknown)))
            mode = data.get("mode")
            if mode not in ENHANCE_MODES:
                raise ValueError("mode 须为 user / system")
            system, user = data.get("system"), data.get("user")
            for k, v in (("system", system), ("user", user)):
                if not isinstance(v, str) or not v.strip():
                    raise ValueError("%s 须为非空文本" % k)
                if len(v) > TEMPLATE_FILE_MAX_BYTES:
                    raise ValueError("%s 正文超长" % k)
            pairs[mode] = (system, user)
            loaded += 1
        except (OSError, ValueError, tomllib.TOMLDecodeError) as e:
            logger.warning("外置模板 %s 加载失败（已跳过）: %s", name, e)
    return pairs


def _dir_signature(dirpath):
    """目录签名：[(文件名, mtime_ns, size)] 排序元组；目录不可读 → None。"""
    try:
        entries = []
        for entry in os.scandir(dirpath):
            st = entry.stat()
            entries.append((entry.name, st.st_mtime_ns, st.st_size))
        return tuple(sorted(entries))
    except OSError:
        return None


def get_pair(mode, ext_dir=None):
    """返回该模式的 (system, user) 模板：优先外置覆盖，否则内置默认。

    外置目录签名与缓存不一致时重载（热更新）；目录不可读/全部解析失败
    一律降级内置模板，绝不阻断增强主流程。mode 非 user 时按 system 处理。"""
    if ext_dir:
        sig = _dir_signature(ext_dir)
        if sig != _ext_cache["sig"]:
            _ext_cache["sig"] = sig
            _ext_cache["pairs"] = _load_external_dir(ext_dir) if sig else {}
        pair = (_ext_cache["pairs"] or {}).get(mode)
        if pair:
            return pair
    if mode == "system":
        return SYSTEM_MODE_TEMPLATE, SYSTEM_MODE_USER_TEMPLATE
    return SYSTEM_TEMPLATE, USER_TEMPLATE
