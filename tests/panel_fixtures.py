# -*- coding: utf-8 -*-
"""panel 轮换测试场景：样本轮（wave 0）+ 三轮计划（wave 1..3），两层。

层：SA（分类 A/B，目标 2）、SB（数值 [0,10)，目标 2）。
轮换规则：最大连续 2 轮、退出后冷却 1 轮、本轮最低留存比例 0.5。

场景覆盖：
- wave 1：F02 拒绝退出、F07 撤回退出；F11 与留存成员 F01 同业务单位
  （business_unit_conflict）；F13/F17 的业务单位本轮刚退出、冷却中
  （cooldown_active）；F18 不可联系；F12/F16 新加入。
- wave 2：F01/F06 连续 2 轮达上限退出（max_consecutive_reached）；
  F22 的业务单位 BU-02 上一轮退出、冷却 1 轮后到期，可回归（同一
  panel_member 身份）；F27 不可联系；F26 新加入。
- wave 3：F26 资格状态变为拒绝（exited_refused）；F12/F16 达连续上限；
  SB 留存 0 低于最低比例（retention_below_minimum）且候选全部排除
  （F36 不可联系、F37 业务单位 BU-16 冷却中）→ 明确记录不可补齐后发布。
"""

# ---------------------------------------------------------------- 层规则
PANEL_FRAME_RULES = {
    "name": "panel_frame_rules",
    "strata": [
        {"code": "SA", "ordinal": 0, "target_n": 2, "capacity": 20,
         "field_type": "categorical", "values": ["A", "B"]},
        {"code": "SB", "ordinal": 1, "target_n": 2, "capacity": 20,
         "field_type": "numeric",
         "ranges": [{"lower_inclusive": 0.0, "upper_exclusive": 10.0}]},
    ],
}

ROTATION_RULES = {
    "name": "rot_main",
    "frame_rule_set": "panel_frame_rules",
    "max_consecutive_waves": 2,
    "cooldown_waves": 1,
    "min_retention_ratio": 0.5,
    "strata": [{"code": "SA", "target_n": 2}, {"code": "SB", "target_n": 2}],
}


def _u(frame_id, version, value, rank, contact=True, bu=None, prob=0.5):
    return {
        "frame_id": frame_id, "frame_version": version,
        "business_unit_id": bu,
        "stratum_value": None if value is None else str(value),
        "is_missing": value is None, "contactable": contact,
        "random_rank": rank, "inclusion_prob": prob,
    }


# ---------------------------------------------------------------- wave 0：样本轮
FRAME_W0 = [
    _u("F01", "w0", "A", 0.01, bu="BU-01"),
    _u("F02", "w0", "A", 0.02, bu="BU-02"),
    _u("F03", "w0", "A", 0.03, bu="BU-03"),
    _u("F04", "w0", "B", 0.04, bu="BU-04"),
    _u("F06", "w0", "5", 0.06, bu="BU-06"),
    _u("F07", "w0", "6", 0.07, bu="BU-07"),
    _u("F08", "w0", "7", 0.08, bu="BU-08"),
    _u("F09", "w0", "9", 0.09, bu="BU-09"),
]

DRAWS_W0 = [
    {"sample_id": "D-F01", "frame_id": "F01", "frame_version": "w0",
     "target_stratum": "SA", "random_rank": 0.01,
     "selection_position": 1, "inclusion_prob": 0.5},
    {"sample_id": "D-F02", "frame_id": "F02", "frame_version": "w0",
     "target_stratum": "SA", "random_rank": 0.02,
     "selection_position": 2, "inclusion_prob": 0.5},
    {"sample_id": "D-F06", "frame_id": "F06", "frame_version": "w0",
     "target_stratum": "SB", "random_rank": 0.06,
     "selection_position": 1, "inclusion_prob": 0.5},
    {"sample_id": "D-F07", "frame_id": "F07", "frame_version": "w0",
     "target_stratum": "SB", "random_rank": 0.07,
     "selection_position": 2, "inclusion_prob": 0.5},
]

EVENTS_W0 = [
    {"event_id": "W0-E1", "frame_id": "F01", "event_type": "eligible",
     "event_time": "2026-11-01T09:00:00Z", "source_seq": 1},
    {"event_id": "W0-E2", "frame_id": "F02", "event_type": "eligible",
     "event_time": "2026-11-01T09:01:00Z", "source_seq": 2},
    {"event_id": "W0-E3", "frame_id": "F06", "event_type": "eligible",
     "event_time": "2026-11-01T09:02:00Z", "source_seq": 3},
    {"event_id": "W0-E4", "frame_id": "F07", "event_type": "eligible",
     "event_time": "2026-11-01T09:03:00Z", "source_seq": 4},
]

