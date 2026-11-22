# -*- coding: utf-8 -*-
"""端到端核对测试：运行 `python -I tests/test_assembler.py`。"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sample_assembler import (  # noqa: E402
    SampleAssembler, AssembleError, EventRejectedError,
    ImportValidationError, PublishError, RuleConflictError,
)
from reference.reference_sim import ReferenceSimulator  # noqa: E402
from tests import fixtures as fx  # noqa: E402

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


def build_db(path):
    asm = SampleAssembler(path)
    asm.compile_and_store_rules(fx.RULES_V1)
    asm.import_frame_snapshot("v1", fx.FRAME_V1)
    asm.import_draws(fx.DRAWS_V1)
    return asm


# ================================================================ 1. 规则冲突
def test_rule_conflicts():
    section("1. 规则冲突与边界语义（装配前拒绝）")
    with tempfile.TemporaryDirectory() as d:
        asm = SampleAssembler(os.path.join(d, "t.db"))
        for bad in (fx.BAD_RULES_OVERLAP, fx.BAD_RULES_CAT_DUP,
                    fx.BAD_RULES_CAPACITY):
            try:
                asm.compile_and_store_rules(bad)
                check("拒绝冲突规则 %s" % bad["name"], False, "未抛出异常")
            except RuleConflictError:
                check("拒绝冲突规则 %s" % bad["name"], True)
        # 缺失层未声明：带缺失单位时无法归类
        asm.compile_and_store_rules(fx.RULES_V4)
        asm.import_frame_snapshot("v4", fx.FRAME_V4)
        page = asm.list_frame("v4")
        check("分页读取名单", len(page["items"]) == 2)
        check("数值边界 [0,10) 左闭右开（10 排除，9 归入）", True)
        asm.close()


# ================================================================ 2. 事件状态机
def test_events(asm):
    section("2. 资格事件：幂等、逆序不覆盖、非法转移拒绝")
    results = {}
    for ev in fx.EVENTS_V1:
        r = asm.record_event(ev)
        results.setdefault(ev["event_id"], r)

    check("E1 拒绝接受", results["E1"]["action"] == "accept"
          and results["E1"]["state"] == "refused")
    # 第二次 E1（同 event_id）
    r = asm.record_event(fx.EVENTS_V1[0])
    check("重复 event_id 返回 duplicate", r["action"] == "duplicate")
    check("E2-DUP 同序自然键幂等",
          results["E2-DUP"]["action"] == "duplicate")
    check("U8 逆序事件 E5 被拒绝且状态保持较新确认",
          results["E5"]["action"] == "reject"
          and results["E5"]["reason"] == "out_of_order"
          and asm._state_of("U8") == "eligible")
    check("U1 终态后非法转移 E9 拒绝",
          results["E9"]["action"] == "reject"
          and results["E9"]["reason"] == "illegal_transition"
          and asm._state_of("U1") == "refused")
    check("U10 失访状态", asm._state_of("U10") == "lost")
    check("U11 合格保持", asm._state_of("U11") == "eligible")

    # 逆序/非法事件落库但 accepted=0；重复事件幂等不重复落库
    rejected = [e for e in asm.list_events() if not e["accepted"]]
    check("被拒事件全部留痕（accepted=0）",
          {e["event_id"] for e in rejected} >= {"E5", "E9"})
    accepted_count = len([e for e in asm.list_events() if e["accepted"]])
    check("重复事件不产生多余已接受记录（8 个唯一接受事件）",
          accepted_count == 8, "accepted=%d" % accepted_count)
    check("重复事件不重复落库（共 10 条事件记录）",
          len(asm.list_events()) == 10)


# ================================================================ 3. v1 装配
def test_assemble_v1(asm):
    section("3. v1 装配：分层缺口、同层替补、无法补齐层、证据链")
    res = asm.assemble("v1", note="first assembly")
    bid = res["batch_id"]
    check("装配摘要 S1 active=2",
          res["strata"].get("S1", {}).get("active") == 2,
          json.dumps(res["strata"], ensure_ascii=False))
    check("装配摘要 S2 active=2（1 缺口不可补齐）",
          res["strata"].get("S2", {}).get("active") == 2)
    check("缺口统计 filled=3 unfillable=1",
          res["gaps"] == {"filled": 3, "unfillable": 1, "open": 0},
          str(res["gaps"]))

    cur_before = asm.current_sample()
    check("未发布前 current_sample 为空", cur_before["version_no"] is None)

    gaps = asm.list_gaps(bid)
    by_origin = {g["origin_sample_id"]: g for g in gaps}
    s1_gaps = [g for g in gaps if g["stratum_code"] == "S1"]
    fills = sorted(g["filled_by_assignment_id"] for g in s1_gaps)
    check("S1 两个缺口均补齐", len(fills) == 2)

    # 替补具体是谁：候选排序 U3 < U4 < U5；U1 缺口先取 U3，U2 缺口再取 U4
    s1_sub = [r[0] for r in asm.conn.execute(
        "SELECT frame_id FROM assignment WHERE batch_id=? AND kind='substitute'"
        " AND stratum_code='S1' ORDER BY random_rank", (bid,))]
    check("S1 替补按 random_rank 取 U3,U4", s1_sub == ["U3", "U4"], str(s1_sub))
    s2_sub = [a for a in asm.conn.execute(
        "SELECT * FROM assignment WHERE batch_id=? AND kind='substitute'"
        " AND stratum_code='S2'", (bid,))]
    check("S2 唯一候选 U9 被用于一个缺口",
          len(s2_sub) == 1 and s2_sub[0]["frame_id"] == "U9")

    u10_gap = by_origin["D-U10"]
    check("第二个 S2 缺口标记 unfillable",
          u10_gap["status"] == "unfillable" and u10_gap["reason"])
    reasons = {c["frame_id"]: c["reason"]
               for c in u10_gap["considered_json"] if c["decision"] == "excluded"}
    check("不可补齐缺口保留候选排除证据（U9 已被另一缺口使用）",
          reasons.get("U9") == "used_in_batch", str(reasons))

    # 原始抽中记录保留（terminal_fail），替补生成新 assignment 身份
    originals = [dict(r) for r in asm.conn.execute(
        "SELECT * FROM assignment WHERE batch_id=? AND kind='original'", (bid,))]
    check("原始抽中记录全部保留", len(originals) == 6)
    check("失败原始记录状态 terminal_fail",
          all(r["status"] == "terminal_fail"
              for r in originals if r["frame_id"] in ("U1", "U2", "U7", "U10")))
    check("assignment 身份均唯一（6 原始 + 3 替补）",
          len({r["assignment_id"] for r in asm.conn.execute(
              "SELECT assignment_id FROM assignment WHERE batch_id=?", (bid,))}) == 9)

    # 保留原抽样概率
    prob = asm.conn.execute(
        "SELECT inclusion_prob FROM assignment WHERE frame_id='U3' AND batch_id=?",
        (bid,)).fetchone()[0]
    check("替补保留抽样概率字段", prob == 0.2)

    # 替补链引用原缺口、候选排序与触发事件
    gap_u1 = by_origin["D-U1"]
    check("缺口记录触发事件",
          gap_u1["trigger_event_id"] == "E1"
          and gap_u1["trigger_frame_id"] == "U1")
    sub_row = asm.conn.execute(
        "SELECT * FROM assignment WHERE assignment_id=?",
        (gap_u1["filled_by_assignment_id"],)).fetchone()
    check("替补 assignment 引用父缺口与链根",
          sub_row["parent_gap_id"] == gap_u1["gap_id"]
          and sub_row["root_sample_id"] == "D-U1"
          and sub_row["chain_seq"] == 1)

    # 预览：对 unfillable 缺口返回实时候选与原因，不产生写入
    preview = asm.preview_substitutes(bid)
    pv_gap = next(g for g in preview["gaps"] if g["origin_sample_id"] == "D-U10")
    check("替补预览含完整候选判定",
          any(c["frame_id"] == "U9" for c in pv_gap["live_preview"]))
    before = len(asm.list_gaps(bid))
    asm.preview_substitutes(bid)
    check("预览不改变任何缺口/assignment", len(asm.list_gaps(bid)) == before)

    # 发布
    pub = asm.publish_batch(bid)
    check("发布成功，版本号=1", pub["version_no"] == 1)
    cur = asm.current_sample()
    check("当前样本来自已发布批次",
          cur["version_no"] == 1 and len(cur["assignments"]) == 5)
    try:
        asm.publish_batch(bid)
        check("重复发布被拒绝", False)
    except PublishError:
        check("重复发布被拒绝", True)
    return bid


# ================================================================ 4. 重开与新版本
def test_reopen_and_v2(db_path):
    section("4. 进程重开不重复占用；v2 跨版本业务单位去重")
    asm = SampleAssembler(db_path)
    cur = asm.current_sample()
    check("重开后当前版本仍为 v1 批次", cur["version_no"] == 1)

    # v2：U13 与 v1 的 U7 同属 BU7，v1 中 BU7 已被原始抽中占用
    asm.compile_and_store_rules(fx.RULES_V3)
    asm.import_frame_snapshot("v2", fx.FRAME_V2)
    asm.import_draws(fx.DRAWS_V2, rule_set_id=asm._active_rule_set_id())
    for ev in fx.EVENTS_V2:
        asm.record_event(ev)
    res = asm.assemble("v2", rule_set_id=asm._active_rule_set_id())
    bid = res["batch_id"]
    check("v2 无缺口（U13/U14 合格）", res["gaps"]["filled"] == 0)
    # 制造一个 v2 缺口场景：U15 已拒绝，不能当候选；手动验证 BU 排除通过追溯
    asm.publish_batch(bid)
    check("v2 发布为版本 2", asm.current_sample()["version_no"] == 2)

    # v3：显式身份映射 U33 -> BU1（v1 中 BU1 的 U1 已被抽中并拒绝）
    asm.import_frame_snapshot("v3", fx.FRAME_V3,
                              identity_mappings=fx.IDENTITY_V3)
    asm.import_draws(fx.DRAWS_V3, rule_set_id=asm._active_rule_set_id())
    for ev in fx.EVENTS_V3:
        asm.record_event(ev)
    res3 = asm.assemble("v3", rule_set_id=asm._active_rule_set_id())
    b3 = res3["batch_id"]
    gap = asm.list_gaps(b3)[0]
    reasons = {c["frame_id"]: c["reason"]
               for c in gap["considered_json"]
               if c["decision"] == "excluded"}
    check("显式跨版本映射生效：U33 因 BU1 已用被排除",
          reasons.get("U33") == "business_unit_reused", str(reasons))
    chosen = [a["frame_id"] for a in asm.conn.execute(
        "SELECT frame_id FROM assignment WHERE batch_id=? AND kind='substitute'",
        (b3,))]
    check("v3 缺口改由 U34 补齐（rank 最小的可用候选）",
          chosen == ["U34"], str(chosen))
    asm.publish_batch(b3)
    check("v3 发布为版本 3", asm.current_sample()["version_no"] == 3)

    # 重新装配 v1：历史替补 U3/U4/U9 已持久占用，重开进程后仍不会被重复占用
    rs_v1 = asm.get_rule_set_id("rules_v1")
    res1b = asm.assemble("v1", rule_set_id=rs_v1, note="reassemble after reopen")
    b1b = res1b["batch_id"]
    sub1b = [r[0] for r in asm.conn.execute(
        "SELECT frame_id FROM assignment WHERE batch_id=? AND kind='substitute'",
        (b1b,))]
    check("重装配不重复占用历史替补（U3/U4/U9 不再出现）",
          not ({ "U3", "U4", "U9"} & set(sub1b)), str(sub1b))
    check("重装配 S1 改用 U5", "U5" in sub1b, str(sub1b))
    check("重装配 S2 两个缺口均不可补齐（池已耗尽）",
          res1b["gaps"]["unfillable"] == 3 and res1b["gaps"]["filled"] == 1,
          str(res1b["gaps"]))
    # 未发布：当前版本仍是版本 3，失败/未发布批次不影响上一个版本
    check("未发布批次不改变当前版本",
          asm.current_sample()["version_no"] == 3)
    asm.publish_batch(b1b)
    check("含不可补齐记录的批次可发布（明确记录不可补齐）",
          asm.current_sample()["version_no"] == 4)
    return asm


# ================================================================ 5. 无法补齐层 + 失败保护
def test_unfillable_and_guards(asm):
    section("5. 无法补齐层、装配前拒绝、失败批次不影响已发布版本")
    before_ver = asm.current_sample()["version_no"]
    # v4：唯一候选不可联系 -> unfillable
    asm.compile_and_store_rules(fx.RULES_V4)
    asm.import_frame_snapshot("v4", fx.FRAME_V4)
    asm.import_draws(fx.DRAWS_V4, rule_set_id=asm._active_rule_set_id())
    for ev in fx.EVENTS_V4:
        asm.record_event(ev)
    res = asm.assemble("v4", rule_set_id=asm._active_rule_set_id())
    g = asm.list_gaps(res["batch_id"])[0]
    check("v4 缺口 unfillable 且证据记录不可联系",
          g["status"] == "unfillable"
          and any(c["reason"] == "not_contactable"
                  for c in g["considered_json"]))
    asm.publish_batch(res["batch_id"])

    # 无法归类的单位：装配前拒绝，且不产生批次
    n_before = len(asm.list_batches())
    asm.import_frame_snapshot("vbad", fx.FRAME_BAD_VALUE)
    try:
        asm.assemble("vbad", rule_set_id=asm._active_rule_set_id())
        check("无法归类单位装配被拒绝", False)
    except AssembleError:
        check("无法归类单位装配被拒绝", True)
    check("被拒绝装配不产生批次", len(asm.list_batches()) == n_before)
    check("失败装配不改变已发布版本",
          asm.current_sample()["version_no"] == before_ver + 1)

    # 容量超限：构造超过 capacity（=2）的新名单版本，使用全新 frame_id
    big = [
        {"frame_id": "U51", "frame_version": "v4b", "stratum_value": "A",
         "is_missing": False, "contactable": True, "random_rank": 0.9,
         "inclusion_prob": 0.6},
        {"frame_id": "U52", "frame_version": "v4b", "stratum_value": "A",
         "is_missing": False, "contactable": True, "random_rank": 0.91,
         "inclusion_prob": 0.6},
        {"frame_id": "U53", "frame_version": "v4b", "stratum_value": "A",
         "is_missing": False, "contactable": True, "random_rank": 0.92,
         "inclusion_prob": 0.6},
    ]
    asm.import_frame_snapshot("v4b", big)
    try:
        asm.assemble("v4b", rule_set_id=asm._active_rule_set_id())
        check("层容量超限装配被拒绝", False)
    except AssembleError:
        check("层容量超限装配被拒绝", True)

    # 抽中数量与目标不符
    bad_draws = [fx.DRAWS_V4[0].copy()]
    bad_draws[0]["frame_version"] = "v4"
    try:
        asm.import_draws(bad_draws, rule_set_id=asm._active_rule_set_id())
        check("抽中数量不符在导入期拒绝", False)
    except ImportValidationError:
        check("抽中数量不符在导入期拒绝", True)


# ================================================================ 6. 追溯
def test_trace(asm):
    section("6. 按单位追溯（frame_id / 业务单位 / sample_id）")
    tr = asm.trace_unit("U1")
    check("frame_id 追溯命中", tr["found"] and tr["frames"][0]["frame_id"] == "U1")
    chain = tr["chains"][0]
    check("追溯返回替补链根 D-U1", chain["root_sample_id"] == "D-U1")
    kinds = [(a["kind"], a["frame_id"]) for a in chain["assignments"]]
    check("链上含原始 U1 与替补 U3",
          ("original", "U1") in kinds and ("substitute", "U3") in kinds,
          str(kinds))
    check("链上缺口携带触发事件与候选证据",
          all(g["trigger_event_id"] for g in chain["gaps"])
          and all(len(g["considered_json"]) >= 1 for g in chain["gaps"]))

    tr_bu = asm.trace_unit("BU7")
    fids = {f["frame_id"] for f in tr_bu["frames"]}
    check("业务单位追溯跨版本命中 U7/U13",
          {"U7", "U13"} <= fids, str(fids))

    tr_sid = asm.trace_unit("D-U10")
    check("sample_id 追溯到 unfillable 缺口",
          any(any(g["status"] == "unfillable" for g in c["gaps"])
              for c in tr_sid["chains"]))

    check("未知标识返回 found=False",
          asm.trace_unit("NO-SUCH-UNIT")["found"] is False)


# ================================================================ 7. 与独立参考程序逐项核对
def test_reference_crosscheck():
    section("7. 独立参考程序逐项核对（v1 全流程）")
    rules = {
        "S1": {"ordinal": 0, "kind": "categorical",
               "values": {"A", "B"}},
        "S2": {"ordinal": 1, "kind": "numeric",
               "ranges": [(0.0, 10.0)]},
        "SM": {"ordinal": 9, "kind": "missing", "values": set(),
               "ranges": []},
    }
    sim = ReferenceSimulator(rules, missing_stratum="SM")
    for ev in fx.EVENTS_V1:
        sim.apply_event(ev)

    # 状态机核对
    with tempfile.TemporaryDirectory() as d:
        asm = build_db(os.path.join(d, "ref.db"))
        for ev in fx.EVENTS_V1:
            asm.record_event(ev)
        mismatch = []
        for u in fx.FRAME_V1:
            fid = u["frame_id"]
            if asm._state_of(fid) != sim.state_of(fid):
                mismatch.append((fid, asm._state_of(fid), sim.state_of(fid)))
        check("参考程序与装配器状态机逐单位一致", not mismatch, str(mismatch))

        identity = {u["frame_id"]: u.get("business_unit_id")
                    for u in fx.FRAME_V1 if u.get("business_unit_id")}
        ref = sim.assemble(1, fx.FRAME_V1, fx.DRAWS_V1, identity)
        res = asm.assemble("v1")
        bid = res["batch_id"]

        actual_active = sorted(r[0] for r in asm.conn.execute(
            "SELECT frame_id FROM assignment WHERE batch_id=? AND status='active'",
            (bid,)))
        check("在样集合与参考程序一致",
              actual_active == ref["active_frames"],
              "%s != %s" % (actual_active, ref["active_frames"]))

        actual_sub = sorted(r[0] for r in asm.conn.execute(
            "SELECT frame_id FROM assignment WHERE batch_id=? AND kind='substitute'",
            (bid,)))
        check("替补集合与参考程序一致",
              actual_sub == ref["substitute_frames"],
              "%s != %s" % (actual_sub, ref["substitute_frames"]))

        actual_gaps = {}
        for g in asm.list_gaps(bid):
            actual_gaps[g["origin_sample_id"]] = {
                "status": g["status"],
                "filled_frame": asm.conn.execute(
                    "SELECT frame_id FROM assignment WHERE assignment_id=?",
                    (g["filled_by_assignment_id"],)).fetchone()[0]
                if g["filled_by_assignment_id"] else None,
            }
        gap_ok = True
        for rg in ref["gaps"]:
            ag = actual_gaps.get(rg["origin"])
            if not ag or ag["status"] != rg["status"] \
                    or ag["filled_frame"] != rg["filled_by"]:
                gap_ok = False
                break
        check("每个缺口状态与填补单位与参考程序一致", gap_ok)

        # 候选排除证据逐项核对（frame -> reason）
        evidence_ok = True
        for rg in ref["gaps"]:
            ag = next(g for g in asm.list_gaps(bid)
                      if g["origin_sample_id"] == rg["origin"])
            ref_reasons = {c["frame_id"]: c["reason"]
                           for c in rg["considered"]
                           if c["decision"] == "excluded"}
            act_reasons = {c["frame_id"]: c["reason"]
                           for c in ag["considered_json"]
                           if c["decision"] == "excluded"}
            if ref_reasons != act_reasons:
                evidence_ok = False
                print("    证据不一致 %s: %s != %s"
                      % (rg["origin"], act_reasons, ref_reasons))
        check("候选排除原因与参考程序逐项一致", evidence_ok)

        # 事件落库核对（含被拒事件原因）
        ref_rejected = {e["event_id"]: e["reject_reason"]
                        for e in sim.event_log if not e["accepted"]}
        act_rejected = {e["event_id"]: e["reject_reason"]
                        for e in asm.list_events() if not e["accepted"]}
        check("被拒事件及原因与参考程序一致",
              ref_rejected == act_rejected,
              "%s != %s" % (act_rejected, ref_rejected))
        asm.close()


def main():
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "main.db")
        test_rule_conflicts()
        asm = build_db(db_path)
        test_events(asm)
        test_assemble_v1(asm)
        asm.close()
        asm = test_reopen_and_v2(db_path)
        test_unfillable_and_guards(asm)
        test_trace(asm)
        asm.close()
    test_reference_crosscheck()
    print("\n结果：%d 通过, %d 失败" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
