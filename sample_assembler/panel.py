# -*- coding: utf-8 -*-
"""跨轮次 panel 轮换计划。

在单轮样本装配（assembler.py）之上增加“下一轮 panel 计划”能力，不重实现
单轮替补链：

- 首轮从当前已发布样本批次（published_version 指向的 batch）出发；后续轮
  只从上一个**已发布** panel 计划与历史账本（panel_change）继续计算，绝不
  只读当前样本表。
- 轮换规则声明：目标层、每层目标数、最大连续访问轮数、退出后的冷却轮数、
  本轮可留存的最低比例；名单归类/随机顺序/业务身份沿用既有层规则与
  identity_map 语义。
- 确定性计算：先按稳定排序 (random_rank, frame_id) 产生留存候选（上一轮仍
  合格、未处于冷却、未达连续上限），再为每层剩余名额选择新候选；每个成员
  生成一条带 wave、来源 assignment、连续轮数、冷却计数与决定原因的账本。
- 计划先落 draft（可重试）；request_key 唯一保证相同请求键重复计算不产生
  第二个计划、不重复占用候选；发布是单语句状态推进，失败/中断不改变上一
  份已发布计划。
- 占用兜底：计划内 (plan_id, frame_id) 部分唯一索引 + 全局 joined frame
  唯一索引，配合 BEGIN IMMEDIATE 整批重试，同一候选不可能被分给两个成员。
"""

import hashlib
import json
import math
import sqlite3
import time
import uuid
from datetime import datetime, timezone

from . import eligibility as elig
from .errors import PanelError, PublishError, RuleCompileError

MAX_PLAN_ATTEMPTS = 25
CONSIDERED_LIMIT = 1000

DECISION_RETAINED = "retained"
DECISION_EXITED = "exited"
DECISION_JOINED = "joined"

REASON_RETAINED = "retained"
REASON_JOINED = "joined_fill_gap"
REASON_MAX_CONSECUTIVE = "max_consecutive_reached"
REASON_OVER_TARGET = "exceeds_stratum_target"
REASON_STRATUM_DROPPED = "stratum_not_in_rotation"


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _new_id(prefix):
    return "%s_%s" % (prefix, uuid.uuid4().hex)


def member_key_for(business_unit_id, frame_id):
    """成员身份锚：优先业务单位，否则名单行本身。"""
    return business_unit_id if business_unit_id else "frame:%s" % frame_id


def panel_member_id_for(member_key):
    """确定性 panel_member 身份：同一业务身份永远得到同一 id。"""
    digest = hashlib.sha256(member_key.encode("utf-8")).hexdigest()
    return "PM_%s" % digest[:16]


class _RetryPlan(Exception):
    """内部信号：并发唯一约束冲突，整批计划重试。"""


