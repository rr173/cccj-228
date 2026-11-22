# -*- coding: utf-8 -*-
"""panel 轮换独立参考模拟程序。

刻意不 import sample_assembler 包，只用标准库按同一书面规范独立模拟：
  1) 资格状态机（pending/eligible/refused/lost/withdrew，终态集合固定）；
  2) 名单分层归类（缺失层、左闭右开数值区间、分类值）；
  3) 轮换计算：上一轮成员中“仍合格、未冷却、未达连续上限”者按稳定排序
     (random_rank, frame_id) 进入留存候选并截取至层目标；退出者进入冷却
     （cooldown_waves 轮内其业务身份/名单行不得回归）；每层剩余名额从新名单
     同层候选按同一稳定序补齐，排除不可联系/终态/历史已占用/业务身份冲突/
     冷却中候选；
  4) 逐轮输出每个成员的决定、连续轮数、冷却计数、层级缺口与候选判定证据，
     供测试与装配器逐项核对。

成员身份锚定：business_unit_id 优先，否则 "frame:<frame_id>"。
"""

import math

PENDING, ELIGIBLE, REFUSED, LOST, WITHDREW = (
    "pending", "eligible", "refused", "lost", "withdrew")
FAIL = {REFUSED, LOST, WITHDREW}
ALLOWED = {
    PENDING: {"eligible", "refused", "lost"},
    ELIGIBLE: {"eligible", "refused", "lost", "withdrew"},
    REFUSED: set(), LOST: set(), WITHDREW: set(),
}
NEW_STATE = {"eligible": ELIGIBLE, "refused": REFUSED,
             "lost": LOST, "withdrew": WITHDREW}


def member_key(bu, frame_id):
    return bu if bu else "frame:%s" % frame_id


