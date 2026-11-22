# -*- coding: utf-8 -*-
"""回答转移测试场景：两层（S / T 均为分类）与四轮已发布计划链。

样本轮（wave 0）抽中五个业务单位：BU-01、BU-02、BU-03 在 S，BU-04、BU-05 在 T。
计划链 wave 1..4 的成员轨迹（用于验证区段语义）：

    BU-01  w1..w4 (S)          连续一个区段（主配对用例）
    BU-02  w1..w4 (S)          连续一个区段，可贡献与 BU-01 相同的汇总键
    BU-03  w1..w2 (S)          w3 前拒绝离样
    BU-04  w1..w4 (T)          连续一个区段（T 层配对）
    BU-05  w1 (T) -> w2 拒绝离样 -> w3..w4 (S)   两个区段，不跨段配对；
                               回归后的层取自计划账本（S），不是其锚定名单行（T）
    BU-23  w2 (T) 仅一轮，w3 前离样                  无配对

wave 5 在绑定之后才发布（另有同轮备选 draft），用于证明之后发布的计划与
同轮其他计划都不进入已绑定链；最初的 wave 0 抽样批次也不进入。
"""

PANEL_RULES = {
    "name": "answer_rules",
    "strata": [
        {"code": "S", "ordinal": 0, "target_n": 3, "capacity": 20,
         "field_type": "categorical", "values": ["A"]},
        {"code": "T", "ordinal": 1, "target_n": 2, "capacity": 20,
         "field_type": "categorical", "values": ["B"]},
    ],
}

ROTATION = {
    "name": "answer_rot",
    "frame_rule_set": "answer_rules",
    "max_consecutive_waves": 10,
    "cooldown_waves": 1,
    "min_retention_ratio": 0.0,
    "strata": [{"code": "S", "target_n": 3}, {"code": "T", "target_n": 2}],
}


def _u(frame_id, version, value, rank, contact=True, bu=None, prob=0.5):
    return {
        "frame_id": frame_id, "frame_version": version,
        "business_unit_id": bu,
        "stratum_value": None if value is None else str(value),
        "is_missing": value is None, "contactable": contact,
        "random_rank": rank, "inclusion_prob": prob,
    }


FRAME_W0 = [
    _u("A01", "w0", "A", 0.01, bu="BU-01"),
    _u("A02", "w0", "A", 0.02, bu="BU-02"),
    _u("A03", "w0", "A", 0.03, bu="BU-03"),
    _u("B04", "w0", "B", 0.04, bu="BU-04"),
    _u("B05", "w0", "B", 0.05, bu="BU-05"),
]

DRAWS_W0 = [
    {"sample_id": "D-A01", "frame_id": "A01", "frame_version": "w0",
     "target_stratum": "S", "random_rank": 0.01,
     "selection_position": 1, "inclusion_prob": 0.5},
    {"sample_id": "D-A02", "frame_id": "A02", "frame_version": "w0",
     "target_stratum": "S", "random_rank": 0.02,
     "selection_position": 2, "inclusion_prob": 0.5},
    {"sample_id": "D-A03", "frame_id": "A03", "frame_version": "w0",
     "target_stratum": "S", "random_rank": 0.03,
     "selection_position": 3, "inclusion_prob": 0.5},
    {"sample_id": "D-B04", "frame_id": "B04", "frame_version": "w0",
     "target_stratum": "T", "random_rank": 0.04,
     "selection_position": 1, "inclusion_prob": 0.5},
    {"sample_id": "D-B05", "frame_id": "B05", "frame_version": "w0",
     "target_stratum": "T", "random_rank": 0.05,
     "selection_position": 2, "inclusion_prob": 0.5},
]

EVENTS_W0 = [
    {"event_id": "AW0-E1", "frame_id": "A01", "event_type": "eligible",
     "event_time": "2026-11-01T09:00:00Z", "source_seq": 1},
    {"event_id": "AW0-E2", "frame_id": "A02", "event_type": "eligible",
     "event_time": "2026-11-01T09:01:00Z", "source_seq": 2},
    {"event_id": "AW0-E3", "frame_id": "A03", "event_type": "eligible",
     "event_time": "2026-11-01T09:02:00Z", "source_seq": 3},
    {"event_id": "AW0-E4", "frame_id": "B04", "event_type": "eligible",
     "event_time": "2026-11-01T09:03:00Z", "source_seq": 4},
    {"event_id": "AW0-E5", "frame_id": "B05", "event_type": "eligible",
     "event_time": "2026-11-01T09:04:00Z", "source_seq": 5},
]

# wave 1：w0 五人全部留存，名单候选不会被使用
FRAME_W1 = [
    _u("A11", "w1", "A", 0.11, bu="BU-11"),
    _u("B12", "w1", "B", 0.12, bu="BU-12"),
]

# wave 2 前：BU-05 拒绝离样（T 空出一个名额，由 B23 新加入）
EVENTS_W2 = [
    {"event_id": "AW2-E1", "frame_id": "B05", "event_type": "refused",
     "event_time": "2026-11-02T09:00:00Z", "source_seq": 10},
]

FRAME_W2 = [
    _u("B23", "w2", "B", 0.23, bu="BU-23"),
    _u("A21", "w2", "A", 0.21, bu="BU-21"),
]

# wave 3 前：BU-03 拒绝（S 空一名）、BU-23 拒绝（T 空一名）；
# BU-05 冷却到期，以 S 层的新名单行 B33 回归（其原锚定名单行 B05 是 T 层）。
EVENTS_W3 = [
    {"event_id": "AW3-E1", "frame_id": "A03", "event_type": "refused",
     "event_time": "2026-11-03T09:00:00Z", "source_seq": 20},
    {"event_id": "AW3-E2", "frame_id": "B23", "event_type": "refused",
     "event_time": "2026-11-03T09:01:00Z", "source_seq": 21},
]

FRAME_W3 = [
    _u("B33", "w3", "A", 0.30, bu="BU-05"),   # BU-05 以 S 层回归
    _u("A31", "w3", "A", 0.31, bu="BU-31"),
    _u("B34", "w3", "B", 0.34, bu="BU-34"),   # 补 T 的名额
]

# wave 4：全部留存，名单候选不会被使用
FRAME_W4 = [
    _u("A41", "w4", "A", 0.41, bu="BU-41"),
    _u("B42", "w4", "B", 0.42, bu="BU-42"),
]

# wave 5：绑定之后才发布；名单足够生成同轮备选计划
FRAME_W5 = [
    _u("A51", "w5", "A", 0.51, bu="BU-51"),
    _u("A52", "w5", "A", 0.52, bu="BU-52"),
    _u("A53", "w5", "A", 0.53, bu="BU-53"),
    _u("B54", "w5", "B", 0.54, bu="BU-54"),
    _u("B55", "w5", "B", 0.55, bu="BU-55"),
]

