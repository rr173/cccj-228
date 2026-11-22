# -*- coding: utf-8 -*-
"""访谈回答转移表（随访谈修订增量维护，SQLite 持久化）。

分析员给定一个**已发布**的 panel 计划 ``plan_id`` 作为端点。只沿该计划的
``base_plan_id`` 回溯得到的已发布计划链（含端点）工作：

- 不纳入最初的单轮抽样批次（``base_batch_id`` / ``published_version``），
  不纳入同轮的其他计划（例如备选 draft），也不纳入端点之后发布的计划。
- 每个成员的在样记录取计划账本 ``panel_change.decision``：``retained`` 或
  ``joined`` 为在样；``exited`` 或链上缺轮为缺席，切断区段。层取**该计划
  账本行**的 ``stratum_code``，不读成员当前层。
- 访谈修订 ``(plan_id, panel_member_id, revision, answer)`` 只接受链上的
  在样记录；同一 (计划, 成员) 以最大修订号为准，空答案/最新修订作废不代表
  离开在样区段，只是不构成有效端点。
- 配对只发生在同一在样区段内**相邻的两个有效回答**之间，可以跨过仍在样但
  没有有效答案的轮次。汇总键 = (起轮, 止轮, 起点记录的层, 前答案, 后答案)。

维护策略（增量、事务化）：

- 每次录入一批修订，只对“该批使有效答案实际变化”的成员重推其区段配对；
  其他成员的访谈记录与配对明细不读、不重建。汇总表按键做旧值/新值差量更新。
- 修订、有效答案、配对明细、汇总与审计行在同一 ``BEGIN IMMEDIATE`` 事务内
  提交；任何坏输入/冲突整批回滚，不留下部分修改。
"""

import json
from collections import Counter, OrderedDict

from .errors import AnswerTransferError

_ANSWER_OK = (0, 1)


def _now_iso():
    # 保持与 assembler/panel 一致的 UTC 文本时间
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ================================================================ 绑定与初始化
def initialize(conn, endpoint_plan_id):
    """绑定端点计划并构建计划链/区段；已有绑定时必须使用同一端点。

    同一端点重复初始化不清空任何已录入修订、配对明细或汇总：只是幂等地
    补齐链上结构并对全部成员做一次差量重推（结果不变时零写入）。

    返回 {"endpoint_plan_id", "endpoint_wave", "chain": [...], "members",
          "segments", "pairs", "idempotent"}。
    """
    if not isinstance(endpoint_plan_id, int) or isinstance(endpoint_plan_id, bool):
        raise AnswerTransferError("endpoint plan_id 必须是整数")
    chain = _resolve_published_chain(conn, endpoint_plan_id)

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO answer_transfer_binding(id, endpoint_plan_id,"
            " endpoint_wave, created_at) VALUES (1,?,?,?)"
            " ON CONFLICT(id) DO NOTHING",
            (endpoint_plan_id, chain[-1]["wave"], _now_iso()),
        )
        bound = conn.execute(
            "SELECT endpoint_plan_id FROM answer_transfer_binding WHERE id=1"
        ).fetchone()
        if bound["endpoint_plan_id"] != endpoint_plan_id:
            raise AnswerTransferError(
                "该库已绑定端点计划 %s，不能改用 %s"
                % (bound["endpoint_plan_id"], endpoint_plan_id)
            )
        existing_binding = conn.execute(
            "SELECT COUNT(*) FROM answer_transfer_chain").fetchone()[0] > 0
        _store_chain(conn, chain)
        members, structure_changed = _store_samples(conn, chain)
        if existing_binding and not structure_changed:
            # 同一端点重开：结构未变，直接沿用已持久化的配对（不清空、不重推）。
            pairs = conn.execute(
                "SELECT COUNT(*) FROM answer_transfer_pair").fetchone()[0]
        else:
            pairs = 0
            for mid in members:
                n_pairs, _n_segments = _refresh_member(conn, mid)
                pairs += n_pairs
        info = _binding_info(conn, chain, members, pairs)
        info["idempotent"] = bool(existing_binding)
        conn.execute("COMMIT")
        return info
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _resolve_published_chain(conn, endpoint_plan_id):
    """沿 base_plan_id 回溯，返回按时间正序的已发布计划链（含端点）。"""
    endpoint = conn.execute(
        "SELECT * FROM panel_plan WHERE plan_id=?", (endpoint_plan_id,)
    ).fetchone()
    if endpoint is None:
        raise AnswerTransferError("端点计划不存在: %s" % endpoint_plan_id)
    if endpoint["status"] != "published":
        raise AnswerTransferError(
            "端点计划 %s 未发布（只能使用已发布的计划链）" % endpoint_plan_id
        )

    chain, seen = [], set()
    row = endpoint
    while row is not None:
        pid = row["plan_id"]
        if pid in seen:
            raise AnswerTransferError("计划链存在环: plan_id=%s" % pid)
        seen.add(pid)
        if row["status"] != "published":
            raise AnswerTransferError(
                "计划 %s 在链上但未发布；已发布链被破坏" % pid
            )
        chain.append({"plan_id": pid, "wave": row["wave"],
                      "base_plan_id": row["base_plan_id"]})
        if row["base_plan_id"] is None:
            break
        row = conn.execute(
            "SELECT * FROM panel_plan WHERE plan_id=?", (row["base_plan_id"],)
        ).fetchone()
        if row is None:
            raise AnswerTransferError(
                "计划链指向不存在的 base_plan_id=%s"
                % chain[-1]["base_plan_id"]
            )
    chain.reverse()  # 时间正序：最早 -> 端点
    for i in range(1, len(chain)):
        if chain[i]["base_plan_id"] != chain[i - 1]["plan_id"]:
            raise AnswerTransferError(
                "计划链不连续：%s 的 base_plan_id=%s 不是前一轮 %s"
                % (chain[i]["plan_id"], chain[i]["base_plan_id"],
                   chain[i - 1]["plan_id"])
            )
    return chain


