# -*- coding: utf-8 -*-
"""分层样本装配器核心。

接口（详见 README）：
  import_frame_snapshot / compile_and_store_rules / import_draws /
  record_event / assemble / preview_substitutes / publish_batch /
  current_sample / list_gaps / trace_unit / list_frame / list_batches

并发与持久化：
- 每个写事务以 BEGIN IMMEDIATE 立即获取保留锁；配合 WAL 与 busy_timeout，
  两个进程交错装配时，后到者阻塞后在最新数据上重试整个装配。
- 替补占用由数据库部分唯一索引 ux_assignment_substitute_frame 兜底，
  即使两个进程同时选中同一候选，也只有一个能提交，另一个整批重试，
  因此同一候选不可能被双重分配；进程退出重开后占用仍然存在。
"""

import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone

from . import eligibility as elig
from .errors import (
    AssembleError,
    EventRejectedError,
    ImportValidationError,
    PublishError,
    RuleCompileError,
)
from .rules import CompiledRuleSet, compile_rules
from .schema import initialize_db

PAGE_SIZE = 500
MAX_ASSEMBLE_ATTEMPTS = 25
CONSIDERED_LIMIT = 1000


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _new_id(prefix):
    return "%s_%s" % (prefix, uuid.uuid4().hex)


class SampleAssembler:
    # ---------------------------------------------------------------- 生命周期
    def __init__(self, db_path):
        self.db_path = db_path
        self.conn = sqlite3.connect(
            db_path, timeout=30, isolation_level=None, check_same_thread=False
        )
        self.conn.row_factory = sqlite3.Row
        # WAL 允许读写并发；busy_timeout 让锁竞争等待而不是立即报错
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=15000")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        initialize_db(self.conn)

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # ================================================================ 名单导入
    def import_frame_snapshot(self, frame_version, units, identity_mappings=None,
                              replace_version=False):
        """导入一个名单版本快照。

        units: [{frame_id, business_unit_id?, stratum_value?, is_missing?,
                 contactable, random_rank, inclusion_prob?}, ...]
        identity_mappings: 可选的显式跨版本身份映射
            [{business_unit_id, frame_id, frame_version}, ...]
        """
        if not frame_version:
            raise ImportValidationError("frame_version 不能为空")
        if not isinstance(units, list) or not units:
            raise ImportValidationError("units 必须是非空列表")

        prepared = []
        seen = set()
        for i, u in enumerate(units):
            fid = u.get("frame_id")
            if not fid:
                raise ImportValidationError("第 %d 行缺少 frame_id" % i)
            if fid in seen:
                raise ImportValidationError("快照内 frame_id 重复: %s" % fid)
            seen.add(fid)
            is_missing = bool(u.get("is_missing") or u.get("stratum_value") is None)
            contactable = bool(u.get("contactable"))
            rank = u.get("random_rank")
            if not isinstance(rank, (int, float)) or isinstance(rank, bool):
                raise ImportValidationError("单位 %s 的 random_rank 必须是数值" % fid)
            prob = u.get("inclusion_prob")
            if prob is not None:
                if not isinstance(prob, (int, float)) or not 0 < prob <= 1:
                    raise ImportValidationError(
                        "单位 %s 的 inclusion_prob 必须在 (0,1]" % fid
                    )
            prepared.append(
                (
                    fid, frame_version, u.get("business_unit_id"),
                    None if is_missing else str(u.get("stratum_value")),
                    1 if is_missing else 0,
                    1 if contactable else 0,
                    float(rank), prob, _now(),
                )
            )

        explicit_maps = []
        for m in identity_mappings or []:
            if not all(m.get(k) for k in ("business_unit_id", "frame_id", "frame_version")):
                raise ImportValidationError("身份映射字段不完整: %r" % m)
            explicit_maps.append(
                (m["business_unit_id"], m["frame_id"], m["frame_version"])
            )

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            exists = self.conn.execute(
                "SELECT 1 FROM frame_unit WHERE frame_version=? LIMIT 1",
                (frame_version,),
            ).fetchone()
            if exists:
                if not replace_version:
                    raise ImportValidationError(
                        "名单版本 %s 已存在；如需替换请显式 replace_version=True"
                        % frame_version
                    )
                referenced = self.conn.execute(
                    "SELECT 1 FROM sample_draw WHERE frame_version=? LIMIT 1",
                    (frame_version,),
                ).fetchone()
                if referenced:
                    raise ImportValidationError(
                        "名单版本 %s 已被抽样结果引用，不能替换" % frame_version
                    )
                self.conn.execute(
                    "DELETE FROM frame_unit WHERE frame_version=?", (frame_version,)
                )

            self.conn.executemany(
                "INSERT INTO frame_unit(frame_id, frame_version, business_unit_id,"
                " stratum_value, is_missing, contactable, random_rank,"
                " inclusion_prob, imported_at) VALUES (?,?,?,?,?,?,?,?,?)",
                prepared,
            )
            # 业务单位自带的身份关系自动入映射表
            auto_maps = [
                (p[2], p[0], p[1]) for p in prepared if p[2]
            ]
            for row in auto_maps + explicit_maps:
                self.conn.execute(
                    "INSERT OR IGNORE INTO identity_map"
                    "(business_unit_id, frame_id, frame_version) VALUES (?,?,?)",
                    row,
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"imported": len(prepared),
                "identity_mappings": len(set(auto_maps + explicit_maps))}

    # ================================================================ 规则编译
    def compile_and_store_rules(self, spec, activate=True):
        """编译规则、检测冲突并持久化；返回 rule_set_id。"""
        compiled = compile_rules(spec)  # 冲突在这里抛出，不写库
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            dup = self.conn.execute(
                "SELECT 1 FROM rule_set WHERE name=?", (spec["name"],)
            ).fetchone()
            if dup:
                raise RuleCompileError("规则集名称已存在: %s" % spec["name"])
            cur = self.conn.execute(
                "INSERT INTO rule_set(name, is_active, compiled_at, missing_stratum)"
                " VALUES (?,?,?,?)",
                (spec["name"], 0, _now(), compiled.missing_stratum),
            )
            rsid = cur.lastrowid
            for code in compiled.ordinal_order:
                s = compiled.strata[code]
                self.conn.execute(
                    "INSERT INTO stratum_rule(rule_set_id, stratum_code, ordinal,"
                    " target_n, capacity, kind, raw_json) VALUES (?,?,?,?,?,?,?)",
                    (rsid, code, s["ordinal"], s["target_n"], s["capacity"],
                     s["kind"], json.dumps(s["raw"], ensure_ascii=False)),
                )
                idx = 0
                if s["kind"] == "categorical":
                    for v in sorted(s["values"]):
                        self.conn.execute(
                            "INSERT INTO rule_condition(rule_set_id, stratum_code,"
                            " cond_index, kind, value) VALUES (?,?,?,'value',?)",
                            (rsid, code, idx, v),
                        )
                        idx += 1
                elif s["kind"] == "numeric":
                    for lo, hi in s["ranges"]:
                        self.conn.execute(
                            "INSERT INTO rule_condition(rule_set_id, stratum_code,"
                            " cond_index, kind, lower_inclusive, upper_exclusive)"
                            " VALUES (?,?,?,'range',?,?)",
                            (rsid, code, idx, lo, hi),
                        )
                        idx += 1
            if activate:
                self.conn.execute("UPDATE rule_set SET is_active=0")
                self.conn.execute(
                    "UPDATE rule_set SET is_active=1 WHERE rule_set_id=?", (rsid,)
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return rsid

    def _active_rule_set_id(self):
        row = self.conn.execute(
            "SELECT rule_set_id FROM rule_set WHERE is_active=1"
        ).fetchone()
        if row is None:
            raise RuleCompileError("尚无已激活的规则集")
        return row[0]

    def get_rule_set_id(self, name):
        row = self.conn.execute(
            "SELECT rule_set_id FROM rule_set WHERE name=?", (name,)
        ).fetchone()
        if row is None:
            raise RuleCompileError("规则集不存在: %s" % name)
        return row[0]

    def _load_rules(self, rule_set_id):
        rs = self.conn.execute(
            "SELECT * FROM rule_set WHERE rule_set_id=?", (rule_set_id,)
        ).fetchone()
        if rs is None:
            raise RuleCompileError("规则集不存在: %s" % rule_set_id)
        strata, order = {}, []
        for sr in self.conn.execute(
            "SELECT * FROM stratum_rule WHERE rule_set_id=? ORDER BY ordinal",
            (rule_set_id,),
        ):
            conds = self.conn.execute(
                "SELECT * FROM rule_condition WHERE rule_set_id=? AND stratum_code=?"
                " ORDER BY cond_index", (rule_set_id, sr["stratum_code"])
            ).fetchall()
            entry = {
                "target_n": sr["target_n"], "capacity": sr["capacity"],
                "kind": sr["kind"], "ordinal": sr["ordinal"],
                "raw": json.loads(sr["raw_json"]),
            }
            if sr["kind"] == "categorical":
                entry["values"] = {c["value"] for c in conds}
                entry["ranges"] = []
            elif sr["kind"] == "numeric":
                entry["ranges"] = [
                    (c["lower_inclusive"], c["upper_exclusive"]) for c in conds
                ]
                entry["values"] = set()
            else:
                entry["values"] = set()
                entry["ranges"] = []
            strata[sr["stratum_code"]] = entry
            order.append(sr["stratum_code"])
        return CompiledRuleSet(rs["name"], rs["missing_stratum"], strata, order)

    # ================================================================ 抽样结果
    def import_draws(self, draws, rule_set_id=None):
        rsid = rule_set_id or self._active_rule_set_id()
        rules = self._load_rules(rsid)
        prepared, counts, seen_frames = [], {}, set()
        for i, d in enumerate(draws):
            sid = d.get("sample_id")
            fid = d.get("frame_id")
            version = d.get("frame_version")
            stratum = d.get("target_stratum")
            if not all([sid, fid, version, stratum]):
                raise ImportValidationError("第 %d 条抽样结果字段不完整" % i)
            if stratum not in rules.strata:
                raise ImportValidationError(
                    "sample_id=%s 的目标层 %r 不在规则集中" % (sid, stratum)
                )
            pos = d.get("selection_position")
            if not isinstance(pos, int) or pos <= 0:
                raise ImportValidationError(
                    "sample_id=%s 的 selection_position 必须是正整数" % sid
                )
            rank = d.get("random_rank")
            if not isinstance(rank, (int, float)) or isinstance(rank, bool):
                raise ImportValidationError(
                    "sample_id=%s 的 random_rank 非法" % sid
                )
            prob = d.get("inclusion_prob")
            if prob is None or not 0 < prob <= 1:
                raise ImportValidationError(
                    "sample_id=%s 必须带 (0,1] 的 inclusion_prob" % sid
                )
            if fid in seen_frames:
                raise ImportValidationError(
                    "同一 frame_id 被多次抽中: %s" % fid
                )
            seen_frames.add(fid)
            counts[stratum] = counts.get(stratum, 0) + 1
            prepared.append((sid, fid, version, stratum, float(rank), pos, prob))

        # 样本数量与层目标核对（容量在规则编译期已保证 target<=capacity）
        for code, s in rules.strata.items():
            n = counts.get(code, 0)
            if n != s["target_n"]:
                raise ImportValidationError(
                    "层 %s 抽中数量 %d 与声明目标 %d 不一致（装配前拒绝）"
                    % (code, n, s["target_n"])
                )

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for sid, fid, version, stratum, rank, pos, prob in prepared:
                fr = self.conn.execute(
                    "SELECT frame_version FROM frame_unit WHERE frame_id=?", (fid,)
                ).fetchone()
                if fr is None:
                    raise ImportValidationError(
                        "sample_id=%s 引用了不存在的 frame_id=%s" % (sid, fid)
                    )
                if fr["frame_version"] != version:
                    raise ImportValidationError(
                        "sample_id=%s 的 frame_version 与名单不符" % sid
                    )
            self.conn.executemany(
                "INSERT INTO sample_draw(sample_id, frame_id, frame_version,"
                " target_stratum, random_rank, selection_position, inclusion_prob)"
                " VALUES (?,?,?,?,?,?,?)",
                prepared,
            )
            self.conn.execute("COMMIT")
        except sqlite3.IntegrityError as e:
            self.conn.execute("ROLLBACK")
            raise ImportValidationError("抽样结果唯一性冲突: %s" % e)
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"imported": len(prepared)}

    # ================================================================ 资格事件
    def record_event(self, event):
        eid = event.get("event_id")
        fid = event.get("frame_id")
        etype = event.get("event_type")
        etime = event.get("event_time")
        seq = event.get("source_seq")
        if not all([eid, fid, etype, etime is not None, seq is not None]):
            raise EventRejectedError("资格事件字段不完整", reason="bad_payload")
        if not isinstance(seq, int) or seq < 0:
            raise EventRejectedError("source_seq 必须是非负整数", reason="bad_payload")

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            frame = self.conn.execute(
                "SELECT 1 FROM frame_unit WHERE frame_id=?", (fid,)
            ).fetchone()
            if frame is None:
                raise EventRejectedError(
                    "事件 %s 引用未知 frame_id=%s" % (eid, fid),
                    reason="unknown_frame",
                )
            dup_id = self.conn.execute(
                "SELECT accepted, reject_reason FROM eligibility_event WHERE event_id=?",
                (eid,),
            ).fetchone()
            if dup_id is not None:
                self.conn.execute("ROLLBACK")
                return {"action": "duplicate", "reason": "duplicate_event_id",
                        "event_id": eid, "state": self._state_of(fid)}

            st = self.conn.execute(
                "SELECT * FROM unit_status WHERE frame_id=?", (fid,)
            ).fetchone()
            current = st["current_state"] if st else elig.STATE_PENDING
            current_order = None
            if st is not None and st["last_event_time"] is not None:
                current_order = (st["last_event_time"], st["last_source_seq"])

            action, reason, new_state = elig.decide_transition(
                current, current_order, etype, etime, seq
            )
            if action == "duplicate":
                # 自然键重复：幂等返回，不重复落库
                self.conn.execute("ROLLBACK")
                return {"action": "duplicate", "reason": "duplicate_natural_key",
                        "event_id": eid, "state": current}

            self.conn.execute(
                "INSERT INTO eligibility_event(event_id, frame_id, event_type,"
                " event_time, source_seq, accepted, reject_reason, recorded_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (eid, fid, etype, str(etime), seq,
                 1 if action == "accept" else 0, reason, _now()),
            )
            if action == "accept":
                self.conn.execute(
                    "INSERT INTO unit_status(frame_id, current_state, last_event_time,"
                    " last_source_seq, last_event_id) VALUES (?,?,?,?,?)"
                    " ON CONFLICT(frame_id) DO UPDATE SET current_state=excluded.current_state,"
                    " last_event_time=excluded.last_event_time,"
                    " last_source_seq=excluded.last_source_seq,"
                    " last_event_id=excluded.last_event_id",
                    (fid, new_state, str(etime), seq, eid),
                )
            self.conn.execute("COMMIT")
            return {"action": action, "reason": reason, "event_id": eid,
                    "state": new_state if action == "accept" else current}
        except sqlite3.IntegrityError:
            # 唯一索引兜底的并发重复事件
            self.conn.execute("ROLLBACK")
            return {"action": "duplicate", "reason": "unique_index",
                    "event_id": eid, "state": self._state_of(fid)}
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _state_of(self, frame_id):
        row = self.conn.execute(
            "SELECT current_state FROM unit_status WHERE frame_id=?", (frame_id,)
        ).fetchone()
        return row["current_state"] if row else elig.STATE_PENDING

    def list_events(self, frame_id=None):
        sql = ("SELECT event_id, frame_id, event_type, event_time, source_seq,"
               " accepted, reject_reason FROM eligibility_event")
        params = ()
        if frame_id:
            sql += " WHERE frame_id=?"
            params = (frame_id,)
        sql += " ORDER BY event_time, source_seq, event_id"
        return [dict(r) for r in self.conn.execute(sql, params)]

    # ================================================================ 分页查询
    def list_frame(self, frame_version, stratum_code=None, page_size=PAGE_SIZE,
                   after_frame_id=None, rule_set_id=None):
        """按层 keyset 分页读取名单（大名单场景）；小名单一次调用即可取完。

        层过滤下推到 SQL（缺失层按 is_missing，分类层按原始值集合，
        数值层按区间），保证过滤条件不会“吃掉”页容量。
        返回 {"items": [...], "next_after": frame_id 或 None, "has_more": bool}
        """
        rsid = rule_set_id or self._active_rule_set_id()
        rules = self._load_rules(rsid)
        sql = ("SELECT frame_id, frame_version, business_unit_id, stratum_value,"
               " is_missing, contactable, random_rank, inclusion_prob"
               " FROM frame_unit WHERE frame_version=?")
        params = [frame_version]
        if after_frame_id:
            sql += " AND frame_id > ?"
            params.append(after_frame_id)
        if stratum_code is not None:
            clause, fparams = self._stratum_sql_filter(rules, stratum_code)
            sql += " AND " + clause
            params.extend(fparams)
        sql += " ORDER BY frame_id LIMIT ?"
        params.append(int(page_size) + 1)
        rows = self.conn.execute(sql, params).fetchall()
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        items = []
        for r in rows:
            d = dict(r)
            d["resolved_stratum"] = rules.classify(
                r["stratum_value"], bool(r["is_missing"]))
            items.append(d)
        return {
            "items": items,
            "has_more": has_more,
            "next_after": rows[-1]["frame_id"] if has_more else None,
        }

    @staticmethod
    def _stratum_sql_filter(rules, stratum_code):
        """把一层规则翻译成 SQL 过滤片段；无法精确翻译时返回全量条件由调用方筛。"""
        if stratum_code not in rules.strata:
            # 未知层：不可能匹配
            return "1=0", []
        s = rules.strata[stratum_code]
        if s["kind"] == "missing":
            return "is_missing=1", []
        if s["kind"] == "categorical":
            vals = sorted(s["values"])
            placeholders = ",".join("?" for _ in vals)
            return "is_missing=0 AND stratum_value IN (%s)" % placeholders, vals
        # 数值层：区间 OR；GLOB 守卫排除非数字值（避免 CAST 成 0 误入区间）
        ors, params = [], []
        numeric_guard = (
            "stratum_value GLOB '[0-9]*'"
            " OR stratum_value GLOB '-[0-9]*'"
            " OR stratum_value GLOB '.[0-9]*'"
            " OR stratum_value GLOB '-.[0-9]*'"
        )
        for lo, hi in s["ranges"]:
            ors.append("(is_missing=0 AND (%s) AND CAST(stratum_value AS REAL) >= ?"
                       " AND CAST(stratum_value AS REAL) < ?)" % numeric_guard)
            params.extend([lo, hi])
        return "(" + " OR ".join(ors) + ")", params

    def _iter_version_frames(self, frame_version):
        """keyset 分页遍历整个版本，避免一次性加载大名单。"""
        after = None
        while True:
            page = self.list_frame(frame_version, page_size=PAGE_SIZE,
                                   after_frame_id=after)
            for item in page["items"]:
                yield item
            if not page["has_more"]:
                return
            after = page["next_after"]

    # ================================================================ 装配
    def assemble(self, frame_version, rule_set_id=None, note=None):
        """执行一次完整装配，返回 batch_id 与缺口摘要。

        校验失败（规则无法归类、容量超限、抽中数量不符）在写入前拒绝；
        并发冲突时整批重试，保证替补不被双重占用。
        """
        rsid = rule_set_id or self._active_rule_set_id()
        for attempt in range(1, MAX_ASSEMBLE_ATTEMPTS + 1):
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                result = self._assemble_tx(rsid, frame_version, note)
                self.conn.execute("COMMIT")
                result["attempts"] = attempt
                return result
            except _RetryAssemble:
                self.conn.execute("ROLLBACK")
                time.sleep(0.02 * attempt)
                continue
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        raise AssembleError(
            "装配在 %d 次并发重试后仍失败" % MAX_ASSEMBLE_ATTEMPTS
        )

    def _assemble_tx(self, rsid, frame_version, note):
        rules = self._load_rules(rsid)

        # ---- 1. 全量分层归类 + 容量校验（分页扫描） ----
        per_stratum = {c: [] for c in rules.ordinal_order}
        for row in self._iter_version_frames(frame_version):
            code = rules.classify(row["stratum_value"], bool(row["is_missing"]))
            if code is None:
                raise AssembleError(
                    "单位 %s 的层字段 %r 无法归入任何层（装配前拒绝）"
                    % (row["frame_id"], row["stratum_value"])
                )
            per_stratum[code].append(row)
        for code in rules.ordinal_order:
            cap = rules.strata[code]["capacity"]
            if len(per_stratum[code]) > cap:
                raise AssembleError(
                    "层 %s 名单单位数 %d 超过层容量 %d（装配前拒绝）"
                    % (code, len(per_stratum[code]), cap)
                )

        # ---- 2. 抽中记录与目标核对 ----
        draws = self.conn.execute(
            "SELECT * FROM sample_draw WHERE frame_version=? ORDER BY target_stratum,"
            " selection_position", (frame_version,)
        ).fetchall()
        draw_counts = {}
        for d in draws:
            draw_counts[d["target_stratum"]] = draw_counts.get(d["target_stratum"], 0) + 1
        for code, s in rules.strata.items():
            if draw_counts.get(code, 0) != s["target_n"]:
                raise AssembleError(
                    "层 %s 抽中数量 %d 与目标 %d 不符（装配前拒绝）"
                    % (code, draw_counts.get(code, 0), s["target_n"])
                )

        # ---- 3. 历史占用（进程重开后依然生效，防止重复占用替补） ----
        prior_sub_frames = {
            r[0] for r in self.conn.execute(
                "SELECT frame_id FROM assignment WHERE kind='substitute'"
            )
        }
        used_bu_history = {
            r[0] for r in self.conn.execute(
                "SELECT DISTINCT business_unit_id FROM assignment"
                " WHERE business_unit_id IS NOT NULL"
            )
        }
        bu_by_frame = {}
        for bu, fid, _ver in self.conn.execute(
            "SELECT business_unit_id, frame_id, frame_version FROM identity_map"
        ):
            bu_by_frame[fid] = bu

        cur = self.conn.execute(
            "INSERT INTO batch(rule_set_id, frame_version, status, created_at, note)"
            " VALUES (?,?, 'draft', ?, ?)",
            (rsid, frame_version, _now(), note),
        )
        batch_id = cur.lastrowid

        def state_of(fid):
            r = self.conn.execute(
                "SELECT current_state, last_event_id FROM unit_status WHERE frame_id=?",
                (fid,),
            ).fetchone()
            if r is None:
                return elig.STATE_PENDING, None
            return r["current_state"], r["last_event_id"]

        seq_counter = 0
        gaps = []          # 当前批次缺口
        used_frames = set()
        used_bus = set(used_bu_history)

        # ---- 4. 原始抽中 -> assignment（保留原始记录，不删除不覆盖） ----
        for d in draws:
            fid = d["frame_id"]
            state, last_event = state_of(fid)
            failed = elig.is_failure(state)
            bu = bu_by_frame.get(fid)
            self.conn.execute(
                "INSERT INTO assignment(assignment_id, batch_id, kind, stratum_code,"
                " frame_id, frame_version, business_unit_id, sample_id, root_sample_id,"
                " parent_gap_id, chain_seq, selection_position, random_rank,"
                " inclusion_prob, status, created_event_id)"
                " VALUES (?,?, 'original', ?,?,?,?,?,?, NULL, 0, ?,?,?, ?, ?)",
                (_new_id("A"), batch_id, d["target_stratum"], fid,
                 d["frame_version"], bu, d["sample_id"], d["sample_id"],
                 d["selection_position"], d["random_rank"], d["inclusion_prob"],
                 "terminal_fail" if failed else "active",
                 last_event if failed else None),
            )
            used_frames.add(fid)
            if bu:
                used_bus.add(bu)
            if failed:
                seq_counter += 1
                self.conn.execute(
                    "INSERT INTO gap(batch_id, stratum_code, gap_seq, origin_sample_id,"
                    " trigger_event_id, trigger_frame_id, status) VALUES (?,?,?,?,?,?,'open')",
                    (batch_id, d["target_stratum"], seq_counter, d["sample_id"],
                     last_event, fid),
                )
                gap_id = self.conn.execute(
                    "SELECT last_insert_rowid()"
                ).fetchone()[0]
                gaps.append({
                    "gap_id": gap_id, "stratum": d["target_stratum"],
                    "seq": seq_counter, "origin": d["sample_id"],
                    "trigger_frame": fid, "trigger_event": last_event,
                })

        # ---- 5. 逐层、按缺口顺序补替补 ----
        for gap in sorted(gaps, key=lambda g: (
            rules.strata[g["stratum"]]["ordinal"], g["seq"]
        )):
            considered, chosen = self._pick_candidate(
                rules, gap, per_stratum[gap["stratum"]], frame_version,
                used_frames, used_bus, prior_sub_frames, bu_by_frame, state_of,
            )
            self.conn.execute(
                "UPDATE gap SET considered_json=? WHERE gap_id=?",
                (json.dumps(considered, ensure_ascii=False), gap["gap_id"]),
            )
            if chosen is None:
                reasons = sorted({
                    c["reason"] for c in considered if c["decision"] == "excluded"
                })
                self.conn.execute(
                    "UPDATE gap SET status='unfillable', reason=? WHERE gap_id=?",
                    ("无可用候选；排除原因: %s" % ",".join(reasons or ["pool_empty"]),
                     gap["gap_id"]),
                )
                gap["status"] = "unfillable"
                continue

            # 父 assignment 的链序号
            parent_chain = self.conn.execute(
                "SELECT COALESCE(MAX(chain_seq), 0) FROM assignment"
                " WHERE root_sample_id=? AND batch_id=?",
                (gap["origin"], batch_id),
            ).fetchone()[0]
            try:
                self.conn.execute(
                    "INSERT INTO assignment(assignment_id, batch_id, kind,"
                    " stratum_code, frame_id, frame_version, business_unit_id,"
                    " sample_id, root_sample_id, parent_gap_id, chain_seq,"
                    " selection_position, random_rank, inclusion_prob, status,"
                    " created_event_id)"
                    " VALUES (?,?,'substitute',?,?,?,?, NULL, ?,?,?, 0,?,?, 'active', NULL)",
                    (_new_id("A"), batch_id, gap["stratum"], chosen["frame_id"],
                     frame_version, chosen.get("business_unit_id"), gap["origin"],
                     gap["gap_id"], parent_chain + 1, chosen["random_rank"],
                     chosen["inclusion_prob"] if chosen["inclusion_prob"] is not None
                     else 1.0),
                )
            except sqlite3.IntegrityError as e:
                # 并发下唯一索引拦截双重分配 -> 整批重试。
                # 替补插入唯一可能命中的约束就是 frame 唯一索引（batch 内
                # frame 重复已由 used_frames 前置排除），任何 IntegrityError
                # 都按竞争冲突处理，绝不带着半批数据提交。
                raise _RetryAssemble(str(e))
            sub_id = self.conn.execute(
                "SELECT assignment_id FROM assignment WHERE batch_id=? AND frame_id=?",
                (batch_id, chosen["frame_id"]),
            ).fetchone()[0]
            self.conn.execute(
                "UPDATE gap SET status='filled', filled_by_assignment_id=?"
                " WHERE gap_id=?",
                (sub_id, gap["gap_id"]),
            )
            used_frames.add(chosen["frame_id"])
            if chosen.get("business_unit_id"):
                used_bus.add(chosen["business_unit_id"])
            gap["status"] = "filled"
            gap["filled_by"] = sub_id

        # ---- 6. 组装结果（发布条件：不存在 open 缺口） ----
        open_gaps = self.conn.execute(
            "SELECT COUNT(*) FROM gap WHERE batch_id=? AND status='open'",
            (batch_id,),
        ).fetchone()[0]
        if open_gaps:
            raise AssembleError("批次 %s 仍有 %d 个 open 缺口" % (batch_id, open_gaps))

        summary = self._batch_summary(batch_id)
        return summary

    def _pick_candidate(self, rules, gap, stratum_rows, frame_version,
                        used_frames, used_bus, prior_sub_frames, bu_by_frame,
                        state_of):
        """同层候选池：按 (random_rank, frame_id) 排序，逐项记录排除证据。"""
        considered = []
        chosen = None
        pool = []
        for row in stratum_rows:
            fid = row["frame_id"]
            # 不同名单版本不进池（扫描本身已限定版本，这里保留显式语义）
            if row["frame_version"] != frame_version:
                considered.append({"frame_id": fid, "random_rank": row["random_rank"],
                                   "decision": "excluded", "reason": "wrong_version"})
                continue
            if not row["contactable"]:
                considered.append({"frame_id": fid, "random_rank": row["random_rank"],
                                   "decision": "excluded", "reason": "not_contactable"})
                continue
            state, _ = state_of(fid)
            if elig.is_failure(state):
                considered.append({"frame_id": fid, "random_rank": row["random_rank"],
                                   "decision": "excluded",
                                   "reason": "terminal_state:%s" % state})
                continue
            if fid in used_frames:
                considered.append({"frame_id": fid, "random_rank": row["random_rank"],
                                   "decision": "excluded", "reason": "used_in_batch"})
                continue
            if fid in prior_sub_frames:
                considered.append({"frame_id": fid, "random_rank": row["random_rank"],
                                   "decision": "excluded",
                                   "reason": "occupied_prior_batch"})
                continue
            bu = bu_by_frame.get(fid) or row.get("business_unit_id")
            if bu and bu in used_bus:
                considered.append({"frame_id": fid, "random_rank": row["random_rank"],
                                   "decision": "excluded",
                                   "reason": "business_unit_reused"})
                continue
            pool.append(row)

        pool.sort(key=lambda r: (r["random_rank"], r["frame_id"]))
        for row in pool:
            if chosen is None:
                chosen = row
                considered.append({
                    "frame_id": row["frame_id"], "random_rank": row["random_rank"],
                    "decision": "chosen", "reason": None,
                })
            else:
                considered.append({
                    "frame_id": row["frame_id"], "random_rank": row["random_rank"],
                    "decision": "available_not_used", "reason": None,
                })
        considered.sort(key=lambda c: (c["random_rank"], c["frame_id"]))
        truncated = len(considered) > CONSIDERED_LIMIT
        if truncated:
            considered = considered[:CONSIDERED_LIMIT]
        for c in considered:
            c["gap_id"] = gap["gap_id"]
        if truncated:
            considered.append({"gap_id": gap["gap_id"], "frame_id": None,
                               "decision": "truncated",
                               "reason": "considered_over_%d" % CONSIDERED_LIMIT})
        return considered, chosen

    # ================================================================ 替补预览
    def preview_substitutes(self, batch_id):
        """不写入的替补预览：返回批次每个缺口的实时候选排序与排除原因。"""
        batch = self.conn.execute(
            "SELECT * FROM batch WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if batch is None:
            raise AssembleError("批次不存在: %s" % batch_id)
        rules = self._load_rules(batch["rule_set_id"])
        version = batch["frame_version"]

        prior_sub_frames = {
            r[0] for r in self.conn.execute(
                "SELECT frame_id FROM assignment WHERE kind='substitute'"
            )
        }
        used_bu_history = {
            r[0] for r in self.conn.execute(
                "SELECT DISTINCT business_unit_id FROM assignment"
                " WHERE business_unit_id IS NOT NULL"
            )
        }
        bu_by_frame = {
            fid: bu for bu, fid, _v in self.conn.execute(
                "SELECT business_unit_id, frame_id, frame_version FROM identity_map"
            )
        }
        per_stratum = {c: [] for c in rules.ordinal_order}
        for row in self._iter_version_frames(version):
            code = rules.classify(row["stratum_value"], bool(row["is_missing"]))
            if code:
                per_stratum[code].append(row)

        def state_of(fid):
            r = self.conn.execute(
                "SELECT current_state, last_event_id FROM unit_status WHERE frame_id=?",
                (fid,),
            ).fetchone()
            if r is None:
                return elig.STATE_PENDING, None
            return r["current_state"], r["last_event_id"]

        used_frames = {
            r[0] for r in self.conn.execute(
                "SELECT frame_id FROM assignment WHERE batch_id=?", (batch_id,)
            )
        }
        used_bus = set(used_bu_history)
        gaps_out = []
        for g in self.conn.execute(
            "SELECT * FROM gap WHERE batch_id=? ORDER BY gap_seq", (batch_id,)
        ):
            gap = {"gap_id": g["gap_id"], "stratum": g["stratum_code"],
                   "status": g["status"], "origin_sample_id": g["origin_sample_id"],
                   "trigger_event_id": g["trigger_event_id"]}
            if g["status"] == "open":
                considered, chosen = self._pick_candidate(
                    rules, dict(gap_id=g["gap_id"], stratum=g["stratum_code"]),
                    per_stratum[g["stratum_code"]], version,
                    used_frames, used_bus, prior_sub_frames, bu_by_frame, state_of,
                )
                gap["live_preview"] = considered
                gap["would_choose"] = chosen["frame_id"] if chosen else None
            else:
                gap["live_preview"] = json.loads(g["considered_json"])
                gap["would_choose"] = g["filled_by_assignment_id"]
            gaps_out.append(gap)
        return {"batch_id": batch_id, "gaps": gaps_out}

    # ================================================================ 发布
    def publish_batch(self, batch_id):
        """发布批次：所有层达到目标或已明确记录不可补齐，且无 open 缺口。

        失败不改变已发布版本（单条 published_version 行，原子替换）。
        """
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            batch = self.conn.execute(
                "SELECT * FROM batch WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise PublishError("批次不存在: %s" % batch_id)
            if batch["status"] == "published":
                raise PublishError("批次 %s 已发布" % batch_id)

            open_gaps = self.conn.execute(
                "SELECT COUNT(*) FROM gap WHERE batch_id=? AND status='open'",
                (batch_id,),
            ).fetchone()[0]
            if open_gaps:
                raise PublishError(
                    "批次 %s 存在 %d 个 open 缺口，不能发布" % (batch_id, open_gaps)
                )
            rules = self._load_rules(batch["rule_set_id"])
            # 防御性检查：任何层在样数不得超过目标
            for code, s in rules.strata.items():
                active_n = self.conn.execute(
                    "SELECT COUNT(*) FROM assignment WHERE batch_id=? AND stratum_code=?"
                    " AND status='active'", (batch_id, code)
                ).fetchone()[0]
                if active_n > s["target_n"]:
                    raise PublishError(
                        "层 %s 在样 %d 超过目标 %d" % (code, active_n, s["target_n"])
                    )

            prev = self.conn.execute(
                "SELECT version_no FROM published_version WHERE id=1"
            ).fetchone()
            next_version = (prev["version_no"] + 1) if prev else 1
            self.conn.execute(
                "INSERT INTO published_version(id, batch_id, version_no, published_at)"
                " VALUES (1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET"
                " batch_id=excluded.batch_id, version_no=excluded.version_no,"
                " published_at=excluded.published_at",
                (batch_id, next_version, _now()),
            )
            self.conn.execute(
                "UPDATE batch SET status='published', published_at=? WHERE batch_id=?",
                (_now(), batch_id),
            )
            self.conn.execute("COMMIT")
            return {"batch_id": batch_id, "version_no": next_version}
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ================================================================ 查询接口
    def current_sample(self):
        """返回当前已发布版本的在样 assignment；未发布过则为空。"""
        pv = self.conn.execute(
            "SELECT * FROM published_version WHERE id=1"
        ).fetchone()
        if pv is None:
            return {"version_no": None, "batch_id": None, "assignments": []}
        rows = self.conn.execute(
            "SELECT * FROM assignment WHERE batch_id=? AND status='active'"
            " ORDER BY stratum_code, chain_seq, selection_position",
            (pv["batch_id"],),
        ).fetchall()
        return {
            "version_no": pv["version_no"], "batch_id": pv["batch_id"],
            "assignments": [dict(r) for r in rows],
        }

    def list_gaps(self, batch_id=None):
        sql = ("SELECT g.*, b.status AS batch_status FROM gap g"
               " JOIN batch b ON b.batch_id=g.batch_id")
        params = ()
        if batch_id is not None:
            sql += " WHERE g.batch_id=?"
            params = (batch_id,)
        sql += " ORDER BY g.batch_id, g.gap_seq"
        out = []
        for r in self.conn.execute(sql, params):
            d = dict(r)
            d["considered_json"] = json.loads(d["considered_json"])
            out.append(d)
        return out

    def list_batches(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM batch ORDER BY batch_id"
        )]

    def _batch_summary(self, batch_id):
        rows = self.conn.execute(
            "SELECT stratum_code, status, COUNT(*) n FROM assignment"
            " WHERE batch_id=? GROUP BY stratum_code, status", (batch_id,)
        ).fetchall()
        strata = {}
        for r in rows:
            strata.setdefault(r["stratum_code"], {})[r["status"]] = r["n"]
        gaps = {"filled": 0, "unfillable": 0, "open": 0}
        for r in self.conn.execute(
            "SELECT status, COUNT(*) n FROM gap WHERE batch_id=? GROUP BY status",
            (batch_id,),
        ):
            gaps[r["status"]] = r["n"]
        return {"batch_id": batch_id, "strata": strata, "gaps": gaps}

    def trace_unit(self, identifier):
        """按 frame_id / business_unit_id / sample_id 追溯单位全链路。"""
        fids = set()
        fr = self.conn.execute(
            "SELECT frame_id FROM frame_unit WHERE frame_id=?", (identifier,)
        ).fetchall()
        fids.update(r[0] for r in fr)
        for r in self.conn.execute(
            "SELECT frame_id FROM identity_map WHERE business_unit_id=?",
            (identifier,),
        ):
            fids.add(r[0])
        for r in self.conn.execute(
            "SELECT frame_id FROM sample_draw WHERE sample_id=?", (identifier,)
        ):
            fids.add(r[0])
        if not fids:
            return {"identifier": identifier, "found": False,
                    "frames": [], "chains": []}

        frames, chain_roots = [], set()
        for fid in sorted(fids):
            fu = self.conn.execute(
                "SELECT * FROM frame_unit WHERE frame_id=?", (fid,)
            ).fetchone()
            st = self.conn.execute(
                "SELECT * FROM unit_status WHERE frame_id=?", (fid,)
            ).fetchone()
            events = [dict(r) for r in self.conn.execute(
                "SELECT * FROM eligibility_event WHERE frame_id=?"
                " ORDER BY event_time, source_seq", (fid,)
            )]
            mappings = [dict(r) for r in self.conn.execute(
                "SELECT * FROM identity_map WHERE frame_id=?", (fid,)
            )]
            draws = [dict(r) for r in self.conn.execute(
                "SELECT * FROM sample_draw WHERE frame_id=?", (fid,)
            )]
            for r in self.conn.execute(
                "SELECT root_sample_id FROM assignment WHERE frame_id=?", (fid,)
            ):
                chain_roots.add(r[0])
            frames.append({
                "frame_id": fid,
                "frame": dict(fu) if fu else None,
                "identity_mappings": mappings,
                "status": dict(st) if st else {"current_state": elig.STATE_PENDING},
                "events": events,
                "draws": draws,
            })

        chains = []
        for root in sorted(chain_roots):
            chain_assignments = [dict(r) for r in self.conn.execute(
                "SELECT * FROM assignment WHERE root_sample_id=?"
                " ORDER BY batch_id, chain_seq", (root,)
            )]
            chain_gaps = []
            for a in chain_assignments:
                if a["parent_gap_id"]:
                    g = self.conn.execute(
                        "SELECT * FROM gap WHERE gap_id=?", (a["parent_gap_id"],)
                    ).fetchone()
                    if g:
                        d = dict(g)
                        d["considered_json"] = json.loads(d["considered_json"])
                        chain_gaps.append(d)
            # 根上未被替补填补的缺口也要进链
            for g in self.conn.execute(
                "SELECT * FROM gap WHERE origin_sample_id=? ORDER BY batch_id, gap_seq",
                (root,),
            ):
                if not any(c["gap_id"] == g["gap_id"] for c in chain_gaps):
                    d = dict(g)
                    d["considered_json"] = json.loads(d["considered_json"])
                    chain_gaps.append(d)
            chains.append({
                "root_sample_id": root,
                "assignments": chain_assignments,
                "gaps": sorted(chain_gaps, key=lambda x: (x["batch_id"], x["gap_seq"])),
            })
        return {"identifier": identifier, "found": True,
                "frames": frames, "chains": chains}


class _RetryAssemble(Exception):
    """内部信号：并发唯一约束冲突，整批装配重试。"""