# ================================================================ 轮换规则导入
def import_rotation_rules(asm, spec):
    """编译并持久化一套声明式轮换规则，返回 rotation_rule_id。

    spec = {
      "name": "rot1",
      "frame_rule_set": "rules_v1" 或 rule_set_id,   # 新名单归类用层规则
      "max_consecutive_waves": 2,     # 最大连续访问轮数（>=1）
      "cooldown_waves": 1,            # 退出后的冷却轮数（>=0）
      "min_retention_ratio": 0.5,     # 本轮可留存的最低比例 [0,1]
      "strata": [{"code": "S1", "target_n": 2}, ...]  # 目标层与每层目标数
    }
    """
    if not isinstance(spec, dict):
        raise RuleCompileError("轮换规则 spec 必须是 dict")
    name = spec.get("name")
    if not name:
        raise RuleCompileError("轮换规则缺少 name")

    ref = spec.get("frame_rule_set")
    if ref is None:
        raise RuleCompileError("轮换规则缺少 frame_rule_set（层规则引用）")
    if isinstance(ref, int):
        rsid = ref
    else:
        row = asm.conn.execute(
            "SELECT rule_set_id FROM rule_set WHERE name=?", (ref,)
        ).fetchone()
        if row is None:
            raise RuleCompileError("轮换规则引用的层规则集不存在: %r" % (ref,))
        rsid = row[0]
    rules = asm._load_rules(rsid)  # 不存在时抛 RuleCompileError

    max_cw = spec.get("max_consecutive_waves")
    if not isinstance(max_cw, int) or isinstance(max_cw, bool) or max_cw < 1:
        raise RuleCompileError("max_consecutive_waves 必须是 >=1 的整数")
    cooldown = spec.get("cooldown_waves")
    if not isinstance(cooldown, int) or isinstance(cooldown, bool) or cooldown < 0:
        raise RuleCompileError("cooldown_waves 必须是 >=0 的整数")
    ratio = spec.get("min_retention_ratio")
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) \
            or not 0.0 <= float(ratio) <= 1.0:
        raise RuleCompileError("min_retention_ratio 必须在 [0,1] 内")

    strata_spec = spec.get("strata")
    if not isinstance(strata_spec, list) or not strata_spec:
        raise RuleCompileError("轮换规则 strata 必须是非空列表")
    targets, seen = {}, set()
    for i, s in enumerate(strata_spec):
        code = s.get("code")
        if not code:
            raise RuleCompileError("第 %d 条轮换目标缺少 code" % i)
        if code in seen:
            raise RuleCompileError("轮换目标层重复: %s" % code)
        seen.add(code)
        if code not in rules.strata:
            raise RuleCompileError(
                "轮换目标层 %r 不在层规则集 %r 中" % (code, rules.name)
            )
        tn = s.get("target_n")
        if not isinstance(tn, int) or isinstance(tn, bool) or tn < 0:
            raise RuleCompileError("层 %s 的 target_n 必须是非负整数" % code)
        if tn > rules.strata[code]["capacity"]:
            raise RuleCompileError(
                "层 %s 轮换目标 %d 超过层容量 %d"
                % (code, tn, rules.strata[code]["capacity"])
            )
        targets[code] = tn

    conn = asm.conn
    conn.execute("BEGIN IMMEDIATE")
    try:
        dup = conn.execute(
            "SELECT 1 FROM rotation_rule_set WHERE name=?", (name,)
        ).fetchone()
        if dup:
            raise RuleCompileError("轮换规则集名称已存在: %s" % name)
        cur = conn.execute(
            "INSERT INTO rotation_rule_set(name, frame_rule_set_id,"
            " max_consecutive_waves, cooldown_waves, min_retention_ratio,"
            " created_at) VALUES (?,?,?,?,?,?)",
            (name, rsid, max_cw, cooldown, float(ratio), _now()),
        )
        rot_id = cur.lastrowid
        for code in sorted(targets, key=lambda c: rules.strata[c]["ordinal"]):
            conn.execute(
                "INSERT INTO rotation_stratum_target(rotation_rule_id,"
                " stratum_code, target_n, ordinal) VALUES (?,?,?,?)",
                (rot_id, code, targets[code], rules.strata[code]["ordinal"]),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return rot_id


def load_rotation(asm, ref=None):
    """按 id / name / 缺省（最新一套）加载轮换规则。"""
    conn = asm.conn
    if ref is None:
        row = conn.execute(
            "SELECT * FROM rotation_rule_set ORDER BY rotation_rule_id DESC"
            " LIMIT 1"
        ).fetchone()
    elif isinstance(ref, int):
        row = conn.execute(
            "SELECT * FROM rotation_rule_set WHERE rotation_rule_id=?", (ref,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM rotation_rule_set WHERE name=?", (ref,)
        ).fetchone()
    if row is None:
        raise PanelError("轮换规则不存在: %r" % (ref,))
    targets, order = {}, []
    for t in conn.execute(
        "SELECT stratum_code, target_n FROM rotation_stratum_target"
        " WHERE rotation_rule_id=? ORDER BY ordinal", (row["rotation_rule_id"],)
    ):
        targets[t["stratum_code"]] = t["target_n"]
        order.append(t["stratum_code"])
    return {
        "rotation_rule_id": row["rotation_rule_id"],
        "name": row["name"],
        "frame_rule_set_id": row["frame_rule_set_id"],
        "max_consecutive_waves": row["max_consecutive_waves"],
        "cooldown_waves": row["cooldown_waves"],
        "min_retention_ratio": row["min_retention_ratio"],
        "targets": targets,
        "order": order,
    }


# ================================================================ 生成下一轮计划
def generate_next_plan(asm, request_key, frame_version, rotation_rule=None,
                       note=None):
    """生成下一轮 panel 计划（先落 draft，可重试）。

    相同 request_key 重复调用直接返回已有计划，不产生第二个计划、不重复
    占用候选；并发冲突时整批回滚并在最新数据上重试。
    """
    if not request_key:
        raise PanelError("request_key 不能为空（幂等键）")
    rot = load_rotation(asm, rotation_rule)
    conn = asm.conn
    for attempt in range(1, MAX_PLAN_ATTEMPTS + 1):
        conn.execute("BEGIN IMMEDIATE")
        try:
            dup = conn.execute(
                "SELECT plan_id FROM panel_plan WHERE request_key=?",
                (request_key,),
            ).fetchone()
            if dup is not None:
                # 幂等命中：同一请求键不重新计算、不重复占用
                conn.execute("COMMIT")
                return _plan_result(conn, dup["plan_id"], idempotent=True)
            result = _compute_plan_tx(asm, rot, request_key, frame_version, note)
            conn.execute("COMMIT")
            result["attempts"] = attempt
            return result
        except _RetryPlan:
            conn.execute("ROLLBACK")
            time.sleep(0.02 * attempt)
            continue
        except Exception:
            conn.execute("ROLLBACK")
            raise
    raise PanelError("计划在 %d 次并发重试后仍失败" % MAX_PLAN_ATTEMPTS)


def _compute_plan_tx(asm, rot, request_key, frame_version, note):
    conn = asm.conn
    rules = asm._load_rules(rot["frame_rule_set_id"])

    # ---- 0. 新名单快照存在性 / 归类 / 容量（生成前拒绝） ----
    n_units = conn.execute(
        "SELECT COUNT(*) FROM frame_unit WHERE frame_version=?", (frame_version,)
    ).fetchone()[0]
    if n_units == 0:
        raise PanelError("名单版本 %s 不存在或为空" % frame_version)
    per_stratum_new = {c: [] for c in rules.ordinal_order}
    after = None
    while True:  # keyset 分页遍历新名单，归类沿用轮换规则声明的层规则集
        page = asm.list_frame(frame_version, page_size=500,
                              after_frame_id=after,
                              rule_set_id=rot["frame_rule_set_id"])
        for row in page["items"]:
            code = rules.classify(row["stratum_value"], bool(row["is_missing"]))
            if code is None:
                raise PanelError(
                    "单位 %s 的层字段 %r 无法归入任何层（生成前拒绝）"
                    % (row["frame_id"], row["stratum_value"])
                )
            per_stratum_new[code].append(row)
        if not page["has_more"]:
            break
        after = page["next_after"]
    for code in rules.ordinal_order:
        cap = rules.strata[code]["capacity"]
        if len(per_stratum_new[code]) > cap:
            raise PanelError(
                "层 %s 名单单位数 %d 超过层容量 %d（生成前拒绝）"
                % (code, len(per_stratum_new[code]), cap)
            )

    # ---- 1. 轮换基数：上一个已发布计划；首轮用已发布样本批次 ----
    base_plan = conn.execute(
        "SELECT * FROM panel_plan WHERE rotation_rule_id=? AND status='published'"
        " ORDER BY wave DESC LIMIT 1", (rot["rotation_rule_id"],)
    ).fetchone()
    prev_members = []   # 上一轮在样成员
    if base_plan is not None:
        base_wave = base_plan["wave"]
        base_plan_id, base_batch_id = base_plan["plan_id"], None
        for r in conn.execute(
            "SELECT * FROM panel_change WHERE plan_id=?"
            " AND decision IN ('retained','joined')", (base_plan["plan_id"],)
        ):
            prev_members.append({
                "frame_id": r["frame_id"],
                "business_unit_id": r["business_unit_id"],
                "stratum_code": r["stratum_code"],
                "consecutive_waves": r["consecutive_waves"],
                "source_assignment_id": r["assignment_id"],
            })
    else:
        pv = conn.execute(
            "SELECT * FROM published_version WHERE id=1"
        ).fetchone()
        if pv is None:
            raise PanelError(
                "尚无已发布样本批次或已发布计划，无法作为轮换基数"
            )
        base_wave = 0
        base_plan_id, base_batch_id = None, pv["batch_id"]
        for a in conn.execute(
            "SELECT * FROM assignment WHERE batch_id=? AND status='active'"
            " ORDER BY stratum_code, chain_seq, selection_position",
            (pv["batch_id"],),
        ):
            prev_members.append({
                "frame_id": a["frame_id"],
                "business_unit_id": a["business_unit_id"],
                "stratum_code": a["stratum_code"],
                "consecutive_waves": 1,   # 样本轮计为第 1 次连续参与
                "source_assignment_id": a["assignment_id"],
            })
    wave = base_wave + 1

    # ---- 2. 辅助集：身份映射、冷却集（已发布账本）、历史新加入占用 ----
    bu_by_frame = {}
    for bu, fid, _ver in conn.execute(
        "SELECT business_unit_id, frame_id, frame_version FROM identity_map"
    ):
        bu_by_frame[fid] = bu

    # 已发布账本中每个成员最近一次退出 -> 本轮剩余冷却 = cd - (wave - exit_wave)
    last_exit = {}  # member_id -> (wave, cooldown, bu, frame)
    for r in conn.execute(
        "SELECT pc.panel_member_id, pc.wave, pc.cooldown_remaining,"
        " pm.anchor_business_unit_id, pm.anchor_frame_id"
        " FROM panel_change pc"
        " JOIN panel_plan pp ON pp.plan_id=pc.plan_id AND pp.status='published'"
        " JOIN panel_member pm ON pm.panel_member_id=pc.panel_member_id"
        " WHERE pc.decision='exited' AND pp.rotation_rule_id=?",
        (rot["rotation_rule_id"],),
    ):
        mid = r["panel_member_id"]
        if mid not in last_exit or r["wave"] > last_exit[mid][0]:
            last_exit[mid] = (
                r["wave"], r["cooldown_remaining"],
                r["anchor_business_unit_id"], r["anchor_frame_id"],
            )
    cooldown_bus, cooldown_frames = set(), set()
    for mid, (exit_wave, cd, bu, fid) in last_exit.items():
        if cd - (wave - exit_wave) > 0:
            if bu:
                cooldown_bus.add(bu)
            cooldown_frames.add(fid)

    joined_frames_history = {
        r[0] for r in conn.execute(
            "SELECT frame_id FROM panel_change WHERE decision='joined'"
        )
    }

    def state_of(fid):
        r = conn.execute(
            "SELECT current_state FROM unit_status WHERE frame_id=?", (fid,)
        ).fetchone()
        return r["current_state"] if r else elig.STATE_PENDING

    rank_of = {}
    for r in conn.execute(
        "SELECT frame_id, random_rank FROM frame_unit"
    ):
        rank_of[r["frame_id"]] = r["random_rank"]

    # ---- 3. 落 draft 计划行 ----
    cur = conn.execute(
        "INSERT INTO panel_plan(request_key, rotation_rule_id, wave,"
        " frame_version, base_plan_id, base_batch_id, status, created_at, note)"
        " VALUES (?,?,?,?,?,?, 'draft', ?, ?)",
        (request_key, rot["rotation_rule_id"], wave, frame_version,
         base_plan_id, base_batch_id, _now(), note),
    )
    plan_id = cur.lastrowid

    def ensure_member(bu, fid, first_wave):
        key = member_key_for(bu, fid)
        pmid = panel_member_id_for(key)
        conn.execute(
            "INSERT OR IGNORE INTO panel_member(panel_member_id, member_key,"
            " anchor_business_unit_id, anchor_frame_id, first_wave, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (pmid, key, bu, fid, first_wave, _now()),
        )
        return pmid

    def insert_change(pmid, stratum, decision, reason, fid, bu,
                      source_assignment, assignment_id, consecutive, cooldown):
        try:
            conn.execute(
                "INSERT INTO panel_change(plan_id, wave, panel_member_id,"
                " stratum_code, decision, reason, frame_id, business_unit_id,"
                " source_assignment_id, assignment_id, consecutive_waves,"
                " cooldown_remaining, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (plan_id, wave, pmid, stratum, decision, reason, fid, bu,
                 source_assignment, assignment_id, consecutive, cooldown,
                 _now()),
            )
        except sqlite3.IntegrityError as e:
            # 并发下唯一索引拦截重复占用 -> 整批重试
            raise _RetryPlan(str(e))

    # ---- 4. 上一轮成员逐个决定（稳定排序：层序、random_rank、frame_id） ----
    def member_sort_key(m):
        code = m["stratum_code"]
        ordinal = rules.strata[code]["ordinal"] if code in rules.strata else 999
        return (ordinal, rank_of.get(m["frame_id"], 0.0), m["frame_id"])

    prev_members.sort(key=member_sort_key)
    retained_pool = {c: [] for c in rot["order"]}
    prev_n = {c: 0 for c in rot["order"]}
    used_frames_plan, used_bus_plan = set(), set()
    first_wave_base = 0 if base_batch_id is not None else wave - 1

    for m in prev_members:
        code = m["stratum_code"]
        fid = m["frame_id"]
        bu = m["business_unit_id"] or bu_by_frame.get(fid)
        pmid = ensure_member(bu, fid, first_wave_base)
        if code not in rot["targets"]:
            prev_n[code] = prev_n.get(code, 0) + 1
            insert_change(pmid, code, DECISION_EXITED, REASON_STRATUM_DROPPED,
                          fid, bu, m["source_assignment_id"], None,
                          m["consecutive_waves"], rot["cooldown_waves"])
            _enter_cooldown(cooldown_bus, cooldown_frames, bu, fid,
                            rot["cooldown_waves"])
            continue
        prev_n[code] += 1
        state = state_of(fid)
        if elig.is_failure(state):
            # 拒绝/失访/撤回：带原因退出
            insert_change(pmid, code, DECISION_EXITED, "exited_%s" % state,
                          fid, bu, m["source_assignment_id"], None,
                          m["consecutive_waves"], rot["cooldown_waves"])
            _enter_cooldown(cooldown_bus, cooldown_frames, bu, fid,
                            rot["cooldown_waves"])
        elif m["consecutive_waves"] >= rot["max_consecutive_waves"]:
            insert_change(pmid, code, DECISION_EXITED, REASON_MAX_CONSECUTIVE,
                          fid, bu, m["source_assignment_id"], None,
                          m["consecutive_waves"], rot["cooldown_waves"])
            _enter_cooldown(cooldown_bus, cooldown_frames, bu, fid,
                            rot["cooldown_waves"])
        else:
            retained_pool[code].append({**m, "panel_member_id": pmid,
                                        "business_unit_id": bu})

    # ---- 5. 每层留存截取（超出目标者退出） ----
    retained_n = {}
    for code in rot["order"]:
        target = rot["targets"][code]
        pool = retained_pool[code]          # 已按稳定序
        keep, drop = pool[:target], pool[target:]
        for m in drop:
            insert_change(m["panel_member_id"], code, DECISION_EXITED,
                          REASON_OVER_TARGET, m["frame_id"],
                          m["business_unit_id"], m["source_assignment_id"],
                          None, m["consecutive_waves"], rot["cooldown_waves"])
            _enter_cooldown(cooldown_bus, cooldown_frames,
                            m["business_unit_id"], m["frame_id"],
                            rot["cooldown_waves"])
        for m in keep:
            insert_change(m["panel_member_id"], code, DECISION_RETAINED,
                          REASON_RETAINED, m["frame_id"], m["business_unit_id"],
                          m["source_assignment_id"], _new_id("PA"),
                          m["consecutive_waves"] + 1, 0)
            used_frames_plan.add(m["frame_id"])
            if m["business_unit_id"]:
                used_bus_plan.add(m["business_unit_id"])
        retained_n[code] = len(keep)

    # ---- 6. 逐层补缺：新候选只填同层缺口 ----
    for code in rot["order"]:
        target = rot["targets"][code]
        open_slots = target - retained_n[code]
        required = int(math.ceil(
            rot["min_retention_ratio"] * prev_n.get(code, 0)))
        retention_ok = 1 if retained_n[code] >= required else 0

        considered, pool = [], []
        for row in per_stratum_new.get(code, []):
            fid = row["frame_id"]
            rank = row["random_rank"]
            if not row["contactable"]:
                considered.append({"frame_id": fid, "random_rank": rank,
                                   "decision": "excluded",
                                   "reason": "not_contactable"})
                continue
            state = state_of(fid)
            if elig.is_failure(state):
                considered.append({"frame_id": fid, "random_rank": rank,
                                   "decision": "excluded",
                                   "reason": "terminal_state:%s" % state})
                continue
            if fid in joined_frames_history:
                considered.append({"frame_id": fid, "random_rank": rank,
                                   "decision": "excluded",
                                   "reason": "occupied_prior_plan"})
                continue
            if fid in used_frames_plan:
                considered.append({"frame_id": fid, "random_rank": rank,
                                   "decision": "excluded",
                                   "reason": "frame_used_in_plan"})
                continue
            bu = bu_by_frame.get(fid) or row.get("business_unit_id")
            if bu and bu in used_bus_plan:
                considered.append({"frame_id": fid, "random_rank": rank,
                                   "decision": "excluded",
                                   "reason": "business_unit_conflict"})
                continue
            if (bu and bu in cooldown_bus) or fid in cooldown_frames:
                considered.append({"frame_id": fid, "random_rank": rank,
                                   "decision": "excluded",
                                   "reason": "cooldown_active"})
                continue
            pool.append((row, bu))

        pool.sort(key=lambda x: (x[0]["random_rank"], x[0]["frame_id"]))
        n_choose = max(open_slots, 0)
        chosen = pool[:n_choose]
        for idx, (row, bu) in enumerate(pool):
            considered.append({
                "frame_id": row["frame_id"],
                "random_rank": row["random_rank"],
                "decision": "chosen" if idx < n_choose else "available_not_used",
                "reason": None,
            })
        considered.sort(key=lambda c: (c["random_rank"], c["frame_id"]))
        if len(considered) > CONSIDERED_LIMIT:
            considered = considered[:CONSIDERED_LIMIT]

        joined_n = 0
        for row, bu in chosen:
            fid = row["frame_id"]
            pmid = ensure_member(bu, fid, wave)
            insert_change(pmid, code, DECISION_JOINED, REASON_JOINED,
                          fid, bu, None, _new_id("PA"), 1, 0)
            used_frames_plan.add(fid)
            if bu:
                used_bus_plan.add(bu)
            joined_n += 1

        remaining_open = open_slots - joined_n
        reasons = []
        if not retention_ok:
            reasons.append("retention_below_minimum:retained=%d,required=%d"
                           % (retained_n[code], required))
        if remaining_open > 0:
            excl = sorted({c["reason"] for c in considered
                           if c["decision"] == "excluded"})
            reasons.append("unfillable_slots:%d;exclusions=%s"
                           % (remaining_open, ",".join(excl or ["pool_empty"])))
        conn.execute(
            "INSERT INTO panel_stratum_gap(plan_id, stratum_code, target_n,"
            " prev_n, retained_n, joined_n, open_slots, retention_required,"
            " retention_ok, reason, considered_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (plan_id, code, target, prev_n.get(code, 0), retained_n[code],
             joined_n, max(remaining_open, 0), required, retention_ok,
             "; ".join(reasons) if reasons else None,
             json.dumps(considered, ensure_ascii=False)),
        )

    return _plan_result(conn, plan_id, idempotent=False)


