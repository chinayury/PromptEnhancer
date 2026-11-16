#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Project: 提示词工坊项目
# Copyright (C) 2026 智维ITMSKY
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
增强引擎（业务层核心）。

职责：
  - 调用任意通用兼容的 /chat/completions 接口，将用户提示词增强为更优版本。
  - 抛出结构化异常（鉴权 / 限流 / 超时 / 网络 / 空结果），便于上层精确提示。
  - 对「模型返回空结果」做有限次自动重试；对「疑似复述原文」打低质量标记。
  - 校验连接配置，避免把明显错误参数发给远端。

本模块只依赖标准库（urllib / json / socket），不依赖任何 GUI 或存储组件，
因此可被 GUI、CLI、测试独立复用。
"""

import json
import os
import socket
import time
import urllib.request
import urllib.error

from ..utils.logging_setup import get_logger
from ..data import llm_cache
from .lint import MAX_INPUT_CHARS
from .templates import (SYSTEM_TEMPLATE, USER_TEMPLATE, STYLE_GUIDANCE,
                        MAX_OUTPUT_CHARS, SYSTEM_MODE_TEMPLATE,
                        SYSTEM_MODE_USER_TEMPLATE, ENHANCE_MODES,
                        MODIFIERS)

logger = get_logger("promptforge.core.engine")

# 默认与上限
DEFAULT_TIMEOUT = 90
DEFAULT_TEMPERATURE = 0.7
DEFAULT_MAX_TOKENS = 1024
MAX_RETRIES = 2  # 空结果重试次数（不计入正式请求上限）

TEMP_WHITELIST = {"0.3", "0.5", "0.7", "0.9", "1.0"}
MAXTOKEN_WHITELIST = {"512", "1024", "2048", "4096"}
# 增强模式白名单（user=普通提示词增强；system=系统提示词草稿重写）。
MODE_WHITELIST = set(ENHANCE_MODES)
# 行为修饰符白名单（第九轮）：键集合来自 templates.MODIFIERS。
MODIFIER_WHITELIST = set(MODIFIERS.keys())

# 响应缓存总开关：环境变量 PROMPTFORGE_CACHE，默认开启；显式 0/false/no/off 关闭。
_CACHE_OFF = {"0", "false", "no", "off"}


def _cache_enabled() -> bool:
    """PROMPTFORGE_CACHE 未设置或非关闭值 → 启用缓存；关闭时既不读也不写。"""
    return os.environ.get("PROMPTFORGE_CACHE", "1").strip().lower() not in _CACHE_OFF


# =====================================================================
# 结构化异常
# =====================================================================
class EnhanceError(Exception):
    """增强调用的基础异常（所有具体异常的父类）。

    携带可选的稳定 code 与面向用户的中文 hint（默认 None，保持
    ``EnhanceError(msg)`` 的向后兼容构造）。code 供上层做程序化分支，
    hint 供前端直接展示 actionable 建议；二者都不改变原有消息文案。
    """

    # 稳定的错误码常量集合（括号内为对应的 HTTP 语义状态码，仅作文档说明，
    # 真正映射到响应状态码的逻辑在 Web 层 _enhance_status_code）。
    CODE_AUTH_FAILED = "AUTH_FAILED"                  # 400 鉴权失败
    CODE_RATE_LIMITED = "RATE_LIMITED"                # 429 触发限流
    CODE_TIMEOUT = "TIMEOUT"                          # 504 请求超时
    CODE_BAD_RESPONSE = "BAD_RESPONSE"                # 502 响应非有效 JSON
    CODE_EMPTY_RESULT = "EMPTY_RESULT"                # 502 模型返回空内容
    CODE_MODEL_NOT_CONFIGURED = "MODEL_NOT_CONFIGURED"  # 400 连接配置缺失/非法
    CODE_UPSTREAM_ERROR = "UPSTREAM_ERROR"            # 502 上游其它错误

    def __init__(self, *args, code=None, hint=None):
        super().__init__(*args)
        self.code = code
        self.hint = hint


class AuthError(EnhanceError):
    """鉴权失败（401/403）。"""


class RateLimitError(EnhanceError):
    """触发限流（429）。"""


class TimeoutErrorEx(EnhanceError):
    """请求超时。"""


class NetworkError(EnhanceError):
    """网络层错误（无法连接、DNS 等）。"""


class EmptyResponseError(EnhanceError):
    """模型返回了空内容，需重试。"""


class ConfigError(EnhanceError):
    """连接配置校验未通过（如缺少 Key / URL 非法）。"""


# =====================================================================
# 配置校验
# =====================================================================
def validate_config(cfg: dict) -> dict:
    """校验并归一化连接配置；不通过时抛出 ConfigError（带友好中文提示）。"""
    if cfg is None:
        raise ConfigError("连接配置为空，请先在「模型连接」中保存配置",
                          code=EnhanceError.CODE_MODEL_NOT_CONFIGURED,
                          hint="请先在「模型连接」中保存连接配置后再增强")
    api_key = (cfg.get("api_key") or "").strip()
    base_url = (cfg.get("base_url") or "").strip()
    model = (cfg.get("model") or "").strip()

    if not api_key:
        raise ConfigError("请先填写 API Key（在「模型连接」区）",
                          code=EnhanceError.CODE_MODEL_NOT_CONFIGURED,
                          hint="API Key 为必填项，请填写后保存配置")
    if not base_url:
        raise ConfigError("请填写 Base URL，或先选择厂商预设",
                          code=EnhanceError.CODE_MODEL_NOT_CONFIGURED,
                          hint="请填写 Base URL，或从厂商预设中选择一项")
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        raise ConfigError("Base URL 须以 http:// 或 https:// 开头",
                          code=EnhanceError.CODE_MODEL_NOT_CONFIGURED,
                          hint="Base URL 须以 http:// 或 https:// 开头，请检查是否多输/漏输")
    if not model:
        raise ConfigError("请填写模型名（Model）",
                          code=EnhanceError.CODE_MODEL_NOT_CONFIGURED,
                          hint="请填写模型名（Model），或从厂商预设中自动带入")

    temperature = (cfg.get("temperature") or str(DEFAULT_TEMPERATURE)).strip()
    if temperature not in TEMP_WHITELIST:
        # 不在白名单时尝试数值兜底，仍非法则报错
        try:
            float(temperature)
        except ValueError:
            raise ConfigError("温度取值非法，请从下拉框选择或填写合法数值",
                              code=EnhanceError.CODE_MODEL_NOT_CONFIGURED,
                              hint="温度请从下拉框选择，或填写 0~1 之间的数值")
    max_tokens = (cfg.get("max_tokens") or str(DEFAULT_MAX_TOKENS)).strip()
    if max_tokens not in MAXTOKEN_WHITELIST:
        try:
            int(max_tokens)
        except ValueError:
            raise ConfigError("最大长度非法，请从下拉框选择或填写合法整数",
                              code=EnhanceError.CODE_MODEL_NOT_CONFIGURED,
                              hint="最大长度请从下拉框选择，或填写一个正整数")
    mode = (cfg.get("mode") or "user").strip()
    if mode not in MODE_WHITELIST:
        raise ConfigError("增强模式非法（仅支持 user / system）",
                          code=EnhanceError.CODE_MODEL_NOT_CONFIGURED,
                          hint="增强模式仅支持「用户提示词 / 系统提示词」，请从下拉框选择")
    mods_in = cfg.get("modifiers") or []
    if not isinstance(mods_in, (list, tuple)):
        raise ConfigError("行为修饰符须为列表",
                          code=EnhanceError.CODE_MODEL_NOT_CONFIGURED,
                          hint="行为修饰符请从界面勾选，不要手工填写")
    modifiers = []
    for m in mods_in:
        if m not in MODIFIER_WHITELIST:
            raise ConfigError("行为修饰符非法：%s" % m,
                              code=EnhanceError.CODE_MODEL_NOT_CONFIGURED,
                              hint="行为修饰符请从界面勾选，不要手工填写")
        if m not in modifiers:
            modifiers.append(m)

    return {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "style": cfg.get("style", "balanced"),
        "mode": mode,
        "modifiers": modifiers,
    }


# =====================================================================
# 低质量判定
# =====================================================================
def is_low_quality(text: str, output: str) -> bool:
    """判断增强结果是否仅复述原文（质量偏低）。"""
    import difflib
    a = (text or "").strip().lower()
    b = (output or "").strip().lower()
    if not b:
        return True
    if a and b == a:
        return True
    if len(a) >= 10 and len(b) >= 10:
        ratio = difflib.SequenceMatcher(None, a, b).ratio()
        if ratio > 0.9:
            return True
    return False


# =====================================================================
# 核心调用
# =====================================================================
def _http_post(url: str, payload: dict, api_key: str, timeout: int) -> dict:
    """执行一次 HTTP POST，返回解析后的响应体；将传输层异常转为结构化异常。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", "Bearer " + api_key)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")
        if e.code in (401, 403):
            raise AuthError("鉴权失败（HTTP %s），请检查 API Key" % e.code,
                            code=EnhanceError.CODE_AUTH_FAILED,
                            hint="API Key 无效、已过期或无权限，请在「模型连接」中检查并更换")
        if e.code == 429:
            raise RateLimitError("触发限流（HTTP 429），请稍后重试或降低并发",
                                 code=EnhanceError.CODE_RATE_LIMITED,
                                 hint="厂商侧流量保护，等待 1 分钟或更换模型/账号")
        if e.code in (408, 504):
            raise TimeoutErrorEx("请求超时（HTTP %s）" % e.code,
                                 code=EnhanceError.CODE_TIMEOUT,
                                 hint="上游响应超时，可稍后重试或在连接配置中调大超时时间")
        raise EnhanceError("HTTP %s: %s" % (e.code, detail[:400]),
                           code=EnhanceError.CODE_UPSTREAM_ERROR,
                           hint="模型服务返回异常，请稍后重试或检查 Base URL 是否正确")
    except urllib.error.URLError as e:
        raise NetworkError("网络错误: %s" % e.reason)
    except socket.timeout:
        raise TimeoutErrorEx("请求超时（socket）",
                             code=EnhanceError.CODE_TIMEOUT,
                             hint="上游响应超时，可稍后重试或在连接配置中调大超时时间")
    # 解码与解析放在传输成功之后：上游返回 200 但正文不是 JSON（如网关 HTML
    # 错误页 / 编码异常）时，原实现会让 JSONDecodeError/UnicodeDecodeError 裸抛、
    # 上层按未处理异常转 500；现归一为 EnhanceError，走既有的失败提示链路。
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise EnhanceError("模型响应不是有效 JSON（HTTP 200），"
                           "请检查 Base URL 是否指向 OpenAI 兼容的 /chat/completions 端点",
                           code=EnhanceError.CODE_BAD_RESPONSE,
                           hint="模型返回的不是有效数据，请检查 Base URL 是否指向 OpenAI 兼容的 /chat/completions 端点")


