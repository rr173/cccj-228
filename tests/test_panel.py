# -*- coding: utf-8 -*-
"""panel 轮换端到端测试：运行 `python -I tests/test_panel.py`。

手工构造样本轮（wave 0）+ 三轮计划（wave 1..3）、两层样本，覆盖：
成员连续达到上限、退出后的冷却、资格状态变化、留存比例不足、
候选与历史业务单位冲突、每层无法补齐、相同请求键重试、发布中断；
并用独立参考模拟器逐轮核对 panel_member、留存/退出/新加入决定、
连续轮数、冷却计数、缺口原因与变更账本。
"""

import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from sample_assembler import (  # noqa: E402
    PanelError, PublishError, RuleCompileError, SampleAssembler,
)
from reference.panel_reference_sim import PanelReferenceSim  # noqa: E402
from tests import panel_fixtures as fx  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (name, detail))


def section(title):
    print("\n=== %s ===" % title)


SIM_STRATA = {
    "SA": {"ordinal": 0, "kind": "categorical", "values": {"A", "B"}},
    "SB": {"ordinal": 1, "kind": "numeric", "ranges": [(0.0, 10.0)]},
}
SIM_ROTATION = {
    "targets": {"SA": 2, "SB": 2},
    "max_consecutive_waves": 2,
    "cooldown_waves": 1,
    "min_retention_ratio": 0.5,
}
IDENTITY = {}  # frame_id -> business_unit_id（全部名单版本）
for _units in (fx.FRAME_W0, fx.FRAME_W1, fx.FRAME_W2, fx.FRAME_W3):
    for _u in _units:
        if _u["business_unit_id"]:
            IDENTITY[_u["frame_id"]] = _u["business_unit_id"]


def compare_wave(wave_no, preview, sim_result):
    """逐成员、逐层、逐候选证据核对装配器与参考模拟器。"""
    ok = True
    # 1) 变更账本：member_key -> (decision, reason, consecutive, cooldown, frame)
    sim_changes = {c["member_key"]: c for c in sim_result["changes"]}
    asm_changes = {c["member_key"]: c for c in preview["changes"]}
    if set(sim_changes) != set(asm_changes):
        check("wave %d 账本成员集合一致" % wave_no, False,
              "%s != %s" % (sorted(asm_changes), sorted(sim_changes)))
        return
    for key, sc in sim_changes.items():
        ac = asm_changes[key]
        fields = ("decision", "reason", "consecutive_waves",
                  "cooldown_remaining", "frame_id", "stratum_code")
        for f in fields:
            if ac[f] != sc[f]:
                ok = False
                print("    wave %d 成员 %s 字段 %s: %r != %r"
                      % (wave_no, key, f, ac[f], sc[f]))
    check("wave %d 逐成员决定/连续轮数/冷却计数一致（%d 条账本）"
          % (wave_no, len(sim_changes)), ok)

    # 2) 层级缺口
    asm_strata = {s["stratum_code"]: s for s in preview["strata"]}
    ok = True
    for code, ss in sim_result["strata"].items():
        as_ = asm_strata[code]
        for f in ("target_n", "prev_n", "retained_n", "joined_n",
                  "open_slots", "retention_required", "retention_ok"):
            if as_[f] != ss[f]:
                ok = False
                print("    wave %d 层 %s 字段 %s: %r != %r"
                      % (wave_no, code, f, as_[f], ss[f]))
        # 缺口原因：模拟器的每个原因标记都必须出现在装配器记录中
        if ss["reason"]:
            for token in ss["reason"].split("; "):
                if not (as_["reason"] and token in as_["reason"]):
                    ok = False
                    print("    wave %d 层 %s 原因缺少 %r（实际 %r）"
                          % (wave_no, code, token, as_["reason"]))
        elif as_["reason"]:
            ok = False
            print("    wave %d 层 %s 多出原因 %r" % (wave_no, code,
                                                     as_["reason"]))
    check("wave %d 层级缺口与原因一致" % wave_no, ok)

    # 3) 候选判定证据（frame -> decision/reason）
    ok = True
    for code, ss in sim_result["strata"].items():
        sim_ev = {c["frame_id"]: (c["decision"], c["reason"])
                  for c in ss["considered"]}
        asm_ev = {c["frame_id"]: (c["decision"], c["reason"])
                  for c in asm_strata[code]["considered"]}
        if sim_ev != asm_ev:
            ok = False
            print("    wave %d 层 %s 证据不一致: %s != %s"
                  % (wave_no, code, asm_ev, sim_ev))
    check("wave %d 候选判定证据逐项一致" % wave_no, ok)


