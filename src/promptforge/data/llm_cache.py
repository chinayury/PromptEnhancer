#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Project: 提示词工坊项目
# Copyright (C) 2026 智维ITMSKY
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
LLM 响应缓存（本地 SQLite，纯标准库）。

借鉴 promptfoo 的「本地落库缓存」与 pezzo 的「显式缓存」思路：把一次
/chat/completions 的响应按「模型 + 消息 + 采样参数」全量哈希为键存入本地
库，命中即直接复用，省去重复的上游调用与费用。

设计取舍：
  - **全量 sha256 而非前缀匹配**：前缀/提示缓存（prefix cache）依赖具体厂商
    的服务端能力（如 Anthropic 的 prompt caching），无法在纯客户端侧对任意
    OpenAI 兼容端点稳定复现；全量内容哈希则是提供商无关的精确匹配——同样的
    (model, messages, params) 必然命中同一条响应，语义确定、跨厂商通用，且
    实现只依赖标准库、无需改造请求体。
  - **hits 计数用于观测命中率**：命中的那次读取会把 hits+1，配合 stats() 的
    entries/hits/bytes 汇总，可在运营侧观察「缓存到底省了多少次上游调用」，
    据此调整 TTL 与 prune 阈值。hits 纯属观测，不参与任何正确性判断。
  - **TTL 与淘汰**：ttl_seconds=0 表示永不过期；prune() 按 created_at 淘汰
    最旧条目并清理超龄条目，防止缓存无限增长。

安全约束：
  - **缓存绝不存储 API Key**：cache_key 只由 model + messages + params 派生，
    绝不含任何凭证；入库的 response_json 亦仅为模型返回的补全内容。调用方
    传入 make_key 的 params 也只应包含 temperature / max_tokens 等采样参数。
"""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

from . import database
from ..utils.logging_setup import get_logger

logger = get_logger("promptforge.data.llm_cache")

# 建表 DDL：CREATE TABLE IF NOT EXISTS 保证幂等，可反复执行。
_DDL = """
CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key     TEXT PRIMARY KEY,
    response_json TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    hits          INTEGER NOT NULL DEFAULT 0,
    ttl_seconds   INTEGER NOT NULL DEFAULT 0
)
"""

# make_key 归一化时需要剔除的「无关键」：这些字段不影响补全内容，
# 若纳入键会导致本可复用的响应被当成不同请求而漏命中。
_NON_CRITICAL_PARAMS = {"stream", "stream_options", "user", "metadata"}

_TIME_FMT = "%Y-%m-%d %H:%M:%S"


def _now_iso():
    return datetime.now().strftime(_TIME_FMT)


def _connect():
    """取一条数据库连接：优先复用数据层的 _conn()（自动跟随线程工作区），
    该函数不可用时退回 sqlite3.connect(database._db_path())。"""
    conn_fn = getattr(database, "_conn", None)
    if callable(conn_fn):
        return conn_fn()
    return sqlite3.connect(database._db_path())


@contextmanager
def _cursor():
    """确保 schema 的连接 + 游标上下文：进入即幂等建表，退出即提交并关闭。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(_DDL)
        yield cur
        conn.commit()
    finally:
        conn.close()


def ensure_schema():
    """幂等创建 llm_cache 表（沿用数据层的线程工作区绑定）。

    多用户 Web 版每个工作区一套库，测试夹具亦会删库重建；因此本函数采用
    CREATE TABLE IF NOT EXISTS，可在任意库路径上反复安全执行，且每个公开
    操作执行前都会先调用它，保证旧库升级 / 新工作区首次使用均无感。"""
    with _cursor() as cur:
        cur.execute("SELECT 1 FROM llm_cache LIMIT 1")


# 说明：不做「模块导入即建表」。数据根目录可能在本模块导入时尚未创建
# （Web 版由 users 模块在建号时才初始化），导入期建表必然失败并打出误导性
# 警告。_cursor() 每次操作都会执行幂等 DDL，schema 由首次使用时自然建立，
# 因此这里刻意保持导入期零副作用。


