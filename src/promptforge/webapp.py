#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Project: 提示词工坊项目
# Copyright (C) 2026 智维ITMSKY
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
提示词工坊 · PromptForge Web 版启动入口（v0.1.1 · 多用户）。

以 Flask + Waitress 提供浏览器界面，复用与桌面版完全相同的核心层：
  promptforge.core   —— 增强引擎（/chat/completions 调用、重试、质量判定）
  promptforge.data   —— SQLite 持久化（配置/历史/审计/备份）
  forge.gui_model    —— 五槽指令构造控制器（纯逻辑，无 tkinter）

多用户体系：
  - 每个用户拥有独立工作区 data/users/<username>/，各自一套 promptforge.db
    （连接配置 + 历史档案 + 备份互不可见）；账号表 pf_users 放全局库。
  - 登录：普通用户 = 用户名+密码；管理员 = 用户名+密码+平台口令
    （WEB_ACCESS_PASSWORD，三重认证）。
  - 数据层按线程绑定工作区（before_request 绑定 / teardown 解绑），
    业务路由（config/enhance/batch/history/forge）代码零改动即天然隔离。
  - 用户可导出自己的全部数据（ZIP：config.json + history.csv + 库快照）；
    管理员可查看/编辑任意用户的连接参数、查看/导出/清空其历史、
    新建/重置/启停/改角色/解锁/删除账号。
  - 账号安全（users.py）：连续失败 5 次锁定 15 分钟（DB 持久化）、
    口令历史最近 5 个不重复、软删除、防枚举统一文案；
    登录接口另有每 IP 失败限流（20 次/10 分钟）。
  - Token 用量统计（usage.py，全局库 api_usage 表）、历史全文检索
    （FTS5/LIKE 双模式）、五槽构造模板变量（jinja2 严格受限渲染）。
  - 增强响应本地缓存（promptforge.data.llm_cache，可按 model+messages+采样
    参数命中复用，环境变量 PROMPTFORGE_CACHE=0 可整体关闭）。
  - 错误响应统一携带 code / hint 字段（源自 engine.EnhanceError），便于前端
    按稳定错误码精确提示；code/hint 缺失时自动省略，不影响旧前端。

用法：
  python -m promptforge.webapp    # 默认监听 0.0.0.0:8000
环境变量：
  HOST / PORT                 监听地址（默认 0.0.0.0:8000）
  WEB_ACCESS_PASSWORD         平台口令：管理员登录的第三因子（强烈建议设置）
  WEB_ADMIN_PASSWORD          可选：首次启动内置 admin 的初始密码（不设则随机并打印一次）
  PROMPTFORGE_API_KEY         注入模型密钥（优先于所有用户界面保存值）

版本沿革（完整线索见 docs/版本迭代说明-v0.1.0.md）：
  v1.x noVNC 桌面方案 → v2.0 Flask 单口令 → 3.0 多用户 → 3.1 管理后台 →
  3.2 自助注册 → 3.3 用量/检索/模板变量 → 2026-10-12 归一为 v0.1.0 正式版。