def _store_chain(conn, chain):
    for idx, node in enumerate(chain):
        conn.execute(
            "INSERT INTO answer_transfer_chain(plan_id, wave, chain_index,"
            " base_plan_id) VALUES (?,?,?,?)"
            " ON CONFLICT(plan_id) DO UPDATE SET wave=excluded.wave,"
            " chain_index=excluded.chain_index, base_plan_id=excluded.base_plan_id",
            (node["plan_id"], node["wave"], idx, node["base_plan_id"]),
        )


def _store_samples(conn, chain):
    """把链上每个在样账本行落成区段记录；返回 (成员 id 列表, 结构是否变化)。

    区段 = 同一成员沿计划链**连续出现**为 retained/joined 的轮次；任意缺席
    （该计划账本无该成员）或 exited 都切断区段，之后重新加入另起区段。
    """
    by_member = OrderedDict()
    for idx, node in enumerate(chain):
        for r in conn.execute(
            "SELECT panel_member_id, stratum_code FROM panel_change"
            " WHERE plan_id=? AND decision IN ('retained','joined')"
            " ORDER BY panel_member_id", (node["plan_id"],)
        ):
            by_member.setdefault(r["panel_member_id"], []).append(
                (idx, node["plan_id"], node["wave"], r["stratum_code"])
            )

    previous = {
        (r["plan_id"], r["panel_member_id"]):
            (r["wave"], r["chain_index"], r["stratum_code"], r["segment_no"])
        for r in conn.execute(
            "SELECT plan_id, panel_member_id, wave, chain_index,"
            " stratum_code, segment_no FROM answer_transfer_sample")
    }
    seen_keys, structure_changed = set(), False
    member_order = []
    for mid, rows in by_member.items():
        member_order.append(mid)
        rows.sort(key=lambda x: x[0])
        seg, prev_idx = 0, None
        for idx, pid, wave, stratum in rows:
            if prev_idx is None or idx != prev_idx + 1:
                seg += 1
            prev_idx = idx
            new_vals = (wave, idx, stratum, seg)
            if previous.get((pid, mid)) != new_vals:
                structure_changed = True
            seen_keys.add((pid, mid))
            conn.execute(
                "INSERT INTO answer_transfer_sample(plan_id, panel_member_id,"
                " wave, chain_index, stratum_code, segment_no)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(plan_id, panel_member_id) DO UPDATE SET"
                " wave=excluded.wave, chain_index=excluded.chain_index,"
                " stratum_code=excluded.stratum_code,"
                " segment_no=excluded.segment_no",
                (pid, mid, wave, idx, stratum, seg),
            )
    if seen_keys != set(previous):
        structure_changed = True
    return member_order, structure_changed


def _binding_info(conn, chain, members, pairs):
    segments = conn.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT panel_member_id, segment_no"
        " FROM answer_transfer_sample)"
    ).fetchone()[0]
    return {
        "endpoint_plan_id": chain[-1]["plan_id"],
        "endpoint_wave": chain[-1]["wave"],
        "chain": [{"plan_id": n["plan_id"], "wave": n["wave"]}
                  for n in chain],
        "members": len(members),
        "segments": segments,
        "pairs": pairs,
    }