class PanelReferenceSim:
    def __init__(self, strata, rotation):
        """strata: code -> {ordinal, kind, values|ranges}；rotation:
        {targets, max_consecutive_waves, cooldown_waves, min_retention_ratio}"""
        self.strata = strata
        self.order = sorted(strata, key=lambda c: strata[c]["ordinal"])
        self.rotation = rotation
        self.status = {}                 # frame_id -> state
        self.members = {}                # member_key -> dict
        self.exits = {}                  # member_key -> (exit_wave, cooldown)
        self.joined_frames = set()
        self.wave = 0
        self.waves = []                  # 每轮已发布结果

    # ------------------------------------------------------------ 事件
    def apply_event(self, ev):
        fid = ev["frame_id"]
        current = self.status.get(fid, PENDING)
        if ev["event_type"] in ALLOWED[current]:
            self.status[fid] = NEW_STATE[ev["event_type"]]

    def state_of(self, fid):
        return self.status.get(fid, PENDING)

    # ------------------------------------------------------------ 分层
    def classify(self, unit):
        if unit.get("is_missing") or unit.get("stratum_value") is None:
            return unit.get("_missing_stratum")
        v = str(unit["stratum_value"])
        for code in self.order:
            r = self.strata[code]
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

    # ------------------------------------------------------------ 轮换基数
    def seed_wave0(self, active_members):
        """首轮基数：已发布样本批次在样成员。
        active_members: [{frame_id, business_unit_id, stratum_code}]"""
        for m in active_members:
            key = member_key(m.get("business_unit_id"), m["frame_id"])
            self.members[key] = {
                "frame_id": m["frame_id"],
                "business_unit_id": m.get("business_unit_id"),
                "stratum_code": m["stratum_code"],
                "consecutive_waves": 1,   # 样本轮计为第 1 次连续参与
            }

    # ------------------------------------------------------------ 一轮计算
    def compute_wave(self, frame_units, identity_map):
        """identity_map: frame_id -> business_unit_id。返回本轮完整结果。"""
        rot = self.rotation
        wave = self.wave + 1
        targets = rot["targets"]
        order = [c for c in self.order if c in targets]

        rank_of = {u["frame_id"]: u["random_rank"] for u in frame_units}
        # 冷却集：已发布历史中退出且剩余冷却 > 0 的成员
        cooldown_bus, cooldown_frames = set(), set()
        for key, (exit_wave, cd) in self.exits.items():
            if cd - (wave - exit_wave) > 0:
                m = self.members.get(key)
                if m is None:
                    continue
                if m["business_unit_id"]:
                    cooldown_bus.add(m["business_unit_id"])
                cooldown_frames.add(m["frame_id"])

        changes = []
        retained_pool = {c: [] for c in order}
        prev_n = {c: 0 for c in order}
        used_frames, used_bus = set(), set()

        # 上一轮成员按稳定排序逐个决定
        prev = sorted(
            self.members.values(),
            key=lambda m: (
                self.strata[m["stratum_code"]]["ordinal"]
                if m["stratum_code"] in self.strata else 999,
                rank_of.get(m["frame_id"], 0.0),
                m["frame_id"],
            ),
        )
        exited_keys = []

        def exit_member(m, reason):
            changes.append({
                "member_key": member_key(m["business_unit_id"], m["frame_id"]),
                "stratum_code": m["stratum_code"],
                "decision": "exited", "reason": reason,
                "frame_id": m["frame_id"],
                "business_unit_id": m["business_unit_id"],
                "consecutive_waves": m["consecutive_waves"],
                "cooldown_remaining": rot["cooldown_waves"],
            })
            exited_keys.append(
                member_key(m["business_unit_id"], m["frame_id"]))
            if rot["cooldown_waves"] > 0:
                if m["business_unit_id"]:
                    cooldown_bus.add(m["business_unit_id"])
                cooldown_frames.add(m["frame_id"])

        for m in prev:
            code = m["stratum_code"]
            if code not in targets:
                exit_member(m, "stratum_not_in_rotation")
                continue
            prev_n[code] += 1
            state = self.state_of(m["frame_id"])
            if state in FAIL:
                exit_member(m, "exited_%s" % state)
            elif m["consecutive_waves"] >= rot["max_consecutive_waves"]:
                exit_member(m, "max_consecutive_reached")
            else:
                retained_pool[code].append(m)

        # 留存截取
        retained_n = {}
        for code in order:
            keep, drop = (retained_pool[code][:targets[code]],
                          retained_pool[code][targets[code]:])
            for m in drop:
                exit_member(m, "exceeds_stratum_target")
            for m in keep:
                changes.append({
                    "member_key": member_key(m["business_unit_id"],
                                             m["frame_id"]),
                    "stratum_code": code,
                    "decision": "retained", "reason": "retained",
                    "frame_id": m["frame_id"],
                    "business_unit_id": m["business_unit_id"],
                    "consecutive_waves": m["consecutive_waves"] + 1,
                    "cooldown_remaining": 0,
                })
                used_frames.add(m["frame_id"])
                if m["business_unit_id"]:
                    used_bus.add(m["business_unit_id"])
            retained_n[code] = len(keep)

        # 逐层补缺
        by_stratum = {c: [] for c in self.order}
        for u in frame_units:
            code = self.classify(u)
            if code is not None:
                by_stratum[code].append(u)

        strata_out = {}
        for code in order:
            open_slots = targets[code] - retained_n[code]
            required = int(math.ceil(
                rot["min_retention_ratio"] * prev_n.get(code, 0)))
            retention_ok = retained_n[code] >= required
            considered, pool = [], []
            for u in by_stratum.get(code, []):
                fid = u["frame_id"]
                rank = u["random_rank"]
                if not u["contactable"]:
                    considered.append({"frame_id": fid, "random_rank": rank,
                                       "decision": "excluded",
                                       "reason": "not_contactable"})
                    continue
                state = self.state_of(fid)
                if state in FAIL:
                    considered.append({"frame_id": fid, "random_rank": rank,
                                       "decision": "excluded",
                                       "reason": "terminal_state:%s" % state})
                    continue
                if fid in self.joined_frames:
                    considered.append({"frame_id": fid, "random_rank": rank,
                                       "decision": "excluded",
                                       "reason": "occupied_prior_plan"})
                    continue
                if fid in used_frames:
                    considered.append({"frame_id": fid, "random_rank": rank,
                                       "decision": "excluded",
                                       "reason": "frame_used_in_plan"})
                    continue
                bu = identity_map.get(fid) or u.get("business_unit_id")
                if bu and bu in used_bus:
                    considered.append({"frame_id": fid, "random_rank": rank,
                                       "decision": "excluded",
                                       "reason": "business_unit_conflict"})
                    continue
                if (bu and bu in cooldown_bus) or fid in cooldown_frames:
                    considered.append({"frame_id": fid, "random_rank": rank,
                                       "decision": "excluded",
                                       "reason": "cooldown_active"})
                    continue
                pool.append((u, bu))
            pool.sort(key=lambda x: (x[0]["random_rank"], x[0]["frame_id"]))
            n_choose = max(open_slots, 0)
            for idx, (u, bu) in enumerate(pool):
                considered.append({
                    "frame_id": u["frame_id"],
                    "random_rank": u["random_rank"],
                    "decision": "chosen" if idx < n_choose
                    else "available_not_used",
                    "reason": None,
                })
            considered.sort(key=lambda c: (c["random_rank"], c["frame_id"]))

            joined_n = 0
            for u, bu in pool[:n_choose]:
                key = member_key(bu, u["frame_id"])
                changes.append({
                    "member_key": key, "stratum_code": code,
                    "decision": "joined", "reason": "joined_fill_gap",
                    "frame_id": u["frame_id"], "business_unit_id": bu,
                    "consecutive_waves": 1, "cooldown_remaining": 0,
                })
                used_frames.add(u["frame_id"])
                if bu:
                    used_bus.add(bu)
                joined_n += 1

            remaining_open = open_slots - joined_n
            reasons = []
            if not retention_ok:
                reasons.append("retention_below_minimum")
            if remaining_open > 0:
                reasons.append("unfillable_slots:%d" % remaining_open)
            strata_out[code] = {
                "target_n": targets[code], "prev_n": prev_n.get(code, 0),
                "retained_n": retained_n[code], "joined_n": joined_n,
                "open_slots": max(remaining_open, 0),
                "retention_required": required,
                "retention_ok": retention_ok,
                "reason": "; ".join(reasons) if reasons else None,
                "considered": considered,
            }

        return {"wave": wave, "changes": changes, "strata": strata_out,
                "_exited_keys": exited_keys}

    # ------------------------------------------------------------ 发布
    def publish(self, result):
        """发布本轮结果：推进成员状态、冷却与占用（模拟已发布账本）。"""
        wave = result["wave"]
        new_members = {}
        for key, m in self.members.items():
            new_members[key] = m
        for c in result["changes"]:
            key = c["member_key"]
            if c["decision"] == "exited":
                self.exits[key] = (wave, c["cooldown_remaining"])
                if key in new_members:
                    del new_members[key]
            else:
                new_members[key] = {
                    "frame_id": c["frame_id"],
                    "business_unit_id": c["business_unit_id"],
                    "stratum_code": c["stratum_code"],
                    "consecutive_waves": c["consecutive_waves"],
                }
                if c["decision"] == "joined":
                    self.joined_frames.add(c["frame_id"])
        self.members = new_members
        self.wave = wave
        self.waves.append(result)

    def active_panel(self):
        """当前在样 member_key 集合（已发布最后一轮）。"""
        return sorted(self.members)
