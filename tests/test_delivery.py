# -*- coding: utf-8 -*-
"""研究交付包端到端测试：运行 `python -I tests/test_delivery.py`。

覆盖（按需求条款组织，用几层小名单核对实际交付成员与组名，而不只数行）：

1. 兄弟叶各两人、k=2 时分别按叶交付（组名 L1/L2）；候选集只含每个选定
   计划都在样 + 有效答案 + 授权的成员；跳轮选择时 Q3（首尾在样但中间离样、
   不同区段）被排除，中间未选轮次无答案/无授权不影响 Q1/Q2。
2. 未映射的候选层（S3）拒绝交付；同轮备选 draft 不能替代链上计划；
   授权只能登记在链上在样记录；k>=2 与节点标签唯一等规格校验。
3. 分组最优性：兄弟叶各 2 人、k=2 时选两片叶（4 人、深度和 8），
   而不是父节点（4 人、深度和 0）；一层 2 人、另一层 1 人、k=2 时
   交付前者、不输出空组；选中节点后其成员不得再被拆给别的组。
4. 撤回：已交付成员在选定计划撤回 -> 旧包永久停领（再次授予也不恢复）；
   新请求键重算把剩余三人归到父组；未交付成员 / 未选轮次的撤回不影响旧包。
5. 请求重试：同键同规格（轮次乱序、树兄弟顺序不同）返回原包标识；
   同键改规格报错；答案修订不改变已生成答卷；重开数据库后授权/停领/
   幂等仍持久。
6. 两连接交错：撤回与领取并发时，领取结果只能对应某个完整且仍可领取的
   版本（要么旧的完整答卷，要么明确的停领错误），不会看到半撤回状态。
"""

import os
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from sample_assembler import DeliveryError, SampleAssembler  # noqa: E402
from tests import delivery_fixtures as fx  # noqa: E402

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


def expect_error(label, fn, exc=DeliveryError):
    try:
        fn()
        check(label, False, "未抛出异常")
        return None
    except exc as e:
        check(label, True)
        return str(e)


def member_names(asm, package_id):
    """按包内代号反查真实成员名（仅测试内部使用）。"""
    mapping = fx.code_to_member(asm, package_id)
    return sorted(fx.bu_of(asm, ref) for ref in mapping.values())


def group_map(asm, package_id):
    """{组名: 排序后的成员业务单位}。"""
    mapping = fx.code_to_member(asm, package_id)
    out = {}
    for code, ref in mapping.items():
        bu = fx.bu_of(asm, ref)
        row = asm.conn.execute(
            "SELECT group_name FROM delivery_member WHERE package_id=?"
            " AND code=?", (package_id, code)).fetchone()
        out.setdefault(row["group_name"], []).append(bu)
    return {k: sorted(v) for k, v in out.items()}


def answers_by_bu(asm, package_id):
    mapping = fx.code_to_member(asm, package_id)
    out = {}
    for r in asm.conn.execute(
            "SELECT code, answers_json FROM delivery_member WHERE package_id=?",
            (package_id,)):
        import json
        out[fx.bu_of(asm, mapping[r["code"]])] = json.loads(r["answers_json"])
    return out


# ================================================================ 场景搭建
def fresh(db_path):
    asm, plans, alt4 = fx.build_panel(db_path)
    members = fx.member_ids(asm, plans)
    fx.submit_answers(asm, plans, members)
    return asm, plans, alt4, members


