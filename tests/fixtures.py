# -*- coding: utf-8 -*-
"""手工构造的测试场景。

覆盖：两层（分类层 + 数值层 + 明确缺失层）、重复业务单位（同版本/跨版本）、
缺失层单位、抽中后拒绝、逆序资格事件、非法状态转移、重复事件幂等、
候选被另一缺口先占用、显式跨版本身份映射、无法补齐层、规则冲突。
"""

# ---------------------------------------------------------------- 规则集
RULES_V1 = {
    "name": "rules_v1",
    "missing_stratum": "SM",
    "strata": [
        {"code": "S1", "ordinal": 0, "target_n": 2, "capacity": 6,
         "field_type": "categorical", "values": ["A", "B"]},
        {"code": "S2", "ordinal": 1, "target_n": 3, "capacity": 4,
         "field_type": "numeric",
         "ranges": [{"lower_inclusive": 0.0, "upper_exclusive": 10.0}]},
        {"code": "SM", "ordinal": 9, "target_n": 1, "capacity": 2,
         "field_type": "missing"},
    ],
}

RULES_V3 = {
    "name": "rules_v3",
    "missing_stratum": "SM",
    "strata": [
        {"code": "S1", "ordinal": 0, "target_n": 2, "capacity": 10,
         "field_type": "categorical", "values": ["A", "B"]},
        {"code": "S2", "ordinal": 1, "target_n": 0, "capacity": 10,
         "field_type": "numeric",
         "ranges": [{"lower_inclusive": 0.0, "upper_exclusive": 10.0}]},
        {"code": "SM", "ordinal": 9, "target_n": 0, "capacity": 5,
         "field_type": "missing"},
    ],
}

RULES_V4 = {
    "name": "rules_v4",
    "strata": [
        {"code": "S1", "ordinal": 0, "target_n": 1, "capacity": 2,
         "field_type": "categorical", "values": ["A"]},
    ],
}

# 编译期必须被拒绝的规则
BAD_RULES_OVERLAP = {
    "name": "bad_overlap",
    "strata": [
        {"code": "S1", "ordinal": 0, "target_n": 1, "capacity": 5,
         "field_type": "numeric",
         "ranges": [{"lower_inclusive": 0.0, "upper_exclusive": 10.0}]},
        {"code": "S2", "ordinal": 1, "target_n": 1, "capacity": 5,
         "field_type": "numeric",
         "ranges": [{"lower_inclusive": 5.0, "upper_exclusive": 15.0}]},
    ],
}
BAD_RULES_CAT_DUP = {
    "name": "bad_cat_dup",
    "strata": [
        {"code": "S1", "ordinal": 0, "target_n": 1, "capacity": 5,
         "field_type": "categorical", "values": ["A"]},
        {"code": "S2", "ordinal": 1, "target_n": 1, "capacity": 5,
         "field_type": "categorical", "values": ["A"]},
    ],
}
BAD_RULES_CAPACITY = {
    "name": "bad_capacity",
    "strata": [
        {"code": "S1", "ordinal": 0, "target_n": 4, "capacity": 3,
         "field_type": "categorical", "values": ["A"]},
    ],
}
BAD_RULES_MISSING_UNDECLARED = {
    "name": "bad_missing",
    "strata": [
        {"code": "S1", "ordinal": 0, "target_n": 1, "capacity": 5,
         "field_type": "categorical", "values": ["A"]},
    ],
}

# ---------------------------------------------------------------- 名单 v1
def _u(frame_id, version, value, rank, contact=True, bu=None, missing=False,
       prob=None):
    return {
        "frame_id": frame_id, "frame_version": version,
        "business_unit_id": bu,
        "stratum_value": None if missing else str(value),
        "is_missing": missing, "contactable": contact,
        "random_rank": rank, "inclusion_prob": prob,
    }


