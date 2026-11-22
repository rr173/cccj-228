# -*- coding: utf-8 -*-
"""panel 计划并发 worker：由 tests/test_panel.py 以两个子进程启动。

两个进程使用**相同** request_key 同时生成下一轮计划：request_key 唯一约束
保证只产生一个计划；后到者幂等返回同一计划，不重复占用候选。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sample_assembler import SampleAssembler  # noqa: E402


def wait_barrier(marker_dir, token, timeout=30):
    deadline = time.time() + timeout
    my = os.path.join(marker_dir, "ready-%s" % token)
    with open(my, "w") as f:
        f.write(str(os.getpid()))
    while time.time() < deadline:
        markers = {n for n in os.listdir(marker_dir) if n.startswith("ready-")}
        if len(markers) >= 2:
            return
        time.sleep(0.005)
    raise TimeoutError("barrier 等待超时")


def main():
    db_path, request_key, frame_version, marker_dir, token = sys.argv[1:6]
    asm = SampleAssembler(db_path)
    wait_barrier(marker_dir, token)
    result = asm.generate_next_plan(
        request_key, frame_version, rotation_rule="conc_rotation",
        note="concurrent %s" % token)
    joined = [r[0] for r in asm.conn.execute(
        "SELECT frame_id FROM panel_change WHERE plan_id=? AND decision='joined'"
        " ORDER BY frame_id", (result["plan_id"],))]
    print("%s plan=%s wave=%s idem=%s joined=%s"
          % (token, result["plan_id"], result["wave"],
             result["idempotent"], ",".join(joined)))
    asm.close()


if __name__ == "__main__":
    main()