# ================================================================ 1. 基本交付
def test_basic_two_leaves():
    section("1. 兄弟叶各两人：k=2 分别按叶交付，跳轮离样被排除")
    with tempfile.TemporaryDirectory() as d:
        asm, plans, alt4, m = fresh(os.path.join(d, "basic.db"))
        p1, p4 = plans[1], plans[4]

        # 只授权 Q1/Q2（S1）与 Q4/Q5（S2），且只对选定的 w1、w4 授权。
        fx.grant(asm, plans, m, ["Q1", "Q2", "Q4", "Q5"], waves=(1, 4))

        res = asm.generate_delivery_package(
            "REQ-BASIC", [p1, p4], 2, fx.TREE_TWO_LEAVES)
        pkg = res["package_id"]
        check("生成成功且非幂等命中", res["idempotent"] is False)
        check("轮次顺序按链序（w1 -> w4）", res["rounds"] == [1, 4], res["rounds"])
        check("领取记录只有 code/group/answers 三个字段",
              all(set(x) == {"code", "group", "answers"}
                  for x in res["members"]))
        check("交付 4 人（Q1/Q2/Q4/Q5），不含 Q3/Q6/Q7",
              member_names(asm, pkg) == ["BU-01", "BU-02", "BU-04", "BU-05"],
              member_names(asm, pkg))
        gm = group_map(asm, pkg)
        check("兄弟叶各自成组：L1=Q1,Q2；L2=Q4,Q5",
              gm == {"L1": ["BU-01", "BU-02"],
                     "L2": ["BU-04", "BU-05"]}, gm)
        am = answers_by_bu(asm, pkg)
        check("答案按轮次顺序对应：[w1,w4]",
              am == {"BU-01": [0, 0], "BU-02": [1, 0],
                     "BU-04": [1, 1], "BU-05": [0, 1]}, am)
        check("Q3 虽首尾在样且有答案，但中间离样（不同区段）被排除",
              "BU-03" not in am)
        check("Q6 在 w1 在样但 w4 缺席被排除", "BU-06" not in am)
        check("Q7 因 S3 未映射不进入候选（含其授权/答案）",
              "BU-07" not in am)

        codes = [x["code"] for x in res["members"]]
        check("包内代号唯一且不使用原成员标识", len(set(codes)) == len(codes)
              and all(c.startswith("C") for c in codes)
              and not any("PM_" in c or "BU-" in c for c in codes), codes)

        # 代号固定性：一次成功发布后同一包反复领取，代号不变。
        again = asm.claim_delivery_package(pkg)
        check("同一包内代号稳定（重复领取结果完全一致）",
              again["members"] == res["members"])

        # 代号不跨包稳定：同一成员在两个包中拿到不同代号。
        fx.grant(asm, plans, m, ["Q1", "Q2", "Q4", "Q5"], waves=(1, 4))
        other = asm.generate_delivery_package(
            "REQ-BASIC-2", [p1, p4], 2, fx.TREE_TWO_LEAVES)["package_id"]
        map_a = fx.code_to_member(asm, pkg)   # code -> member_ref
        map_b = fx.code_to_member(asm, other)
        check("两包的代号集合不相交（代号不跨包复用）",
              set(map_a).isdisjoint(set(map_b)),
              (sorted(map_a), sorted(map_b)))
        check("两包交付成员集合相同",
              sorted(map_a.values()) == sorted(map_b.values()))

        # 领取面不泄露内部标识/原层/被排除者。
        claimed = asm.claim_delivery_package(pkg)
        blob = repr(claimed)
        check("领取结果不含被排除者的内部标识",
              all(m[name] not in blob for name in ("Q3", "Q6", "Q7")))
        check("领取结果不含业务单位或原层标记",
              "BU-" not in blob and "S1" not in blob and "S2" not in blob)
        check("领取结果与生成结果一致",
              claimed["members"] == res["members"]
              and claimed["rounds"] == [1, 4])
        check("领取记录字段顺序为 代号/公开组名/答案",
              all(list(x) == ["code", "group", "answers"]
                  for x in claimed["members"]))


def test_skipped_wave_semantics():
    section("2. 跳轮选择：中间未选轮次不要求答案/授权，但必须仍在样")
    with tempfile.TemporaryDirectory() as d:
        asm, plans, alt4, m = fresh(os.path.join(d, "skip.db"))
        p1, p2, p3, p4 = plans[1], plans[2], plans[3], plans[4]
        # 选 w1、w3（跳过 w2）。Q6 在 w3 前已拒绝离样 -> 不是候选；
        # Q3 在 w2 离样 -> 不是候选；Q1/Q2/Q4/Q5 连续在样。
        fx.grant(asm, plans, m, ["Q1", "Q2", "Q4", "Q5"],
                 waves=(1, 3))
        res = asm.generate_delivery_package(
            "REQ-SKIP", [p3, p1], 2, fx.TREE_TWO_LEAVES, endpoint_plan_id=p4)
        pkg = res["package_id"]
        check("乱序输入按链序使用（w1 -> w3）", res["rounds"] == [1, 3],
              res["rounds"])
        check("Q1/Q2/Q4/Q5 交付，Q6 因 w3 离样被排除",
              member_names(asm, pkg) == ["BU-01", "BU-02", "BU-04", "BU-05"],
              member_names(asm, pkg))
        am = answers_by_bu(asm, pkg)
        check("中间的 w2 有答案/授权也不进入答卷列（answers 只含选定轮次）",
              all(len(v) == 2 for v in am.values()), am)
        check("Q1 的 w3 答案取自有效答案表",
              am["BU-01"] == [fx.ANSWERS_W1["Q1"], fx.ANSWERS_W3["Q1"]], am)
        check("Q3 在 w1 在样但 w2 离样，不能跨缺席段成为候选",
              "BU-03" not in am)

        # 另一组选中轮次 w1、w4（跳过 w2、w3）：Q1/Q2/Q4/Q5/Q7 连续在样，
        # 但 Q6 已于 w3 离样 -> 只有前五人是候选；再验证中间轮次缺失授权
        # 不影响（w2/w3 未授予任何授权）。
        fx.grant(asm, plans, m, ["Q1", "Q2", "Q4", "Q5", "Q7"],
                 waves=(1, 4))
        res_b = asm.generate_delivery_package(
            "REQ-SKIP-B", [p4, p1], 2, fx.TREE_THREE_LEAVES)
        gm_b = group_map(asm, res_b["package_id"])
        check("跳轮时 5 人全部交付；为多交付 S3 单人会归到父组 G",
              gm_b == {"G": ["BU-01", "BU-02", "BU-04", "BU-05", "BU-07"]},
              gm_b)
        check("中间的 w2/w3 完全没有授权也能交付", len(gm_b["G"]) == 5)
        # 若 S3 成员不是候选（未授权），则三片叶中两片达标、L3 为空，
        # 不得输出空组；此时两片叶覆盖全部候选、深度和最大。
        # 两叶树只映射 S1/S2：候选里的 S3（Q7 已授权）未映射 -> 拒绝交付。
        expect_error(
            "两叶树遇到候选中的 S3 未映射时拒绝交付",
            lambda: asm.generate_delivery_package(
                "REQ-SKIP-D", [p4, p1], 2, fx.TREE_TWO_LEAVES))