FRAME_V1 = [
    # S1 分类层：容量 6，抽 2，拒绝 2，候选 U3<U4<U5，U6 不可联系
    _u("U1", "v1", "A", 0.11, bu="BU1", prob=0.20),
    _u("U2", "v1", "A", 0.21, prob=0.20),
    _u("U3", "v1", "A", 0.31, prob=0.20),
    _u("U4", "v1", "B", 0.41, prob=0.20),
    _u("U5", "v1", "A", 0.51, prob=0.20),
    _u("U6", "v1", "B", 0.61, contact=False, prob=0.20),
    # S2 数值层：容量 4，抽 3；U7 拒绝、U10 失访，只有 U9 一个候选
    _u("U7", "v1", 5.0, 0.12, bu="BU7", prob=0.25),
    _u("U8", "v1", 8.0, 0.22, prob=0.25),
    _u("U9", "v1", 2.0, 0.32, prob=0.25),
    _u("U10", "v1", 9.0, 0.42, prob=0.25),
    # SM 缺失层：容量 2，抽 1
    _u("U11", "v1", None, 0.13, missing=True, prob=0.50),
    _u("U12", "v1", None, 0.23, contact=False, missing=True, prob=0.50),
]

DRAWS_V1 = [
    {"sample_id": "D-U1", "frame_id": "U1", "frame_version": "v1",
     "target_stratum": "S1", "random_rank": 0.11,
     "selection_position": 1, "inclusion_prob": 0.20},
    {"sample_id": "D-U2", "frame_id": "U2", "frame_version": "v1",
     "target_stratum": "S1", "random_rank": 0.21,
     "selection_position": 2, "inclusion_prob": 0.20},
    {"sample_id": "D-U7", "frame_id": "U7", "frame_version": "v1",
     "target_stratum": "S2", "random_rank": 0.12,
     "selection_position": 1, "inclusion_prob": 0.25},
    {"sample_id": "D-U8", "frame_id": "U8", "frame_version": "v1",
     "target_stratum": "S2", "random_rank": 0.22,
     "selection_position": 2, "inclusion_prob": 0.25},
    {"sample_id": "D-U10", "frame_id": "U10", "frame_version": "v1",
     "target_stratum": "S2", "random_rank": 0.42,
     "selection_position": 3, "inclusion_prob": 0.25},
    {"sample_id": "D-U11", "frame_id": "U11", "frame_version": "v1",
     "target_stratum": "SM", "random_rank": 0.13,
     "selection_position": 1, "inclusion_prob": 0.50},
]

# 资格事件（事件时间 + 来源序号）
EVENTS_V1 = [
    {"event_id": "E1", "frame_id": "U1", "event_type": "refused",
     "event_time": "2026-11-01T09:00:00Z", "source_seq": 1},
    {"event_id": "E2", "frame_id": "U2", "event_type": "refused",
     "event_time": "2026-11-01T09:05:00Z", "source_seq": 2},
    # 与 E2 完全同序的重复事件（自然键幂等）
    {"event_id": "E2-DUP", "frame_id": "U2", "event_type": "refused",
     "event_time": "2026-11-01T09:05:00Z", "source_seq": 2},
    {"event_id": "E3", "frame_id": "U8", "event_type": "eligible",
     "event_time": "2026-11-01T10:00:00Z", "source_seq": 3},
    # 逆序事件：U8 先收到更晚的 E4（再次合格确认），再收到更早的 E5，
    # E5 不得静默覆盖较新的已确认状态
    {"event_id": "E4", "frame_id": "U8", "event_type": "eligible",
     "event_time": "2026-11-02T10:00:00Z", "source_seq": 6},
    {"event_id": "E5", "frame_id": "U8", "event_type": "eligible",
     "event_time": "2026-11-01T11:00:00Z", "source_seq": 4},
    {"event_id": "E6", "frame_id": "U11", "event_type": "eligible",
     "event_time": "2026-11-01T12:00:00Z", "source_seq": 5},
    {"event_id": "E7", "frame_id": "U10", "event_type": "lost",
     "event_time": "2026-11-02T09:00:00Z", "source_seq": 7},
    {"event_id": "E8", "frame_id": "U7", "event_type": "refused",
     "event_time": "2026-11-02T09:10:00Z", "source_seq": 8},
    # 终态后的非法转移（refused -> lost），必须拒绝
    {"event_id": "E9", "frame_id": "U1", "event_type": "lost",
     "event_time": "2026-11-03T09:00:00Z", "source_seq": 9},
    # event_id 完全重复（幂等，不重复落库）
    {"event_id": "E1", "frame_id": "U1", "event_type": "refused",
     "event_time": "2026-11-01T09:00:00Z", "source_seq": 1},
    {"event_id": "E10", "frame_id": "U9", "event_type": "eligible",
     "event_time": "2026-11-03T10:00:00Z", "source_seq": 10},
]

