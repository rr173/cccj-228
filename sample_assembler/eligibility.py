# -*- coding: utf-8 -*-
"""资格事件状态机。

状态：
    pending   尚未收到任何已接受事件
    eligible  资格核验合格（在样）
    refused   拒绝（终态）
    lost      失访（终态）
    withdrew  合格后撤回（终态）

规则：
- 重复事件（同 event_id，或同 frame+type+time+source_seq）幂等，不改变状态。
- 逆序事件（event_time/source_seq 早于已确认的最新事件）不能静默覆盖较新状态，
  事件仍落库但 accepted=0，reject_reason='out_of_order'。
- 非法转移（终态之后再转移、pending 直接 withdrew）同样拒绝落库标记。
- 终态集合固定；eligible 之后允许 refused / lost / withdrew。
"""

STATE_PENDING = "pending"
STATE_ELIGIBLE = "eligible"
STATE_REFUSED = "refused"
STATE_LOST = "lost"
STATE_WITHDREW = "withdrew"

TERMINAL_STATES = frozenset((STATE_REFUSED, STATE_LOST, STATE_WITHDREW))
FAIL_STATES = frozenset((STATE_REFUSED, STATE_LOST, STATE_WITHDREW))
VALID_EVENTS = frozenset(("lost", "refused", "eligible", "withdrew"))

# 当前状态 -> 允许接收的事件类型
TRANSITIONS = {
    STATE_PENDING: frozenset(("eligible", "refused", "lost")),
    STATE_ELIGIBLE: frozenset(("eligible", "refused", "lost", "withdrew")),
    STATE_REFUSED: frozenset(),
    STATE_LOST: frozenset(),
    STATE_WITHDREW: frozenset(),
}


def is_terminal(state):
    return state in TERMINAL_STATES


def is_failure(state):
    """该状态是否产生缺口（失访/拒绝/撤回均需要替补补齐）。"""
    return state in FAIL_STATES


def event_order(event_time, source_seq):
    return (str(event_time), int(source_seq))


def decide_transition(current_state, current_order, event_type, event_time, source_seq):
    """返回 (action, reason, new_state)。

    action:
      'accept'  接受，推进状态
      'duplicate' 幂等重复，状态不变（事件不重复落库）
      'reject'  逆序或非法，落库 accepted=0，状态不变
    """
    if event_type not in VALID_EVENTS:
        return ("reject", "unknown_event_type", current_state)

    new_order = event_order(event_time, source_seq)
    if current_order is not None and new_order < current_order:
        return ("reject", "out_of_order", current_state)

    if event_type == "eligible":
        new_state = STATE_ELIGIBLE
    elif event_type == "refused":
        new_state = STATE_REFUSED
    elif event_type == "lost":
        new_state = STATE_LOST
    else:  # withdrew
        new_state = STATE_WITHDREW

    # 同序重复（时间与来源序号完全相同且类型相同）按幂等处理
    if current_order is not None and new_order == current_order:
        return ("duplicate", "duplicate", current_state)

    if event_type not in TRANSITIONS[current_state]:
        return ("reject", "illegal_transition", current_state)

    return ("accept", None, new_state)