# ================================================================ 3. 规格校验
def test_spec_validation():
    section("3. 规格校验：未映射层、同轮计划、非链上授权、k、标签唯一")
    with tempfile.TemporaryDirectory() as d:
        asm, plans, alt4, m = fresh(os.path.join(d, "spec.db"))
        p1, p2, p4 = plans[1], plans[2], plans[4]
        fx.grant(asm, plans, m, ["Q1", "Q2", "Q4", "Q5", "Q7"], waves=(1, 4))

        # 两片叶但候选里出现 S3（Q7 有授权 + 答案）-> 未映射层拒绝交付
        msg = expect_error(
            "候选中存在未映射层 S3 时拒绝交付",
            lambda: asm.generate_delivery_package(
                "REQ-UNMAPPED", [p1, p4], 2, fx.TREE_TWO_LEAVES))
        check("拒绝原因点名未映射的分层代码",
              msg is not None and "S3" in msg, msg)

        # 同轮备选 draft 不在绑定链上 -> 拒绝
        expect_error(
            "同轮备选计划不能替代链上计划",
            lambda: asm.generate_delivery_package(
                "REQ-ALT", [p1, alt4], 2, fx.TREE_TWO_LEAVES))

        # 只给一个计划 -> 拒绝（至少两个不同计划）
        expect_error(
            "少于两个不同计划被拒绝",
            lambda: asm.generate_delivery_package(
                "REQ-ONE", [p1, p1], 2, fx.TREE_TWO_LEAVES))

        # k < 2 -> 拒绝；k 非整数 -> 拒绝
        expect_error(
            "k=1 被拒绝",
            lambda: asm.generate_delivery_package(
                "REQ-K1", [p1, p4], 1, fx.TREE_TWO_LEAVES))
        expect_error(
            "k=True（布尔冒充整数）被拒绝",
            lambda: asm.generate_delivery_package(
                "REQ-KBOOL", [p1, p4], True, fx.TREE_TWO_LEAVES))

        # 节点标签重复 -> 拒绝
        dup_tree = {"label": "G", "children": [
            {"label": "L1", "stratum": "S1"},
            {"label": "L1", "stratum": "S2"}]}
        expect_error(
            "分组树节点标签重复被拒绝",
            lambda: asm.generate_delivery_package(
                "REQ-DUP", [p1, p4], 2, dup_tree))
        # 一层对应两片叶 -> 拒绝
        two_same = {"label": "G", "children": [
            {"label": "L1", "stratum": "S1"},
            {"label": "L2", "stratum": "S1"}]}
        expect_error(
            "一层对应多片叶被拒绝",
            lambda: asm.generate_delivery_package(
                "REQ-SAME", [p1, p4], 2, two_same))
        # 叶无 stratum -> 拒绝
        no_stratum = {"label": "G", "children": [{"label": "L1"}]}
        expect_error(
            "叶节点缺少 stratum 被拒绝",
            lambda: asm.generate_delivery_package(
                "REQ-LEAF", [p1, p4], 2, no_stratum))

        # 非链上在样记录登记授权 -> 拒绝（alt4 draft 的账本行）
        expect_error(
            "只能对绑定链上的在样记录登记授权",
            lambda: asm.grant_delivery_authorization(
                alt4, m["Q1"]))
        # 链上但该轮已离样（Q3 于 w2 已退出）-> 拒绝
        expect_error(
            "链上但已离样的轮次不能登记授权",
            lambda: asm.grant_delivery_authorization(p2, m["Q3"]))
        # 不存在的成员 -> 拒绝
        expect_error(
            "未知成员不能登记授权",
            lambda: asm.grant_delivery_authorization(p1, "PM_does_not_exist"))

        # k 大于候选人数 -> 拒绝，且不留下半个包。
        before = asm.conn.execute(
            "SELECT COUNT(*) FROM delivery_package").fetchone()[0]
        expect_error(
            "k 大于候选人数时拒绝交付",
            lambda: asm.generate_delivery_package(
                "REQ-BIGK", [p1, p4], 99, fx.TREE_TWO_LEAVES))
        check("失败交付不留下包行（原子提交）",
              asm.conn.execute(
                  "SELECT COUNT(*) FROM delivery_package").fetchone()[0]
              == before)
        # 未映射层拒绝后同样不留包行。
        expect_error(
            "未映射层拒绝后不留包",
            lambda: asm.generate_delivery_package(
                "REQ-UNMAPPED", [p1, p4], 2, fx.TREE_TWO_LEAVES))
        check("未映射层被拒绝且没有包行",
              asm.conn.execute(
                  "SELECT COUNT(*) FROM delivery_package"
                  " WHERE request_key='REQ-UNMAPPED'").fetchone()[0] == 0)


