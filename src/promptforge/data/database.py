#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Project: 提示词工坊项目
# Copyright (C) 2026 智维ITMSKY
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
数据层（SQLite 持久化）。

职责：
  - 初始化数据库（配置表 / 业务记录表 / 清空审计表），并对旧列做兼容迁移。
  - 配置与业务数据分表：清空历史只动业务表，连接配置始终保留。
  - 原子备份：使用 VACUUM INTO 生成一致性快照 + 写 .sha256 校验和 + 按留存数清理旧备份。
  - 统计查询：按日期 / 厂商 / 成败 / 耗时 / token 维度聚合，支撑分析与展示。
  - 一次性迁移：若检测到旧版数据库 / 旧版 JSON，平滑迁到新结构。

本模块不依赖任何 GUI / 第三方网络库，仅使用标准库；所有对 API Key 的写库
操作均不向日志输出明文（调用方亦需遵守）。
"""

import os
import sys
import json
import sqlite3
import shutil
import hashlib
import threading
from contextlib import contextmanager
from datetime import datetime

from ..utils.logging_setup import get_logger

logger = get_logger("promptforge.data.database")

# =====================================================================
# 路径与常量
# =====================================================================
# 数据目录：开发态位于项目根目录下的 data/；PyInstaller 单文件模式（frozen）
# 下 sys.executable 为 exe 真实路径，数据落在同目录 data/，保证持久化不丢失。
if getattr(sys, "frozen", False):
    _APP_DIR = os.path.dirname(sys.executable)
else:
    # src 布局：database.py 位于 src/promptforge/data/ 下，向上 4 级到项目根
    _APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
BASE_DIR = os.path.join(_APP_DIR, "data")
DB_PATH = os.path.join(BASE_DIR, "promptforge.db")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
DISPLAY_LIMIT = 100              # 历史档案页展示上限（数据库本身不截断，便于统计）
BACKUP_RETAIN = 30              # 备份留存份数上限

DEFAULT_CONFIG = {
    "provider": "DeepSeek",
    "api_key": "",
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "temperature": "0.7",
    "max_tokens": "1024",
    "style": "balanced",
}


# =====================================================================
# 测试钩子 / 路径配置
# =====================================================================
def set_base_dir(path):
    """覆盖数据目录（测试或自定义部署时使用）；随后需重新调用 init_db()。"""
    global BASE_DIR, DB_PATH, BACKUP_DIR
    BASE_DIR = path
    DB_PATH = os.path.join(BASE_DIR, "promptforge.db")
    BACKUP_DIR = os.path.join(BASE_DIR, "backups")


# =====================================================================
# 多用户工作区 (Web 多用户体系)
# =====================================================================
# Web 版每个登录账号拥有独立数据目录 (data/users/<username>/), 各自一套
# promptforge.db / backups。waitress 以线程池并发处理请求, 因此工作区绑定
# 必须是**线程局部**的: 请求处理期间 use_workspace() 覆盖本线程路径,
# 退出即恢复, 线程之间互不影响 (桌面版与默认全局路径行为完全不变)。
_tls = threading.local()


def _base_dir():
    return getattr(_tls, "base_dir", None) or BASE_DIR


def _db_path():
    return getattr(_tls, "db_path", None) or DB_PATH


def _backup_dir():
    return getattr(_tls, "backup_dir", None) or BACKUP_DIR


@contextmanager
def use_workspace(base_dir):
    """在本线程内把数据层绑定到指定工作区目录 (嵌套安全, 退出自动恢复)。"""
    prev = (
        getattr(_tls, "db_path", None),
        getattr(_tls, "base_dir", None),
        getattr(_tls, "backup_dir", None),
    )
    _tls.base_dir = base_dir
    _tls.db_path = os.path.join(base_dir, "promptforge.db")
    _tls.backup_dir = os.path.join(base_dir, "backups")
    try:
        yield base_dir
    finally:
        _tls.db_path, _tls.base_dir, _tls.backup_dir = prev


def set_workspace(base_dir):
    """直接绑定/解绑当前线程工作区 (None=恢复默认全局路径)。

    Flask 每请求钩子用 (before_request 绑定登录用户工作区, teardown 解绑)。
    需要**临时**切到别的用户工作区再切回 (如管理员代管) 时, 请优先用
    use_workspace() 上下文管理器, 它保证嵌套与恢复。"""
    if base_dir is None:
        _tls.db_path = _tls.base_dir = _tls.backup_dir = None
    else:
        _tls.base_dir = base_dir
        _tls.db_path = os.path.join(base_dir, "promptforge.db")
        _tls.backup_dir = os.path.join(base_dir, "backups")


def get_db_path():
    return _db_path()


def _current_username():
    """从线程局部工作区反解当前登录用户名（复用既有 _base_dir() 取值方式，不新造轮子）。

    多用户态工作区形如 <BASE_DIR>/users/<username>（见 users.workspace_dir）；
    默认/桌面态工作区即 BASE_DIR 本身（不在 users/ 之下）→ 返回 None，表示
    "无当前用户上下文"。用户名来自已通过 create_user 同款正则校验的账号，此处
    仅按路径反解，不引入新的用户状态存储。"""
    base = os.path.normpath(_base_dir())
    users_root = os.path.normpath(os.path.join(BASE_DIR, "users"))
    prefix = users_root + os.sep
    if base.startswith(prefix):
        name = base[len(prefix):].split(os.sep)[0]
        return name or None
    return None


def record_scope_sql(alias=""):
    """行级可见域（用户归属）SQL 片段构造器，返回 (sql_fragment, params)。

    借鉴 aircrushin/promptMinder buildPromptVersionScope。**供 Web 层与后续模块
    统一使用，从结构上消除每接口各写一遍的越权风险**：把"当前用户只能看自己的行"
    这一规则收口到一处，避免各处手拼 user_id 条件、漏做参数化。

    当前用户的取值**复用既有 _current_username()**（线程局部工作区反解登录名，
    与 Web 层工作区绑定同一套机制），不新造轮子、不引入新的用户状态存储。
      - 有当前用户上下文 → (" AND {alias}user_id=?", (uid,))；alias 仅为列名前缀
        （调用方自带点），uid 一律以绑定参数传入，绝不 f-string 拼接用户输入；
      - 无当前用户上下文（默认/桌面态，或工作区未落在 users/ 下）→ ("", ())，
        即不追加任何条件，保持既有查询行为不变。

    alias 用法：无表别名传 ""（→ user_id）；FTS JOIN 里 enhance_records 别名 r
    的场景请改用 history_scope_sql("r")。

    注意：当前架构采用"每用户独立工作区库"隔离，enhance_records 并无 user_id 列，
    行级隔离靠选库完成；本构造器为将来查询带 user_id 的共享表（如 api_usage）或
    引入 user_id 列时预留，现阶段不接入既有 enhance_records 查询，故不影响任何
    现有行为。"""
    uid = _current_username()
    if not uid:
        return "", ()
    return " AND %suser_id=?" % alias, (uid,)


def history_scope_sql(alias="r"):
    """record_scope_sql 的 FTS JOIN 版（供 Web 层与后续模块统一使用）。

    search_records 的 fts 分支把 enhance_records 别名成 r，故 alias 传裸名、
    由本函数补点（"alias 前缀带点"）→ " AND r.user_id=?"。其余语义（复用
    _current_username、参数化、无用户上下文返回 ("", ())）与 record_scope_sql
    完全一致。"""
    prefix = ("%s." % alias) if alias else ""
    return record_scope_sql(prefix)


def _now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _conn():
    conn = sqlite3.connect(_db_path())
    try:
        conn.execute("PRAGMA journal_mode=WAL")      # 写前日志，提升并发读写性能
        conn.execute("PRAGMA foreign_keys=ON")
    except sqlite3.Error:
        pass
    return conn


# =====================================================================
# 初始化与迁移
# =====================================================================
def init_db():
    """建表、建索引、补齐旧列，并在首次运行时迁移旧数据。"""
    os.makedirs(_base_dir(), exist_ok=True)
    conn = _conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS app_config (
            id          INTEGER PRIMARY KEY CHECK (id = 1),
            provider    TEXT,
            api_key     TEXT,
            base_url    TEXT,
            model       TEXT,
            updated_at  TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS enhance_records (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at      TEXT,
            date            TEXT,
            provider        TEXT,
            model           TEXT,
            base_url        TEXT,
            input_text      TEXT,
            output_text     TEXT,
            input_chars     INTEGER,
            output_chars    INTEGER,
            status          TEXT,
            error_msg       TEXT,
            duration_ms     INTEGER,
            source          TEXT,
            prompt_tokens   INTEGER,
            completion_tokens INTEGER,
            request_id      TEXT,
            quality         TEXT,
            style           TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS clear_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            cleared_at      TEXT,
            backup_path     TEXT,
            records_removed INTEGER,
            reason          TEXT
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_rec_created  ON enhance_records(created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_rec_date     ON enhance_records(date)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_rec_provider ON enhance_records(provider)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_rec_status   ON enhance_records(status)")
    conn.commit()

    _migrate_columns(cur, conn)
    conn.commit()
    conn.close()

    # 旧数据迁移仅在「默认数据目录」下进行（测试或自定义目录时跳过，避免误搬迁）。
    # 多用户工作区 (users/<name>) 同样跳过: 每个新工作区从空库开始, 不得吞并旧全局数据。
    if _base_dir() == os.path.join(_APP_DIR, "data"):
        _migrate_legacy_db()
        _migrate_legacy_json()

    # 全文检索：库重建/新建后同步检索设施（缓存里的旧状态一并失效，
    # 覆盖测试夹具删库重建等场景）。
    _fts_modes.pop(_db_path(), None)
    ensure_search_index()

    # 版本链（F2）：新建库随 init_db 一次建齐；旧库由 ensure_version_schema 惰性补齐。
    _version_ensured.discard(_db_path())   # 测试夹具可能删库重建，缓存必须失效
    ensure_version_schema()


# =====================================================================
# 提示词版本链（F2）表结构与惰性迁移
# =====================================================================
# 设计取舍（对照 langfuse/PromptWorks/pezzo 调研结论，按本项目实际结构调整）：
#   - 本项目没有独立 prompts 表：历史记录 enhance_records 就是提示词的锚点，
#     版本链挂在 record_id 上，删记录级联删版本（版本不单独删，防断链）。
#   - 账号在独立的全局库 pf_users，工作区库无 users 表 → created_by 存用户名
#     （TEXT），不设跨库 FK。
#   - 回滚采用 PromptWorks 指针式：enhance_records.current_version_id 为普通列
#     （无 FK），激活版本只 UPDATE 指针、不产生新行，链不断。刻意避开
#     "两表互指 FK"——SQLite 不支持 ALTER ADD CONSTRAINT，照抄必在建表期卡死。
#   - 版本行不可变：任何修改=插新行，version_no=MAX+1，UNIQUE(record_id,version_no)
#     兜并发撞号（撞号由上层捕获 IntegrityError 重试一次，再失败按冲突返回）。
_VERSION_DDL = [
    """
    CREATE TABLE IF NOT EXISTS prompt_versions (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        record_id      INTEGER NOT NULL REFERENCES enhance_records(id) ON DELETE CASCADE,
        version_no     INTEGER NOT NULL,
        parent_id      INTEGER REFERENCES prompt_versions(id) ON DELETE SET NULL,
        source         TEXT NOT NULL DEFAULT 'manual',
        slot_values    TEXT NOT NULL DEFAULT '{}',
        content        TEXT NOT NULL,
        model          TEXT,
        commit_message TEXT NOT NULL,
        score          TEXT,
        created_by     TEXT NOT NULL DEFAULT '',
        created_at     TEXT NOT NULL,
        UNIQUE (record_id, version_no)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pv_record ON prompt_versions(record_id, version_no)",
    """
    CREATE TABLE IF NOT EXISTS version_audit (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        record_id  INTEGER NOT NULL,
        version_id INTEGER,
        action     TEXT NOT NULL,
        actor      TEXT NOT NULL DEFAULT '',
        detail     TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_va_record ON version_audit(record_id)",
]

_version_ensured = set()       # 已确认含版本链结构的库路径（与 _fts_modes 同型缓存）
_version_lock = threading.Lock()


def ensure_version_schema():
    """确保当前库具备版本链结构（幂等，每库路径只真正执行一次）。

    新建库由 init_db 覆盖；存量工作区库因 ensure_workspace 以文件存在性跳过
    init_db，故版本链读写入口（load_records/search_records/get_record）都会
    先调本函数惰性补齐，保证旧库升级无感。"""
    path = _db_path()
    if path in _version_ensured:
        return
    with _version_lock:
        if path in _version_ensured:
            return
        conn = _conn()
        try:
            for ddl in _VERSION_DDL:
                conn.execute(ddl)
            if not _column_exists(conn.cursor(), "enhance_records", "current_version_id"):
                conn.execute("ALTER TABLE enhance_records ADD COLUMN current_version_id INTEGER")
            # 历史收藏（P1-1，2026-11-08 三轮迭代）：惰性补列，默认 0，旧库无感
            if not _column_exists(conn.cursor(), "enhance_records", "favorite"):
                conn.execute("ALTER TABLE enhance_records ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        finally:
            conn.close()
        _version_ensured.add(path)


def _column_exists(cur, table, column):
    rows = cur.execute("PRAGMA table_info(%s)" % table).fetchall()
    return any(r[1] == column for r in rows)


def _migrate_columns(cur, conn):
    for col in ("temperature", "max_tokens", "style"):
        if not _column_exists(cur, "app_config", col):
            cur.execute("ALTER TABLE app_config ADD COLUMN %s TEXT" % col)
    for col, ctype in (
        ("prompt_tokens", "INTEGER"),
        ("completion_tokens", "INTEGER"),
        ("request_id", "TEXT"),
        ("quality", "TEXT"),
        ("style", "TEXT"),
    ):
        if not _column_exists(cur, "enhance_records", col):
            cur.execute("ALTER TABLE enhance_records ADD COLUMN %s %s" % (col, ctype))


def _migrate_legacy_db():
    """若项目根存在旧版 prompt_enhancer.db 且新库尚未建立，则整库复制迁移。"""
    legacy = os.path.join(_APP_DIR, "prompt_enhancer.db")
    dbp = _db_path()
    if os.path.exists(legacy) and not os.path.exists(dbp):
        try:
            shutil.copyfile(legacy, dbp)
            logger.info("已迁移旧版数据库: %s -> %s", legacy, dbp)
        except Exception as e:
            logger.warning("旧版数据库迁移失败: %s", e)


def _migrate_legacy_json():
    """把旧版 JSON 配置/历史（若存在且库为空）一次性导入数据库。"""
    conn = _conn()
    cur = conn.cursor()
    has_cfg = cur.execute("SELECT COUNT(*) FROM app_config").fetchone()[0] > 0
    has_rec = cur.execute("SELECT COUNT(*) FROM enhance_records").fetchone()[0] > 0
    # 若已通过旧版 db 迁移获得数据，则跳过 JSON 迁移
    if has_cfg or has_rec:
        conn.close()
        return
    old_cfg = os.path.join(_APP_DIR, "prompt_enhancer_config.json")
    if os.path.exists(old_cfg):
        try:
            with open(old_cfg, "r", encoding="utf-8") as f:
                c = json.load(f)
            cur.execute(
                "INSERT INTO app_config (id, provider, api_key, base_url, model, updated_at) "
                "VALUES (1,?,?,?,?,?)",
                (c.get("provider", "DeepSeek"), c.get("api_key", ""),
                 c.get("base_url", ""), c.get("model", ""), _now_iso()))
            conn.commit()
            os.rename(old_cfg, old_cfg + ".migrated")
            logger.info("已迁移旧版配置 JSON")
        except Exception:
            conn.rollback()
    old_hist = os.path.join(_APP_DIR, "prompt_enhancer_history.json")
    if os.path.exists(old_hist):
        try:
            with open(old_hist, "r", encoding="utf-8") as f:
                data = json.load(f)
            for it in data:
                cur.execute(
                    "INSERT INTO enhance_records "
                    "(created_at,date,provider,model,input_text,output_text,status,source) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (it.get("time", _now_iso()), (it.get("time", "") or "")[:10],
                     it.get("provider", ""), "", it.get("input", ""),
                     it.get("output", ""), "success", "migrated"))
            conn.commit()
            os.rename(old_hist, old_hist + ".migrated")
            logger.info("已迁移旧版历史 JSON")
        except Exception:
            conn.rollback()
    conn.close()


# =====================================================================
# 配置读写
# =====================================================================
def load_config():
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT provider, api_key, base_url, model, temperature, max_tokens, style "
            "FROM app_config WHERE id=1")
        row = cur.fetchone()
        conn.close()
        if row:
            return {
                "provider": row[0] or DEFAULT_CONFIG["provider"],
                "api_key": row[1] or "",
                "base_url": row[2] or DEFAULT_CONFIG["base_url"],
                "model": row[3] or DEFAULT_CONFIG["model"],
                "temperature": (row[4] or DEFAULT_CONFIG["temperature"]),
                "max_tokens": (row[5] or DEFAULT_CONFIG["max_tokens"]),
                "style": (row[6] or DEFAULT_CONFIG["style"]),
            }
    except Exception as e:
        logger.warning("读取配置失败，使用默认配置: %s", e)
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO app_config "
        "(id, provider, api_key, base_url, model, temperature, max_tokens, style, updated_at) "
        "VALUES (1,?,?,?,?,?,?,?,?)",
        (cfg.get("provider", "DeepSeek"), cfg.get("api_key", ""),
         cfg.get("base_url", ""), cfg.get("model", ""),
         str(cfg.get("temperature", "0.7")), str(cfg.get("max_tokens", "1024")),
         cfg.get("style", "balanced"), _now_iso()))
    conn.commit()
    conn.close()
    logger.info("配置已保存（provider=%s）", cfg.get("provider"))


# =====================================================================
# 业务记录写入
# =====================================================================
def append_record(created_at, provider, model, base_url, input_text, output_text,
                  status, error_msg="", duration_ms=None, source="single",
                  prompt_tokens=None, completion_tokens=None, request_id=None,
                  quality="", style=""):
    """写入一条业务记录，返回新行 id（Web 层用它把增强结果固化为版本链锚点）。"""
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO enhance_records "
        "(created_at,date,provider,model,base_url,input_text,output_text,"
        "input_chars,output_chars,status,error_msg,duration_ms,source,"
        "prompt_tokens,completion_tokens,request_id,quality,style) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (created_at, created_at[:10], provider, model, base_url, input_text, output_text,
         len(input_text), len(output_text), status, error_msg, duration_ms, source,
         prompt_tokens, completion_tokens, request_id, quality, style))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def _record_dicts(rows):
    """enhance_records 查询行 → 前端字典（load_records / search_records 共用）。"""
    return [{
        "id": r[0], "time": r[1], "provider": r[2], "model": r[3],
        "input": r[4], "output": r[5], "status": r[6], "duration_ms": r[7],
        "quality": r[8] or "", "style": r[9] or "", "prompt_tokens": r[10],
        "completion_tokens": r[11], "request_id": r[12] or "",
        "current_version_id": r[13],
        "favorite": bool(r[14] or 0),
    } for r in rows]


_RECORD_COLS = ("id, created_at, provider, model, input_text, output_text, status, "
                "duration_ms, quality, style, prompt_tokens, completion_tokens, "
                "request_id, current_version_id, favorite")


def get_record(record_id):
    """按 id 取单条历史记录（版本链接口用），返回结构与 load_records 一致。

    先行惰性迁移：旧工作区库缺 current_version_id 列时，直接按 id 访问的
    端点（versions/diff/evaluate/管理员代管）不经过 /api/history 列表自愈，
    必须在此补齐否则 SELECT 抛 no such column。"""
    ensure_version_schema()
    conn = _conn()
    try:
        row = conn.execute("SELECT %s FROM enhance_records WHERE id=?"
                           % _RECORD_COLS, (record_id,)).fetchone()
    finally:
        conn.close()
    return _record_dicts([row])[0] if row else None


def load_records(limit=None, favorites=None):
    ensure_version_schema()      # 旧工作区库惰性补列后再 SELECT current_version_id
    conn = _conn()
    cur = conn.cursor()
    where = " WHERE favorite=1" if favorites else ""
    if limit:
        cur.execute(
            "SELECT %s FROM enhance_records%s ORDER BY id DESC LIMIT ?" % (_RECORD_COLS, where),
            (limit,))
    else:
        cur.execute("SELECT %s FROM enhance_records%s ORDER BY id DESC" % (_RECORD_COLS, where))
    rows = cur.fetchall()
    conn.close()
    return _record_dicts(rows)


def toggle_favorite(record_id):
    """切换某条历史记录的收藏状态（P1-1）。

    返回切换后的收藏状态；记录不存在返回 None。收藏是非关键路径，
    异常统一上抛由调用方按 500 处理，便于定位。"""
    ensure_version_schema()
    conn = _conn()
    try:
        row = conn.execute("SELECT favorite FROM enhance_records WHERE id=?",
                           (record_id,)).fetchone()
        if row is None:
            return None
        new = 0 if int(row[0] or 0) else 1
        conn.execute("UPDATE enhance_records SET favorite=? WHERE id=?", (new, record_id))
        conn.commit()
        return bool(new)
    finally:
        conn.close()


# =====================================================================
# 历史档案全文检索
# =====================================================================
# 优先 SQLite FTS5 + trigram 分词（支持中文子串匹配，需 sqlite >= 3.34）；
# 编译选项不带 fts5 或 trigram 时自动回退 LIKE 全表扫描。
# hist_fts 为 external-content 索引虚表（不复制正文、省空间），由三个触发器
# 随 enhance_records 的增/删/改保持同步，清空历史无需额外清理代码；
# 旧库首次启用或行数对不上（如恢复备份）时整体 rebuild 回填。

_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS hist_fts USING fts5(
    input_text, output_text,
    content='enhance_records', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS hist_fts_ai AFTER INSERT ON enhance_records BEGIN
    INSERT INTO hist_fts(rowid, input_text, output_text)
    VALUES (new.id, new.input_text, new.output_text);
END;
CREATE TRIGGER IF NOT EXISTS hist_fts_ad AFTER DELETE ON enhance_records BEGIN
    INSERT INTO hist_fts(hist_fts, rowid, input_text, output_text)
    VALUES ('delete', old.id, old.input_text, old.output_text);
END;
CREATE TRIGGER IF NOT EXISTS hist_fts_au AFTER UPDATE ON enhance_records BEGIN
    INSERT INTO hist_fts(hist_fts, rowid, input_text, output_text)
    VALUES ('delete', old.id, old.input_text, old.output_text);
    INSERT INTO hist_fts(rowid, input_text, output_text)
    VALUES (new.id, new.input_text, new.output_text);
END;
"""

_fts_lock = threading.Lock()
_fts_modes = {}          # db_path -> "fts" | "like"（每库只探测/建索引一次）


def _probe_fts_trigram(conn):
    """探测当前 sqlite 是否同时支持 FTS5 与 trigram 分词器。"""
    try:
        conn.execute("SELECT fts5(?1)", ("a",)).fetchone()
        conn.execute("CREATE VIRTUAL TABLE temp._fts_probe USING fts5(t, tokenize='trigram')")
        conn.execute("DROP TABLE temp._fts_probe")
        return True
    except sqlite3.Error:
        return False


def ensure_search_index():
    """确保当前库检索设施就绪，返回实际生效模式："fts" 或 "like"。

    like 模式不建任何对象；fts 模式幂等建虚表+触发器，行数与基表不一致时
    整体 rebuild。任何异常都降级为 like，绝不阻断搜索功能。"""
    path = _db_path()
    mode = _fts_modes.get(path)
    if mode:
        return mode
    with _fts_lock:
        mode = _fts_modes.get(path)
        if mode:
            return mode
        conn = _conn()
        try:
            if _probe_fts_trigram(conn):
                conn.executescript(_FTS_DDL)
                n_base = conn.execute("SELECT COUNT(*) FROM enhance_records").fetchone()[0]
                n_idx = conn.execute("SELECT COUNT(*) FROM hist_fts").fetchone()[0]
                if n_base != n_idx:
                    conn.execute("INSERT INTO hist_fts(hist_fts) VALUES('rebuild')")
                conn.commit()
                mode = "fts"
            else:
                mode = "like"
        except sqlite3.Error:
            mode = "like"
        finally:
            conn.close()
        _fts_modes[path] = mode
        return mode


def search_records(keyword, limit=None, favorites=None):
    """按关键字检索历史（input_text/output_text 子串匹配），返回结构与
    load_records 完全一致，按 id 倒序。

    fts 模式下 trigram 需 >= 3 字符才能命中，更短的关键字自动退 LIKE；
    like 模式对 %/_ 做转义，避免通配符误匹配。"""
    ensure_version_schema()      # 同 load_records：先保证 current_version_id 列就绪
    kw = (keyword or "").strip()
    if not kw:
        return load_records(limit, favorites=favorites)
    mode = ensure_search_index()
    like = "%" + kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    fav = " AND favorite=1" if favorites else ""
    fav_fts = " AND r.favorite=1" if favorites else ""
    tail = " ORDER BY id DESC LIMIT ?" if limit else " ORDER BY id DESC"
    sql_like = ("SELECT %s FROM enhance_records "
                "WHERE (input_text LIKE ? ESCAPE '\\' OR output_text LIKE ? ESCAPE '\\')%s"
                "%s") % (_RECORD_COLS, fav, tail)
    sql_fts = ("SELECT %s FROM enhance_records r "
               "JOIN hist_fts f ON f.rowid = r.id WHERE f MATCH ?%s"
               "%s") % (", ".join("r." + c.strip() for c in _RECORD_COLS.split(",")),
                        fav_fts,
                        " ORDER BY r.id DESC LIMIT ?" if limit else " ORDER BY r.id DESC")
    conn = _conn()
    try:
        if mode == "fts" and len(kw) >= 3:
            # 双引号包成短语查询：关键字里的 FTS 语法字符不再被解释
            args = ('"%s"' % kw.replace('"', '""'),)
            if limit:
                args = args + (limit,)
            try:
                cur = conn.execute(sql_fts, args)
                rows = cur.fetchall()
            except sqlite3.OperationalError:
                # 索引虚表意外缺失/损坏 → 当次退回 LIKE，保证可用性
                args = (like, like) + ((limit,) if limit else ())
                rows = conn.execute(sql_like, args).fetchall()
        else:
            args = (like, like) + ((limit,) if limit else ())
            rows = conn.execute(sql_like, args).fetchall()
    finally:
        conn.close()
    return _record_dicts(rows)


# =====================================================================
# 原子备份（VACUUM INTO + 校验和 + 留存清理）
# =====================================================================
class BackupError(Exception):
    pass


def backup_database():
    """生成一致性数据库快照（VACUUM INTO），写 .sha256 校验和，并清理旧备份。
    返回备份文件路径；失败时抛出 BackupError。"""
    backup_dir = _backup_dir()
    try:
        os.makedirs(backup_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d")
        base = os.path.join(backup_dir, "promptforge_%s" % stamp)
        target = base + ".db"
        n = 1
        while os.path.exists(target):
            # 同日多次清空：递增序号避免覆盖上一次备份
            target = "%s_%d.db" % (base, n)
            n += 1

        # VACUUM INTO 生成一致、独立的副本（目标文件不可已存在，循环已保证唯一）。
        # 注意：filename 必须是字符串字面量，不能绑定参数；且需对单引号转义。
        safe_target = target.replace("'", "''")
        src = _conn()
        try:
            src.execute("VACUUM INTO '%s'" % safe_target)
        finally:
            src.close()

        if not os.path.exists(target) or os.path.getsize(target) == 0:
            raise BackupError("备份文件生成失败或为空")

        sha = _sha256_file(target)
        with open(target + ".sha256", "w", encoding="utf-8") as f:
            f.write("%s  %s\n" % (sha, os.path.basename(target)))

        _prune_backups()
        logger.info("数据库已备份: %s", target)
        return target
    except BackupError:
        raise
    except Exception as e:
        logger.error("备份失败: %s", e)
        raise BackupError("备份失败: %s" % e)


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _prune_backups():
    """仅保留最近 BACKUP_RETAIN 份 .db 备份及其校验和。"""
    backup_dir = _backup_dir()
    try:
        files = [f for f in os.listdir(backup_dir) if f.endswith(".db")]
        files.sort(key=lambda f: os.path.getmtime(os.path.join(backup_dir, f)), reverse=True)
        for old in files[BACKUP_RETAIN:]:
            try:
                os.remove(os.path.join(backup_dir, old))
                sha = os.path.join(backup_dir, old + ".sha256")
                if os.path.exists(sha):
                    os.remove(sha)
            except OSError:
                pass
    except Exception:
        pass


# =====================================================================
# 清空业务数据 + 审计（配置始终保留）
# =====================================================================
def clear_records():
    """仅清空业务数据（enhance_records），保留 app_config 配置。返回删除条数。"""
    conn = _conn()
    cur = conn.cursor()
    n = cur.execute("SELECT COUNT(*) FROM enhance_records").fetchone()[0]
    cur.execute("DELETE FROM enhance_records")
    conn.commit()
    conn.close()
    logger.info("已清空 %d 条增强记录（配置保留）", n)
    return n


def log_clear(backup_path, removed, reason=""):
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO clear_log (cleared_at, backup_path, records_removed, reason) "
        "VALUES (?,?,?,?)",
        (_now_iso(), backup_path or "", removed, reason))
    conn.commit()
    conn.close()


def get_clear_logs(limit=20):
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT cleared_at, backup_path, records_removed, reason "
        "FROM clear_log ORDER BY id DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return [{"cleared_at": r[0], "backup_path": r[1], "records_removed": r[2], "reason": r[3]}
            for r in rows]


