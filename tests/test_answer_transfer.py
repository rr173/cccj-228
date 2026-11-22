# -*- coding: utf-8 -*-
"""回答转移表端到端测试：运行 `python -I tests/test_answer_transfer.py`。

覆盖：
- 只用端点计划沿 base_plan_id 回溯的已发布链（不含样本轮、同轮备选 draft、
  端点之后发布的计划）；同一端点重复初始化不清空；换端点被拒绝；
- 在样区段的相邻有效回答配对（可跨过在样但无有效答案的轮次），
  离样后回归不跨区段配对，层取计划账本而非成员当前层；
- 补入中间答案：一条长配对拆成两条短配对；作废中间答案：接回；
  另一位成员贡献同一汇总键时，更正只减掉本人计数；
- 乱序修订、晚到旧修订、同批多处更改、同号冲突/坏输入整批无效；
- 用独立推导（只读计划链账本 + 输入修订）核对每个汇总键；
- 进程重开后结果完整；用 SQL trace 证明更正只触及受影响成员区段，
  读取汇总不重跑配对。
"""

import os
import re
import sys
import tempfile
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from sample_assembler import (  # noqa: E402
    AnswerTransferError, SampleAssembler,
)
from tests import answer_fixtures as fx  # noqa: E402

PASS, FAIL = 0, 0
PM_RE = re.compile(r"PM_[0-9a-f]{16}")
INTERVIEW_TABLES = ("answer_revision", "answer_transfer_pair",
                    "answer_transfer_sample", "answer_effective")


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


# ================================================================ 场景构建
def build_panel(db_path):
    """构造样本轮 + wave1..4 已发布计划链，并额外产生同轮备选 draft。"""
    asm = SampleAssembler(db_path)
    asm.compile_and_store_rules(fx.PANEL_RULES)
    asm.import_frame_snapshot("w0", fx.FRAME_W0)
    asm.import_draws(fx.DRAWS_W0)
    for ev in fx.EVENTS_W0:
        asm.record_event(ev)
    batch0 = asm.assemble("w0")["batch_id"]
    asm.publish_batch(batch0)
    rot = asm.import_rotation_rules(fx.ROTATION)

    asm.import_frame_snapshot("w1", fx.FRAME_W1)
    plans = {}
    plans[1] = asm.generate_next_plan("REQ-W1", "w1", rotation_rule=rot)
    asm.publish_plan(plans[1]["plan_id"])

    asm.import_frame_snapshot("w2", fx.FRAME_W2)
    for ev in fx.EVENTS_W2:
        asm.record_event(ev)
    plans[2] = asm.generate_next_plan("REQ-W2", "w2", rotation_rule=rot)
    asm.publish_plan(plans[2]["plan_id"])

    asm.import_frame_snapshot("w3", fx.FRAME_W3)
    for ev in fx.EVENTS_W3:
        asm.record_event(ev)
    plans[3] = asm.generate_next_plan("REQ-W3", "w3", rotation_rule=rot)
    asm.publish_plan(plans[3]["plan_id"])

    asm.import_frame_snapshot("w4", fx.FRAME_W4)
    plans[4] = asm.generate_next_plan("REQ-W4", "w4", rotation_rule=rot)
    # 同轮其他计划：wave 4 的备选 draft（不发布）
    alt4 = asm.generate_next_plan("REQ-W4-ALT", "w4", rotation_rule=rot)
    asm.publish_plan(plans[4]["plan_id"])
    return asm, batch0, plans, alt4


def add_wave5(asm):
    """绑定之后才发布 wave 5：证明之后发布的计划不进入已绑定链。"""
    rot = asm.conn.execute(
        "SELECT rotation_rule_id FROM rotation_rule_set ORDER BY 1 LIMIT 1"
    ).fetchone()[0]
    asm.import_frame_snapshot("w5", fx.FRAME_W5)
    p5 = asm.generate_next_plan("REQ-W5", "w5", rotation_rule=rot)
    asm.publish_plan(p5["plan_id"])
    return p5


