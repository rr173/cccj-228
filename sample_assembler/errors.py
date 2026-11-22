# -*- coding: utf-8 -*-
"""装配器领域错误。装配前可拒绝的问题全部使用显式异常，不静默吞掉。"""


class AssemblerError(Exception):
    """所有装配器错误的基类。"""


class RuleConflictError(AssemblerError):
    """层规则冲突：边界重叠、缺失层未声明、容量不足等。"""


class ImportValidationError(AssemblerError):
    """名单/抽样结果导入校验失败。"""


class RuleCompileError(AssemblerError):
    """规则编译失败（语法/结构问题）。"""


class EventRejectedError(AssemblerError):
    """资格事件被拒绝（逆序覆盖、非法状态转移）。"""

    def __init__(self, message, reason=None, duplicate=False):
        super().__init__(message)
        self.reason = reason
        self.duplicate = duplicate


class AssembleError(AssemblerError):
    """装配批次无法完成（存在 open 缺口、层容量超限等）。"""


class PublishError(AssemblerError):
    """批次不满足发布条件。"""


class PanelError(AssemblerError):
    """panel 轮换计划无法生成或校验失败（规则、基数、归类等）。"""