"""

import os
import io
import csv
import json
import time
import hmac
import secrets
import threading
import zipfile
from datetime import timedelta
from functools import wraps

from flask import (Flask, request, jsonify, send_file, session,
                   redirect, url_for, abort)
from werkzeug.exceptions import HTTPException

from . import __version__
from .core import engine, templates, lint as lint_core
from .data import database as db
from .data import usage as usage_db
from .data import users
from .data import versions as verchain
from .config import load_providers
from .utils.logging_setup import set_log_file, configure_logging, get_logger, ensure_utf8_console
from forge import assembler as forge_assembler
from forge.gui_model import InstructionController

# 日志文件在业务初始化前确定
set_log_file(os.path.join(db.BASE_DIR, "promptforge.log"))
configure_logging()
logger = get_logger("promptforge.webapp")

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
PLATFORM_PASSPHRASE = os.environ.get("WEB_ACCESS_PASSWORD", "").strip()
MAX_BATCH_LINES = 200          # 单次批量上限（每条独立调用模型，防长时间占用）
MAX_DOC_CHARS = 2_000_000      # 指令构造文档长度上限
MAX_INPUT_CHARS = 100_000      # 单条增强输入上限（防超长输入撑爆上游调用与历史库）
DISPLAY_LIMIT = 100            # 历史页默认展示上限（与桌面版一致）

_WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
app.json.ensure_ascii = False
app.permanent_session_lifetime = timedelta(days=30)
# 会话 Cookie 安全属性（v0.1.0 加固）：HttpOnly 防 JS 读取，SameSite=Lax 拦截
# 跨站 POST 携带 Cookie（配合全部写接口强制 JSON Content-Type，CSRF 双保险）。
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Secure 属性：默认关闭（HTTP 内网直部署不受影响）；HTTPS / 反代部署设
# WEB_COOKIE_SECURE=1 开启后，浏览器仅在加密连接回传会话 Cookie，
# 杜绝明文 HTTP 下 Cookie 被旁路嗅探（v0.1.0 审查加固，行为默认不变）。
app.config["SESSION_COOKIE_SECURE"] = (
    os.environ.get("WEB_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes", "on"))


def _load_or_create_secret():
    """session 密钥持久化在数据目录，容器重启不掉登录态。"""
    secret_file = os.path.join(db.BASE_DIR, ".web_secret")
    try:
        if os.path.exists(secret_file):
            with open(secret_file, "r", encoding="utf-8") as f:
                s = f.read().strip()
            if s:
                return s
    except OSError:
        pass
    s = secrets.token_hex(32)
    try:
        os.makedirs(db.BASE_DIR, exist_ok=True)
        with open(secret_file, "w", encoding="utf-8") as f:
            f.write(s)
        os.chmod(secret_file, 0o600)
    except OSError:
        pass
    return s


app.secret_key = _load_or_create_secret()


# =====================================================================
# 会话 / 门禁 / 工作区绑定
# =====================================================================
def current_user():
    """会话用户与账号库对账：停用/软删 → 会话立即失效；角色变更 → 同步。"""
    name = session.get("u")
    if not name:
        return None
    row = users.get_user(name)
    if row is None or not int(row.get("is_active") or 0):
        # 停用与软删同样踢下线 (与其余四系统"禁用后会话立即失效"对齐)
        session.clear()
        return None
    if row["role"] != session.get("role"):
        session["role"] = row["role"]
    return row


def auth_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            if request.path.startswith("/api/"):
                return jsonify({"error": "未登录或会话已过期"}), 401
            return redirect(url_for("login_page"))
        # B2 强制首登改密: 管理员建号/重置下发的口令未换掉前, 业务接口一律拦截
        # (与其余四系统 DEFAULT_PWD_REQUIRED 同口径; 改密/登出/查我是逃生通道)
        if int(user.get("must_change") or 0) and request.path not in _MUST_CHANGE_ALLOWED:
            if request.path.startswith("/api/"):
                return jsonify({"error": "您仍在使用初始密码，请先修改密码后再使用",
                                "code": "MUST_CHANGE"}), 403
        return view(*args, **kwargs)
    return wrapper


# 强制改密态下仍放行的路径 (页面壳 + 逃生通道)
_MUST_CHANGE_ALLOWED = {"/", "/login", "/api/me", "/api/change-password", "/api/logout"}


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            return jsonify({"error": "未登录或会话已过期"}), 401
        if int(user.get("must_change") or 0):
            # 管理员自己也在强制改密态 (如重置了自己的口令): 管理面同样拦,
            # 否则 auth_required 的门禁可被 admin 路由绕过
            return jsonify({"error": "您仍在使用初始密码，请先修改密码后再使用",
                            "code": "MUST_CHANGE"}), 403
        if user["role"] != "admin":
            return jsonify({"error": "需要管理员权限"}), 403
        return view(*args, **kwargs)
    return wrapper


@app.before_request
def _bind_workspace():
    """每请求把数据层绑定到登录用户的工作区（线程局部，waitress 线程池安全）。
    ensure_workspace 以文件存在性判据, 常态只是一次 os.path.exists。"""
    name = session.get("u")
    if name:
        users.ensure_workspace(name)
        db.set_workspace(users.workspace_dir(name))


@app.teardown_request
def _unbind_workspace(_exc):
    db.set_workspace(None)


@app.after_request
def _security_headers(resp):
    """统一安全响应头（v0.1.0 加固）：防 MIME 嗅探 / 点击劫持 / Referrer 泄露 / XSS 注入面收敛。

    CSP 说明: 前端 web/index.html 为内联脚本/样式单页, script/style 需 unsafe-inline
    (与其余四系统同一取舍, 已记录安全报告遗留风险); connect-src 'self' 同时限制
    浏览器侧对模型 API 的跨源直连, 增强调用一律走服务端转发。"""
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'self'; "
        "form-action 'self'; frame-ancestors 'none'",
    )
    return resp


# 指令构造控制器：按用户各持一份（原全局单实例是单用户遗留，
# 多用户下 A 解析的文档会被 B 的 generate 误用）。
# v0.1.0：有界缓存——会话过期/未登出的用户控制器不再无限常驻
# （单个控制器最多持有 2MB 解析文档，长时间运行防内存缓涨）。
_FORGE_CTRL_MAX = 200
_forge_ctrls = {}
_forge_lock = threading.Lock()


def _forge():
    name = session.get("u") or "_"
    with _forge_lock:
        ctrl = _forge_ctrls.get(name)
        if ctrl is None:
            if len(_forge_ctrls) >= _FORGE_CTRL_MAX:
                # 淘汰最旧条目（dict 保持插入序；活动用户会随下次使用重新插入）
                oldest = next(iter(_forge_ctrls))
                _forge_ctrls.pop(oldest, None)
            ctrl = _forge_ctrls[name] = InstructionController()
        return ctrl


def _page(name):
    with open(os.path.join(_WEB_DIR, name), "r", encoding="utf-8") as f:
        return f.read()


def _page_raw(name, mimetype):
    """按原始字节返回 web/ 下的静态资源（图标等），并附缓存头。"""
    path = os.path.join(_WEB_DIR, name)
    if not os.path.isfile(path):
        abort(404)
    with open(path, "rb") as f:
        resp = app.response_class(f.read(), mimetype=mimetype)
    resp.headers.setdefault("Cache-Control", "public, max-age=86400")
    return resp


# =====================================================================
# 登录 / 登出 / 账号
# =====================================================================
# 每 IP 登录失败限流（v0.1.0 加固）：账号维度已有 5 次锁定，此处补 IP 维度，
# 阻断「换用户名字典慢速喷洒、每个账号只错一次」的口令猜测路径。
# 仅统计失败；内存态（重启清零，与注册限流同型，内网单机规模够用）。
_LOGIN_FAIL_WINDOW = 600
_LOGIN_FAIL_MAX_PER_IP = 20
_login_fails = {}
_login_lock = threading.Lock()


def _login_fail_ok(ip):
    now = time.time()
    with _login_lock:
        arr = [t for t in _login_fails.get(ip, []) if now - t < _LOGIN_FAIL_WINDOW]
        _login_fails[ip] = arr
        if len(_login_fails) > 5000:
            _login_fails.clear()
        return len(arr) < _LOGIN_FAIL_MAX_PER_IP


def _login_fail_bump(ip):
    with _login_lock:
        _login_fails.setdefault(ip, []).append(time.time())


@app.route("/favicon.svg")
def favicon():
    """站点图标（钴蓝工坊品牌标）。CSP img-src 'self' 下可直接引用。"""
    return _page_raw("favicon.svg", "image/svg+xml")


@app.route("/")
@auth_required
def index():
    return _page("index.html")


@app.route("/login")
def login_page():
    if session.get("u"):
        return redirect(url_for("index"))
    return _page("login.html")


@app.post("/api/login")
def api_login():
    ip = request.remote_addr or "?"
    if not _login_fail_ok(ip):
        logger.warning("登录 IP 限流触发 (ip=%s)", ip)
        return jsonify({"error": "尝试过于频繁，请 10 分钟后再试"}), 429
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    passphrase = body.get("passphrase") or ""
    try:
        user = users.authenticate(username, password)
    except ValueError as e:
        _login_fail_bump(ip)
        return jsonify({"error": str(e)}), 403
    # 管理员三重认证：用户名 + 密码 + 平台口令。口令错误与密码错误
    # 文案一致（防"该账号是管理员"的枚举泄露）。
    if user["role"] == "admin" and PLATFORM_PASSPHRASE:
        if not hmac.compare_digest(passphrase.encode("utf-8"),
                                   PLATFORM_PASSPHRASE.encode("utf-8")):
            logger.warning("管理员 %s 登录：平台口令不符", username)
            _login_fail_bump(ip)
            return jsonify({"error": "用户名或密码错误"}), 403
    session.clear()
    session.permanent = True
    session["u"] = user["username"]
    session["role"] = user["role"]
    users.ensure_workspace(user["username"])
    logger.info("用户 %s 登录成功 (role=%s)", username, user["role"])
    return jsonify({"ok": True, "role": user["role"]})


@app.post("/api/logout")
def api_logout():
    name = session.get("u")
    session.clear()
    with _forge_lock:
        _forge_ctrls.pop(name, None)
    return jsonify({"ok": True})


# =====================================================================
# 自助注册: 登录页注册入口, 管理员可在后台一键开关
# =====================================================================
_REG_WINDOW = 3600
_REG_MAX_PER_IP = 5          # 每 IP 每小时最多 5 次注册 (防批量灌水)
_reg_hits = {}
_reg_lock = threading.Lock()


def _register_open():
    """注册开关: pf_settings.register_open, 缺省开启 (1)。"""
    return (users.get_setting("register_open", "1") or "1") != "0"


def _reg_rate_ok(ip):
    now = time.time()
    with _reg_lock:
        arr = [t for t in _reg_hits.get(ip, []) if now - t < _REG_WINDOW]
        if len(arr) >= _REG_MAX_PER_IP:
            _reg_hits[ip] = arr
            return False
        arr.append(now)
        _reg_hits[ip] = arr
        if len(_reg_hits) > 5000:
            _reg_hits.clear()
        return True


@app.get("/api/register-status")
def api_register_status():
    """登录页据此决定是否显示注册入口。"""
    return jsonify({"open": _register_open()})


@app.post("/api/register")
def api_register():
    """自助注册: 角色固定 user, 自设密码不强制改密; 成功即自动登录。

    安全: ① 绝不接受 role 入参 (否则公开接口传 role=admin 即自建管理员);
    ② IP 限流 5 次/小时; ③ 用户名/口令强度由 create_user 统一校验;
    ④ 管理员可后台关闭注册 (防滥用)。"""
    if not _register_open():
        return jsonify({"error": "管理员已关闭自助注册，请联系管理员开通账号"}), 403
    ip = request.remote_addr or "?"
    if not _reg_rate_ok(ip):
        logger.warning("注册限流触发 (ip=%s)", ip)
        return jsonify({"error": "注册过于频繁，请稍后再试或联系管理员"}), 429
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    try:
        users.create_user(username, password, role="user", must_change=0)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    session.clear()
    session.permanent = True
    session["u"] = username
    session["role"] = "user"
    logger.info("新用户 %s 自助注册成功 (ip=%s)", username, ip)
    return jsonify({"ok": True, "role": "user"})


@app.get("/api/me")
@auth_required
def api_me():
    user = current_user()
    return jsonify({"username": user["username"], "role": user["role"],
                    "must_change": bool(int(user.get("must_change") or 0)),
                    "version": __version__,
                    "passphrase_enabled": bool(PLATFORM_PASSPHRASE)})


@app.post("/api/change-password")
@auth_required
def api_change_password():
    body = request.get_json(silent=True) or {}
    user = current_user()
    forced = bool(int(user.get("must_change") or 0))
    try:
        users.change_password(session["u"], body.get("new_password") or "",
                              old_password=body.get("old_password"), force=forced)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


# =====================================================================
# 连接配置（当前用户工作区）
# =====================================================================
def _effective_cfg():
    """读取已保存配置；环境变量 PROMPTFORGE_API_KEY 优先（与桌面版一致）。"""
    cfg = db.load_config()
    env_key = os.environ.get("PROMPTFORGE_API_KEY")
    if env_key:
        cfg["api_key"] = env_key
    return cfg


@app.get("/api/config")
@auth_required
def api_get_config():
    return jsonify({
        "config": db.load_config(),
        "env_key_injected": bool(os.environ.get("PROMPTFORGE_API_KEY")),
        "providers": load_providers(),
        "styles": templates.STYLE_LABEL_TO_KEY,
        "modes": templates.MODE_KEY_TO_LABEL,
        "temp_options": templates.TEMP_OPTIONS,
        "maxtoken_options": templates.MAXTOKEN_OPTIONS,
        "version": __version__,
        "auth_enabled": True,
    })


def _validate_cfg(body):
    style = body.get("style") or "balanced"
    if style not in templates.STYLE_KEY_TO_LABEL:
        raise ValueError("增强风格非法")
    mode = body.get("mode") or "user"
    if mode not in engine.MODE_WHITELIST:
        raise ValueError("增强模式非法（仅支持 user / system）")
    return {
        "provider": (body.get("provider") or "").strip(),
        "api_key": (body.get("api_key") or "").strip(),
        "base_url": (body.get("base_url") or "").strip(),
        "model": (body.get("model") or "").strip(),
        "temperature": str(body.get("temperature") or "0.7").strip(),
        "max_tokens": str(body.get("max_tokens") or "1024").strip(),
        "style": style,
        "mode": mode,
    }


def _apply_request_overrides(cfg, body):
    """把请求体里的可选覆盖项合并进有效配置（缺省一律不动，行为兼容）。

    修复项（2026-11-14 第五轮）：此前 /api/enhance 只用已保存配置，A/B 双版
    对比传来的 style 一直被忽略（两版风格实际相同）；现按「请求覆盖 > 已保存
    配置」合并，A/B 恢复按所选风格各生成一版。
      - style：取值须在 STYLE_KEY_TO_LABEL 白名单内；
      - mode：取值须在 engine.MODE_WHITELIST（user/system）内；
      - provider：须是 providers.json 既有预设（base_url/model 仅取自服务端
        预设，绝不接受请求传入的任意 URL——与 CLI --provider 同语义）。
    校验不通过抛 ValueError，由调用方转 400。"""
    style = body.get("style")
    if style:
        if style not in templates.STYLE_KEY_TO_LABEL:
            raise ValueError("增强风格非法")
        cfg["style"] = style
    mode = body.get("mode")
    if mode:
        if mode not in engine.MODE_WHITELIST:
            raise ValueError("增强模式非法（仅支持 user / system）")
        cfg["mode"] = mode
    provider = (body.get("provider") or "").strip()
    if provider:
        preset = load_providers().get(provider)
        if not preset:
            raise ValueError("未知厂商：%s" % provider)
        cfg["provider"] = provider
        if preset.get("base_url"):
            cfg["base_url"] = preset["base_url"]
        if preset.get("model"):
            cfg["model"] = preset["model"]
    return cfg


@app.post("/api/config")
@auth_required
def api_save_config():
    body = request.get_json(silent=True) or {}
    try:
        cfg = _validate_cfg(body)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    db.save_config(cfg)
    logger.info("连接配置已保存（user=%s, provider=%s, model=%s）",
                session["u"], cfg["provider"], cfg["model"])
    return jsonify({"ok": True})


# =====================================================================
# 增强（单条 / 批量）
# =====================================================================
def _safe_int(v):
    """token 数安全整数化：模型响应的 usage 可能缺失/为非数字字符串，
    一律折算为 None（按 0 记），绝不让异常打断增强主流程。"""
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _record(cfg, text, output, status, dur, source, err="", result=None):
    """与桌面版同口径落库（成功/失败都记录，便于统计与审计）。返回新记录 id。"""
    result = result or {}
    rid = db.append_record(
        created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        provider=cfg.get("provider", ""), model=cfg.get("model", ""),
        base_url=cfg.get("base_url", ""), input_text=text, output_text=output,
        status=status, error_msg=(err[:500] if err else None),
        duration_ms=dur, source=source,
        prompt_tokens=_safe_int(result.get("prompt_tokens")),
        completion_tokens=_safe_int(result.get("completion_tokens")),
        request_id=result.get("request_id"),
        quality=result.get("quality", ""),
        style=cfg.get("style", "balanced"),
        enh_mode=cfg.get("mode", "user"))
    # 用量统计（全局库 api_usage 表）：仅成功调用产生 token 明细，
    # 失败无 usage 不记；响应缺失 usage 时按 0 记，统计失败不影响增强结果。
    if status == "success":
        usage_db.record_usage(session.get("u") or "-", cfg.get("model", ""),
                              _safe_int(result.get("prompt_tokens")) or 0,
                              _safe_int(result.get("completion_tokens")) or 0)
    return rid


def _enhance_status_code(e):
    if isinstance(e, engine.ConfigError):
        return 400
    if isinstance(e, engine.AuthError):
        # 模型上游鉴权失败 = 本用户的 Key/配置问题（不可映射 401，
        # 那会让前端误判为登录态失效而跳登录页）。
        return 400
    if isinstance(e, engine.RateLimitError):
        return 429
    if isinstance(e, engine.TimeoutErrorEx):
        return 504
    return 502


def _error_payload(e, message=None):
    """统一错误响应体：{"error": message}，并附带 EnhanceError 的 code/hint。

    code / hint 为 None 时省略该键，保证旧前端（只读 error）不受影响。"""
    payload = {"error": message if message is not None else str(e)}
    code = getattr(e, "code", None)
    hint = getattr(e, "hint", None)
    if code is not None:
        payload["code"] = code
    if hint is not None:
        payload["hint"] = hint
    return payload


# =====================================================================
# 提示词静态体检（Prompt Lint · 借鉴 LangGPT 可验证结构特征，零 LLM 成本）
# =====================================================================
@app.post("/api/lint")
@auth_required
def api_lint():
    """提示词体检：返回 issues 列表 + 0-100 参考分。纯本地规则，无模型调用。"""
    body = request.get_json(silent=True) or {}
    text = body.get("text") or ""
    if len(text) > MAX_INPUT_CHARS:
        return jsonify({"error": "输入过长（上限 %d 字符）" % MAX_INPUT_CHARS}), 400
    return jsonify({"ok": True, "lint": lint_core.lint(text)})


@app.post("/api/enhance")
@auth_required
def api_enhance():
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "请输入要增强的提示词"}), 400
    if len(text) > MAX_INPUT_CHARS:
        return jsonify({"error": "输入过长（上限 %d 字符），请精简后重试" % MAX_INPUT_CHARS}), 400
    cfg = _effective_cfg()
    try:
        _apply_request_overrides(cfg, body)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    t0 = time.time()
    try:
        result = engine.enhance(cfg, text)
    except engine.EnhanceError as e:
        dur = int((time.time() - t0) * 1000)
        _record(cfg, text, "", "failed", dur, "single", err=str(e))
        return jsonify(_error_payload(e)), _enhance_status_code(e)
    dur = int((time.time() - t0) * 1000)
    rid = _record(cfg, text, result["output"], "success", dur, "single", result=result)
    note = ""
    if len(result["output"]) > templates.MAX_OUTPUT_CHARS:
        note = "结果 %d 字，超过 %d 字建议精简" % (
            len(result["output"]), templates.MAX_OUTPUT_CHARS)
    return jsonify({
        "output": result["output"],
        "quality": result.get("quality", ""),
        "duration_ms": dur,
        "prompt_tokens": result.get("prompt_tokens"),
        "completion_tokens": result.get("completion_tokens"),
        "request_id": result.get("request_id"),
        "length_note": note,
        "record_id": rid,       # 版本链锚点：前端可把本次增强结果固化为版本
    })


@app.post("/api/batch")
@auth_required
def api_batch():
    body = request.get_json(silent=True) or {}
    raw = body.get("lines") or []
    lines = [l.strip() for l in raw if isinstance(l, str) and l.strip()]
    if not lines:
        return jsonify({"error": "批量内容为空，请每行输入一条提示词"}), 400
    if len(lines) > MAX_BATCH_LINES:
        return jsonify({"error": "单次批量最多 %d 条，请分批提交" % MAX_BATCH_LINES}), 400
    too_long = next((l for l in lines if len(l) > MAX_INPUT_CHARS), None)
    if too_long is not None:
        return jsonify({"error": "单条输入过长（上限 %d 字符），请精简后分批提交"
                        % MAX_INPUT_CHARS}), 400
    cfg = _effective_cfg()
    try:
        _apply_request_overrides(cfg, body)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    results, ok, fail = [], 0, 0
    for i, line in enumerate(lines, 1):
        t0 = time.time()
        try:
            result = engine.enhance(cfg, line)
        except engine.EnhanceError as e:
            dur = int((time.time() - t0) * 1000)
            _record(cfg, line, "", "failed", dur, "batch", err=str(e))
            results.append({"index": i, "input": line, "ok": False,
                            **_error_payload(e)})
            fail += 1
            continue
        dur = int((time.time() - t0) * 1000)
        rid = _record(cfg, line, result["output"], "success", dur, "batch", result=result)
        results.append({"index": i, "input": line, "ok": True, "record_id": rid,
                        "output": result["output"], "quality": result.get("quality", "")})
        ok += 1
    logger.info("批量增强完成：成功 %d / 失败 %d", ok, fail)
    return jsonify({"results": results, "ok": ok, "fail": fail})


# =====================================================================
# 历史档案（当前用户工作区）
# =====================================================================
_CSV_HEADER = ["时间", "厂商", "模型", "风格", "模式", "状态", "输入字数",
               "输出字数", "质量", "输入", "输出", "RequestID"]


def _records_csv_bytes(recs):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_CSV_HEADER)
    for it in recs:
        w.writerow([
            it.get("time", ""), it.get("provider", ""),
            it.get("model", ""), it.get("style", ""), it.get("mode", "user"),
            it.get("status", ""),
            len(it.get("input", "") or ""), len(it.get("output", "") or ""),
            it.get("quality", ""), it.get("input", ""), it.get("output", ""),
            it.get("request_id", ""),
        ])
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


@app.get("/api/history")
@auth_required
def api_history():
    try:
        limit = max(1, min(int(request.args.get("limit", DISPLAY_LIMIT)), 1000))
    except ValueError:
        limit = DISPLAY_LIMIT
    # 全文检索：带 q 关键字时过滤本人历史（FTS5 MATCH，环境不支持则 LIKE 回退）
    q = (request.args.get("q") or "").strip()
    favorites = (request.args.get("favorites") or "").strip() in ("1", "true", "yes")
    records = db.search_records(q, limit, favorites=favorites) if q \
        else db.load_records(limit, favorites=favorites)
    return jsonify({"stats": db.get_stats(), "records": records, "search": q,
                    "favorites": favorites})


def _records_md_text(recs):
    """历史档案导出为 Markdown（P0-8）：每条记录一个小节，便于直接归档。"""
    parts = ["# 提示词工坊 · 历史档案导出\n",
             "导出时间：%s\n" % time.strftime("%Y-%m-%d %H:%M:%S")]
    for i, it in enumerate(recs, 1):
        ok = (it.get("status") or "success") == "success"
        parts.append("\n## %d. [%s] %s · %s\n" % (
            i, it.get("time", ""), it.get("provider", ""), it.get("model", "")))
        parts.append("- 风格：%s｜模式：%s｜状态：%s\n" % (
            it.get("style", "") or "—",
            templates.MODE_KEY_TO_LABEL.get(it.get("mode", "user"), it.get("mode", "user") or "user"),
            "成功" if ok else "失败"))
        parts.append("\n**输入**\n\n```text\n%s\n```\n" % (it.get("input", "") or ""))
        if it.get("output"):
            parts.append("\n**输出**\n\n```text\n%s\n```\n" % it["output"])
    return "".join(parts)


@app.get("/api/history/export")
@auth_required
def api_history_export():
    fmt = (request.args.get("format") or "csv").strip().lower()
    data = db.load_records()  # 不传 limit，导出全部
    if fmt == "md":
        return send_file(io.BytesIO(_records_md_text(data).encode("utf-8")),
                         mimetype="text/markdown; charset=utf-8", as_attachment=True,
                         download_name="promptforge-history.md")
    if fmt != "csv":
        return jsonify({"error": "不支持的导出格式（可选 csv / md）"}), 400
    out = io.BytesIO(_records_csv_bytes(data))
    return send_file(out, mimetype="text/csv", as_attachment=True,
                     download_name="promptforge-history.csv")


@app.post("/api/history/<int:rid>/favorite")
@auth_required
def api_history_favorite(rid):
    """切换历史记录收藏状态（P1-1）。记录不属于当前工作区 → 404（用户隔离）。"""
    rec = db.get_record(rid)
    if rec is None:
        return jsonify({"error": "历史记录不存在"}), 404
    try:
        new_state = db.toggle_favorite(rid)
    except Exception as e:  # 收藏非关键路径：异常上抛但转 500 + 日志，便于审计
        logger.exception("切换收藏失败 (rid=%s): %s", rid, e)
        return jsonify({"error": "收藏切换失败，请稍后重试"}), 500
    return jsonify({"ok": True, "record_id": rid, "favorite": new_state})


@app.post("/api/history/clear")
@auth_required
def api_history_clear():
    try:
        backup_path = db.backup_database()
    except db.BackupError as e:
        return jsonify({"error": "数据库备份失败，已取消清空：%s" % e}), 500
    removed = db.clear_records()
    db.log_clear(os.path.basename(backup_path), removed, "Web 端主动清空历史档案")
    logger.info("已清空 %d 条历史（备份：%s）", removed, backup_path)
    return jsonify({"ok": True, "removed": removed,
                    "backup": os.path.basename(backup_path)})


# =====================================================================
# 提示词版本链（F2）与评估裁判
# =====================================================================
# 权限口径（对照调研的 langfuse/pezzo 版本行 createdBy 语义）：
#   - 本人：/api/history/<rid>/... 系列，工作区绑定天然隔离，只能看到自己的记录；
#   - 管理员：只读可见（/api/admin/users/<u>/history/<rid>/versions 与 /diff，
#     借用既有代管 use_workspace 模式）；写操作（固化/回滚/评估）仅限本人。
_JUDGE_SYSTEM = (
    "你是提示词质量评估裁判。对给定的「原始提示词」与「增强后的提示词」，"
    "从三个维度打分：accuracy(准确性)、completeness(完整性)、clarity(清晰度)，"
    "各为 1-10 整数；再给一句不超过 80 字的中文改进建议。"
    "只输出一个 JSON 对象，格式："
    '{"accuracy": 整数, "completeness": 整数, "clarity": 整数, "suggestion": "一句话"}，'
    "不要输出 JSON 以外的任何内容（不要代码围栏、不要解释）。"
)
_JUDGE_MAX_CHARS = 8000       # 送入裁判的原文/结果截断上限（控 token 成本）


def _parse_judge_score(text):
    """解析裁判模型输出的评分 JSON（容忍代码围栏/前后缀文案）；非法返回 None。"""
    if not text:
        return None
    s = text.strip()
    if "```" in s:                      # 剥掉 ```json ... ``` 围栏
        parts = s.split("```")
        s = max(parts, key=len) if len(parts) > 1 else s
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        d = json.loads(s[start:end + 1])
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None

    def _score(v):
        try:
            return max(1, min(10, int(v)))
        except (TypeError, ValueError):
            return None

    out = {"accuracy": _score(d.get("accuracy")),
           "completeness": _score(d.get("completeness")),
           "clarity": _score(d.get("clarity")),
           "suggestion": str(d.get("suggestion") or "").strip()[:200]}
    if any(out[k] is None for k in ("accuracy", "completeness", "clarity")):
        return None
    return out


@app.get("/api/history/<int:rid>/versions")
@auth_required
def api_versions(rid):
    """某历史记录的版本链（本人）。记录不属于当前工作区 → 404（用户隔离）。"""
    rec = db.get_record(rid)
    if rec is None:
        return jsonify({"error": "历史记录不存在"}), 404
    return jsonify({"record_id": rid,
                    "current_version_id": rec.get("current_version_id"),
                    "versions": verchain.list_versions(rid)})


@app.post("/api/history/<int:rid>/versions")
@auth_required
def api_create_version(rid):
    """把内容固化为该记录的新版本（一切变更皆 commit 的统一入口）。"""
    body = request.get_json(silent=True) or {}
    try:
        v = verchain.create_version(
            rid,
            content=body.get("content"),
            commit_message=body.get("commit_message"),
            slot_values=body.get("slot_values"),
            source=body.get("source") or "manual",
            model=body.get("model"),
            created_by=session["u"])
    except verchain.VersionError as e:
        return jsonify({"error": str(e)}), e.status
    logger.info("用户 %s 为记录 %d 固化版本 v%d（%s）",
                session["u"], rid, v["version_no"], v["source"])
    return jsonify({"ok": True, "version": v})


@app.post("/api/history/<int:rid>/versions/<int:vid>/activate")
@auth_required
def api_activate_version(rid, vid):
    """回滚：把当前版本指针切到目标版本（不产生新行；写审计与日志）。"""
    try:
        result = verchain.activate_version(rid, vid, actor=session["u"])
    except verchain.VersionError as e:
        return jsonify({"error": str(e)}), e.status
    return jsonify(result)


@app.get("/api/history/<int:rid>/diff")
@auth_required
def api_version_diff(rid):
    """两版本结构化并排 diff（difflib）。left==right → {identical:true}。"""
    rec = db.get_record(rid)
    if rec is None:
        return jsonify({"error": "历史记录不存在"}), 404
    try:
        data = verchain.diff_versions(rid, request.args.get("left"),
                                      request.args.get("right"))
    except verchain.VersionError as e:
        return jsonify({"error": str(e)}), e.status
    return jsonify(data)


@app.post("/api/history/<int:rid>/evaluate")
@auth_required
def api_history_evaluate(rid):
    """评估裁判（加做项）：复用当前用户已配置模型对指定历史记录打分。
    模型失败/输出非法 → 明确错误提示（不静默）；评分有当前版本时入版本链元数据。"""
    rec = db.get_record(rid)
    if rec is None:
        return jsonify({"error": "历史记录不存在"}), 404
    if not (rec.get("output") or "").strip():
        return jsonify({"error": "该记录没有增强结果，无法评估"}), 400
    cfg = _effective_cfg()
    user_prompt = ("【原始提示词】\n%s\n\n【增强后的提示词】\n%s\n\n请按系统要求只输出评分 JSON。"
                   % ((rec.get("input") or "")[:_JUDGE_MAX_CHARS],
                      (rec.get("output") or "")[:_JUDGE_MAX_CHARS]))
    t0 = time.time()
    try:
        result = engine.raw_completion(cfg, _JUDGE_SYSTEM, user_prompt, timeout=90)
    except engine.EnhanceError as e:
        return jsonify(_error_payload(e, "评估失败：%s" % e)), _enhance_status_code(e)
    score = _parse_judge_score(result["output"])
    if score is None:
        return jsonify({"error": "评估失败：模型未返回有效的评分 JSON，请更换模型后重试"}), 502
    version_id = rec.get("current_version_id")
    persisted = False
    if version_id:
        try:
            verchain.set_version_score(rid, version_id, score, actor=session["u"])
            persisted = True
        except verchain.VersionError:
            persisted = False       # 指针被并发切走等边缘：评分仍返回，不静默丢
    logger.info("用户 %s 评估了记录 %d（accuracy=%s, persisted=%s）",
                session["u"], rid, score["accuracy"], persisted)
    return jsonify({"ok": True, "score": score, "version_id": version_id,
                    "persisted": persisted,
                    "duration_ms": int((time.time() - t0) * 1000)})


# =====================================================================
# Token 用量与成本统计（全局库 api_usage 表）
# =====================================================================
@app.get("/api/usage")
@auth_required
def api_usage():
    """本人用量汇总（按模型 / 按日近 30 天）。"""
    return jsonify({"ok": True, "usage": usage_db.usage_summary(session["u"])})


@app.get("/api/admin/usage")
@admin_required
def api_admin_usage():
    """全员用量汇总：总览 + 按用户 + 按模型 / 按日。"""
    return jsonify({"ok": True, "usage": usage_db.usage_summary(None),
                    "by_user": usage_db.usage_by_user()})


@app.get("/api/admin/usage-prices")
@admin_required
def api_admin_get_prices():
    return jsonify({"ok": True, "prices": usage_db.get_prices()})


@app.post("/api/admin/usage-prices")
@admin_required
def api_admin_set_prices():
    """配置模型单价（单位：元/百万 tokens，JSON：{模型: {prompt, completion}}）。
    配置后新增的调用才计成本（cost 可空）；空对象 = 清除配置。"""
    body = request.get_json(silent=True) or {}
    prices = body.get("prices")
    if not isinstance(prices, dict):
        return jsonify({"error": "单价配置须为对象"}), 400
    clean = {}
    for model, p in prices.items():
        model = str(model or "").strip()
        if not model:
            continue
        if not isinstance(p, dict):
            return jsonify({"error": "模型 %s 的单价须为对象" % model}), 400
        try:
            entry = {"prompt": float(p.get("prompt") or 0),
                     "completion": float(p.get("completion") or 0)}
        except (TypeError, ValueError):
            return jsonify({"error": "模型 %s 的单价须为数字" % model}), 400
        if entry["prompt"] < 0 or entry["completion"] < 0:
            return jsonify({"error": "单价不能为负数"}), 400
        clean[model] = entry
    usage_db.set_prices(clean)
    logger.info("管理员 %s 更新了模型单价配置（%d 个模型）", session["u"], len(clean))
    return jsonify({"ok": True, "prices": clean})


# =====================================================================
# 数据导出（ZIP：配置 + 历史 CSV + 数据库快照）
# =====================================================================
def _workspace_zip(username):
    wd = users.workspace_dir(username)
    users.ensure_workspace(username)
    with db.use_workspace(wd):
        cfg = db.load_config()
        recs = db.load_records()
        snap = db.backup_database()  # VACUUM INTO 一致性快照（含配置+历史+审计）
    buf = io.BytesIO()
    try:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("config.json", json.dumps(cfg, ensure_ascii=False, indent=2))
            z.writestr("history.csv", _records_csv_bytes(recs).decode("utf-8-sig"))
            z.write(snap, "promptforge.db")
            z.writestr("README.txt",
                       "PromptForge 数据导出包\n\n"
                       "config.json     连接配置（provider/base_url/model/风格等）\n"
                       "history.csv     历史档案（UTF-8 BOM，Excel 可直接打开）\n"
                       "promptforge.db  数据库一致性快照（含配置/历史/清空审计）\n\n"
                       "导出用户：%s\n导出时间：%s\n" % (username, time.strftime("%Y-%m-%d %H:%M:%S")))
    finally:
        for p in (snap, snap + ".sha256"):
            try:
                os.remove(p)
            except OSError:
                pass
    buf.seek(0)
    return buf


@app.get("/api/export")
@auth_required
def api_export():
    buf = _workspace_zip(session["u"])
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name="promptforge-%s-export.zip" % session["u"])


# =====================================================================
# 五槽指令构造（forge，按用户隔离）
# =====================================================================
@app.post("/api/forge/parse")
@auth_required
def api_forge_parse():
    body = request.get_json(silent=True) or {}
    text = body.get("text") or ""
    # 文档名截断（v0.1.0）：防超长字符串进入提示词正文
    name = ((body.get("name") or "").strip() or "<粘贴文本>")[:200]
    if not text.strip():
        return jsonify({"error": "请粘贴或上传设计文档内容"}), 400
    if len(text) > MAX_DOC_CHARS:
        return jsonify({"error": "文档过长（上限 %d 字符）" % MAX_DOC_CHARS}), 400
    try:
        _forge().load_text(text, name)
    except Exception as e:
        return jsonify({"error": "解析失败：%s" % e}), 400
    return jsonify({"path": _forge().path,
                    "outline": _forge().outline(),
                    "detected": _forge().detected()})


@app.post("/api/forge/generate")
@auth_required
def api_forge_generate():
    body = request.get_json(silent=True) or {}
    ctrl = _forge()
    if not ctrl.loaded:
        return jsonify({"error": "尚未载入文档，请先解析"}), 400
    try:
        lines = [int(x) for x in (body.get("selected") or [])]
    except (TypeError, ValueError):
        return jsonify({"error": "章节选择参数非法"}), 400
    ctrl.set_selected(lines)
    try:
        out = ctrl.generate(
            task=body.get("task"), period=body.get("period"),
            code=body.get("code"), decisions=body.get("decisions"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    # 模板变量：首次生成检测到 {{变量}} 且未提供值 → 返回变量清单，
    # 前端动态补输入框；前端带 variables 二次提交后用 jinja2 渲染成最终指令。
    # 不含变量的模板完全不走渲染路径，行为与旧版一致。
    variables = body.get("variables")
    if isinstance(variables, dict) and variables:
        try:
            out["prompt"] = forge_assembler.render_variables(out.get("prompt", ""), variables)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    else:
        names, bad = forge_assembler.detect_variables(out.get("prompt", ""))
        if bad:
            return jsonify({"error": "模板变量名仅允许字母/数字/下划线：%s" % "、".join(bad)}), 400
        if names:
            out["needs_variables"] = True
            out["variables"] = names
    return jsonify(out)


@app.post("/api/forge/clear")
@auth_required
def api_forge_clear():
    _forge().clear()
    return jsonify({"ok": True})


# =====================================================================
# 管理员：账号与用户数据管理
# =====================================================================
@app.get("/api/admin/users")
@admin_required
def api_admin_users():
    out = []
    for u in users.list_users():
        u["stats"] = users.workspace_stats(u["username"])
        out.append(u)
    return jsonify({"users": out})


@app.post("/api/admin/users")
@admin_required
def api_admin_create_user():
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    role = body.get("role") or "user"
    temp = None
    if not password:
        temp = users.generate_temp_password()
        password = temp
    try:
        # 管理员下发的口令一律强制首登改密 (与其余四系统一致)
        users.create_user(username, password, role, must_change=1)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "username": username, "role": role,
                    "temp_password": temp})


def _admin_target(username):
    """目标账号存在性校验（不存在 → 404 语义 400 文案）。"""
    row = users.get_user(username)
    if row is None:
        raise ValueError("用户不存在")
    return row


@app.post("/api/admin/users/<username>/reset-password")
@admin_required
def api_admin_reset_password(username):
    body = request.get_json(silent=True) or {}
    explicit = (body.get("password") or "").strip()
    try:
        _admin_target(username)
        result = users.admin_reset_password(username, explicit or None)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "temp_password": None if explicit else result})


@app.post("/api/admin/users/<username>/active")
@admin_required
def api_admin_active(username):
    body = request.get_json(silent=True) or {}
    try:
        _admin_target(username)
        users.set_active(username, bool(body.get("active")))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.post("/api/admin/users/<username>/role")
@admin_required
def api_admin_role(username):
    body = request.get_json(silent=True) or {}
    try:
        _admin_target(username)
        users.set_role(username, body.get("role") or "")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.post("/api/admin/users/<username>/unlock")
@admin_required
def api_admin_unlock(username):
    try:
        users.unlock_user(username)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.delete("/api/admin/users/<username>")
@admin_required
def api_admin_delete(username):
    if username == session["u"]:
        return jsonify({"error": "不能删除当前登录账号"}), 400
    try:
        _admin_target(username)
        users.delete_user(username)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    with _forge_lock:
        _forge_ctrls.pop(username, None)
    return jsonify({"ok": True})


@app.get("/api/admin/users/<username>/config")
@admin_required
def api_admin_get_config(username):
    try:
        _admin_target(username)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    wd = users.workspace_dir(username)
    users.ensure_workspace(username)
    with db.use_workspace(wd):
        cfg = db.load_config()
    # API Key 不回传明文（管理员可覆写，不可窥视）
    cfg["api_key_set"] = bool(cfg.get("api_key"))
    cfg["api_key"] = ""
    return jsonify({"config": cfg})


@app.post("/api/admin/users/<username>/config")
@admin_required
def api_admin_save_config(username):
    body = request.get_json(silent=True) or {}
    try:
        _admin_target(username)
        cfg = _validate_cfg(body)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    wd = users.workspace_dir(username)
    users.ensure_workspace(username)
    with db.use_workspace(wd):
        if not cfg["api_key"]:
            # 留空 = 保持该用户原密钥（与"密码留空保持原值"同规则）
            cfg["api_key"] = db.load_config().get("api_key", "")
        db.save_config(cfg)
    logger.info("管理员 %s 更新了 %s 的连接配置", session["u"], username)
    return jsonify({"ok": True})


@app.get("/api/admin/users/<username>/history")
@admin_required
def api_admin_history(username):
    try:
        _admin_target(username)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    wd = users.workspace_dir(username)
    users.ensure_workspace(username)
    with db.use_workspace(wd):
        stats = db.get_stats()
        recs = db.load_records(100)
    return jsonify({"stats": stats, "records": recs})


@app.get("/api/admin/users/<username>/history/<int:rid>/versions")
@admin_required
def api_admin_versions(username, rid):
    """管理员只读查看某用户某记录的版本链（本人或管理员可见口径）。"""
    try:
        _admin_target(username)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    wd = users.workspace_dir(username)
    users.ensure_workspace(username)
    with db.use_workspace(wd):
        rec = db.get_record(rid)
        if rec is None:
            return jsonify({"error": "历史记录不存在"}), 404
        return jsonify({"record_id": rid,
                        "current_version_id": rec.get("current_version_id"),
                        "versions": verchain.list_versions(rid)})


@app.get("/api/admin/users/<username>/history/<int:rid>/diff")
@admin_required
def api_admin_version_diff(username, rid):
    """管理员只读对比某用户某记录的两个版本。"""
    try:
        _admin_target(username)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    wd = users.workspace_dir(username)
    users.ensure_workspace(username)
    with db.use_workspace(wd):
        rec = db.get_record(rid)
        if rec is None:
            return jsonify({"error": "历史记录不存在"}), 404
        try:
            data = verchain.diff_versions(rid, request.args.get("left"),
                                          request.args.get("right"))
        except verchain.VersionError as e:
            return jsonify({"error": str(e)}), e.status
    return jsonify(data)


@app.post("/api/admin/users/<username>/clear-history")
@admin_required
def api_admin_clear_history(username):
    try:
        _admin_target(username)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    wd = users.workspace_dir(username)
    with db.use_workspace(wd):
        try:
            backup_path = db.backup_database()
        except db.BackupError as ex:
            return jsonify({"error": "数据库备份失败，已取消清空：%s" % ex}), 500
        removed = db.clear_records()
        db.log_clear(os.path.basename(backup_path), removed,
                     "管理员 %s 清空 %s 的历史档案" % (session["u"], username))
    logger.info("管理员 %s 清空了 %s 的 %d 条历史（备份：%s）",
                session["u"], username, removed, backup_path)
    return jsonify({"ok": True, "removed": removed,
                    "backup": os.path.basename(backup_path)})


@app.get("/api/admin/users/<username>/export")
@admin_required
def api_admin_export(username):
    try:
        _admin_target(username)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    buf = _workspace_zip(username)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name="promptforge-%s-export.zip" % username)


# =====================================================================
# 管理员：平台概览与新用户默认配置
# =====================================================================
@app.get("/api/admin/stats")
@admin_required
def api_admin_stats():
    """全站聚合: 账号数 / 增强记录 / 今日 / token / 失败 (管理员概览卡用)。"""
    return jsonify({"ok": True, "stats": users.global_stats()})


@app.get("/api/admin/register-toggle")
@admin_required
def api_admin_get_register():
    return jsonify({"open": _register_open()})


@app.post("/api/admin/register-toggle")
@admin_required
def api_admin_set_register():
    """管理员开关自助注册。"""
    body = request.get_json(silent=True) or {}
    users.set_setting("register_open", "1" if body.get("open") else "0")
    logger.info("管理员 %s %s了自助注册", session["u"], "开启" if body.get("open") else "关闭")
    return jsonify({"ok": True, "open": bool(body.get("open"))})


def _validate_default_cfg(body):
    """校验并归一化平台默认配置; 允许字段留空 (留空=该项不预设)。"""
    style = (body.get("style") or "").strip()
    if style and style not in templates.STYLE_KEY_TO_LABEL:
        raise ValueError("增强风格非法")
    temp = (str(body.get("temperature") or "")).strip()
    if temp and temp not in [str(t) for t in templates.TEMP_OPTIONS]:
        raise ValueError("温度取值不在允许列表")
    mt = (str(body.get("max_tokens") or "")).strip()
    if mt and mt not in [str(t) for t in templates.MAXTOKEN_OPTIONS]:
        raise ValueError("最大长度取值不在允许列表")
    return {
        "provider": (body.get("provider") or "").strip(),
        "api_key": (body.get("api_key") or "").strip(),
        "base_url": (body.get("base_url") or "").strip(),
        "model": (body.get("model") or "").strip(),
        "temperature": temp,
        "max_tokens": mt,
        "style": style,
    }


@app.get("/api/admin/default-config")
@admin_required
def api_admin_get_default_config():
    cfg = users.get_default_config() or {}
    # API Key 只回"是否已设置", 不回明文 (管理员可覆写不可窥视)
    return jsonify({"config": {
        "provider": cfg.get("provider", ""), "base_url": cfg.get("base_url", ""),
        "model": cfg.get("model", ""), "temperature": cfg.get("temperature", ""),
        "max_tokens": cfg.get("max_tokens", ""), "style": cfg.get("style", ""),
        "api_key_set": bool(cfg.get("api_key")), "api_key": "",
    }})


@app.post("/api/admin/default-config")
@admin_required
def api_admin_save_default_config():
    body = request.get_json(silent=True) or {}
    try:
        cfg = _validate_default_cfg(body)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not any(cfg.values()):
        users.set_default_config(None)   # 全空 = 清除默认 (新用户走出厂配置)
        logger.info("管理员 %s 清除了新用户默认配置", session["u"])
        return jsonify({"ok": True, "cleared": True})
    if not cfg["api_key"]:
        old = users.get_default_config() or {}
        cfg["api_key"] = old.get("api_key", "")   # 留空保持原默认密钥
    users.set_default_config(cfg)
    logger.info("管理员 %s 更新了新用户默认配置 (provider=%s)", session["u"], cfg["provider"])
    return jsonify({"ok": True})


# =====================================================================
# 兜底错误处理
# =====================================================================
@app.errorhandler(Exception)
def _unhandled(e):
    if isinstance(e, HTTPException):
        return jsonify({"error": e.description}), e.code
    logger.exception("未处理异常")
    return jsonify({"error": "服务器内部错误，请稍后重试"}), 500


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "version": __version__})


def main():
    ensure_utf8_console()
    db.init_db()  # 全局库（桌面兼容 + 承载 pf_users 账号表）
    generated = users.init_users_db()
    usage_db.init_usage_db()  # 全局用量明细表（api_usage）
    if generated:
        print("[webapp] 已创建内置管理员 admin / %s （请立即登录修改）" % generated,
              flush=True)
    if not PLATFORM_PASSPHRASE:
        logger.warning("未设置 WEB_ACCESS_PASSWORD — 管理员登录不校验平台口令，"
                       "生产环境务必在 .env 中配置！")
    from waitress import serve
    logger.info("PromptForge Web v%s 启动: http://%s:%d（多用户模式）",
                __version__, HOST, PORT)
    print("[webapp] PromptForge Web v%s 监听 %s:%d（多用户；平台口令：%s）" % (
        __version__, HOST, PORT, "已启用" if PLATFORM_PASSPHRASE else "未设置"))
    serve(app, host=HOST, port=PORT, threads=8)


if __name__ == "__main__":
    main()