def member_by_frame(asm, plan_id, frame_id):
    row = asm.conn.execute(
        "SELECT panel_member_id FROM panel_change"
        " WHERE plan_id=? AND frame_id=?", (plan_id, frame_id)
    ).fetchone()
    return row[0]


def load_ledger(asm, plan_ids):
    """只读计划账本：链上每个在样记录的层（输入的一部分，不读转移表）。"""
    marks = ",".join("?" for _ in plan_ids)
    out = {}
    for r in asm.conn.execute(
        "SELECT plan_id, panel_member_id, stratum_code FROM panel_change"
        " WHERE decision IN ('retained','joined') AND plan_id IN (%s)" % marks,
        tuple(plan_ids),
    ):
        out[(r["plan_id"], r["panel_member_id"])] = r["stratum_code"]
    return out


def derive_expected(chain, ledger, revisions):
    """从计划链、账本与录入修订独立推导汇总（不使用转移表内部结果）。

    chain: [(plan_id, wave), ...] 时间正序
    ledger: {(plan_id, panel_member_id): stratum}
    revisions: [(plan_id, panel_member_id, revision, answer_or_None), ...]
    """
    pid_at = [pid for pid, _w in chain]
    wave_at = [w for _pid, w in chain]
    index_at = {pid: i for i, pid in enumerate(pid_at)}

    # 每个 (计划, 成员) 取最大修订号；无答案/最新作废不构成有效端点。
    best = {}
    for pid, mid, rev, answer in revisions:
        if (pid, mid) not in ledger:
            continue
        key = (pid, mid)
        if key not in best or rev > best[key][0]:
            best[key] = (rev, answer)
    valid_answer = {k: v[1] for k, v in best.items() if v[1] is not None}

    by_member = {}
    for (pid, mid), stratum in ledger.items():
        by_member.setdefault(mid, {})[index_at[pid]] = stratum

    counter = Counter()
    for mid, at_index in by_member.items():
        segments, current = [], []
        for i in range(len(chain)):
            if i in at_index:
                current.append(i)
            elif current:
                segments.append(current)
                current = []
        if current:
            segments.append(current)
        for seg in segments:
            valid = [i for i in seg
                     if (pid_at[i], mid) in valid_answer]
            for a, b in zip(valid, valid[1:]):
                key = (
                    wave_at[a], wave_at[b], at_index[a],
                    valid_answer[(pid_at[a], mid)],
                    valid_answer[(pid_at[b], mid)],
                )
                counter[key] += 1
    return counter


def summary_counter(rows):
    return Counter({
        (r["start_wave"], r["end_wave"], r["stratum_code"],
         r["from_answer"], r["to_answer"]): r["pair_count"]
        for r in rows
    })


def same_summary(asm, expected):
    return summary_counter(asm.answer_transfer_summary()) == expected