# wave 1 前的资格变化：F02 拒绝、F07 撤回
EVENTS_W1 = [
    {"event_id": "W1-E1", "frame_id": "F02", "event_type": "refused",
     "event_time": "2026-11-02T09:00:00Z", "source_seq": 10},
    {"event_id": "W1-E2", "frame_id": "F07", "event_type": "withdrew",
     "event_time": "2026-11-02T09:01:00Z", "source_seq": 11},
]

# ---------------------------------------------------------------- wave 1 名单
FRAME_W1 = [
    _u("F11", "w1", "A", 0.11, bu="BU-01"),   # 与留存成员 F01 同 BU -> 冲突
    _u("F12", "w1", "A", 0.12, bu="BU-12"),   # -> SA 新加入
    _u("F13", "w1", "A", 0.13, bu="BU-02"),   # BU-02 本轮刚退出，冷却中
    _u("F16", "w1", "5", 0.16, bu="BU-16"),   # -> SB 新加入
    _u("F17", "w1", "6", 0.17, bu="BU-07"),   # BU-07 冷却中
    _u("F18", "w1", "7", 0.18, contact=False, bu="BU-18"),
]

# ---------------------------------------------------------------- wave 2 名单
FRAME_W2 = [
    _u("F22", "w2", "A", 0.19, bu="BU-02"),   # BU-02 冷却到期 -> 回归（同成员）
    _u("F21", "w2", "A", 0.21, bu="BU-21"),
    _u("F26", "w2", "5", 0.26, bu="BU-26"),   # -> SB 新加入
    _u("F27", "w2", "6", 0.27, contact=False, bu="BU-27"),
]

# wave 3 前的资格变化：F26 拒绝
EVENTS_W3 = [
    {"event_id": "W3-E1", "frame_id": "F26", "event_type": "refused",
     "event_time": "2026-11-04T09:00:00Z", "source_seq": 20},
]

# ---------------------------------------------------------------- wave 3 名单
FRAME_W3 = [
    _u("F31", "w3", "A", 0.31, bu="BU-31"),   # -> SA 新加入
    _u("F36", "w3", "5", 0.36, contact=False, bu="BU-36"),
    _u("F37", "w3", "6", 0.37, bu="BU-16"),   # BU-16 本轮刚退出，冷却中
]

# wave 4 名单（发布门测试用；frame_id 全局唯一，不复用 w3 的行）
FRAME_W3B = [
    _u("F41", "w3b", "A", 0.41, bu="BU-41"),  # -> SA 新加入
    _u("F36B", "w3b", "5", 0.36, contact=False, bu="BU-36B"),
    _u("F37B", "w3b", "6", 0.37, bu="BU-16"),  # BU-16 冷却已到期 -> 可回归
]

# ---------------------------------------------------------------- 并发场景
CONC_FRAME_RULES = {
    "name": "conc_frame_rules",
    "strata": [
        {"code": "SA", "ordinal": 0, "target_n": 1, "capacity": 10,
         "field_type": "categorical", "values": ["A"]},
    ],
}
CONC_ROTATION = {
    "name": "conc_rotation",
    "frame_rule_set": "conc_frame_rules",
    "max_consecutive_waves": 2,
    "cooldown_waves": 1,
    "min_retention_ratio": 0.0,
    "strata": [{"code": "SA", "target_n": 1}],
}
CONC_FRAME_W0 = [
    _u("P1", "cw0", "A", 0.1, bu="PB-1"),
    _u("P2", "cw0", "A", 0.2, bu="PB-2"),
]
CONC_DRAWS_W0 = [
    {"sample_id": "D-P1", "frame_id": "P1", "frame_version": "cw0",
     "target_stratum": "SA", "random_rank": 0.1,
     "selection_position": 1, "inclusion_prob": 0.5},
]
# 装配前：P1 拒绝 -> 缺口由 P2 替补
CONC_EVENTS_W0 = [
    {"event_id": "CW0-E1", "frame_id": "P1", "event_type": "refused",
     "event_time": "2026-11-01T09:00:00Z", "source_seq": 1},
]
# 装配发布后、wave 1 前：P2 拒绝 -> wave 1 产生一个缺口
CONC_EVENTS_W1 = [
    {"event_id": "CW1-E1", "frame_id": "P2", "event_type": "refused",
     "event_time": "2026-11-02T09:00:00Z", "source_seq": 2},
]
CONC_FRAME_W1 = [
    _u("P3", "cw1", "A", 0.3, bu="PB-3"),
    _u("P4", "cw1", "A", 0.4, bu="PB-4"),
]
