#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Project: 提示词工坊项目
# Copyright (C) 2026 智维ITMSKY
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Token 用量与成本统计（Web 正式版）。

设计：
  - 明细表 api_usage 放**全局库**（data/promptforge.db，与 pf_users 同库）：
    用量天然要跨用户聚合（管理员看全员汇总），放每用户工作区则要遍历 N 个
    库；行内 user_id 冗余存用户名，本人/管理员查询都在这一个表上完成，
    且登录前建表不受工作区绑定影响。
  - 每次增强成功（单条 / 批量每条）写一行；模型响应缺失 usage 时按 0 记，
    不报错（与 core.engine 的宽松解析同口径）。
  - cost 可空：仅当管理员在 pf_settings.usage_prices 配置了该模型单价
    （单位：元/百万 tokens，JSON：{"模型名": {"prompt": x, "completion": y}}）
    才按调用量折算；不写死任何单价表，未配置的模型 cost 为 NULL。

本模块仅用标准库；连接始终指向全局库路径，不随线程工作区切换。
"""

import json
import sqlite3
import threading
from datetime import datetime

from ..utils.logging_setup import get_logger
from . import database as db
from . import users

logger = get_logger("promptforge.data.usage")

PRICES_SETTING_KEY = "usage_prices"   # pf_settings 键：模型单价配置 JSON

_DDL = """
CREATE TABLE IF NOT EXISTS api_usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           TEXT NOT NULL,
    model             TEXT,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    cost              REAL,
    created_at        TEXT
)
"""

_lock = threading.Lock()


def _conn():
    """全局库连接（与 users._conn 同路径；用量与账号同库便于聚合）。"""
    conn = sqlite3.connect(db.DB_PATH)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    return conn


def init_usage_db():
    """建 api_usage 表（幂等），webapp 启动时调用。"""
    conn = _conn()
    conn.executescript(_DDL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_user_time "
                 "ON api_usage(user_id, created_at)")
    conn.commit()
    conn.close()


# =====================================================================
# 模型单价配置（管理员）
# =====================================================================
def get_prices():
    """读取模型单价配置；无配置/JSON 损坏返回 {}（cost 保持 NULL）。"""
    raw = users.get_setting(PRICES_SETTING_KEY, "")
    if not raw:
        return {}
    try:
        cfg = json.loads(raw)
        return cfg if isinstance(cfg, dict) else {}
    except (ValueError, TypeError):
        return {}


def set_prices(prices):
    """保存模型单价配置（空 dict = 清除）。结构校验由 Web 层完成。"""
    users.set_setting(PRICES_SETTING_KEY,
                      json.dumps(prices or {}, ensure_ascii=False))


def _model_cost(model, prompt_tokens, completion_tokens):
    """按配置单价折算成本；未配置/配置非法的模型返回 None。"""
    price = get_prices().get(model or "")
    if not isinstance(price, dict):
        return None
    try:
        p = float(price.get("prompt") or 0.0)
        c = float(price.get("completion") or 0.0)
    except (TypeError, ValueError):
        return None
    return round(prompt_tokens / 1e6 * p + completion_tokens / 1e6 * c, 6)


# =====================================================================
# 明细写入
# =====================================================================
def _to_int(v):
    """token 数安全整数化：模型响应缺失/非数字字符串一律折 0。"""
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def record_usage(user_id, model, prompt_tokens, completion_tokens):
    """增强成功后写一行用量明细。缺 usage 或 usage 非数字一律按 0 记；
    写失败仅告警，绝不向上抛（统计不能打断增强主流程）——因此外层兜底
    捕获所有异常，而不仅是 sqlite3.Error。"""
    try:
        pt = _to_int(prompt_tokens)
        ct = _to_int(completion_tokens)
        try:
            cost = _model_cost(model, pt, ct)
        except Exception:
            cost = None              # 单价折算意外失败 → 该行 cost 记 NULL，不丢明细
        args = (user_id, model or "", pt, ct, pt + ct, cost,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        with _lock:
            try:
                conn = _conn()
                conn.execute(
                    "INSERT INTO api_usage (user_id, model, prompt_tokens,"
                    " completion_tokens, total_tokens, cost, created_at)"
                    " VALUES (?,?,?,?,?,?,?)", args)
                conn.commit()
                conn.close()
            except sqlite3.OperationalError:
                # 表尚不存在（绕过启动流程等）→ 建表后补记一次
                init_usage_db()
                conn = _conn()
                conn.execute(
                    "INSERT INTO api_usage (user_id, model, prompt_tokens,"
                    " completion_tokens, total_tokens, cost, created_at)"
                    " VALUES (?,?,?,?,?,?,?)", args)
                conn.commit()
                conn.close()
    except sqlite3.Error as e:
        logger.warning("用量明细写入失败（不影响增强结果）: %s", e)
    except Exception as e:
        logger.warning("用量明细写入异常（不影响增强结果）: %s", e)


# =====================================================================
# 汇总查询
# =====================================================================
def usage_summary(user_id=None, days=30):
    """用量汇总。user_id=None 时聚合全员（仅管理员接口使用）。

    返回 {"summary": 总量, "by_model": 按模型, "by_day": 按日（近 days 天）}。
    cost 分组求和只累加已计价行（SUM 忽略 NULL），未配置单价时整体为 null。"""
    cond, base = (" WHERE user_id=?", [user_id]) if user_id else ("", [])
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(prompt_tokens),0),"
            " COALESCE(SUM(completion_tokens),0), COALESCE(SUM(total_tokens),0),"
            " SUM(cost) FROM api_usage" + cond, base).fetchone()
        by_model = conn.execute(
            "SELECT model, COUNT(*), COALESCE(SUM(prompt_tokens),0),"
            " COALESCE(SUM(completion_tokens),0), COALESCE(SUM(total_tokens),0),"
            " SUM(cost) FROM api_usage" + cond +
            " GROUP BY model ORDER BY SUM(total_tokens) DESC", base).fetchall()
        by_day = conn.execute(
            "SELECT substr(created_at,1,10), COUNT(*), COALESCE(SUM(prompt_tokens),0),"
            " COALESCE(SUM(completion_tokens),0), COALESCE(SUM(total_tokens),0),"
            " SUM(cost) FROM api_usage" + cond +
            " GROUP BY substr(created_at,1,10) ORDER BY 1 DESC LIMIT ?",
            base + [days]).fetchall()
    finally:
        conn.close()

    def _fmt(items, key_name):
        return [{
            key_name: r[0], "calls": r[1], "prompt_tokens": r[2],
            "completion_tokens": r[3], "total_tokens": r[4], "cost": r[5],
        } for r in items]

    return {
        "summary": {"calls": row[0], "prompt_tokens": row[1],
                    "completion_tokens": row[2], "total_tokens": row[3],
                    "cost": row[4]},
        "by_model": _fmt(by_model, "model"),
        "by_day": _fmt(by_day, "date"),
    }


def usage_by_user():
    """全员按用户聚合（管理员汇总用），按总 token 倒序。"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT user_id, COUNT(*), COALESCE(SUM(prompt_tokens),0),"
            " COALESCE(SUM(completion_tokens),0), COALESCE(SUM(total_tokens),0),"
            " SUM(cost) FROM api_usage"
            " GROUP BY user_id ORDER BY SUM(total_tokens) DESC").fetchall()
    finally:
        conn.close()
    return [{"user_id": r[0], "calls": r[1], "prompt_tokens": r[2],
             "completion_tokens": r[3], "total_tokens": r[4], "cost": r[5]}
            for r in rows]