def _chat_request(cfg: dict, messages: list, timeout: int) -> dict:
    """按消息列表发起一次 /chat/completions 调用并解析结果（增强与评估共用）。
    空内容抛 EmptyResponseError；响应结构异常时按空内容/结构化异常处理，
    绝不让 AttributeError 之类裸抛成 500。"""
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": float(cfg["temperature"]),
        "max_tokens": int(cfg["max_tokens"]),
    }
    body = _http_post(cfg["base_url"].rstrip("/") + "/chat/completions",
                      payload, cfg["api_key"], timeout)
    choices = body.get("choices") if isinstance(body, dict) else None
    first = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    choice = (message.get("content") or "")
    choice = choice.strip() if isinstance(choice, str) else ""
    if not choice:
        raise EmptyResponseError("模型返回为空，将自动重试",
                                 code=EnhanceError.CODE_EMPTY_RESULT,
                                 hint="模型返回了空内容，请稍后重试或更换模型")
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    return {
        "output": choice,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "request_id": body.get("id") if isinstance(body, dict) else "",
    }


def _enhance_messages(cfg: dict, text: str) -> list:
    """构造增强用的 system/user 双模板消息。

    缓存键与实际请求共用同一构造：既保证「命中键」与「真实请求体」严格一致，
    又不改变任何既有请求语义（与旧 _do_one_request 内联构造逐字节等价）。

    增强模式（mode，借鉴 linshenkx/prompt-optimizer 的双模式优化）：
      - user（缺省）：SYSTEM_TEMPLATE + USER_TEMPLATE，与历史行为逐字节一致；
      - system：SYSTEM_MODE_TEMPLATE + SYSTEM_MODE_USER_TEMPLATE，
        把输入按「系统提示词草稿」标准重写。

    行为修饰符（modifiers，第九轮借鉴 prompt-decorators）：按勾选顺序把
    各修饰符约束段追加到系统提示词末尾；空列表 → 消息与历史逐字节一致。"""
    style_note = STYLE_GUIDANCE.get(cfg["style"], "")
    mod_note = "".join("\n" + MODIFIERS[m] for m in (cfg.get("modifiers") or [])
                       if m in MODIFIERS)
    if cfg.get("mode", "user") == "system":
        system_content = SYSTEM_MODE_TEMPLATE + ("\n\n" + style_note if style_note else "") + mod_note
        return [
            {"role": "system", "content": system_content},
            {"role": "user", "content": SYSTEM_MODE_USER_TEMPLATE.replace("{input}", text)},
        ]
    system_content = SYSTEM_TEMPLATE + ("\n\n" + style_note if style_note else "") + mod_note
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": USER_TEMPLATE.replace("{input}", text)},
    ]