# ================================================================ 修订录入
def _validate_revision(item, index):
    if not isinstance(item, dict):
        raise AnswerTransferError("第 %d 条修订必须是 dict" % index)
    pid = item.get("plan_id")
    mid = item.get("panel_member_id")
    rev = item.get("revision")
    if not isinstance(pid, int) or isinstance(pid, bool):
        raise AnswerTransferError("第 %d 条修订 plan_id 必须是整数" % index)
    if not isinstance(mid, str) or not mid:
        raise AnswerTransferError("第 %d 条修订 panel_member_id 必须是非空字符串" % index)
    if not isinstance(rev, int) or isinstance(rev, bool) or rev <= 0:
        raise AnswerTransferError("第 %d 条修订 revision 必须是正整数" % index)
    if "answer" not in item:
        raise AnswerTransferError("第 %d 条修订缺少 answer（作废请显式给 None）" % index)
    answer = item["answer"]
    if answer is not None:
        if isinstance(answer, bool) or not isinstance(answer, int) \
                or answer not in _ANSWER_OK:
            raise AnswerTransferError(
                "第 %d 条修订 answer 必须是 0、1 或 None（作废）" % index
            )
    return pid, mid, rev, answer


def submit_revisions(conn, revisions):
    """录入一批访谈修订并增量维护配对/汇总；坏输入或冲突整批不生效。

    revisions: [{plan_id, panel_member_id, revision, answer}, ...]
    answer: 0 / 1，或 None 表示该修订作废。

    - 只接受绑定计划链上的在样记录；不在链上/不在样 -> 整批拒绝。
    - 同键同修订号同答案可重送（冗余，不重复落库）；同键同修订号不同答案
      -> 整批拒绝（即使该修订已不是最新）。
    - 批内顺序无关；同批同键同号多答案冲突 -> 整批拒绝。
    - 晚到的旧修订（号小于当前最大）只入库，不改变有效答案与配对。
    """
    if not isinstance(revisions, list):
        raise AnswerTransferError("revisions 必须是列表")
    if not revisions:
        raise AnswerTransferError("revisions 不能为空批次")

    parsed = [_validate_revision(item, i) for i, item in enumerate(revisions)]

    # 批内去重/冲突（与到达顺序无关）
    keyed = OrderedDict()
    conflicts = []
    for pid, mid, rev, answer in parsed:
        key = (pid, mid, rev)
        if key in keyed:
            if keyed[key] != answer:
                conflicts.append(key)
        else:
            keyed[key] = answer
    if conflicts:
        raise AnswerTransferError(
            "批内同一修订号出现不同答案（整批无效）: %s"
            % ", ".join("plan=%s member=%s rev=%s" % k for k in sorted(conflicts))
        )

    conn.execute("BEGIN IMMEDIATE")
    try:
        binding = conn.execute(
            "SELECT endpoint_plan_id FROM answer_transfer_binding WHERE id=1"
        ).fetchone()
        if binding is None:
            raise AnswerTransferError("尚未初始化回答转移表（先绑定端点计划）")

        # 只接受链上在样记录；同时取当前有效答案用于冲突判断与差量。
        now = _now_iso()
        stored = 0
        duplicates = 0
        affected = set()
        inserted_keys = []
        for (pid, mid, rev), answer in keyed.items():
            sample = conn.execute(
                "SELECT 1 FROM answer_transfer_sample"
                " WHERE plan_id=? AND panel_member_id=?", (pid, mid)
            ).fetchone()
            if sample is None:
                raise AnswerTransferError(
                    "修订指向非绑定链在样记录: plan_id=%s panel_member_id=%s"
                    % (pid, mid)
                )
            old = conn.execute(
                "SELECT revision, answer FROM answer_effective"
                " WHERE plan_id=? AND panel_member_id=?", (pid, mid)
            ).fetchone()
            if old is not None and old["revision"] == rev:
                if old["answer"] != answer:
                    raise AnswerTransferError(
                        "修订冲突：plan=%s member=%s revision=%s 已存答案 %r，"
                        "新答案 %r（整批无效）"
                        % (pid, mid, rev, old["answer"], answer)
                    )
                duplicates += 1
                continue
            prev = conn.execute(
                "SELECT answer FROM answer_revision"
                " WHERE plan_id=? AND panel_member_id=? AND revision=?",
                (pid, mid, rev),
            ).fetchone()
            if prev is not None:
                # 理论上与 answer_effective 同号异答已被拦截；这里兜底旧行。
                if prev["answer"] != answer:
                    raise AnswerTransferError(
                        "修订冲突：plan=%s member=%s revision=%s 已存答案 %r，"
                        "新答案 %r（整批无效）"
                        % (pid, mid, rev, prev["answer"], answer)
                    )
                duplicates += 1
                continue
            conn.execute(
                "INSERT INTO answer_revision(plan_id, panel_member_id, revision,"
                " answer, recorded_at) VALUES (?,?,?,?,?)",
                (pid, mid, rev, answer, now),
            )
            stored += 1
            inserted_keys.append((pid, mid, rev))
            effective = conn.execute(
                "SELECT revision, answer FROM answer_effective"
                " WHERE plan_id=? AND panel_member_id=?", (pid, mid)
            ).fetchone()
            old_answer = effective["answer"] if effective is not None else None
            if effective is None:
                conn.execute(
                    "INSERT INTO answer_effective(plan_id, panel_member_id,"
                    " revision, answer) VALUES (?,?,?,?)",
                    (pid, mid, rev, answer),
                )
            elif rev > effective["revision"]:
                # 只有最大修订号推进才可能改变有效答案；晚到的旧修订只入库。
                conn.execute(
                    "UPDATE answer_effective SET revision=?, answer=?"
                    " WHERE plan_id=? AND panel_member_id=?",
                    (rev, answer, pid, mid),
                )
            else:
                # 旧修订号：落库留证，但不影响有效答案与配对。
                continue
            if old_answer != answer:
                # 只有“有效答案实际变化”的成员才重推区段配对。
                affected.add(mid)

        pair_total, seg_total = 0, 0
        affected_json = []
        for mid in sorted(affected):
            n_pairs, n_segments = _refresh_member(conn, mid)
            pair_total += n_pairs
            seg_total += n_segments
            affected_json.append({
                "panel_member_id": mid,
                "segments": n_segments,
                "pairs": n_pairs,
            })

        # 审计行：只记录本批实际影响的成员区段数；不含全部配对明细。
        conn.execute(
            "INSERT INTO answer_revision_batch(endpoint_plan_id, received_count,"
            " unique_count, stored_count, affected_members, affected_segments,"
            " pair_count, affected_json, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (binding["endpoint_plan_id"], len(revisions), len(keyed), stored,
             len(affected), seg_total, pair_total,
             json.dumps(affected_json, ensure_ascii=False), now),
        )
        batch_id = conn.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]
        for pid, mid, rev in inserted_keys:
            conn.execute(
                "INSERT INTO answer_revision_batch_item(batch_id, plan_id,"
                " panel_member_id, revision) VALUES (?,?,?,?)",
                (batch_id, pid, mid, rev),
            )
        conn.execute("COMMIT")
        return {
            "batch_id": batch_id,
            "received": len(revisions),
            "unique": len(keyed),
            "stored": stored,
            "duplicate": duplicates,
            "affected_members": sorted(affected),
            "affected_segments": seg_total,
            "pairs": pair_total,
            "stored_keys": inserted_keys,
        }
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ================================================================ 区段配对重推
def _refresh_member(conn, panel_member_id):
    """只重推该成员自己的区段配对，并对其汇总键做差量更新。

    - 读取范围严格限定 ``panel_member_id``：不扫描其他成员的访谈记录。
    - 先取出该成员现有的配对明细作为旧计数，删除后重算，再按差额更新
      ``answer_transfer_summary``；不重建整表。
    返回 (新配对数, 涉及区段数)。
    """
    old_counter = Counter()
    for r in conn.execute(
        "SELECT from_wave, to_wave, stratum_code, from_answer, to_answer"
        " FROM answer_transfer_pair WHERE panel_member_id=?", (panel_member_id,)
    ):
        old_counter[(r["from_wave"], r["to_wave"], r["stratum_code"],
                     r["from_answer"], r["to_answer"])] += 1
    conn.execute(
        "DELETE FROM answer_transfer_pair WHERE panel_member_id=?",
        (panel_member_id,),
    )

    rows = conn.execute(
        "SELECT s.plan_id, s.wave, s.chain_index, s.stratum_code, s.segment_no,"
        " e.answer FROM answer_transfer_sample s"
        " LEFT JOIN answer_effective e ON e.plan_id=s.plan_id"
        " AND e.panel_member_id=s.panel_member_id"
        " WHERE s.panel_member_id=? ORDER BY s.chain_index", (panel_member_id,)
    ).fetchall()

    new_counter = Counter()
    segments = OrderedDict()
    for r in rows:
        segments.setdefault(r["segment_no"], []).append(r)

    for seg in segments.values():
        valid = [r for r in seg if r["answer"] is not None]
        for a, b in zip(valid, valid[1:]):
            key = (a["wave"], b["wave"], a["stratum_code"],
                   a["answer"], b["answer"])
            new_counter[key] += 1
            conn.execute(
                "INSERT INTO answer_transfer_pair(panel_member_id, segment_no,"
                " from_plan_id, from_wave, to_plan_id, to_wave, stratum_code,"
                " from_answer, to_answer) VALUES (?,?,?,?,?,?,?,?,?)",
                (panel_member_id, a["segment_no"], a["plan_id"], a["wave"],
                 b["plan_id"], b["wave"], a["stratum_code"], a["answer"],
                 b["answer"]),
            )

    _apply_summary_delta(conn, old_counter, new_counter)
    return sum(new_counter.values()), len(segments)