# ---------------------------------------------------------------- 名单 v2
# U13 与 v1 的 U7 共享业务单位 BU7（跨版本身份，单位自带 BU 字段）
FRAME_V2 = [
    _u("U13", "v2", "A", 0.10, bu="BU7", prob=0.30),
    _u("U14", "v2", "A", 0.20, prob=0.30),
    _u("U15", "v2", "A", 0.30, prob=0.30),
    _u("U16", "v2", "B", 0.40, prob=0.30),
    _u("U17", "v2", "B", 0.50, prob=0.30),
]
DRAWS_V2 = [
    {"sample_id": "D2-U13", "frame_id": "U13", "frame_version": "v2",
     "target_stratum": "S1", "random_rank": 0.10,
     "selection_position": 1, "inclusion_prob": 0.30},
    {"sample_id": "D2-U14", "frame_id": "U14", "frame_version": "v2",
     "target_stratum": "S1", "random_rank": 0.20,
     "selection_position": 2, "inclusion_prob": 0.30},
]
EVENTS_V2 = [
    {"event_id": "V2-E1", "frame_id": "U13", "event_type": "eligible",
     "event_time": "2026-11-05T09:00:00Z", "source_seq": 1},
    {"event_id": "V2-E2", "frame_id": "U14", "event_type": "eligible",
     "event_time": "2026-11-05T09:05:00Z", "source_seq": 2},
    {"event_id": "U15-REF", "frame_id": "U15", "event_type": "refused",
     "event_time": "2026-11-05T09:10:00Z", "source_seq": 3},
]

# ---------------------------------------------------------------- 名单 v3
# 显式跨版本身份映射：U33 自身不带 BU，通过 identity_mappings 声明属于 BU1
FRAME_V3 = [
    _u("U31", "v3", "A", 0.10, prob=0.40),
    _u("U32", "v3", "A", 0.20, prob=0.40),
    _u("U33", "v3", "A", 0.30, prob=0.40),   # 显式映射到 BU1
    _u("U34", "v3", "B", 0.40, prob=0.40),
    _u("U35", "v3", "B", 0.50, prob=0.40),
]
IDENTITY_V3 = [
    {"business_unit_id": "BU1", "frame_id": "U33", "frame_version": "v3"},
]
DRAWS_V3 = [
    {"sample_id": "D3-U31", "frame_id": "U31", "frame_version": "v3",
     "target_stratum": "S1", "random_rank": 0.10,
     "selection_position": 1, "inclusion_prob": 0.40},
    {"sample_id": "D3-U32", "frame_id": "U32", "frame_version": "v3",
     "target_stratum": "S1", "random_rank": 0.20,
     "selection_position": 2, "inclusion_prob": 0.40},
]
EVENTS_V3 = [
    {"event_id": "V3-E1", "frame_id": "U31", "event_type": "refused",
     "event_time": "2026-11-08T09:00:00Z", "source_seq": 1},
    {"event_id": "V3-E2", "frame_id": "U32", "event_type": "eligible",
     "event_time": "2026-11-08T09:05:00Z", "source_seq": 2},
]

# ---------------------------------------------------------------- 名单 v4：无法补齐层
FRAME_V4 = [
    _u("U41", "v4", "A", 0.10, prob=0.60),
    _u("U42", "v4", "A", 0.20, contact=False, prob=0.60),
]
DRAWS_V4 = [
    {"sample_id": "D4-U41", "frame_id": "U41", "frame_version": "v4",
     "target_stratum": "S1", "random_rank": 0.10,
     "selection_position": 1, "inclusion_prob": 0.60},
]
EVENTS_V4 = [
    {"event_id": "V4-E1", "frame_id": "U41", "event_type": "lost",
     "event_time": "2026-11-10T09:00:00Z", "source_seq": 1},
]

# 无法归类的单位（用于装配前拒绝测试）
FRAME_BAD_VALUE = [_u("U99", "vbad", "X", 0.10, prob=0.5)]
