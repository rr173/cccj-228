# -*- coding: utf-8 -*-
"""研究交付包测试场景：三层（S1/S2/S3）与四轮已发布计划链。

样本轮（wave 0）抽中七个业务单位：

    Q1  BU-01  S1      Q2  BU-02  S1      Q3  BU-03  S1
    Q4  BU-04  S2      Q5  BU-05  S2      Q6  BU-06  S2
    Q7  BU-07  S3

计划链 wave 1..4（均已发布）的成员轨迹：

    Q1/Q2  w1..w4 S1        连续一个区段（兄弟叶 L1 的两名成员）
    Q4/Q5  w1..w4 S2        连续一个区段（兄弟叶 L2 的两名成员）
    Q7     w1..w4 S3        连续一个区段（第三层，未映射层/小子树测试）
    Q3     w1 在样 -> w2 拒绝离样 -> w3 缺席 -> w4 以新名单行回归 S1
                            首尾都“在样且有答案”，但不是同一个区段（跳轮离样）
    Q6     w1..w2 在样 -> w3 拒绝离样，w4 缺席
                            选 w1..w2 时是候选，选 w1..w4 时不是

同轮备选：wave 4 另生成一个 draft 计划（不发布、不在绑定链上），用于验证
“同轮的其他计划不能替代链上计划”。
"""

from sample_assembler import SampleAssembler

RULES = {
    "name": "delivery_rules",
    "strata": [
        {"code": "S1", "ordinal": 0, "target_n": 3, "capacity": 20,
         "field_type": "categorical", "values": ["A"]},
        {"code": "S2", "ordinal": 1, "target_n": 3, "capacity": 20,
         "field_type": "categorical", "values": ["B"]},
        {"code": "S3", "ordinal": 2, "target_n": 1, "capacity": 20,
         "field_type": "categorical", "values": ["C"]},
    ],
}

ROTATION = {
    "name": "delivery_rot",
    "frame_rule_set": "delivery_rules",
    "max_consecutive_waves": 10,
    "cooldown_waves": 1,
    "min_retention_ratio": 0.0,
    "strata": [{"code": "S1", "target_n": 3},
               {"code": "S2", "target_n": 3},
               {"code": "S3", "target_n": 1}],
}

# 两片兄弟叶：L1->S1、L2->S2（Q7 的 S3 未映射）
TREE_TWO_LEAVES = {
    "label": "G",
    "children": [
        {"label": "L1", "stratum": "S1"},
        {"label": "L2", "stratum": "S2"},
    ],
}

# 三片叶：多出 L3->S3（用于“小子树不交付”、无空组）
TREE_THREE_LEAVES = {
    "label": "G",
    "children": [
        {"label": "L1", "stratum": "S1"},
        {"label": "L2", "stratum": "S2"},
        {"label": "L3", "stratum": "S3"},
    ],
}

# 两层中间节点：G -> (P1 -> L1(S1)) / (L2(S2))
TREE_NESTED = {
    "label": "G",
    "children": [
        {"label": "P1", "children": [
            {"label": "L1", "stratum": "S1"},
        ]},
        {"label": "L2", "stratum": "S2"},
    ],
}


def _u(frame_id, version, value, rank, bu, contact=True, prob=0.5):
    return {
        "frame_id": frame_id, "frame_version": version,
        "business_unit_id": bu, "stratum_value": value,
        "is_missing": False, "contactable": contact,
        "random_rank": rank, "inclusion_prob": prob,
    }


FRAME_W0 = [
    _u("Q1", "w0", "A", 0.01, "BU-01"),
    _u("Q2", "w0", "A", 0.02, "BU-02"),
    _u("Q3", "w0", "A", 0.03, "BU-03"),
    _u("Q4", "w0", "B", 0.04, "BU-04"),
    _u("Q5", "w0", "B", 0.05, "BU-05"),
    _u("Q6", "w0", "B", 0.06, "BU-06"),
    _u("Q7", "w0", "C", 0.07, "BU-07"),
]