def _enter_cooldown(cooldown_bus, cooldown_frames, bu, fid, cooldown_waves):
    """本轮退出成员立即进入冷却（cooldown_waves>0 时本轮即不可回归）。"""
    if cooldown_waves > 0:
        if bu:
            cooldown_bus.add(bu)
        cooldown_frames.add(fid)


# ================================================================ 计划结果装配
def _plan_result(conn, plan_id, idempotent):
    plan = conn.execute(
        "SELECT * FROM panel_plan WHERE plan_id=?", (plan_id,)
    ).fetchone()
    decisions = {DECISION_RETAINED: 0, DECISION_EXITED: 0, DECISION_JOINED: 0}
    for r in conn.execute(
        "SELECT decision, COUNT(*) n FROM panel_change WHERE plan_id=?"
        " GROUP BY decision", (plan_id,)
    ):
        decisions[r["decision"]] = r["n"]
    strata = {}
    for g in conn.execute(
        "SELECT * FROM panel_stratum_gap WHERE plan_id=? ORDER BY stratum_code",
        (plan_id,),
    ):
        strata[g["stratum_code"]] = {
            "target_n": g["target_n"], "prev_n": g["prev_n"],
            "retained_n": g["retained_n"], "joined_n": g["joined_n"],
            "open_slots": g["open_slots"],
            "retention_required": g["retention_required"],
            "retention_ok": bool(g["retention_ok"]),
            "reason": g["reason"],
        }
    members = [dict(r) for r in conn.execute(
        "SELECT panel_member_id, stratum_code, decision, frame_id,"
        " business_unit_id, consecutive_waves FROM panel_change"
        " WHERE plan_id=? AND decision IN ('retained','joined')"
        " ORDER BY stratum_code, frame_id", (plan_id,)
    )]
    return {
        "plan_id": plan["plan_id"], "request_key": plan["request_key"],
        "wave": plan["wave"], "status": plan["status"],
        "frame_version": plan["frame_version"],
        "idempotent": idempotent, "decisions": decisions,
        "strata": strata, "members": members,
    }