# ================================================================ 1. 规则导入
def test_rotation_rule_import():
    section("1. 轮换规则导入校验")
    with tempfile.TemporaryDirectory() as d:
        asm = SampleAssembler(os.path.join(d, "t.db"))
        asm.compile_and_store_rules(fx.PANEL_FRAME_RULES)

        bad_specs = [
            ("缺 name", {"frame_rule_set": "panel_frame_rules",
                         "max_consecutive_waves": 2, "cooldown_waves": 1,
                         "min_retention_ratio": 0.5,
                         "strata": [{"code": "SA", "target_n": 2}]}),
            ("未知层规则集", {"name": "b1", "frame_rule_set": "nope",
                              "max_consecutive_waves": 2, "cooldown_waves": 1,
                              "min_retention_ratio": 0.5,
                              "strata": [{"code": "SA", "target_n": 2}]}),
            ("连续轮数非法", {"name": "b2", "frame_rule_set": "panel_frame_rules",
                              "max_consecutive_waves": 0, "cooldown_waves": 1,
                              "min_retention_ratio": 0.5,
                              "strata": [{"code": "SA", "target_n": 2}]}),
            ("冷却轮数非法", {"name": "b3", "frame_rule_set": "panel_frame_rules",
                              "max_consecutive_waves": 2, "cooldown_waves": -1,
                              "min_retention_ratio": 0.5,
                              "strata": [{"code": "SA", "target_n": 2}]}),
            ("留存比例越界", {"name": "b4", "frame_rule_set": "panel_frame_rules",
                              "max_consecutive_waves": 2, "cooldown_waves": 1,
                              "min_retention_ratio": 1.5,
                              "strata": [{"code": "SA", "target_n": 2}]}),
            ("目标层不存在", {"name": "b5", "frame_rule_set": "panel_frame_rules",
                              "max_consecutive_waves": 2, "cooldown_waves": 1,
                              "min_retention_ratio": 0.5,
                              "strata": [{"code": "SX", "target_n": 2}]}),
            ("目标超容量", {"name": "b6", "frame_rule_set": "panel_frame_rules",
                            "max_consecutive_waves": 2, "cooldown_waves": 1,
                            "min_retention_ratio": 0.5,
                            "strata": [{"code": "SA", "target_n": 99}]}),
        ]
        for label, spec in bad_specs:
            try:
                asm.import_rotation_rules(spec)
                check("拒绝非法轮换规则（%s）" % label, False)
            except (RuleCompileError, PanelError):
                check("拒绝非法轮换规则（%s）" % label, True)

        rot_id = asm.import_rotation_rules(fx.ROTATION_RULES)
        check("合法轮换规则导入成功", isinstance(rot_id, int))
        try:
            asm.import_rotation_rules(fx.ROTATION_RULES)
            check("重名轮换规则被拒绝", False)
        except RuleCompileError:
            check("重名轮换规则被拒绝", True)

        # 无基数时不能生成计划
        asm.import_frame_snapshot("w1", fx.FRAME_W1)
        try:
            asm.generate_next_plan("REQ-X", "w1", rotation_rule=rot_id)
            check("无已发布基数时生成被拒绝", False)
        except PanelError:
            check("无已发布基数时生成被拒绝", True)
        asm.close()


# ================================================================ 2. 主流程
def build_wave0(db_path, sim):
    """样本轮：导入名单/抽样/事件，装配并发布，返回 (asm, batch_id)。"""
    asm = SampleAssembler(db_path)
    asm.compile_and_store_rules(fx.PANEL_FRAME_RULES)
    asm.import_frame_snapshot("w0", fx.FRAME_W0)
    asm.import_draws(fx.DRAWS_W0)
    for ev in fx.EVENTS_W0:
        asm.record_event(ev)
        sim.apply_event(ev)
    res = asm.assemble("w0", note="wave0 sample")
    check("wave0 装配无缺口", res["gaps"] == {"filled": 0, "unfillable": 0,
                                              "open": 0}, str(res["gaps"]))
    pub = asm.publish_batch(res["batch_id"])
    check("wave0 批次发布", pub["version_no"] == 1)
    cur = asm.current_sample()
    check("wave0 在样 4 人", len(cur["assignments"]) == 4)
    sim.seed_wave0([{"frame_id": a["frame_id"],
                    "business_unit_id": a["business_unit_id"],
                    "stratum_code": a["stratum_code"]}
                   for a in cur["assignments"]])
    return asm, res["batch_id"]


