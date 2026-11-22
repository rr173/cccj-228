# -*- coding: utf-8 -*-
"""研究交付包：管理员登记交付授权，课题组按请求键领取跨轮次答卷。

本模块只读既有 panel 计划/账本与回答转移表，不重新计算样本、轮换或访谈
配对；既有调查表、轮换规则与回答转移表行为不变。

交付规格（一次请求）
--------------------

- 一个已经绑定回答转移表的库（``answer_transfer_binding``）；
- 该绑定链上至少两个**不同**的已发布 ``plan_id``（按链序使用，输入顺序
  无关；同轮的其他计划不能替代链上计划）；
- 最小人数 ``k >= 2``；
- 一棵节点标签唯一的分组树，叶子显式对应分层代码（一层只对应一片叶）；
- 请求键 ``request_key``。

候选成员
--------

在**每个**选定计划都有「在样记录 + 有效回答 + 交付授权」，并在第一个与
最后一个选定计划之间沿**同一在样区段**贯穿的成员。中间没被选为答卷列的
轮次仍要求在样（同一区段），但不要求那些轮次有答案或授权。

分组
----

以**第一个选定计划**账本记录的层作为成员分组起点，不随后续轮次改层而漂移。
选定节点收下其子树内的全部候选；同一包内选定节点构成反链（不能同时选某
节点与它的后代），每个选定组的成员数必须达到 ``k``；可以整棵子树不交付。
优化目标：先最大化交付人数，再最大化各成员公开组深度之和（根深度 0）。
结果不输出空组，也不以额外表披露更细的分组。

授权
----

管理员按 ``(plan_id, panel_member_id)`` 授予/撤回，持久生效；未显式授予
视为无权；只有绑定链上的在样记录能登记。

停领
----

若任一**已交付**成员在包的某个**选定**计划上的授权后来被撤回，整个包
此后永久停止领取（再次授予也不恢复）；需要出库请用新请求键重新计算。
撤回与停领标记在同一个 ``BEGIN IMMEDIATE`` 事务内落库；领取事务同样以
``BEGIN IMMEDIATE`` 读取并在同一事务内校验停领，因此不存在“已撤回但仍可
领取”的可见中间状态。未实际交付的成员、未选轮次的撤回不会使包失效。

幂等
----

同一请求键 + 同一规格（轮次输入乱序、树兄弟顺序都不算更改）返回原包标识；
同键改规格报错。答案之后修订不改变已经生成的答卷（答卷是生成时的快照）。
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone

from .errors import DeliveryError


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _new_package_id():
    return "DP_%s" % uuid.uuid4().hex


def _new_code():
    # 包内随机代号：与原成员标识无关，也不跨包稳定。
    return "C%s" % uuid.uuid4().hex[:16]


# ================================================================ 分组树
class _Node:
    __slots__ = ("label", "stratum", "children", "parent", "depth", "members")

    def __init__(self, label, stratum, depth):
        self.label = label
        self.stratum = stratum
        self.children = []
        self.parent = None
        self.depth = depth
        self.members = []      # 叶子：候选成员引用（panel_member_id）

    def __repr__(self):
        return "<Node %s depth=%d>" % (self.label, self.depth)


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _build_tree(spec):
    """解析并校验一棵节点标签唯一的树；返回 (root, leaves_by_stratum)。

    节点结构: {"label": str, "stratum": str|None, "children": [...]}。
    - 标签全局唯一且非空；子节点列表为空时视为叶子，必须给出 stratum。
    - 非叶节点不得给出 stratum（层只由叶子对应）。
    - 同一层只能对应一片叶；不要求每片叶子都用上。
    """
    if not isinstance(spec, dict):
        raise DeliveryError("分组树必须是 dict 节点")
    seen = set()

    def walk(raw, depth):
        if not isinstance(raw, dict):
            raise DeliveryError("分组树节点必须是 dict")
        label = raw.get("label")
        if not isinstance(label, str) or not label.strip():
            raise DeliveryError("分组树节点缺少非空 label")
        if label in seen:
            raise DeliveryError("分组树节点 label 重复: %r" % label)
        seen.add(label)
        stratum = raw.get("stratum")
        children_raw = raw.get("children") or []
        if not isinstance(children_raw, list):
            raise DeliveryError("节点 %r 的 children 必须是列表" % label)
        node = _Node(label, stratum, depth)
        if not children_raw:
            if not isinstance(stratum, str) or not stratum:
                raise DeliveryError("叶节点 %r 必须显式对应一个 stratum" % label)
        else:
            if stratum is not None:
                raise DeliveryError(
                    "非叶节点 %r 不能带 stratum（层只由叶子对应）" % label)
            for child_raw in children_raw:
                child = walk(child_raw, depth + 1)
                child.parent = node
                node.children.append(child)
        return node

    root = walk(spec, 0)
    leaves, duplicated = {}, []
    for node in _walk(root):
        if node.children:
            continue
        if node.stratum in leaves:
            duplicated.append(node.stratum)
            continue
        leaves[node.stratum] = node
    if duplicated:
        raise DeliveryError(
            "分层代码 %r 同时对应多片叶节点（一层只能对应一片叶）"
            % sorted(set(duplicated)))
    return root, leaves


def _canonical_tree(spec):
    """树的结构化规范：兄弟顺序无关；与输入顺序、空白都无关。"""
    if not isinstance(spec, dict):
        raise DeliveryError("分组树必须是 dict 节点")
    children = spec.get("children") or []
    if not isinstance(children, list):
        raise DeliveryError("children 必须是列表")
    canonical_children = sorted(
        (_canonical_tree(c) for c in children),
        key=lambda c: json.dumps(c, ensure_ascii=False, sort_keys=True),
    )
    return {
        "label": spec.get("label"),
        "stratum": spec.get("stratum"),
        "children": canonical_children,
    }


# ================================================================ 规格校验
def _normalize_plan_ids(plan_ids):
    if not isinstance(plan_ids, (list, tuple)) or not plan_ids:
        raise DeliveryError("plan_ids 必须是非空列表")
    out = []
    for pid in plan_ids:
        if not isinstance(pid, int) or isinstance(pid, bool):
            raise DeliveryError("plan_id 必须是整数: %r" % (pid,))
        if pid not in out:
            out.append(pid)
    if len(out) < 2:
        raise DeliveryError("一次交付至少需要两个不同的已发布 plan_id")
    return out


def _normalize_k(k):
    if not isinstance(k, int) or isinstance(k, bool) or k < 2:
        raise DeliveryError("最小人数 k 必须是 >=2 的整数")
    return k


def _fingerprint(chain_plan_ids, k, tree_spec):
    payload = json.dumps(
        {"plans": list(chain_plan_ids), "k": k,
         "tree": _canonical_tree(tree_spec)},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_binding_chain(conn):
    return [dict(r) for r in conn.execute(
        "SELECT plan_id, wave, chain_index, base_plan_id"
        " FROM answer_transfer_chain ORDER BY chain_index")]


def _resolve_selected(chain, plan_ids):
    """把调用方给出的 plan_id 映射到绑定链上的位置（按链序返回）。

    只接受绑定链上的计划；同轮其他计划不在链上 -> 拒绝。
    """
    index_of = {node["plan_id"]: node["chain_index"] for node in chain}
    missing = [pid for pid in plan_ids if pid not in index_of]
    if missing:
        raise DeliveryError(
            "plan_id %r 不在当前绑定的已发布计划链上（同轮其他计划不能替代）"
            % (missing,))
    selected = sorted(index_of[pid] for pid in plan_ids)
    if len(selected) < 2:
        raise DeliveryError("一次交付至少需要两个不同的已发布 plan_id")
    return selected


# ================================================================ 授权登记
def _validate_authorization_target(conn, plan_id, panel_member_id):
    if not isinstance(plan_id, int) or isinstance(plan_id, bool):
        raise DeliveryError("plan_id 必须是整数")
    if not isinstance(panel_member_id, str) or not panel_member_id:
        raise DeliveryError("panel_member_id 必须是非空字符串")
    if conn.execute(
        "SELECT 1 FROM answer_transfer_binding WHERE id=1").fetchone() is None:
        raise DeliveryError("尚未初始化回答转移表（先绑定端点计划）")
    if conn.execute(
        "SELECT 1 FROM answer_transfer_sample WHERE plan_id=?"
        " AND panel_member_id=?", (plan_id, panel_member_id)).fetchone() is None:
        raise DeliveryError(
            "只能对绑定链上的在样记录登记授权: plan_id=%s member=%s"
            % (plan_id, panel_member_id))


def grant_authorization(conn, plan_id, panel_member_id):
    """显式授予某个链上在样记录在某一轮的交付授权；重复授予幂等。"""
    _validate_authorization_target(conn, plan_id, panel_member_id)
    conn.execute("BEGIN IMMEDIATE")
    try:
        _validate_authorization_target(conn, plan_id, panel_member_id)
        now = _now_iso()
        conn.execute(
            "INSERT INTO delivery_authorization(plan_id, panel_member_id,"
            " authorized, granted_at, revoked_at, updated_at)"
            " VALUES (?,?,1,?,NULL,?)"
            " ON CONFLICT(plan_id, panel_member_id) DO UPDATE SET"
            " authorized=1, granted_at=excluded.granted_at,"
            " revoked_at=NULL, updated_at=excluded.updated_at",
            (plan_id, panel_member_id, now, now),
        )
        conn.execute("COMMIT")
        return {"plan_id": plan_id, "panel_member_id": panel_member_id,
                "authorized": True}
    except Exception:
        conn.execute("ROLLBACK")
        raise


def revoke_authorization(conn, plan_id, panel_member_id):
    """撤回授权；持久生效，并把受影响的已生成交付包标记为永久停领。

    - 只有该成员是**已交付成员**且该计划是**选定答卷列**时才使整包停领；
      未实际交付的成员 / 未选轮次的撤回不影响旧包。
    - 停领标记与撤回在同一 ``BEGIN IMMEDIATE`` 事务内写入；再次授予不会
      清除停领标记。
    """
    _validate_authorization_target(conn, plan_id, panel_member_id)
    conn.execute("BEGIN IMMEDIATE")
    try:
        _validate_authorization_target(conn, plan_id, panel_member_id)
        now = _now_iso()
        conn.execute(
            "INSERT INTO delivery_authorization(plan_id, panel_member_id,"
            " authorized, granted_at, revoked_at, updated_at)"
            " VALUES (?,?,0,NULL,?,?)"
            " ON CONFLICT(plan_id, panel_member_id) DO UPDATE SET"
            " authorized=0, revoked_at=excluded.revoked_at,"
            " updated_at=excluded.updated_at",
            (plan_id, panel_member_id, now, now),
        )
        affected = [r[0] for r in conn.execute(
            "SELECT DISTINCT dm.package_id FROM delivery_member dm"
            " JOIN delivery_package_plan dpp ON dpp.package_id=dm.package_id"
            " WHERE dm.member_ref=? AND dpp.plan_id=?",
            (panel_member_id, plan_id),
        )]
        for package_id in affected:
            conn.execute(
                "INSERT OR IGNORE INTO delivery_revocation(package_id,"
                " plan_id, panel_member_id, revoked_at) VALUES (?,?,?,?)",
                (package_id, plan_id, panel_member_id, now),
            )
        conn.execute("COMMIT")
        return {"plan_id": plan_id, "panel_member_id": panel_member_id,
                "authorized": False, "stopped_packages": sorted(affected)}
    except Exception:
        conn.execute("ROLLBACK")
        raise


def list_authorizations(conn, plan_id=None):
    """管理员核对用：当前授权状态（内部接口，不属于课题组领取面）。"""
    sql = ("SELECT plan_id, panel_member_id, authorized, granted_at,"
           " revoked_at, updated_at FROM delivery_authorization")
    params = ()
    if plan_id is not None:
        sql += " WHERE plan_id=?"
        params = (plan_id,)
    sql += " ORDER BY plan_id, panel_member_id"
    out = []
    for r in conn.execute(sql, params):
        d = dict(r)
        d["authorized"] = bool(d["authorized"])
        out.append(d)
    return out


# ================================================================ 候选集
def _candidates(conn, chain, selected):
    """计算候选成员：每个选定计划都有在样 + 有效答案 + 授权，且同一区段贯穿。

    返回 [(member_ref, first_stratum, answers)]，answers 按选定计划链序。
    selected: 选定计划在绑定链上的 chain_index 列表（升序）。
    """
    first_idx, last_idx = selected[0], selected[-1]

    rows = conn.execute(
        "SELECT s.panel_member_id, s.chain_index, s.segment_no,"
        " s.stratum_code, e.answer, da.authorized"
        " FROM answer_transfer_sample s"
        " LEFT JOIN answer_effective e ON e.plan_id=s.plan_id"
        " AND e.panel_member_id=s.panel_member_id"
        " LEFT JOIN delivery_authorization da ON da.plan_id=s.plan_id"
        " AND da.panel_member_id=s.panel_member_id"
        " WHERE s.chain_index>=? AND s.chain_index<=?",
        (first_idx, last_idx),
    ).fetchall()

    by_member = {}
    for r in rows:
        by_member.setdefault(r["panel_member_id"], {})[r["chain_index"]] = r

    out = []
    for member_id, at in by_member.items():
        # 同一在样区段必须覆盖 [first_idx, last_idx] 的每个链位置。
        if any(i not in at for i in range(first_idx, last_idx + 1)):
            continue
        if len({at[i]["segment_no"]
                for i in range(first_idx, last_idx + 1)}) != 1:
            continue
        answers = []
        ok = True
        for idx in selected:
            r = at[idx]
            # 选定计划：必须有效答案 + 显式授权。
            if r["answer"] is None or r["authorized"] != 1 \
                    or r["stratum_code"] is None:
                ok = False
                break
            answers.append(int(r["answer"]))
        if not ok:
            continue
        out.append((member_id, at[first_idx]["stratum_code"], answers))
    out.sort(key=lambda x: x[0])
    return out


# ================================================================ 分组求解
def _solve_grouping(root, leaf_by_stratum, candidates, k):
    """在分组树上选反链节点，按（交付人数, 深度和）字典序最大化。

    每个候选按第一个选定计划的层落到唯一叶子上；候选中的层没有对应叶子
    就拒绝本次交付（可以整片叶子不交付，但不能有候选层无叶）。

    DP（自底向上），每个节点保留：
      take(v) = (子树候选总数, 子树候选总数 * v.depth)   仅当总数 >= k
      skip(v) = 各子节点最优结果之和
    取字典序更大者；成员数与深度和都相同的退化平手时保留不选（更保守，
    不额外披露更细的分组）。
    """
    for node in _walk(root):
        node.members = []
    for member_id, stratum, _answers in candidates:
        leaf = leaf_by_stratum.get(stratum)
        if leaf is None:
            raise DeliveryError(
                "候选成员中存在未映射的分层代码 %r（分组树必须为本次候选"
                "出现的每个层显式指定一片叶子）" % stratum)
        leaf.members.append(member_id)

    counts = {}

    def count_of(node):
        if node.label not in counts:
            counts[node.label] = (
                len(node.members) if not node.children
                else sum(count_of(c) for c in node.children))
        return counts[node.label]

    count_of(root)

    best = {}

    def dp(node):
        if node.label in best:
            return best[node.label]
        skip_m, skip_d = 0, 0
        for child in node.children:
            m, d, _kind = dp(child)
            skip_m += m
            skip_d += d
        options = [(skip_m, skip_d, "skip")]
        total = count_of(node)
        if total >= k:
            options.append((total, total * node.depth, "take"))
        chosen = options[0]
        for cand in options[1:]:
            if (cand[0], cand[1]) > (chosen[0], chosen[1]):
                chosen = cand
        best[node.label] = chosen
        return chosen

    dp(root)

    def reconstruct(node):
        _m, _d, kind = best[node.label]
        if kind == "take":
            return [node]
        selected = []
        for child in node.children:
            selected.extend(reconstruct(child))
        return selected

    return reconstruct(root)


def _assign_members(candidates, chosen_nodes):
    """按选定节点（反链）把候选成员归到公开组；返回 (member, node, answers)。"""
    info = {m[0]: m[2] for m in candidates}
    out = []
    for node in chosen_nodes:
        for leaf in _walk(node):
            if leaf.children:
                continue
            for member_id in leaf.members:
                out.append((member_id, node, info[member_id]))
    refs = [m[0] for m in out]
    if len(refs) != len(set(refs)):
        raise DeliveryError("分组求解产生重复成员（内部不一致）")
    out.sort(key=lambda x: x[0])
    return out


# ================================================================ 生成交付包
def generate_package(asm, request_key, plan_ids, k, group_tree,
                     endpoint_plan_id=None):
    """生成交付包：生成 + 完整结果 + 可领取状态在同一条提交里落库。

    返回 ``{"package_id", "rounds", "members", "idempotent", "created"}``；
    ``members`` 是课题组可见的完整答卷。
    """
    conn = asm.conn
    if not isinstance(request_key, str) or not request_key:
        raise DeliveryError("request_key 必须是非空字符串")
    k = _normalize_k(k)
    requested = _normalize_plan_ids(plan_ids)
    _build_tree(group_tree)  # 结构/标签/叶映射先校验（不写库）

    conn.execute("BEGIN IMMEDIATE")
    try:
        binding = conn.execute(
            "SELECT endpoint_plan_id FROM answer_transfer_binding WHERE id=1"
        ).fetchone()
        if binding is None:
            raise DeliveryError("尚未初始化回答转移表（先绑定端点计划）")
        if endpoint_plan_id is not None \
                and int(endpoint_plan_id) != binding["endpoint_plan_id"]:
            raise DeliveryError(
                "交付请求的 endpoint_plan_id 与库内绑定不一致（%s != %s）"
                % (endpoint_plan_id, binding["endpoint_plan_id"]))

        chain = _load_binding_chain(conn)
        selected = _resolve_selected(chain, requested)
        selected_plan_ids = [chain[i]["plan_id"] for i in selected]
        fp = _fingerprint(selected_plan_ids, k, group_tree)

        existing = conn.execute(
            "SELECT package_id, spec_fingerprint FROM delivery_package"
            " WHERE request_key=?", (request_key,)).fetchone()
        if existing is not None:
            if fp != existing["spec_fingerprint"]:
                raise DeliveryError(
                    "同一 request_key 的交付规格已变更"
                    "（轮次/最小人数/分组树），不能返回原包")
            # 已生成的包只按创建时的答卷快照返回；停领状态在 read_package
            # 内校验（同请求键重试也不能绕过停领）。
            result = read_package(conn, existing["package_id"])
            result["idempotent"] = True
            conn.execute("COMMIT")
            return result

        candidates = _candidates(conn, chain, selected)
        root, leaves = _build_tree(group_tree)
        chosen_nodes = _solve_grouping(root, leaves, candidates, k)
        delivered = _assign_members(candidates, chosen_nodes)
        if not delivered:
            raise DeliveryError(
                "没有可在最小人数 k=%d 下交付的候选成员" % k)

        package_id = _new_package_id()
        conn.execute(
            "INSERT INTO delivery_package(package_id, request_key,"
            " spec_fingerprint, endpoint_plan_id, min_members,"
            " plan_chain_json, group_tree_json, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (package_id, request_key, fp, binding["endpoint_plan_id"], k,
             json.dumps(selected_plan_ids),
             json.dumps(_canonical_tree(group_tree), ensure_ascii=False),
             _now_iso()),
        )
        for chain_idx in selected:
            conn.execute(
                "INSERT INTO delivery_package_plan(package_id, plan_id,"
                " chain_index) VALUES (?,?,?)",
                (package_id, chain[chain_idx]["plan_id"], chain_idx),
            )
        used_codes = set()
        for member_id, node, answers in delivered:
            code = _new_code()
            while code in used_codes:
                code = _new_code()
            used_codes.add(code)
            conn.execute(
                "INSERT INTO delivery_member(package_id, member_ref, code,"
                " group_name, group_depth, answers_json)"
                " VALUES (?,?,?,?,?,?)",
                (package_id, member_id, code, node.label, node.depth,
                 json.dumps(answers)),
            )
        result = read_package(conn, package_id)
        result["idempotent"] = False
        conn.execute("COMMIT")
        return result
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ================================================================ 停领与领取
def _assert_not_stopped(conn, package_id):
    if conn.execute(
        "SELECT 1 FROM delivery_revocation WHERE package_id=? LIMIT 1",
        (package_id,)).fetchone() is not None:
        raise DeliveryError(
            "交付包 %s 已停止领取：有已交付成员在选定计划的授权被撤回；"
            "如需出库请使用新请求键重新计算" % package_id)


def read_package(conn, package_id):
    """在**调用方当前事务**内校验停领并返回课题组可见答卷。

    只返回：包标识、轮次顺序（按选定计划链序的 wave）、每个交付成员的一条
    ``{code, group, answers}``；不含内部标识、原层或被排除者信息。
    """
    pkg = conn.execute(
        "SELECT package_id FROM delivery_package WHERE package_id=?",
        (package_id,)).fetchone()
    if pkg is None:
        raise DeliveryError("交付包不存在: %s" % package_id)
    _assert_not_stopped(conn, package_id)
    rounds = [r[0] for r in conn.execute(
        "SELECT pp.wave FROM delivery_package_plan dpp"
        " JOIN panel_plan pp ON pp.plan_id=dpp.plan_id"
        " WHERE dpp.package_id=? ORDER BY dpp.chain_index", (package_id,))]
    members = [
        {"code": r["code"], "group": r["group_name"],
         "answers": json.loads(r["answers_json"])}
        for r in conn.execute(
            "SELECT code, group_name, answers_json FROM delivery_member"
            " WHERE package_id=? ORDER BY group_name, code", (package_id,))
    ]
    return {"package_id": package_id, "rounds": rounds, "members": members}


def claim_package(conn, package_id):
    """课题组领取入口：只按包标识返回答卷；已停领的包一律拒绝。

    读取发生在单个 ``BEGIN IMMEDIATE`` 事务内（拿到保留锁后再校验停领并
    读取成员），因此与撤回事务互斥：领取结果只能对应某个完整且仍可领取
    的版本，不会读到“撤回已提交、停领标记未写”的中间状态。
    """
    if not isinstance(package_id, str) or not package_id:
        raise DeliveryError("package_id 必须是非空字符串")
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = read_package(conn, package_id)
        conn.execute("COMMIT")
        return result
    except Exception:
        conn.execute("ROLLBACK")
        raise


def list_packages(conn):
    """管理员核对用：包清单（规格、组成员数、是否停领；无内部成员标识）。"""
    plans_by_pkg, groups_by_pkg = {}, {}
    for r in conn.execute(
        "SELECT package_id, plan_id FROM delivery_package_plan"
        " ORDER BY package_id, chain_index"):
        plans_by_pkg.setdefault(r["package_id"], []).append(r["plan_id"])
    for r in conn.execute(
        "SELECT package_id, group_name, COUNT(*) n FROM delivery_member"
        " GROUP BY package_id, group_name ORDER BY group_name"):
        groups_by_pkg.setdefault(r["package_id"], []).append(
            {"group": r["group_name"], "size": r["n"]})
    out = []
    for r in conn.execute(
            "SELECT * FROM delivery_package ORDER BY created_at, package_id"):
        d = dict(r)
        d["plan_ids"] = plans_by_pkg.get(d["package_id"], [])
        d["group_tree"] = json.loads(d.pop("group_tree_json"))
        d["plan_chain"] = json.loads(d.pop("plan_chain_json"))
        d["groups"] = groups_by_pkg.get(d["package_id"], [])
        d["stopped"] = conn.execute(
            "SELECT 1 FROM delivery_revocation WHERE package_id=? LIMIT 1",
            (d["package_id"],)).fetchone() is not None
        d["delivered"] = sum(g["size"] for g in d["groups"])
        out.append(d)
    return out