# ================================================================ 1. 绑定与链
def test_binding_and_scope(builder):
    section("1. 绑定、已发布链范围与端点不可更改")
    asm, batch0, plans, alt4 = builder
    p1, p2, p3, p4 = (plans[i]["plan_id"] for i in (1, 2, 3, 4))

    info = asm.init_answer_transfer(p4)
    check("端点绑定成功并回溯到 wave1..4",
          [n["wave"] for n in info["chain"]] == [1, 2, 3, 4], str(info["chain"]))
    check("链中不含最初的单轮抽样批次（无 wave 0）",
          all(n["wave"] >= 1 for n in info["chain"]))
    check("链中不含同轮备选 draft 计划",
          alt4["plan_id"] not in [n["plan_id"] for n in info["chain"]])
    check("链成员 7、区段 8", info["members"] == 7 and info["segments"] == 8,
          str(info))

    # 同一端点重复初始化：幂等、不清空（此时尚无修订）
    again = asm.init_answer_transfer(p4)
    check("同一端点重复初始化幂等且结果不变",
          again["idempotent"] and again["segments"] == 8
          and again["endpoint_plan_id"] == p4)

    try:
        asm.init_answer_transfer(alt4["plan_id"])
        check("未发布的端点计划被拒绝", False)
    except AnswerTransferError:
        check("未发布的端点计划被拒绝", True)

    try:
        asm.init_answer_transfer(p2)
        check("已绑定后改用其他端点被拒绝", False)
    except AnswerTransferError:
        check("已绑定后改用其他端点被拒绝", True)

    # 绑定之后发布 wave 5，再以同一端点初始化：链保持不变
    add_wave5(asm)
    after = asm.init_answer_transfer(p4)
    check("之后发布的计划不进入已绑定链",
          [n["wave"] for n in after["chain"]] == [1, 2, 3, 4],
          str(after["chain"]))
    bound = asm.answer_transfer_binding()
    check("持久化绑定链仍为 wave1..4",
          [n["wave"] for n in bound["chain"]] == [1, 2, 3, 4])
    check("wave5 的在样记录未进入转移区段",
          all(r["wave"] <= 4 for r in asm.list_answer_segments()))

    return p1, p2, p3, p4, plans


def test_chain_subset():
    section("2. 以较早计划为端点时只取到该端点")
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "sub.db")
        asm, _b0, plans, _alt = build_panel(db)
        info = asm.init_answer_transfer(plans[2]["plan_id"])
        check("端点 wave2 的链只有 wave1..2",
              [n["wave"] for n in info["chain"]] == [1, 2], str(info["chain"]))
        check("段：6 位成员各 1 个区段（BU-05 只在 w1、BU-23 只在 w2）",
              info["members"] == 6 and info["segments"] == 6, str(info))
        asm.close()


# ================================================================ 2. 基础配对
def test_baseline_pairing(asm, p1, p2, p3, p4, ledger, accepted):
    section("3. 相邻有效回答配对：跨空轮、不跨区段、层取计划账本")
    pm01 = member_by_frame(asm, p1, "A01")   # BU-01, S
    pm02 = member_by_frame(asm, p1, "A02")   # BU-02, S
    pm03 = member_by_frame(asm, p1, "A03")   # BU-03, S
    pm04 = member_by_frame(asm, p1, "B04")   # BU-04, T
    pm05 = member_by_frame(asm, p1, "B05")   # BU-05, T -> S 回归
    pm23 = member_by_frame(asm, p2, "B23")   # BU-23, T 仅一轮
    pm34 = member_by_frame(asm, p3, "B34")   # B34, T

    # 批内故意打乱顺序（后面还有更复杂的乱序批次）
    batch = [
        {"plan_id": p4, "panel_member_id": pm02, "revision": 1, "answer": 1},
        {"plan_id": p1, "panel_member_id": pm01, "revision": 1, "answer": 0},
        {"plan_id": p4, "panel_member_id": pm05, "revision": 1, "answer": 1},
        {"plan_id": p1, "panel_member_id": pm05, "revision": 1, "answer": 1},
        {"plan_id": p1, "panel_member_id": pm02, "revision": 1, "answer": 0},
        {"plan_id": p2, "panel_member_id": pm03, "revision": 1, "answer": 0},
        {"plan_id": p1, "panel_member_id": pm04, "revision": 1, "answer": 1},
        {"plan_id": p4, "panel_member_id": pm01, "revision": 1, "answer": 1},
        {"plan_id": p4, "panel_member_id": pm04, "revision": 1, "answer": 0},
        {"plan_id": p1, "panel_member_id": pm03, "revision": 1, "answer": 1},
        {"plan_id": p3, "panel_member_id": pm05, "revision": 1, "answer": 0},
        {"plan_id": p2, "panel_member_id": pm23, "revision": 1, "answer": 0},
        {"plan_id": p3, "panel_member_id": pm34, "revision": 1, "answer": 1},
        {"plan_id": p4, "panel_member_id": pm34, "revision": 1, "answer": 1},
    ]
    res = asm.submit_answer_revisions(batch)
    accepted.extend((b["plan_id"], b["panel_member_id"], b["revision"],
                     b["answer"]) for b in batch)
    check("乱序批次全部落库（14 条，顺序无关）", res["stored"] == 14, str(res))

    expected = derive_expected(
        [(p1, 1), (p2, 2), (p3, 3), (p4, 4)], ledger, accepted)
    check("汇总与独立推导完全一致", same_summary(asm, expected),
          "%s != %s" % (summary_counter(asm.answer_transfer_summary()),
                        expected))
    summary = summary_counter(asm.answer_transfer_summary())
    check("BU-01/BU-02 共享键 (1,4,S,0,1) 计数 2",
          summary[(1, 4, "S", 0, 1)] == 2, str(dict(summary)))
    check("跨过在样但无答案的 w2/w3 仍配成 (1,4)",
          (1, 4, "S", 0, 1) in summary and (1, 3, "S", 0, 1) not in summary)
    check("BU-04 的 T 层键 (1,4,T,1,0) 计数 1",
          summary[(1, 4, "T", 1, 0)] == 1)

    pm05_pairs = asm.list_answer_pairs(pm05)
    check("离样后回归的 BU-05 不跨区段配对",
          [(x["from_wave"], x["to_wave"], x["stratum_code"])
           for x in pm05_pairs] == [(3, 4, "S")], str(pm05_pairs))
    check("BU-05 回归区段的层取计划账本 S（锚定名单行是 T）",
          pm05_pairs[0]["stratum_code"] == "S")
    check("BU-23 仅一轮无配对", asm.list_answer_pairs(pm23) == [])
    check("总配对数 6", len(asm.list_answer_pairs()) == 6,
          str(len(asm.list_answer_pairs())))
    return pm01, pm02, pm03, pm04, pm05, pm23, pm34


