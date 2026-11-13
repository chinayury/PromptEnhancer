#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Project: 提示词工坊项目
# Copyright (C) 2026 智维ITMSKY
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Web 多用户账号体系 (借鉴《用户管理需求说明文档》)。

设计:
  - 账号库放**全局** data/promptforge.db 的 pf_users 表 (登录前就要能查库,
    不能依赖用户工作区); 每个用户的工作区 (配置+历史) 在 data/users/<username>/。
  - 口令哈希: 标准库 hashlib.pbkdf2_hmac(sha256, 240000 轮, 16B 随机盐)。
    Web 层依赖表刻意只有 flask+waitress, 不为此新增第三方依赖;
    PBKDF2-SHA256 240k 轮为 OWASP 2023 推荐档, 单次约 100ms, 爆破成本足够。
  - 管理员三重认证: 用户名 + 密码 + 平台口令 (WEB_ACCESS_PASSWORD)。
    普通用户只需用户名+密码。平台口令不落库、不进日志, 仅登录瞬间比对。
  - 账号安全策略与其余四系统对齐: 连续失败锁定 (5 次/15 分钟, DB 持久化)、
    口令历史不重复 (最近 5 个)、软删除 (改名释放唯一键)、防枚举统一文案。
  - 首次启动自动建 admin (密码取 WEB_ADMIN_PASSWORD, 未设置则随机生成打印一次)。

表结构 (pf_users):
  username / pw_salt / pw_hash / role / is_active / created_at / last_login_at
  failed_attempts / locked_until / pw_history / deleted_at / must_change
"""

import os
import re
import json
import time
import hmac
import hashlib
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta

from ..utils.logging_setup import get_logger
from . import database as db

logger = get_logger("promptforge.data.users")

PBKDF2_ROUNDS = 240_000
ACCOUNT_FAIL_LIMIT = 5
ACCOUNT_LOCK_MINUTES = 15
PASSWORD_HISTORY_KEEP = 5
MIN_PW_LEN = 8

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fa5]{2,32}$")
_USERS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS pf_users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT UNIQUE NOT NULL,
    pw_salt         TEXT NOT NULL,
    pw_hash         TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'user',
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT,
    last_login_at   TEXT,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until    TEXT,
    pw_history      TEXT NOT NULL DEFAULT '[]',
    deleted_at      TEXT,
    must_change     INTEGER NOT NULL DEFAULT 0
)
"""

# 平台级键值设置: 新用户默认连接配置等, 存全局库 (与 pf_users 同库)
_SETTINGS_DDL = """
CREATE TABLE IF NOT EXISTS pf_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
)
"""

_lock = threading.Lock()


# =====================================================================
# 口令原语
# =====================================================================
def hash_password(password, salt_hex=None):
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return salt.hex(), digest.hex()


def verify_password(password, salt_hex, expected_hex):
    try:
        _, digest = hash_password(password or "", salt_hex)
    except Exception:
        return False
    return hmac.compare_digest(digest, expected_hex or "")


# 计时匀势哑哈希（防账号枚举侧信道）：用户不存在时也跑一次同等成本的
# PBKDF2，使「账号存在」与「账号不存在」的响应时间无可测差异。
# 导入期一次性计算（此后每次不存在用户登录恰好一次 PBKDF2，与存在用户
# 校验口令的成本严格一致），不参与任何真实口令校验。
_TIMING_SALT, _TIMING_HASH = hash_password("timing-equalizer-not-a-real-password")


def _equalize_timing(password):
    """对不存在的用户执行一次同等成本的 PBKDF2 校验（结果恒为 False，仅耗时）。"""
    verify_password(password or "", _TIMING_SALT, _TIMING_HASH)


