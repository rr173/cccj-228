# -*- coding: utf-8 -*-
"""双进程交错装配测试：运行 `python -I tests/test_concurrency.py`。

构造一个缺口 2、候选 4 的层，两个进程在 barrier 同步后同时装配：
- 若双重分配发生，数据库部分唯一索引会拒绝其中一个事务，该进程整批重试；
- 最终两个批次占用的替补 frame 集合必须互不相交，且无 open 缺口。
"""

import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from sample_assembler import SampleAssembler  # noqa: E402

RULES = {
    "name": "concurrency_rules",
    "strata": [
        {"code": "S1", "ordinal": 0, "target_n": 2, "capacity": 10,
         "field_type": "categorical", "values": ["A"]},
    ],
}


def make_units():
    # C1/C2 抽中后拒绝；C3..C6 是候选；C7 拒绝状态、C8 不可联系用于排除检查
    rows = []
    for i in range(1, 9):
        fid = "C%d" % i
        rows.append({
            "frame_id": fid, "frame_version": "vc",
            "business_unit_id": None,
            "stratum_value": "A", "is_missing": False,
            "contactable": fid not in ("C8",),
            "random_rank": i / 10.0, "inclusion_prob": 0.1,
        })
    return rows


DRAWS = [
    {"sample_id": "DC1", "frame_id": "C1", "frame_version": "vc",
     "target_stratum": "S1", "random_rank": 0.1,
     "selection_position": 1, "inclusion_prob": 0.1},
    {"sample_id": "DC2", "frame_id": "C2", "frame_version": "vc",
     "target_stratum": "S1", "random_rank": 0.2,
     "selection_position": 2, "inclusion_prob": 0.1},
]

EVENTS = [
    {"event_id": "EC1", "frame_id": "C1", "event_type": "refused",
     "event_time": "2026-11-01T09:00:00Z", "source_seq": 1},
    {"event_id": "EC2", "frame_id": "C2", "event_type": "refused",
     "event_time": "2026-11-01T09:01:00Z", "source_seq": 2},
    {"event_id": "EC7", "frame_id": "C7", "event_type": "refused",
     "event_time": "2026-11-01T09:02:00Z", "source_seq": 3},
]


def main():
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "conc.db")
        marker_dir = os.path.join(d, "markers")
        os.makedirs(marker_dir)
        asm = SampleAssembler(db_path)
        asm.compile_and_store_rules(RULES)
        asm.import_frame_snapshot("vc", make_units())
        asm.import_draws(DRAWS)
        for ev in EVENTS:
            asm.record_event(ev)
        asm.close()

        env = dict(os.environ, PYTHONPATH=ROOT)
        procs = []
        for token in ("p1", "p2"):
            p = subprocess.Popen(
                [sys.executable, "-I",
                 os.path.join(HERE, "_concurrent_worker.py"),
                 db_path, "vc", marker_dir, token],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True,
            )
            procs.append(p)
        outputs, errors = [], []
        for p in procs:
            out, err = p.communicate(timeout=60)
            outputs.append(out.strip())
            errors.append(err.strip())
            if p.returncode != 0:
                print("worker 失败:\n%s" % err)
                sys.exit(1)
        for line in outputs:
            print("  " + line)

        # 重新打开数据库做最终核验（进程退出后占用仍持久）
        checker = SampleAssembler(db_path)
        batches = checker.list_batches()
        assert len(batches) == 2, "应有两个持久化批次，实际 %d" % len(batches)
        per_batch = {}
        for b in batches:
            rows = [r[0] for r in checker.conn.execute(
                "SELECT frame_id FROM assignment WHERE batch_id=? AND kind='substitute'"
                " ORDER BY frame_id", (b["batch_id"],))]
            per_batch[b["batch_id"]] = rows
            open_gaps = checker.conn.execute(
                "SELECT COUNT(*) FROM gap WHERE batch_id=? AND status='open'",
                (b["batch_id"],)).fetchone()[0]
            assert open_gaps == 0, "批次 %s 存在 open 缺口" % b["batch_id"]
            assert len(rows) == 2, "批次 %s 应各补 2 个，实际 %r" % (b["batch_id"], rows)

        sets = [set(v) for v in per_batch.values()]
        disjoint = sets[0].isdisjoint(sets[1])
        all_subs = sets[0] | sets[1]
        allowed = {"C3", "C4", "C5", "C6"}
        print("\n批次替补占用: %s" % per_batch)
        print("两批替补集合互不相交: %s" % disjoint)
        print("占用候选均在允许池 %s 内: %s" % (sorted(allowed), all_subs <= allowed))

        # 数据库层兜底核验：部分唯一索引保证全局替补 frame 唯一
        dup = checker.conn.execute(
            "SELECT frame_id, COUNT(*) c FROM assignment WHERE kind='substitute'"
            " GROUP BY frame_id HAVING c > 1"
        ).fetchall()
        print("数据库唯一索引检测到重复占用: %r" % dup)
        # 候选耗尽后再装配：应全部 unfillable，而不是重复占用
        third = checker.assemble("vc")
        assert third["gaps"] == {"filled": 0, "unfillable": 2, "open": 0}, third["gaps"]
        print("候选耗尽后的第三批: 两个缺口均 unfillable（不重复占用历史替补）")
        checker.close()

        ok = disjoint and not dup and all_subs <= allowed and not any(errors)
        print("\n并发测试: %s" % ("通过" if ok else "失败"))
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