# ================================================================ 4. 分组最优性
def test_grouping_optimality():
    section("4. 分组最优性：先最大化人数，再最大化深度和；不输出空组")
    with tempfile.TemporaryDirectory() as d:
        asm, plans, alt4, m = fresh(os.path.join(d, "opt.db"))
        p1, p4 = plans[1], plans[4]
        fx.grant(asm, plans, m, ["Q1", "Q2", "Q4", "Q5"], waves=(1, 4))

        res = asm.generate_delivery_package(
            "REQ-OPT", [p1, p4], 2, fx.TREE_TWO_LEAVES)
        gm = group_map(asm, res["package_id"])
        check("同为 4 人时选两片叶（深度和 8）而非父节点（深度和 0）",
              gm == {"L1": ["BU-01", "BU-02"], "L2": ["BU-04", "BU-05"]}, gm)

        # 嵌套树：P1 下只有一片叶 L1。若选 P1，则 Q1/Q2 归 P1，L2 仍归 L2。
        res2 = asm.generate_delivery_package(
            "REQ-NEST", [p1, p4], 2, fx.TREE_NESTED)
        gm2 = group_map(asm, res2["package_id"])
        check("嵌套树中人数相同则取更深节点（L1 而非 P1）",
              gm2 == {"L1": ["BU-01", "BU-02"],
                      "L2": ["BU-04", "BU-05"]}, gm2)

        # 同一包不能同时选一个节点与它的后代（无重复成员、无嵌套组）。
        pkg2 = res2["package_id"]
        labels = [r[0] for r in asm.conn.execute(
            "SELECT DISTINCT group_name FROM delivery_member"
            " WHERE package_id=?", (pkg2,))]
        check("选定节点构成反链：没有同时出现祖先与后代标签",
              not ({"P1", "L1"} <= set(labels)) and set(labels) <= {"L1", "L2"},
              labels)
        dup_members = asm.conn.execute(
            "SELECT member_ref, COUNT(*) c FROM delivery_member"
            " WHERE package_id=? GROUP BY member_ref HAVING c > 1",
            (pkg2,)).fetchall()
        check("没有成员被拆到多个组", dup_members == [])

        # 三片叶但 S3 只有 1 人（Q7）：根 G 收下全部 5 人，成员数更大，
        # 因此按“先尽可能多交付成员”应归到 G；不输出 1 人空组。
        fx.grant(asm, plans, m, ["Q7"], waves=(1, 4))
        res3 = asm.generate_delivery_package(
            "REQ-THREE", [p1, p4], 2, fx.TREE_THREE_LEAVES)
        pkg3 = res3["package_id"]
        gm3 = group_map(asm, pkg3)
        check("多人优先：5 人（含 S3 单人）归到父组 G",
              gm3 == {"G": ["BU-01", "BU-02", "BU-04", "BU-05", "BU-07"]},
              gm3)
        sizes = {g: len(v) for g, v in gm3.items()}
        check("每个公开组人数都 >= k", all(n >= 2 for n in sizes.values()),
              sizes)
        depths = [r[0] for r in asm.conn.execute(
            "SELECT DISTINCT group_depth FROM delivery_member"
            " WHERE package_id=?", (pkg3,))]
        check("公开组深度为根 0", depths == [0], depths)

        # 单叶 2 人、另叶 1 人、第三叶 1 人，k=2：根 4 人 > 单叶 2 人，
        # 因此按“先最大化人数”归到 G，不为凑数拆人。
        fx.revoke(asm, plans, m, "Q5", 1)   # 让 Q5 不再满足全部选定轮次
        res4 = asm.generate_delivery_package(
            "REQ-SPLIT", [p1, p4], 2, fx.TREE_THREE_LEAVES)
        gm4 = group_map(asm, res4["package_id"])
        check("4 人全部交付时归到父组 G（不为分组细而少交人）",
              gm4 == {"G": ["BU-01", "BU-02", "BU-04", "BU-07"]}, gm4)


def test_k_larger_than_leaf():
    section("5. k 大于叶容量时提升到父节点，且不允许同时选节点与后代")
    with tempfile.TemporaryDirectory() as d:
        asm, plans, alt4, m = fresh(os.path.join(d, "k3.db"))
        p1, p4 = plans[1], plans[4]
        fx.grant(asm, plans, m, ["Q1", "Q2", "Q4", "Q5"], waves=(1, 4))
        # k=3：两片叶各 2 人都不达标，根 4 人达标 -> 归到根 G。
        res = asm.generate_delivery_package(
            "REQ-K3", [p1, p4], 3, fx.TREE_TWO_LEAVES)
        gm = group_map(asm, res["package_id"])
        check("k=3 时两叶均不达标，整体归到父组 G",
              gm == {"G": ["BU-01", "BU-02", "BU-04", "BU-05"]}, gm)
        check("不存在同时选 G 与叶的分组（无重复成员、无嵌套组）",
              set(gm) == {"G"})


