# -*- coding: utf-8 -*-
"""层规则编译与层归属判定。

规则输入（一个 rule set）结构：

    {
      "name": "r1",
      "missing_stratum": "SM",          # 层字段缺失时归入的明确缺失层；None 则拒绝
      "strata": [
        {"code": "S1", "ordinal": 0, "target_n": 2, "capacity": 6,
         "field_type": "categorical", "values": ["A", "B"]},
        {"code": "S2", "ordinal": 1, "target_n": 3, "capacity": 4,
         "field_type": "numeric",
         "ranges": [{"lower_inclusive": 0.0, "upper_exclusive": 10.0}]},
        {"code": "SM", "ordinal": 9, "target_n": 1, "capacity": 2,
         "field_type": "missing"}
      ]
    }

边界值语义：数值区间一律左闭右开 [lower, upper)，规则按 ordinal 排序；
编译期检查重叠与容量，冲突直接拒绝，不进入装配。
"""

from .errors import RuleCompileError, RuleConflictError

VALID_FIELD_TYPES = ("categorical", "numeric", "missing")


class CompiledRuleSet:
    def __init__(self, name, missing_stratum, strata, ordinal_order):
        self.name = name
        self.missing_stratum = missing_stratum
        # strata: code -> dict(target_n, capacity, kind, ordinal, raw)
        self.strata = strata
        self.ordinal_order = ordinal_order

    def classify(self, stratum_value, is_missing):
        """判定一个名单单位的层；无法归入返回 None（由调用方拒绝装配）。"""
        if is_missing:
            return self.missing_stratum
        for code in self.ordinal_order:
            s = self.strata[code]
            if s["kind"] == "categorical":
                if stratum_value in s["values"]:
                    return code
            elif s["kind"] == "numeric":
                try:
                    num = float(stratum_value)
                except (TypeError, ValueError):
                    continue
                for lo, hi in s["ranges"]:
                    if lo <= num < hi:
                        return code
            # missing 类型只处理 is_missing，不在这里匹配
        return None


def compile_rules(spec):
    if not isinstance(spec, dict):
        raise RuleCompileError("规则 spec 必须是 dict")
    name = spec.get("name")
    if not name:
        raise RuleCompileError("规则集缺少 name")
    strata_spec = spec.get("strata")
    if not isinstance(strata_spec, list) or not strata_spec:
        raise RuleCompileError("规则集 strata 必须是非空列表")

    missing_stratum = spec.get("missing_stratum")
    strata = {}
    ordinals = []
    categorical_spaces = {}   # 值 -> 层
    numeric_intervals = []    # (lo, hi, code)
    seen_missing = []

    for i, s in enumerate(strata_spec):
        code = s.get("code")
        if not code:
            raise RuleCompileError("第 %d 条层规则缺少 code" % i)
        if code in strata:
            raise RuleConflictError("层编码重复: %s" % code)
        ftype = s.get("field_type")
        if ftype not in VALID_FIELD_TYPES:
            raise RuleCompileError(
                "层 %s 的 field_type 非法: %r" % (code, ftype)
            )
        target = s.get("target_n")
        capacity = s.get("capacity")
        if not isinstance(target, int) or target < 0:
            raise RuleCompileError("层 %s 的 target_n 必须是非负整数" % code)
        if not isinstance(capacity, int) or capacity < 0:
            raise RuleCompileError("层 %s 的 capacity 必须是非负整数" % code)
        if target > capacity:
            raise RuleConflictError(
                "层 %s 目标样本数 %d 超过层容量 %d（装配前拒绝）"
                % (code, target, capacity)
            )
        ordinal = s.get("ordinal", i)
        if not isinstance(ordinal, int):
            raise RuleCompileError("层 %s 的 ordinal 必须是整数" % code)
        ordinals.append(ordinal)

        entry = {
            "target_n": target,
            "capacity": capacity,
            "kind": ftype,
            "ordinal": ordinal,
            "raw": s,
        }

        if ftype == "categorical":
            values = s.get("values")
            if not isinstance(values, list) or not values:
                raise RuleCompileError("分类层 %s 需要非空 values" % code)
            for v in values:
                v = str(v)
                if v in categorical_spaces:
                    raise RuleConflictError(
                        "分类值 %r 同时映射到层 %s 与 %s（规则冲突）"
                        % (v, categorical_spaces[v], code)
                    )
                categorical_spaces[v] = code
            entry["values"] = {str(v) for v in values}
        elif ftype == "numeric":
            ranges = s.get("ranges")
            if not isinstance(ranges, list) or not ranges:
                raise RuleCompileError("数值层 %s 需要非空 ranges" % code)
            parsed = []
            for r in ranges:
                lo = r.get("lower_inclusive")
                hi = r.get("upper_exclusive")
                if lo is None or hi is None:
                    raise RuleCompileError(
                        "层 %s 区间必须给出 lower_inclusive/upper_exclusive" % code
                    )
                lo, hi = float(lo), float(hi)
                if not lo < hi:
                    raise RuleConflictError(
                        "层 %s 区间非法（要求 lower < upper）: [%r, %r)"
                        % (code, lo, hi)
                    )
                parsed.append((lo, hi))
            # 层内区间重叠检查
            parsed_sorted = sorted(parsed)
            for (l1, h1), (l2, h2) in zip(parsed_sorted, parsed_sorted[1:]):
                if l2 < h1:
                    raise RuleConflictError(
                        "层 %s 内部区间重叠: [%r,%r) 与 [%r,%r)"
                        % (code, l1, h1, l2, h2)
                    )
            for lo, hi in parsed_sorted:
                for (lo2, hi2, code2) in numeric_intervals:
                    # 左闭右开区间：相邻边界 [0,10) 与 [10,20) 不冲突
                    if lo < hi2 and lo2 < hi:
                        raise RuleConflictError(
                            "数值边界冲突：层 %s [%r,%r) 与层 %s [%r,%r) 重叠"
                            % (code, lo, hi, code2, lo2, hi2)
                        )
                numeric_intervals.append((lo, hi, code))
            entry["ranges"] = parsed_sorted
        else:  # missing
            seen_missing.append(code)
            entry["values"] = set()
            entry["ranges"] = []

        strata[code] = entry

    if len(ordinals) != len(set(ordinals)):
        raise RuleConflictError("层 ordinal 重复")

    if len(seen_missing) > 1:
        raise RuleConflictError(
            "缺失层只能声明一个，发现: %s" % ", ".join(seen_missing)
        )
    if seen_missing:
        miss_code = seen_missing[0]
        if missing_stratum is None:
            missing_stratum = miss_code
        elif missing_stratum != miss_code:
            raise RuleConflictError(
                "missing_stratum=%r 与 field_type=missing 的层 %r 不一致"
                % (missing_stratum, miss_code)
            )
    # 声明了缺失层但没有 missing 规则也算冲突
    if missing_stratum is not None and missing_stratum not in strata:
        raise RuleConflictError(
            "missing_stratum 指向不存在的层: %r" % missing_stratum
        )

    ordinal_order = [
        c for c, _ in sorted(
            ((c, e["ordinal"]) for c, e in strata.items()),
            key=lambda x: x[1],
        )
    ]
    return CompiledRuleSet(name, missing_stratum, strata, ordinal_order)