# ================================================================ 3. 补中间答案
def test_insert_middle(asm, p2, pm01, pm02, ledger, accepted):
    section("4. 补入中间答案：一条长配对拆成两条短配对，不误删他人计数")
    batch = [{"plan_id": p2, "panel_member_id": pm01,
              "revision": 1, "answer": 1}]
    res = asm.submit_answer_revisions(batch)
    accepted.append((p2, pm01, 1, 1))
    check("更正只影响 BU-01 成员",
          res["affected_members"] == [pm01], str(res))

    summary = summary_counter(asm.answer_transfer_summary())
    check("长配对 (1,4,S,0,1) 从 2 减为 1（只减 BU-01 本人）",
          summary[(1, 4, "S", 0, 1)] == 1, str(dict(summary)))
    check("新增短配对 (1,2,S,0,1) 与 (2,4,S,1,1)",
          summary[(1, 2, "S", 0, 1)] == 1
          and summary[(2, 4, "S", 1, 1)] == 1)
    check("另一位成员 BU-02 仍贡献 (1,4,S,0,1)",
          any(x["panel_member_id"] == pm02 and x["from_wave"] == 1
              and x["to_wave"] == 4 for x in asm.list_answer_pairs()))
    expected = derive_expected(
        [(n["plan_id"], n["wave"])
         for n in asm.answer_transfer_binding()["chain"]], ledger, accepted)
    check("补中间答案后汇总与独立推导一致", same_summary(asm, expected),
          "%s != %s" % (summary, expected))