def main():
    global PASS, FAIL
    sim = PanelReferenceSim(SIM_STRATA, SIM_ROTATION)

    test_rotation_rule_import()

    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "panel.db")
        asm, batch0 = build_wave0(db_path, sim)
        rot_id = asm.import_rotation_rules(fx.ROTATION_RULES)

        # ------------------------------------------------------ wave 1
        section("2. wave 1：拒绝/撤回退出、BU 冲突、冷却、新加入")
        asm.import_frame_snapshot("w1", fx.FRAME_W1)
        for ev in fx.EVENTS_W1:
            asm.record_event(ev)
            sim.apply_event(ev)
        r1 = asm.generate_next_plan("REQ-W1", "w1", rotation_rule=rot_id,
                                    note="wave 1")
        check("wave 1 计划为 draft", r1["status"] == "draft")
        check("wave 1 wave 编号=1", r1["wave"] == 1)
        check("wave 1 决定统计 retained=2 exited=2 joined=2",
              r1["decisions"] == {"retained": 2, "exited": 2, "joined": 2},
              str(r1["decisions"]))

        sim_w1 = sim.compute_wave(fx.FRAME_W1, IDENTITY)
        compare_wave(1, asm.preview_plan_changes(r1["plan_id"]), sim_w1)

        # 账本字段：wave、来源 assignment、连续轮数、冷却计数、原因
        pv1 = asm.preview_plan_changes(r1["plan_id"])
        by_frame = {c["frame_id"]: c for c in pv1["changes"]}
        check("F02 带原因退出（exited_refused，冷却 1）",
              by_frame["F02"]["decision"] == "exited"
              and by_frame["F02"]["reason"] == "exited_refused"
              and by_frame["F02"]["cooldown_remaining"] == 1)
        check("F07 带原因退出（exited_withdrew）",
              by_frame["F07"]["reason"] == "exited_withdrew")
        check("F01 留存连续轮数 1->2",
              by_frame["F01"]["decision"] == "retained"
              and by_frame["F01"]["consecutive_waves"] == 2)
        check("F12 新加入连续轮数 1",
              by_frame["F12"]["decision"] == "joined"
              and by_frame["F12"]["consecutive_waves"] == 1)
        # 来源 assignment 链接到 wave0 的 assignment
        src = by_frame["F01"]["source_assignment_id"]
        row = asm.conn.execute(
            "SELECT frame_id, batch_id FROM assignment WHERE assignment_id=?",
            (src,)).fetchone()
        check("留存账本来源 assignment 指向 wave0 批次",
              row is not None and row["frame_id"] == "F01"
              and row["batch_id"] == batch0)
        check("新加入账本有本轮 assignment 身份",
              by_frame["F12"]["assignment_id"]
              and by_frame["F12"]["assignment_id"].startswith("PA_"))
        check("退出账本无本轮 assignment",
              by_frame["F02"]["assignment_id"] is None)

        # 候选证据
        ev_sa = {c["frame_id"]: c["reason"]
                 for s in pv1["strata"] if s["stratum_code"] == "SA"
                 for c in s["considered"] if c["decision"] == "excluded"}
        check("F11 与历史业务单位冲突被排除",
              ev_sa.get("F11") == "business_unit_conflict", str(ev_sa))
        check("F13 业务单位冷却中被排除",
              ev_sa.get("F13") == "cooldown_active", str(ev_sa))
        ev_sb = {c["frame_id"]: c["reason"]
                 for s in pv1["strata"] if s["stratum_code"] == "SB"
                 for c in s["considered"] if c["decision"] == "excluded"}
        check("F17 冷却中、F18 不可联系被排除",
              ev_sb.get("F17") == "cooldown_active"
              and ev_sb.get("F18") == "not_contactable", str(ev_sb))

        # 幂等：相同请求键重复计算
        n_plans = len(asm.list_panel_plans())
        n_changes = asm.conn.execute(
            "SELECT COUNT(*) FROM panel_change").fetchone()[0]
        r1b = asm.generate_next_plan("REQ-W1", "w1", rotation_rule=rot_id)
        check("相同请求键返回同一计划（幂等）",
              r1b["plan_id"] == r1["plan_id"] and r1b["idempotent"])
        check("重复计算不产生第二个计划",
              len(asm.list_panel_plans()) == n_plans)
        check("重复计算不重复占用候选（账本行数不变）",
              asm.conn.execute(
                  "SELECT COUNT(*) FROM panel_change").fetchone()[0]
              == n_changes)

        # 预览只读
        before = asm.conn.execute(
            "SELECT COUNT(*) FROM panel_change").fetchone()[0]
        asm.preview_plan_changes(r1["plan_id"])
        check("预览成员变更不产生写入",
              asm.conn.execute(
                  "SELECT COUNT(*) FROM panel_change").fetchone()[0] == before)

        pub1 = asm.publish_plan(r1["plan_id"])
        check("wave 1 发布成功", pub1["published"] and pub1["wave"] == 1)
        sim.publish(sim_w1)
        cur_panel = asm.current_panel()
        check("wave 1 发布后 panel 为 F01/F12/F06/F16",
              sorted(m["frame_id"] for m in cur_panel["members"])
              == ["F01", "F06", "F12", "F16"],
              str([m["frame_id"] for m in cur_panel["members"]]))
        check("单轮样本表不受 panel 影响（仍是 wave0 版本）",
              asm.current_sample()["version_no"] == 1)

        # ------------------------------------------------------ wave 2
        section("3. wave 2：连续上限退出、冷却到期回归、stale 发布失败")
        asm.import_frame_snapshot("w2", fx.FRAME_W2)
        r2 = asm.generate_next_plan("REQ-W2", "w2", rotation_rule=rot_id)
        check("wave 2 wave 编号=2（从已发布计划继续）", r2["wave"] == 2)
        # 备选计划：看到 REQ-W2 的占用后另选候选
        r2alt = asm.generate_next_plan("REQ-W2-ALT", "w2",
                                       rotation_rule=rot_id)
        check("备选计划同为 wave 2 draft",
              r2alt["wave"] == 2 and r2alt["status"] == "draft")
        alt_joined = {c["frame_id"] for c in
                      asm.preview_plan_changes(r2alt["plan_id"])["changes"]
                      if c["decision"] == "joined"}
        main_joined = {c["frame_id"] for c in
                       asm.preview_plan_changes(r2["plan_id"])["changes"]
                       if c["decision"] == "joined"}
        check("备选计划不重复占用主计划候选",
              not (alt_joined & main_joined),
              "%s vs %s" % (alt_joined, main_joined))
        check("主计划候选 F22/F26，备选改取 F21",
              main_joined == {"F22", "F26"} and alt_joined == {"F21"},
              "%s vs %s" % (main_joined, alt_joined))

        sim_w2 = sim.compute_wave(fx.FRAME_W2, IDENTITY)
        compare_wave(2, asm.preview_plan_changes(r2["plan_id"]), sim_w2)

        pv2 = asm.preview_plan_changes(r2["plan_id"])
        by_frame2 = {c["frame_id"]: c for c in pv2["changes"]}
        check("F01/F06 连续达到上限退出",
              by_frame2["F01"]["reason"] == "max_consecutive_reached"
              and by_frame2["F01"]["consecutive_waves"] == 2
              and by_frame2["F06"]["reason"] == "max_consecutive_reached")
        # wave 2 账本来源链接 wave 1 计划产生的 assignment（从已发布账本继续）
        w1_assign = {c["frame_id"]: c["assignment_id"]
                     for c in pv1["changes"] if c["assignment_id"]}
        check("wave 2 留存来源 = wave 1 计划的 assignment 身份",
              by_frame2["F12"]["source_assignment_id"] == w1_assign["F12"]
              and by_frame2["F16"]["source_assignment_id"] == w1_assign["F16"])
        check("wave 2 退出来源 = wave 1 计划的 assignment 身份",
              by_frame2["F01"]["source_assignment_id"] == w1_assign["F01"])
        check("F22（BU-02）冷却到期后回归",
              by_frame2["F22"]["decision"] == "joined"
              and by_frame2["F22"]["consecutive_waves"] == 1)
        # 回归成员保持同一 panel_member 身份
        m_f02 = asm.trace_panel_member("F02")["member"]
        m_f22 = asm.trace_panel_member("F22")["member"]
        check("F02 与 F22 共享同一 panel_member 身份（BU-02）",
              m_f02["panel_member_id"] == m_f22["panel_member_id"])

        pub2 = asm.publish_plan(r2["plan_id"])
        check("wave 2 发布成功", pub2["wave"] == 2)
        sim.publish(sim_w2)
        # 发布中断/失败：stale 计划发布被拒绝，上一份已发布计划不变
        try:
            asm.publish_plan(r2alt["plan_id"])
            check("过期（stale）计划发布被拒绝", False)
        except PublishError:
            check("过期（stale）计划发布被拒绝", True)
        latest = [p for p in asm.list_panel_plans() if p["status"] == "published"]
        check("发布失败不改变上一份已发布计划",
              len(latest) == 2 and latest[-1]["plan_id"] == r2["plan_id"])
        try:
            asm.publish_plan(r2["plan_id"])
            check("重复发布被拒绝", False)
        except PublishError:
            check("重复发布被拒绝", True)

        # ------------------------------------------------------ 重开数据库
        section("4. 进程重开：幂等重试不重复占用，wave 3 从账本继续")
        asm.close()
        asm = SampleAssembler(db_path)
        n_changes = asm.conn.execute(
            "SELECT COUNT(*) FROM panel_change").fetchone()[0]
        r2re = asm.generate_next_plan("REQ-W2", "w2", rotation_rule=rot_id)
        check("重开后相同请求键返回同一计划",
              r2re["plan_id"] == r2["plan_id"] and r2re["idempotent"])
        check("重开后重试不重复占用候选",
              asm.conn.execute(
                  "SELECT COUNT(*) FROM panel_change").fetchone()[0]
              == n_changes)
        check("重开后已发布计划链不变",
              [p["plan_id"] for p in asm.list_panel_plans()
               if p["status"] == "published"] == [r1["plan_id"],
                                                  r2["plan_id"]])

        # ------------------------------------------------------ wave 3
        asm.import_frame_snapshot("w3", fx.FRAME_W3)
        for ev in fx.EVENTS_W3:
            asm.record_event(ev)
            sim.apply_event(ev)
        r3 = asm.generate_next_plan("REQ-W3", "w3", rotation_rule=rot_id)
        check("wave 3 wave 编号=3", r3["wave"] == 3)

        sim_w3 = sim.compute_wave(fx.FRAME_W3, IDENTITY)
        compare_wave(3, asm.preview_plan_changes(r3["plan_id"]), sim_w3)

        pv3 = asm.preview_plan_changes(r3["plan_id"])
        by_frame3 = {c["frame_id"]: c for c in pv3["changes"]}
        check("F26 资格状态变化后带原因退出（exited_refused）",
              by_frame3["F26"]["reason"] == "exited_refused")
        check("F12/F16 连续达到上限退出",
              by_frame3["F12"]["reason"] == "max_consecutive_reached"
              and by_frame3["F16"]["reason"] == "max_consecutive_reached")
        sb3 = next(s for s in pv3["strata"] if s["stratum_code"] == "SB")
        check("SB 层无法补齐（open_slots=2 且明确记录原因）",
              sb3["open_slots"] == 2 and sb3["reason"]
              and "unfillable_slots:2" in sb3["reason"], str(sb3))
        check("SB 留存比例不足被明确记录",
              not sb3["retention_ok"]
              and "retention_below_minimum" in sb3["reason"])
        ev_sb3 = {c["frame_id"]: c["reason"] for c in sb3["considered"]
                  if c["decision"] == "excluded"}
        check("SB 候选排除原因：F36 不可联系、F37 冷却中",
              ev_sb3.get("F36") == "not_contactable"
              and ev_sb3.get("F37") == "cooldown_active", str(ev_sb3))

        pub3 = asm.publish_plan(r3["plan_id"])
        check("含不可补齐层的计划可发布", pub3["published"])
        sim.publish(sim_w3)
        cur3 = asm.current_panel()
        check("wave 3 发布后 panel 为 F22/F31（SB 空缺）",
              sorted(m["frame_id"] for m in cur3["members"])
              == ["F22", "F31"],
              str([m["frame_id"] for m in cur3["members"]]))
        check("在样集合与参考模拟器一致",
              sorted(m["frame_id"] for m in cur3["members"])
              == sorted(sim.members[k]["frame_id"] for k in sim.members))

        # ------------------------------------------------------ 追溯
        section("5. 按 panel_member 追溯跨轮次历史")
        tr = asm.trace_panel_member("BU-02")
        check("BU-02 追溯命中", tr["found"])
        hist = [(h["wave"], h["decision"], h["reason"]) for h in tr["history"]]
        check("BU-02 跨轮历史：wave1 退出 -> wave2 回归 -> wave3 留存",
              hist == [(1, "exited", "exited_refused"),
                       (2, "joined", "joined_fill_gap"),
                       (3, "retained", "retained")], str(hist))
        check("BU-02 历史账本不覆盖（3 条独立轮次记录）",
              len({h["wave"] for h in tr["history"]}) == 3)
        check("BU-02 当前状态为 wave3 留存",
              tr["current"]["wave"] == 3
              and tr["current"]["decision"] == "retained")
        tr_pm = asm.trace_panel_member(tr["member"]["panel_member_id"])
        check("panel_member_id 追溯等价", tr_pm["found"]
              and tr_pm["member"]["member_key"] == "BU-02")
        tr_frame = asm.trace_panel_member("F22")
        check("frame_id 追溯命中同一成员",
              tr_frame["found"] and tr_frame["member"]["panel_member_id"]
              == tr["member"]["panel_member_id"])
        check("未知标识返回 found=False",
              asm.trace_panel_member("NO-SUCH")["found"] is False)
        # 账本只增不改：三轮已发布 + 备选 draft 账本全部保留
        total_changes = asm.conn.execute(
            "SELECT COUNT(*) FROM panel_change").fetchone()[0]
        check("各轮账本全部保留（W1=6, W2=6, W2-ALT=5, W3=5，共 22 条）",
              total_changes == 22, str(total_changes))

        # ------------------------------------------------------ 发布门
        section("6. 发布门：未达标且无原因记录的计划不能发布")
        # 生成 wave 4 draft 后篡改其层记录（模拟中断/损坏）：缺口原因清空、
        # joined 清零 -> 不再满足“达标或明确记录不可补齐”
        asm.import_frame_snapshot("w3b", fx.FRAME_W3B)
        r4 = asm.generate_next_plan("REQ-W4", "w3b", rotation_rule=rot_id)
        asm.conn.execute(
            "UPDATE panel_stratum_gap SET reason=NULL, joined_n=0"
            " WHERE plan_id=? AND stratum_code='SB'", (r4["plan_id"],))
        asm.conn.commit()
        try:
            asm.publish_plan(r4["plan_id"])
            check("未达标且无原因记录的计划发布被拒绝", False)
        except PublishError:
            check("未达标且无原因记录的计划发布被拒绝", True)
        latest = [p for p in asm.list_panel_plans() if p["status"] == "published"]
        check("发布门失败不改变上一份已发布计划",
              latest[-1]["plan_id"] == r3["plan_id"])
        asm.close()

    # ------------------------------------------------------ 并发幂等
    section("7. 双进程相同请求键：只产生一个计划、不重复占用")
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "conc.db")
        marker_dir = os.path.join(d, "markers")
        os.makedirs(marker_dir)
        asm = SampleAssembler(db_path)
        asm.compile_and_store_rules(fx.CONC_FRAME_RULES)
        asm.import_frame_snapshot("cw0", fx.CONC_FRAME_W0)
        asm.import_draws(fx.CONC_DRAWS_W0)
        for ev in fx.CONC_EVENTS_W0:
            asm.record_event(ev)
        res = asm.assemble("cw0")
        asm.publish_batch(res["batch_id"])
        for ev in fx.CONC_EVENTS_W1:
            asm.record_event(ev)
        asm.import_frame_snapshot("cw1", fx.CONC_FRAME_W1)
        asm.import_rotation_rules(fx.CONC_ROTATION)
        asm.close()

        env = dict(os.environ, PYTHONPATH=ROOT)
        procs = []
        for token in ("p1", "p2"):
            procs.append(subprocess.Popen(
                [sys.executable, "-I",
                 os.path.join(HERE, "_panel_worker.py"),
                 db_path, "CONC-REQ", "cw1", marker_dir, token],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True))
        outputs = []
        for p in procs:
            out, err = p.communicate(timeout=60)
            if p.returncode != 0:
                print("worker 失败:\n%s" % err)
                FAIL += 1
            outputs.append(out.strip())
            print("  " + out.strip())

        asm = SampleAssembler(db_path)
        plans = asm.list_panel_plans()
        check("两个进程只产生一个计划", len(plans) == 1, str(len(plans)))
        joined = [r[0] for r in asm.conn.execute(
            "SELECT frame_id FROM panel_change WHERE decision='joined'")]
        check("候选只被占用一次（P3 joined，无重复）",
              joined == ["P3"], str(joined))
        plan_ids = {line.split(" plan=")[1].split(" ")[0]
                    for line in outputs if " plan=" in line}
        check("两进程返回同一 plan_id", len(plan_ids) == 1, str(plan_ids))
        pub = asm.publish_plan(plans[0]["plan_id"])
        check("并发计划可发布", pub["published"])
        asm.close()

    print("\n结果：%d 通过, %d 失败" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
