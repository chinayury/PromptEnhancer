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
from forge import assembler as forge_assembler
from forge import parser as forge_parser

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
    {
        "name": "forge_outline",
        "description": "指令构造器（五槽）：解析设计文档，返回章节目录 / 待确认项概览（纯本地零 token，定位用）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "设计文档全文（粘贴）"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "forge_generate",
        "description": "指令构造器（五槽）：把设计文档编译为「任务+切片+范围+反向边界+验收」的结构化指令（纯本地零 token）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "设计文档全文（粘贴）"},
                "task": {"type": "string", "description": "一句话任务，动词开头"},
                "pick": {"type": "string", "description": "本期切片章节编号，逗号分隔（如 3,4.6；先 forge_outline 核对）"},
                "period": {"type": "string", "description": "本期名称（如 一期）"},
                "code": {"type": "string", "description": "涉及代码文件，逗号分隔"},
                "decision": {"type": "array", "items": {"type": "string"},
                             "description": "对文档待确认项的答复（每条一句）"},
                "no_gate": {"type": "boolean", "description": "不附加【本期出口】门禁（默认 false）"},
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


def _tool_forge_outline(arguments):
    text = (arguments.get("text") or "").strip()
    if not text:
        raise ValueError("text 不能为空")
    if len(text) > engine.MAX_INPUT_CHARS:
        raise ValueError("输入过长（上限 %d 字符）" % engine.MAX_INPUT_CHARS)
    import contextlib
    from forge.cli import _print_outline
    doc = forge_parser.parse(text, "MCP 粘贴文本")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):      # outline 打印流重定向，保 stdout 协议纯净
        _print_outline(doc)
    return buf.getvalue()


def _tool_forge_generate(arguments):
    text = (arguments.get("text") or "").strip()
    if not text:
        raise ValueError("text 不能为空")
    if len(text) > engine.MAX_INPUT_CHARS:
        raise ValueError("输入过长（上限 %d 字符）" % engine.MAX_INPUT_CHARS)
    doc = forge_parser.parse(text, "MCP 粘贴文本")
    tokens = [t.strip() for t in (arguments.get("pick") or "").split(",") if t.strip()]
    picks = forge_parser.resolve_picks(doc, tokens) if tokens else []
    res = forge_assembler.assemble(
        doc, picks=picks, task=arguments.get("task"),
        code_files=[c.strip() for c in (arguments.get("code") or "").split(",") if c.strip()],
        decisions=arguments.get("decision") or [],
        period=arguments.get("period"),
        add_exit_gate=not bool(arguments.get("no_gate")))
    unresolved = json.dumps(res.get("unresolved", []), ensure_ascii=False)
    return res["prompt"] + "\n\n---\n[forge 报告] 待确认项：" + unresolved


_TOOL_HANDLERS = {
    "enhance_prompt": _tool_enhance_prompt,
    "lint_prompt": _tool_lint_prompt,
    "forge_outline": _tool_forge_outline,
    "forge_generate": _tool_forge_generate,
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