def make_key(model, messages, params):
    """由 (model, messages, params) 派生 64 位十六进制缓存键。

    对剔除无关键后的 params 与 messages 做规范化 JSON（sort_keys 保证键序
    稳定、ensure_ascii=False 保留中文可读性）后取 sha256。相同的逻辑请求
    必然得到相同键；params 里的 stream 等字段不参与哈希。"""
    norm = {k: v for k, v in (params or {}).items() if k not in _NON_CRITICAL_PARAMS}
    payload = json.dumps(
        {"model": model, "messages": messages, "params": norm},
        sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _expired(created_at, ttl_seconds):
    """判断条目是否已过期：ttl_seconds<=0 视为永不过期；created_at 解析失败
    时保守按「未过期」处理（宁可复用也不误删）。"""
    if not ttl_seconds or ttl_seconds <= 0:
        return False
    try:
        created = datetime.strptime(created_at, _TIME_FMT)
    except (ValueError, TypeError):
        return False
    return (datetime.now() - created).total_seconds() > ttl_seconds


def get(key):
    """命中且未过期：hits+1 并返回 dict；未命中或已过期返回 None（过期条目顺手删）。"""
    with _cursor() as cur:
        row = cur.execute(
            "SELECT response_json, created_at, ttl_seconds FROM llm_cache WHERE cache_key=?",
            (key,)).fetchone()
        if row is None:
            return None
        response_json, created_at, ttl_seconds = row
        if _expired(created_at, ttl_seconds):
            cur.execute("DELETE FROM llm_cache WHERE cache_key=?", (key,))
            return None
        cur.execute("UPDATE llm_cache SET hits=hits+1 WHERE cache_key=?", (key,))
    try:
        return json.loads(response_json)
    except ValueError:
        return None


def put(key, response, ttl_seconds=0):
    """写入/刷新一条缓存（INSERT OR REPLACE，命中同键即整体替换、hits 归零）。"""
    payload = json.dumps(response, ensure_ascii=False)
    with _cursor() as cur:
        cur.execute(
            "INSERT OR REPLACE INTO llm_cache "
            "(cache_key, response_json, created_at, hits, ttl_seconds) VALUES (?,?,?,?,?)",
            (key, payload, _now_iso(), 0, int(ttl_seconds or 0)))


def stats():
    """返回 {"entries": n, "hits": n, "bytes": n}；bytes 为 response_json 总长度。"""
    with _cursor() as cur:
        row = cur.execute(
            "SELECT COUNT(*), COALESCE(SUM(hits),0), COALESCE(SUM(LENGTH(response_json)),0) "
            "FROM llm_cache").fetchone()
    return {"entries": int(row[0] or 0), "hits": int(row[1] or 0), "bytes": int(row[2] or 0)}


def clear():
    """删除全部缓存条目，返回删除行数。"""
    with _cursor() as cur:
        n = cur.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0]
        cur.execute("DELETE FROM llm_cache")
    return int(n or 0)


def prune(max_entries=2000, max_age_days=30):
    """防无限增长：先删超龄（created_at 早于 max_age_days）条目，再按 created_at
    淘汰最旧以使总量回落到 max_entries 以内。返回删除行数。"""
    removed = 0
    with _cursor() as cur:
        if max_age_days and max_age_days > 0:
            cutoff = (datetime.now() - timedelta(days=max_age_days)).strftime(_TIME_FMT)
            cur.execute("DELETE FROM llm_cache WHERE created_at < ?", (cutoff,))
            removed += cur.rowcount or 0
        if max_entries and max_entries > 0:
            total = cur.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0]
            if total > max_entries:
                cur.execute(
                    "DELETE FROM llm_cache WHERE cache_key IN "
                    "(SELECT cache_key FROM llm_cache ORDER BY created_at ASC LIMIT ?)",
                    (total - max_entries,))
                removed += cur.rowcount or 0
    return int(removed)