def _do_one_request(cfg: dict, text: str, timeout: int) -> dict:
    """发起单轮增强请求（内置 system/user 双模板）；空内容抛 EmptyResponseError。"""
    return _chat_request(cfg, _enhance_messages(cfg, text), timeout)


def raw_completion(cfg: dict, system: str, user: str,
                   timeout: int = DEFAULT_TIMEOUT) -> dict:
    """自由 system/user 双角色调用（不套用增强模板），供评估裁判等场景复用。

    与 enhance 的差异：不做空结果重试（评估语义下空响应即失败，由调用方
    给出明确错误提示）；同样只抛结构化异常，绝不裸抛网络/解析异常。

    与 enhance 一致，在最外层接入响应缓存：命中直接返回（带 cached=True），
    可用 PROMPTFORGE_CACHE=0 关闭；超长输入不缓存；任何缓存异常都只记
    warning 并忽略，绝不影响调用结果。"""
    safe = validate_config(cfg)
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    cache_key = None
    cacheable = _cache_enabled() and (len(system) + len(user)) <= MAX_INPUT_CHARS
    if cacheable:
        try:
            params = {"temperature": float(safe["temperature"]),
                      "max_tokens": int(safe["max_tokens"])}
            cache_key = llm_cache.make_key(safe["model"], messages, params)
            cached = llm_cache.get(cache_key)
        except Exception as e:
            logger.warning("读取 LLM 缓存失败，本次忽略缓存: %s", e)
            cached = None
        if cached is not None:
            result = dict(cached)
            result["cached"] = True
            logger.info("LLM 缓存命中，跳过上游调用（model=%s）", safe["model"])
            return result

    result = _chat_request(safe, messages, timeout)
    if cache_key is not None:
        try:
            llm_cache.put(cache_key, {
                "output": result.get("output"),
                "prompt_tokens": result.get("prompt_tokens"),
                "completion_tokens": result.get("completion_tokens"),
                "request_id": result.get("request_id"),
            })
        except Exception as e:
            logger.warning("写入 LLM 缓存失败，忽略: %s", e)
    return result