DRAWS_W0 = [
    {"sample_id": "D-Q1", "frame_id": "Q1", "frame_version": "w0",
     "target_stratum": "S1", "random_rank": 0.01,
     "selection_position": 1, "inclusion_prob": 0.5},
    {"sample_id": "D-Q2", "frame_id": "Q2", "frame_version": "w0",
     "target_stratum": "S1", "random_rank": 0.02,
     "selection_position": 2, "inclusion_prob": 0.5},
    {"sample_id": "D-Q3", "frame_id": "Q3", "frame_version": "w0",
     "target_stratum": "S1", "random_rank": 0.03,
     "selection_position": 3, "inclusion_prob": 0.5},
    {"sample_id": "D-Q4", "frame_id": "Q4", "frame_version": "w0",
     "target_stratum": "S2", "random_rank": 0.04,
     "selection_position": 1, "inclusion_prob": 0.5},
    {"sample_id": "D-Q5", "frame_id": "Q5", "frame_version": "w0",
     "target_stratum": "S2", "random_rank": 0.05,
     "selection_position": 2, "inclusion_prob": 0.5},
    {"sample_id": "D-Q6", "frame_id": "Q6", "frame_version": "w0",
     "target_stratum": "S2", "random_rank": 0.06,
     "selection_position": 3, "inclusion_prob": 0.5},
    {"sample_id": "D-Q7", "frame_id": "Q7", "frame_version": "w0",
     "target_stratum": "S3", "random_rank": 0.07,
     "selection_position": 1, "inclusion_prob": 0.5},
]

EVENTS_W0 = [
    {"event_id": "DW0-E%d" % i, "frame_id": "Q%d" % i,
     "event_type": "eligible",
     "event_time": "2026-11-01T09:0%d:00Z" % i, "source_seq": i}
    for i in range(1, 8)
]

# wave 1：目标已满，名单候选不会被使用
FRAME_W1 = [
    _u("W1N1", "w1", "A", 0.11, "BU-71"),
    _u("W1N2", "w1", "B", 0.12, "BU-72"),
]

# wave 2 前：Q3 拒绝离样；R3 补 S1 名额
EVENTS_W2 = [
    {"event_id": "DW2-E1", "frame_id": "Q3", "event_type": "refused",
     "event_time": "2026-11-02T09:00:00Z", "source_seq": 10},
]
FRAME_W2 = [
    _u("W2R3", "w2", "A", 0.13, "BU-53"),
    _u("W2N2", "w2", "B", 0.14, "BU-73"),
]

# wave 3 前：Q6 拒绝离样；R4 补 S2 名额
EVENTS_W3 = [
    {"event_id": "DW3-E1", "frame_id": "Q6", "event_type": "refused",
     "event_time": "2026-11-03T09:00:00Z", "source_seq": 20},
]
FRAME_W3 = [
    _u("W3R4", "w3", "B", 0.15, "BU-54"),
    _u("W3N1", "w3", "A", 0.16, "BU-74"),
]

# wave 4 前：R3 拒绝离样；Q3（BU-03）冷却到期，以新名单行回归 S1
EVENTS_W4 = [
    {"event_id": "DW4-E1", "frame_id": "W2R3", "event_type": "refused",
     "event_time": "2026-11-04T09:00:00Z", "source_seq": 30},
]
FRAME_W4 = [
    _u("W4Q3", "w4", "A", 0.17, "BU-03"),
]

# 各轮访谈答案（revision=1）。w2 / w3 不是交付选定轮，只用于证明
# “中间轮次有答案也不影响、答案缺失也不需要”。
ANSWERS_W1 = {"Q1": 0, "Q2": 1, "Q3": 1, "Q4": 1, "Q5": 0, "Q6": 0, "Q7": 1}
ANSWERS_W2 = {"Q1": 1, "Q2": 1, "Q6": 1}
ANSWERS_W3 = {"Q1": 0, "Q2": 1, "Q4": 1, "Q5": 1}
ANSWERS_W4 = {"Q1": 0, "Q2": 0, "Q3": 1, "Q4": 1, "Q5": 1, "Q7": 1}