# ================================================================ 6. 撤回与停领
def test_revocation_stops_package():
    section("6. 撤回已交付成员 -> 旧包永久停领；新请求键重算归父组")
    with tempfile.TemporaryDirectory() as d:
        asm, plans, alt4, m = fresh(os.path.join(d, "revoke.db"))
        p1, p4 = plans[1], plans[4]
        fx.grant(asm, plans, m, ["Q1", "Q2", "Q4", "Q5"], waves=(1, 4))
        res = asm.generate_delivery_package(
            "REQ-REVOKE", [p1, p4], 2, fx.TREE_TWO_LEAVES)
        pkg = res["package_id"]
        check("旧包初始可领取", asm.claim_delivery_package(pkg)["package_id"] == pkg)

        # 撤回 Q4 在选定轮次 w1 的授权 -> 旧包停领。
        out = fx.revoke(asm, plans, m, "Q4", 1)
        check("撤回报告停领的包包含旧包", pkg in out["stopped_packages"], out)
        expect_error("撤回后旧包不可领取",
                     lambda: asm.claim_delivery_package(pkg))
        # 再次授予也不恢复。
        fx.grant(asm, plans, m, ["Q4"], waves=(1, 4))
        expect_error("再次授予后旧包仍不可领取",
                     lambda: asm.claim_delivery_package(pkg))
        # 同请求键重试也不得绕过停领。
        expect_error("同请求键重试旧包被拒绝",
                     lambda: asm.generate_delivery_package(
                         "REQ-REVOKE", [p1, p4], 2, fx.TREE_TWO_LEAVES))

        # 新请求键重算：Q4 的 w1 已撤回、随即再次授予 -> 仍是候选；
        # 旧包（生成于撤回前）保持永久停领，新键按最新授权重新计算。
        res2 = asm.generate_delivery_package(
            "REQ-REVOKE-2", [p1, p4], 2, fx.TREE_TWO_LEAVES)
        pkg2 = res2["package_id"]
        check("新请求键生成新包标识", pkg2 != pkg)
        gm2 = group_map(asm, pkg2)
        check("新键按当前授权重算，四人均回到候选",
              gm2 == {"L1": ["BU-01", "BU-02"],
                      "L2": ["BU-04", "BU-05"]}, gm2)
        check("新包可领取", asm.claim_delivery_package(pkg2)["package_id"] == pkg2)
        check("旧包仍然停领（新键不改变旧包状态）",
              _claim_fails(asm, pkg))


def test_revocation_three_remaining_go_to_parent():
    section("7. 两兄弟叶各 2 人：撤回 1 人后新键把剩余 3 人归到父组")
    with tempfile.TemporaryDirectory() as d:
        asm, plans, alt4, m = fresh(os.path.join(d, "three.db"))
        p1, p4 = plans[1], plans[4]
        fx.grant(asm, plans, m, ["Q1", "Q2", "Q4", "Q5"], waves=(1, 4))
        pkg = asm.generate_delivery_package(
            "REQ-THREE-1", [p1, p4], 2, fx.TREE_TWO_LEAVES)["package_id"]
        check("初始两叶各 2 人",
              group_map(asm, pkg) == {"L1": ["BU-01", "BU-02"],
                                      "L2": ["BU-04", "BU-05"]})

        # 撤回 Q4（L2 一人）在 w4 的授权：旧包停领。
        fx.revoke(asm, plans, m, "Q4", 4)
        expect_error("旧包停领", lambda: asm.claim_delivery_package(pkg))

        # 新请求键：剩 Q1/Q2(S1) + Q5(S2) 共 3 人。k=2 时单叶只剩 1 人，
        # 只能整体归到父组 G（人数 3 最大，深度 0）。
        res = asm.generate_delivery_package(
            "REQ-THREE-2", [p1, p4], 2, fx.TREE_TWO_LEAVES)
        gm = group_map(asm, res["package_id"])
        check("新包把剩余三人归到父组 G",
              gm == {"G": ["BU-01", "BU-02", "BU-05"]}, gm)
        check("父组人数 3 >= k", len(gm["G"]) == 3)


def test_untouched_revocations_do_not_stop():
    section("8. 未交付成员 / 未选轮次的撤回不影响旧包")
    with tempfile.TemporaryDirectory() as d:
        asm, plans, alt4, m = fresh(os.path.join(d, "untouched.db"))
        p1, p2, p3, p4 = plans[1], plans[2], plans[3], plans[4]
        fx.grant(asm, plans, m, ["Q1", "Q2", "Q4", "Q5"], waves=(1, 4))
        # 未交付成员 Q6 也登记授权（但它 w4 缺席 -> 不是候选）；
        # Q7 是候选但 S3 未映射（两叶树）-> 拒绝交付，因此不在本节使用。
        fx.grant(asm, plans, m, ["Q6"], waves=(1,))
        pkg = asm.generate_delivery_package(
            "REQ-UNTOUCH", [p1, p4], 2, fx.TREE_TWO_LEAVES)["package_id"]

        # 撤回未交付成员 Q6 在 w1 的授权：不影响。
        fx.revoke(asm, plans, m, "Q6", 1)
        check("未交付成员在选定计划的撤回不影响旧包",
              asm.claim_delivery_package(pkg)["package_id"] == pkg)

        # 撤回已交付成员 Q1 在**未选轮次** w2 的授权：不影响。
        # 先确认 Q1 在 w2 是在样记录（是），再撤回。
        out = fx.revoke(asm, plans, m, "Q1", 2)
        check("未选轮次的撤回不产生停领", out["stopped_packages"] == [], out)
        check("已交付成员在未选轮次撤回后旧包仍可领取",
              asm.claim_delivery_package(pkg)["package_id"] == pkg)

        # 撤回已交付成员 Q1 在选定计划 w4 的授权 -> 停领。
        fx.revoke(asm, plans, m, "Q1", 4)
        expect_error("已交付成员在选定计划撤回后停领",
                     lambda: asm.claim_delivery_package(pkg))