def enhance(cfg: dict, text: str, timeout: int = DEFAULT_TIMEOUT,
            max_retries: int = MAX_RETRIES) -> dict:
    """
    增强一条提示词，内置空结果重试。

    参数：
      cfg         —— 经 validate_config 校验后的连接配置。
      text        —— 原始提示词。
      timeout     —— 单次请求超时（秒）。
      max_retries —— 空结果重试次数。

    返回：{"output", "prompt_tokens", "completion_tokens", "request_id", "quality"}
          （命中缓存时额外带 cached=True，不改变既有键的语义）
    异常：ConfigError / AuthError / RateLimitError / TimeoutErrorEx /
          NetworkError / EmptyResponseError / EnhanceError

    缓存：最外层接入本地响应缓存（llm_cache），按 model+messages+采样参数
    命中即直接返回；可用 PROMPTFORGE_CACHE=0 关闭（关闭时既不读也不写）；
    单条输入超过 lint.MAX_INPUT_CHARS 不缓存；任何缓存异常都只记 warning
    并忽略，绝不因缓存失败导致增强失败。

    注意：本函数不直接处理「疑似复述原文」——那属于质量标记，由上层或调用方
    通过 is_low_quality 判定并写入数据库，便于审计与展示。
    """
    safe = validate_config(cfg)
    t0 = time.time()
    cache_key = None
    cacheable = _cache_enabled() and len(text) <= MAX_INPUT_CHARS
    if cacheable:
        try:
            messages = _enhance_messages(safe, text)
            params = {"temperature": float(safe["temperature"]),
                      "max_tokens": int(safe["max_tokens"])}
            cache_key = llm_cache.make_key(safe["model"], messages, params)
            cached = llm_cache.get(cache_key)
        except Exception as e:
            logger.warning("读取 LLM 缓存失败，本次忽略缓存: %s", e)
            cached = None
        if cached is not None:
            result = dict(cached)
            result["quality"] = ("low_quality"
                                 if is_low_quality(text, result.get("output", "")) else "")
            result["cached"] = True
            logger.info("LLM 缓存命中，跳过上游调用（model=%s）", safe["model"])
            return result

    last_err = ""
    result = None
    for attempt in range(1 + max_retries):
        try:
            result = _do_one_request(safe, text, timeout)
            break
        except EmptyResponseError as e:
            last_err = str(e)
            logger.warning("空结果，进行第 %d 次重试（上限 %d）", attempt + 1, max_retries)
        except EnhanceError:
            # 鉴权/限流/超时/网络等不参与空结果重试，直接上抛
            raise
    if result is None:
        dur = int((time.time() - t0) * 1000)
        logger.error("增强失败（空结果重试耗尽）: %s", last_err)
        raise EmptyResponseError("模型连续返回空内容，请稍后重试或更换模型",
                                 code=EnhanceError.CODE_EMPTY_RESULT,
                                 hint="模型连续返回空内容，请稍后重试或更换模型")

    result["quality"] = "low_quality" if is_low_quality(text, result["output"]) else ""
    if len(result["output"]) > MAX_OUTPUT_CHARS:
        logger.info("增强结果 %d 字，超过 %d 字软上限",
                    len(result["output"]), MAX_OUTPUT_CHARS)
    if cache_key is not None:
        try:
            llm_cache.put(cache_key, {
                "output": result["output"],
                "prompt_tokens": result.get("prompt_tokens"),
                "completion_tokens": result.get("completion_tokens"),
                "request_id": result.get("request_id"),
            })
        except Exception as e:
            logger.warning("写入 LLM 缓存失败，忽略: %s", e)
    return result
