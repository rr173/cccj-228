# -*- coding: utf-8 -*-
"""并发装配 worker：由 tests/test_concurrency.py 以两个子进程启动。

参数：db_path frame_version barrier_path
两个进程在 barrier 处同步后同时调用 assemble，争抢同一候选池。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sample_assembler import SampleAssembler  # noqa: E402


def wait_barrier(marker_dir, token, timeout=30):
    """两阶段栅栏：两个 worker 各写一个 marker，互相看到对方后同时放行。"""
    deadline = time.time() + timeout
    my = os.path.join(marker_dir, "ready-%s" % token)
    with open(my, "w") as f:
        f.write(str(os.getpid()))
    while time.time() < deadline:
        markers = {n for n in os.listdir(marker_dir)
                   if n.startswith("ready-")}
        if len(markers) >= 2:
            return
        time.sleep(0.005)
    raise TimeoutError("barrier 等待超时")


def main():
    db_path, version, marker_dir, token = sys.argv[1:5]
    asm = SampleAssembler(db_path)
    wait_barrier(marker_dir, token)
    # 同步后同时发起装配，制造 BEGIN IMMEDIATE 锁竞争与候选争抢
    result = asm.assemble(version, note="concurrent %s" % version)
    rows = asm.conn.execute(
        "SELECT frame_id FROM assignment WHERE batch_id=? AND kind='substitute'"
        " ORDER BY frame_id", (result["batch_id"],)
    ).fetchall()
    print("%s batch=%s subs=%s"
          % (token, result["batch_id"], ",".join(r[0] for r in rows)))
    asm.close()


if __name__ == "__main__":
    main()