# ================================================================ 4. 作废中间
def test_void_middle(asm, p2, pm01, ledger, accepted):
    section("5. 作废中间答案：两次回答重新接回一条长配对")
    batch = [{"plan_id": p2, "panel_member_id": pm01,
              "revision": 2, "answer": None}]
    res = asm.submit_answer_revisions(batch)
    accepted.append((p2, pm01, 2, None))
    check("作废修订影响同一成员", res["affected_members"] == [pm01], str(res))

    summary = summary_counter(asm.answer_transfer_summary())
    check("接回后 (1,4,S,0,1) 恢复为 2",
          summary[(1, 4, "S", 0, 1)] == 2, str(dict(summary)))
    check("两条短配对消失（无配对键不保留）",
          (1, 2, "S", 0, 1) not in summary and (2, 4, "S", 1, 1) not in summary)
    chain = [(n["plan_id"], n["wave"])
             for n in asm.answer_transfer_binding()["chain"]]
    expected = derive_expected(chain, ledger, accepted)
    check("作废后汇总与独立推导一致", same_summary(asm, expected),
          "%s != %s" % (summary, expected))


# ================================================================ 5. 乱序/旧修订
def test_out_of_order_and_multichange(asm, p1, p2, p3, p4, pm01, pm02, pm04,
                                      pm34, ledger, accepted):
    section("6. 乱序修订、晚到旧修订、同批多处更改")
    # 同一键一次送来 rev3/rev1/rev2（顺序打乱）：只有 rev3 生效，但三条都入库
    batch = [
        {"plan_id": p3, "panel_member_id": pm01, "revision": 3, "answer": 0},
        {"plan_id": p3, "panel_member_id": pm01, "revision": 1, "answer": 1},
        {"plan_id": p3, "panel_member_id": pm01, "revision": 2, "answer": 0},
    ]
    res = asm.submit_answer_revisions(batch)
    accepted.extend([(p3, pm01, 3, 0), (p3, pm01, 1, 1), (p3, pm01, 2, 0)])
    check("乱序同键修订全部入库、只重推一次", res["stored"] == 3
          and res["affected_members"] == [pm01], str(res))
    eff = asm.conn.execute(
        "SELECT revision, answer FROM answer_effective"
        " WHERE plan_id=? AND panel_member_id=?", (p3, pm01)).fetchone()
    check("最大修订号 rev3 生效（晚到旧修订不改变结果）",
          eff["revision"] == 3 and eff["answer"] == 0, dict(eff))
    summary = summary_counter(asm.answer_transfer_summary())
    # w2 有效答案是作废（rev2=None），w1 与 w3 因此相邻；BU-05 也贡献
    # (3,4,S,0,1)，故该键合计 2。
    check("BU-01 配对变为 (1,3,S,0,0) 与 (3,4,S,0,1)（后者与 BU-05 合计 2）",
          summary[(1, 3, "S", 0, 0)] == 1
          and summary[(3, 4, "S", 0, 1)] == 2,
          str(dict(summary)))
    check("(1,4,S,0,1) 只剩 BU-02 的 1",
          summary[(1, 4, "S", 0, 1)] == 1)

    # 相同键同号同答案重送：幂等，不重推
    res = asm.submit_answer_revisions(
        [{"plan_id": p3, "panel_member_id": pm01,
          "revision": 2, "answer": 0}])
    check("同键同号同答案重送为 duplicate，不影响汇总",
          res["duplicate"] == 1 and res["stored"] == 0
          and res["affected_members"] == [], str(res))

    # 同批多处更改：BU-02 的 w4、BU-04 的 w1、B34 的 w3（顺序打乱）
    batch = [
        {"plan_id": p3, "panel_member_id": pm34, "revision": 2, "answer": 0},
        {"plan_id": p1, "panel_member_id": pm04, "revision": 2, "answer": 0},
        {"plan_id": p4, "panel_member_id": pm02, "revision": 2, "answer": 0},
        {"plan_id": p1, "panel_member_id": pm04, "revision": 1, "answer": 1},
    ]
    res = asm.submit_answer_revisions(batch)
    accepted.extend([
        (p3, pm34, 2, 0), (p1, pm04, 2, 0),
        (p4, pm02, 2, 0), (p1, pm04, 1, 1),
    ])
    check("同批三处实际更改 + 一条重复：只重推 3 个成员",
          sorted(res["affected_members"]) == sorted([pm34, pm04, pm02])
          and res["duplicate"] == 1, str(res))
    summary = summary_counter(asm.answer_transfer_summary())
    # BU-02(w4)=0 使 (1,4,S,0,1) 消失，改为 (1,4,S,0,0)；
    # BU-04(w1)=0 使 (1,4,T,1,0) 消失，改为 (1,4,T,0,0)；
    # B34(w3)=0 使 (3,4,T,1,1) 消失，改为 (3,4,T,0,1)。
    check("多处更改后旧键被移除、新键出现",
          (1, 4, "S", 0, 1) not in summary
          and summary[(1, 4, "S", 0, 0)] == 1
          and (1, 4, "T", 1, 0) not in summary
          and summary[(1, 4, "T", 0, 0)] == 1
          and (3, 4, "T", 1, 1) not in summary
          and summary[(3, 4, "T", 0, 1)] == 1, str(dict(summary)))
    chain = [(n["plan_id"], n["wave"])
             for n in asm.answer_transfer_binding()["chain"]]
    expected = derive_expected(chain, ledger, accepted)
    check("乱序/多处更改后汇总与独立推导一致", same_summary(asm, expected),
          "%s != %s" % (summary, expected))