def _apply_summary_delta(conn, old_counter, new_counter):
    for key in set(old_counter) | set(new_counter):
        delta = new_counter.get(key, 0) - old_counter.get(key, 0)
        if delta == 0:
            continue
        row = conn.execute(
            "SELECT pair_count FROM answer_transfer_summary WHERE"
            " start_wave=? AND end_wave=? AND stratum_code=?"
            " AND from_answer=? AND to_answer=?", key
        ).fetchone()
        current = row["pair_count"] if row is not None else 0
        updated = current + delta
        if updated < 0:
            raise AnswerTransferError(
                "汇总差量为负（内部不一致）: key=%r current=%d delta=%d"
                % (key, current, delta)
            )
        if updated == 0:
            conn.execute(
                "DELETE FROM answer_transfer_summary WHERE start_wave=?"
                " AND end_wave=? AND stratum_code=? AND from_answer=?"
                " AND to_answer=?", key,
            )
        elif row is None:
            conn.execute(
                "INSERT INTO answer_transfer_summary(start_wave, end_wave,"
                " stratum_code, from_answer, to_answer, pair_count)"
                " VALUES (?,?,?,?,?,?)", key + (updated,),
            )
        else:
            conn.execute(
                "UPDATE answer_transfer_summary SET pair_count=? WHERE"
                " start_wave=? AND end_wave=? AND stratum_code=?"
                " AND from_answer=? AND to_answer=?",
                (updated,) + key,
            )


