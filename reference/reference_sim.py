# -*- coding: utf-8 -*-
"""独立参考模拟程序。

刻意不 import sample_assembler 包，只用标准库按同一书面规范独立模拟：
  1) 资格事件状态机（时间+来源序号定序，重复幂等，逆序/非法拒绝）；
  2) 分层归类（缺失层、左闭右开数值边界、分类值）；
  3) assignment 与替补链（同层、random_rank 排序、排除条件、BU 去重、
     跨批次历史占用）；
  4) 输出每个批次的在样集合、缺口与完整候选判定，供主测试逐项核对。
"""

PENDING, ELIGIBLE, REFUSED, LOST, WITHDREW = (
    "pending", "eligible", "refused", "lost", "withdrew")
TERMINAL = {REFUSED, LOST, WITHDREW}
FAIL = {REFUSED, LOST, WITHDREW}
ALLOWED = {
    PENDING: {"eligible", "refused", "lost"},
    ELIGIBLE: {"eligible", "refused", "lost", "withdrew"},
    REFUSED: set(), LOST: set(), WITHDREW: set(),
}
NEW_STATE = {"eligible": ELIGIBLE, "refused": REFUSED,
             "lost": LOST, "withdrew": WITHDREW}


class ReferenceSimulator:
    def __init__(self, rules, missing_stratum=None):
        self.rules = rules                      # code -> dict
        self.order = sorted(rules, key=lambda c: rules[c]["ordinal"])
        self.missing_stratum = missing_stratum
        self.status = {}                        # frame_id -> state
        self.event_log = []                    # 落库事件（含拒绝）
        self.prior_sub_frames = set()
        self.prior_bus = set()
        self.batches = []

    # ------------------------------------------------------------ 分层
    def classify(self, unit):
        if unit.get("is_missing") or unit.get("stratum_value") is None:
            return self.missing_stratum
        v = str(unit["stratum_value"])
        for code in self.order:
            r = self.rules[code]
            if r["kind"] == "categorical" and v in r["values"]:
                return code
            if r["kind"] == "numeric":
                try:
                    num = float(v)
                except ValueError:
                    continue
                for lo, hi in r["ranges"]:
                    if lo <= num < hi:
                        return code
        return None

    # ------------------------------------------------------------ 事件
    def apply_event(self, ev):
        fid = ev["frame_id"]
        current = self.status.get(fid, PENDING)
        order_now = None
        # 已接受事件的最新序（拒绝事件不推进状态，也不推进序）
        for e in reversed(self.event_log):
            if e["frame_id"] == fid and e["accepted"]:
                order_now = (e["event_time"], e["source_seq"])
                break
        new_order = (str(ev["event_time"]), int(ev["source_seq"]))

        for e in self.event_log:
            if (e["event_id"] == ev["event_id"]
                    or (e["frame_id"] == fid and e["event_type"] == ev["event_type"]
                        and (e["event_time"], e["source_seq"]) == new_order)):
                # 幂等重复：不重复入流（与装配器“重复事件不重复落库”一致）
                return "duplicate", "duplicate", current

        if order_now is not None and new_order < order_now:
            self.event_log.append({**ev, "accepted": False,
                                   "reject_reason": "out_of_order"})
            return "reject", "out_of_order", current
        etype = ev["event_type"]
        if etype not in ALLOWED[current]:
            self.event_log.append({**ev, "accepted": False,
                                   "reject_reason": "illegal_transition"})
            return "reject", "illegal_transition", current
        new_state = NEW_STATE[etype]
        self.status[fid] = new_state
        self.event_log.append({**ev, "accepted": True, "reject_reason": None})
        return "accept", None, new_state

    def state_of(self, fid):
        return self.status.get(fid, PENDING)

    # ------------------------------------------------------------ 装配
    def assemble(self, batch_no, frame_units, draws, identity_map):
        """identity_map: frame_id -> business_unit_id（含显式映射与自带 BU）。"""
        by_stratum = {c: [] for c in self.order}
        for u in frame_units:
            code = self.classify(u)
            if code is None:
                raise ValueError("unclassifiable:%s" % u["frame_id"])
            by_stratum[code].append(u)

        used_frames, used_bus = set(), set(self.prior_bus)
        assignments, gaps = [], []

        for d in sorted(draws, key=lambda x: (
                self.rules[x["target_stratum"]]["ordinal"],
                x["selection_position"])):
            fid = d["frame_id"]
            state = self.state_of(fid)
            bu = identity_map.get(fid)
            assignments.append({
                "kind": "original", "frame_id": fid,
                "stratum": d["target_stratum"],
                "sample_id": d["sample_id"], "root": d["sample_id"],
                "inclusion_prob": d["inclusion_prob"],
                "status": "terminal_fail" if state in FAIL else "active",
                "random_rank": d["random_rank"],
            })
            used_frames.add(fid)
            if bu:
                used_bus.add(bu)
            if state in FAIL:
                gaps.append({
                    "stratum": d["target_stratum"], "origin": d["sample_id"],
                    "trigger_frame": fid, "status": "open",
                    "filled_by": None, "considered": [],
                })

        gap_seq = 0
        for gap in sorted(gaps, key=lambda g: (
                self.rules[g["stratum"]]["ordinal"], gap_seq)):
            gap_seq += 1
            pool = []
            for u in by_stratum[gap["stratum"]]:
                fid = u["frame_id"]
                if not u["contactable"]:
                    gap["considered"].append(
                        {"frame_id": fid, "decision": "excluded",
                         "reason": "not_contactable",
                         "random_rank": u["random_rank"]})
                elif self.state_of(fid) in FAIL:
                    gap["considered"].append(
                        {"frame_id": fid, "decision": "excluded",
                         "reason": "terminal_state:%s" % self.state_of(fid),
                         "random_rank": u["random_rank"]})
                elif fid in used_frames:
                    gap["considered"].append(
                        {"frame_id": fid, "decision": "excluded",
                         "reason": "used_in_batch",
                         "random_rank": u["random_rank"]})
                elif fid in self.prior_sub_frames:
                    gap["considered"].append(
                        {"frame_id": fid, "decision": "excluded",
                         "reason": "occupied_prior_batch",
                         "random_rank": u["random_rank"]})
                else:
                    bu = identity_map.get(fid) or u.get("business_unit_id")
                    if bu and bu in used_bus:
                        gap["considered"].append(
                            {"frame_id": fid, "decision": "excluded",
                             "reason": "business_unit_reused",
                             "random_rank": u["random_rank"]})
                    else:
                        pool.append(u)
            pool.sort(key=lambda u: (u["random_rank"], u["frame_id"]))
            gap["considered"].sort(
                key=lambda c: (c["random_rank"], c["frame_id"]))
            if not pool:
                gap["status"] = "unfillable"
                continue
            chosen = pool[0]
            for u in pool:
                gap["considered"].append({
                    "frame_id": u["frame_id"],
                    "decision": "chosen" if u is chosen else "available_not_used",
                    "reason": None, "random_rank": u["random_rank"]})
            gap["considered"].sort(
                key=lambda c: (c["random_rank"], c["frame_id"]))
            gap["status"] = "filled"
            gap["filled_by"] = chosen["frame_id"]
            assignments.append({
                "kind": "substitute", "frame_id": chosen["frame_id"],
                "stratum": gap["stratum"], "sample_id": None,
                "root": gap["origin"],
                "inclusion_prob": chosen.get("inclusion_prob") or 1.0,
                "status": "active", "random_rank": chosen["random_rank"]})
            used_frames.add(chosen["frame_id"])
            bu = identity_map.get(chosen["frame_id"])
            if bu:
                used_bus.add(bu)
            self.prior_sub_frames.add(chosen["frame_id"])
            if bu:
                self.prior_bus.add(bu)

        active = [a for a in assignments if a["status"] == "active"]
        result = {
            "batch_no": batch_no,
            "active_frames": sorted(a["frame_id"] for a in active),
            "active_by_stratum": {
                c: sorted(a["frame_id"] for a in active
                          if a["stratum"] == c)
                for c in self.order
            },
            "gaps": [
                {"origin": g["origin"], "stratum": g["stratum"],
                 "status": g["status"], "filled_by": g["filled_by"],
                 "considered": g["considered"]}
                for g in gaps
            ],
            "substitute_frames": sorted(
                a["frame_id"] for a in assignments if a["kind"] == "substitute"),
            "terminal_originals": sorted(
                a["frame_id"] for a in assignments
                if a["kind"] == "original" and a["status"] == "terminal_fail"),
        }
        self.batches.append(result)
        return result