# ================================================================ 预览成员变更
def preview_plan_changes(conn, plan_id):
    """只读预览一个计划的逐成员变更账本与层级缺口（不产生任何写入）。"""
    plan = conn.execute(
        "SELECT * FROM panel_plan WHERE plan_id=?", (plan_id,)
    ).fetchone()
    if plan is None:
        raise PanelError("计划不存在: %s" % plan_id)
    changes = []
    for r in conn.execute(
        "SELECT pc.*, pm.member_key FROM panel_change pc"
        " JOIN panel_member pm ON pm.panel_member_id=pc.panel_member_id"
        " WHERE pc.plan_id=? ORDER BY pc.stratum_code, pc.change_id",
        (plan_id,),
    ):
        d = dict(r)
        d.pop("change_id", None)
        changes.append(d)
    strata = []
    for g in conn.execute(
        "SELECT * FROM panel_stratum_gap WHERE plan_id=? ORDER BY stratum_code",
        (plan_id,),
    ):
        d = dict(g)
        d.pop("id", None)
        d["retention_ok"] = bool(d["retention_ok"])
        d["considered"] = json.loads(d.pop("considered_json"))
        strata.append(d)
    return {"plan": dict(plan), "changes": changes, "strata": strata}


# ================================================================ 发布计划
def publish_plan(conn, plan_id):
    """发布计划：所有层达到目标或已明确记录不可补齐，且 wave 链未过期。

    单语句状态推进；任何失败整体回滚，不改变上一份已发布计划。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        plan = conn.execute(
            "SELECT * FROM panel_plan WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if plan is None:
            raise PublishError("计划不存在: %s" % plan_id)
        if plan["status"] != "draft":
            raise PublishError("计划 %s 已发布，不能重复发布" % plan_id)

        latest = conn.execute(
            "SELECT MAX(wave) w FROM panel_plan"
            " WHERE rotation_rule_id=? AND status='published'",
            (plan["rotation_rule_id"],),
        ).fetchone()["w"]
        expected = (latest or 0) + 1
        if plan["wave"] != expected:
            raise PublishError(
                "计划 wave=%d 已过期：当前应发布 wave=%d（上一份已发布计划不变）"
                % (plan["wave"], expected)
            )

        # 发布门：每层达到目标，或明确记录不可补齐/留存不足原因
        for g in conn.execute(
            "SELECT * FROM panel_stratum_gap WHERE plan_id=?", (plan_id,)
        ):
            filled = g["retained_n"] + g["joined_n"]
            if filled != g["target_n"] and not g["reason"]:
                raise PublishError(
                    "层 %s 在样 %d 未达目标 %d 且未记录不可补齐原因，不能发布"
                    % (g["stratum_code"], filled, g["target_n"])
                )
            if filled > g["target_n"]:
                raise PublishError(
                    "层 %s 在样 %d 超过目标 %d" % (g["stratum_code"], filled,
                                                  g["target_n"])
                )

        cur = conn.execute(
            "UPDATE panel_plan SET status='published', published_at=?"
            " WHERE plan_id=? AND status='draft'",
            (_now(), plan_id),
        )
        if cur.rowcount != 1:
            raise PublishError("计划 %s 发布冲突" % plan_id)
        conn.execute("COMMIT")
        return {"plan_id": plan_id, "wave": plan["wave"], "published": True}
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ================================================================ 查询接口
def list_panel_plans(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM panel_plan ORDER BY rotation_rule_id, wave, plan_id"
    )]


def current_panel(conn, rotation_rule=None):
    """当前已发布的最新一轮 panel 成员清单；未发布过则为空。"""
    if rotation_rule is None:
        plan = conn.execute(
            "SELECT * FROM panel_plan WHERE status='published'"
            " ORDER BY wave DESC, plan_id DESC LIMIT 1"
        ).fetchone()
    else:
        plan = conn.execute(
            "SELECT * FROM panel_plan WHERE status='published'"
            " AND rotation_rule_id=? ORDER BY wave DESC LIMIT 1",
            (rotation_rule,),
        ).fetchone()
    if plan is None:
        return {"plan_id": None, "wave": None, "members": []}
    members = [dict(r) for r in conn.execute(
        "SELECT pc.panel_member_id, pm.member_key, pc.stratum_code,"
        " pc.frame_id, pc.business_unit_id, pc.consecutive_waves,"
        " pc.assignment_id, pc.source_assignment_id"
        " FROM panel_change pc"
        " JOIN panel_member pm ON pm.panel_member_id=pc.panel_member_id"
        " WHERE pc.plan_id=? AND pc.decision IN ('retained','joined')"
        " ORDER BY pc.stratum_code, pc.frame_id", (plan["plan_id"],)
    )]
    return {"plan_id": plan["plan_id"], "wave": plan["wave"],
            "members": members}


def trace_panel_member(conn, identifier):
    """按 panel_member_id / business_unit_id / frame_id 追溯跨轮次历史。"""
    member = None
    for sql, param in (
        ("SELECT * FROM panel_member WHERE panel_member_id=?", identifier),
        ("SELECT * FROM panel_member WHERE member_key=?", identifier),
        ("SELECT * FROM panel_member WHERE anchor_business_unit_id=?",
         identifier),
        ("SELECT * FROM panel_member WHERE anchor_frame_id=?", identifier),
        ("SELECT * FROM panel_member WHERE member_key=?",
         "frame:%s" % identifier),
    ):
        member = conn.execute(sql, (param,)).fetchone()
        if member is not None:
            break
    if member is None:
        # 名单行 -> 业务单位 -> 成员
        bu = conn.execute(
            "SELECT business_unit_id FROM identity_map WHERE frame_id=?",
            (identifier,),
        ).fetchone()
        if bu is not None:
            member = conn.execute(
                "SELECT * FROM panel_member WHERE anchor_business_unit_id=?",
                (bu["business_unit_id"],),
            ).fetchone()
    if member is None:
        return {"identifier": identifier, "found": False,
                "member": None, "history": []}

    history = []
    for r in conn.execute(
        "SELECT pc.*, pp.status AS plan_status, pp.request_key"
        " FROM panel_change pc JOIN panel_plan pp ON pp.plan_id=pc.plan_id"
        " WHERE pc.panel_member_id=? ORDER BY pc.wave, pc.change_id",
        (member["panel_member_id"],),
    ):
        history.append(dict(r))
    current = None
    for h in reversed(history):
        if h["plan_status"] == "published":
            current = h
            break
    return {
        "identifier": identifier, "found": True,
        "member": dict(member), "history": history, "current": current,
    }