# ================================================================ 9. 幂等
def test_idempotency_and_immutability():
    section("9. 请求幂等、同键改规格报错、答案修订不改已生成答卷")
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "idem.db")
        asm, plans, alt4, m = fresh(db)
        p1, p4 = plans[1], plans[4]
        fx.grant(asm, plans, m, ["Q1", "Q2", "Q4", "Q5"], waves=(1, 4))
        first = asm.generate_delivery_package(
            "REQ-IDEM", [p1, p4], 2, fx.TREE_TWO_LEAVES)
        pkg = first["package_id"]

        # 轮次输入乱序。
        again = asm.generate_delivery_package(
            "REQ-IDEM", [p4, p1], 2, fx.TREE_TWO_LEAVES)
        check("同键 + 轮次乱序返回原包（幂等）",
              again["package_id"] == pkg and again["idempotent"])

        # 树兄弟顺序不同：still same。
        swapped = {"label": "G", "children": [
            {"label": "L2", "stratum": "S2"},
            {"label": "L1", "stratum": "S1"}]}
        again2 = asm.generate_delivery_package(
            "REQ-IDEM", [p1, p4], 2, swapped)
        check("同键 + 树兄弟顺序不同返回原包",
              again2["package_id"] == pkg and again2["idempotent"])

        # 同键改规格：k 变、树变、轮次变 都报错。
        expect_error("同键改 k 报错",
                     lambda: asm.generate_delivery_package(
                         "REQ-IDEM", [p1, p4], 3, fx.TREE_TWO_LEAVES))
        expect_error("同键改轮次报错",
                     lambda: asm.generate_delivery_package(
                         "REQ-IDEM", [plans[1], plans[2]], 2,
                         fx.TREE_TWO_LEAVES))
        expect_error("同键改分组树报错",
                     lambda: asm.generate_delivery_package(
                         "REQ-IDEM", [p1, p4], 2, fx.TREE_THREE_LEAVES))
        check("改规格失败后原包仍可领取",
              asm.claim_delivery_package(pkg)["package_id"] == pkg)

        # 答案后来修订：已生成答卷不变。
        before = asm.claim_delivery_package(pkg)["members"]
        asm.submit_answer_revisions([
            {"plan_id": p4, "panel_member_id": m["Q1"],
             "revision": 2, "answer": 1}])
        after = asm.claim_delivery_package(pkg)["members"]
        check("答案修订不改变已经生成的答卷", before == after)
        am = answers_by_bu(asm, pkg)
        check("旧包答卷仍是生成时的 [0,0]（Q1）",
              am["BU-01"] == [0, 0], am)

        # 重开数据库：授权/停领/幂等仍持久。
        asm.close()
        asm2 = SampleAssembler(db)
        check("重开后同键仍返回原包",
              asm2.generate_delivery_package(
                  "REQ-IDEM", [p1, p4], 2, fx.TREE_TWO_LEAVES)["package_id"]
              == pkg)
        check("重开后旧包仍可领取",
              asm2.claim_delivery_package(pkg)["package_id"] == pkg)
        fx.revoke(asm2, plans, m, "Q2", 4)
        expect_error("重开后撤回仍使旧包停领",
                     lambda: asm2.claim_delivery_package(pkg))
        check("重开后撤回记录持久：再次打开仍停领",
              _reopen_still_stopped(db, pkg))
        asm2.close()


def _reopen_still_stopped(db_path, pkg):
    asm = SampleAssembler(db_path)
    try:
        asm.claim_delivery_package(pkg)
        return False
    except DeliveryError:
        return True
    finally:
        asm.close()