def generate_temp_password():
    """12 位一次性随机口令: 去易混字符, 保证大小写+数字。"""
    upper = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    lower = "abcdefghjkmnpqrstuvwxyz"
    digit = "23456789"
    pool = upper + lower + digit
    chars = [secrets.choice(upper), secrets.choice(lower), secrets.choice(digit)]
    chars += [secrets.choice(pool) for _ in range(9)]
    out = []
    while chars:
        out.append(chars.pop(secrets.randbelow(len(chars))))
    return "".join(out)


def check_password_strength(password):
    """返回 (ok, reason)。至少 8 位且含大小写+数字中 3 类。"""
    if not password or len(password) < MIN_PW_LEN:
        return False, "密码至少 %d 位" % MIN_PW_LEN
    if len(password) > 128:
        return False, "密码不超过 128 字符"
    classes = [bool(re.search(r"[a-z]", password)), bool(re.search(r"[A-Z]", password)),
               bool(re.search(r"\d", password)), bool(re.search(r"[^A-Za-z0-9]", password))]
    if sum(classes) < 3:
        return False, "密码需同时包含大写、小写、数字中的至少 3 类"
    weak = ["password", "qwerty", "admin123", "12345678", "letmein", "welcome",
            "iloveyou", "abc12345", "11111111", "00000000"]
    if password.lower() in weak:
        return False, "密码过于简单, 请换一个"
    return True, ""


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


# =====================================================================
# 库与连接 (始终走全局路径: 登录先于工作区绑定)
# =====================================================================
def _conn():
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    return conn


def init_users_db():
    """建 pf_users / pf_settings 表 (含幂等补列), 无账号时创建内置 admin。返回初始密码或 None。"""
    os.makedirs(db.BASE_DIR, exist_ok=True)
    conn = _conn()
    conn.executescript(_USERS_TABLE_DDL)
    conn.executescript(_SETTINGS_DDL)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pf_users)")}
    if "must_change" not in cols:
        conn.execute("ALTER TABLE pf_users ADD COLUMN must_change INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    n = conn.execute("SELECT COUNT(*) c FROM pf_users WHERE role='admin' AND deleted_at IS NULL").fetchone()["c"]
    conn.close()
    if n:
        return None
    env_pw = (os.environ.get("WEB_ADMIN_PASSWORD") or "").strip()
    if env_pw:
        pw = env_pw
    else:
        pw = generate_temp_password()
    # 随机生成的初始密码 = 系统下发的口令 → 强制首登改密 (与其余四系统一致)
    create_user("admin", pw, role="admin", must_change=0 if env_pw else 1)
    logger.warning("已创建内置管理员 admin (来源: %s)", "环境变量 WEB_ADMIN_PASSWORD" if env_pw else "随机生成")
    return pw if not env_pw else None


# =====================================================================
# CRUD
# =====================================================================
def create_user(username, password, role="user", must_change=0):
    username = (username or "").strip()
    if not _USERNAME_RE.match(username):
        raise ValueError("用户名需为 2-32 位中文/字母/数字/下划线")
    if role not in ("user", "admin"):
        raise ValueError("角色只能是 user/admin")
    if not password:
        raise ValueError("密码不能为空")
    ok, reason = check_password_strength(password)
    if not ok:
        raise ValueError(reason)
    salt, digest = hash_password(password)
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                "INSERT INTO pf_users (username, pw_salt, pw_hash, role, created_at, last_login_at, must_change)"
                " VALUES (?,?,?,?,?,NULL,?)",
                (username, salt, digest, role, _now(), 1 if must_change else 0))
            conn.commit()
        except sqlite3.IntegrityError:
            raise ValueError("用户名已存在")
        finally:
            conn.close()
    seed_workspace(username)   # 建工作区并套用平台默认配置
    logger.info("已创建账号 %s (role=%s)", username, role)
    return {"username": username, "role": role}