# ================================================================ 读取接口
def read_summary(conn):
    """直接读取持久化汇总；不重跑任何访谈配对。"""
    return [dict(r) for r in conn.execute(
        "SELECT start_wave, end_wave, stratum_code, from_answer, to_answer,"
        " pair_count FROM answer_transfer_summary"
        " ORDER BY start_wave, end_wave, stratum_code, from_answer, to_answer"
    )]


def get_binding(conn):
    row = conn.execute(
        "SELECT * FROM answer_transfer_binding WHERE id=1"
    ).fetchone()
    if row is None:
        return None
    chain = [dict(r) for r in conn.execute(
        "SELECT plan_id, wave, chain_index, base_plan_id"
        " FROM answer_transfer_chain ORDER BY chain_index"
    )]
    return {"endpoint_plan_id": row["endpoint_plan_id"],
            "endpoint_wave": row["endpoint_wave"],
            "created_at": row["created_at"], "chain": chain}


def list_pairs(conn, panel_member_id=None):
    sql = ("SELECT * FROM answer_transfer_pair")
    params = ()
    if panel_member_id is not None:
        sql += " WHERE panel_member_id=?"
        params = (panel_member_id,)
    sql += " ORDER BY panel_member_id, segment_no, from_plan_id, to_plan_id"
    return [dict(r) for r in conn.execute(sql, params)]


def list_segments(conn, panel_member_id=None):
    sql = ("SELECT s.panel_member_id, s.segment_no, s.plan_id, s.wave,"
           " s.chain_index, s.stratum_code, e.revision, e.answer"
           " FROM answer_transfer_sample s"
           " LEFT JOIN answer_effective e ON e.plan_id=s.plan_id"
           " AND e.panel_member_id=s.panel_member_id")
    params = ()
    if panel_member_id is not None:
        sql += " WHERE s.panel_member_id=?"
        params = (panel_member_id,)
    sql += " ORDER BY s.panel_member_id, s.segment_no, s.chain_index"
    return [dict(r) for r in conn.execute(sql, params)]


def list_batches(conn):
    out = []
    for r in conn.execute(
        "SELECT * FROM answer_revision_batch ORDER BY batch_id"
    ):
        d = dict(r)
        d["stored_items"] = [
            dict(x) for x in conn.execute(
                "SELECT plan_id, panel_member_id, revision"
                " FROM answer_revision_batch_item WHERE batch_id=?"
                " ORDER BY plan_id, panel_member_id, revision",
                (d["batch_id"],),
            )
        ]
        out.append(d)
    return out