def test_reopen_reauth_cycle():
    section("10. 重开库后的撤回再授权：旧包不恢复、新键按最新授权重算")
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "cycle.db")
        asm, plans, alt4, m = fresh(db)
        p1, p4 = plans[1], plans[4]
        fx.grant(asm, plans, m, ["Q1", "Q2", "Q4", "Q5"], waves=(1, 4))
        pkg = asm.generate_delivery_package(
            "REQ-CYCLE", [p1, p4], 2, fx.TREE_TWO_LEAVES)["package_id"]
        asm.close()

        asm = SampleAssembler(db)
        fx.revoke(asm, plans, m, "Q4", 1)
        asm.close()

        asm = SampleAssembler(db)
        check("重开后撤回仍生效：旧包停领", _claim_fails(asm, pkg))
        fx.grant(asm, plans, m, ["Q4"], waves=(1, 4))
        check("重开后再次授予不恢复旧包", _claim_fails(asm, pkg))
        res = asm.generate_delivery_package(
            "REQ-CYCLE-2", [p1, p4], 2, fx.TREE_TWO_LEAVES)
        gm = group_map(asm, res["package_id"])
        check("新键按最新授权重算，Q4 回到候选",
              gm == {"L1": ["BU-01", "BU-02"],
                     "L2": ["BU-04", "BU-05"]}, gm)
        asm.close()


def _claim_fails(asm, pkg):
    try:
        asm.claim_delivery_package(pkg)
        return False
    except DeliveryError:
        return True


# ================================================================ 并发
def test_concurrent_generate_revoke():
    section("13. 两连接：生成与撤回交错，成功生成的包必须是完整可领取版本")
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "genconc.db")
        asm, plans, alt4, m = fresh(db)
        p1, p4 = plans[1], plans[4]
        fx.grant(asm, plans, m, ["Q1", "Q2", "Q4", "Q5"], waves=(1, 4))
        asm.close()

        writer = SampleAssembler(db)
        creator = SampleAssembler(db)
        stop = threading.Event()
        outcomes = []

        def revoker():
            import time
            time.sleep(0.03)
            fx.revoke(writer, plans, m, "Q4", 4)
            stop.set()

        def generator():
            import time
            i = 0
            deadline = time.time() + 8
            while time.time() < deadline and not stop.is_set():
                key = "REQ-GEN-%d" % i
                i += 1
                try:
                    res = creator.generate_delivery_package(
                        key, [p1, p4], 2, fx.TREE_TWO_LEAVES)
                    outcomes.append(("ok", res))
                except DeliveryError as e:
                    outcomes.append(("err", str(e)))
            # 撤回提交后：再生成一次，必须拒绝（Q4 已无 w4 授权）。
            try:
                res = creator.generate_delivery_package(
                    "REQ-GEN-AFTER", [p1, p4], 2, fx.TREE_TWO_LEAVES)
                outcomes.append(("ok", res))
            except DeliveryError as e:
                outcomes.append(("err", str(e)))

        t1 = threading.Thread(target=revoker)
        t2 = threading.Thread(target=generator)
        t1.start(); t2.start()
        t1.join(timeout=60); t2.join(timeout=60)

        ok = [r for kind, r in outcomes if kind == "ok"]
        errs = [r for kind, r in outcomes if kind == "err"]
        check("至少有一个包成功生成", bool(ok), str(len(ok)))
        check("交错中每个包都是完整的 4 人或完整重算的 3 人",
              all(len(r["members"]) in (3, 4) for r in ok),
              sorted({len(r["members"]) for r in ok}))
        # 每个成功生成的包都与某个完整授权快照一致：要么撤回前（4 人 L1/L2），
        # 要么撤回已提交（Q4 不可用 -> 3 人归父组 G 或拒绝）；不存在
        # “3 人却按 L1/L2 分组”或含 1 人组的半撤回版本。
        consistent = True
        for r in ok:
            groups = {x["group"] for x in r["members"]}
            if len(r["members"]) == 4:
                consistent &= groups == {"L1", "L2"}
            elif len(r["members"]) == 3:
                consistent &= groups == {"G"}
            else:
                consistent = False
        check("每个生成的包都与某个完整的授权快照一致（无半撤回版本）",
              consistent, [(len(r["members"]),
                            sorted({x["group"] for x in r["members"]}))
                           for r in ok])

        # 最终状态：旧包（若有）在撤回后停领；新键只反映 3 人候选。
        final = creator.generate_delivery_package(
            "REQ-GEN-FINAL", [p1, p4], 2, fx.TREE_TWO_LEAVES)
        check("撤回后最终重算只剩 3 人且归父组 G",
              len(final["members"]) == 3
              and {x["group"] for x in final["members"]} == {"G"},
              [(x["group"], x["answers"]) for x in final["members"]])
        writer.close(); creator.close()


