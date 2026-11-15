#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Project: 提示词工坊项目
# Copyright (C) 2026 智维ITMSKY
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
提示词版本链与 diff（F2，纯标准库 difflib）。

设计（对照 langfuse / PromptWorks / pezzo 调研结论，落地按本项目实际结构）：
  - 版本锚点是历史记录 enhance_records（本项目无独立 prompts 表），表结构见
    database._VERSION_DDL：UNIQUE(record_id, version_no) 防并发撞号、parent_id
    成链、source 标记来源（init/manual/enhance）、回滚为指针切换不产生新行。
  - 版本行不可变：任何修改=插新行（version_no = MAX+1，IntegrityError 重试一次）。
  - diff 全部用 stdlib difflib：ndiff 组装并排行对（与调研的 PromptWorks 前端
    行对模型同构）、unified_diff 生成统一格式、SequenceMatcher.get_opcodes 做
    改动行内的字符级高亮，零第三方依赖。

本模块只依赖标准库与同包 database；多用户隔离由调用方（Web 层工作区绑定）保证，
本层 SQL 全部参数化。
"""

import difflib
import json
import sqlite3
import threading
from datetime import datetime

from ..utils.logging_setup import get_logger
from . import database as db

logger = get_logger("promptforge.data.versions")

# 版本来源：init=首个固化且与记录原始输出一致；manual=手动编辑/粘贴；
# enhance=标注由某模型增强而来；rollback_restore 预留（回滚只切指针不建行）。
SOURCES = ("init", "manual", "enhance")
CLIENT_SOURCES = ("manual", "enhance")     # 客户端允许传入的来源（init 由服务端判定）
COMMIT_MSG_MAX = 80                        # 版本说明上限（借 pezzo/langfuse 的必填短说明）
CONTENT_MAX_CHARS = 200_000                # 版本正文上限（防多 MB 大对象入库）
SLOT_VALUE_MAX = 20_000                    # 单个槽值上限


class VersionError(Exception):
    """版本链业务异常；status 供 Web 层映射 HTTP 状态码。"""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _conn():
    conn = db._conn()
    conn.row_factory = sqlite3.Row
    return conn


def _norm_slots(slot_values):
    """槽值归一化：dict[str,str]、键排序、合法字符不做假设；非法入参抛 VersionError。
    归一化后 json.dumps(sort_keys=True) 作为存储与 diff 的规范形（借 langfuse
    createSmartDiff 的"先排序再序列化"思路，保证同内容不同键序 diff 为空）。"""
    if slot_values is None:
        slot_values = {}
    if not isinstance(slot_values, dict):
        raise VersionError("槽值须为对象（键值对）")
    clean = {}
    for k, v in slot_values.items():
        k = str(k).strip()
        if not k:
            raise VersionError("槽名不能为空")
        v = "" if v is None else str(v)
        if len(v) > SLOT_VALUE_MAX:
            raise VersionError("槽 %s 的值超过 %d 字符上限" % (k, SLOT_VALUE_MAX))
        clean[k] = v
    return json.dumps(clean, sort_keys=True, ensure_ascii=False)


def _slots_dict(raw):
    """库中 slot_values JSON → dict；损坏数据按空对象处理（不阻断 diff/展示）。"""
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


# =====================================================================
# 版本写入
# =====================================================================
def create_version(record_id, content, commit_message, slot_values=None,
                   source="manual", model=None, created_by=""):
    """为一条历史记录创建新版本（不可变追加，最新版本自动成为当前版本）。

    校验与边界：
      - 记录不存在 → 404；
      - content 非空且 ≤ CONTENT_MAX_CHARS；commit_message 必填 ≤ 80 字符；
      - 与当前版本 content+slot_values 完全一致 → 400「内容无变化」（避免链上
        垃圾节点，与 pezzo 全留痕策略二选一时选后者）；
      - 首个版本且 content 与记录原始输出一致 → source 强制 'init'；
      - 并发撞号：捕获 UNIQUE IntegrityError 重查 MAX 重试一次，再失败 → 409。
    返回新版本 dict；失败抛 VersionError。"""
    ensure_ready()
    if source not in CLIENT_SOURCES:
        raise VersionError("版本来源非法（仅允许 manual/enhance）")
    if not isinstance(content, str):
        raise VersionError("版本内容须为文本")
    if not content.strip():
        raise VersionError("版本内容不能为空")
    if len(content) > CONTENT_MAX_CHARS:
        raise VersionError("版本内容超过 %d 字符上限" % CONTENT_MAX_CHARS)
    commit_message = (commit_message or "").strip()
    if not commit_message:
        raise VersionError("请填写版本说明")
    if len(commit_message) > COMMIT_MSG_MAX:
        raise VersionError("版本说明不超过 %d 字符" % COMMIT_MSG_MAX)
    if model is not None and not isinstance(model, str):
        raise VersionError("模型名须为文本")
    slot_json = _norm_slots(slot_values)
    model = (model.strip()[:200]) if model and model.strip() else None

    conn = _conn()
    try:
        rec = conn.execute(
            "SELECT id, output_text, current_version_id FROM enhance_records WHERE id=?",
            (record_id,)).fetchone()
        if rec is None:
            raise VersionError("历史记录不存在", 404)
        cur_id = rec["current_version_id"]
        if cur_id:
            cur = conn.execute(
                "SELECT content, slot_values FROM prompt_versions WHERE id=? AND record_id=?",
                (cur_id, record_id)).fetchone()
            if cur and cur["content"] == content and cur["slot_values"] == slot_json:
                raise VersionError("内容无变化，未创建新版本")
        for attempt in (1, 2):
            try:
                mx = conn.execute(
                    "SELECT COALESCE(MAX(version_no),0) FROM prompt_versions WHERE record_id=?",
                    (record_id,)).fetchone()[0]
                vno = mx + 1
                src = source
                if mx == 0 and content == (rec["output_text"] or ""):
                    src = "init"          # 首个版本且与原始输出一致 → 初始固化
                cur2 = conn.execute(
                    "INSERT INTO prompt_versions "
                    "(record_id, version_no, parent_id, source, slot_values, content,"
                    " model, commit_message, created_by, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (record_id, vno, cur_id, src, slot_json, content, model,
                     commit_message, created_by or "", _now()))
                vid = cur2.lastrowid
                # 新版本成为当前版本（回滚切走后，再次保存又把指针推到最新）
                conn.execute("UPDATE enhance_records SET current_version_id=? WHERE id=?",
                             (vid, record_id))
                conn.commit()
                logger.info("记录 %d 新增版本 v%d（source=%s, user=%s）",
                            record_id, vno, src, created_by or "-")
                return get_version(record_id, vid)
            except sqlite3.IntegrityError:
                # 并发下两请求同时算出同一 version_no：回滚重查再试一次
                conn.rollback()
                if attempt == 2:
                    raise VersionError("版本号并发冲突，请稍后重试", 409)
                logger.warning("记录 %d 版本号撞号，重试中", record_id)
    finally:
        conn.close()


def list_versions(record_id):
    """某记录的版本链（按 version_no 倒序，is_current 标记当前指针位置）。"""
    ensure_ready()
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT v.id, v.version_no, v.parent_id, v.source, v.slot_values, v.content,"
            " v.model, v.commit_message, v.score, v.created_by, v.created_at,"
            " CASE WHEN v.id = r.current_version_id THEN 1 ELSE 0 END AS is_current"
            " FROM prompt_versions v JOIN enhance_records r ON r.id = v.record_id"
            " WHERE v.record_id=? ORDER BY v.version_no DESC", (record_id,)).fetchall()
    finally:
        conn.close()
    return [{
        "id": r["id"], "version_no": r["version_no"], "parent_id": r["parent_id"],
        "source": r["source"], "slot_values": _slots_dict(r["slot_values"]),
        "content": r["content"], "model": r["model"] or "",
        "commit_message": r["commit_message"], "score": _slots_dict(r["score"]) or None,
        "created_by": r["created_by"], "created_at": r["created_at"],
        "is_current": bool(r["is_current"]),
    } for r in rows]


def get_version(record_id, version_id):
    """取某记录下的单个版本；不属于该记录或不存在 → None。"""
    ensure_ready()
    conn = _conn()
    try:
        r = conn.execute(
            "SELECT id, version_no, parent_id, source, slot_values, content, model,"
            " commit_message, score, created_by, created_at"
            " FROM prompt_versions WHERE id=? AND record_id=?",
            (version_id, record_id)).fetchone()
    finally:
        conn.close()
    if r is None:
        return None
    return {
        "id": r["id"], "version_no": r["version_no"], "parent_id": r["parent_id"],
        "source": r["source"], "slot_values": _slots_dict(r["slot_values"]),
        "content": r["content"], "model": r["model"] or "",
        "commit_message": r["commit_message"], "score": _slots_dict(r["score"]) or None,
        "created_by": r["created_by"], "created_at": r["created_at"],
    }


def activate_version(record_id, version_id, actor=""):
    """回滚：把当前版本指针切到目标版本（PromptWorks 模式，不产生新行）。

    校验照调研结论：目标不存在或不属于该记录 → 400「目标版本不存在或不属于
    该提示词」；重复激活同一版本幂等成功（already_current）。切换写 version_audit
    审计行并落日志（沿用现有 logging 机制）。"""
    ensure_ready()
    conn = _conn()
    try:
        rec = conn.execute("SELECT id FROM enhance_records WHERE id=?", (record_id,)).fetchone()
        if rec is None:
            raise VersionError("历史记录不存在", 404)
        v = conn.execute("SELECT id, version_no FROM prompt_versions WHERE id=? AND record_id=?",
                         (version_id, record_id)).fetchone()
        if v is None:
            raise VersionError("目标版本不存在或不属于该提示词", 400)
        cur = conn.execute("SELECT current_version_id FROM enhance_records WHERE id=?",
                           (record_id,)).fetchone()
        already = (cur["current_version_id"] == version_id) if cur else False
        if not already:
            conn.execute("UPDATE enhance_records SET current_version_id=? WHERE id=?",
                         (version_id, record_id))
            conn.execute(
                "INSERT INTO version_audit (record_id, version_id, action, actor, detail, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (record_id, version_id, "rollback", actor or "",
                 "回滚到 v%d" % v["version_no"], _now()))
            conn.commit()
            logger.info("用户 %s 将记录 %d 回滚到版本 v%d", actor or "-", record_id, v["version_no"])
        return {"ok": True, "record_id": record_id, "version_id": version_id,
                "version_no": v["version_no"], "already_current": already}
    finally:
        conn.close()


def set_version_score(record_id, version_id, score, actor=""):
    """把评估评分写入版本链元数据（score 列，JSON 字符串；版本正文保持不可变）。"""
    ensure_ready()
    if not isinstance(score, dict):
        raise VersionError("评分须为对象")
    payload = json.dumps(score, ensure_ascii=False)
    conn = _conn()
    try:
        v = conn.execute("SELECT id FROM prompt_versions WHERE id=? AND record_id=?",
                         (version_id, record_id)).fetchone()
        if v is None:
            raise VersionError("目标版本不存在或不属于该提示词", 400)
        conn.execute("UPDATE prompt_versions SET score=? WHERE id=?", (payload, version_id))
        conn.execute(
            "INSERT INTO version_audit (record_id, version_id, action, actor, detail, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (record_id, version_id, "evaluate", actor or "", payload, _now()))
        conn.commit()
    finally:
        conn.close()


# =====================================================================
# diff（stdlib difflib）
# =====================================================================
def _norm_with_map(text, ignore_case=False, ignore_ws=False):
    """diff 规范化（B-11，借鉴 promptfoo 断言比较的 ignore-case/
    ignore-whitespace 惯例）：返回 (规范化串, 索引映射)。

      - ignore_case：逐字符 lower（极少数展开成多字符的映射按原字符保留）；
      - ignore_ws：连续空白段（含制表/行尾空白）折叠为单个空格——空白「数量」
        不构成差异；
      - idx_map[k] = 规范化串第 k 个字符在原文中的下标，用于把规范化序列上
        算出的 opcodes 对齐回原文渲染（原文一字不改）。
    不开开关时返回 (原文, 恒等映射)。"""
    if not ignore_case and not ignore_ws:
        return text or "", list(range(len(text or "")))
    out, idx, prev_ws = [], [], False
    for i, ch in enumerate(text or ""):
        c = ch.lower() if ignore_case else ch
        if len(c) != 1:                # 大小写映射展开成多字符的罕见情况 → 保原字符
            c = ch
        ws = ch.isspace()
        if ignore_ws and ws:
            if prev_ws:                # 折叠连续空白段
                continue
            c = " "
        out.append(c)
        idx.append(i)
        prev_ws = ws if ignore_ws else False
    return "".join(out), idx


def _norm_key(text, ignore_case=False, ignore_ws=False):
    """行/条目级比较键（side_by_side 行匹配用；长度无约束）。"""
    return _norm_with_map(text, ignore_case, ignore_ws)[0]


def side_by_side(old, new, ignore_case=False, ignore_ws=False):
    """difflib.ndiff 输出解析为并排行对模型：
    added → {left:None, right:{text,type:'added'}}；removed → 左有右无；
    unchanged → 双侧同文；'? ' 指导行丢弃；空行以单空格占位保证双栏对齐。
    ndiff 保证删除块先于对应新增块出现，缓冲区逐位配对即得并排。

    ignore_case / ignore_ws（B-11）：行匹配用规范化行序列，渲染仍取各自
    原文行（ndiff 输出按序消费输入行，逐位回映即可）。unified/复制 Markdown
    保持严格原文 diff，不受开关影响。"""
    rows, buf_del, buf_add = [], [], []
    old_lines = (old or "").splitlines()
    new_lines = (new or "").splitlines()
    old_keys = [_norm_key(l, ignore_case, ignore_ws) for l in old_lines]
    new_keys = [_norm_key(l, ignore_case, ignore_ws) for l in new_lines]

    def flush():
        while buf_del or buf_add:
            l = buf_del.pop(0) if buf_del else None
            r = buf_add.pop(0) if buf_add else None
            rows.append({"left": l, "right": r})

    oi = ni = 0
    for line in difflib.ndiff(old_keys, new_keys):
        tag = line[:2]
        if tag == "- ":
            buf_del.append({"text": old_lines[oi] or " ", "type": "removed"}); oi += 1
        elif tag == "+ ":
            buf_add.append({"text": new_lines[ni] or " ", "type": "added"}); ni += 1
        elif tag == "  ":
            flush()
            rows.append({"left": {"text": old_lines[oi] or " ", "type": "unchanged"},
                         "right": {"text": new_lines[ni] or " ", "type": "unchanged"}})
            oi += 1; ni += 1
        # '? ' 行内指导标记：丢弃（不消费任何原文行）
    flush()
    return rows


def unified_diff_text(old, new, fromdesc="", todesc=""):
    """统一格式 diff 文本（导出/复制场景；严格原文，不受粒度开关影响）。"""
    return "\n".join(difflib.unified_diff(
        (old or "").splitlines(), (new or "").splitlines(),
        fromfile=fromdesc or "left", tofile=todesc or "right", lineterm=""))


def char_opcodes(left, right, ignore_case=False, ignore_ws=False):
    """同一改动行的左右文本做字符级差异（stdlib 无词级 diff，用 opcodes 等价），
    供前端在 added/removed 底色上再做行内高亮。

    ignore_case / ignore_ws（B-11）：在规范化序列（含空白折叠）上计算
    opcodes，再经索引映射切回原文渲染——原文片段一字不改，仅匹配口径放宽。"""
    lo, ro = left or "", right or ""
    na, ma = _norm_with_map(lo, ignore_case, ignore_ws)
    nb, mb = _norm_with_map(ro, ignore_case, ignore_ws)
    out = []
    for t, i1, i2, j1, j2 in difflib.SequenceMatcher(None, na, nb).get_opcodes():
        lseg = lo[ma[i1]: ma[i2 - 1] + 1] if i2 > i1 else ""
        rseg = ro[mb[j1]: mb[j2 - 1] + 1] if j2 > j1 else ""
        out.append({"tag": t, "left": lseg, "right": rseg})
    return out


def slot_diff(old_slots, new_slots):
    """逐槽 diff 统计：缺槽按空串对齐，输出 per-slot {added,removed,changed}。"""
    old_slots = old_slots or {}
    new_slots = new_slots or {}
    out = {}
    for k in sorted(set(old_slots) | set(new_slots)):
        a = str(old_slots.get(k, "") or "")
        b = str(new_slots.get(k, "") or "")
        added = removed = 0
        if a != b:
            for line in difflib.ndiff(a.splitlines(), b.splitlines()):
                t = line[:2]
                if t == "+ ":
                    added += 1
                elif t == "- ":
                    removed += 1
        out[k] = {"added": added, "removed": removed, "changed": a != b}
    return out


def diff_versions(record_id, left_id, right_id, ignore_case=False, ignore_ws=False):
    """两版本结构化对比。left==right → {identical: true}；版本不存在或不属于
    该记录 → 404。返回：行对 rows（改动行内附字符级 opcodes）、槽值段
    （规范化 JSON 并排 + 逐槽统计）、统一 diff 文本。

    ignore_case / ignore_ws（B-11）：仅作用于内容行匹配与行内高亮；
    identical 判定、槽值对比与 unified 文本保持严格原文。"""
    ensure_ready()
    try:
        left_id, right_id = int(left_id), int(right_id)
    except (TypeError, ValueError):
        raise VersionError("diff 参数须为版本 id")
    if left_id == right_id:
        return {"identical": True}
    left = get_version(record_id, left_id)
    right = get_version(record_id, right_id)
    if left is None or right is None:
        raise VersionError("版本不存在或不属于该提示词", 404)

    rows = side_by_side(left["content"], right["content"],
                        ignore_case=ignore_case, ignore_ws=ignore_ws)
    # 行内字符级高亮：仅对左右同有且不同、且长度受控的行附加 opcodes（控 payload）
    for row in rows:
        l, r = row["left"], row["right"]
        if (l and r and l["text"] != r["text"]
                and len(l["text"]) <= 2000 and len(r["text"]) <= 2000):
            row["opcodes"] = char_opcodes(l["text"], r["text"],
                                          ignore_case=ignore_case, ignore_ws=ignore_ws)

    def _meta(v):
        return {"id": v["id"], "version_no": v["version_no"], "source": v["source"],
                "commit_message": v["commit_message"], "model": v["model"],
                "created_by": v["created_by"], "created_at": v["created_at"],
                "is_current": v.get("is_current", False)}

    slots_old, slots_new = left["slot_values"], right["slot_values"]
    slots = {
        "rows": side_by_side(
            json.dumps(slots_old, sort_keys=True, ensure_ascii=False, indent=2),
            json.dumps(slots_new, sort_keys=True, ensure_ascii=False, indent=2)),
        "per_slot": slot_diff(slots_old, slots_new),
    }
    return {
        "identical": left["content"] == right["content"],
        "left": _meta(left), "right": _meta(right),
        "rows": rows, "slots": slots,
        "unified": unified_diff_text(left["content"], right["content"],
                                     "v%d" % left["version_no"], "v%d" % right["version_no"]),
    }


# =====================================================================
# 惰性结构保障（与 database.ensure_version_schema 同一实现，转发以收口调用方）
# =====================================================================
_ensure_lock = threading.Lock()


def repair_consistency():
    """版本链自愈：把 current_version_id 收敛到该记录 MAX(version_no) 那一行
    （单事务、幂等）。

    借鉴 legeling/PromptHub repairPromptVersionConsistency，按本项目**指针式
    回滚**的实际结构调整不变量。需要修的"坏指针/陈旧指针"：
      - current_version_id 为 NULL（却有版本历史）；
      - 指向已删除/不存在的版本（dangling），或指向别的记录的版本；
      - 指向的 version_no != MAX(version_no) 的旧版。
    以上统一收敛为指向 MAX(version_no) 那一行。

    唯一保留的例外：current_version_id 处存在 version_audit 的
    action='rollback' 留痕——这是本项目唯一会产生"正当停在旧版"的途径
    （activate_version 回滚必写审计行）。这类指针必须保留，否则每次
    ensure_ready（而它被每个版本读写入口调用）都会把用户的回滚悄悄退回
    最新版；已实测该情形会打破 test_activate_rollback_switches_pointer_and_audits
    与 test_version_api_full_flow 的回滚断言。

    无任何版本历史的记录：不建行，current_version_id 保持 NULL（与 create_version
    首版由内容判定 init 的逻辑不冲突）；仅当它已 dangling 成非 NULL 时才清回 NULL。

    并发：整个扫描+修复在单个 `BEGIN IMMEDIATE` 事务内完成，先拿写锁再判定，
    避免与 create_version 的并发撞号/切指针交叉。返回：
      {"scanned": 记录扫描条数, "repaired": 实际修复条数,
       "details": [{"record_id","from","to"}...]（最多 50 条）}

    兜底：任何异常（建表、取连接、开事务、扫描、更新）都只打 error 日志并返回
      {"scanned": 0, "repaired": 0, "details": []}
    绝不向上抛，保证启动与版本读写永不被自愈阻断。"""
    details_max = 50      # details 最多留 50 条，防异常数据一次回传过大 payload
    conn = None
    try:
        db.ensure_version_schema()      # 直调本函数时自给自足（ensure_ready 内为幂等命中缓存）
        conn = _conn()
        conn.isolation_level = None     # 交出手动事务控制，才能显式 BEGIN IMMEDIATE
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT r.id AS rid, r.current_version_id AS cur,"
            " (SELECT v.id FROM prompt_versions v WHERE v.record_id=r.id"
            "   ORDER BY v.version_no DESC LIMIT 1) AS target,"
            " EXISTS(SELECT 1 FROM prompt_versions v"
            "         WHERE v.id=r.current_version_id AND v.record_id=r.id) AS valid,"
            " EXISTS(SELECT 1 FROM version_audit va"
            "         WHERE va.record_id=r.id AND va.version_id=r.current_version_id"
            "         AND va.action='rollback') AS is_rb"
            " FROM enhance_records r").fetchall()
        scanned = len(rows)
        repaired = 0
        details = []
        for row in rows:
            cur = row["cur"]
            target = row["target"]
            if cur == target:
                continue                    # 已指向 MAX 行（含"无版本且指针为 NULL"）
            if row["valid"] and row["is_rb"]:
                continue                    # 合法回滚点（有 rollback 审计）→ 保留
            conn.execute("UPDATE enhance_records SET current_version_id=? WHERE id=?",
                         (target, row["rid"]))
            repaired += 1
            if len(details) < details_max:
                details.append({"record_id": row["rid"], "from": cur, "to": target})
        conn.execute("COMMIT")
        return {"scanned": scanned, "repaired": repaired, "details": details}
    except Exception as e:
        if conn is not None:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        logger.error("版本链自愈失败（已降级放行，不影响本次操作）: %s", e)
        return {"scanned": 0, "repaired": 0, "details": []}
    finally:
        if conn is not None:
            conn.close()


def ensure_ready():
    """版本链读写入口统一先行调用：确保当前库结构就绪（幂等、缓存化）。

    结构就绪后顺带做一次版本链自愈（repair_consistency）：修复 NULL/悬空指针，
    0 修复打 debug 避免每次启动都刷 info，有修复打 info。自愈任何异常都只记
    error 日志后放行，绝不阻断版本读写或应用启动。"""
    db.ensure_version_schema()
    try:
        rep = repair_consistency()
        n = rep.get("repaired", 0) if isinstance(rep, dict) else 0
        if n:
            logger.info("版本链自愈：扫描 %d 条记录，修复 %d 处指针不一致",
                        rep.get("scanned", 0), n)
        else:
            logger.debug("版本链自愈：无指针不一致（扫描 %d 条）", rep.get("scanned", 0))
    except Exception as e:        # 兜底：repair_consistency 已自身降级，此处双保险，
        logger.error("版本链自愈异常（不影响本次操作）: %s", e)   # 绝不阻断读写/启动