# ================================================================ 6. 冲突/坏输入
def test_conflicts_and_bad_input(asm, p1, p3, p4, plans, alt4, pm01, pm23,
                                 ledger, accepted):
    section("7. 冲突与坏输入：整批无效，不留部分修改")
    before_summary = summary_counter(asm.answer_transfer_summary())
    before_pairs = len(asm.list_answer_pairs())
    before_rev = asm.conn.execute(
        "SELECT COUNT(*) FROM answer_revision").fetchone()[0]
    before_batches = len(asm.list_answer_batches())
    chain = [(n["plan_id"], n["wave"])
             for n in asm.answer_transfer_binding()["chain"]]
    baseline = derive_expected(chain, ledger, accepted)

    def rejected(label, batch):
        try:
            asm.submit_answer_revisions(batch)
            check(label, False)
        except AnswerTransferError:
            check(label, True)

    # 批内同键同号不同答案
    rejected("批内同号不同答案整批拒绝", [
        {"plan_id": p3, "panel_member_id": pm01, "revision": 5, "answer": 0},
        {"plan_id": p3, "panel_member_id": pm01, "revision": 5, "answer": 1},
    ])
    # 与已存修订冲突，且该修订已不是最新（rev1 < rev3）
    rejected("同号不同答案（即使不是最新修订）整批拒绝", [
        {"plan_id": p3, "panel_member_id": pm01, "revision": 1, "answer": 0},
        {"plan_id": p4, "panel_member_id": pm01, "revision": 2, "answer": 1},
    ])
    # 非链上计划（同轮备选 draft，且也已发布过 wave5）
    rejected("非绑定链计划（同轮备选 draft）整批拒绝", [
        {"plan_id": p4, "panel_member_id": pm01, "revision": 2, "answer": 1},
        {"plan_id": alt4["plan_id"], "panel_member_id": pm01,
         "revision": 1, "answer": 1},
    ])
    # 链上但该轮不在样：BU-23 在 wave4 已缺席；BU-03 在 wave3 已退出
    pm03 = member_by_frame(asm, p1, "A03")
    rejected("链上但该轮不在样（BU-23 于 w4）整批拒绝", [
        {"plan_id": p4, "panel_member_id": pm23, "revision": 1, "answer": 1},
    ])
    rejected("链上但该轮已 exited（BU-03 于 w3）整批拒绝", [
        {"plan_id": p3, "panel_member_id": pm03, "revision": 1, "answer": 1},
    ])
    # 不合法的 answer / revision / 缺字段
    rejected("answer=2 整批拒绝", [
        {"plan_id": p4, "panel_member_id": pm01, "revision": 3, "answer": 2},
    ])
    rejected("answer=True（布尔冒充整数）整批拒绝", [
        {"plan_id": p4, "panel_member_id": pm01, "revision": 3, "answer": True},
    ])
    rejected("revision=0 整批拒绝", [
        {"plan_id": p4, "panel_member_id": pm01, "revision": 0, "answer": 1},
    ])
    rejected("缺 answer 字段整批拒绝", [
        {"plan_id": p4, "panel_member_id": pm01, "revision": 3},
    ])
    # 一条合法 + 一条非法：合法那条也不能留下（原子回滚）
    rejected("批内混入非法项时合法项也不落库", [
        {"plan_id": p4, "panel_member_id": pm01, "revision": 3, "answer": 1},
        {"plan_id": p4, "panel_member_id": pm23, "revision": 3, "answer": 1},
    ])

    check("所有失败批次后汇总不变",
          summary_counter(asm.answer_transfer_summary()) == before_summary)
    check("所有失败批次后配对明细不变",
          len(asm.list_answer_pairs()) == before_pairs)
    check("所有失败批次后原始修订行数不变",
          asm.conn.execute(
              "SELECT COUNT(*) FROM answer_revision").fetchone()[0] == before_rev)
    check("失败批次不产生审计行",
          len(asm.list_answer_batches()) == before_batches)
    check("失败批次后汇总仍与输入独立推导一致",
          same_summary(asm, baseline))

    # 已发布 wave5（端点之后）：其成员不得作为修订键
    wave5_plan = asm.conn.execute(
        "SELECT plan_id FROM panel_plan WHERE wave=5 AND status='published'"
    ).fetchone()[0]
    pm_w5 = asm.conn.execute(
        "SELECT panel_member_id FROM panel_change WHERE plan_id=?"
        " AND decision IN ('retained','joined') LIMIT 1", (wave5_plan,)
    ).fetchone()[0]
    rejected("端点之后发布的计划记录整批拒绝", [
        {"plan_id": wave5_plan, "panel_member_id": pm_w5,
         "revision": 1, "answer": 1},
    ])