def build_panel(db_path):
    """构造样本轮 + wave1..4 已发布计划链（+ wave4 备选 draft），绑定回答转移。

    返回 (asm, plans, alt4_plan_id)；plans = {1: plan_id, ..., 4: plan_id}。
    """
    asm = SampleAssembler(db_path)
    asm.compile_and_store_rules(RULES)
    asm.import_frame_snapshot("w0", FRAME_W0)
    asm.import_draws(DRAWS_W0)
    for ev in EVENTS_W0:
        asm.record_event(ev)
    batch0 = asm.assemble("w0")["batch_id"]
    asm.publish_batch(batch0)
    rot = asm.import_rotation_rules(ROTATION)

    plans = {}
    asm.import_frame_snapshot("w1", FRAME_W1)
    plans[1] = asm.generate_next_plan("D-REQ-W1", "w1", rotation_rule=rot)
    asm.publish_plan(plans[1]["plan_id"])

    asm.import_frame_snapshot("w2", FRAME_W2)
    for ev in EVENTS_W2:
        asm.record_event(ev)
    plans[2] = asm.generate_next_plan("D-REQ-W2", "w2", rotation_rule=rot)
    asm.publish_plan(plans[2]["plan_id"])

    asm.import_frame_snapshot("w3", FRAME_W3)
    for ev in EVENTS_W3:
        asm.record_event(ev)
    plans[3] = asm.generate_next_plan("D-REQ-W3", "w3", rotation_rule=rot)
    asm.publish_plan(plans[3]["plan_id"])

    asm.import_frame_snapshot("w4", FRAME_W4)
    for ev in EVENTS_W4:
        asm.record_event(ev)
    plans[4] = asm.generate_next_plan("D-REQ-W4", "w4", rotation_rule=rot)
    alt4 = asm.generate_next_plan("D-REQ-W4-ALT", "w4", rotation_rule=rot)
    asm.publish_plan(plans[4]["plan_id"])

    asm.init_answer_transfer(plans[4]["plan_id"])
    return asm, {k: v["plan_id"] for k, v in plans.items()}, alt4["plan_id"]


def member_of(asm, plan_id, frame_id):
    row = asm.conn.execute(
        "SELECT panel_member_id FROM panel_change WHERE plan_id=?"
        " AND frame_id=?", (plan_id, frame_id)).fetchone()
    if row is None:
        raise AssertionError("frame %s 不在计划 %s 的账本中"
                             % (frame_id, plan_id))
    return row[0]


def member_ids(asm, plans):
    """测试关心的成员锚点：{名字: panel_member_id}；并核对回归身份一致。"""
    p1, p4 = plans[1], plans[4]
    out = {"Q%d" % i: member_of(asm, p1, "Q%d" % i) for i in range(1, 8)}
    assert member_of(asm, p4, "W4Q3") == out["Q3"], "Q3 回归身份不一致"
    return out


def submit_answers(asm, plans, members):
    """录入 fixture 中声明的各轮答案（w1..w4）。"""
    batch = []
    for wave, answers in ((1, ANSWERS_W1), (2, ANSWERS_W2),
                          (3, ANSWERS_W3), (4, ANSWERS_W4)):
        for name, ans in answers.items():
            batch.append({"plan_id": plans[wave],
                          "panel_member_id": members[name],
                          "revision": 1, "answer": ans})
    return asm.submit_answer_revisions(batch)


def grant(asm, plans, members, names, waves=(1, 4)):
    for name in names:
        for wave in waves:
            asm.grant_delivery_authorization(plans[wave], members[name])


def revoke(asm, plans, members, name, wave):
    return asm.revoke_delivery_authorization(plans[wave], members[name])


def code_to_member(asm, package_id):
    """测试专用（内部核对）：包内代号 -> 真实成员名（业务单位）。"""
    return {
        r["code"]: r["member_ref"]
        for r in asm.conn.execute(
            "SELECT code, member_ref FROM delivery_member"
            " WHERE package_id=?", (package_id,))
    }


def bu_of(asm, member_ref):
    return asm.conn.execute(
        "SELECT anchor_business_unit_id FROM panel_member"
        " WHERE panel_member_id=?", (member_ref,)).fetchone()[0]