def get_user(username):
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM pf_users WHERE username=? AND deleted_at IS NULL",
        ((username or "").strip(),)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_users():
    conn = _conn()
    rows = conn.execute(
        "SELECT username, role, is_active, created_at, last_login_at, failed_attempts,"
        " locked_until, deleted_at, must_change FROM pf_users WHERE deleted_at IS NULL ORDER BY id").fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        lu = _parse_ts(d.get("locked_until"))
        d["is_locked"] = bool(lu and lu > datetime.now())
        d.pop("locked_until", None)
        out.append(d)
    return out


def _bump_fail(conn, row):
    """失败计数 +1；达上限即锁定。

    单条原子 UPDATE（CASE 在 SQLite 中以旧行值为基准求值）：并发登录失败时
    两个并行请求不会互相覆盖计数，锁定阈值不会被延迟或丢失（原读-改-写
    两段式在竞态下可能把已累计的失败数写回旧值）。语义与原实现一致：
    第 5 次失败 → failed_attempts 归零 + locked_until 置为当前 + 15 分钟。"""
    until = (datetime.now() + timedelta(minutes=ACCOUNT_LOCK_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE pf_users SET "
        "failed_attempts = CASE WHEN failed_attempts + 1 >= ? THEN 0"
        " ELSE failed_attempts + 1 END, "
        "locked_until = CASE WHEN failed_attempts + 1 >= ? THEN ?"
        " ELSE locked_until END "
        "WHERE id=?",
        (ACCOUNT_FAIL_LIMIT, ACCOUNT_FAIL_LIMIT, until, row["id"]))


def authenticate(username, password):
    """校验口令。成功返回用户 dict; 失败抛 ValueError (统一文案防枚举,
    锁定/停用是运维状态可如实告知)。"""
    username = (username or "").strip()
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM pf_users WHERE username=? AND deleted_at IS NULL",
        (username,)).fetchone()
    if row is None:
        # 计时匀势：不存在的用户也跑一次同等 PBKDF2 成本，
        # 避免响应时间差暴露账号是否存在（防枚举侧信道）
        _equalize_timing(password)
        conn.close()
        raise ValueError("用户名或密码错误")
    if not int(row["is_active"] or 0):
        conn.close()
        raise ValueError("账号已被停用, 请联系管理员")
    lu = _parse_ts(row["locked_until"])
    if lu and lu > datetime.now():
        remain = max(1, int((lu - datetime.now()).total_seconds() // 60) + 1)
        conn.close()
        raise ValueError("连续失败次数过多, 请 %d 分钟后再试或联系管理员解锁" % remain)
    if not verify_password(password or "", row["pw_salt"], row["pw_hash"]):
        _bump_fail(conn, row)
        conn.commit()
        conn.close()
        raise ValueError("用户名或密码错误")
    conn.execute("UPDATE pf_users SET last_login_at=?, failed_attempts=0, locked_until=NULL WHERE id=?",
                 (_now(), row["id"]))
    conn.commit()
    out = dict(row)
    conn.close()
    return out


def _push_history(conn, row, new_salt, new_digest):
    hist = json.loads(row["pw_history"] or "[]")
    hist = (["%s$%s" % (row["pw_salt"], row["pw_hash"])] + hist)[:PASSWORD_HISTORY_KEEP]
    conn.execute("UPDATE pf_users SET pw_history=? WHERE id=?", (json.dumps(hist), row["id"]))
    return hist


def _in_history(row, password):
    entries = ["%s$%s" % (row["pw_salt"], row["pw_hash"])] + json.loads(row["pw_history"] or "[]")
    for e in entries[:PASSWORD_HISTORY_KEEP]:
        try:
            salt, digest = e.split("$", 1)
        except ValueError:
            continue
        if verify_password(password, salt, digest):
            return True
    return False


def change_password(username, new_password, old_password=None, force=False):
    """本人改密 (force=强制改密模式, 跳过旧密码校验——用户手上是管理员给的临时口令)。
    强度+历史不重复约束恒定。成功即解除 must_change。"""
    ok, reason = check_password_strength(new_password)
    if not ok:
        raise ValueError(reason)
    conn = _conn()
    row = conn.execute("SELECT * FROM pf_users WHERE username=? AND deleted_at IS NULL",
                       (username,)).fetchone()
    if row is None:
        conn.close()
        raise ValueError("用户不存在")
    if not force:
        if old_password is None:
            conn.close()
            raise ValueError("请输入旧密码")
        if not verify_password(old_password, row["pw_salt"], row["pw_hash"]):
            conn.close()
            raise ValueError("旧密码错误")
    if _in_history(row, new_password):
        conn.close()
        raise ValueError("新密码不能与最近 %d 次使用过的密码相同" % PASSWORD_HISTORY_KEEP)
    salt, digest = hash_password(new_password)
    _push_history(conn, row, salt, digest)
    conn.execute("UPDATE pf_users SET pw_salt=?, pw_hash=?, failed_attempts=0, locked_until=NULL,"
                 " must_change=0 WHERE id=?", (salt, digest, row["id"]))
    conn.commit()
    conn.close()
    logger.info("账号 %s 修改了密码", username)


def admin_reset_password(username, new_password=None):
    """管理员重置: 留空=生成一次性临时口令。返回明文口令 (仅展示一次)。
    重置后强制该用户首登改密 (must_change=1)。"""
    conn = _conn()
    row = conn.execute("SELECT * FROM pf_users WHERE username=? AND deleted_at IS NULL",
                       (username,)).fetchone()
    if row is None:
        conn.close()
        raise ValueError("用户不存在")
    temp = None
    if not new_password:
        new_password = generate_temp_password()
        temp = new_password
    else:
        ok, reason = check_password_strength(new_password)
        if not ok:
            conn.close()
            raise ValueError(reason)
    salt, digest = hash_password(new_password)
    _push_history(conn, row, salt, digest)
    conn.execute("UPDATE pf_users SET pw_salt=?, pw_hash=?, failed_attempts=0, locked_until=NULL,"
                 " must_change=1 WHERE id=?", (salt, digest, row["id"]))
    conn.commit()
    conn.close()
    logger.info("管理员重置了 %s 的密码", username)
    return temp or new_password


def set_active(username, active):
    _guard_admin_target(username)
    conn = _conn()
    cur = conn.execute("UPDATE pf_users SET is_active=? WHERE username=? AND deleted_at IS NULL",
                       (1 if active else 0, username))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise ValueError("用户不存在")


def set_role(username, role):
    if role not in ("user", "admin"):
        raise ValueError("角色只能是 user/admin")
    conn = _conn()
    row = conn.execute("SELECT role FROM pf_users WHERE username=? AND deleted_at IS NULL",
                       (username,)).fetchone()
    if row is None:
        conn.close()
        raise ValueError("用户不存在")
    if row["role"] == "admin" and role != "admin":
        n = conn.execute("SELECT COUNT(*) c FROM pf_users WHERE role='admin' AND deleted_at IS NULL AND is_active=1").fetchone()["c"]
        if n <= 1:
            conn.close()
            raise ValueError("不能降级最后一个管理员")
    conn.execute("UPDATE pf_users SET role=? WHERE username=?", (role, username))
    conn.commit()
    conn.close()


def delete_user(username):
    """软删: 改名释放唯一键, 停用使会话立即失效; 工作区目录保留 (数据不销毁)。"""
    _guard_admin_target(username, allow_self=True)
    conn = _conn()
    row = conn.execute("SELECT id, username, role FROM pf_users WHERE username=? AND deleted_at IS NULL",
                       (username,)).fetchone()
    if row is None:
        conn.close()
        raise ValueError("用户不存在")
    if row["role"] == "admin":
        n = conn.execute("SELECT COUNT(*) c FROM pf_users WHERE role='admin' AND deleted_at IS NULL").fetchone()["c"]
        if n <= 1:
            conn.close()
            raise ValueError("不能删除最后一个管理员")
    conn.execute("UPDATE pf_users SET deleted_at=?, is_active=0, username=? WHERE id=?",
                 (_now(), "%s#del%d" % (username, row["id"]), row["id"]))
    conn.commit()
    conn.close()
    logger.info("已软删账号 %s (工作区保留)", username)


def unlock_user(username):
    conn = _conn()
    cur = conn.execute("UPDATE pf_users SET failed_attempts=0, locked_until=NULL WHERE username=? AND deleted_at IS NULL",
                       (username,))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise ValueError("用户不存在")


def _guard_admin_target(username, allow_self=False):
    """内置 admin 不可被停用/删除 (保底管理入口)。"""
    if username == "admin" and not allow_self:
        raise ValueError("内置管理员账号不可停用/删除")


# =====================================================================
# 工作区
# =====================================================================
def workspace_dir(username):
    """用户工作区目录。用户名进入路径拼接，此处按建号同款正则再做一次
    校验（纵深防御）：即便 pf_users 被绕过应用直接篡改注入 "../x" 之类
    用户名，也不会拼出越界路径。正常路径下所有调用方传入的名字都来自
    已通过 create_user 校验的账号，行为不变。"""
    if not _USERNAME_RE.match(username or ""):
        raise ValueError("非法用户名（不允许作为工作区目录）: %r" % (username,))
    return os.path.join(db.BASE_DIR, "users", username)


def ensure_workspace(username):
    """惰性建用户工作区目录+库 (幂等)。

    以 promptforge.db 是否存在为判据: 存在即跳过建表 (每请求绑定工作区时
    避免重开 sqlite 连接跑 CREATE TABLE); 目录被外部误删则自动重建。"""
    wd = workspace_dir(username)
    os.makedirs(wd, exist_ok=True)
    if os.path.exists(os.path.join(wd, "promptforge.db")):
        return wd
    with db.use_workspace(wd):
        db.init_db()
    return wd


def workspace_stats(username):
    """工作区概况 (管理员列表用): 记录条数 + 目录大小。非法用户名（库被
    直接篡改等）返回零值，不中断账号列表。"""
    try:
        wd = workspace_dir(username)
    except ValueError:
        return {"records": 0, "size_bytes": 0}
    if not os.path.isdir(wd):
        return {"records": 0, "size_bytes": 0}
    n = 0
    dbp = os.path.join(wd, "promptforge.db")
    try:
        # 先判存在再连接：sqlite3.connect 对不存在的库会创建空文件，
        # 遍历账号列表不应产生这种副作用文件（查询失败按 0 计）
        if os.path.exists(dbp):
            conn = sqlite3.connect(dbp)
            n = conn.execute("SELECT COUNT(*) FROM enhance_records").fetchone()[0]
            conn.close()
    except sqlite3.Error:
        pass
    size = 0
    for root, _dirs, files in os.walk(wd):
        for f in files:
            try:
                size += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return {"records": n, "size_bytes": size}


# =====================================================================
# 平台级设置: 新用户默认连接配置
# =====================================================================
def get_setting(key, default=None):
    conn = _conn()
    row = conn.execute("SELECT value FROM pf_settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row and row["value"] is not None else default


def set_setting(key, value):
    conn = _conn()
    conn.execute(
        "INSERT INTO pf_settings (key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, _now()))
    conn.commit()
    conn.close()


def get_default_config():
    """新用户默认连接配置 (未设置返回 None)。"""
    raw = get_setting("default_config")
    if not raw:
        return None
    try:
        cfg = json.loads(raw)
        return cfg if isinstance(cfg, dict) and cfg else None
    except (ValueError, TypeError):
        return None


def set_default_config(cfg):
    """写入新用户默认配置 (None/空 dict = 清除)。"""
    if not cfg:
        set_setting("default_config", "")
        return
    set_setting("default_config", json.dumps(cfg, ensure_ascii=False))


def seed_workspace(username):
    """新建工作区时套用平台默认连接配置 (仅当默认配置存在且工作区尚无配置)。
    使管理员"一次配置, 全员开箱可用"。"""
    defaults = get_default_config()
    if not defaults:
        return
    wd = ensure_workspace(username)
    with db.use_workspace(wd):
        try:
            conn = db._conn()
            has = conn.execute("SELECT COUNT(*) FROM app_config WHERE id=1").fetchone()[0]
            conn.close()
            if has:
                return
        except sqlite3.Error:
            return
        merged = dict(db.DEFAULT_CONFIG)
        for k in ("provider", "api_key", "base_url", "model", "temperature", "max_tokens", "style"):
            if defaults.get(k):
                merged[k] = defaults[k]
        db.save_config(merged)


# =====================================================================
# 全局统计 (管理概览): 跨所有工作区聚合
# =====================================================================
def global_stats():
    """平台总览: 账号数 / 活跃数 / 全站增强记录 / 今日记录 / 累计 token / 失败数。

    遍历 data/users/ 下每个工作区库聚合; 单库损坏不致命 (跳过并计数)。"""
    conn = _conn()
    rows = conn.execute(
        "SELECT username FROM pf_users WHERE deleted_at IS NULL").fetchall()
    conn.close()
    names = [r["username"] for r in rows]
    users_total = len(names)
    conn2 = _conn()
    active = conn2.execute(
        "SELECT COUNT(*) c FROM pf_users WHERE deleted_at IS NULL AND is_active=1").fetchone()["c"]
    admins = conn2.execute(
        "SELECT COUNT(*) c FROM pf_users WHERE deleted_at IS NULL AND role='admin'").fetchone()["c"]
    conn2.close()
    today = time.strftime("%Y-%m-%d")
    total = success = failed = today_n = tokens_in = tokens_out = 0
    broken = 0
    for name in names:
        try:
            p = os.path.join(workspace_dir(name), "promptforge.db")
        except ValueError:
            # 库被绕过应用直接篡改出的非法用户名：计入异常工作区而非中断聚合
            broken += 1
            continue
        if not os.path.exists(p):
            continue
        try:
            c = sqlite3.connect(p)
            r = c.execute(
                "SELECT COUNT(*) n,"
                " SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) s,"
                " SUM(CASE WHEN status!='success' THEN 1 ELSE 0 END) f,"
                " SUM(CASE WHEN date=? THEN 1 ELSE 0 END) t,"
                " COALESCE(SUM(prompt_tokens),0) pi,"
                " COALESCE(SUM(completion_tokens),0) po "
                "FROM enhance_records", (today,)).fetchone()
            c.close()
            total += r[0] or 0
            success += r[1] or 0
            failed += r[2] or 0
            today_n += r[3] or 0
            tokens_in += r[4] or 0
            tokens_out += r[5] or 0
        except sqlite3.Error:
            broken += 1
    return {
        "users_total": users_total, "users_active": active, "admins": admins,
        "records_total": total, "records_today": today_n,
        "success": success, "failed": failed,
        "tokens_in": tokens_in, "tokens_out": tokens_out,
        "tokens_total": tokens_in + tokens_out,
        "broken_workspaces": broken,
    }


def user_recent_records(username, limit=20):
    """管理员查看某用户最近的增强记录 (脱敏: 不返回完整输入输出, 只给摘要)。"""
    limit = max(1, min(int(limit or 20), 100))
    p = os.path.join(workspace_dir(username), "promptforge.db")
    if not os.path.exists(p):
        return []
    out = []
    try:
        c = sqlite3.connect(p)
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT created_at, provider, model, style, status, quality,"
            " duration_ms, input_chars, output_chars, input_text "
            "FROM enhance_records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        c.close()
        for r in rows:
            d = dict(r)
            txt = d.pop("input_text", "") or ""
            d["input_preview"] = (txt[:40] + "…") if len(txt) > 40 else txt
            out.append(d)
    except sqlite3.Error:
        return []
    return out