# ================================================================ 7. 增量范围
def test_incremental_scope(asm, p4, pm02, pm01, ledger, accepted):
    section("8. 更正只处理受影响区段（SQL trace + 明细快照）")
    before_pairs = {
        mid: asm.list_answer_pairs(mid)
        for mid in {x["panel_member_id"] for x in asm.list_answer_pairs()}
    }
    before_eff = {
        (r["plan_id"], r["panel_member_id"]): (r["revision"], r["answer"])
        for r in asm.conn.execute(
            "SELECT plan_id, panel_member_id, revision, answer"
            " FROM answer_effective")
    }

    statements = []
    asm.conn.set_trace_callback(statements.append)
    try:
        res = asm.submit_answer_revisions([
            {"plan_id": p4, "panel_member_id": pm02,
             "revision": 3, "answer": 1},
        ])
    finally:
        asm.conn.set_trace_callback(None)
    accepted.append((p4, pm02, 3, 1))
    check("只报告 BU-02 为受影响成员",
          res["affected_members"] == [pm02], str(res))

    touched = set()
    for sql in statements:
        low = sql.lower()
        if any(t in low for t in INTERVIEW_TABLES):
            touched.update(PM_RE.findall(sql))
    check("涉及访谈/配对/区段表的 SQL 只出现受影响成员",
          touched <= {pm02}, "touched=%s" % sorted(touched))
    check("未扫描无关成员 BU-01 的访谈记录", pm01 not in touched)

    for mid, rows in before_pairs.items():
        if mid == pm02:
            continue
        check("无关成员 %s 的配对明细逐行不变" % mid[:9],
              asm.list_answer_pairs(mid) == rows)
    untouched_ok = all(
        v == before_eff.get(k)
        for k, v in {
            (r["plan_id"], r["panel_member_id"]): (r["revision"], r["answer"])
            for r in asm.conn.execute(
                "SELECT plan_id, panel_member_id, revision, answer"
                " FROM answer_effective WHERE panel_member_id<>?", (pm02,))
        }.items())
    check("无关成员的有效答案不变", untouched_ok)
    check("受影响成员有效答案已更新为 rev3=1",
          tuple(asm.conn.execute(
              "SELECT revision, answer FROM answer_effective"
              " WHERE plan_id=? AND panel_member_id=?", (p4, pm02)
          ).fetchone()) == (3, 1))

    # 读取汇总不重跑配对：SQL 只触及 summary 表
    statements = []
    asm.conn.set_trace_callback(statements.append)
    try:
        asm.answer_transfer_summary()
    finally:
        asm.conn.set_trace_callback(None)
    touched_tables = [s for s in statements
                      if any(t in s.lower() for t in INTERVIEW_TABLES)]
    check("读取汇总不触碰修订/配对/区段/有效答案表",
          touched_tables == [], str(touched_tables[:3]))

    # 共享键再次验证：BU-02 改动只减自己
    summary = summary_counter(asm.answer_transfer_summary())
    expected = derive_expected(
        [(n["plan_id"], n["wave"])
         for n in asm.answer_transfer_binding()["chain"]], ledger, accepted)
    check("增量更正后汇总与独立推导一致", same_summary(asm, expected),
          "%s != %s" % (summary, expected))