def test_concurrent_claim_revoke():
    section("11. 两连接交错：领取结果只能是完整且仍可领取的版本")
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "conc.db")
        asm, plans, alt4, m = fresh(db)
        p1, p4 = plans[1], plans[4]
        fx.grant(asm, plans, m, ["Q1", "Q2", "Q4", "Q5"], waves=(1, 4))
        pkg = asm.generate_delivery_package(
            "REQ-CONC", [p1, p4], 2, fx.TREE_TWO_LEAVES)["package_id"]
        expected = asm.claim_delivery_package(pkg)["members"]

        writer = SampleAssembler(db)
        reader = SampleAssembler(db)
        results, errors = [], []
        stop = threading.Event()

        def do_revoke():
            # 先让领取跑起来，再在另一连接上撤回。
            import time
            time.sleep(0.05)
            fx.revoke(writer, plans, m, "Q4", 4)
            stop.set()

        def do_claim():
            # 持续领取直到看到停领错误或超时；每条成功结果都必须是完整答卷。
            import time
            deadline = time.time() + 10
            while time.time() < deadline and not stop.is_set():
                try:
                    results.append(reader.claim_delivery_package(pkg))
                except DeliveryError as e:
                    errors.append(str(e))
                    return
            # 撤回已提交仍可领取 -> 至少补一次，必须失败。
            try:
                results.append(reader.claim_delivery_package(pkg))
                errors.append("撤回后仍成功领取")
            except DeliveryError as e:
                errors.append(str(e))

        t1 = threading.Thread(target=do_revoke)
        t2 = threading.Thread(target=do_claim)
        t1.start(); t2.start()
        t1.join(timeout=60); t2.join(timeout=60)

        # 撤回是幂等/一次性的；重复多次撤回以覆盖交错窗口。
        for _ in range(9):
            try:
                fx.revoke(writer, plans, m, "Q4", 4)
            except Exception:
                pass

        check("交错期间领取到的每个结果都与完整答卷一致",
              all(r["members"] == expected for r in results),
              "领取 %d 次" % len(results))
        check("撤回提交后领取必然失败（无半撤回结果）",
              errors and errors[-1] != "撤回后仍成功领取",
              "成功 %d 次，错误 %r" % (len(results), errors[-1:]))
        # 撤回已提交，最终状态必须停领。
        check("最终包已停领", _claim_fails(reader, pkg))
        check("停领标记唯一且持久",
              reader.conn.execute(
                  "SELECT COUNT(*) FROM delivery_revocation WHERE package_id=?",
                  (pkg,)).fetchone()[0] == 1)

        # 另一条连接上的新键应看到 Q4 不是候选（授权已持久撤回）。
        res = reader.generate_delivery_package(
            "REQ-CONC-2", [p1, p4], 2, fx.TREE_TWO_LEAVES)
        check("另一连接的领取只能对应完整版本：新包不含 Q4",
              "BU-04" not in answers_by_bu(reader, res["package_id"]),
              group_map(reader, res["package_id"]))
        writer.close(); reader.close()


def test_concurrent_claim_sees_full_version():
    section("12. 两连接：撤回提交前后领取到的都只能是完整且可领取的答卷")
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "conc2.db")
        asm, plans, alt4, m = fresh(db)
        p1, p4 = plans[1], plans[4]
        fx.grant(asm, plans, m, ["Q1", "Q2", "Q4", "Q5"], waves=(1, 4))
        pkg = asm.generate_delivery_package(
            "REQ-CONC-B", [p1, p4], 2, fx.TREE_TWO_LEAVES)["package_id"]
        expected = asm.claim_delivery_package(pkg)["members"]
        asm.close()

        writer = SampleAssembler(db)
        reader = SampleAssembler(db)
        seen, failures = [], []
        start = threading.Event()
        stopped = threading.Event()

        def writer_loop():
            start.wait()
            # 撤回一次即可停领；随后反复“再授予 + 再撤回”，验证授权反复
            # 变化时，领取端也只会看到“完整且仍可领取”或明确停领两种状态。
            fx.revoke(writer, plans, m, "Q4", 4)
            stopped.set()
            for _ in range(5):
                fx.grant(writer, plans, m, ["Q4"], waves=(4,))
                fx.revoke(writer, plans, m, "Q4", 4)

        def reader_loop():
            import time
            start.wait()
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    seen.append(reader.claim_delivery_package(pkg))
                except DeliveryError as e:
                    failures.append(str(e))
                    return
                if stopped.is_set():
                    break
            # 撤回已提交：再领取一次，必须失败。
            try:
                seen.append(reader.claim_delivery_package(pkg))
            except DeliveryError as e:
                failures.append(str(e))

        tw = threading.Thread(target=writer_loop)
        tr = threading.Thread(target=reader_loop)
        tw.start(); tr.start()
        start.set()
        tw.join(timeout=60); tr.join(timeout=60)

        check("所有成功领取都返回完整、未修改的答卷",
              all(r["members"] == expected for r in seen),
              "%d 次成功" % len(seen))
        check("撤回一旦提交，之后领取全部失败",
              bool(failures) and _claim_fails(reader, pkg),
              "失败 %d 次" % len(failures))
        # 没有任何结果只含部分成员或半撤回答案。
        check("不存在成员数不完整的领取结果",
              all(len(r["members"]) == 4 for r in seen))
        writer.close(); reader.close()


# ================================================================ 主流程
def main():
    test_basic_two_leaves()
    test_skipped_wave_semantics()
    test_spec_validation()
    test_grouping_optimality()
    test_k_larger_than_leaf()
    test_revocation_stops_package()
    test_revocation_three_remaining_go_to_parent()
    test_untouched_revocations_do_not_stop()
    test_idempotency_and_immutability()
    test_reopen_reauth_cycle()
    test_concurrent_claim_revoke()
    test_concurrent_claim_sees_full_version()
    test_concurrent_generate_revoke()
    print("\n结果：%d 通过, %d 失败" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