# =====================================================================
# 统计分析
# =====================================================================
def get_stats():
    conn = _conn()
    cur = conn.cursor()
    overall = cur.execute(
        "SELECT COUNT(*), "
        "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END), "
        "COALESCE(AVG(duration_ms),0), "
        "COALESCE(SUM(prompt_tokens),0), "
        "COALESCE(SUM(completion_tokens),0), "
        "SUM(CASE WHEN quality='low_quality' THEN 1 ELSE 0 END) "
        "FROM enhance_records").fetchone()
    by_date = cur.execute(
        "SELECT date, COUNT(*), "
        "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) "
        "FROM enhance_records GROUP BY date ORDER BY date DESC LIMIT 30").fetchall()
    by_provider = cur.execute(
        "SELECT provider, COUNT(*) FROM enhance_records "
        "GROUP BY provider ORDER BY COUNT(*) DESC").fetchall()
    today = datetime.now().strftime("%Y-%m-%d")
    today_count = cur.execute(
        "SELECT COUNT(*) FROM enhance_records WHERE date=?", (today,)).fetchone()[0]
    conn.close()
    return {
        "total": overall[0],
        "success": overall[1],
        "failed": overall[2],
        "avg_duration_ms": round(overall[3] or 0),
        "total_prompt_tokens": overall[4] or 0,
        "total_completion_tokens": overall[5] or 0,
        "low_quality": overall[6] or 0,
        "today": today_count,
        "by_date": [{"date": r[0], "total": r[1], "success": r[2], "failed": r[3]} for r in by_date],
        "by_provider": [{"provider": r[0], "count": r[1]} for r in by_provider],
    }


if __name__ == "__main__":
    init_db()
    print("database ok, db =", DB_PATH)