# ================================================================ 8. 重开
def test_reopen(db_path, ledger, accepted):
    section("9. 进程重开：已提交结果完整可见")
    asm = SampleAssembler(db_path)
    chain = [(n["plan_id"], n["wave"])
             for n in asm.answer_transfer_binding()["chain"]]
    expected = derive_expected(chain, ledger, accepted)
    actual = summary_counter(asm.answer_transfer_summary())
    check("重开后汇总与输入独立推导一致", actual == expected,
          "%s != %s" % (actual, expected))
    # 汇总与内部配对明细自洽
    internal = Counter()
    for r in asm.list_answer_pairs():
        internal[(r["from_wave"], r["to_wave"], r["stratum_code"],
                  r["from_answer"], r["to_answer"])] += 1
    check("重开后汇总等于内部配对明细的分组计数", actual == internal)
    # 重开后同一端点仍幂等、不清空
    endpoint = chain[-1][0]
    info = asm.init_answer_transfer(endpoint)
    check("重开后同端点重复初始化不清空",
          info["idempotent"]
          and same_summary(asm, expected))
    # 重开后仍拒绝换端点
    try:
        asm.init_answer_transfer(chain[0][0])
        check("重开后换端点仍被拒绝", False)
    except AnswerTransferError:
        check("重开后换端点仍被拒绝", True)
    asm.close()


# ================================================================ 主流程
def main():
    global PASS, FAIL
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "answers.db")
        asm, _batch0, plans, alt4 = build_panel(db_path)
        p1, p2, p3, p4 = (plans[i]["plan_id"] for i in (1, 2, 3, 4))

        test_binding_and_scope((asm, _batch0, plans, alt4))
        ledger = load_ledger(asm, [p1, p2, p3, p4])
        chain = [(p1, 1), (p2, 2), (p3, 3), (p4, 4)]
        accepted = []

        pm01, pm02, pm03, pm04, pm05, pm23, pm34 = test_baseline_pairing(
            asm, p1, p2, p3, p4, ledger, accepted)
        test_insert_middle(asm, p2, pm01, pm02, ledger, accepted)
        test_void_middle(asm, p2, pm01, ledger, accepted)
        test_out_of_order_and_multichange(
            asm, p1, p2, p3, p4, pm01, pm02, pm04, pm34, ledger, accepted)
        test_conflicts_and_bad_input(
            asm, p1, p3, p4, plans, alt4, pm01, pm23, ledger, accepted)
        test_incremental_scope(asm, p4, pm02, pm01, ledger, accepted)
        asm.close()
        test_reopen(db_path, ledger, accepted)

    test_chain_subset()

    print("\n结果：%d 通过, %d 失败" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
