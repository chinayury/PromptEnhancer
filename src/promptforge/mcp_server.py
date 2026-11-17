#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Project: 提示词工坊项目
# Copyright (C) 2026 智维ITMSKY
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
MCP stdio 服务（第十轮规划 10 落地，借鉴 linshenkx/prompt-optimizer 的
packages/mcp-server）：把增强 / 体检能力以 Model Context Protocol 暴露给
本地 MCP 客户端（Claude Desktop、Cursor 等）。

用法：
  python -m promptforge mcp

协议：stdio 上的换行分隔 JSON-RPC 2.0（MCP 2024-11-05 规范子集）——
initialize / notifications/initialized / tools/list / tools/call，ping。
实现为标准库最小子集：不支持 resources / prompts / logging，够用即止。

工具面（与 Web/CLI 同一引擎，行为一致）：
  - enhance_prompt(text, mode?, style?, modifiers?) → 增强后的提示词
  - lint_prompt(text)                                → 体检 JSON（issues+score）

安全边界：
  - 仅 stdio：不监听任何端口，信任面 = 本机进程（谁启动谁能用），无远程暴露；
  - 复用桌面版连接配置（data/app_config + PROMPTFORGE_API_KEY 环境变量优先）；
  - stdout 只输出协议 JSON，日志一律走 stderr，绝不打印 API Key。

环境变量：PROMPTFORGE_DATA_DIR —— 指定数据目录（默认跟随桌面版 data/），
便于测试与多实例。
"""

import io
import json
import os
import sys

from . import __version__
from .config import load_providers
from .core import engine
from .data import database as db
from .utils.logging_setup import get_logger

logger = get_logger("promptforge.mcp_server")

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "promptforge"

_JSONRPC_PARSE_ERROR = -32700
_JSONRPC_METHOD_NOT_FOUND = -32601
_JSONRPC_INVALID_PARAMS = -32602

_TOOLS = [
    {
        "name": "enhance_prompt",
        "description": "增强一条提示词：返回更清晰、更专业、可执行的版本（与 Web/桌面版同一引擎）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "待增强的提示词（≤100000 字符）"},
                "mode": {"type": "string", "enum": ["user", "system"],
                         "description": "user=普通提示词（默认）；system=系统提示词草稿重写"},
                "style": {"type": "string", "enum": ["balanced", "casual", "technical", "concise"],
                          "description": "增强风格，默认 balanced"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "lint_prompt",
        "description": "提示词静态体检：零 LLM 成本，返回问题列表、八维评分与 0-100 参考分",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "待体检的提示词（≤100000 字符）"},
            },
            "required": ["text"],
        },
    },
]


def _build_cfg(arguments):
    """从桌面配置 + 环境变量合成增强配置（与 cli._build_cfg 同口径）。"""
    saved = db.load_config()
    provider = saved.get("provider", "DeepSeek")
    preset = load_providers().get(provider, {})
    return {
        "provider": provider,
        "api_key": os.environ.get("PROMPTFORGE_API_KEY") or saved.get("api_key", ""),
        "base_url": saved.get("base_url", "") or preset.get("base_url", ""),
        "model": saved.get("model", "") or preset.get("model", ""),
        "temperature": saved.get("temperature", "0.7"),
        "max_tokens": saved.get("max_tokens", "1024"),
        "style": arguments.get("style") or saved.get("style", "balanced"),
        "mode": arguments.get("mode") or saved.get("mode", "user"),
        "modifiers": saved.get("modifiers", []),
    }


def _tool_enhance_prompt(arguments):
    text = (arguments.get("text") or "").strip()
    if not text:
        raise ValueError("text 不能为空")
    if len(text) > engine.MAX_INPUT_CHARS:
        raise ValueError("输入过长（上限 %d 字符）" % engine.MAX_INPUT_CHARS)
    cfg = _build_cfg(arguments)
    result = engine.enhance(cfg, text)
    return result["output"]


def _tool_lint_prompt(arguments):
    text = (arguments.get("text") or "").strip()
    if not text:
        raise ValueError("text 不能为空")
    if len(text) > engine.MAX_INPUT_CHARS:
        raise ValueError("输入过长（上限 %d 字符）" % engine.MAX_INPUT_CHARS)
    from .core import lint as lint_core
    return lint_core.lint(text)


_TOOL_HANDLERS = {
    "enhance_prompt": _tool_enhance_prompt,
    "lint_prompt": _tool_lint_prompt,
}


def _handle(msg):
    """处理一条已解析的 JSON-RPC 请求；返回响应 dict 或 None（通知/忽略）。"""
    method = msg.get("method")
    msg_id = msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
        }}
    if method == "notifications/initialized" or method.startswith("notifications/"):
        return None                      # 通知不回包
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": _TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        handler = _TOOL_HANDLERS.get(name)
        if handler is None:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {
                "code": _JSONRPC_INVALID_PARAMS, "message": "未知工具：%s" % name}}
        try:
            payload = handler(arguments)
            return {"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text", "text": payload if isinstance(payload, str)
                             else json.dumps(payload, ensure_ascii=False)}]}}
        except engine.EnhanceError as e:
            return {"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text", "text": _error_text(e)}], "isError": True}}
        except (ValueError, engine.ConfigError) as e:
            return {"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text", "text": str(e)}], "isError": True}}
    if msg_id is None:
        return None
    return {"jsonrpc": "2.0", "id": msg_id, "error": {
        "code": _JSONRPC_METHOD_NOT_FOUND, "message": "未知方法：%s" % method}}


def _error_text(e):
    text = str(e)
    hint = getattr(e, "hint", None)
    return text + ("（建议：%s）" % hint if hint else "")


def run():
    """MCP stdio 主循环：逐行读 JSON-RPC、处理后写 stdout；EOF 退出。"""
    ensure_out = getattr(sys.stdout, "buffer", None)
    if ensure_out is not None:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    try:
        db.init_db()
    except Exception as e:               # 库不可用也要能应答 initialize（增强工具再报错）
        logger.warning("数据库初始化失败（增强工具将不可用）: %s", e)
    logger.info("PromptForge MCP stdio v%s 启动（数据目录：%s）", __version__, db._base_dir())
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            if not isinstance(msg, dict):
                raise ValueError("载荷须为 JSON 对象")
        except ValueError as e:
            resp = {"jsonrpc": "2.0", "id": None, "error": {
                "code": _JSONRPC_PARSE_ERROR, "message": "JSON 解析失败：%s" % e}}
            _emit(resp)
            continue
        try:
            resp = _handle(msg)
        except Exception as e:           # 处理器兜底：任何意外转工具级错误，进程不退
            logger.exception("MCP 处理异常")
            resp = {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                    "error": {"code": -32603, "message": "内部错误：%s" % e}}
        if resp is not None:
            _emit(resp)


def _emit(resp):
    sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    run()
